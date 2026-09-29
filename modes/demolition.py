"""Retail-style two-base Demolition objective mode."""

from __future__ import annotations

import logging
import time

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL

from .airstrike import sound_airstrike_siren, trigger_airstrike
from .base_mode import BaseMode
from .objective_zones import (
    ObjectiveZone,
    around,
    clamp_bounds,
    from_map_zone,
    lock_to_zone_packet,
    minimap_zone_clear_packet,
    minimap_zone_packet,
)


logger = logging.getLogger(__name__)
_PLAYABLE_TEAMS = (TEAM1, TEAM2)
# The objective airstrike's five shells start 48 blocks above the base at the
# recovered 100 blocks/s shell speed, so they land well inside one second.
# Hold the end-of-round sequence this long after launch so every client sees
# the siren, the impacts and the collapse before the result is announced.
AIRSTRIKE_IMPACT_DELAY = 3.0


def _configured_rule(server, key: str, rule: str, fallback):
    resolver = getattr(getattr(server, "config", None), "mode_rule", None)
    if callable(resolver):
        try:
            value = resolver("dem", key, rule)
            if value is not None:
                return value
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    overlay = getattr(getattr(server, "config", None), "mode_settings", {}).get(
        "dem", {}
    )
    return overlay.get(key, fallback)


