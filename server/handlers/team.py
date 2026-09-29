"""Team, palette, and menu-state packet handlers."""

from __future__ import annotations

import logging
import time

import shared.constants as C

from protocol.handler_registry import register_handler
from server.game_constants import (
    KILL_TEAM_CHANGE,
    KILL_WEAPON,
    PALETTE_TOOL_IDS,
    TEAM1,
    TEAM2,
    TEAM_SPECTATOR,
)

logger = logging.getLogger(__name__)

# Minimum seconds between two accepted ChangeTeam(77) requests per player
# (overridable with a ``team_change_cooldown`` config attribute). Every
# switch kills the Character and rebroadcasts roster state, so an
# unthrottled client could flood every peer with KillAction/CreatePlayer.
TEAM_CHANGE_COOLDOWN_SECONDS = 5.0
# Minimum seconds between SetColor(11) relays per player. The latest colour
# is always committed server-side and a trailing relay delivers it once the
# interval passes, so observers never keep a stale palette.
SET_COLOR_RELAY_INTERVAL_SECONDS = 0.1

_SYSTEM_SENDER_ID = 255


def _tell_player(player, message: str) -> None:
    """Send one private CHAT_SYSTEM line (same form as command replies)."""
    import shared.constants as C
    from shared.packet import ChatMessage

    send = getattr(player, "send", None)
    if not callable(send):
        return
    packet = ChatMessage()
    packet.player_id = _SYSTEM_SENDER_ID
    packet.chat_type = int(getattr(C, "CHAT_SYSTEM", 2))
    packet.value = message
    try:
        send(bytes(packet.generate()))
    except Exception:
        logger.debug(
            "team notice to %s failed", getattr(player, "name", "?"),
            exc_info=True,
        )


def _tell_player_localised(player, string_id: str) -> None:
    """Send one private retail LocalisedMessage(50) team notice.

    EN:116-122 is a block of server-sent HUD lines (COUNTDOWN_*, then
    TEAM_SWITCH_NOT_ALLOWED, TEAM_SWITCH_WAIT, TEAM_LOCKED, TEAM_FULL); no
    stock client binary references the four team ids, so only the server
    can show them.  Same lane as the TEAM_FULL auto-balance notice.
    """
    from server.announcements import build_localised_overlay

    send = getattr(player, "send", None)
    if not callable(send):
        return
    try:
        send(build_localised_overlay(string_id))
    except Exception:
        logger.debug(
            "team notice %s to %s failed", string_id,
            getattr(player, "name", "?"), exc_info=True,
        )


class _LockProbe:
    """Collects the team-lock bits a mode would publish in StateData."""

    team1_locked = False
    team2_locked = False

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)


def team_join_locked(server, team: int) -> bool:
    """Whether the client shows ``team`` as locked (StateData/LockTeam 79)."""
    configure = getattr(getattr(server, "mode", None), "configure_state_data", None)
    if not callable(configure) or team not in (TEAM1, TEAM2):
        return False
    probe = _LockProbe()
    try:
        configure(probe)
    except Exception:  # noqa: BLE001 - presentation only
        return False
    return bool(probe.team1_locked if team == TEAM1 else probe.team2_locked)


def _team_change_cooldown(server) -> float:
    config = getattr(server, "config", None)
    value = getattr(config, "team_change_cooldown", None)
    if value is None:
        return TEAM_CHANGE_COOLDOWN_SECONDS
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return TEAM_CHANGE_COOLDOWN_SECONDS


def _switch_unbalances_teams(server, player, new_team: int) -> bool:
    """Whether ``[teams] auto_balance`` forbids moving ``player`` to ``new_team``.

    Same rule as a joiner (``Connection._balance_join_team``): the requested
    side may not already lead the other by ``balance_threshold`` players (not
    counting the mover). Modes that assign teams themselves
    (``prepare_join_team``: Zombie, ...) are exempt; spectating is always
    allowed.
    """
    if new_team not in (TEAM1, TEAM2):
        return False
    mode = getattr(server, "mode", None)
    if callable(getattr(mode, "prepare_join_team", None)):
        return False
    if not bool(getattr(getattr(server, "config", None), "auto_balance", False)):
        return False
    connection = getattr(player, "connection", None)
    balance = getattr(connection, "_balance_join_team", None)
    if not callable(balance):
        return False
    return int(balance(new_team)) != int(new_team)


