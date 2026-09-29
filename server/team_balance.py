"""Mid-match team auto-balance (``[teams] auto_balance``).

Joins and voluntary switches are balanced at their own boundaries
(``Connection._balance_join_team``, ``handlers.team.change_team``). Teams can
still drift apart during a match when players leave. ``TeamBalancer.tick``
runs once per second from the gameplay tick (``BattleSpadesServer``'s
periodic services, just before respawns) and repairs that drift:

1. Nothing happens until one side leads by ``balance_threshold`` (never less
   than two: a one-player lead cannot be improved by moving anyone) for
   ``balance_grace_seconds``, so a reconnect or the bot backfill filling the
   gap itself does not cause churn.
2. Bots even it first. A dead bot on the bigger side switches sides through
   the ordinary team-change path (it respawns there). When no bot dies within
   ``balance_bot_wait_seconds`` a retire-safe bot on the bigger side is
   retired and a replacement joins the smaller side.
3. Then humans: the lowest-impact, most recently joined human on the bigger
   side who is DEAD moves and respawns on the other side. A live player is
   never killed; objective carriers, VIPs, last survivors and anyone the
   mode's team lock refuses are never picked; nobody is moved twice within
   ``balance_player_cooldown`` seconds. The moved player gets the retail
   ``TEAM_FULL`` ("Team is full. Auto-balancing...") overlay and a private
   chat line.

Modes that assign teams themselves (``prepare_join_team``: Zombie, Tutorial,
UGC) are never balanced.
"""

from __future__ import annotations

import logging
import time

from server.game_constants import TEAM1, TEAM2

logger = logging.getLogger(__name__)

_PLAYABLE = (TEAM1, TEAM2)
_SYSTEM_SENDER_ID = 255

DEFAULT_CHECK_INTERVAL = 1.0
DEFAULT_GRACE_SECONDS = 5.0
DEFAULT_PLAYER_COOLDOWN = 600.0
DEFAULT_BOT_WAIT_SECONDS = 10.0
BALANCE_STRING_ID = "TEAM_FULL"


def _config_value(server, name: str, default: float) -> float:
    value = getattr(getattr(server, "config", None), name, None)
    if value is None:
        return float(default)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(default)


def team_counts(server) -> dict[int, int]:
    """Players (humans and bots) on each playable team.

    Same population rule as the join balance: every roster entry counts,
    including clients still loading into the scene.
    """
    counts = {TEAM1: 0, TEAM2: 0}
    for player in getattr(server, "players", {}).values():
        try:
            team = int(getattr(player, "team", -1))
        except (TypeError, ValueError):
            continue
        if team in counts:
            counts[team] += 1
    return counts


def _is_bot(player) -> bool:
    return bool(getattr(player, "is_bot", False))


