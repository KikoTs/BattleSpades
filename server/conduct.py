"""Player conduct: team-grief accounting and AFK kicks.

Both run on the gameplay thread and need no packets beyond the retail
``ChatMessage`` (system lane, private warnings) and the native disconnect
reasons ``ERROR_KICK_GRIEFING`` / ``ERROR_AFK_TIMEOUT``.

Team griefing
-------------
Every HP a player removes from a *teammate* earns grief points, and a team
kill earns a bonus.  Harm counts whether it is direct (bullet, melee, own
grenade with friendly fire on) or indirect: a player who shoots or blasts a
teammate's landmine is the *instigator* of that mine's explosion, so the
owner's resulting "suicide" (and any teammate hurt by it) is charged to the
instigator -- that works even with friendly fire off, where the owner is the
only teammate the blast can hurt.  :func:`blast_instigator` carries the
instigator through the synchronous blast, including chained mines.

Points decay linearly (one point per ``grief_decay_seconds``); one burst
(e.g. a single grenade into a crowd) is capped at
``grief_incident_max_points`` so an accident alone never reaches the kick
threshold.  The player is warned privately at ``grief_warn_points`` and
kicked at ``grief_kick_points``.

AFK
---
Only real input counts: a change of movement keys, fire/aim/hover keys, or a
visible orientation change.  The retail client streams identical ClientData
while idle, so mere packet arrival is not activity.  The idle clock pauses
while the player loads, is dead, or the round is not running (end screen,
pre-round), so nobody is kicked for waiting on the server.
"""

from __future__ import annotations

import logging
import math
import time
from contextlib import contextmanager
from dataclasses import dataclass

import shared.constants as C

logger = logging.getLogger("conduct")

# Action-flag indices from ClientData that only change on user input:
# primary, secondary, zoom, hover.  can_pickup / is_on_fire / display flags
# flip on their own as the world changes around an idle player.
_ACTIVE_ACTION_INDICES = (0, 1, 2, 7)
_ORIENTATION_EPSILON = 0.02

# (server, instigator) for the explosion currently being applied; nested
# chain detonations keep the outermost instigator.
_blast_stack: list[tuple[object, object]] = []


@dataclass
class _Defaults:
    grief_kick_enabled: bool = True
    grief_kick_points: float = 10.0
    grief_warn_points: float = 5.0
    grief_decay_seconds: float = 60.0
    grief_team_kill_points: float = 3.0
    grief_team_damage_points: float = 1.0
    grief_incident_seconds: float = 3.0
    grief_incident_max_points: float = 6.0
    grief_exempt_admins: bool = True
    afk_kick_seconds: float = 600.0
    afk_warn_seconds: float = 540.0
    afk_spectator_kick_seconds: float = 1800.0
    afk_exempt_admins: bool = True
    announce_kicks: bool = True


_DEFAULTS = _Defaults()


def setting(server, name: str):
    """A ``[conduct]`` value with the built-in default as fallback."""

    config = getattr(getattr(server, "config", None), "conduct", None)
    default = getattr(_DEFAULTS, name)
    if config is None:
        return default
    return getattr(config, name, default)


def _server_of(player):
    return getattr(getattr(player, "connection", None), "server", None)


def _is_human(player) -> bool:
    return (
        player is not None
        and not bool(getattr(player, "is_bot", False))
        and getattr(player, "connection", None) is not None
    )


def _send_private(player, text: str) -> None:
    send = getattr(player, "send", None)
    if not callable(send):
        return
    try:
        from shared.packet import ChatMessage

        packet = ChatMessage()
        packet.player_id = 255
        packet.chat_type = int(getattr(C, "CHAT_SYSTEM", 2))
        packet.value = str(text)[:200]
        send(bytes(packet.generate()))
    except Exception:
        logger.debug("conduct private message failed", exc_info=True)


