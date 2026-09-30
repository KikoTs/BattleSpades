"""Retail-style Diamond Mine objective mode."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL

from . import objective_guard
from .base_mode import BaseMode
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
_FALLBACK_RADIUS = 12.0
# Three times the blocks the best retail chance needs on average.
DEFAULT_DISCOVERY_GUARANTEE_BLOCKS = 300


def _configured_rule(server, key: str, rule: str, fallback):
    resolver = getattr(getattr(server, "config", None), "mode_rule", None)
    if callable(resolver):
        try:
            value = resolver("dia", key, rule)
            if value is not None and value is not False:
                return value
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    overlay = getattr(getattr(server, "config", None), "mode_settings", {}).get(
        "dia", {}
    )
    return overlay.get(key, fallback)


@dataclass(slots=True)
class DiamondDropoff:
    """One authored cash-in volume with its remaining retail capacity."""

    zone: ObjectiveZone
    team: int = TEAM_NEUTRAL
    capacity: int = 1
    remaining: int = 1
    active: bool = False


@dataclass(slots=True)
class GroundDiamond:
    """One authoritative diamond entity awaiting pickup."""

    entity_id: int
    serial: int
    position: tuple[float, float, float]
    spawned_at: float
    expires_at: float
    pickup_after: float
    # Team of the player who last carried it; None for a diamond that was
    # only ever mined. A loose diamond is cashed in for this team.
    last_team: int | None = None


class DiamondMineMode(BaseMode):
    """Mine hidden diamonds, carry one as the native tool, and cash it in."""

    name = "Diamond Mine"
    description = "Mine to find diamonds, then cash them in at drop-off points!"
    mode_code = "dia"

    def __init__(self, server) -> None:
        super().__init__(server)
        data = mode_data.get(self.mode_code)
        resolve_time = getattr(getattr(server, "config", None), "configured_time_limit", None)
        self.time_limit = (
            float(resolve_time(self.mode_code, data.default_time_limit))
            if callable(resolve_time)
            else float(data.default_time_limit)
        )
        self.score_limit = max(1, int(_configured_rule(
            server, "score_limit", "RULE_DIA_SCORE_TARGET",
            CG.DIA_DIAMONDS_TO_GET_FOR_MAP_ROTATION,
        )))
        self.max_active_bases = max(1, min(5, int(_configured_rule(
            server, "max_active_bases", "RULE_DIAMOND_MAX_ACTIVE_BASES",
            CG.DIA_DEFAULT_ACTIVE_BASES_AT_ONCE,
        ))))
        self.max_active_diamonds = max(1, min(5, int(_configured_rule(
            server, "max_active_diamonds", "RULE_MAX_ACTIVE_DIAMONDS",
            CG.DIA_DEFAULT_MAX_ACTIVE_DIAMONDS,
        ))))
        self.diamond_lifetime = max(1.0, float(_configured_rule(
            server, "diamond_lifetime", "RULE_DIAMOND_LIFETIME", 60.0
        )))
        # [modes.dia] loose_cash_in: a dropped or thrown diamond that rests
        # in a drop-off scores for the team that carried it.
        self.loose_cash_in = bool(getattr(
            getattr(server, "config", None), "mode_settings", {}
        ).get("dia", {}).get("loose_cash_in", True))
        # [modes.dia] discovery_guarantee_blocks: retail is pure chance (one
        # diamond per 100 mined blocks at best), so a round can stay empty
        # for a long time. While no diamond is in play, the block that
        # brings the count since the last find to this number uncovers one.
        # 0 = retail chance only.
        self.discovery_guarantee_blocks = max(0, int(getattr(
            getattr(server, "config", None), "mode_settings", {}
        ).get("dia", {}).get(
            "discovery_guarantee_blocks", DEFAULT_DISCOVERY_GUARANTEE_BLOCKS
        )))
        self._mined_since_discovery = 0
        self.dropoffs: list[DiamondDropoff] = []
        self.active_dropoffs: list[DiamondDropoff] = []
        self.ground_diamonds: dict[int, GroundDiamond] = {}
        self.carriers: dict[int, int] = {}
        self._serial = 1
        self._rotation_cursor = 0
        self._next_discovery_at = 0.0
        self._next_carry_score_at = 0.0
        self._rng = random.Random()
        from server.combat_scores import EscortTracker

        self._escorts = EscortTracker(
            float(CG.DIA_ESCORT_RADIUS), float(CG.DIA_ESCORT_HYSTERESIS)
        )
        # serial -> (uncovering player or None, teams that have carried it):
        # feeds DIA_STEAL_TOTAL / DIA_FINDANDCASHIN_TOTAL (COM_DIA_STEAL).
        self._diamond_history: dict[int, tuple[object, set[int]]] = {}
        # Blocks removed since the last report, for the once-a-minute log
        # line: "no diamonds" is nearly always "nobody is digging".
        self._dig_report = {"mined": 0, "other": 0, "found": 0}
        self._next_dig_report_at = 0.0

    def _report_digging(self, now: float) -> None:
        if now < self._next_dig_report_at:
            return
        if self._next_dig_report_at:
            report = self._dig_report
            logger.info(
                "Diamond Mine, last minute: %d blocks mined, %d removed by "
                "other means, %d diamonds uncovered; %d on the ground, %d carried",
                report["mined"], report["other"], report["found"],
                len(self.ground_diamonds), len(self.carriers),
            )
        self._dig_report = {"mined": 0, "other": 0, "found": 0}
        self._next_dig_report_at = float(now) + 60.0

    async def on_mode_start(self) -> None:
        # Clear before the base class rebuilds map resources: on a round
        # restart the registry was already wiped and its ids restart at 0, so
        # a later clear would DestroyEntity freshly rebuilt crates.
        self._clear_runtime_entities()
        await super().on_mode_start()
        for team in self.server.teams.values():
            team.reset()
        # An in-place restart keeps the GameScene: clear the previous
        # round's drop-off minimap zones before the rebuilt list replaces
        # them, or their icons stay on every client forever.
        for dropoff in tuple(self.active_dropoffs):
            dropoff.active = False
            self._clear_dropoff(dropoff)
        self.dropoffs = self._build_dropoffs()
        self.active_dropoffs = []
        self.carriers.clear()
        self._escorts.reset()
        self._diamond_history.clear()
        self._rotation_cursor = 0
        self._mined_since_discovery = 0
        self._activate_next_dropoffs()
        self.broadcast_start_cue()
        now = time.time()
        self._next_discovery_at = now
        self._next_carry_score_at = now + float(CG.DIA_SCORE_CARRY_INTERVAL)
        logger.info(
            "Diamond Mine started with %d drop-offs (%d active), max %d diamonds",
            len(self.dropoffs),
            len(self.active_dropoffs),
            self.max_active_diamonds,
        )

    async def deactivate(self) -> None:
        for dropoff in tuple(self.active_dropoffs):
            self._clear_dropoff(dropoff)
        self._clear_runtime_entities()
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        await super().on_tick(tick)
        if self.ended:
            return
        now = time.time()
        self._report_digging(now)
        registry = getattr(self.server, "entity_registry", None)
        for diamond in tuple(self.ground_diamonds.values()):
            if now < diamond.expires_at:
                # Keep the wire fuse at the remaining lifetime so a late
                # joiner's CreateEntity replay counts down from the live value.
                entity = registry.get(diamond.entity_id) if registry else None
                if entity is not None:
                    entity.fuse = max(0.0, float(diamond.expires_at - now))
            if now >= diamond.expires_at:
                self._remove_ground_diamond(diamond.entity_id)
                # An expired serial can never cash in. Do not retain its
                # uncovering Player (and connection/world graph) for the match.
                self._diamond_history.pop(diamond.serial, None)
                # A diamond left on the ground too long vanishes with its
                # own cue where it lay (DIAMOND_DISAPPEAR, server-only id).
                from server.audio import SND_DIAMOND_DISAPPEAR, play_sound

                play_sound(
                    self.server, SND_DIAMOND_DISAPPEAR, position=diamond.position
                )
            elif diamond.last_team is not None:
                # Also reached when a drop-off opens around a resting diamond.
                await self._cash_in_loose(diamond)
                if self.ended:
                    return

        for player in tuple(getattr(self.server, "players", {}).values()):
            if not self._active_player(player):
                continue
            if int(player.id) not in self.carriers:
                diamond = self._nearest_ground_diamond(player)
                if diamond is not None and now >= diamond.pickup_after:
                    self._pickup_diamond(player, diamond)
            if int(player.id) in self.carriers:
                dropoff = self._cashable_dropoff(player)
                if dropoff is not None:
                    await self._cash_in(player, dropoff)
                    if self.ended:
                        return

        if now >= self._next_carry_score_at:
            periods = max(1, int(
                (now - self._next_carry_score_at)
                / float(CG.DIA_SCORE_CARRY_INTERVAL)
            ) + 1)
            self._next_carry_score_at += periods * float(CG.DIA_SCORE_CARRY_INTERVAL)
            self._award_carry_and_escort(periods)

    async def on_blocks_destroyed(
        self,
        player,
        positions: tuple[tuple[int, int, int], ...],
        mined: bool,
    ) -> None:
        """Roll once per mined voxel batch after the server commits terrain."""

        self._dig_report["mined" if mined else "other"] += len(positions)
        if (
            self.ended
            or not mined
            or not self._active_player(player)
            or not positions
        ):
            return
        self._mined_since_discovery += len(positions)
        if self._active_diamond_count() >= self.max_active_diamonds:
            return
        now = time.time()
        if now < self._next_discovery_at:
            return
        overdue = (
            self.discovery_guarantee_blocks > 0
            and self._active_diamond_count() == 0
            and self._mined_since_discovery >= self.discovery_guarantee_blocks
        )
        active_ratio = self._active_diamond_count() / float(
            max(1, self.max_active_diamonds)
        )
        chance = (
            float(CG.DIA_HIGHEST_DIAMOND_CHANCE)
            + (float(CG.DIA_LOWEST_DIAMOND_CHANCE)
               - float(CG.DIA_HIGHEST_DIAMOND_CHANCE)) * active_ratio
        )
        # A bulk spade/prefab removal still represents multiple independently
        # mined blocks.  This exact complement calculation preserves per-voxel
        # chance while spawning at most one diamond from one server event.
        event_chance = 1.0 - (1.0 - chance) ** len(positions)
        if not overdue and self._rng.random() > event_chance:
            return
        position = positions[self._rng.randrange(len(positions))]
        self._dig_report["found"] += 1
        self._mined_since_discovery = 0
        self._spawn_diamond((
            float(position[0]) + 0.5,
            float(position[1]) + 0.5,
            float(position[2]) + 0.5,
        ), now=now, uncovered_by=player)
        # Only a mined discovery starts the spawn cooldown; a carrier's drop
        # re-places an existing diamond and must not delay the next find.
        self._next_discovery_at = float(now) + float(CG.DIA_TIME_BETWEEN_DIAMOND_SPAWN)

    async def on_player_death(self, player, killer, kill_type: int) -> None:
        await super().on_player_death(player, killer, kill_type)
        self._award_kill_events(player, killer, kill_type)
        await self._drop_carried_diamond(player)

    # Retail objective kill events (see server.combat_scores).
    _KILL_EVENT_AMOUNTS = {
        "intercept": int(CG.DIA_SCORE_INTERCEPT),
        "carrier_defend": int(CG.DIA_SCORE_CARRIER_DEFEND),
        "defend": int(CG.DIA_SCORE_DEFEND),
        "assault": int(CG.DIA_SCORE_ASSAULT),
        "assault_enemy": int(CG.DIA_SCORE_ASSAULT),
        "distract": int(CG.DIA_SCORE_DISTRACT),
    }
    _KILL_EVENT_REASONS = {
        "intercept": int(C.SCORE_REASON.DIA_INTERCEPT_SCORE_REASON),
        "carrier_defend": int(C.SCORE_REASON.DIA_CARRIER_DEFEND_SCORE_REASON),
        "defend": int(C.SCORE_REASON.DIA_DEFEND_SCORE_REASON),
        "assault": int(C.SCORE_REASON.DIA_ASSAULT_SCORE_REASON),
        "assault_enemy": int(C.SCORE_REASON.DIA_ASSAULT_SCORE_REASON),
        "distract": int(C.SCORE_REASON.DIA_DISTRACT_SCORE_REASON),
    }

    def _team_carriers(self, team: int) -> list:
        players = getattr(self.server, "players", {})
        result = []
        for player_id in self.carriers:
            carrier = players.get(player_id)
            if carrier is not None and getattr(carrier, "team", None) == team:
                result.append(carrier)
        return result

    def _award_kill_events(self, victim, killer, kill_type: int) -> None:
        """Objective kill events, evaluated BEFORE the victim drops a diamond.

        Intercept Carrier: kill an enemy diamond carrier. Carrier Defend:
        kill an enemy within DIA_CARRIER_THREAT_RADIUS (10) of your carrier.
        Diamond Defend: kill an enemy within DIA_THREAT_RADIUS (20) of a
        ground diamond. Diamond Assault: a kill at an active drop-off your
        team may use (you or the victim within DIA_THREAT_RADIUS of it).
        Diamond Distraction: the victim died to an enemy within
        DIA_ESCORT_RADIUS (15) of its own carrier.
        """
        from server import combat_scores as cs

        if self.ended or not cs.eligible_kill(killer, victim, kill_type):
            return
        killer_team, victim_team = int(killer.team), int(victim.team)
        event = cs.classify_objective_kill(
            killer, victim,
            victim_carrying=int(getattr(victim, "id", -1)) in self.carriers,
            killer_team_carriers=self._team_carriers(killer_team),
            defend_points=[d.position for d in self.ground_diamonds.values()],
            attack_points=[
                dropoff.zone.center for dropoff in self.active_dropoffs
                if dropoff.remaining > 0
                and dropoff.team in (TEAM_NEUTRAL, killer_team)
            ],
            carrier_threat_radius=float(CG.DIA_CARRIER_THREAT_RADIUS),
            threat_radius=float(CG.DIA_THREAT_RADIUS),
        )
        cs.award_kill_event(
            self.server, killer, event,
            self._KILL_EVENT_AMOUNTS, self._KILL_EVENT_REASONS, mode=self,
        )
        if cs.is_distraction(
            victim, killer, kill_type, self._team_carriers(victim_team),
            float(CG.DIA_ESCORT_RADIUS),
        ):
            cs.award_kill_event(
                self.server, victim, "distract",
                self._KILL_EVENT_AMOUNTS, self._KILL_EVENT_REASONS, mode=self,
            )

    async def on_player_leave(self, player) -> None:
        await self._drop_carried_diamond(player)

    async def on_player_team_change(self, player, old_team: int, new_team: int) -> None:
        # The diamond stays with the team that carried it, not the new one.
        await self._drop_carried_diamond(player, team=old_team)

    async def handle_drop_pickup(self, player, position, velocity) -> bool:
        if int(getattr(player, "id", -1)) not in self.carriers:
            return False
        await self._drop_carried_diamond(player, position=position, velocity=velocity)
        return True

    def reveal_to(self, connection) -> None:
        super().reveal_to(connection)
        for dropoff in self.active_dropoffs:
            self._send_dropoff(dropoff, connection=connection)
        from server.entities.registry import send_create_entity_to

        for diamond in self.ground_diamonds.values():
            entity = self.server.entity_registry.get(diamond.entity_id)
            if entity is not None:
                send_create_entity_to(connection, entity)
        self.send_start_cue_to(connection)

    def start_cue_for(self, player):
        return "DIAMOND_START"

    def _build_dropoffs(self) -> list[DiamondDropoff]:
        wm = getattr(self.server, "world_manager", None)
        metadata = getattr(wm, "map_metadata", None)
        authored = list(getattr(metadata, "diamond_base_zones", ()) or ())
        capacities = list(
            getattr(metadata, "diamond_base_capacities", ()) or ()
        )
        shift = int(getattr(getattr(wm, "map", None), "source_z_shift", 0))
        result = []
        for index, zone in enumerate(authored[:10]):
            capacity = capacities[index] if index < len(capacities) else 1
            team = int(getattr(zone, "team", TEAM_NEUTRAL))
            if team not in (*_PLAYABLE_TEAMS, TEAM_NEUTRAL):
                team = TEAM_NEUTRAL
            result.append(DiamondDropoff(
                zone=from_map_zone(index, zone, z_shift=shift),
                team=team,
                capacity=max(1, int(capacity)),
                remaining=max(1, int(capacity)),
            ))
        if result:
            return result

        first, second = self._team_anchors()
        wm = getattr(self.server, "world_manager", None)
        dry = getattr(wm, "dry_ground_anchor", None)
        result = []
        for index, fraction in enumerate((0.25, 0.5, 0.75)):
            x = first[0] + (second[0] - first[0]) * fraction
            y = first[1] + (second[1] - first[1]) * fraction
            center = dry(x, y, 48) if callable(dry) else (x, y, 58.0)
            result.append(DiamondDropoff(
                zone=around(index, center, radius_xy=_FALLBACK_RADIUS),
            ))
        logger.warning(
            "Map %s has no Diamond Mine sidecar; using dry corridor drop-offs",
            getattr(wm, "map_name", "<unknown>"),
        )
        return result

    def _team_anchors(self):
        wm = getattr(self.server, "world_manager", None)
        reader = getattr(wm, "team_base_anchor", None)
        if callable(reader):
            return (
                tuple(float(value) for value in reader(TEAM1)),
                tuple(float(value) for value in reader(TEAM2)),
            )
        return (64.0, 256.0, 58.0), (448.0, 256.0, 58.0)

    def _activate_next_dropoffs(self) -> None:
        if not self.dropoffs:
            return
        for old in tuple(self.active_dropoffs):
            old.active = False
            self._clear_dropoff(old)
        count = min(self.max_active_bases, len(self.dropoffs))
        selected = [
            self.dropoffs[(self._rotation_cursor + offset) % len(self.dropoffs)]
            for offset in range(count)
        ]
        self._rotation_cursor = (self._rotation_cursor + count) % len(self.dropoffs)
        for dropoff in selected:
            dropoff.active = True
            dropoff.remaining = dropoff.capacity
            self._send_dropoff(dropoff)
        self.active_dropoffs = selected

    def _send_dropoff(self, dropoff: DiamondDropoff, connection=None) -> None:
        color = (
            self.server.teams[dropoff.team].color
            if dropoff.team in _PLAYABLE_TEAMS
            else _NEUTRAL_COLOR
        )
        packet = minimap_zone_packet(
            dropoff.zone,
            color=color,
            icon_id=int(CG.ZONE_ICON_DIAMONDMINE),
            visible_team=TEAM_NEUTRAL,
        )
        self._send_packet(packet, connection)

    def _clear_dropoff(self, dropoff: DiamondDropoff) -> None:
        self.server.broadcast(
            bytes(minimap_zone_clear_packet(dropoff.zone).generate()), reliable=True
        )

    def _spawn_diamond(
        self,
        position,
        *,
        now: float,
        uncovered_by=None,
        pickup_delay: float = 0.0,
    ) -> GroundDiamond:
        entity = self.server.entity_registry.place(
            int(C.DIAMOND_PICKUP),
            *position,
            state=TEAM_NEUTRAL,
            kind="diamond",
            radius=0.5,
            # Retail sends the diamond lifetime (RULE_DIAMOND_LIFETIME) as
            # the packet-21 fuse; the client's 3D label counts it down.
            fuse=float(self.diamond_lifetime),
        )
        self.server.broadcast_create_entity(entity)
        diamond = GroundDiamond(
            entity_id=int(entity.entity_id),
            serial=self._serial,
            position=tuple(float(value) for value in position),
            spawned_at=float(now),
            expires_at=float(now) + self.diamond_lifetime,
            pickup_after=float(now) + max(0.0, float(pickup_delay)),
        )
        self._serial += 1
        self.ground_diamonds[diamond.entity_id] = diamond
        logger.info(
            "Diamond on the ground at (%.1f, %.1f, %.1f), entity %d, %s",
            *diamond.position, diamond.entity_id,
            "dropped" if uncovered_by is None
            else f"uncovered by {getattr(uncovered_by, 'name', '?')}",
        )
        if uncovered_by is not None:
            self._diamond_history[diamond.serial] = (uncovered_by, set())
            self._award_player(
                uncovered_by,
                int(CG.DIA_INDIVIDUAL_SCORE_FOR_MINED_DIAMOND),
                int(C.SCORE_REASON.DIA_UNCOVER_SCORE_REASON),
            )
            self.announce_localised(
                "DIAMOND_UNCOVERED", (str(getattr(uncovered_by, "name", "")),)
            )
            from server.audio import SND_DIAMOND_APPEAR, play_sound

            play_sound(self.server, SND_DIAMOND_APPEAR)
        return diamond

    def _pickup_diamond(self, player, diamond: GroundDiamond) -> None:
        from server.pickups import broadcast_pickup

        if not broadcast_pickup(
            self.server,
            player,
            int(C.DIAMOND_PICKUP),
            burdensome=True,
            state=diamond.serial,
        ):
            return
        self.carriers[int(player.id)] = diamond.serial
        history = self._diamond_history.setdefault(diamond.serial, (None, set()))
        history[1].add(int(player.team))
        objective_guard.end_spawn_protection_for_objective(self.server, player)
        self._remove_ground_diamond(diamond.entity_id)
        name = str(getattr(player, "name", ""))
        self.announce_localised_to_team(
            int(player.team), "DIAMOND_PICKEDUP_YOURTEAM", (name,)
        )
        self.announce_localised_to_team(
            TEAM2 if int(player.team) == TEAM1 else TEAM1,
            "DIAMOND_PICKEDUP_OPPOSITION", (name,)
        )

    async def _cash_in(self, player, dropoff: DiamondDropoff) -> None:
        from server.pickups import broadcast_drop

        if broadcast_drop(
            self.server,
            player,
            (player.x, player.y, player.z),
            (0.0, 0.0, 0.0),
        ) is None:
            return
        serial = self.carriers.pop(int(player.id), None)
        self._escorts.forget_carrier(player)
        self._record_cash_in_totals(player, serial)
        self._award_player(
            player,
            int(CG.DIA_INDIVIDUAL_SCORE_FOR_CASHED_IN_DIAMOND),
            int(C.SCORE_REASON.DIA_CAPTURE_SCORE_REASON),
        )
        team = self.server.teams[int(player.team)]
        team.add_score(1)
        try:
            self.server.broadcast_set_score(
                team, reason=int(C.SCORE_REASON.DIA_CAPTURE_SCORE_REASON)
            )
        except TypeError:
            self.server.broadcast_set_score(team)
        dropoff.remaining -= 1
        name = str(getattr(player, "name", ""))
        # The carrier reads "You cashed in a diamond for your team!"; the
        # rest of the team sees the named variant.
        self.announce_localised_to_player(player, "DIAMOND_CASHED_IN_YOURSELF")
        self.announce_localised_to_team(
            int(player.team), "DIAMOND_CASHED_IN_YOURTEAM", (name,), exclude=player
        )
        self.announce_localised_to_team(
            TEAM2 if int(player.team) == TEAM1 else TEAM1,
            "DIAMOND_CASHED_IN_OPPOSITION", (name,)
        )
        from server.audio import SND_DIAMOND_DROPINBASE, play_team_relative

        play_team_relative(
            self.server, int(player.team), good=SND_DIAMOND_DROPINBASE
        )
        if team.score >= self.score_limit:
            await self._end_by_score(int(player.team))
            return
        self._open_map_vote_if_due(int(team.score))
        if dropoff.remaining <= 0:
            self._rotate_dropoff(dropoff)

    def map_vote_trigger_score(self) -> int:
        """Team diamonds that open the next-map ballot.

        Retail DIA_DIAMONDS_TO_TRIGGER_MAP_VOTE is defined as
        DIA_DIAMONDS_TO_GET_FOR_MAP_ROTATION - 3 (12 of 15); keep that lead
        when an operator changes the target.
        """
        lead = int(CG.DIA_DIAMONDS_TO_GET_FOR_MAP_ROTATION) - int(
            CG.DIA_DIAMONDS_TO_TRIGGER_MAP_VOTE
        )
        return max(1, int(self.score_limit) - lead)

    def _open_map_vote_if_due(self, score: int) -> None:
        if score < self.map_vote_trigger_score() or score >= self.score_limit:
            return
        vote_manager = getattr(self.server, "vote_manager", None)
        ensure_vote = getattr(vote_manager, "ensure_map_vote", None)
        if not callable(ensure_vote):
            return
        try:
            ensure_vote(time.time())
        except Exception:  # noqa: BLE001 - the ballot must not stop scoring
            logger.debug("diamond map vote failed to open", exc_info=True)

    def _record_cash_in_totals(self, player, serial) -> None:
        """DIA_STEAL_TOTAL: cash in a diamond the enemy team carried.
        DIA_FINDANDCASHIN_TOTAL: cash in a diamond you uncovered yourself.
        Both roll into the COM_DIA_STEAL commendation."""
        from server.combat_scores import record_profile_total

        uncovered_by, teams = self._diamond_history.pop(serial, (None, set()))
        if any(team != int(player.team) for team in teams):
            record_profile_total(player, C.DIA_STEAL_TOTAL)
        if uncovered_by is player:
            record_profile_total(player, C.DIA_FINDANDCASHIN_TOTAL)

    def _loose_dropoff(self, diamond: GroundDiamond) -> DiamondDropoff | None:
        """The open drop-off a resting diamond lies in, for its last team."""
        x, y, z = diamond.position
        # Entities rest on the surface; a standing player is 2.25 above it.
        points = ((x, y, z), (x, y, z - 2.25))
        return next((
            dropoff
            for dropoff in self.active_dropoffs
            if dropoff.remaining > 0
            and dropoff.team in (TEAM_NEUTRAL, diamond.last_team)
            and any(dropoff.zone.contains(point) for point in points)
        ), None)

    async def _cash_in_loose(self, diamond: GroundDiamond) -> bool:
        """Cash in a diamond nobody carries: it was dropped or thrown in.

        Retail has DIAMOND_CASHED_IN_LOOSE_YOURTEAM/_OPPOSITION ("Diamond
        cashed in for your team!" / "for the enemy!"), sent by the server and
        without a player name, and DiamondTool's primary fire throws the
        diamond (DIAMOND_THROW_SPEED). The team point is the same; nobody
        gets the carrier's individual score.
        """
        if (
            not self.loose_cash_in
            or self.ended
            or diamond.last_team not in _PLAYABLE_TEAMS
            or diamond.entity_id not in self.ground_diamonds
        ):
            return False
        dropoff = self._loose_dropoff(diamond)
        if dropoff is None:
            return False
        team_id = int(diamond.last_team)
        self._remove_ground_diamond(diamond.entity_id)
        self._diamond_history.pop(diamond.serial, None)
        team = self.server.teams[team_id]
        team.add_score(1)
        try:
            self.server.broadcast_set_score(
                team, reason=int(C.SCORE_REASON.DIA_CAPTURE_SCORE_REASON)
            )
        except TypeError:
            self.server.broadcast_set_score(team)
        dropoff.remaining -= 1
        self.announce_localised_to_team(team_id, "DIAMOND_CASHED_IN_LOOSE_YOURTEAM")
        self.announce_localised_to_team(
            TEAM2 if team_id == TEAM1 else TEAM1,
            "DIAMOND_CASHED_IN_LOOSE_OPPOSITION",
        )
        from server.audio import SND_DIAMOND_DROPINBASE, play_team_relative

        play_team_relative(self.server, team_id, good=SND_DIAMOND_DROPINBASE)
        logger.info("Loose diamond cashed in for team %d", team_id)
        if team.score >= self.score_limit:
            await self._end_by_score(team_id)
            return True
        self._open_map_vote_if_due(int(team.score))
        if dropoff.remaining <= 0:
            self._rotate_dropoff(dropoff)
        return True

    async def _drop_carried_diamond(
        self, player, position=None, velocity=None, team=None
    ) -> None:
        serial = self.carriers.get(int(getattr(player, "id", -1)))
        if serial is None:
            return
        from server.pickups import broadcast_drop

        position = position or (player.x, player.y, player.z)
        velocity = velocity or (
            float(getattr(player, "vx", 0.0)),
            float(getattr(player, "vy", 0.0)),
            float(getattr(player, "vz", 0.0)),
        )
        dropped = broadcast_drop(self.server, player, position, velocity)
        if dropped is None:
            return
        self.carriers.pop(int(player.id), None)
        self._escorts.forget_carrier(player)
        if self.ended:
            # Clear the carried tool only: no new pickup entity is created
            # into the end screen (the restart would have to destroy it).
            self._diamond_history.pop(serial, None)
            return
        settled = self._surface_anchor(dropped[2][0], dropped[2][1])
        # The client sounds the pickup itself but not the drop.
        from server.audio import SND_DIAMOND_DROP, play_sound

        play_sound(self.server, SND_DIAMOND_DROP, position=settled)
        diamond = self._spawn_diamond(
            settled,
            now=time.time(),
            pickup_delay=float(C.NO_PICKUP_AFTER_DROP_TIME),
        )
        diamond.serial = serial
        diamond.last_team = int(player.team if team is None else team)
        await self._cash_in_loose(diamond)

    def _rotate_dropoff(self, exhausted: DiamondDropoff) -> None:
        """Replace one depleted drop-off without disrupting other live bases."""

        exhausted.active = False
        self._clear_dropoff(exhausted)
        self.active_dropoffs = [
            dropoff for dropoff in self.active_dropoffs if dropoff is not exhausted
        ]
        inactive = [dropoff for dropoff in self.dropoffs if not dropoff.active]
        replacement = next(
            (dropoff for dropoff in inactive if dropoff is not exhausted),
            exhausted,
        )
        replacement.active = True
        replacement.remaining = replacement.capacity
        self.active_dropoffs.append(replacement)
        self._send_dropoff(replacement)
        # Retail DIAMOND_BASE "Bring diamonds here!" (server-sent; no client
        # binary references it). Sent when a new drop-off opens mid-round --
        # inferred timing; the round-start drop-off is covered by
        # DIAMOND_START.
        self.announce_localised("DIAMOND_BASE")

    def _surface_anchor(self, x: float, y: float) -> tuple[float, float, float]:
        wm = getattr(self.server, "world_manager", None)
        reader = getattr(wm, "dry_surface_anchor", None)
        if callable(reader):
            return tuple(float(value) for value in reader(x, y))
        return float(x), float(y), 60.0

    def _remove_ground_diamond(self, entity_id: int) -> None:
        diamond = self.ground_diamonds.pop(int(entity_id), None)
        if diamond is None:
            return
        self.server.broadcast_destroy_entity(diamond.entity_id)
        self.server.entity_registry.remove(diamond.entity_id)

    def _clear_runtime_entities(self) -> None:
        from server.pickups import broadcast_drop

        for player_id in tuple(self.carriers):
            player = getattr(self.server, "players", {}).get(player_id)
            if (
                player is not None
                and getattr(player, "pickup_id", None) == int(C.DIAMOND_PICKUP)
            ):
                broadcast_drop(
                    self.server,
                    player,
                    (player.x, player.y, player.z),
                    (0.0, 0.0, 0.0),
                )
        registry = self.server.entity_registry
        for entity_id in tuple(self.ground_diamonds):
            # After reset_round_runtime the registry was wiped (clients got
            # their DestroyEntity then) and ids restart at 0: only destroy an
            # id that still names this mode's own diamond entity.
            entity = registry.get(entity_id)
            if entity is not None and getattr(entity, "kind", None) == "diamond":
                self._remove_ground_diamond(entity_id)
        self.ground_diamonds.clear()
        self.carriers.clear()
        self._diamond_history.clear()

    def _nearest_ground_diamond(self, player) -> GroundDiamond | None:
        radius_sq = float(C.PICKUP_DISTANCE) ** 2
        candidates = [
            diamond
            for diamond in self.ground_diamonds.values()
            if sum((float(value) - float(origin)) ** 2 for value, origin in zip(
                diamond.position, (player.x, player.y, player.z)
            )) <= radius_sq
            # No grabbing through a wall/floor: mined diamonds sit at the
            # mined cell's centre, dropped ones on a surface; aim just below
            # either so the target stays inside the diamond's own air cell.
            and objective_guard.pickup_line_of_sight(
                self.server, player, diamond.position, player_space=False, lift=0.25
            )
        ]
        return min(candidates, key=lambda item: item.entity_id) if candidates else None

    def escape_watch_objective_player(self, player) -> bool:
        return int(getattr(player, "id", -1)) in self.carriers

    def _cashable_dropoff(self, player) -> DiamondDropoff | None:
        return next((
            dropoff
            for dropoff in self.active_dropoffs
            if dropoff.remaining > 0
            and dropoff.team in (TEAM_NEUTRAL, int(player.team))
            and dropoff.zone.contains((player.x, player.y, player.z))
        ), None)

    def _award_carry_and_escort(self, periods: int) -> None:
        players = getattr(self.server, "players", {})
        for player_id in tuple(self.carriers):
            carrier = players.get(player_id)
            if not self._active_player(carrier):
                continue
            self._award_player(
                carrier,
                periods * int(CG.DIA_SCORE_CARRY_SCORE),
                int(C.SCORE_REASON.DIA_CARRY_SCORE_REASON),
            )
            # DIA_ESCORT_RADIUS (15) to join, + DIA_ESCORT_HYSTERESIS (10)
            # before an existing escort drops out.
            escorts = self._escorts.escorts(carrier, [
                escort for escort in players.values() if self._active_player(escort)
            ])
            for escort in escorts:
                self._award_player(
                    escort,
                    periods * int(CG.DIA_SCORE_ESCORT_SCORE),
                    int(C.SCORE_REASON.DIA_ESCORT_SCORE_REASON),
                )

    def _active_diamond_count(self) -> int:
        return len(self.ground_diamonds) + len(self.carriers)

    @staticmethod
    def _active_player(player) -> bool:
        return bool(
            player is not None
            and getattr(player, "alive", False)
            and getattr(player, "spawned", True)
            and int(getattr(player, "team", -1)) in _PLAYABLE_TEAMS
        )

    def _award_player(self, player, points: int, reason: int) -> None:
        if points <= 0 or not self._owns_slot(player) or self.retiring:
            return
        from server.scoreboard import send_player_score

        player.score = int(getattr(player, "score", 0)) + int(points)
        send_player_score(self.server, player, reason=int(reason))

    def _send_packet(self, packet, connection=None) -> None:
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            connection.send(data, reliable=True)


__all__ = ["DiamondDropoff", "DiamondMineMode", "GroundDiamond"]