# Seconds after the last enemy hit during which a self-inflicted transition
# death (/kill, ChangeClass(78), SetClassLoadout(13), ChangeTeam(77)) is
# converted into an ordinary kill credited to that enemy. Without it a
# modified client could send ChangeClass at 1 HP and deny the kill (the
# transition kill types count no death and credit nobody).
# Stock server-only PLAYER_INTERACTION_EXPIRY_SECONDS (A100 = 5.0). The same
# window credits a fall death to the last enemy damager (Player.damage).
TRANSITION_DEATH_CREDIT_WINDOW_SECONDS = float(
    getattr(C, "PLAYER_INTERACTION_EXPIRY_SECONDS", 5.0)
)


def recent_enemy_attacker(server, player, *, now: float | None = None):
    """The enemy who damaged ``player``'s current life in the credit window.

    Returns ``None`` unless ``player`` is alive, the last recorded damage
    came from another player on the opposing playable team, that damage
    landed during the current life and within
    :data:`TRANSITION_DEATH_CREDIT_WINDOW_SECONDS`, and the attacker is
    still connected to this server (a dead attacker still earns the kill,
    exactly as a grenade thrown before dying would).
    """
    if not bool(getattr(player, "alive", False)):
        return None
    last_hit = getattr(player, "_last_combat_damage_at", None)
    try:
        last_hit = float(last_hit)
    except (TypeError, ValueError):
        return None
    if last_hit <= 0.0:
        return None
    spawned_at = getattr(player, "spawned_at", None)
    if isinstance(spawned_at, (int, float)) and last_hit < float(spawned_at):
        return None  # the damage belonged to a previous life
    current = time.monotonic() if now is None else float(now)
    if current - last_hit > TRANSITION_DEATH_CREDIT_WINDOW_SECONDS:
        return None
    try:
        source_id = int(getattr(player, "_last_damage_source_id", -1))
    except (TypeError, ValueError):
        return None
    if source_id < 0 or source_id == int(getattr(player, "id", -2)):
        return None
    players = getattr(server, "players", None)
    attacker = players.get(source_id) if isinstance(players, dict) else None
    if attacker is None or attacker is player:
        return None
    attacker_team = getattr(attacker, "team", None)
    if attacker_team not in (TEAM1, TEAM2):
        return None
    if attacker_team == getattr(player, "team", None):
        return None
    return attacker


def end_life_for_transition(server, player, kill_type: int):
    """Retire ``player``'s live Character for a team/class/suicide transition.

    Normally this is a transition death (``kill_type``: no death counted, no
    credit). When an enemy damaged the player within
    :data:`TRANSITION_DEATH_CREDIT_WINDOW_SECONDS` it is instead the ordinary
    kill that the transition would otherwise have denied: the death counts
    and the attacker gets the kill, killfeed entry and mode scoring through
    the normal ``Player.die`` path. Returns the credited attacker (or None).
    """
    if not bool(getattr(player, "alive", False)):
        return None
    attacker = recent_enemy_attacker(server, player)
    if attacker is None:
        player.die(kill_type=int(kill_type))
        return None
    from server import anticheat

    anticheat.report(
        server,
        player,
        "transition_death_credited",
        kill_type=int(kill_type),
        attacker=getattr(attacker, "id", "?"),
        health=getattr(player, "health", "?"),
    )
    player.die(killer=attacker, kill_type=KILL_WEAPON)
    return attacker