def _announce(server, text: str) -> None:
    if not bool(setting(server, "announce_kicks")):
        return
    broadcast = getattr(server, "broadcast", None)
    if not callable(broadcast):
        return
    try:
        from shared.packet import ChatMessage

        packet = ChatMessage()
        packet.player_id = 255
        packet.chat_type = int(getattr(C, "CHAT_SYSTEM", 2))
        packet.value = str(text)[:200]
        broadcast(bytes(packet.generate()))
    except Exception:
        logger.debug("conduct announcement failed", exc_info=True)


# --------------------------------------------------------------------------
# Team griefing
# --------------------------------------------------------------------------

@dataclass
class GriefState:
    points: float = 0.0
    stamp: float | None = None
    warned: bool = False
    incident_until: float = 0.0
    incident_points: float = 0.0
    kick_pending: bool = False
    team_kills: int = 0
    team_damage: int = 0


def grief_state(player) -> GriefState:
    state = getattr(player, "_conduct_grief", None)
    if isinstance(state, GriefState):
        return state
    state = GriefState()
    try:
        player._conduct_grief = state
    except AttributeError:
        pass
    return state


def grief_points(player, now: float | None = None) -> float:
    """Current (decayed) grief points; also advances the stored decay."""

    state = grief_state(player)
    current = time.monotonic() if now is None else float(now)
    server = _server_of(player)
    decay_seconds = float(setting(server, "grief_decay_seconds"))
    elapsed = 0.0 if state.stamp is None else max(0.0, current - state.stamp)
    if decay_seconds > 0.0 and elapsed > 0.0:
        state.points = max(0.0, state.points - elapsed / decay_seconds)
    state.stamp = current
    if state.points < float(setting(server, "grief_warn_points")) * 0.5:
        state.warned = False
    return state.points


@contextmanager
def blast_instigator(server, instigator):
    """Mark ``instigator`` as the player who set off the current explosion.

    Nested (chain) detonations inherit the outermost instigator: a teammate
    who shoots one mine of a cluster instigated the whole chain.
    """

    current = current_blast_instigator(server)
    _blast_stack.append((server, current if current is not None else instigator))
    try:
        yield
    finally:
        _blast_stack.pop()


def current_blast_instigator(server):
    for owner, instigator in reversed(_blast_stack):
        if owner is server:
            return instigator
    return None


def attribute_damage_source(server, victim, source):
    """Return the player a blast's damage to ``victim`` is charged to.

    Retail credits a deployable's explosion to its owner.  When a teammate
    of the victim *set it off* (shot the owner's mine), the damage is theirs:
    the kill feed and team-kill scoring name the instigator instead of
    recording a suicide for the owner.  Enemy-instigated blasts keep the
    owner's credit.
    """

    instigator = current_blast_instigator(server)
    if (
        instigator is None
        or instigator is source
        or instigator is victim
        or source is None
        or getattr(instigator, "team", None) != getattr(victim, "team", None)
    ):
        return source
    return instigator


def _grief_exempt(server, player) -> bool:
    if not _is_human(player):
        return True
    if bool(getattr(getattr(server, "mode", None), "grief_exempt", False)):
        return True
    if bool(setting(server, "grief_exempt_admins")) and bool(
        getattr(player, "admin", False)
    ):
        return True
    team = getattr(player, "team", None)
    return team is None or int(team) == int(C.TEAM_SPECTATOR)