class TeamBalancer:
    """Once-per-second mid-match auto-balance."""

    def __init__(self, server) -> None:
        self.server = server
        self._next_check = 0.0
        self._uneven_since: float | None = None
        self._first_seen: dict[int, tuple[object, float]] = {}
        self.bot_moves = 0
        self.human_moves = 0

    # ------------------------------------------------------------------ policy
    def enabled(self) -> bool:
        config = getattr(self.server, "config", None)
        if not bool(getattr(config, "auto_balance", False)):
            return False
        return bool(getattr(config, "balance_mid_match", True))

    def _applicable(self) -> bool:
        server = self.server
        if not self.enabled():
            return False
        mode = getattr(server, "mode", None)
        if mode is None:
            return False
        if not bool(getattr(mode, "started", True)) or bool(getattr(mode, "ended", False)):
            return False
        if bool(getattr(mode, "retiring", False)):
            return False
        # Modes that own team assignment (Zombie, Tutorial, UGC) or opt out.
        if callable(getattr(mode, "prepare_join_team", None)):
            return False
        if not bool(getattr(mode, "auto_balance_enabled", True)):
            return False
        transition = getattr(server, "match_transition", None)
        busy = getattr(transition, "_transition_busy", None)
        if callable(busy):
            try:
                if busy():
                    return False
            except Exception:  # noqa: BLE001 - never block the tick
                return False
        elif bool(getattr(transition, "in_progress", False)):
            return False
        return True

    def threshold(self) -> int:
        config = getattr(self.server, "config", None)
        try:
            configured = int(getattr(config, "balance_threshold", 2) or 2)
        except (TypeError, ValueError):
            configured = 2
        # A lead of one is as even as an odd population gets; moving someone
        # would only flip it and ping-pong forever.
        return max(2, configured)

    # -------------------------------------------------------------------- tick
    async def tick(self, now: float | None = None) -> bool:
        """Run one balance check when due. Returns whether anyone moved."""
        now = time.monotonic() if now is None else float(now)
        if now < self._next_check:
            return False
        # The caller already runs at 1 Hz; the small slack keeps scheduler
        # jitter from skipping every other call.
        self._next_check = now + max(0.0, _config_value(
            self.server, "balance_check_interval", DEFAULT_CHECK_INTERVAL
        ) - 0.1)
        self._remember_arrivals(now)
        if not self._applicable():
            self._uneven_since = None
            return False
        counts = team_counts(self.server)
        big = TEAM1 if counts[TEAM1] >= counts[TEAM2] else TEAM2
        small = TEAM2 if big == TEAM1 else TEAM1
        if counts[big] - counts[small] < self.threshold():
            self._uneven_since = None
            return False
        if self._uneven_since is None:
            self._uneven_since = now
        waited = now - self._uneven_since
        if waited < _config_value(
            self.server, "balance_grace_seconds", DEFAULT_GRACE_SECONDS
        ):
            return False
        bot_wait = _config_value(
            self.server, "balance_bot_wait_seconds", DEFAULT_BOT_WAIT_SECONDS
        )
        if await self._move_bot(big, small, allow_retire=waited >= bot_wait):
            self.bot_moves += 1
            self._uneven_since = now
            return True
        if self._has_movable_bot(big) and waited < bot_wait:
            # A bot on the bigger side will die (or become retirable) soon;
            # prefer that over moving a person.
            return False
        if self._move_human(big, small, now):
            self.human_moves += 1
            self._uneven_since = now
            return True
        return False

    def _remember_arrivals(self, now: float) -> None:
        """Approximate join order: first second a roster object was seen."""
        players = getattr(self.server, "players", {})
        seen = self._first_seen
        for player_id, player in players.items():
            entry = seen.get(int(player_id))
            if entry is None or entry[0] is not player:
                seen[int(player_id)] = (player, now)
        for player_id in [key for key in seen if key not in players]:
            del seen[player_id]

    def joined_at(self, player) -> float:
        entry = self._first_seen.get(int(getattr(player, "id", -1)))
        if entry is not None and entry[0] is player:
            return float(entry[1])
        return 0.0

    # ------------------------------------------------------------ candidates
    def _mode_allows(self, player, new_team: int) -> bool:
        mode = getattr(self.server, "mode", None)
        allows = getattr(mode, "allows_team_change", None)
        if callable(allows):
            try:
                if not allows(player, new_team):
                    return False
            except Exception:  # noqa: BLE001 - a broken hook means "no"
                return False
        return True

    def _objective_safe(self, player) -> bool:
        """Never move a carrier, VIP, patient zero or round-deciding body."""
        if getattr(player, "pickup_id", None) is not None:
            return False
        mode = getattr(self.server, "mode", None)
        if mode is None:
            return True
        vips = getattr(mode, "vips", None)
        values = getattr(vips, "values", None)
        if callable(values) and any(vip is player for vip in values()):
            return False
        patient_zero = getattr(mode, "patient_zero_ids", None) or ()
        try:
            if int(player.id) in patient_zero:
                return False
        except (TypeError, ValueError):
            return False
        last_survivor = getattr(mode, "last_survivor_id", None)
        if last_survivor is not None and int(last_survivor) == int(player.id):
            return False
        veto = getattr(mode, "bot_retire_safe", None)
        if callable(veto):
            try:
                if not veto(player):
                    return False
            except Exception:  # noqa: BLE001
                return False
        return True

    def _in_game(self, player) -> bool:
        if _is_bot(player):
            return True
        connection = getattr(player, "connection", None)
        return bool(connection is not None and getattr(connection, "in_game", False))

    def _movable(self, player, new_team: int, *, dead_only: bool = True) -> bool:
        if dead_only and bool(getattr(player, "alive", False)):
            return False
        if not self._in_game(player):
            return False
        if not self._objective_safe(player):
            return False
        return self._mode_allows(player, new_team)

    def _has_movable_bot(self, big: int) -> bool:
        bots = self._director()
        if bots is None:
            return False
        small = TEAM2 if big == TEAM1 else TEAM1
        return any(
            int(getattr(bot, "team", -1)) == big
            and self._objective_safe(bot)
            and self._mode_allows(bot, small)
            for bot in tuple(getattr(bots, "bots", ()))
        )

    def _director(self):
        bots = getattr(self.server, "bots", None)
        if bots is None or not bool(getattr(bots, "_started", True)):
            return None
        if getattr(bots, "_reconnect_count", None) is not None:
            return None  # a match transition owns the bot roster
        return bots

    # ------------------------------------------------------------------ moves
    async def _move_bot(self, big: int, small: int, *, allow_retire: bool) -> bool:
        bots = self._director()
        if bots is None:
            return False
        on_big = [
            bot for bot in tuple(getattr(bots, "bots", ()))
            if int(getattr(bot, "team", -1)) == big
        ]
        if not on_big:
            return False
        from server.handlers.team import change_team

        dead = sorted(
            (bot for bot in on_big if self._movable(bot, small)),
            key=lambda bot: int(bot.id),
        )
        for bot in dead:
            if change_team(self.server, bot, small, force=True):
                logger.info(
                    "Auto-balance: bot %s switched to team %s", bot.name, small
                )
                return True
        if not allow_retire:
            return False
        # No bot died in time: retire a safe one and backfill the other side.
        safe_to_retire = getattr(bots, "_safe_to_retire", None)
        remove_bot = getattr(bots, "remove_bot", None)
        add_bot = getattr(bots, "add_bot", None)
        if not callable(remove_bot):
            return False
        candidates = sorted(
            on_big,
            key=lambda bot: (
                bool(getattr(bot, "alive", False)),
                int(bot.id),
            ),
        )
        for bot in candidates:
            if not self._objective_safe(bot):
                continue
            if callable(safe_to_retire) and not safe_to_retire(bot):
                continue
            if not await remove_bot(bot):
                continue
            logger.info("Auto-balance: retired bot %s from team %s", bot.name, big)
            if callable(add_bot):
                try:
                    await add_bot(team=small)
                except Exception:  # noqa: BLE001 - the retirement stands
                    logger.exception("Auto-balance: replacement bot failed")
            return True
        return False

    async def force_balance(self) -> int:
        """Admin ``/balance``: even the teams now under the same rules.

        Skips the grace/bot-wait timers and the per-player cooldown, but
        keeps every eligibility rule of the automatic balancer: bots first,
        only DEAD players move (nobody is killed), carriers/VIPs/last
        survivors and mode team locks are never touched, and each moved
        human gets the retail TEAM_FULL notice.  Modes that assign teams
        themselves are never balanced.  Returns how many players moved.
        """
        mode = getattr(self.server, "mode", None)
        if mode is None or callable(getattr(mode, "prepare_join_team", None)):
            return 0
        if not bool(getattr(mode, "auto_balance_enabled", True)):
            return 0
        moved = 0
        for _attempt in range(64):
            counts = team_counts(self.server)
            big = TEAM1 if counts[TEAM1] >= counts[TEAM2] else TEAM2
            small = TEAM2 if big == TEAM1 else TEAM1
            if counts[big] - counts[small] < 2:
                break
            if await self._move_bot(big, small, allow_retire=True):
                self.bot_moves += 1
                moved += 1
                continue
            if self._move_human(big, small, time.monotonic(), respect_cooldown=False):
                self.human_moves += 1
                moved += 1
                continue
            break
        return moved

    def _move_human(self, big: int, small: int, now: float, *,
                    respect_cooldown: bool = True) -> bool:
        cooldown = _config_value(
            self.server, "balance_player_cooldown", DEFAULT_PLAYER_COOLDOWN
        ) if respect_cooldown else 0.0
        candidates = []
        for player in tuple(getattr(self.server, "players", {}).values()):
            if _is_bot(player) or int(getattr(player, "team", -1)) != big:
                continue
            moved_at = getattr(player, "_balance_moved_at", None)
            if moved_at is not None and now - float(moved_at) < cooldown:
                continue
            if not self._movable(player, small):
                continue
            candidates.append(player)
        if not candidates:
            return False
        # Lowest impact first (round score, captures), then the newest arrival.
        candidates.sort(
            key=lambda player: (
                int(getattr(player, "captures", 0) or 0) > 0,
                int(getattr(player, "score", 0) or 0),
                -self.joined_at(player),
                -int(player.id),
            )
        )
        from server.handlers.team import change_team

        for player in candidates:
            if not change_team(self.server, player, small, force=True):
                continue
            try:
                player._balance_moved_at = now
            except AttributeError:
                pass
            logger.info(
                "Auto-balance: moved %s from team %s to team %s", player.name, big, small
            )
            self._notify(player)
            return True
        return False

    def _notify(self, player) -> None:
        send = getattr(player, "send", None)
        if not callable(send):
            return
        try:
            from server.announcements import build_localised_overlay

            send(build_localised_overlay(BALANCE_STRING_ID))
        except Exception:  # noqa: BLE001 - the notice is best effort
            logger.debug("auto-balance overlay failed", exc_info=True)
        try:
            import shared.constants as C
            from shared.packet import ChatMessage

            packet = ChatMessage()
            packet.player_id = _SYSTEM_SENDER_ID
            packet.chat_type = int(getattr(C, "CHAT_SYSTEM", 2))
            packet.value = (
                "Teams were uneven: you will respawn on the other team."
            )
            send(bytes(packet.generate()))
        except Exception:  # noqa: BLE001
            logger.debug("auto-balance notice failed", exc_info=True)


__all__ = ["TeamBalancer", "team_counts"]