def change_team(
    server, player, new_team: int, *, explain: bool = False, force: bool = False
) -> bool:
    """Apply one requested team move under every server rule.

    Shared by ChangeTeam(77) and ``/team``: the spectator rule, mode team
    locks (``allows_team_change``), the per-player cooldown,
    ``auto_balance``, deployable retirement, kill credit for a recent enemy
    hit, the spectator CreatePlayer and ``on_player_team_change``. Cooldown
    and balance refusals always tell the player; ``explain`` also answers the
    refusals the retail menu never offers (a typed command should get a
    reply). Returns whether the move happened.

    ``force`` is the server's own auto-balance move (``server.team_balance``):
    it skips the per-player cooldown and the ``auto_balance`` refusal (the
    balancer only ever moves toward the smaller side) but never a mode team
    lock or the spectator rule.
    """
    if new_team == TEAM_SPECTATOR:
        from server.game_rules import get_rules

        if not get_rules(server.config).enabled("RULE_ENABLE_SPECTATORS"):
            _tell_player_localised(player, "TEAM_SWITCH_NOT_ALLOWED")
            return False
    if new_team == player.team:
        if explain:
            _tell_player(player, "You're already on that team!")
        return False
    mode = getattr(server, "mode", None)
    allows_team_change = getattr(mode, "allows_team_change", None)
    if callable(allows_team_change) and not allows_team_change(player, new_team):
        logger.debug("Ignoring mode-locked team change from %s", player.name)
        # The ChangeTeam(77) menu path gets the same retail line as /team.
        _tell_player_localised(
            player,
            "TEAM_LOCKED" if team_join_locked(server, new_team)
            else "TEAM_SWITCH_NOT_ALLOWED",
        )
        return False
    now = time.monotonic()
    last_change = getattr(player, "_last_team_change_at", None)
    cooldown = _team_change_cooldown(server)
    # Leaving the spectator roster kills no Character and emits no roster
    # packet (the respawn does), so it is exempt; every move that retires a
    # body (including team -> spectator) is rate-limited, which still bounds
    # any team/spectator ping-pong to one KillAction per cooldown.
    if (
        not force
        and player.team != TEAM_SPECTATOR
        and last_change is not None
        and now - float(last_change) < cooldown
    ):
        remaining = max(1, int(cooldown - (now - float(last_change)) + 0.999))
        _tell_player_localised(player, "TEAM_SWITCH_WAIT")
        if explain:
            # TEAM_SWITCH_WAIT has no {0}; a typed /team also gets the count.
            _tell_player(
                player, f"You can change team again in {remaining} second(s)."
            )
        return False
    if not force and _switch_unbalances_teams(server, player, new_team):
        logger.info(
            "Auto-balance: refused %s switching to team %s", player.name, new_team
        )
        _tell_player_localised(player, "TEAM_FULL")
        return False
    try:
        player._last_team_change_at = now
    except AttributeError:
        pass
    old_team = player.team
    # Team-bound deployables snapshot allegiance at placement. Retire them
    # before changing the owner or turrets may acquire their former owner and
    # radar reference counts remain attached to the old team.
    lifecycle = getattr(server, "round_lifecycle", None)
    retire = getattr(lifecycle, "remove_owned_deployables", None)
    if callable(retire):
        retire(player)
    if player.alive and recent_enemy_attacker(server, player) is not None:
        # Credit the denied kill while the victim is still on its old team:
        # Player.die's killer/victim team check and the mode's scoring must
        # see the allegiance the damage was dealt under.
        end_life_for_transition(server, player, KILL_TEAM_CHANGE)
    if player.team in server.teams:
        server.teams[player.team].remove_player(player)
    player.team = new_team
    if new_team in server.teams:
        server.teams[new_team].add_player(player)
    if player.alive:
        player.die(kill_type=KILL_TEAM_CHANGE)
    if new_team == TEAM_SPECTATOR:
        # KillAction retires the old Character. CreatePlayer(team=0) then
        # moves every retail roster to its spectator representation. There is
        # no server->client ChangeTeam handler in this build.
        player.death_time = 0.0
        from server.roster import build_create_player, remember_player_life

        data = bytes(build_create_player(player).generate())
        server.broadcast(data)
        for connection in server.connections.values():
            if getattr(connection, "in_game", False):
                remember_player_life(connection, player)
    elif old_team == TEAM_SPECTATOR:
        # A spectator has no death event to schedule. Arm one ordinary
        # respawn so its staged class/loadout is applied at the same boundary
        # as every other team transition.
        player.death_time = time.time()
    server.queue_mode_event("on_player_team_change", player, old_team, new_team)
    return True


