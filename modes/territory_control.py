"""Retail-style Territory Control objective mode."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL

from .base_mode import BaseMode
from .objective_zones import ObjectiveZone, around, from_map_zone, minimap_zone_packet


logger = logging.getLogger(__name__)

_PLAYABLE_TEAMS = (TEAM1, TEAM2)
_NEUTRAL_COLOR = (255, 255, 255)
# Stock A2550 TC_CAPTURE_RATE: (capturing players, capture % per 0.5 s
# TC_CAPTURE_TICK_RATE tick). The wire capture_amount is 0..100 % of one
# ownership step; internally one step is 0.5 of ``progress``.
TC_CAPTURE_RATE_TABLE = tuple(
    (int(players), float(rate)) for players, rate in CG.TC_CAPTURE_RATE
)


def tc_capture_percent_per_tick(players: int) -> float:
    """Capture % one tick adds for ``players`` capturers.

    Linear between the stock table's points and flat past its last point.
    The interpolation (vs a step lookup) is inferred: the table only lists
    1, 5, 10 and 15 players, so a step would give 2-4 players the one-player
    rate.
    """
    players = max(0, int(players))
    table = TC_CAPTURE_RATE_TABLE
    if players <= table[0][0]:
        return float(table[0][1])
    for (low_n, low_rate), (high_n, high_rate) in zip(table, table[1:]):
        if players <= high_n:
            span = float(high_n - low_n)
            return low_rate + (high_rate - low_rate) * (players - low_n) / span
    return float(table[-1][1])

_FALLBACK_RADIUS = 15.0
_ENTER_SHOUT_COOLDOWN = float(CG.TC_NEW_TEAM_ENTERS_SHOUT_COOLDOWN)
# Retail capture-point resupply interval (shared/constants.py
# CAPTURE_POINT_REFILL_TIME = 10.0).
CAPTURE_POINT_REFILL_TIME = float(C.CAPTURE_POINT_REFILL_TIME)


class CapturePointResupply:
    """Refill players standing on a capture point their team owns.

    The client tables carry CAPTURE_POINT_REFILL_TIME (10 s) but no code in
    either client tree reads it, so it is server behaviour. Owned TC
    territories and held Multi-Hill hills are the capture points: a live
    member of the owning team inside an uncontested zone is restocked at
    once and then at most every CAPTURE_POINT_REFILL_TIME seconds - health
    to its maximum (SetHP heal), weapons and tools (Restock type
    AMMO_CRATE, which unlike type 0 does not also restore health) and the
    block wallet (Restock type 5). Operators can turn it off per mode with
    ``capture_point_resupply = false`` in ``[modes.tc]`` / ``[modes.mh]``.
    """

    def __init__(self, server, mode_code: str) -> None:
        overlay = getattr(getattr(server, "config", None), "mode_settings", {})
        overlay = overlay.get(mode_code, {}) if isinstance(overlay, dict) else {}
        self.enabled = bool(overlay.get("capture_point_resupply", True))
        self.interval = max(0.5, float(overlay.get(
            "capture_point_refill_time", CAPTURE_POINT_REFILL_TIME
        )))
        self._last: dict[int, tuple[object, float]] = {}

    def reset(self) -> None:
        self._last = {}

    def forget(self, player) -> None:
        self._last.pop(int(getattr(player, "id", -1)), None)

    def offer(self, player, now: float) -> bool:
        """Resupply ``player`` when its interval has elapsed."""
        if not self.enabled or not bool(getattr(player, "alive", False)):
            return False
        player_id = int(getattr(player, "id", -1))
        previous = self._last.get(player_id)
        if (
            previous is not None
            and previous[0] is player
            and now - float(previous[1]) < self.interval
        ):
            return False
        self._last[player_id] = (player, now)
        heal = getattr(player, "heal", None)
        if callable(heal):
            maximum = int(getattr(player, "max_health", 100) or 100)
            if int(getattr(player, "health", maximum)) < maximum:
                heal(maximum)
        restock = getattr(player, "restock_ammo", None)
        if callable(restock):
            restock(int(C.AMMO_CRATE))
        blocks = getattr(player, "restock_blocks", None)
        if callable(blocks):
            blocks()
        return True


def territory_name(index: int) -> str:
    """Retail base letter ("A".."J") used by the TC_* string templates."""
    names = CG.TC_BASENAMES
    index = int(index)
    return str(names[index]) if 0 <= index < len(names) else str(index + 1)


def _configured_rule(server, key: str, rule: str, fallback):
    resolver = getattr(getattr(server, "config", None), "mode_rule", None)
    if callable(resolver):
        try:
            value = resolver("tc", key, rule)
            if value is not None and value is not False:
                return value
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    overlay = getattr(getattr(server, "config", None), "mode_settings", {}).get(
        "tc", {}
    )
    return overlay.get(key, fallback)


@dataclass(slots=True)
class Territory:
    """One active native TC base and its continuous capture state."""

    zone: ObjectiveZone
    owner: int = TEAM_NEUTRAL
    attacker: int = TEAM_NEUTRAL
    progress: float = 0.5
    active: bool = True
    contested: bool = False
    occupants: dict[int, set[int]] = field(
        default_factory=lambda: {TEAM1: set(), TEAM2: set()}
    )
    last_non_neutral_owner: int = TEAM_NEUTRAL
    # Identity of each occupant id: a departed player's reused compact id
    # must never be paid (or messaged) for the previous body's presence.
    occupant_players: dict[int, dict[int, object]] = field(
        default_factory=lambda: {TEAM1: {}, TEAM2: {}}
    )


def _wire_capture(territory: "Territory") -> tuple[int, float]:
    """Packet-106 ``(attacked_by, capture_amount)`` for one territory.

    The stock TerritoryBasesHud stretches the attacker plate to
    ``capture_amount / 100`` in the ``attacked_by`` colour over the owner's
    backplate, so the wire amount is a 0..100 percentage of the way to the
    next ownership change. ``progress`` is ours (0 Blue, 0.5 neutral,
    1 Green): the amount is the distance from the owner's anchor, and the
    attacker is the team the progress leans toward (a half-taken base keeps
    showing its partial plate after the attackers leave).
    """
    anchor = 0.0 if territory.owner == TEAM1 else 1.0 if territory.owner == TEAM2 else 0.5
    delta = float(territory.progress) - anchor
    amount = max(0.0, min(100.0, abs(delta) / 0.5 * 100.0))
    if amount <= 0.0:
        return int(territory.attacker), 0.0
    return (TEAM2 if delta > 0.0 else TEAM1), amount


class TerritoryControlMode(BaseMode):
    """Capture a line of territories until one team owns every active base.

    Packet 106 is the native TC HUD state machine.  Internal progress is
    continuous: ``0`` is Blue, ``0.5`` neutral, and ``1`` Green; the wire
    carries the retail 0..100 attacker percentage (``_wire_capture``).
    Packet 43 supplies the
    matching lettered minimap/billboard zones.
    """

    name = "Territory Control"
    description = "Capture and hold all active territories."
    mode_code = "tc"

    def __init__(self, server) -> None:
        super().__init__(server)
        data = mode_data.get(self.mode_code)
        self.score_limit = int(data.default_score_limit)
        resolve_time = getattr(getattr(server, "config", None), "configured_time_limit", None)
        self.time_limit = (
            float(resolve_time(self.mode_code, data.default_time_limit))
            if callable(resolve_time)
            else float(data.default_time_limit)
        )
        self.max_active_bases = max(2, min(5, int(_configured_rule(
            server,
            "max_active_bases",
            "RULE_TC_MAX_ACTIVE_BASES",
            CG.TC_DEFAULT_BASE_COUNT_TO_USE,
        ))))
        self.capture_multiplier = max(0.1, float(_configured_rule(
            server, "capture_rate", "RULE_CAPTURE_RATE", 1.0
        )))
        self.territories: list[Territory] = []
        # (territory index, team) -> last TC_ENTER_BASE_* shout time.
        self._enter_shout_at: dict[tuple[int, int], float] = {}
        self._next_capture_at = 0.0
        self._next_personal_score_at = 0.0
        self.resupply = CapturePointResupply(server, self.mode_code)

    async def on_mode_start(self) -> None:
        await super().on_mode_start()
        for team in self.server.teams.values():
            team.reset()
        zones = self._select_active_zones(self._build_zones())
        self.territories = self._initialise_territories(zones)
        # The win is owning every active territory and team.score counts
        # owned territories, so StateData/HUD must advertise that target.
        self.score_limit = max(1, len(self.territories))
        self._enter_shout_at = {}
        self.resupply.reset()
        now = time.time()
        self._next_capture_at = now
        self._next_personal_score_at = now + float(CG.TC_SCORE_OCCUPY_INTERVAL)
        for territory in self.territories:
            self._send_zone(territory)
            self._send_state(territory, int(C.TC_INITIAL_INFO))
            self._send_state(territory, int(C.TC_BASE_ACTIVATE))
        self._refresh_team_scores()
        self.broadcast_start_cue()
        logger.info(
            "Territory Control started with %d active bases at %.2fx capture rate",
            len(self.territories),
            self.capture_multiplier,
        )

    async def deactivate(self) -> None:
        for territory in self.territories:
            self._send_state(territory, int(C.TC_BASE_DEACTIVATE))
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        await super().on_tick(tick)
        if self.ended:
            return
        now = time.time()
        interval = float(CG.TC_CAPTURE_TICK_RATE)
        if now >= self._next_capture_at:
            elapsed = max(interval, now - self._next_capture_at + interval)
            self._next_capture_at = now + interval
            await self._capture_tick(min(elapsed, interval * 3.0))
        if not self.ended and now >= self._next_personal_score_at:
            periods = max(1, int(
                (now - self._next_personal_score_at)
                / float(CG.TC_SCORE_OCCUPY_INTERVAL)
            ) + 1)
            self._next_personal_score_at += (
                periods * float(CG.TC_SCORE_OCCUPY_INTERVAL)
            )
            self._award_presence_scores(periods)

    def reveal_to(self, connection) -> None:
        super().reveal_to(connection)
        for territory in self.territories:
            self._send_zone(territory, connection=connection)
            self._send_state(
                territory, int(C.TC_INITIAL_INFO), connection=connection
            )
            if territory.active:
                self._send_state(
                    territory, int(C.TC_BASE_ACTIVATE), connection=connection
                )
                if territory.contested:
                    # Contention is edge-triggered; a joiner never saw it.
                    self._send_state(
                        territory,
                        int(C.TC_BASE_CONTENDED),
                        connection=connection,
                    )
        # Retail mode-start cue (string-table only; no client binary sends
        # it), replayed per settled GameScene like TDM/Diamond.
        self.send_start_cue_to(connection)

    def start_cue_for(self, player):
        return "TC_START"

    async def on_player_kill(self, killer, victim, kill_type: int) -> None:
        await super().on_player_kill(killer, victim, kill_type)
        if (
            self.ended
            or killer is None
            or killer is victim
            or int(getattr(killer, "team", -1)) not in _PLAYABLE_TEAMS
            or int(getattr(victim, "team", -1)) not in _PLAYABLE_TEAMS
            or int(killer.team) == int(victim.team)
        ):
            return
        killer_zone = self._territory_at(killer)
        victim_zone = self._territory_at(victim)
        if killer_zone is not None and killer_zone.owner == int(killer.team):
            self._award_player(
                killer,
                int(CG.TC_SCORE_KILL_KILLERINHILL),
                int(C.SCORE_REASON.TC_DEFEND_SCORE_REASON),
            )
        elif victim_zone is not None and victim_zone.owner != int(killer.team):
            self._award_player(
                killer,
                int(CG.TC_SCORE_KILL_VICTIMINHILL),
                int(C.SCORE_REASON.TC_ASSAULT_SCORE_REASON),
            )

    def _build_zones(self) -> list[ObjectiveZone]:
        wm = getattr(self.server, "world_manager", None)
        metadata = getattr(wm, "map_metadata", None)
        authored = list(getattr(metadata, "neutral_base_zones", ()) or ())
        shift = int(getattr(getattr(wm, "map", None), "source_z_shift", 0))
        zones = [
            from_map_zone(index, zone, z_shift=shift)
            for index, zone in enumerate(authored[:10])
        ]
        if len(zones) >= 2:
            return self._sort_along_team_axis(zones)

        first, second = self._team_anchors()
        dry = getattr(wm, "dry_ground_anchor", None)
        zones = []
        for index, fraction in enumerate((0.15, 0.325, 0.5, 0.675, 0.85)):
            x = first[0] + (second[0] - first[0]) * fraction
            y = first[1] + (second[1] - first[1]) * fraction
            center = dry(x, y, 48) if callable(dry) else (x, y, 58.0)
            zones.append(around(
                index,
                center,
                radius_xy=_FALLBACK_RADIUS,
                height_above=8.0,
                depth_below=10.0,
            ))
        logger.warning(
            "Map %s has no complete TC objective sidecar; using dry corridor bases",
            getattr(wm, "map_name", "<unknown>"),
        )
        return zones

    def _team_anchors(self):
        wm = getattr(self.server, "world_manager", None)
        reader = getattr(wm, "team_base_anchor", None)
        if callable(reader):
            return (
                tuple(float(value) for value in reader(TEAM1)),
                tuple(float(value) for value in reader(TEAM2)),
            )
        return (64.0, 256.0, 58.0), (448.0, 256.0, 58.0)

    def _sort_along_team_axis(self, zones: list[ObjectiveZone]) -> list[ObjectiveZone]:
        first, second = self._team_anchors()
        dx, dy = second[0] - first[0], second[1] - first[1]
        if abs(dx) + abs(dy) < 1e-6:
            return sorted(zones, key=lambda zone: zone.index)
        return sorted(
            zones,
            key=lambda zone: (
                (zone.center[0] - first[0]) * dx
                + (zone.center[1] - first[1]) * dy,
                zone.index,
            ),
        )

    def _select_active_zones(self, zones: list[ObjectiveZone]) -> list[ObjectiveZone]:
        if len(zones) <= self.max_active_bases:
            selected = zones
        else:
            count = self.max_active_bases
            indexes = [
                int(round(index * (len(zones) - 1) / float(count - 1)))
                for index in range(count)
            ]
            selected = [zones[index] for index in indexes]
        return [
            ObjectiveZone(index, zone.bounds, zone.center)
            for index, zone in enumerate(selected[:10])
        ]

    def _initialise_territories(
        self, zones: list[ObjectiveZone]
    ) -> list[Territory]:
        count = len(zones)
        result = []
        for index, zone in enumerate(zones):
            if index < count // 2:
                owner, progress = TEAM1, 0.0
            elif index > (count - 1) // 2:
                owner, progress = TEAM2, 1.0
            else:
                owner, progress = TEAM_NEUTRAL, 0.5
            result.append(Territory(
                zone=zone,
                owner=owner,
                progress=progress,
                last_non_neutral_owner=owner,
            ))
        return result

    async def _capture_tick(self, elapsed: float) -> None:
        changed_score = False
        captures: list[tuple[Territory, int, bool]] = []
        now = time.time()
        for territory in self.territories:
            occupants = self._occupants(territory.zone)
            self._send_presence_transitions(territory, occupants, now)
            blue = len(occupants[TEAM1])
            green = len(occupants[TEAM2])
            contested = blue > 0 and green > 0
            if contested != territory.contested:
                territory.contested = contested
                self._send_state(
                    territory,
                    int(C.TC_BASE_CONTENDED if contested else C.TC_BASE_UNCONTENDED),
                )
            if not contested and territory.owner in _PLAYABLE_TEAMS:
                for player in occupants[territory.owner]:
                    self.resupply.offer(player, now)
            # A contested base is frozen (TC_BASE_CONTENDED; contending
            # players are paid TC_Contend for exactly that). Otherwise the
            # present team moves the base by the stock per-tick table.
            net = 0 if contested else green - blue
            territory.attacker = (
                TEAM2 if net > 0 else TEAM1 if net < 0 else TEAM_NEUTRAL
            )
            if net == 0:
                continue

            previous_progress = territory.progress
            previous_owner = territory.owner
            ticks = float(elapsed) / float(CG.TC_CAPTURE_TICK_RATE)
            step = (
                tc_capture_percent_per_tick(abs(net)) / 100.0 * 0.5
                * self.capture_multiplier * ticks
            )
            progress = territory.progress + (step if net > 0 else -step)
            # Snap float residue from summing many small ticks (0.005 each).
            for anchor in (0.0, 0.5, 1.0):
                if abs(progress - anchor) < 1e-9:
                    progress = anchor
            territory.progress = min(1.0, max(0.0, progress))
            if territory.progress <= 0.0:
                territory.owner = TEAM1
            elif territory.progress >= 1.0:
                territory.owner = TEAM2
            elif (
                previous_owner in _PLAYABLE_TEAMS
                and (previous_progress - 0.5) * (territory.progress - 0.5) <= 0.0
            ):
                territory.last_non_neutral_owner = previous_owner
                territory.owner = TEAM_NEUTRAL

            if territory.owner != previous_owner:
                changed_score = True
                self._send_zone(territory)
                if territory.owner in _PLAYABLE_TEAMS:
                    was_enemy = (
                        territory.last_non_neutral_owner in _PLAYABLE_TEAMS
                        and territory.last_non_neutral_owner != territory.owner
                    )
                    captures.append((territory, territory.owner, was_enemy))
                    # Every capturing occupant took part in the capture and
                    # is paid Claim/Control (it used to be the lowest id only).
                    for capturer in sorted(
                        occupants[territory.owner],
                        key=lambda player: int(getattr(player, "id", 0)),
                    ):
                        self._award_player(
                            capturer,
                            int(CG.TC_SCORE_CONTROL if was_enemy else CG.TC_SCORE_CLAIM),
                            int(
                                C.SCORE_REASON.TC_CONTROL_SCORE_REASON
                                if was_enemy
                                else C.SCORE_REASON.TC_CLAIM_SCORE_REASON
                            ),
                        )
                    territory.last_non_neutral_owner = territory.owner
            self._send_state(territory, int(C.TC_BASE_CAPTURE_UPDATE))

        if changed_score:
            self._refresh_team_scores()
            for territory, capturer, was_enemy in captures:
                self._announce_capture(territory, capturer, was_enemy)
            for team_id in _PLAYABLE_TEAMS:
                if self.server.teams[team_id].score >= self.score_limit:
                    await self._end_by_score(team_id)
                    break

    def _announce_capture(
        self, territory: Territory, capturer: int, was_enemy: bool
    ) -> None:
        """TC_CAPTURED_* / TC_NEUTRALCAPTURED_*, once per ownership change.

        ``{0}`` is the base letter and ``{1} of {2} left`` counts, for the
        capturing team, the territories it still has to take and, for the
        other team, the territories it still holds (inferred reading; the
        retail server source is not recovered).
        """
        capturer = int(capturer)
        loser = TEAM2 if capturer == TEAM1 else TEAM1
        total = len(self.territories)
        owned = {
            team: sum(1 for item in self.territories if item.owner == team)
            for team in _PLAYABLE_TEAMS
        }
        letter = territory_name(territory.zone.index)
        prefix = "TC_CAPTURED" if was_enemy else "TC_NEUTRALCAPTURED"
        self.announce_localised_to_team(
            capturer,
            f"{prefix}_CAPTURINGTEAM",
            (letter, str(total - owned[capturer]), str(total)),
        )
        self.announce_localised_to_team(
            loser,
            f"{prefix}_LOSINGTEAM",
            (letter, str(owned[loser]), str(total)),
        )
        from server.audio import play_team_relative

        play_team_relative(self.server, capturer)

    def _occupants(self, zone: ObjectiveZone) -> dict[int, list]:
        result = {TEAM1: [], TEAM2: []}
        for player in tuple(getattr(self.server, "players", {}).values()):
            team = int(getattr(player, "team", -1))
            if (
                team not in _PLAYABLE_TEAMS
                or not bool(getattr(player, "alive", False))
                or not bool(getattr(player, "spawned", True))
            ):
                continue
            position = getattr(player, "position", None)
            if position is None:
                position = (player.x, player.y, player.z)
            # AFK bodies and escape-flagged (sealed/out-of-map) players
            # cannot hold or contest a territory.
            if zone.contains(position) and self.objective_presence_eligible(player):
                result[team].append(player)
        return result

    def escape_watch_objective_player(self, player) -> bool:
        """A player standing in a territory is holding an objective."""
        position = getattr(player, "position", None)
        if position is None:
            return False
        return any(t.zone.contains(position) for t in self.territories)

    def _send_presence_transitions(
        self, territory: Territory, occupants, now: float | None = None
    ) -> None:
        if now is None:
            now = time.time()
        players = getattr(self.server, "players", {})
        for team in _PLAYABLE_TEAMS:
            current = {int(player.id) for player in occupants[team]}
            previous = territory.occupants[team]
            previous_players = territory.occupant_players[team]
            entered = current - previous
            left = previous - current
            for player_id in sorted(entered):
                player = players.get(player_id)
                if player is not None:
                    self._send_state_to_player(
                        territory, int(C.TC_BASE_ENTERING), player
                    )
            for player_id in sorted(left):
                player = players.get(player_id)
                if player is not None and player is previous_players.get(player_id):
                    self._send_state_to_player(
                        territory, int(C.TC_BASE_LEAVING), player
                    )
            territory.occupants[team] = current
            territory.occupant_players[team] = {
                int(player.id): player for player in occupants[team]
            }
            if current and not previous and territory.owner != team:
                self._announce_team_entered(territory, team, occupants[team], now)

    def _announce_team_entered(self, territory: Territory, team: int, entrants, now: float) -> None:
        """TC_ENTER_BASE_* when a team moves onto a territory it does not own.

        Rate limited per (territory, team) by the retail
        TC_NEW_TEAM_ENTERS_SHOUT_COOLDOWN so boundary flicker cannot spam.
        """
        key = (int(territory.zone.index), int(team))
        last = self._enter_shout_at.get(key)
        if last is not None and now - last < _ENTER_SHOUT_COOLDOWN:
            return
        self._enter_shout_at[key] = now
        leader = min(entrants, key=lambda player: int(getattr(player, "id", 0)))
        name = str(getattr(leader, "name", f"Player {leader.id}"))
        letter = territory_name(territory.zone.index)
        inside = {int(player.id) for player in entrants}
        for player in tuple(getattr(self.server, "players", {}).values()):
            player_team = int(getattr(player, "team", -1))
            if player_team == int(team):
                if int(player.id) in inside:
                    self.announce_localised_to_player(
                        player, "TC_ENTER_BASE_PLAYER", (letter,)
                    )
                else:
                    self.announce_localised_to_player(
                        player, "TC_ENTER_BASE_TEAMMATES", (letter, name)
                    )
            elif player_team in _PLAYABLE_TEAMS:
                self.announce_localised_to_player(
                    player, "TC_ENTER_BASE_OPPOSITION", (letter, name)
                )

    def _send_state_to_player(self, territory: Territory, action: int, player) -> None:
        """Per-player HUD state, only to a settled GameScene (bots have none)."""
        connection = getattr(player, "connection", None)
        if connection is None or not getattr(connection, "in_game", True):
            return
        self._send_state(territory, action, connection=connection)

    def _occupant(self, territory: Territory, team: int, player_id: int):
        """The live body behind an occupant id, or None when it departed."""
        player = getattr(self.server, "players", {}).get(player_id)
        if player is None:
            return None
        expected = territory.occupant_players.get(team, {}).get(player_id)
        if expected is not None and expected is not player:
            return None
        if int(getattr(player, "team", -1)) != int(team):
            return None
        return player

    def _award_presence_scores(self, periods: int) -> None:
        for territory in self.territories:
            if territory.contested:
                for team in _PLAYABLE_TEAMS:
                    for player_id in territory.occupants[team]:
                        player = self._occupant(territory, team, player_id)
                        if player is not None:
                            self._award_player(
                                player,
                                periods * int(CG.TC_SCORE_CONTEND_HILL),
                                int(C.SCORE_REASON.TC_CONTEND_SCORE_REASON),
                            )
                continue
            if territory.owner not in _PLAYABLE_TEAMS:
                continue
            for player_id in territory.occupants[territory.owner]:
                player = self._occupant(territory, territory.owner, player_id)
                if player is not None:
                    self._award_player(
                        player,
                        periods * int(CG.TC_SCORE_OCCUPY_PERHILL),
                        int(C.SCORE_REASON.TC_OCCUPY_SCORE_REASON),
                    )

    def _refresh_team_scores(self) -> None:
        for team_id in _PLAYABLE_TEAMS:
            team = self.server.teams[team_id]
            team.score = sum(
                1 for territory in self.territories if territory.owner == team_id
            )
            try:
                self.server.broadcast_set_score(
                    team, reason=int(C.SCORE_REASON.TC_CONTROL_SCORE_REASON)
                )
            except TypeError:
                self.server.broadcast_set_score(team)

    def _send_zone(self, territory: Territory, connection=None) -> None:
        color = (
            self.server.teams[territory.owner].color
            if territory.owner in _PLAYABLE_TEAMS
            else _NEUTRAL_COLOR
        )
        packet = minimap_zone_packet(
            territory.zone,
            color=color,
            icon_id=int(CG.ZONE_ICON_TERRITORY_A) + territory.zone.index,
            visible_team=TEAM_NEUTRAL,
        )
        self._send_packet(packet, connection)

    def _send_state(self, territory: Territory, action: int, connection=None) -> None:
        from shared.packet import TerritoryBaseState

        packet = TerritoryBaseState()
        packet.base_index = int(territory.zone.index)
        packet.action = int(action)
        packet.controlled_by = int(territory.owner)
        attacked_by, amount = _wire_capture(territory)
        packet.attacked_by = int(attacked_by)
        packet.capture_amount = float(amount)
        self._send_packet(packet, connection)

    def _send_packet(self, packet, connection=None) -> None:
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            send = getattr(connection, "send", None)
            if callable(send):
                send(data, reliable=True)

    def _territory_at(self, player) -> Territory | None:
        position = getattr(player, "position", None)
        if position is None:
            position = (player.x, player.y, player.z)
        return next(
            (territory for territory in self.territories if territory.zone.contains(position)),
            None,
        )

    def _award_player(self, player, points: int, reason: int) -> None:
        if not self._owns_slot(player):
            return
        from server.scoreboard import send_player_score

        player.score = int(getattr(player, "score", 0)) + int(points)
        send_player_score(self.server, player, reason=int(reason))


__all__ = ["Territory", "TerritoryControlMode"]