def record_team_harm(
    server,
    victim,
    source,
    amount: int,
    killed: bool,
    kill_type: int = 0,
    *,
    now: float | None = None,
) -> float:
    """Charge ``source`` for HP removed from a teammate; return points added."""

    if (
        server is None
        or source is None
        or victim is None
        or source is victim
        or getattr(source, "team", None) != getattr(victim, "team", None)
        or int(kill_type) in {
            int(C.FORCED_TEAM_CHANGE_KILL),
            int(C.TEAM_CHANGE_KILL),
            int(C.CLASS_CHANGE_KILL),
        }
        or _grief_exempt(server, source)
    ):
        return 0.0
    amount = max(0, int(amount))
    if amount <= 0 and not killed:
        return 0.0
    current = time.monotonic() if now is None else float(now)
    state = grief_state(source)
    grief_points(source, current)
    added = amount / 100.0 * float(setting(server, "grief_team_damage_points"))
    if killed:
        added += float(setting(server, "grief_team_kill_points"))
        state.team_kills += 1
    state.team_damage += amount

    # One burst (a single grenade/mine into a group) is capped so an
    # accident alone can never reach the kick threshold.
    cap = float(setting(server, "grief_incident_max_points"))
    if not (state.incident_until - float(
        setting(server, "grief_incident_seconds")
    ) <= current <= state.incident_until):
        state.incident_until = current + float(
            setting(server, "grief_incident_seconds")
        )
        state.incident_points = 0.0
    if cap > 0.0:
        added = max(0.0, min(added, cap - state.incident_points))
    state.incident_points += added
    state.points += added

    instigated = current_blast_instigator(server) is source
    logger.info(
        "conduct team-harm source=%s name=%r victim=%s name=%r hp=%d killed=%s "
        "kill_type=%d indirect=%s +%.2f points=%.2f",
        getattr(source, "id", "?"), getattr(source, "name", ""),
        getattr(victim, "id", "?"), getattr(victim, "name", ""),
        amount, bool(killed), int(kill_type), instigated, added, state.points,
    )
    _evaluate_grief(server, source, state)
    return added


def _evaluate_grief(server, player, state: GriefState) -> None:
    if state.kick_pending:
        return
    kick_at = float(setting(server, "grief_kick_points"))
    if (
        bool(setting(server, "grief_kick_enabled"))
        and kick_at > 0.0
        and state.points >= kick_at
    ):
        state.kick_pending = True
        return
    if not state.warned and state.points >= float(
        setting(server, "grief_warn_points")
    ):
        state.warned = True
        _send_private(
            player,
            "Warning: stop hurting your teammates or you will be kicked "
            "for griefing.",
        )


def _kick(server, player, reason: int, why: str, announce: str) -> None:
    logger.warning(
        "conduct kick player=%s name=%r reason=%s",
        getattr(player, "id", "?"), getattr(player, "name", ""), why,
    )
    disconnect = getattr(player, "disconnect", None)
    if callable(disconnect):
        disconnect(int(reason))
    _announce(server, announce)


# --------------------------------------------------------------------------
# AFK
# --------------------------------------------------------------------------

@dataclass
class AfkState:
    idle_seconds: float = 0.0
    warned: bool = False
    last_flags: tuple | None = None
    last_actions: tuple | None = None
    last_orientation: tuple | None = None
    kicked: bool = False


def afk_state(player) -> AfkState:
    state = getattr(player, "_conduct_afk", None)
    if isinstance(state, AfkState):
        return state
    state = AfkState()
    try:
        player._conduct_afk = state
    except AttributeError:
        pass
    return state


def note_activity(player) -> None:
    """Reset the idle clock (any deliberate player action)."""

    state = afk_state(player)
    state.idle_seconds = 0.0
    state.warned = False