class DemolitionMode(BaseMode):
    """Build around your base, then destroy the enemy objective volume."""

    name = "Demolition"
    description = "Destroy the enemy base before they destroy yours."
    mode_code = "dem"
    score_limit = 1

    def __init__(self, server) -> None:
        super().__init__(server)
        data = mode_data.get(self.mode_code)
        self.build_state_length = max(0.0, float(_configured_rule(
            server,
            "build_state_length",
            "RULE_BUILD_STATE_LENGTH",
            CG.DEM_DEFAULT_BUILD_TIME,
        )))
        resolve_time = getattr(getattr(server, "config", None), "configured_time_limit", None)
        self.time_limit = (
            float(resolve_time(self.mode_code, data.default_time_limit))
            if callable(resolve_time)
            else float(data.default_time_limit)
        )
        self.phase = "waiting"
        self.base_zones: dict[int, ObjectiveZone] = {}
        self._authored_base: dict[int, bool] = {TEAM1: False, TEAM2: False}
        self.objective_cells: dict[int, set[tuple[int, int, int]]] = {
            TEAM1: set(), TEAM2: set(),
        }
        self.destroyed_cells: dict[int, set[tuple[int, int, int]]] = {
            TEAM1: set(), TEAM2: set(),
        }
        self._build_ends_at = 0.0
        self._airstrike_at = 0.0
        self._end_at = 0.0
        self._strike_launched = False
        self._destroyed_team: int | None = None
        self._destroyed_teams: tuple[int, ...] = ()
        self._pending_winner: int | None = None
        # Personal DEM_SCORE_DESTROY_* / DEM_SCORE_REPAIR_* block counters.
        self._destroy_progress: dict[int, int] = {}
        self._repair_progress: dict[int, int] = {}
        # Objective cells a team knocked out of its own base; rebuilding them
        # is not a repair (no dig-and-refill score farming).
        self._self_damaged: dict[int, set[tuple[int, int, int]]] = {
            TEAM1: set(), TEAM2: set(),
        }
        self._mutation_listener_token: int | None = None
        self._progress_dirty = True
        self._repair_warning_active = {TEAM1: False, TEAM2: False}

    async def on_mode_start(self) -> None:
        self._unsubscribe_mutations()
        await super().on_mode_start()
        for team in self.server.teams.values():
            team.reset()
        self.base_zones = self._build_base_zones()
        self.objective_cells = {TEAM1: set(), TEAM2: set()}
        self.destroyed_cells = {TEAM1: set(), TEAM2: set()}
        self._destroyed_team = None
        self._destroyed_teams = ()
        self._pending_winner = None
        self._airstrike_at = 0.0
        self._end_at = 0.0
        self._strike_launched = False
        self._destroy_progress = {}
        self._repair_progress = {}
        self._self_damaged = {TEAM1: set(), TEAM2: set()}
        self._progress_dirty = True
        self._repair_warning_active = {TEAM1: False, TEAM2: False}
        now = time.time()
        self._build_ends_at = now + self.build_state_length
        self.phase = "building" if self.build_state_length > 0.0 else "active"
        self._subscribe_mutations()
        self._send_base_zones()
        if self.phase == "building":
            self._send_current_locks()
        else:
            self._freeze_objective_cells()
            self._send_progress()
        # HUD clock (DisplayCountdown 84): the build phase counts down to its
        # end, then the round clock takes over (countdown_seconds_remaining).
        self._send_phase_timer(now)
        logger.info(
            "Demolition started with %.0fs build phase and base bounds %s",
            self.build_state_length,
            {team: zone.bounds for team, zone in self.base_zones.items()},
        )

    def countdown_seconds_remaining(self, now: float) -> float:
        """HUD countdown: the build phase first, then the round clock.

        Retail has no string for the build phase (DEM_DEFAULT_BUILD_TIME
        30 s); the HUD timer is the only cue a player gets for when the
        bases unlock. Showing the build clock there is our reading.
        """
        if self.phase == "building":
            return max(0.0, float(self._build_ends_at) - float(now))
        if self.time_limit <= 0:
            return 0.0
        elapsed = float(now) - float(self.start_time)
        return max(0.0, float(self.time_limit) - elapsed)

    def _send_phase_timer(self, now: float) -> None:
        if self.phase != "building" and self.time_limit <= 0:
            return
        from server.scoreboard import send_round_timer

        try:
            send_round_timer(self.server, self.countdown_seconds_remaining(now))
        except Exception:  # noqa: BLE001 - the timer is cosmetic
            logger.debug("demolition phase timer failed", exc_info=True)

    async def deactivate(self) -> None:
        self._unsubscribe_mutations()
        if self.phase == "building":
            # LockToZone persists in the client until replaced: a mode or map
            # switch during the build phase must not leave players penned in
            # their old base under the next ruleset.
            self._release_all_locks()
            self.phase = "waiting"
        for zone in self.base_zones.values():
            self.server.broadcast(
                bytes(minimap_zone_clear_packet(zone).generate()),
                reliable=True,
            )
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        await super().on_tick(tick)
        if self.ended:
            return
        now = time.time()
        if self.phase == "building" and now >= self._build_ends_at:
            self.phase = "active"
            self._freeze_objective_cells()
            self._release_all_locks()
            self._send_base_zones()
            self._send_progress()
            from server.announcements import broadcast_localised_overlay

            broadcast_localised_overlay(self.server, "DEMOLITION_START")
            self._send_phase_timer(now)
            return

        if self.phase == "active":
            if self._progress_dirty:
                self._send_progress()
            destroyed = tuple(
                team for team in _PLAYABLE_TEAMS
                if self.objective_cells[team]
                and len(self.destroyed_cells[team]) >= len(self.objective_cells[team])
            )
            if destroyed:
                # Both bases can fall in the same tick (one blast, or two
                # batches drained together).  Iteration order must not pick a
                # winner: that case is a draw and both bases are struck.
                self._destroyed_teams = destroyed
                self._destroyed_team = destroyed[0] if len(destroyed) == 1 else None
                self.phase = "airstrike"
                self._airstrike_at = now + float(CG.DEM_TIME_TO_WAIT_FOR_AIRSTRIKE)
                # The siren is the warning for the DEM_TIME_TO_WAIT_FOR_AIRSTRIKE
                # gap; the strike itself then brings only the flyby.
                for team in destroyed:
                    sound_airstrike_siren(
                        self.server, self.base_zones[team].center, warn_ahead=True
                    )
                self._send_progress()
            return

        if (
            self.phase == "airstrike"
            and not self._strike_launched
            and now >= self._airstrike_at
        ):
            destroyed = tuple(
                team for team in self._destroyed_teams if team in _PLAYABLE_TEAMS
            )
            if not destroyed:
                return
            for team in destroyed:
                trigger_airstrike(self.server, self.base_zones[team].center)
            self._focus_airstrike(destroyed)
            if len(destroyed) == 1:
                winner = TEAM2 if destroyed[0] == TEAM1 else TEAM1
                self.server.teams[winner].score = 1
                try:
                    self.server.broadcast_set_score(
                        self.server.teams[winner],
                        reason=int(C.SCORE_REASON.DEM_DESTROY_SCORE_REASON),
                    )
                except TypeError:
                    self.server.broadcast_set_score(self.server.teams[winner])
                self._pending_winner = winner
            else:
                self._pending_winner = None
            # Let the shells land before the end sequence takes over.  The
            # phase stays "airstrike" so bots keep clearing the blast area.
            self._strike_launched = True
            self._end_at = now + AIRSTRIKE_IMPACT_DELAY
            return

        if (
            self.phase == "airstrike"
            and self._strike_launched
            and now >= self._end_at
        ):
            if self._pending_winner in _PLAYABLE_TEAMS:
                await self._end_by_score(self._pending_winner)
            else:
                await self.broadcast_localised_message("GAME_DRAWN")
                await self.on_mode_end(None)

    def _focus_airstrike(self, destroyed: tuple[int, ...]) -> None:
        """POIFocus(18): every team player watches the base being struck.

        The stock client pins a LookAtController on the local player with no
        timeout; it is released when that player dies or is spawned, which
        the end-of-round restart (or map change) always does for team
        players. Spectators are never respawned by the restart, so they keep
        their free camera. With both bases down each team watches the base
        it destroyed.
        """

        from server.hud_packets import poi_focus

        targets = {
            team: self.base_zones[team].center
            for team in destroyed
            if team in self.base_zones
        }
        if not targets:
            return
        for team in _PLAYABLE_TEAMS:
            enemy = TEAM2 if team == TEAM1 else TEAM1
            center = targets.get(enemy) or next(iter(targets.values()))
            recipients = [
                player
                for player in tuple(getattr(self.server, "players", {}).values())
                if int(getattr(player, "team", -1)) == team
                and not getattr(player, "is_bot", False)
            ]
            if recipients:
                poi_focus(self.server, center, recipients=recipients)

    async def _end_by_time(self) -> None:
        """At timeout, the base with more remaining objective blocks wins."""

        if self.ended:
            return
        remaining = {}
        for team in _PLAYABLE_TEAMS:
            total = max(1, len(self.objective_cells[team]))
            remaining[team] = 1.0 - len(self.destroyed_cells[team]) / total
        if remaining[TEAM1] > remaining[TEAM2]:
            winner = TEAM1
        elif remaining[TEAM2] > remaining[TEAM1]:
            winner = TEAM2
        else:
            winner = None
        # Same top-line result cue as every other mode's timeout.
        if winner is None:
            await self.broadcast_localised_message("GAME_DRAWN")
        else:
            await self.broadcast_localised_message(
                "TEAM_DEFEAT",
                (self.server.teams[winner].name,),
                localise_parameters=True,
            )
        await self.on_mode_end(winner)

    def end_message_id(self, winner: int | None) -> int:
        """Retail scoreboard headline: DEMOLITION_END_MESSAGE or a draw."""
        return int(C.TEAM_SCORES_DRAW if winner is None else C.DEMOLITION_END_MESSAGE)

    def configure_state_data(self, packet) -> None:
        # Demolition uses the two native TeamProgress base-health rows, not the
        # generic score-race bars used by TDM/CTF.
        packet.team1_show_score = False
        packet.team2_show_score = False
        packet.team1_show_max_score = False
        packet.team2_show_max_score = False

    def reveal_to(self, connection) -> None:
        super().reveal_to(connection)
        self._send_base_zones(connection)
        if self.phase != "building":
            self._send_progress(connection)
        self._send_player_lock(
            getattr(connection, "player", None), connection, reveal=True
        )

    async def on_player_spawn(self, player) -> None:
        self._send_player_lock(player, getattr(player, "connection", None))

    # DEM_Defend (71, DEM_SCORE_DEFEND 100) / DEM_Assault (72,
    # DEM_SCORE_ASSAULT 50). Demolition has no DEM_THREAT_RADIUS, so the
    # other modes' 20-block threat radius is reused around each base zone's
    # centre (inferred; rules audit 2026-09-27 #14).
    _KILL_EVENT_AMOUNTS = {
        "defend": int(CG.DEM_SCORE_DEFEND),
        "assault": int(CG.DEM_SCORE_ASSAULT),
    }
    _KILL_EVENT_REASONS = {
        "defend": int(C.SCORE_REASON.DEM_DEFEND_SCORE_REASON),
        "assault": int(C.SCORE_REASON.DEM_ASSAULT_SCORE_REASON),
    }
    DEM_THREAT_RADIUS = 20.0

    async def on_player_kill(self, killer, victim, kill_type: int) -> None:
        await super().on_player_kill(killer, victim, kill_type)
        from server import combat_scores as cs

        if (
            self.ended
            or self.phase != "active"
            or not cs.eligible_kill(killer, victim, kill_type)
        ):
            return
        own = self.base_zones.get(int(killer.team))
        enemy = self.base_zones.get(int(victim.team))
        event = cs.classify_objective_kill(
            killer, victim,
            defend_points=[own.center] if own is not None else [],
            attack_points=[enemy.center] if enemy is not None else [],
            threat_radius=self.DEM_THREAT_RADIUS,
        )
        cs.award_kill_event(
            self.server, killer, event,
            self._KILL_EVENT_AMOUNTS, self._KILL_EVENT_REASONS, mode=self,
        )

    async def on_player_leave(self, player) -> None:
        """Forget id-keyed partial block credit: the compact id is reused."""
        player_id = int(getattr(player, "id", -1))
        self._destroy_progress.pop(player_id, None)
        self._repair_progress.pop(player_id, None)

    async def on_player_team_change(self, player, old_team: int, new_team: int) -> None:
        self._send_player_lock(player, getattr(player, "connection", None))

    async def on_blocks_destroyed(self, player, positions, mined: bool) -> None:
        """Personal DEM_SCORE_DESTROY award: every DEM_SCORE_DESTROY_INTERVAL
        enemy objective blocks a player removes earns DEM_SCORE_DESTROY_SCORE.

        The terrain listener has already committed these cells to the base
        health; this queued hook only carries the acting player.
        """

        if self.ended or self.phase != "active" or player is None:
            return
        team = int(getattr(player, "team", -1))
        if team not in _PLAYABLE_TEAMS:
            return
        enemy = TEAM2 if team == TEAM1 else TEAM1
        cells = tuple(tuple(int(value) for value in cell) for cell in positions)
        own = self.objective_cells[team]
        self._self_damaged[team].update(cell for cell in cells if cell in own)
        count = sum(1 for cell in cells if cell in self.objective_cells[enemy])
        if not count:
            return
        from server.profile_stats import add

        add(player, C.DEM_DAMAGE_TOTAL, count)
        self._credit_blocks(
            player,
            self._destroy_progress,
            count,
            int(CG.DEM_SCORE_DESTROY_INTERVAL),
            int(CG.DEM_SCORE_DESTROY_SCORE),
            int(C.SCORE_REASON.DEM_DESTROY_SCORE_REASON),
        )

    async def on_blocks_built(self, player, positions) -> None:
        """Personal DEM_SCORE_REPAIR award: every DEM_SCORE_REPAIR_INTERVAL
        blocks rebuilt into the player's own damaged objective earns
        DEM_SCORE_REPAIR_SCORE.  Build-phase placement never counts
        (DEM_ALLOW_SCORE_REPAIR_BUILDINGMODE is False).
        """

        if self.ended or player is None:
            return
        if self.phase != "active" and not (
            self.phase == "building" and CG.DEM_ALLOW_SCORE_REPAIR_BUILDINGMODE
        ):
            return
        team = int(getattr(player, "team", -1))
        if team not in _PLAYABLE_TEAMS:
            return
        own = self.objective_cells[team]
        self_damaged = self._self_damaged[team]
        count = 0
        for cell in positions:
            cell = tuple(int(value) for value in cell)
            if cell not in own:
                continue
            if cell in self_damaged:
                self_damaged.discard(cell)
                continue
            count += 1
        if not count:
            return
        from server.profile_stats import add

        add(player, C.DEM_REPAIR_TOTAL, count)
        self._credit_blocks(
            player,
            self._repair_progress,
            count,
            int(CG.DEM_SCORE_REPAIR_INTERVAL),
            int(CG.DEM_SCORE_REPAIR_SCORE),
            int(C.SCORE_REASON.DEM_REPAIR_SCORE_REASON),
        )

    def _credit_blocks(
        self,
        player,
        progress: dict[int, int],
        count: int,
        interval: int,
        points: int,
        reason: int,
    ) -> None:
        if not self._owns_slot(player):
            # Departed before the queued terrain event drained.
            return
        player_id = int(getattr(player, "id", -1))
        total = progress.get(player_id, 0) + int(count)
        interval = max(1, int(interval))
        awards, progress[player_id] = divmod(total, interval)
        if awards and points > 0:
            from server.scoreboard import send_player_score

            player.score = int(getattr(player, "score", 0)) + awards * int(points)
            send_player_score(self.server, player, reason=int(reason))

    def _build_base_zones(self) -> dict[int, ObjectiveZone]:
        wm = getattr(self.server, "world_manager", None)
        metadata = getattr(wm, "map_metadata", None)
        shift = int(getattr(getattr(wm, "map", None), "source_z_shift", 0))
        result = {}
        for index, team in enumerate(_PLAYABLE_TEAMS):
            authored = (
                list(getattr(metadata, "base_zones", {}).get(team, ()) or ())
                if metadata is not None else []
            )
            if authored:
                self._authored_base[team] = True
                result[team] = from_map_zone(index, authored[0], z_shift=shift)
                continue
            self._authored_base[team] = False
            anchor_reader = getattr(wm, "team_base_anchor", None)
            anchor = (
                anchor_reader(team)
                if callable(anchor_reader)
                else ((96.0, 256.0, 58.0) if team == TEAM1 else (416.0, 256.0, 58.0))
            )
            result[team] = around(
                index, anchor, radius_xy=10.0,
                height_above=8.0, depth_below=9.0,
            )
        if metadata is None or any(
            not getattr(metadata, "base_zones", {}).get(team)
            for team in _PLAYABLE_TEAMS
        ):
            logger.warning(
                "Map %s has no complete Demolition sidecar; using dry team-anchor volumes",
                getattr(wm, "map_name", "<unknown>"),
            )
        return result

    def _freeze_objective_cells(self) -> None:
        wm = self.server.world_manager
        metadata = getattr(wm, "map_metadata", None)
        minimums = getattr(metadata, "base_min_destruction", {})
        for team, zone in self.base_zones.items():
            x0, x1, y0, y1, z0, z1 = zone.bounds
            if self._authored_base.get(team):
                cells = {
                    (x, y, z)
                    for x in range(x0, x1 + 1)
                    for y in range(y0, y1 + 1)
                    for z in range(z0, z1 + 1)
                    if wm.get_solid(x, y, z)
                }
            else:
                # Missing official server sidecars must not turn a 21x21x9
                # terrain slab into an almost indestructible objective.  The
                # compact top-surface core remains repairable, while everything
                # players construct during the build phase still functions as
                # physical defence around it.
                cx, cy = int(round(zone.center[0])), int(round(zone.center[1]))
                cells = set()
                for x in range(max(x0, cx - 2), min(x1, cx + 2) + 1):
                    for y in range(max(y0, cy - 2), min(y1, cy + 2) + 1):
                        for z in range(z0, z1 + 1):
                            if wm.get_solid(x, y, z):
                                cells.add((x, y, z))
                                break
            minimum = max(0, int(minimums.get(team, 0)))
            if minimum and len(cells) < minimum:
                logger.warning(
                    "Demolition team %d zone contains %d blocks (authored minimum %d)",
                    team, len(cells), minimum,
                )
            self.objective_cells[team] = cells
            self.destroyed_cells[team] = set()
        self._progress_dirty = True
        logger.info(
            "Demolition objective snapshot: blue=%d green=%d blocks",
            len(self.objective_cells[TEAM1]),
            len(self.objective_cells[TEAM2]),
        )

    def _subscribe_mutations(self) -> None:
        subscribe = getattr(self.server.world_manager, "subscribe_mutations", None)
        if callable(subscribe):
            self._mutation_listener_token = subscribe(self._on_world_mutation)

    def _unsubscribe_mutations(self) -> None:
        token = self._mutation_listener_token
        self._mutation_listener_token = None
        unsubscribe = getattr(
            getattr(self.server, "world_manager", None),
            "unsubscribe_mutations",
            None,
        )
        if token is not None and callable(unsubscribe):
            unsubscribe(token)

    def _on_world_mutation(
        self,
        x: int,
        y: int,
        z: int,
        solid: bool,
        color: int,
        topology_version: int,
    ) -> None:
        if self.phase not in ("active", "airstrike"):
            return
        position = (int(x), int(y), int(z))
        changed = False
        for team in _PLAYABLE_TEAMS:
            if position not in self.objective_cells[team]:
                continue
            if solid:
                before = len(self.destroyed_cells[team])
                self.destroyed_cells[team].discard(position)
                changed = len(self.destroyed_cells[team]) != before
            else:
                before = len(self.destroyed_cells[team])
                self.destroyed_cells[team].add(position)
                changed = len(self.destroyed_cells[team]) != before
            break
        self._progress_dirty = self._progress_dirty or changed

    def _base_zone_packet(self, team: int):
        return minimap_zone_packet(
            self.base_zones[team],
            color=self.server.teams[team].color,
            icon_id=int(CG.ZONE_ICON_DEMOLITION),
            visible_team=TEAM_NEUTRAL,
            locked_in_zone=self.phase == "building",
        )

    def _send_base_zones(self, connection=None) -> None:
        for team in _PLAYABLE_TEAMS:
            data = bytes(self._base_zone_packet(team).generate())
            if connection is None:
                self.server.broadcast(data, reliable=True)
            else:
                connection.send(data, reliable=True)

    def _progress_packet(self, team: int):
        from shared.packet import TeamProgress

        packet = TeamProgress()
        packet.team_id = int(team)
        packet.visible = 1
        packet.show_particle = 0
        packet.show_previous = 1
        packet.show_as_percent = 0
        # The retail HUD subtracts numerator from denominator.  Send damage
        # taken, not remaining blocks, so the bar falls as a base is destroyed.
        packet.numerator = int(len(self.destroyed_cells[team]))
        packet.denominator = max(1, int(len(self.objective_cells[team])))
        packet.percent = 0.0
        packet.icon_id = 0  # native base icon
        return packet

    def _send_progress(self, connection=None) -> None:
        for team in _PLAYABLE_TEAMS:
            data = bytes(self._progress_packet(team).generate())
            if connection is None:
                self.server.broadcast(data, reliable=True)
            else:
                connection.send(data, reliable=True)
            if connection is None:
                self._update_repair_warning(team)
        self._progress_dirty = False

    def _update_repair_warning(self, team: int) -> None:
        total = max(1, len(self.objective_cells[team]))
        remaining_percent = 100.0 * (
            total - len(self.destroyed_cells[team])
        ) / total
        warning = remaining_percent <= float(CG.DEM_REPAIR_WARNING_PERCENT)
        if warning and not self._repair_warning_active[team]:
            from server.announcements import build_localised_overlay

            data = build_localised_overlay("REPAIR_BASE")
            for player in tuple(getattr(self.server, "players", {}).values()):
                if int(getattr(player, "team", -1)) != int(team):
                    continue
                if not self._settled(getattr(player, "connection", None)):
                    continue
                send = getattr(player, "send", None)
                if callable(send):
                    send(data, reliable=True)
        self._repair_warning_active[team] = warning

    @staticmethod
    def _world_zone() -> ObjectiveZone:
        bounds = clamp_bounds((0, C.MAP_X - 1, 0, C.MAP_Y - 1, 0, C.MAP_Z - 1))
        return ObjectiveZone(255, bounds, (255.5, 255.5, 31.5))

    def _send_current_locks(self) -> None:
        for player in tuple(getattr(self.server, "players", {}).values()):
            self._send_player_lock(player, getattr(player, "connection", None))

    def _release_all_locks(self) -> None:
        packet = bytes(lock_to_zone_packet(self._world_zone()).generate())
        for player in tuple(getattr(self.server, "players", {}).values()):
            connection = getattr(player, "connection", None)
            if self._settled(connection):
                connection.send(packet, reliable=True)

    @staticmethod
    def _settled(connection) -> bool:
        """Only a GameScene past its join reveal may receive live locks;
        ``reveal_to`` replays the current lock to a loading joiner."""
        return connection is not None and bool(getattr(connection, "in_game", True))

    def _send_player_lock(self, player, connection, *, reveal: bool = False) -> None:
        if player is None or connection is None:
            return
        if not reveal and not self._settled(connection):
            return
        team = int(getattr(player, "team", -1))
        zone = (
            self.base_zones.get(team)
            if self.phase == "building"
            else self._world_zone()
        )
        if zone is not None:
            connection.send(
                bytes(lock_to_zone_packet(zone).generate()),
                reliable=True,
            )


__all__ = ["DemolitionMode"]