@register_handler(77)  # ChangeTeam
async def handle_change_team(server, player, packet) -> None:
    """Move a player to a playable team or the native spectator roster."""
    from server.connection import wire_team_to_internal

    wire_team = packet.team
    new_team = wire_team_to_internal(wire_team)
    if new_team is None:
        logger.debug(
            "Ignoring ChangeTeam from %s for wire team %s",
            player.name,
            wire_team,
        )
        return
    change_team(server, player, new_team)


def _relay_color(server, player) -> None:
    """Announce ``player``'s committed palette colour to other peers."""
    from shared.packet import SetColor

    try:
        player._color_relay_at = time.monotonic()
    except AttributeError:
        pass
    broadcast_packet = SetColor()
    broadcast_packet.player_id = player.id
    broadcast_packet.value = int(getattr(player, "block_color", 0)) & 0xFFFFFF
    # The sender has already committed this colour in its palette UI.
    server.broadcast(bytes(broadcast_packet.generate()), exclude=player)


def _flush_color(server, player) -> None:
    """Trailing relay: publish the newest colour a throttle held back."""
    try:
        player._color_flush_handle = None
    except AttributeError:
        pass
    players = getattr(server, "players", None)
    if (
        isinstance(players, dict)
        and players.get(getattr(player, "id", None)) is not player
    ):
        return  # departed; PlayerLeft owns client cleanup
    if not player.alive or not player.spawned:
        return  # the next CreatePlayer + SetColor carries the palette
    _relay_color(server, player)


@register_handler(11)  # SetColor
async def handle_set_color(server, player, packet) -> None:
    """Commit a live palette-tool colour and announce it to other players.

    The retail client already applies its palette choice locally.  Echoing the
    packet to its sender races self WorldUpdate processing and visibly flickers
    the held block, while dead/non-palette-tool updates are invalid state.
    Relays are throttled per player (``SET_COLOR_RELAY_INTERVAL_SECONDS``):
    the server state is always updated and one trailing relay publishes the
    newest colour, so a palette drag or a spamming client cannot flood peers
    while observers still end on the authoritative colour.
    """
    from server.game_rules import get_rules
    config = getattr(server, "config", None)
    if (
        not player.alive
        or not player.spawned
        # Live-measured (2026-09-26): the stock client changes its block
        # colour whatever tool is held (colour-picking with the prefab or
        # disguise tool, or SetColor racing a tool switch). Rejecting it here
        # desynced the builder's blocks from everyone else's.
        or not get_rules(config).enabled("RULE_ENABLE_COLOUR_PICKER")
    ):
        return
    value = int(packet.value) & 0xFFFFFF
    player.set_color(value)
    now = time.monotonic()
    last = getattr(player, "_color_relay_at", None)
    elapsed = None if last is None else now - float(last)
    if elapsed is None or elapsed >= SET_COLOR_RELAY_INTERVAL_SECONDS:
        handle = getattr(player, "_color_flush_handle", None)
        if handle is not None:
            handle.cancel()
            player._color_flush_handle = None
        _relay_color(server, player)
        return
    if getattr(player, "_color_flush_handle", None) is not None:
        return  # a trailing relay is already scheduled; it reads the newest
    import asyncio

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    try:
        player._color_flush_handle = loop.call_later(
            max(0.0, SET_COLOR_RELAY_INTERVAL_SECONDS - elapsed),
            _flush_color,
            server,
            player,
        )
    except AttributeError:
        pass