def observe_input(player, flags, orientation, action_flags=None) -> bool:
    """Compare one ClientData frame with the last; True when it shows input."""

    state = afk_state(player)
    try:
        movement = tuple(bool(value) for value in flags)
    except TypeError:
        movement = ()
    actions = ()
    if action_flags is not None:
        try:
            values = tuple(action_flags)
            actions = tuple(
                bool(values[index])
                for index in _ACTIVE_ACTION_INDICES
                if index < len(values)
            )
        except TypeError:
            actions = ()
    try:
        aim = tuple(float(value) for value in orientation)
        if len(aim) != 3 or not all(math.isfinite(value) for value in aim):
            aim = None
    except (TypeError, ValueError):
        aim = None

    active = False
    if state.last_flags is None or movement != state.last_flags:
        active = True
    if action_flags is not None:
        if state.last_actions is None:
            active = active or any(actions)
        elif actions != state.last_actions:
            active = True
    if aim is not None:
        previous = state.last_orientation
        if previous is None or any(
            abs(a - b) > _ORIENTATION_EPSILON for a, b in zip(aim, previous)
        ):
            active = True
            state.last_orientation = aim
    state.last_flags = movement
    if action_flags is not None:
        state.last_actions = actions
    if active:
        note_activity(player)
    return active


def _afk_paused(server, player) -> bool:
    connection = getattr(player, "connection", None)
    if connection is not None and not bool(getattr(connection, "in_game", True)):
        return True
    mode = getattr(server, "mode", None)
    if mode is not None and (
        bool(getattr(mode, "ended", False))
        or not bool(getattr(mode, "started", True))
    ):
        return True
    team = getattr(player, "team", None)
    spectator = team is not None and int(team) == int(C.TEAM_SPECTATOR)
    # A dead player waits on the server's respawn timer; that is not idling.
    return not spectator and not bool(getattr(player, "alive", False))


def _afk_limit(server, player) -> float:
    if float(setting(server, "afk_kick_seconds")) <= 0.0:
        return 0.0  # AFK kicks disabled entirely
    team = getattr(player, "team", None)
    if team is not None and int(team) == int(C.TEAM_SPECTATOR):
        return float(setting(server, "afk_spectator_kick_seconds"))
    return float(setting(server, "afk_kick_seconds"))


def _tick_afk(server, player, dt: float) -> None:
    limit = _afk_limit(server, player)
    if limit <= 0.0:
        return
    if bool(setting(server, "afk_exempt_admins")) and bool(
        getattr(player, "admin", False)
    ):
        return
    state = afk_state(player)
    if state.kicked or _afk_paused(server, player):
        return
    state.idle_seconds += max(0.0, float(dt))
    if state.idle_seconds >= limit:
        state.kicked = True
        _kick(
            server,
            player,
            int(C.DISCONNECT.ERROR_AFK_TIMEOUT),
            f"afk idle={state.idle_seconds:.0f}s",
            f"{getattr(player, 'name', 'A player')} was kicked for being AFK.",
        )
        return
    warn_at = float(setting(server, "afk_warn_seconds"))
    if limit != float(setting(server, "afk_kick_seconds")):
        # Spectators: keep the same one-minute notice before their limit.
        warn_at = max(0.0, limit - (float(setting(server, "afk_kick_seconds")) - warn_at))
    if not state.warned and 0.0 < warn_at < limit and state.idle_seconds >= warn_at:
        state.warned = True
        remaining = max(1, int(round(limit - state.idle_seconds)))
        _send_private(
            player,
            f"You are AFK. Move or you will be kicked in {remaining} seconds.",
        )


def tick(server, dt: float = 1.0, now: float | None = None) -> None:
    """Once-per-second conduct pass: AFK clocks and pending grief kicks."""

    players = tuple(getattr(server, "players", {}).values())
    current = time.monotonic() if now is None else float(now)
    for player in players:
        if not _is_human(player):
            continue
        grief = getattr(player, "_conduct_grief", None)
        if isinstance(grief, GriefState):
            if grief.kick_pending:
                grief.kick_pending = False
                grief.points = 0.0
                _kick(
                    server,
                    player,
                    int(C.DISCONNECT.ERROR_KICK_GRIEFING),
                    f"griefing team_kills={grief.team_kills} "
                    f"team_damage={grief.team_damage}",
                    f"{getattr(player, 'name', 'A player')} was kicked for "
                    "team griefing.",
                )
                continue
            grief_points(player, current)
        _tick_afk(server, player, dt)
