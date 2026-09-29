"""Retail-style asymmetric Occupation bomb mode."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL

from . import objective_guard
from .base_mode import BaseMode
from .objective_zones import ObjectiveZone, around, from_map_zone, minimap_zone_packet


logger = logging.getLogger(__name__)

_PLAYABLE_TEAMS = (TEAM1, TEAM2)
_TARGET_RADIUS = 16.0
# Our spacing for the BASE_OCCUPIED_* shout (reuses the retail TC "new team
# enters" shout cooldown, TC_NEW_TEAM_ENTERS_SHOUT_COOLDOWN = 5 s).
BASE_OCCUPIED_SHOUT_COOLDOWN = float(
    getattr(CG, "TC_NEW_TEAM_ENTERS_SHOUT_COOLDOWN", 5.0)
)
# Bots cannot send DropPickup.  A defending (Green) bot carrier releases the
# bomb once it is this far outside the target footprint: the recovered
# defender threat radius, never less than the bomb's own blast radius, so the
# fuse it lights cannot damage the base it is protecting.  This keeps bombs
# cycling back into play instead of being hoarded forever by a bot.
_BOT_DEFENDER_DISPOSAL_DISTANCE = max(
    float(CG.OC_THREAT_RADIUS), float(C.BOMB_EXPLOSION_RADIUS)
)


def _configured_rule(server, key: str, rule: str, fallback, *, allow_false=False):
    resolver = getattr(getattr(server, "config", None), "mode_rule", None)
    if callable(resolver):
        try:
            value = resolver("oc", key, rule)
            if value is not None and (allow_false or value is not False):
                return value
        except (AttributeError, KeyError, TypeError, ValueError):
            pass
    overlay = getattr(getattr(server, "config", None), "mode_settings", {}).get(
        "oc", {}
    )
    return overlay.get(key, fallback)


@dataclass(slots=True)
class OccupationBomb:
    """One bomb across ground, carried, armed, and exploding states."""

    serial: int
    entity_id: int | None
    position: tuple[float, float, float]
    carrier_id: int | None = None
    armed: bool = False
    explode_at: float = 0.0
    pickup_after: float = 0.0
    last_carrier_id: int | None = None
    last_carrier_team: int = TEAM_NEUTRAL
    intercepted: bool = False
    # Identity of the last carrier: a departed player's reused slot id must
    # never collect the disposal/boom credit.
    last_carrier: object | None = None


class OccupationMode(BaseMode):
    """Blue retrieves bombs and detonates them in Green's defended base.

    A bomb is carried as the native non-swappable BombTool.  Dropping it
    lights the configured fuse; defenders can kill the carrier, pick up the
    live bomb, and carry it out so its blast counts as a disposal instead of a
    successful occupation strike.
    """

    name = "Occupation"
    description = "Green controls the base. Blue must bomb the base!"
    mode_code = "oc"

    def __init__(self, server) -> None:
        super().__init__(server)
        data = mode_data.get(self.mode_code)
        resolve_time = getattr(getattr(server, "config", None), "configured_time_limit", None)
        self.time_limit = (
            float(resolve_time(self.mode_code, data.default_time_limit))
            if callable(resolve_time)
            else float(data.default_time_limit)
        )
        score = _configured_rule(
            server,
            "score_limit",
            "RULE_OCC_SCORE_TARGET",
            30,
            allow_false=True,
        )
        self.score_limit = 0 if score is False else max(0, int(score))
        self.max_active_bombs = max(1, min(3, int(_configured_rule(
            server, "max_active_bombs", "RULE_MAX_ACTIVE_BOMBS", 1
        ))))
        self.bomb_fuse_time = max(1.0, float(_configured_rule(
            server, "bomb_fuse_time", "RULE_BOMB_FUSE_TIME",
            C.BOMB_EXPLOSION_FUSE,
        )))
        self.target_zone: ObjectiveZone | None = None
        self.bomb_spawn_points: list[tuple[float, float, float]] = []
        self.bombs: dict[int, OccupationBomb] = {}
        self.entity_to_bomb: dict[int, int] = {}
        self.carriers: dict[int, int] = {}
        self._pending_spawns: list[float] = []
        self._serial = 1
        self._spawn_cursor = 0
        self._next_personal_score_at = 0.0
        # Attacking carriers inside the target on the previous tick, and the
        # next moment BASE_OCCUPIED_ATTACK/DEFEND may be shouted again.
        self._carriers_in_base: set[int] = set()
        self._base_occupied_shout_at = 0.0

    async def on_mode_start(self) -> None:
        # Clear our bookkeeping BEFORE the base class rebuilds map resources.
        # On a round restart reset_round_runtime has already destroyed every
        # client entity and reset the registry allocator; the rebuild then
        # reuses ids from 0, so clearing afterwards would DestroyEntity fresh
        # crates that happen to share an old bomb id.
        self._clear_bombs()
        await super().on_mode_start()
        for team in self.server.teams.values():
            team.reset()
        self.target_zone = self._build_target_zone()
        self.bomb_spawn_points = self._build_bomb_spawn_points()
        self._pending_spawns.clear()
        self._spawn_cursor = 0
        self._carriers_in_base = set()
        self._base_occupied_shout_at = 0.0
        self._send_target_zone()
        now = time.time()
        spawned = 0
        for _ in range(self.max_active_bombs):
            spawned += self._spawn_bomb(now) is not None
        # Start cue first; the bomb-wave line queues behind it.
        self.broadcast_start_cue()
        if spawned:
            self._announce_bomb_wave(override_previous=False)
        self._next_personal_score_at = now + float(CG.OC_SCORE_CARRY_INTERVAL)
        logger.info(
            "Occupation started with %d bombs, %.1fs fuse, score target %s",
            len(self.bombs),
            self.bomb_fuse_time,
            self.score_limit or "disabled",
        )

    async def deactivate(self) -> None:
        self._clear_bombs()
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        await super().on_tick(tick)
        if self.ended:
            return
        now = time.time()
        due = [when for when in self._pending_spawns if when <= now]
        self._pending_spawns = [when for when in self._pending_spawns if when > now]
        spawned = 0
        for _ in due:
            if len(self.bombs) < self.max_active_bombs:
                spawned += self._spawn_bomb(now) is not None
        if spawned:
            # One cue per spawn wave, never one per bomb in the wave.
            self._announce_bomb_wave()

        self._guard_ground_bombs(now)

        for player in tuple(getattr(self.server, "players", {}).values()):
            if not self._active_player(player) or int(player.id) in self.carriers:
                continue
            bomb = self._nearest_ground_bomb(player, now)
            if bomb is not None:
                self._pickup_bomb(player, bomb)

        self._announce_base_occupied(now)

        # Bots cannot synthesize a client DropPickup packet.  Once an attacker
        # reaches the exact native target volume, perform the same validated
        # drop that a human triggers with BombTool primary fire.  A defending
        # bot instead disposes of the bomb once it has carried it clear of the
        # base, exactly as a human defender would.
        for player_id, serial in tuple(self.carriers.items()):
            player = getattr(self.server, "players", {}).get(player_id)
            bomb = self.bombs.get(serial)
            if (
                bomb is None
                or player is None
                or not bool(getattr(player, "is_bot", False))
            ):
                continue
            team = int(getattr(player, "team", -1))
            if team == TEAM1 and not bomb.armed and self._inside_target(player):
                await self._drop_bomb(player)
            elif (
                team == TEAM2
                and self._target_distance(player) >= _BOT_DEFENDER_DISPOSAL_DISTANCE
            ):
                await self._drop_bomb(player)

        for bomb in tuple(self.bombs.values()):
            if bomb.armed and now >= bomb.explode_at:
                await self._detonate_bomb(bomb)
                if self.ended:
                    return

        interval = float(CG.OC_SCORE_CARRY_INTERVAL)
        if now >= self._next_personal_score_at:
            periods = max(1, int((now - self._next_personal_score_at) / interval) + 1)
            self._next_personal_score_at += periods * interval
            self._award_periodic_scores(periods)

    async def on_player_death(self, player, killer, kill_type: int) -> None:
        await super().on_player_death(player, killer, kill_type)
        self._award_kill_events(player, killer, kill_type)
        serial = self.carriers.get(int(getattr(player, "id", -1)))
        if serial is None:
            return
        bomb = self.bombs.get(serial)
        # A team switch assigns the new team before die(); judge the kill by
        # the team the carrier actually held the bomb for.
        carrier_team = (
            int(bomb.last_carrier_team)
            if bomb is not None
            else int(getattr(player, "team", -1))
        )
        if (
            not self.ended
            and killer is not None
            and killer is not player
            and int(getattr(killer, "team", -1)) in _PLAYABLE_TEAMS
            and int(getattr(killer, "team", -1)) != carrier_team
        ):
            team = self.server.teams[int(killer.team)]
            team.add_score(int(CG.OC_TEAM_SCORE_FOR_KILLING_CARRIER))
            self._award_player(
                killer,
                int(CG.OC_SCORE_INTERCEPT),
                int(C.SCORE_REASON.OCC_INTERCEPT_SCORE_REASON),
            )
            self._broadcast_team_score(team, C.SCORE_REASON.OCC_INTERCEPT_SCORE_REASON)
            if self.score_limit > 0 and team.score >= self.score_limit:
                await self._end_by_score(int(killer.team))
        await self._drop_bomb(player)

    # Retail objective kill events (see server.combat_scores). Intercept
    # (killing a carrier) keeps its own path above: it also moves the team
    # score (OC_TEAM_SCORE_FOR_KILLING_CARRIER).
    _KILL_EVENT_AMOUNTS = {
        "carrier_defend": int(CG.OC_SCORE_CARRIER_DEFEND),
        "defend": int(CG.OC_SCORE_DEFEND),
        "assault": int(CG.OC_SCORE_ASSAULT),
        "distract": int(CG.OC_SCORE_DISTRACT),
    }
    _KILL_EVENT_REASONS = {
        "carrier_defend": int(C.SCORE_REASON.OCC_CARRIER_DEFEND_SCORE_REASON),
        "defend": int(C.SCORE_REASON.OCC_DEFEND_SCORE_REASON),
        "assault": int(C.SCORE_REASON.OCC_ASSAULT_SCORE_REASON),
        "distract": int(C.SCORE_REASON.OCC_DISTRACT_SCORE_REASON),
    }

    def _team_carriers(self, team: int) -> list:
        players = getattr(self.server, "players", {})
        result = []
        for player_id, serial in self.carriers.items():
            carrier = players.get(player_id)
            bomb = self.bombs.get(serial)
            if (
                carrier is not None
                and bomb is not None
                and int(bomb.last_carrier_team) == int(team)
                and getattr(carrier, "team", None) == team
            ):
                result.append(carrier)
        return result

    def _award_kill_events(self, victim, killer, kill_type: int) -> None:
        """Objective kill events, evaluated BEFORE the victim drops a bomb.

        Carrier Defend: kill an enemy within OC_CARRIER_THREAT_RADIUS (10) of
        your bomb carrier. Bomb Defend: kill an enemy within OC_THREAT_RADIUS
        (20) of a lit bomb your team dropped. Close to Bomb: kill while you
        are within OC_THREAT_RADIUS of any other ground bomb. Bomb
        Distraction: the victim died to an enemy within OC_THREAT_RADIUS of
        its own living carrier. The carrier kill itself is OCC_INTERCEPT.
        """
        from server import combat_scores as cs

        if self.ended or not cs.eligible_kill(killer, victim, kill_type):
            return
        killer_team, victim_team = int(killer.team), int(victim.team)
        victim_serial = self.carriers.get(int(getattr(victim, "id", -1)))
        victim_bomb = self.bombs.get(victim_serial) if victim_serial is not None else None
        victim_carrying = (
            victim_bomb is not None and int(victim_bomb.last_carrier_team) != killer_team
        )
        ground = [
            bomb for bomb in self.bombs.values()
            if bomb.carrier_id is None and bomb.entity_id is not None
        ]
        own_lit = [
            bomb for bomb in ground
            if bomb.armed and int(bomb.last_carrier_team) == killer_team
        ]
        event = None
        if not victim_carrying:
            event = cs.classify_objective_kill(
                killer, victim,
                killer_team_carriers=self._team_carriers(killer_team),
                defend_points=[bomb.position for bomb in own_lit],
                attack_points=[
                    bomb.position for bomb in ground
                    if all(bomb is not lit for lit in own_lit)
                ],
                carrier_threat_radius=float(CG.OC_CARRIER_THREAT_RADIUS),
                threat_radius=float(CG.OC_THREAT_RADIUS),
            )
        cs.award_kill_event(
            self.server, killer, event,
            self._KILL_EVENT_AMOUNTS, self._KILL_EVENT_REASONS, mode=self,
        )
        if cs.is_distraction(
            victim, killer, kill_type, self._team_carriers(victim_team),
            float(CG.OC_THREAT_RADIUS),
        ):
            cs.award_kill_event(
                self.server, victim, "distract",
                self._KILL_EVENT_AMOUNTS, self._KILL_EVENT_REASONS, mode=self,
            )

    def _blast_witnesses(self, position) -> list:
        """Living playable players inside a detonating bomb's radius."""
        from server.combat_scores import within

        radius = float(C.BOMB_EXPLOSION_RADIUS)
        return [
            player for player in tuple(getattr(self.server, "players", {}).values())
            if self._active_player(player) and within(player, position, radius)
        ]

    def _award_blast_survivors(self, witnesses) -> None:
        """OCC_SURVIVE ("Survive Blast", OC_SCORE_SURVIVE 50): alive after a
        bomb exploded within BOMB_EXPLOSION_RADIUS of you."""
        from server.combat_scores import award_score_event

        for player in witnesses:
            if self._active_player(player):
                award_score_event(
                    self.server, player, int(CG.OC_SCORE_SURVIVE),
                    int(C.SCORE_REASON.OCC_SURVIVE_SCORE_REASON), mode=self,
                )

    async def on_player_leave(self, player) -> None:
        await self._drop_bomb(player)

    async def on_player_team_change(self, player, old_team: int, new_team: int) -> None:
        await self._drop_bomb(player)

    async def handle_drop_pickup(self, player, position, velocity) -> bool:
        if int(getattr(player, "id", -1)) not in self.carriers:
            return False
        await self._drop_bomb(player, position=position, velocity=velocity)
        return True

    def end_message_id(self, winner: int | None) -> int:
        """Retail scoreboard headline: OCCUPATION_WIN_MESSAGE or a draw."""
        return int(C.TEAM_SCORES_DRAW if winner is None else C.OCCUPATION_WIN_MESSAGE)

    def reveal_to(self, connection) -> None:
        super().reveal_to(connection)
        self._send_target_zone(connection=connection)
        from server.entities.registry import send_create_entity_to

        for bomb in self.bombs.values():
            if bomb.entity_id is None:
                continue
            entity = self.server.entity_registry.get(bomb.entity_id)
            if entity is not None:
                send_create_entity_to(connection, entity)

        self.send_start_cue_to(connection)

    def start_cue_for(self, player):
        """TEAM1 attacks, TEAM2 defends; spectators get no start line."""
        team = int(getattr(player, "team", -1))
        if team == TEAM1:
            return "OCCUPATION_START_ATTACK"
        if team == TEAM2:
            return "OCCUPATION_START_DEFEND"
        return None

    def _build_target_zone(self) -> ObjectiveZone:
        wm = getattr(self.server, "world_manager", None)
        metadata = getattr(wm, "map_metadata", None)
        authored = getattr(metadata, "occupation_base_zone", None)
        shift = int(getattr(getattr(wm, "map", None), "source_z_shift", 0))
        if authored is not None:
            return from_map_zone(0, authored, z_shift=shift)
        team_bases = getattr(metadata, "base_zones", {}).get(TEAM2, []) if metadata else []
        if team_bases:
            return from_map_zone(0, team_bases[0], z_shift=shift)
        reader = getattr(wm, "team_base_anchor", None)
        center = reader(TEAM2) if callable(reader) else (448.0, 256.0, 58.0)
        logger.warning(
            "Map %s has no Occupation base; using Green's dry base anchor",
            getattr(wm, "map_name", "<unknown>"),
        )
        return around(
            0,
            center,
            radius_xy=_TARGET_RADIUS,
            height_above=12.0,
            depth_below=16.0,
        )

    def _build_bomb_spawn_points(self) -> list[tuple[float, float, float]]:
        wm = getattr(self.server, "world_manager", None)
        metadata = getattr(wm, "map_metadata", None)
        authored = list(getattr(metadata, "occupation_bomb_points", ()) or ())
        shift = int(getattr(getattr(wm, "map", None), "source_z_shift", 0))
        if authored:
            return [
                (float(x), float(y), float(z) + shift) for x, y, z in authored[:5]
            ]
        anchors = getattr(wm, "team_base_anchor", None)
        if callable(anchors):
            blue = anchors(TEAM1)
            green = anchors(TEAM2)
        else:
            blue, green = (64.0, 256.0, 58.0), (448.0, 256.0, 58.0)
        dry = getattr(wm, "dry_surface_anchor", None)
        points = []
        for fraction in (0.35, 0.45, 0.55):
            x = blue[0] + (green[0] - blue[0]) * fraction
            y = blue[1] + (green[1] - blue[1]) * fraction
            point = dry(x, y, 48) if callable(dry) else (x, y, 60.0)
            points.append(tuple(float(value) for value in point))
        logger.warning(
            "Map %s has no Occupation bomb points; using dry corridor spawns",
            getattr(wm, "map_name", "<unknown>"),
        )
        return points

    def _spawn_bomb(self, now: float) -> OccupationBomb | None:
        if not self.bomb_spawn_points:
            return None
        position = self.bomb_spawn_points[self._spawn_cursor % len(self.bomb_spawn_points)]
        self._spawn_cursor += 1
        entity = self.server.entity_registry.place(
            int(C.BOMB_PICKUP),
            *position,
            state=TEAM_NEUTRAL,
            kind="occupation_bomb",
            radius=0.5,
            fuse=0.0,
        )
        self.server.broadcast_create_entity(entity)
        bomb = OccupationBomb(
            serial=self._serial,
            entity_id=int(entity.entity_id),
            position=position,
            pickup_after=float(now),
        )
        self._serial += 1
        self.bombs[bomb.serial] = bomb
        self.entity_to_bomb[int(entity.entity_id)] = bomb.serial
        return bomb

    def _announce_bomb_wave(self, *, override_previous: bool = True) -> None:
        self.announce_localised_to_team(
            TEAM1, "TAKE_BOMB_TO_ENEMY_BASE", override_previous=override_previous
        )
        self.announce_localised_to_team(
            TEAM2, "STOP_BOMB_REACHING_BASE", override_previous=override_previous
        )

    def _announce_base_occupied(self, now: float) -> None:
        """BASE_OCCUPIED_ATTACK/_DEFEND when an attacker carries a bomb in.

        Retail strings "Detonate the bomb inside the enemy base!" / "Stop the
        bomb detonating inside your base!" are referenced by no client binary
        (server-sent). The trigger -- an attacking carrier entering the
        target volume, at most once per BASE_OCCUPIED_SHOUT_COOLDOWN -- is
        inferred from the text; the retail server source is not available.
        """
        players = getattr(self.server, "players", {})
        inside: set[int] = set()
        for player_id in self.carriers:
            player = players.get(player_id)
            if (
                player is None
                or int(getattr(player, "team", -1)) != TEAM1
                or not self._inside_target(player)
            ):
                continue
            inside.add(int(player_id))
        entered = inside - self._carriers_in_base
        self._carriers_in_base = inside
        if not entered or now < self._base_occupied_shout_at:
            return
        self._base_occupied_shout_at = now + BASE_OCCUPIED_SHOUT_COOLDOWN
        self.announce_localised_to_team(
            TEAM1, "BASE_OCCUPIED_ATTACK", override_previous=True
        )
        self.announce_localised_to_team(
            TEAM2, "BASE_OCCUPIED_DEFEND", override_previous=True
        )

    def _pickup_bomb(self, player, bomb: OccupationBomb) -> None:
        from server.pickups import broadcast_pickup

        if not broadcast_pickup(
            self.server,
            player,
            int(C.BOMB_PICKUP),
            burdensome=True,
            state=bomb.serial,
        ):
            return
        if bomb.entity_id is not None:
            self.server.broadcast_destroy_entity(bomb.entity_id)
            self.server.entity_registry.remove(bomb.entity_id)
            self.entity_to_bomb.pop(bomb.entity_id, None)
        bomb.entity_id = None
        bomb.carrier_id = int(player.id)
        bomb.last_carrier_id = int(player.id)
        bomb.last_carrier = player
        bomb.last_carrier_team = int(player.team)
        if bomb.armed and int(player.team) == TEAM2:
            bomb.intercepted = True
        self.carriers[int(player.id)] = bomb.serial
        objective_guard.end_spawn_protection_for_objective(self.server, player)
        name = str(getattr(player, "name", ""))
        if int(player.team) == TEAM1:
            attack, defend = "PLAYER_HAS_BOMB_ATTACK", "PLAYER_HAS_BOMB_DEFEND"
        elif bomb.intercepted:
            attack, defend = "PLAYER_HAS_BOMB_KILL", "PLAYER_HAS_BOMB_HIDE"
        else:
            attack, defend = "DEFENDER_HAS_THE_BOMB_ATTACK", "DEFENDER_HAS_THE_BOMB_DEFEND"
        self.announce_localised_to_team(TEAM1, attack, (name,))
        self.announce_localised_to_team(TEAM2, defend, (name,))

    async def _drop_bomb(self, player, position=None, velocity=None) -> None:
        serial = self.carriers.get(int(getattr(player, "id", -1)))
        bomb = self.bombs.get(serial) if serial is not None else None
        if bomb is None:
            return
        from server.pickups import broadcast_drop

        if self.ended:
            # The match is over (e.g. this carrier's death reached the score
            # limit): clear the native tool, but never arm or broadcast a
            # fresh bomb entity into the end screen.
            if getattr(player, "pickup_id", None) == int(C.BOMB_PICKUP):
                broadcast_drop(
                    self.server,
                    player,
                    (player.x, player.y, player.z),
                    (0.0, 0.0, 0.0),
                )
            self.carriers.pop(int(player.id), None)
            bomb.carrier_id = None
            return

        position = position or (player.x, player.y, player.z)
        velocity = velocity or (
            float(getattr(player, "vx", 0.0)),
            float(getattr(player, "vy", 0.0)),
            float(getattr(player, "vz", 0.0)),
        )
        dropped = broadcast_drop(self.server, player, position, velocity)
        if dropped is None:
            return
        now = time.time()
        self.carriers.pop(int(player.id), None)
        bomb.carrier_id = None
        bomb.last_carrier_id = int(player.id)
        bomb.last_carrier = player
        # last_carrier_team keeps the team recorded at pickup: a team switch
        # sets player.team before the drop, which must not hand the bomb (and
        # its disposal/boom credit) to the carrier's new team.
        bomb.position = self._surface_anchor(dropped[2][0], dropped[2][1])
        # The client sounds the bomb pickup itself but not the drop.
        from server.audio import SND_BOMB_DROP, play_sound

        play_sound(self.server, SND_BOMB_DROP, position=bomb.position)
        if not bomb.armed:
            bomb.armed = True
            bomb.explode_at = now + self.bomb_fuse_time
        remaining = max(0.05, bomb.explode_at - now)
        entity = self.server.entity_registry.place(
            int(C.BOMB_PICKUP),
            *bomb.position,
            state=TEAM_NEUTRAL,
            kind="occupation_bomb_armed",
            radius=0.5,
            fuse=remaining,
        )
        self.server.broadcast_create_entity(entity)
        bomb.entity_id = int(entity.entity_id)
        bomb.pickup_after = now + float(C.NO_PICKUP_AFTER_DROP_TIME)
        self.entity_to_bomb[bomb.entity_id] = bomb.serial

    async def _detonate_bomb(self, bomb: OccupationBomb) -> None:
        thrower = None
        if bomb.carrier_id is not None:
            thrower = getattr(self.server, "players", {}).get(bomb.carrier_id)
            if thrower is not None:
                from server.pickups import broadcast_drop

                broadcast_drop(
                    self.server,
                    thrower,
                    (thrower.x, thrower.y, thrower.z),
                    (0.0, 0.0, 0.0),
                )
                bomb.position = self._surface_anchor(thrower.x, thrower.y)
            self.carriers.pop(bomb.carrier_id, None)
            bomb.carrier_id = None

        if bomb.entity_id is None:
            entity = self.server.entity_registry.place(
                int(C.BOMB_PICKUP),
                *bomb.position,
                state=TEAM_NEUTRAL,
                kind="occupation_bomb_exploding",
                radius=0.5,
                fuse=0.05,
            )
            self.server.broadcast_create_entity(entity)
            bomb.entity_id = int(entity.entity_id)
            self.entity_to_bomb[bomb.entity_id] = bomb.serial

        inside = self._bomb_inside_target(bomb.position)
        scorer = getattr(self.server, "players", {}).get(bomb.last_carrier_id)
        if (
            scorer is not None
            and bomb.last_carrier is not None
            and scorer is not bomb.last_carrier
        ):
            # The slot was reused by a new joiner after the carrier left.
            scorer = None
        if scorer is not None and int(getattr(scorer, "team", -1)) != int(
            bomb.last_carrier_team
        ):
            # The last carrier has since switched teams: no personal credit.
            scorer = None
        if inside:
            team = self.server.teams[TEAM1]
            team.add_score(int(CG.OC_TEAM_SCORE_FOR_BOMB_EXPLOSION_IN_BASE))
            if scorer is not None and bomb.last_carrier_team == TEAM1:
                self._award_player(
                    scorer,
                    int(CG.OC_SCORE_FOR_BOMB_EXPLOSION_IN_BASE),
                    int(C.SCORE_REASON.OCC_BOOM_SCORE_REASON),
                )
            self._broadcast_team_score(team, C.SCORE_REASON.OCC_BOOM_SCORE_REASON)
            await self.broadcast_localised_message(
                "BOMB_SUCCESSFUL", (self.server.teams[TEAM2].name,), localise_parameters=True
            )
        else:
            if scorer is not None and bomb.last_carrier_team == TEAM2:
                self._award_player(
                    scorer,
                    int(
                        CG.OC_SCORE_FOR_DISPOSAL_INTERCEPT
                        if bomb.intercepted
                        else CG.OC_SCORE_FOR_DISPOSAL
                    ),
                    int(
                        C.SCORE_REASON.OCC_INTERCEPT_DISPOSAL_SCORE_REASON
                        if bomb.intercepted
                        else C.SCORE_REASON.OCC_DISPOSAL_SCORE_REASON
                    ),
                )
            await self.broadcast_localised_message(
                "BOMB_FAIL", (self.server.teams[TEAM2].name,), localise_parameters=True
            )

        entity_id = int(bomb.entity_id)
        witnesses = self._blast_witnesses(bomb.position)
        if thrower is not None and not getattr(self.server.config, "friendly_fire", False):
            # The blast cannot hurt the holder's own team: nothing survived.
            witnesses = [p for p in witnesses if p.team != thrower.team]
        self.server._apply_blast(
            bomb.position[0],
            bomb.position[1],
            bomb.position[2],
            float(C.BOMB_EXPLOSION_DAMAGE),
            float(C.BOMB_EXPLOSION_BLOCK_DAMAGE),
            int(C.KILL.BOMB_KILL),
            thrower,
            crater_radius=1,
            force_destroy=True,
            blast_radius=float(C.BOMB_EXPLOSION_RADIUS),
            knockback_min=float(C.BOMB_EXPLOSION_KNOCKBACK_MIN),
            knockback_max=float(C.BOMB_EXPLOSION_KNOCKBACK_MAX),
            native_damage_type=int(C.BOMB_DAMAGE),
            causer_entity_id=entity_id,
        )
        self._award_blast_survivors(witnesses)
        # No client code plays BOMB_EXPLODE_SOUND; the detonation cue is ours.
        from server.audio import (
            SND_BOMB_EXPLODE,
            SND_BOMB_EXPLODE_WATER,
            explosion_sound,
            play_sound,
        )

        play_sound(
            self.server,
            explosion_sound(bomb.position, SND_BOMB_EXPLODE, SND_BOMB_EXPLODE_WATER),
            position=bomb.position,
        )
        self.server.broadcast_destroy_entity(entity_id)
        self.server.entity_registry.remove(entity_id)
        self.entity_to_bomb.pop(entity_id, None)
        self.bombs.pop(bomb.serial, None)
        self._pending_spawns.append(
            time.time() + float(CG.OC_BOMB_RESPAWN_TIME_ON_EXPLOSION)
        )
        if inside and self.score_limit > 0:
            if self.server.teams[TEAM1].score >= self.score_limit:
                await self._end_by_score(TEAM1)

    def _nearest_ground_bomb(self, player, now: float) -> OccupationBomb | None:
        radius_sq = float(C.PICKUP_DISTANCE) ** 2
        candidates = []
        for bomb in self.bombs.values():
            if bomb.entity_id is None or bomb.carrier_id is not None or now < bomb.pickup_after:
                continue
            distance_sq = sum(
                (float(value) - float(origin)) ** 2
                for value, origin in zip(bomb.position, (player.x, player.y, player.z))
            )
            if distance_sq <= radius_sq and objective_guard.pickup_line_of_sight(
                self.server, player, bomb.position, player_space=False
            ):
                candidates.append(bomb)
        return min(candidates, key=lambda item: item.serial) if candidates else None

    def escape_watch_objective_player(self, player) -> bool:
        return int(getattr(player, "id", -1)) in self.carriers

    def _guard_ground_bombs(self, now: float) -> None:
        """Resurface an idle ground bomb that was buried, sealed in or left
        floating for ``objective_entomb_seconds`` (1 Hz).

        Armed bombs detonate on their fuse anyway; an unarmed bomb never
        expires, so defenders could otherwise bury the bomb spawn and stop
        the attack for the rest of the match.
        """
        clock = time.monotonic()
        if clock < getattr(self, "_guard_next_at", 0.0):
            return
        self._guard_next_at = clock + 1.0
        timer = getattr(self, "_bomb_trap", None)
        if timer is None:
            timer = self._bomb_trap = objective_guard.TrapTimer()
        wm = getattr(self.server, "world_manager", None)
        if wm is None or getattr(wm, "map", None) is None:
            return
        grace = objective_guard.entomb_seconds(self.server)
        for bomb in tuple(self.bombs.values()):
            idle = bomb.entity_id is not None and bomb.carrier_id is None and not bomb.armed
            reason = (
                objective_guard.ground_objective_trapped(
                    wm, bomb.position, player_space=False
                )
                if idle else None
            )
            if not timer.due(bomb.serial, reason is not None, clock, grace):
                continue
            try:
                settled = objective_guard.resettle_surface(
                    wm, bomb.position, player_space=False
                )
            except Exception:
                logger.exception("bomb resettle failed")
                continue
            logger.info("Occupation bomb %d %s; resurfacing", bomb.serial, reason)
            old_id = bomb.entity_id
            self.server.broadcast_destroy_entity(old_id)
            self.server.entity_registry.remove(old_id)
            self.entity_to_bomb.pop(old_id, None)
            bomb.position = tuple(float(v) for v in settled)
            entity = self.server.entity_registry.place(
                int(C.BOMB_PICKUP),
                *bomb.position,
                state=TEAM_NEUTRAL,
                kind="occupation_bomb",
                radius=0.5,
                fuse=0.0,
            )
            self.server.broadcast_create_entity(entity)
            bomb.entity_id = int(entity.entity_id)
            self.entity_to_bomb[bomb.entity_id] = bomb.serial

    def _award_periodic_scores(self, periods: int) -> None:
        players = getattr(self.server, "players", {})
        for player_id in tuple(self.carriers):
            player = players.get(player_id)
            if self._active_player(player):
                self._award_player(
                    player,
                    periods * int(CG.OC_SCORE_CARRY_SCORE),
                    int(C.SCORE_REASON.OCC_CARRY_SCORE_REASON),
                )
        for player in players.values():
            if (
                self._active_player(player)
                and int(player.team) == TEAM1
                and self._inside_target(player)
            ):
                self._award_player(
                    player,
                    periods * int(CG.OC_SCORE_OCCUPY_SCORE),
                    int(C.SCORE_REASON.OCC_OCCUPY_SCORE_REASON),
                )

    def _send_target_zone(self, connection=None) -> None:
        if self.target_zone is None:
            return
        packet = minimap_zone_packet(
            self.target_zone,
            color=self.server.teams[TEAM2].color,
            icon_id=int(CG.ZONE_ICON_OCCUPATION),
            visible_team=TEAM_NEUTRAL,
        )
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            connection.send(data, reliable=True)

    def _inside_target(self, player) -> bool:
        return bool(
            self.target_zone
            and self.target_zone.contains((player.x, player.y, player.z))
        )

    def _target_distance(self, player) -> float:
        """Horizontal blocks from the player to the target footprint (0 inside)."""

        if self.target_zone is None:
            return 0.0
        x0, x1, y0, y1, _z0, _z1 = self.target_zone.bounds
        dx = max(float(x0) - float(player.x), 0.0, float(player.x) - float(x1))
        dy = max(float(y0) - float(player.y), 0.0, float(player.y) - float(y1))
        return (dx * dx + dy * dy) ** 0.5

    def _bomb_inside_target(self, position) -> bool:
        """Use the authored horizontal objective footprint for a floor bomb.

        Players occupy the volume at their feet anchor, while a dropped entity
        is serialized on the supporting voxel surface (about 2.25 blocks
        lower in VXL coordinates).  Small UGC zones end two blocks below their
        center, so applying the player Z bounds to the bomb itself rejects a
        visually valid floor placement by a quarter block.
        """

        if self.target_zone is None:
            return False
        x0, x1, y0, y1, _z0, _z1 = self.target_zone.bounds
        return (
            x0 <= float(position[0]) <= x1
            and y0 <= float(position[1]) <= y1
        )

    def _surface_anchor(self, x: float, y: float) -> tuple[float, float, float]:
        wm = getattr(self.server, "world_manager", None)
        reader = getattr(wm, "dry_surface_anchor", None)
        if callable(reader):
            return tuple(float(value) for value in reader(x, y))
        return float(x), float(y), 60.0

    def _clear_bombs(self) -> None:
        from server.pickups import broadcast_drop

        for player_id in tuple(self.carriers):
            player = getattr(self.server, "players", {}).get(player_id)
            if (
                player is not None
                and getattr(player, "pickup_id", None) == int(C.BOMB_PICKUP)
            ):
                broadcast_drop(
                    self.server,
                    player,
                    (player.x, player.y, player.z),
                    (0.0, 0.0, 0.0),
                )
        registry = self.server.entity_registry
        for bomb in tuple(self.bombs.values()):
            if bomb.entity_id is None:
                continue
            # After reset_round_runtime the registry was wiped (clients got
            # their DestroyEntity then) and ids restart at 0: only destroy an
            # id that still names this mode's own bomb entity.
            entity = registry.get(bomb.entity_id)
            if entity is not None and str(getattr(entity, "kind", "")).startswith(
                "occupation_bomb"
            ):
                self.server.broadcast_destroy_entity(bomb.entity_id)
                registry.remove(bomb.entity_id)
        self.bombs.clear()
        self.entity_to_bomb.clear()
        self.carriers.clear()
        self._pending_spawns.clear()

    def _broadcast_team_score(self, team, reason) -> None:
        try:
            self.server.broadcast_set_score(team, reason=int(reason))
        except TypeError:
            self.server.broadcast_set_score(team)

    def _award_player(self, player, points: int, reason: int) -> None:
        if points <= 0 or not self._owns_slot(player) or self.retiring:
            return
        from server.scoreboard import send_player_score

        player.score = int(getattr(player, "score", 0)) + int(points)
        send_player_score(self.server, player, reason=int(reason))

    @staticmethod
    def _active_player(player) -> bool:
        return bool(
            player is not None
            and getattr(player, "alive", False)
            and getattr(player, "spawned", True)
            and int(getattr(player, "team", -1)) in _PLAYABLE_TEAMS
        )


__all__ = ["OccupationBomb", "OccupationMode"]