@register_handler(15)  # NewPlayerConnection from an already-joined peer
async def handle_spectator_rejoin(server, player, packet) -> None:
    """Spectator -> team: the stock client re-sends NewPlayerConnection.

    ChangeTeam.on_select (and SelectTeam) call ``game_scene.create_player``
    for a player whose team is the spectator team; live 2026-09-26 that sent
    SetClassLoadout(13) + NewPlayerConnection(15, team=2/3) and no
    ChangeTeam(77). Packet 15 from a joined peer used to have no handler, so
    a spectator could never enter the game. Route it through the ordinary
    team move (locks, balance, staged class/loadout, respawn arming). Any
    other duplicate join stays ignored.
    """
    if int(getattr(player, "team", -1)) != TEAM_SPECTATOR:
        logger.debug("Ignoring NewPlayerConnection from joined %s", player.name)
        return
    from server.connection import wire_team_to_internal

    new_team = wire_team_to_internal(getattr(packet, "team", None))
    if new_team is None or new_team == TEAM_SPECTATOR:
        return
    change_team(server, player, new_team, explain=True)


# Spectator admission retry while a large pre-snapshot terrain history is
# still draining (``reveal_world_to`` returns False and must be called again).
SPECTATOR_REVEAL_RETRY_SECONDS = 0.05
SPECTATOR_REVEAL_MAX_ATTEMPTS = 2000


def admit_spectator(server, connection, *, attempt: int = 0) -> bool:
    """Admit a settled spectator GameScene into the gameplay stream.

    Every other player is admitted on its first ClientData(4), which a
    retail client only starts sending once it has a Character. A spectator
    never gets one: live 2026-09-26 the stock client sent only ClockSync(0)
    after joining team 0, so the connection stayed gated forever and the
    spectator saw frozen bodies (no WorldUpdate), no entities, scores, chat,
    kill feed or ambience. The spectator's GameScene reports itself with
    ClientInMenu(in_menu=0) right after its NewPlayerConnection(team 0)
    (selectTeam.on_select: create_player() then set_scene(GameScene)), so
    that is the admission boundary; the reveal is the same one ClientData
    runs for everyone else.
    """
    if connection is None or getattr(connection, "in_game", False):
        return bool(getattr(connection, "in_game", False))
    player = getattr(connection, "player", None)
    if player is None or int(getattr(player, "team", -1)) != TEAM_SPECTATOR:
        return False
    players = getattr(server, "players", None)
    if isinstance(players, dict) and players.get(getattr(player, "id", None)) is not player:
        return False  # departed while a retry was pending
    reveal = getattr(server, "reveal_world_to", None)
    if not callable(reveal):
        return False
    try:
        complete = reveal(connection)
    except Exception:
        logger.exception("spectator reveal_world_to failed")
        return False
    if complete is False:
        if attempt < SPECTATOR_REVEAL_MAX_ATTEMPTS:
            import asyncio

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return False
            loop.call_later(
                SPECTATOR_REVEAL_RETRY_SECONDS,
                lambda: admit_spectator(server, connection, attempt=attempt + 1),
            )
        return False
    connection.in_game = True
    prune = getattr(server, "_prune_map_mutations", None)
    if callable(prune):
        prune()
    try:
        from server.join_greeting import send_join_greeting

        send_join_greeting(connection)
    except Exception:
        logger.debug("spectator join greeting failed", exc_info=True)
    return True


@register_handler(110)  # ClientInMenu
async def handle_client_in_menu(server, player, packet) -> None:
    """Track menu state used by safe round and class transitions."""
    if player.connection:
        in_menu = bool(packet.in_menu)
        player.connection.in_menu = in_menu
        player.connection.note_scene_transition_menu(in_menu)
        if not in_menu and int(getattr(player, "team", -1)) == TEAM_SPECTATOR:
            admit_spectator(server, player.connection)
