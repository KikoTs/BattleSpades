"""Retail-style Multi-Hill objective mode."""

from __future__ import annotations

import asyncio
from collections import deque
import logging
import math
import time
from types import SimpleNamespace

import shared.constants as C
import shared.constants_gamemode as CG

from server import achievements, mode_data
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL

from .airstrike import (
    AIRSTRIKE_SPEC,
    NO_OWNER_PLAYER_ID,
    _sound_airstrike_flyby,
    sound_airstrike_siren,
)
from .base_mode import BaseMode
from .territory_control import CapturePointResupply
from .objective_zones import (
    ObjectiveZone,
    around,
    from_map_zone,
    minimap_zone_clear_packet,
    minimap_zone_packet,
)


logger = logging.getLogger(__name__)

_PLAYABLE_TEAMS = (TEAM1, TEAM2)
_NEUTRAL_COLOR = (255, 255, 255)
_CONTESTED_COLOR = (255, 0, 0)  # Both teams present: show the hill in red.
_FALLBACK_RADIUS = 16.0
# Identical positions and launch speed to the working retail airstrike.
# The difference is WHEN each shell is spawned, not its physics.
_AIRSTRIKE_PATTERN = (
    (0.0, 0.0),
    (-5.0, 0.0),
    (5.0, 0.0),
    (0.0, -5.0),
    (0.0, 5.0),
)
# Boundary flicker must not replay "Hill contested!" every capture tick; the
# retail TC shout cooldown (TC_NEW_TEAM_ENTERS_SHOUT_COOLDOWN) is reused.
_CONTESTED_SHOUT_COOLDOWN = float(CG.TC_NEW_TEAM_ENTERS_SHOUT_COOLDOWN)


def _configured_rule(server, key: str, rule: str, fallback):
    resolver = getattr(getattr(server, "config", None), "mode_rule", None)
    if callable(resolver):
        try:
            value = resolver("mh", key, rule)
            if value is not None and value is not False:
                return value
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    overlay = getattr(getattr(server, "config", None), "mode_settings", {}).get(
        "mh", {}
    )
    return overlay.get(key, fallback)


class MultiHillMode(BaseMode):
    """Fight over rotating shared hill volumes.

    The native HUD represents a hill with packet 43/icon 2.  Ownership is the
    zone colour, not its visibility key, so every hill remains TEAM_NEUTRAL
    (shared) while blue/green ownership changes are resent in place.
    """

    name = "Multi-Hill"
    description = (
        "Both teams fight to control changing Hill points. "
        "Watch out for airstrikes!"
    )
    mode_code = "mh"
    # This is also set per-instance. Never inherit BaseMode's score target.
    score_limit = 0
    BUILD_MARKER = "MH_AIRSTRIKE_FIXED_2026_10_16_V7"

    def __init__(self, server) -> None:
        super().__init__(server)
        data = mode_data.get(self.mode_code)
        # Multi-Hill is time-only: scores determine the winner exclusively
        # when the match clock expires. In BaseMode, score_limit <= 0 also
        # disables generic score-based victory checks.
        self.score_limit = 0
        resolve_time = getattr(getattr(server, "config", None), "configured_time_limit", None)
        self.time_limit = (
            float(resolve_time(self.mode_code, data.default_time_limit))
            if callable(resolve_time)
            else float(data.default_time_limit)
        )
        self.max_active_bases = max(1, int(_configured_rule(
            server,
            "max_active_bases",
            "RULE_MULTIHILL_MAX_ACTIVE_BASES",
            CG.MH_DEFAULT_NUMBER_OF_BASE_TO_ACTIVATE_AT_ONCE,
        )))
        self.base_active_time = max(1.0, float(_configured_rule(
            server,
            "base_active_time",
            "RULE_BASE_ACTIVE_TIME",
            CG.MH_DEFAULT_BASE_AUTO_TIMEOUT,
        )))

        self.zones: list[ObjectiveZone] = []
        self.active_zones: list[ObjectiveZone] = []
        self.zone_owner: dict[int, int | None] = {}
        self.zone_contested: dict[int, bool] = {}
        # Live occupant ids per active hill, refreshed every control pass.
        self.zone_occupants: dict[int, dict[int, set[int]]] = {}
        # Active hills whose "First to Hill" award was already paid.
        self._first_paid: set[int] = set()
        self._contested_shout_at: dict[int, float] = {}
        self._next_personal_score_at = 0.0
        self.phase = "waiting"
        self._rotation_cursor = 0
        self._next_rotation_at = 0.0
        self._next_activation_at = 0.0
        self._last_score_at = 0.0
        self.resupply = CapturePointResupply(server, self.mode_code)
        # No per-tick polling to launch shells: the first one is spawned
        # synchronously when the zone expires, the rest by an asyncio task.
        overlay = getattr(getattr(server, "config", None), "mode_settings", {}).get(
            self.mode_code, {}
        )
        if not isinstance(overlay, dict):
            overlay = {}
        self.airstrike_shell_interval = max(
            0.15, float(overlay.get("airstrike_shell_interval", 0.65))
        )
        self._airstrike_shell_queue: deque[
            tuple[tuple[float, float, float], float, float, bool]
        ] = deque()
        self._airstrike_task: asyncio.Task | None = None

    def configure_state_data(self, packet) -> None:
        """Tell stock clients this match has NO score target.

        StateData defaults may carry the old Multi-Hill target of 100.
        Suppress the HUD's max-score bars but keep current team scores.
        Do not broadcast StateData during gameplay (it resets the scene).
        """
        packet.score_limit = 0
        packet.team1_show_max_score = False
        packet.team2_show_max_score = False

    async def check_win_condition(self) -> None:
        """Always wait for the clock, irrespective of accumulated points."""
        return None

    async def _end_by_score(self, winner: int) -> None:
        """Protect against any generic/legacy score-win dispatcher."""
        logger.warning(
            "%s: blocked an attempted SCORE-limit end (team=%r, scores=%s)",
            self.BUILD_MARKER,
            winner,
            {team_id: team.score for team_id, team in self.server.teams.items()},
        )

    async def on_mode_start(self) -> None:
        await self._cancel_airstrike_task()
        self.score_limit = 0
        await super().on_mode_start()
        # Some server wrappers reload configured limits at a round restart.
        self.score_limit = 0
        logger.warning(
            "%s LOADED from %s; SCORE LIMIT DISABLED; match time=%.1fs",
            self.BUILD_MARKER, __file__, float(self.time_limit),
        )
        for team in self.server.teams.values():
            team.reset()
        # An in-place restart reuses this instance: retire the previous
        # round's hill icons before the new rotation, or clients keep a
        # stale packet-43 zone next to the fresh one.
        self._clear_active_zones()
        self.zones = self._build_zones()
        self.active_zones = []
        self.zone_owner = {zone.index: None for zone in self.zones}
        self.zone_contested = {zone.index: False for zone in self.zones}
        self.zone_occupants = {}
        self._contested_shout_at = {}
        self.resupply.reset()
        self.phase = "waiting"
        self._rotation_cursor = 0
        now = time.time()
        self._last_score_at = now
        self._activate_next(now)
        self.broadcast_start_cue()
        logger.info(
            "Multi-Hill started with %d zones (%d active, %.0fs rotation)",
            len(self.zones),
            len(self.active_zones),
            self.base_active_time,
        )

    async def deactivate(self) -> None:
        await self._cancel_airstrike_task()
        self._clear_active_zones()
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        """Settle hill points before resolving the match's time deadline.

        BaseMode ends a match when the clock expires. If called first, its
        ``ended`` flag may prevent the last eligible hill score tick from
        being counted. Keep the objective clock bound to the same wall-clock
        deadline, and use a bounded timeout fallback if queued mode events
        delay BaseMode's final result beyond the HUD's 00:00 display.
        """
        if self.ended:
            return
        if self.score_limit != 0:
            logger.error(
                "%s: score_limit was overwritten to %r; resetting to 0",
                self.BUILD_MARKER, self.score_limit,
            )
            self.score_limit = 0
        now = time.time()
        deadline = (
            self.start_time + float(self.time_limit)
            if self.started and self.time_limit > 0
            else None
        )
        in_time = deadline is None or now < deadline

        if self.phase == "intermission":
            # Never activate a new hill after the match timer expires.
            if in_time and now >= self._next_activation_at:
                self._activate_next(now)
        elif self.phase == "active":
            # Arrivals after the deadline must not claim an expired hill.
            if in_time and now < self._next_rotation_at:
                self._update_control(now)
            score_until = min(
                now,
                self._next_rotation_at,
                deadline if deadline is not None else now,
            )
            await self._award_team_ticks(score_until)
            if self.ended:
                return
            if in_time:
                self._award_presence_scores(score_until)
                if now >= self._next_rotation_at:
                    expired = tuple(self.active_zones)
                    if expired:
                        # Retail BASE_DEPLETED "Hill depleted! \nAirstrike incoming!"
                        self.announce_localised(
                            "BASE_DEPLETED", override_previous=True
                        )
                    for zone in expired:
                        achievements.hill_expired(
                            self.server, zone,
                            self.zone_occupants.get(zone.index),
                        )
                        self._start_airstrike(zone.center)
                    self._clear_active_zones()
                    self.zone_occupants = {}
                    self.phase = "intermission"
                    self._next_activation_at = (
                        now + float(CG.MH_TIME_BETWEEN_BASE_ACTIVATIONS)
                    )

        # The common mode handles the clock, countdown cues and normal end
        # sequence. Do this *after* settling eligible objective points.
        await super().on_tick(tick)
        if self.ended:
            return

        # In rare overloads the mode-event drain cannot empty the captured
        # timeout queue promptly. Never leave players stuck at HUD 00:00.
        # Allow a one-second grace for pre-deadline mode events to settle.
        if deadline is not None and now >= deadline + 1.0:
            logger.warning(
                "Multi-Hill forced clock result after timeout drain delay"
            )
            await self._end_by_time()

    def _start_airstrike(self, center) -> None:
        """Start a real, five-shell objective airstrike immediately.

        The first shell is launched during hill expiry (as in the working V5),
        so no later tick is needed to make the strike visible. Remaining
        shells are spaced in real time. Multiple expiring hills share a single
        sequence, preventing simultaneous bursts.
        """
        if self.ended or getattr(self, "retiring", False):
            return
        center = tuple(float(value) for value in center[:3])
        sound_airstrike_siren(self.server, center)
        for index, (dx, dy) in enumerate(_AIRSTRIKE_PATTERN):
            self._airstrike_shell_queue.append((center, dx, dy, index == 0))

        logger.info(
            "%s: airstrike at %s queued (%d shells)",
            self.BUILD_MARKER, center, len(self._airstrike_shell_queue),
        )
        # There is never a delayed FIRST projectile: this was the main
        # difference from the previously working immediate-launch path.
        if self._airstrike_task is None or self._airstrike_task.done():
            self._fire_next_airstrike_shell()
            if self._airstrike_shell_queue:
                self._airstrike_task = asyncio.create_task(
                    self._drain_airstrike_shells()
                )

    def _fire_next_airstrike_shell(self) -> bool:
        """Use the same engine, CreateEntity path, and speed as airstrike.py."""
        if not self._airstrike_shell_queue:
            return False
        center, dx, dy, first_shell = self._airstrike_shell_queue.popleft()
        if first_shell:
            _sound_airstrike_flyby(self.server, center)
        engine = getattr(self.server, "projectile_engine", None)
        spawn_visual = getattr(self.server, "spawn_projectile_entity", None)
        spawn = getattr(engine, "spawn_spec", None)
        if not callable(spawn):
            logger.error(
                "%s: airstrike NOT SPAWNED: server.projectile_engine.spawn_spec missing",
                self.BUILD_MARKER,
            )
            return False
        position = (
            center[0] + dx,
            center[1] + dy,
            max(1.0, center[2] - 48.0),
        )
        velocity = (0.0, 0.0, float(C.AIRSTRIKE_SHELL_SPEED))
        try:
            projectile = spawn(
                AIRSTRIKE_SPEC, position, velocity, thrower_id=-1
            )
            if callable(spawn_visual):
                owner = SimpleNamespace(
                    id=NO_OWNER_PLAYER_ID, team=TEAM_NEUTRAL
                )
                spawn_visual(projectile, owner, position, velocity)
            else:
                logger.error(
                    "%s: airstrike projectile spawned but CreateEntity service missing",
                    self.BUILD_MARKER,
                )
            logger.debug(
                "%s: airstrike shell at %s velocity %s", 
                self.BUILD_MARKER, position, velocity,
            )
            return True
        except Exception:
            logger.exception("%s: airstrike shell spawn failed", self.BUILD_MARKER)
            return False

    async def _drain_airstrike_shells(self) -> None:
        """Spread the remaining projectiles; no tick-phase checks needed."""
        try:
            while self._airstrike_shell_queue:
                await asyncio.sleep(self.airstrike_shell_interval)
                if self.ended or getattr(self, "retiring", False):
                    break
                self._fire_next_airstrike_shell()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s: airstrike sequence interrupted", self.BUILD_MARKER)
        finally:
            self._airstrike_shell_queue.clear()
            self._airstrike_task = None

    async def _cancel_airstrike_task(self) -> None:
        task = self._airstrike_task
        self._airstrike_shell_queue.clear()
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._airstrike_task = None

    def reveal_to(self, connection) -> None:
        super().reveal_to(connection)
        for zone in self.active_zones:
            self._send_zone(zone, connection=connection)
        # Retail mode-start cue (string-table only, no client binary sends
        # it), replayed per settled GameScene like TDM/Diamond.
        self.send_start_cue_to(connection)

    def start_cue_for(self, player):
        return "MULTI_HILL_START"

    async def on_player_kill(self, killer, victim, kill_type: int) -> None:
        """Generic retail kill score, then the hill defend/assault extras."""
        await super().on_player_kill(killer, victim, kill_type)
        if (
            self.ended
            or killer is None
            or killer is victim
            or int(getattr(killer, "team", -1)) not in _PLAYABLE_TEAMS
            or int(getattr(victim, "team", -1)) not in _PLAYABLE_TEAMS
            or int(killer.team) == int(victim.team)
            or self.phase != "active"
        ):
            return
        killer_zone = self._active_zone_at(killer)
        victim_zone = self._active_zone_at(victim)
        team = int(killer.team)
        achievements.hill_kill(
            self.server, killer, victim,
            killer_in_hill=killer_zone is not None,
            victim_in_hill=victim_zone is not None,
        )
        if (
            killer_zone is not None
            and self.zone_owner.get(killer_zone.index) == team
        ):
            self._award_player_score(
                killer,
                int(CG.MH_SCORE_DEFEND),
                int(C.SCORE_REASON.MH_DEFEND_SCORE_REASON),
                killer_zone,
            )
        elif (
            victim_zone is not None
            and self.zone_owner.get(victim_zone.index) != team
        ):
            self._award_player_score(
                killer,
                int(CG.MH_SCORE_ASSAULT),
                int(C.SCORE_REASON.MH_ASSAULT_SCORE_REASON),
                victim_zone,
            )

    def _active_zone_at(self, player) -> ObjectiveZone | None:
        position = getattr(player, "position", None)
        if position is None:
            position = (player.x, player.y, player.z)
        return next(
            (zone for zone in self.active_zones if zone.contains(position)),
            None,
        )

    def escape_watch_objective_player(self, player) -> bool:
        """A player standing on an active hill is holding an objective."""
        return self._active_zone_at(player) is not None

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
            return zones

        # Stock clients shipped the VXLs but not the official server's zone
        # sidecars.  Build deterministic dry objectives along and beside the
        # two team anchors, never arbitrary world coordinates or water beds.
        anchor_reader = getattr(wm, "team_base_anchor", None)
        if callable(anchor_reader):
            first = tuple(float(v) for v in anchor_reader(TEAM1))
            second = tuple(float(v) for v in anchor_reader(TEAM2))
        else:
            first, second = (64.0, 256.0, 58.0), (448.0, 256.0, 58.0)
        dx, dy = second[0] - first[0], second[1] - first[1]
        distance = max(1.0, math.hypot(dx, dy))
        px, py = -dy / distance, dx / distance
        lateral = min(80.0, distance * 0.20)
        candidates = [
            (first[0] + dx * 0.35, first[1] + dy * 0.35),
            (first[0] + dx * 0.50, first[1] + dy * 0.50),
            (first[0] + dx * 0.65, first[1] + dy * 0.65),
            (first[0] + dx * 0.50 + px * lateral,
             first[1] + dy * 0.50 + py * lateral),
            (first[0] + dx * 0.50 - px * lateral,
             first[1] + dy * 0.50 - py * lateral),
        ]
        ground = getattr(wm, "dry_ground_anchor", None)
        seen: set[tuple[int, int]] = set()
        zones = []
        for x, y in candidates:
            center = ground(x, y, 48) if callable(ground) else (x, y, 58.0)
            key = (int(center[0]), int(center[1]))
            if key in seen:
                continue
            seen.add(key)
            zones.append(around(
                len(zones), center, radius_xy=_FALLBACK_RADIUS,
                height_above=7.0, depth_below=10.0,
            ))
        if len(zones) < 2:
            zones = [
                around(0, first, radius_xy=_FALLBACK_RADIUS),
                around(1, second, radius_xy=_FALLBACK_RADIUS),
            ]
        logger.warning(
            "Map %s has no complete Multi-Hill sidecar; using %d dry fallback zones",
            getattr(wm, "map_name", "<unknown>"),
            len(zones),
        )
        return zones

    def _activate_next(self, now: float) -> None:
        if not self.zones:
            return
        count = min(len(self.zones), self.max_active_bases)
        selected = []
        for offset in range(count):
            selected.append(self.zones[(self._rotation_cursor + offset) % len(self.zones)])
        self._rotation_cursor = (self._rotation_cursor + count) % len(self.zones)
        self.active_zones = selected
        self.zone_occupants = {}
        for zone in selected:
            self._first_paid.discard(zone.index)
            self.zone_owner[zone.index] = None
            self.zone_contested[zone.index] = False
            self.zone_occupants[zone.index] = {TEAM1: set(), TEAM2: set()}
            self._send_zone(zone)
        self.phase = "active"
        self._last_score_at = now
        self._next_personal_score_at = now + float(CG.MH_SCORE_OCCUPY_INTERVAL)
        self._next_rotation_at = now + self.base_active_time

    def _clear_active_zones(self) -> None:
        for zone in tuple(self.active_zones):
            data = bytes(minimap_zone_clear_packet(zone).generate())
            self.server.broadcast(data, reliable=True)
        self.active_zones = []

    def _send_zone(self, zone: ObjectiveZone, connection=None) -> None:
        owner = self.zone_owner.get(zone.index)
        if self.zone_contested.get(zone.index, False):
            color = _CONTESTED_COLOR
        elif owner in _PLAYABLE_TEAMS:
            color = self.server.teams[owner].color
        else:
            color = _NEUTRAL_COLOR
        packet = minimap_zone_packet(
            zone,
            color=color,
            icon_id=int(CG.ZONE_ICON_MULTIHILL),
            visible_team=TEAM_NEUTRAL,
        )
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            connection.send(data, reliable=True)

    def _update_control(self, now: float | None = None) -> None:
        if now is None:
            now = time.time()
        players = tuple(getattr(self.server, "players", {}).values())
        for zone in self.active_zones:
            occupants = {TEAM1: [], TEAM2: []}
            for player in players:
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
                # cannot hold or contest a hill.
                if zone.contains(position) and self.objective_presence_eligible(player):
                    occupants[team].append(player)

            self.zone_occupants[zone.index] = {
                team: {int(player.id) for player in occupants[team]}
                for team in _PLAYABLE_TEAMS
            }
            blue, green = len(occupants[TEAM1]), len(occupants[TEAM2])
            if zone.index not in self._first_paid and (blue or green):
                # "First to Hill": the first body onto a freshly activated
                # hill, whichever team (lowest id breaks a same-tick tie).
                self._first_paid.add(zone.index)
                first = min(
                    occupants[TEAM1] + occupants[TEAM2],
                    key=lambda p: int(getattr(p, "id", 0)),
                )
                self._award_player_score(
                    first,
                    int(CG.MH_SCORE_FIRST),
                    int(C.SCORE_REASON.MH_FIRST_SCORE_REASON),
                    zone,
                )
            was_contested = self.zone_contested.get(zone.index, False)
            contested = blue > 0 and green > 0
            self.zone_contested[zone.index] = contested
            if was_contested and not contested and blue != green:
                # The contest is over with one team left on the hill: it kept
                # its hill or took the contested one.
                left = TEAM1 if blue > green else TEAM2
                achievements.hill_contest_won(
                    self.server, occupants[left],
                    retained=self.zone_owner.get(zone.index) == left,
                )
            if contested and not was_contested:
                # Ownership stays on the server for scoring/defence logic,
                # but all clients must see a contested hill in red.
                self._send_zone(zone)
                last = self._contested_shout_at.get(zone.index)
                if last is None or now - last >= _CONTESTED_SHOUT_COOLDOWN:
                    self._contested_shout_at[zone.index] = now
                    self.announce_localised("MULTIHILL_CONTESTED")
            # Same rule as TC: a contested hill is held -- nobody claims or
            # flips it until one team has it alone (rules audit 2026-09-27
            # #19, inferred from the CONTESTED/"Contend" retail events).
            if not contested:
                holder = self.zone_owner.get(zone.index)
                if holder in _PLAYABLE_TEAMS:
                    for player in occupants[holder]:
                        self.resupply.offer(player, now)
            # A hill has no owner when nobody is holding it. Without this
            # transition, a deserted zone keeps the previous team's colour
            # and continues awarding team points until an enemy captures it.
            if blue == 0 and green == 0:
                if self.zone_owner.get(zone.index) is not None or was_contested:
                    self.zone_owner[zone.index] = None
                    self._send_zone(zone)
                continue
            # Both teams present: zone is red; team score ticks are paused.
            # A team's previous owner id is retained solely for claim bonuses.
            if contested:
                continue
            claimant = TEAM1 if blue > green else TEAM2
            old_owner = self.zone_owner.get(zone.index)
            if claimant == old_owner:
                # Contest ended, but the old owner kept the hill: resend its
                # team colour even though the owner id did not change.
                if was_contested:
                    self._send_zone(zone)
                continue
            self.zone_owner[zone.index] = claimant
            self._send_zone(zone)

            # Personal claim scoring mirrors TC's CLAIM/CONTROL pair: taking
            # the neutral hill is "Claim Hill" (MH_SCORE_CLAIM), taking it
            # from the enemy is "Control Hill" (MH_SCORE_CONTROL); "First to
            # Hill" is paid separately above. Every claiming occupant is
            # paid; holding is scored per interval. The announcement names
            # the lowest id, as before.
            claimers = sorted(occupants[claimant], key=lambda p: int(getattr(p, "id", 0)))
            decisive = claimers[0]
            if old_owner is None:
                reason = int(C.SCORE_REASON.MH_CLAIM_SCORE_REASON)
                points = int(CG.MH_SCORE_CLAIM)
            else:
                reason = int(C.SCORE_REASON.MH_CONTROL_SCORE_REASON)
                points = int(CG.MH_SCORE_CONTROL)
            for claimer in claimers:
                self._award_player_score(claimer, points, reason, zone)
            self._announce_claim(decisive, claimant, old_owner)

    async def _award_team_ticks(self, now: float) -> None:
        interval = float(CG.MH_TEAM_SCORE_TICK_RATE)
        elapsed = max(0.0, now - self._last_score_at)
        ticks = int(elapsed / interval)
        if ticks <= 0:
            return
        self._last_score_at += ticks * interval
        changed = set()
        for zone in self.active_zones:
            owner = self.zone_owner.get(zone.index)
            if owner not in _PLAYABLE_TEAMS or self.zone_contested.get(zone.index):
                continue
            team = self.server.teams[owner]
            team.add_score(ticks * int(CG.MH_TEAM_SCORE_PER_TICK))
            changed.add(owner)
        # Fixed team order: a set's iteration order must not pick the winner.
        for team_id in _PLAYABLE_TEAMS:
            if team_id not in changed:
                continue
            team = self.server.teams[team_id]
            try:
                self.server.broadcast_set_score(
                    team,
                    reason=int(C.SCORE_REASON.MH_CONTROL_SCORE_REASON),
                )
            except TypeError:
                self.server.broadcast_set_score(team)

    def _award_presence_scores(self, now: float) -> None:
        """MH_SCORE_OCCUPY / MH_SCORE_CONTEST every MH_SCORE_OCCUPY_INTERVAL.

        Mirrors TC's presence scoring: on a contested hill every occupant
        earns the contest award; otherwise the owner's occupants earn the
        occupy award.  Missed intervals (a stalled tick) are paid in one row.
        """
        if self.phase != "active" or now < self._next_personal_score_at:
            return
        interval = float(CG.MH_SCORE_OCCUPY_INTERVAL)
        periods = int((now - self._next_personal_score_at) / interval) + 1
        self._next_personal_score_at += periods * interval
        players = getattr(self.server, "players", {})
        for zone in self.active_zones:
            occupants = self.zone_occupants.get(zone.index) or {}
            if self.zone_contested.get(zone.index):
                awards = [
                    (player_id, team, int(CG.MH_SCORE_CONTEST),
                     int(C.SCORE_REASON.MH_CONTEST_SCORE_REASON))
                    for team in _PLAYABLE_TEAMS
                    for player_id in sorted(occupants.get(team, ()))
                ]
            else:
                owner = self.zone_owner.get(zone.index)
                if owner not in _PLAYABLE_TEAMS:
                    continue
                awards = [
                    (player_id, owner, int(CG.MH_SCORE_OCCUPY),
                     int(C.SCORE_REASON.MH_OCCUPY_SCORE_REASON))
                    for player_id in sorted(occupants.get(owner, ()))
                ]
            for player_id, team, points, reason in awards:
                player = players.get(player_id)
                if player is None or not bool(getattr(player, "alive", False)):
                    continue
                if int(getattr(player, "team", -1)) != int(team):
                    # A reused id or a team switch since the last capture
                    # sample: that body did not hold this hill.
                    continue
                self._award_player_score(player, periods * points, reason, zone)

    def _award_player_score(self, player, points: int, reason: int, zone=None) -> None:
        if not self._owns_slot(player):
            return
        from server.scoreboard import send_player_score

        player.score = int(getattr(player, "score", 0)) + int(points)
        send_player_score(self.server, player, reason=int(reason))
        if zone is not None:
            achievements.hill_scored(self.server, zone.index, player)

    def _announce_claim(self, claimant, team: int, old_owner) -> None:
        """Team-relative claim cue through the in-game-gated base helpers."""
        name = str(getattr(claimant, "name", f"Player {claimant.id}"))
        team = int(team)
        enemy = TEAM2 if team == TEAM1 else TEAM1
        self.announce_localised_to_player(
            claimant, "MULTIHILL_OCCUPIED_YOU", (name,)
        )
        self.announce_localised_to_team(
            team, "MULTIHILL_OCCUPIED_FRIENDLY", (name,), exclude=claimant
        )
        if old_owner == enemy:
            self.announce_localised_to_team(enemy, "MULTIHILL_LOST")
        else:
            self.announce_localised_to_team(
                enemy, "MULTIHILL_OCCUPIED_ENEMY", (name,)
            )
        from server.audio import play_team_relative

        play_team_relative(self.server, team)


__all__ = ["MultiHillMode"]
