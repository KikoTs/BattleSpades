"""Contextual, bounded team tasks layered over the existing movement motor.

No native players, mutations, hidden target queries, or path searches live here.
Tasks request ordinary actions and wait for authoritative feedback and world
changes before using what they built. Tactical evaluation is staggered at 2 Hz.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math

import shared.constants as C
from server.game_constants import WEAPON_PROFILES

from .behavior_memory import BehaviorMemory
from .combat_profiles import envelope_for
from .messages import BotAction, BotActionKind, PerceptionFrame, PlayerSnapshot, Vector3
from .policies import ModeBotDecision, ModeBotPosture, canonical_mode_id, mode_objective_committed
from .project_sites import (
    ProjectSite, find_bridge_project, find_decorative_site,
    find_mine_approach, find_prefab_cover, find_rampart_segment,
    find_sniper_outpost,
)
from .schematics import get as get_schematic
from .schematics.model import PALETTE, Placement, Schematic
from .schematics.planner import (
    PLAN_REACH, cell_centre, node_for_position, node_position, ray_clear, step_remaining,
    walk_path,
)
from .schematics.sites import SchematicSite, SchematicSites, find_schematic_site, interior_columns
from .simple_navigation import SimpleVoxelWorld
from .team_tasks import Identity, TacticalOrder, TaskStage, TeamProject, TeamTasks

# Optional schematic choice per purpose; the first feasible one is built.
_DEFENSIVE_SCHEMATICS = ("sandbag_wall", "cover_wall", "corner_cover", "pillbox", "bunker")
_OBJECTIVE_SCHEMATICS = ("objective_ring", "sandbag_wall", "cover_wall")
_SNIPER_SCHEMATICS = ("sniper_nest", "watchtower")
_GENERIC_SCHEMATICS = ("cover_wall", "sandbag_wall", "small_hut", "corner_cover")


def identity(player: PlayerSnapshot) -> Identity:
    return player.player_id, player.generation, player.life_id


def _mix(*values: int) -> float:
    """Stable [0, 1) jitter from identities/time buckets (no global RNG)."""
    acc = 0x811C9DC5
    for value in values:
        acc = ((acc ^ (int(value) & 0xFFFFFFFF)) * 0x01000193) & 0xFFFFFFFF
    return (acc % 997) / 997.0


def _reached_landing(player: PlayerSnapshot, landing: Vector3) -> bool:
    """Route use requires standing on the far bank, beyond the obstacle."""
    return (player.grounded and not player.wade
            and math.floor(player.position[0]) == math.floor(landing[0])
            and math.floor(player.position[1]) == math.floor(landing[1])
            and abs(player.position[2] - landing[2]) < 1)


@dataclass(slots=True)
class _Task:
    task_id: int
    kind: str
    goal: Vector3
    lane: Vector3
    started_at: float
    expires_at: float
    score: float
    site: ProjectSite | None = None
    stage: TaskStage = TaskStage.APPROACH
    phase: str = "prepare"
    patient: Identity | None = None
    patient_health: int = 100
    pending: BotAction | None = None
    sent_at: float = 0.0
    action_site: ProjectSite | None = None
    confirmed: bool = False
    progress_at: float = 0.0
    best_distance: float = math.inf
    remaining_cells: int = -1
    next_action: int = 1
    built_cells: tuple[tuple[int, int, int], ...] = ()
    source_entity_id: int = -1
    starting_supplies: tuple[int, ...] = ()
    # Schematic construction: the shared site, the claimed plan step, the
    # claimed stand and the earliest time the next placement may go out
    # (tool switch / human pause between drags).
    schematic_id: int = -1
    step_index: int = -1
    stand: Vector3 | None = None
    ready_at: float = 0.0
    waiting_since: float = 0.0


@dataclass(slots=True)
class _Life:
    key: Identity
    loadout: tuple[int, ...]
    prefabs: tuple[str, ...] = ()
    memory: BehaviorMemory = field(default_factory=BehaviorMemory)
    task: _Task | None = None
    next_evaluate: float = 0.0
    last_seen: float = 0.0
    next_project: float = 0.0
    partner: Identity | None = None
    partner_until: float = 0.0
    partner_progress_at: float = 0.0
    partner_best_distance: float = math.inf
    next_partner_at: float = 0.0
    partner_anchor: Vector3 | None = None
    partner_heading: tuple[float, float] = (1.0, 0.0)
    partner_holding: bool = False
    climb_order: TacticalOrder | None = None


class CooperativeBehavior:
    """One shared worker coordinator, with small per-life and per-team state."""

    def __init__(self, world: SimpleVoxelWorld) -> None:
        self.world = world
        self.epoch: tuple[int, int] = (-1, -1)
        self.lives: dict[tuple[int, int], _Life] = {}
        self.teams = TeamTasks()
        self.patients: dict[Identity, tuple[Identity, float]] = {}
        self.sites = SchematicSites()

    def reset(self) -> None:
        self.lives.clear()
        self.patients.clear()
        self.teams = TeamTasks()
        if self.epoch != (-1, -1):
            # Sites requested before the first frame belong to that frame's map.
            self.sites = SchematicSites()

    def forget(self, player_id: int, generation: int) -> None:
        life = self.lives.pop((player_id, generation), None)
        self.sites.forget((player_id, generation))
        if life and life.task:
            self.teams.projects.pop(life.task.task_id, None)
        if life:
            self.patients = {key: value for key, value in self.patients.items()
                             if key != life.key and value[0] != life.key}

    def decide(self, frame: PerceptionFrame, player: PlayerSnapshot,
               visible: PlayerSnapshot | None,
               strategic: ModeBotDecision | None) -> TacticalOrder | None:
        now = float(frame.created_at)
        epoch = (frame.map_epoch, frame.mode_epoch)
        if self.epoch != epoch:
            # Sites requested before the first frame belong to this epoch.
            requested = self.sites if self.epoch == (-1, -1) else None
            self.reset()
            if requested is not None:
                self.sites = requested
            self.epoch = epoch
        key = (player.player_id, player.generation)
        life = self.lives.get(key)
        if (life is None or life.key != identity(player) or life.loadout != player.loadout
                or life.prefabs != player.prefabs):
            self.forget(*key)
            life = self.lives[key] = _Life(identity(player), player.loadout, player.prefabs)
        life.last_seen = now
        life.memory.observe(frame, player, visible)
        self._expire(frame, now)
        if not player.grounded or player.wade:
            # Mid-jump on a stair/step the builder climbs: keep the same
            # movement owner instead of handing a half-finished hop back to
            # unrelated navigation.
            order = life.climb_order
            if (order is not None and not player.wade and life.task is not None
                    and life.task.kind == "schematic" and life.task.task_id == order.task_id):
                return order
            return None
        life.climb_order = None
        allies = tuple(p for p in frame.players if p.alive and p.spawned and p.team == player.team)
        # Role urgency is not permission for optional construction/formation
        # to replace the mode's actual winning job.
        fortifying = strategic is not None and strategic.directive == "fortify"
        # A mode that orders fortification (Zombie survivors at their refuge)
        # wants construction here; only carrying an objective or a committed
        # non-building job may suppress optional work.
        critical = player.carried_entity_id >= 0 or (
            mode_objective_committed(strategic) and not fortifying
        )
        combat_visible = visible if self._combat_relevant(frame, player, visible) else None
        danger = visible is not None and math.dist(player.position, visible.position) < 10
        defending = bool(life.task and life.task.kind in {"outpost", "cover", "strongpoint"}
            and life.task.phase == "occupy" and life.task.stage is TaskStage.USE
            and life.task.site and math.dist(player.position, life.task.site.approach) <= 1.5)
        combat_interrupt = (combat_visible is not None and life.task is not None
                            and life.task.kind != "heal" and not defending)
        # Schematic construction that serves the mode (a VIP shelter, a
        # requested zombie stair, a ring at an objective a defender already
        # holds) may run under a committed role; see _schematic_allowed.
        building_ok = self._schematic_allowed(frame, player, strategic, life.task)
        objective_support = bool(critical and life.task and (
            life.task.kind == "schematic" and (building_ok or life.task.pending is not None
                                               or self._site_finished(life.task))
            or strategic is not None and self._supports_objective(life.task, player, strategic, now)))
        if (life.task and life.task.kind == "schematic" and life.task.pending is None
                and player.last_damage_at > 0 and 0 <= now - player.last_damage_at <= 1.0):
            # Taking hits while building: stop and fight; repeated hits on the
            # same site abandon it for the whole team.
            site = self.sites.sites.get(life.task.schematic_id)
            if site is not None:
                self.sites.under_fire(site, now)
            self._finish(life, now, False, "under_fire")
            life.next_project = now + 6.0
        if life.task and (critical and not objective_support or danger and life.task.kind != "heal"
                          or combat_interrupt or self._live_hazard(frame, player)):
            self._finish(life, now, False, "combat_contact" if combat_interrupt else "urgent_interrupt")
        if (life.task and life.task.kind in {"outpost", "cover", "strongpoint", "sabotage"}
                and self._ammunition(player) <= 0):
            # A firing position is worthless to an empty gun; a dry Marksman
            # held its perch with a pickaxe while being shot. Free the bot to
            # resupply or close in instead.
            self._finish(life, now, False, "ammo_exhausted")
        if critical:
            # Retiring the ownership is essential: otherwise a completed
            # objective resumes an obsolete human-follow lease immediately.
            life.partner = None
            life.partner_anchor = None
            life.partner_holding = False
            life.partner_until = 0.0
            if life.task is not None:
                order = self._advance(frame, player, combat_visible, life, allies)
                if order is not None or life.task is not None:
                    return order
            if building_ok and combat_visible is None and not self._live_hazard(frame, player):
                sheltered = self._vip_shelter_hold(frame, player, strategic)
                if sheltered is not None:
                    return sheltered
                if now >= life.next_project:
                    task = self._schematic_task(frame, player, life, strategic, allies,
                                                critical=True, lane=None)
                    if task is not None:
                        life.task = task
                        self.teams.event("tasks_started", task.task_id, task.kind, now)
                        return self._advance(frame, player, combat_visible, life, allies)
                    # Nothing feasible here: site searches plan geometry, so
                    # back off instead of re-planning every decision.
                    life.next_project = now + 4.0
            if (strategic is not None and self._can_stop_for_supplies(strategic)
                    and combat_visible is None and now >= life.next_evaluate
                    and not self._live_hazard(frame, player)
                    and (player.health < 55 or self._ammunition(player) == 0)):
                life.next_evaluate = now + .75
                # Only urgent personal supplies, close to the objective route,
                # can borrow up to four seconds. No patient/partner chasing.
                support = self._medical_task(frame, player, (player,), life)
                if support is None:
                    support = self._supply_task(frame, player, life)
                if support is not None:
                    if self._supports_objective(support, player, strategic, now):
                        support.expires_at = min(support.expires_at, now + 4)
                        life.task = support
                        self.teams.event("tasks_started", support.task_id, support.kind, now)
                        return self._advance(frame, player, combat_visible, life, allies)
                    if support.patient is not None:
                        claim = self.patients.get(support.patient)
                        if claim is not None and claim[0] == life.key:
                            self.patients.pop(support.patient, None)
            return None
        if life.task:
            order = self._advance(frame, player, combat_visible, life, allies)
            if order is not None:
                return order
        if now < life.next_evaluate:
            return (self._support_project(frame, player, combat_visible) if life.task is None else None) or self._partner_order(frame, player, life, allies, combat_visible)
        life.next_evaluate = now + .5 + (player.player_id % 7) * .027
        if life.task is None:
            healing = self._medical_task(frame, player, allies, life)
            if healing:
                life.task = healing
                self.teams.event("tasks_started", healing.task_id, healing.kind, now)
                return self._advance(frame, player, combat_visible, life, allies)
        if life.task is not None:
            return None
        # Heal a nearby patient or fight from already occupied cover, but do
        # not start optional construction/approach work instead of fighting.
        if combat_visible is not None or self._live_hazard(frame, player):
            return None
        contact = life.memory.contact(player.position, now)
        lane = visible.eye if visible else contact.position if contact else (
            strategic.position if strategic else None)
        if fortifying and visible is None and contact is None:
            lane = self._fortify_lane(frame, player, strategic)
        profile = frame.profile
        creativity = profile.creativity if profile else .5
        teamwork = profile.teamwork if profile else .5
        candidates: list[_Task] = []
        task_id = frame.frame_id * 256 + player.player_id
        if combat_visible is None:
            supply = self._supply_task(frame, player, life)
            if supply is not None:
                candidates.append(supply)
            sabotage = self._sabotage_task(frame, player, life, allies)
            if sabotage is not None:
                candidates.append(sabotage)
        friends = tuple(p.position for p in allies if p.player_id != player.player_id)
        reserved = self._reserved_cells(player.team)
        if lane is not None and now >= life.next_project and player.health >= 45:
            # At most one geometry family per evaluation. Stable phase rotation
            # prevents a failure in one family from monopolizing every decision.
            has_sniper = any(tool in player.loadout for tool in (int(C.SNIPER_TOOL), int(C.SNIPER2_TOOL)))
            has_miner = int(C.SUPERSPADE_TOOL) in player.loadout
            site = None
            kind = ""
            refuge = self._refuge(frame, player) if fortifying else None
            if (refuge is not None and int(C.BLOCK_TOOL) in player.loadout
                    and math.dist(player.position, refuge) <= 14):
                # The squad walls its refuge before anything optional: the
                # nearest grounded run on the lowest unfinished layer that
                # no teammate has reserved.
                site = find_rampart_segment(self.world, player, refuge,
                    friendly_positions=friends, reserved_cells=reserved)
                kind = "rampart"
            if site is not None:
                pass
            elif has_sniper and math.dist(player.position, lane) > 18:
                site = find_sniper_outpost(self.world, player, lane, friendly_positions=friends)
                kind = "outpost"
            elif has_miner and combat_visible is None:
                site = find_bridge_project(self.world, player, lane, reserved_cells=reserved)
                kind = "bridge"
                if site is None:
                    from .project_sites import find_breach_project
                    site = find_breach_project(self.world, player, lane)
                    kind = "breach"
            elif player.blocks >= 6 and (combat_visible is None or player.reloading) and (creativity > .35 or fortifying):
                site = find_prefab_cover(self.world, player, lane,
                    friendly_positions=friends, reserved_cells=reserved)
                kind = "strongpoint" if int(C.ROCKET_TURRET_TOOL) in player.loadout else "cover"
            if site is not None and (kind == "rampart"
                                     or not self._near_objective(frame, site.position, 10)):
                score = .66 + creativity * .18 + teamwork * .08
                if kind == "rampart":
                    score = .8 + teamwork * .1
                score -= life.memory.penalty(kind, site.position, now)
                if score > .35:
                    patience = 24 + 48 * (profile.caution if profile else .5)
                    candidates.append(_Task(task_id, kind, site.approach, lane, now,
                        now + (patience if kind == "outpost" else 25), score, site=site,
                        progress_at=now))
        if (visible is None and contact is not None and contact.expires_at - now >= 2.0
                and math.dist(player.position, contact.position) > 4):
            # Evidence about to expire would start an errand only to cancel it
            # on the next decision, resetting the bot's real route twice.
            score = .4 + creativity * .1 - life.memory.penalty("investigate", contact.position, now)
            if score > .2:
                candidates.append(_Task(task_id, "investigate", contact.position, contact.position,
                    now, min(contact.expires_at, now + 6), score, progress_at=now))
        if (visible is None and now >= life.next_project and player.health >= 45
                and not fortifying):
            # Join a teammate's schematic, or (rarely, budgeted) start one.
            schematic = self._schematic_task(frame, player, life, strategic, allies,
                                             critical=False, lane=lane)
            if schematic is not None:
                candidates.append(schematic)
        if candidates:
            # Near ties use an identity/decision-stable preference, not global RNG.
            task = max(candidates[:4], key=lambda t: t.score +
                       ((task_id * 17 + len(t.kind) * 31) % 19) * .001)
            if task.site is None or self.teams.reserve(TeamProject(task.task_id, player.team,
                    task.kind, identity(player), task.site.approach, task.lane, now,
                    task.expires_at, cells=task.site.cells, last_progress_at=now)):
                life.task = task
                life.next_project = now + 3.0
                self.teams.event("tasks_started", task.task_id, task.kind, now)
                return self._advance(frame, player, combat_visible, life, allies)
        support = self._support_project(frame, player, combat_visible)
        if support:
            return support
        partner = self._partner_order(frame, player, life, allies, combat_visible)
        if partner:
            return partner
        # Optional mischief is one added block, only near friendly activity,
        # after useful work and outside combat/objective corridors.
        if (frame.friendly_mischief and frame.local_safety_complete
                and visible is None and contact is None
                and life.memory.pressure < .1 and player.blocks >= 30
                and any(p.player_id != player.player_id and math.dist(p.position, player.position) < 18 for p in allies)
                and creativity > .6
                and now >= self.teams.mischief_ready.get(player.team, 0)
                and int(now + player.player_id * 13) % 47 == 0
                and not self._near_objective(frame, player.position, 24)):
            site = find_decorative_site(self.world, player,
                friendly_positions=friends, reserved_cells=reserved)
            if site and self.teams.allow_mutation(player.team, now, 1):
                self.teams.mischief_ready[player.team] = now + 90
                life.task = _Task(task_id, "mischief", site.approach, site.position,
                    now, now + 8, .1, site=site, progress_at=now)
                self.teams.event("tasks_started", task_id, "mischief", now)
                return self._advance(frame, player, visible, life, allies)
        return None

    @staticmethod
    def _ammunition(player: PlayerSnapshot) -> int:
        stowed = sum(clip + reserve for _tool, clip, reserve in player.weapon_ammo)
        return stowed if player.weapon_ammo else player.ammo_clip + player.ammo_reserve

    @staticmethod
    def _can_stop_for_supplies(strategic: ModeBotDecision) -> bool:
        return not strategic.role.endswith("_passive") and strategic.role not in {
            "demolition_escape_airstrike", "occupation_dispose_bomb",
            "zombie_last_survivor_escape", "vip_retreat",
            "multihill_evade_airstrike",
        }

    @staticmethod
    def _supports_objective(task: _Task, player: PlayerSnapshot,
                            strategic: ModeBotDecision, now: float) -> bool:
        """Permit only short, nearby survival stops, never an objective detour."""
        if task.kind not in {"heal", "resupply"} or now - task.started_at >= 4:
            return False
        distance = math.dist(player.position, task.goal)
        detour = distance + math.dist(task.goal, strategic.position) - math.dist(
            player.position, strategic.position)
        return (distance <= 6 and detour <= 3
                and CooperativeBehavior._can_stop_for_supplies(strategic))

    @staticmethod
    def _combat_relevant(frame: PerceptionFrame, player: PlayerSnapshot,
                         visible: PlayerSnapshot | None) -> bool:
        """Optional work yields to a useful shot or actual nearby/recent threat."""
        if visible is None:
            return False
        if (player.last_damage_source_id == visible.player_id and player.last_damage_at > 0
                and 0 <= frame.created_at - player.last_damage_at <= 2):
            return True
        owned = frozenset(player.loadout)
        weapon = next((tool for tool in (player.weapon_tool, player.tool, *player.loadout[:16])
                       if tool in owned and tool in WEAPON_PROFILES), None)
        # Reuse the real fighting doctrine, not the 160-block sight radius.
        # A distant silhouette must not repeatedly cancel a shotgunner's work.
        effective_range = max(10., envelope_for(weapon).ideal_max) if weapon is not None else 10.
        return math.dist(player.eye, visible.eye) <= effective_range

    def _medical_task(self, frame: PerceptionFrame, player: PlayerSnapshot,
                      allies: tuple[PlayerSnapshot, ...], life: _Life) -> _Task | None:
        now = frame.created_at
        packs = [e for e in frame.entities if e.alive and e.tool_id == int(C.MEDPACK_TOOL)
                 and e.team == player.team and e.uses_remaining != 0]
        patients = sorted((p for p in allies if p.health < 80
            and math.dist(player.position, p.position) <= 24),
            key=lambda p: (p.health + math.dist(player.position, p.position) * 2, p.player_id))
        for patient in patients[:4]:
            claim = self.patients.get(identity(patient))
            existing = next((e for e in packs if math.dist(e.position, patient.position) < 8), None)
            if claim and claim[0] != life.key and claim[1] > now and not (
                    existing is not None and patient.player_id == player.player_id):
                continue
            if existing is not None:
                if patient.player_id != player.player_id:
                    continue
                goal = existing.position
            elif (int(C.MEDPACK_TOOL) not in player.loadout
                  or dict(player.deployable_stock).get(int(C.MEDPACK_TOOL), 0) <= 0):
                continue
            else:
                goal = patient.position
            if life.memory.penalty("heal", goal, now) >= .7:
                continue
            self.patients[identity(patient)] = (life.key, now + 10)
            return _Task(frame.frame_id * 256 + player.player_id, "heal", goal, goal,
                now, now + 12, .95, patient=identity(patient),
                patient_health=patient.health, phase="use_pack" if existing else "prepare",
                progress_at=now)
        return None

    @staticmethod
    def _supplies(player: PlayerSnapshot) -> tuple[int, ...]:
        return (player.health, player.blocks, player.ammo_clip + player.ammo_reserve,
                sum(count for _, count in player.deployable_stock))

    def _supply_task(self, frame: PerceptionFrame, player: PlayerSnapshot,
                     life: _Life) -> _Task | None:
        desired = set()
        if player.health < 55:
            desired.add(int(C.HEALTH_CRATE))
        depleted_medical = (int(C.MEDPACK_TOOL) in player.loadout
            and dict(player.deployable_stock).get(int(C.MEDPACK_TOOL), 0) <= 0)
        if player.blocks < 12:
            desired.add(int(C.BLOCK_CRATE))
        if self._ammunition(player) < 6 or depleted_medical:
            desired.add(int(C.AMMO_CRATE))
        sources = sorted((entity for entity in frame.entities[:96]
            if entity.alive and entity.entity_type in desired
            and math.dist(player.position, entity.position) < 32
            and life.memory.penalty("resupply", entity.position, frame.created_at) < .7),
            key=lambda entity: math.dist(player.position, entity.position))
        for entity in sources[:3]:
            if not self.world.has_line_of_sight(player.eye, entity.position):
                continue
            return _Task(frame.frame_id * 256 + player.player_id, "resupply",
                entity.position, entity.position, frame.created_at, frame.created_at + 12,
                .88, progress_at=frame.created_at, source_entity_id=entity.entity_id,
                starting_supplies=self._supplies(player))
        return None

    def _sabotage_task(self, frame: PerceptionFrame, player: PlayerSnapshot,
                       life: _Life, allies: tuple[PlayerSnapshot, ...]) -> _Task | None:
        """Shoot an actually visible enemy device through ordinary combat."""
        weapon = player.weapon_tool if player.weapon_tool >= 0 else player.tool
        if player.ammo_clip + player.ammo_reserve <= 0 or weapon not in player.loadout:
            return None
        targets = [entity for entity in frame.entities[:96] if entity.alive
            and entity.team >= 0 and entity.team != player.team
            and entity.tool_id in {int(C.ROCKET_TURRET_TOOL), int(C.RADAR_STATION_TOOL)}
            and math.dist(player.eye, entity.position) < 40]
        for target in sorted(targets, key=lambda e: math.dist(player.eye, e.position))[:3]:
            center = target.hit_position
            if center is None or target.hit_radius <= 0:
                continue
            aim = next((point for point in (center, (center[0], center[1], center[2] - target.hit_radius * .65))
                        if self.world.has_line_of_sight(player.eye, point)), None)
            if (aim is None
                    or any(math.dist(p.position, target.position) < 6 for p in allies)
                    or life.memory.penalty("sabotage", aim, frame.created_at) >= .7):
                continue
            aggression = frame.profile.aggression if frame.profile else .5
            return _Task(frame.frame_id * 256 + player.player_id, "sabotage",
                player.position, aim, frame.created_at, frame.created_at + 6,
                .7 + aggression * .15, progress_at=frame.created_at,
                source_entity_id=target.entity_id)
        return None

    def _advance(self, frame: PerceptionFrame, player: PlayerSnapshot,
                 visible: PlayerSnapshot | None, life: _Life,
                 allies: tuple[PlayerSnapshot, ...]) -> TacticalOrder | None:
        task = life.task
        assert task is not None
        now = frame.created_at
        if now >= task.expires_at:
            occupied = (task.kind in {"outpost", "cover", "strongpoint"}
                        and task.stage is TaskStage.USE
                        and math.dist(player.position, task.goal) < 2)
            self._finish(life, now, occupied, "lease_expired")
            return None
        project = self.teams.projects.get(task.task_id)
        if project:
            project.stage = task.stage
        if task.pending is not None:
            action = task.pending
            rejected = (player.last_action_request_id == action.request_id
                        and not player.last_task_accepted)
            if rejected:
                if task.kind == "schematic":
                    return self._schematic_step_result(life, task, False,
                        player.last_action_reason or "rejected", now)
                self._finish(life, now, False, player.last_action_reason or "rejected")
                return None
            if task.action_site and task.action_site.cells:
                confirmed = all(self.world.solid(*cell) for cell in task.action_site.cells)
            else:
                confirmed = any(e.alive and e.owner_id == player.player_id
                    and e.tool_id == action.tool_id and e.position is not None
                    and math.dist(e.position, action.position) < 4 for e in frame.entities)
            confirmed = confirmed and player.last_action_request_id == action.request_id and player.last_task_accepted
            if not confirmed:
                if task.kind == "schematic":
                    if now - task.sent_at > 3.5:
                        return self._schematic_step_result(life, task, False,
                                                           "confirmation_timeout", now)
                    # Keep the latched placement alive until the director has
                    # executed it (it deduplicates by request id); afterwards
                    # just hold the aim while the world delta arrives.
                    executed = player.last_action_request_id == action.request_id
                    return TacticalOrder(task.task_id, "schematic_confirm", player.position,
                                         action.position, BotAction() if executed else action,
                                         hold=True, tool_id=int(C.BLOCK_TOOL))
                if now - task.sent_at > 8:
                    self._finish(life, now, False, "confirmation_timeout")
                    return None
                return TacticalOrder(task.task_id, task.kind + "_confirm", player.position,
                                     task.lane, hold=True,
                                     tool_id=action.tool_id if action.kind is BotActionKind.PLACE_PREFAB else -1)
            task.pending = None
            task.confirmed = True
            if task.action_site and task.action_site.cells:
                task.built_cells += task.action_site.cells
            task.progress_at = now
            self.teams.event("actions_confirmed", task.task_id, task.kind, now)
            if task.kind == "schematic":
                return self._schematic_step_result(life, task, True, "placed", now)
            if task.kind == "rampart":
                self._finish(life, now, True, "rampart_built")
                life.next_project = now + 2.5
                return None
            if task.kind == "heal":
                task.phase = "use_pack"
            elif task.kind == "outpost":
                task.phase = "security" if task.phase == "cover" else "occupy"
            elif task.kind == "strongpoint" and task.phase == "prepare":
                task.phase = "security"
            else:
                task.phase = "occupy"
            task.stage = TaskStage.USE
        if task.kind == "schematic":
            return self._advance_schematic(frame, player, life, allies)
        if task.kind == "heal":
            patient = next((p for p in allies if identity(p) == task.patient), None)
            if patient is None:
                self._finish(life, now, False, "patient_unavailable")
                return None
            if patient.health > task.patient_health:
                self._finish(life, now, True, "patient_healed")
                return None
            self.patients[task.patient] = (life.key, now + 3)
            task.goal = patient.position if task.phase != "use_pack" else task.goal
            if task.phase == "use_pack":
                pack = next((e for e in frame.entities if e.alive and e.tool_id == int(C.MEDPACK_TOOL)
                    and e.team == player.team and e.uses_remaining != 0
                    and math.dist(e.position, task.goal) < 4), None)
                if pack is None:
                    self._finish(life, now, False, "pack_unavailable")
                    return None
                if patient.player_id != player.player_id:
                    # Cover the patient; their own task can seek the pack.
                    return None
                return TacticalOrder(task.task_id, "use_medpack", task.goal, task.lane, arrival_radius=1)
            if math.dist(player.position, patient.position) > 3.25:
                return self._move(task, patient.position, "medic_approach", 2.5)
            node = self.world.surface(math.floor(patient.position[0]), math.floor(patient.position[1]),
                                      patient.position[2], vertical_span=2, clearance=3)
            if node is None:
                self._finish(life, now, False, "unsupported_patient")
                return None
            position = (node.x + .5, node.y + .5, float(node.support_z))
            task.goal = position
            return self._action(frame, player, task,
                BotAction(BotActionKind.DEPLOY, int(C.MEDPACK_TOOL), position=position, face=4))
        if task.kind == "investigate":
            if visible is not None:
                self._finish(life, now, True, "contact_acquired")
                return None
            if math.dist(player.position, task.goal) < 4:
                self._finish(life, now, True, "evidence_checked")
                return None
            return self._move(task, task.goal, "investigate_sound", 3)
        if task.kind == "resupply":
            if any(after > before for after, before in zip(self._supplies(player), task.starting_supplies)):
                self._finish(life, now, True, "supplies_restored")
                return None
            if not any(e.entity_id == task.source_entity_id and e.alive for e in frame.entities):
                self._finish(life, now, False, "supply_unavailable")
                return None
            return self._move(task, task.goal, "resupply", 1)
        if task.kind == "sabotage":
            target = next((e for e in frame.entities if e.entity_id == task.source_entity_id
                           and e.alive and e.team != player.team), None)
            if (visible is not None or target is None
                    or not self.world.has_line_of_sight(player.eye, task.lane)):
                # A missing replication entry is not proof of a kill.
                self._finish(life, now, False, "device_contact_lost")
                return None
            if any(math.dist(p.position, task.lane) < 6 for p in allies):
                self._finish(life, now, False, "friendly_near_device")
                return None
            tool = player.weapon_tool if player.weapon_tool >= 0 else player.tool
            reaction = frame.profile.reaction_time if frame.profile else .3
            if now - task.started_at < reaction or player.reloading:
                action = BotAction()
            elif player.ammo_clip > 0:
                action = BotAction(BotActionKind.FIRE, tool, position=task.lane,
                                   burst=3, burst_pause=.3)
            elif player.ammo_reserve > 0:
                action = BotAction(BotActionKind.RELOAD, tool)
            else:
                self._finish(life, now, False, "ammo_exhausted")
                return None
            return TacticalOrder(task.task_id, "sabotage_device", player.position,
                                 task.lane, action, hold=True)
        site = task.site
        if site is None:
            return None
        if site.support_cells and not all(self.world.solid(*cell) for cell in site.support_cells):
            self._finish(life, now, False, "support_changed")
            return None
        if task.built_cells and not all(self.world.solid(*cell) for cell in task.built_cells):
            self._finish(life, now, False, "construction_destroyed")
            return None
        if task.phase == "mine_approach" and task.action_site:
            mine = task.action_site
            if (dict(player.deployable_stock).get(int(C.LANDMINE_TOOL), 0) <= 0
                    or not all(self.world.solid(*cell) for cell in mine.support_cells)
                    or any(math.dist(p.position, mine.position) < 6 for p in allies
                           if p.player_id != player.player_id)):
                task.phase = "occupy"
                task.action_site = None
                return self._move(task, site.approach, "outpost_return", 1.25)
            if math.dist(player.position, mine.approach) > 1.5:
                return self._move(task, mine.approach, "secure_outpost_approach", 1)
            return self._action(frame, player, task, BotAction(BotActionKind.DEPLOY,
                int(C.LANDMINE_TOOL), position=mine.position), mine)
        destination = site.landing if task.kind in {"breach", "bridge"} else site.approach
        assert destination is not None
        distance = math.dist(player.position, destination)
        if distance < task.best_distance - .5:
            task.best_distance, task.progress_at = distance, now
        if task.kind == "breach":
            remaining = sum(self.world.solid(*cell) for cell in site.cells)
            if task.remaining_cells < 0 or remaining < task.remaining_cells:
                task.progress_at, task.remaining_cells = now, remaining
            if remaining == 0:
                task.stage = TaskStage.USE
                if project:
                    project.stage, project.position = TaskStage.USE, destination
                if _reached_landing(player, destination):
                    self._finish(life, now, True, "breach_used")
                    return None
            if now - task.progress_at > 8:
                self._finish(life, now, False, "no_excavation_progress")
                return None
            return self._move(task, destination, "squad_breach", .35)
        if task.kind == "bridge" and task.phase == "occupy":
            if not all(self.world.solid(*cell) for cell in site.cells):
                self._finish(life, now, False, "crossing_destroyed")
                return None
            if project:
                project.stage, project.position = TaskStage.USE, destination
            if _reached_landing(player, destination):
                self._finish(life, now, True, "bridge_used")
                return None
            if now - task.progress_at > 8:
                self._finish(life, now, False, "crossing_stalled")
                return None
            return self._move(task, destination, "cross_bridge", .35)
        if task.kind == "rampart":
            if any(self.world.solid(*cell) for cell in site.cells):
                # A teammate or human filled part of this run; replan.
                self._finish(life, now, False, "run_changed")
                life.next_project = now + 1.0
                return None
            if distance > 1.25:
                if now - task.progress_at > 8:
                    self._finish(life, now, False, "approach_stalled")
                    return None
                return self._move(task, destination, "rampart_approach", 1.0)
            if not self.teams.allow_mutation(player.team, now, len(site.cells)):
                self._finish(life, now, False, "construction_budget")
                return None
            return self._action(frame, player, task, BotAction(BotActionKind.BUILD_LINE,
                int(C.BLOCK_TOOL), position=site.cells[0], end_position=site.cells[-1]), site)
        if task.kind == "bridge":
            if not self.teams.allow_mutation(player.team, now, len(site.cells)):
                self._finish(life, now, False, "construction_budget")
                return None
            return self._action(frame, player, task, BotAction(BotActionKind.BUILD_LINE,
                int(C.BLOCK_TOOL), position=site.cells[0], end_position=site.cells[-1]), site)
        if distance > 1.5:
            if now - task.progress_at > 8:
                self._finish(life, now, False, "approach_stalled")
                return None
            return self._move(task, destination, task.kind + "_approach", 1.25)
        friends = tuple(p.position for p in allies if p.player_id != player.player_id)
        reserved = self._reserved_cells(player.team, exclude=task.task_id)
        if task.kind == "outpost" and task.phase == "prepare":
            cover = find_prefab_cover(self.world, player, task.lane, rear=True,
                friendly_positions=friends, reserved_cells=reserved)
            if cover:
                task.phase = "cover"
                return self._prefab(frame, player, task, cover)
            task.phase = "security"
        if task.phase == "security":
            if int(C.LANDMINE_TOOL) in player.loadout:
                mine = find_mine_approach(self.world, player, task.lane,
                    friendly_positions=friends, reserved_cells=reserved)
                if mine and not self._near_objective(frame, mine.position, 12):
                    # A separate approach is required; never remotely deploy.
                    task.action_site = mine
                    task.phase = "mine_approach"
            else:
                tool = int(C.ROCKET_TURRET_TOOL) if task.kind == "strongpoint" else int(C.RADAR_STATION_TOOL)
                if dict(player.deployable_stock).get(tool, 0) > 0:
                    node = self.world.surface(math.floor(player.position[0]), math.floor(player.position[1]),
                                              player.position[2], vertical_span=1, clearance=3)
                    if node and not any(e.alive and e.team == player.team and e.tool_id == tool
                                        and math.dist(e.position, player.position) < 15 for e in frame.entities):
                        task.phase = "deploy_security"
                        return self._action(frame, player, task, BotAction(BotActionKind.DEPLOY, tool,
                            position=(node.x + .5, node.y + .5, float(node.support_z)),
                            yaw=math.atan2(task.lane[1] - player.position[1], task.lane[0] - player.position[0])))
            if task.phase == "security":
                task.phase = "occupy"
        if task.phase == "mine_approach" and task.action_site:
            mine = task.action_site
            if math.dist(player.position, mine.approach) > 1.5:
                return self._move(task, mine.approach, "secure_outpost_approach", 1)
            return self._action(frame, player, task, BotAction(BotActionKind.DEPLOY,
                int(C.LANDMINE_TOOL), position=mine.position))
        if task.kind in {"cover", "strongpoint"} and task.phase == "prepare":
            return self._prefab(frame, player, task, site)
        if task.kind == "mischief" and task.phase == "prepare":
            return self._action(frame, player, task, BotAction(BotActionKind.BUILD,
                int(C.BLOCK_TOOL), position=site.position), site)
        task.stage = TaskStage.USE
        if task.kind == "mischief":
            self._finish(life, now, True, "decoration_added")
            return None
        if visible is not None:
            task.lane = visible.eye
            task.progress_at = now
        quiet_patience = 8 + 12 * (frame.profile.caution if frame.profile else .5)
        if task.kind == "outpost" and now - task.progress_at > quiet_patience:
            self._finish(life, now, True, "quiet_lane_relocate")
            return None
        if task.kind != "outpost" and now - task.progress_at > 5:
            self._finish(life, now, True, "cover_used")
            return None
        return TacticalOrder(task.task_id, task.kind + "_watch", site.approach,
                             task.lane, hold=True)

    def _action(self, frame: PerceptionFrame, player: PlayerSnapshot, task: _Task,
                action: BotAction, site: ProjectSite | None = None) -> TacticalOrder:
        request_id = task.task_id * 16 + task.next_action
        task.next_action += 1
        task.pending = replace(action, request_id=request_id)
        task.action_site = site
        task.sent_at = frame.created_at
        task.stage = TaskStage.CONFIRM
        if site and site.cells:
            self.teams.record_mutation(player.team, frame.created_at, len(site.cells))
        self.teams.event("actions_requested", task.task_id, action.kind.value, frame.created_at)
        return TacticalOrder(task.task_id, task.kind + "_execute", player.position,
            action.position, task.pending, hold=True, urgent=task.kind == "heal")

    def _prefab(self, frame: PerceptionFrame, player: PlayerSnapshot,
                task: _Task, site: ProjectSite) -> TacticalOrder | None:
        if not self.teams.allow_mutation(player.team, frame.created_at, len(site.cells)):
            life = self.lives[(player.player_id, player.generation)]
            self._finish(life, frame.created_at, False, "construction_budget")
            return None
        return self._action(frame, player, task, BotAction(BotActionKind.PLACE_PREFAB,
            int(C.PREFAB_TOOL), site.position, argument=site.prefab_name, yaw=site.yaw), site)

    @staticmethod
    def _move(task: _Task, goal: Vector3, role: str, radius: float) -> TacticalOrder:
        return TacticalOrder(task.task_id, role, goal, task.lane, arrival_radius=radius)

    def _finish(self, life: _Life, now: float, success: bool, reason: str) -> None:
        task = life.task
        if task is None:
            return
        life.memory.record(task.kind, task.goal, now, success)
        self.teams.event("tasks_completed" if success else "tasks_failed", task.task_id, reason, now)
        if task.kind == "schematic":
            site = self.sites.sites.get(task.schematic_id)
            if site is not None and task.step_index >= 0:
                self.sites.release(site, task.step_index, life.key)
            if site is not None:
                site.builders.pop(life.key, None)
        project = self.teams.projects.get(task.task_id)
        if success and project and task.kind in {"bridge", "breach"}:
            # Leave a short route-uptake lease after the builder advances.
            project.stage = TaskStage.USE
            project.position = task.site.landing
            project.expires_at = now + 12
        else:
            self.teams.projects.pop(task.task_id, None)
        if task.patient:
            self.patients.pop(task.patient, None)
        life.task = None
        life.next_project = now + (8 if success else 12)
        life.next_evaluate = now + .6

    def _support_project(self, frame: PerceptionFrame, player: PlayerSnapshot,
                         visible: PlayerSnapshot | None) -> TacticalOrder | None:
        if visible is not None or frame.profile and frame.profile.teamwork < .4:
            return None
        now = frame.created_at
        for project in sorted(self.teams.projects.values(), key=lambda p: math.dist(p.position, player.position)):
            if project.stage is TaskStage.USE and project.kind in {"bridge", "breach"}:
                usable = (all(self.world.solid(*cell) for cell in project.cells)
                          if project.kind == "bridge" else not any(self.world.solid(*cell) for cell in project.cells))
                if not usable:
                    self.teams.projects.pop(project.project_id, None)
                    self.teams.event("projects_invalidated", project.project_id, "route_changed", now)
                    continue
            if (project.team != player.team or project.owner == identity(player)
                    or identity(player) in project.used_by
                    or math.dist(project.position, player.position) > 24
                    or len(project.participants) >= 3 and identity(player) not in project.participants):
                continue
            if any(identity(player) in p.participants for p in self.teams.projects.values()
                   if p.project_id != project.project_id):
                continue
            project.participants[identity(player)] = now + 2
            if project.stage is TaskStage.USE and project.kind in {"bridge", "breach"}:
                if _reached_landing(player, project.position):
                    project.participants.pop(identity(player), None)
                    project.used_by.add(identity(player))
                    self.teams.event("routes_used_by_partner", project.project_id, project.kind, now)
                    return None
                return TacticalOrder(project.project_id, "squad_advance", project.position,
                                     project.lane, arrival_radius=.35)
            dx, dy = project.lane[0] - project.position[0], project.lane[1] - project.position[1]
            length = max(1., math.hypot(dx, dy))
            sign = 1 if player.player_id % 2 else -1
            goal = (project.position[0] - dx / length * 4 - dy / length * 3 * sign,
                    project.position[1] - dy / length * 4 + dx / length * 3 * sign,
                    project.position[2])
            return TacticalOrder(project.project_id, "cover_teammate", goal, project.lane,
                                 arrival_radius=2, hold=math.dist(player.position, goal) < 2)
        return None

    def _partner_order(self, frame: PerceptionFrame, player: PlayerSnapshot, life: _Life,
                       allies: tuple[PlayerSnapshot, ...], visible: PlayerSnapshot | None) -> TacticalOrder | None:
        if visible is not None or life.task is not None:
            return None
        if frame.profile and frame.profile.teamwork < .4:
            return None
        now = frame.created_at
        partner = next((p for p in allies if identity(p) == life.partner), None)
        if life.partner is not None and (partner is None or now > life.partner_until
                or now - life.partner_progress_at > 7):
            life.partner = None
            life.partner_anchor = None
            life.partner_holding = False
            life.next_partner_at = now + 10
            partner = None
        if life.partner is None and now < life.next_partner_at:
            return None
        if life.partner is None and any(member.partner == life.key and member.partner_until > now
                                        for member in self.lives.values()):
            return None
        if partner is None or now > life.partner_until or math.dist(player.position, partner.position) > 24:
            life.partner = None
            candidates = [p for p in allies if p.player_id != player.player_id
                          and (not p.is_bot or p.player_id < player.player_id)
                          and math.dist(player.position, p.position) < 18]
            medic = int(C.MEDPACK_TOOL) in player.loadout
            candidates = [p for p in candidates if not p.is_bot or
                          medic and int(C.MEDPACK_TOOL) in p.loadout]
            candidates = [p for p in candidates
                if not (self.lives.get((p.player_id, p.generation)) and
                        self.lives[(p.player_id, p.generation)].partner is not None)
                and sum(member.partner == identity(p) and member.partner_until > now
                        for member in self.lives.values()) < (1 if medic and p.is_bot else 3)]
            partner = min(candidates, key=lambda p: math.dist(player.position, p.position), default=None)
            if partner:
                commitment = 12 + 16 * (frame.profile.teamwork if frame.profile else .5)
                life.partner, life.partner_until = identity(partner), now + commitment
                life.partner_progress_at = now
                life.partner_best_distance = math.inf
                life.partner_anchor = partner.position
                dx, dy = partner.orientation[:2]
                length = math.hypot(dx, dy)
                life.partner_heading = (dx / length, dy / length) if length > .01 else (1., 0.)
                life.partner_holding = False
        if partner is None:
            return None
        # Only lower-ID bots lead, and humans never follow our virtual roles.
        # This makes mutual moving-goal cycles impossible.
        spacing = 4.0
        sign = 1 if player.player_id % 2 else -1
        # Looking around is not a formation change. Update its direction only
        # after the leader actually travels, so a stationary human cannot make
        # followers orbit by aiming left/right (or vertically).
        if life.partner_anchor is not None:
            dx = partner.position[0] - life.partner_anchor[0]
            dy = partner.position[1] - life.partner_anchor[1]
            length = math.hypot(dx, dy)
            if length >= 2:
                life.partner_heading = (dx / length, dy / length)
                life.partner_anchor = partner.position
        dx, dy = life.partner_heading
        goal = (partner.position[0] - spacing * dx - 2 * sign * dy,
                partner.position[1] - spacing * dy + 2 * sign * dx,
                partner.position[2])
        distance = math.dist(player.position, goal)
        if distance < life.partner_best_distance - .5:
            life.partner_best_distance = distance
            life.partner_progress_at = now
        if life.partner_holding and distance > 3:
            life.partner_holding = False
            life.partner_best_distance = distance
            life.partner_progress_at = now
        elif distance < 2:
            life.partner_holding = True
        if life.partner_holding:
            # Keep one owner when in position. Returning None handed movement
            # back to the objective, then immediately chased the partner again.
            life.partner_progress_at = now
        return TacticalOrder(0, "medic_partner" if int(C.MEDPACK_TOOL) in player.loadout
                             else "join_player_push", goal, partner.eye, arrival_radius=2,
                             hold=life.partner_holding)

    # --- schematic construction -------------------------------------------------

    def request_schematic(self, team: int, schematic: Schematic | Placement | str,
                          anchor: Vector3, now: float, *,
                          facing: tuple[float, float] = (1.0, 0.0),
                          rotation: int | None = None, exact: bool = True,
                          builders: tuple[int, ...] = (), priority: float = 0.95,
                          ttl: float = 90.0, requester: str = "strategy",
                          purpose: str = "") -> SchematicSite | None:
        """Strategy API: have the team's bots build a schematic/structure.

        Returns the registered site (``site.site_id`` for status/cancel) or
        ``None`` when no feasible support-ordered, reachable plan exists.
        """

        return self.sites.request(self.world.solid, int(team), schematic, anchor, float(now),
                                  facing=facing, rotation=rotation, exact=exact,
                                  purpose=purpose, requester=requester or "strategy",
                                  builders=builders, priority=priority, ttl=ttl)

    def cancel_schematic(self, site_id: int) -> None:
        self.sites.cancel(int(site_id))

    def schematic_status(self, site_id: int) -> dict[str, object] | None:
        site = self.sites.sites.get(int(site_id))
        return site.status(self.world.solid) if site is not None else None

    def _site_finished(self, task: _Task) -> bool:
        site = self.sites.sites.get(task.schematic_id)
        return site is not None and bool(site.completed_at)

    @staticmethod
    def _own_vip(frame: PerceptionFrame, player: PlayerSnapshot):
        return next((o for o in frame.objectives if o.kind == "vip" and o.team == player.team), None)

    def _schematic_allowed(self, frame: PerceptionFrame, player: PlayerSnapshot,
                           strategic: ModeBotDecision | None, task: _Task | None = None) -> bool:
        """May this bot do schematic work despite a committed mode role?"""

        if (player.carried_entity_id >= 0 or int(C.BLOCK_TOOL) not in player.loadout
                or player.blocks < 2 or not player.grounded or player.wade):
            return False
        if self.sites.site_for(player.team, player.player_id, player.position,
                               requested_only=True) is not None:
            return True
        if strategic is None:
            return False
        role = strategic.role
        if canonical_mode_id(frame.mode_id) == "vip" and str(frame.mode_phase).lower() in {"active", ""}:
            if role == "vip_rally":
                return True  # the VIP itself, not recently hurt
            if role == "vip_guard_formation":
                vip = self._own_vip(frame, player)
                return vip is not None and math.dist(vip.position, player.position) <= 24
        if (strategic.posture is ModeBotPosture.DEFEND and strategic.directive != "fortify"
                and frame.profile is not None and frame.profile.creativity >= .45):
            # A defender that already holds its post may dig in there, and
            # may walk around that post's site while building it.
            if math.dist(player.position, strategic.position) <= strategic.arrival_radius + 3:
                return True
            site = (self.sites.sites.get(task.schematic_id)
                    if task is not None and task.kind == "schematic" else None)
            return (site is not None and site.active
                    and math.dist(site.centre, strategic.position) <= strategic.arrival_radius + 8)
        return False

    def _vip_shelter_hold(self, frame: PerceptionFrame, player: PlayerSnapshot,
                          strategic: ModeBotDecision | None) -> TacticalOrder | None:
        """A sheltered VIP stays in its finished box while it stands."""

        if strategic is None or strategic.role != "vip_rally":
            return None
        for site in self.sites.sites.values():
            if (site.team != player.team or site.purpose != "vip_shelter"
                    or not site.completed_at or site.plan.placement.occupant is None):
                continue
            spot = site.plan.placement.occupant
            if math.dist(spot, player.position) > 4:
                continue
            inside = (math.floor(player.position[0]), math.floor(player.position[1])) in interior_columns(
                site.plan.placement)
            cells = tuple(site.plan.placement.cells)
            intact = sum(1 for cell in cells if self.world.solid(*cell))
            if intact < .7 * len(cells):
                return None
            look = strategic.watch_position or next(
                (o.position for o in frame.objectives if o.kind == "team_anchor" and o.team != player.team),
                (spot[0] + site.plan.placement.forward[0] * 8, spot[1] + site.plan.placement.forward[1] * 8, spot[2]))
            look = (look[0], look[1], player.eye[2])
            holding = inside or math.dist(spot[:2], player.position[:2]) < .8
            return TacticalOrder(0, "vip_sheltered", spot, look, arrival_radius=.6, hold=holding)
        return None

    def _keep_away(self, frame: PerceptionFrame, player: PlayerSnapshot,
                   ring_centre: Vector3 | None = None) -> tuple[tuple[Vector3, float], ...]:
        points = []
        for objective in frame.objectives[:16]:
            if objective.kind in {"zombie_refuge", "zombie_order", "vip", "last_survivor"}:
                continue
            radius = 9.0 if objective.kind == "team_anchor" else 6.5
            if ring_centre is not None and math.dist(objective.position, ring_centre) < 2:
                radius = 3.0
            points.append((objective.position, radius))
        return tuple(points)

    def _start_site(self, frame: PerceptionFrame, player: PlayerSnapshot, names: tuple[str, ...],
                    centre: Vector3, facing: tuple[float, float], purpose: str, *,
                    exact: bool = False, optional: bool = True, occupant: bool = False,
                    requester: str = "", ring_centre: Vector3 | None = None) -> SchematicSite | None:
        now = frame.created_at
        if not self.sites.can_start(player.team, now, optional=optional, position=centre):
            return None
        bodies = tuple(p.position for p in frame.players[:48] if p.alive and p.spawned
                       and math.dist(p.position, centre) < 20)
        reserved = self._reserved_cells(player.team)
        keep_away = self._keep_away(frame, player, ring_centre)
        # At most two schematic searches per decision keep planning bounded.
        start = (player.player_id + int(now / 7)) % max(1, len(names))
        ordered = names[start:] + names[:start]
        for name in ordered[:2]:
            schematic = get_schematic(name)
            if schematic is None or schematic.block_count > player.blocks * 2 + 40:
                continue
            choice = find_schematic_site(self.world.solid, schematic, centre, facing,
                                         exact=exact, keep_away=keep_away, reserved=reserved,
                                         bodies=bodies, occupant_body=player.position if occupant else None,
                                         seed=player.player_id)
            if choice is None:
                continue
            site = self.sites.create(player.team, choice.plan, purpose, identity(player), now,
                                     optional=optional, requester=requester,
                                     occupant=identity(player) if occupant else None)
            if site is not None:
                self.teams.event("schematic_sites_started", site.site_id, site.name, now)
                return site
        if optional:
            # A failed search still spends the team's optional budget briefly.
            self.sites.team_ready[player.team] = max(self.sites.team_ready.get(player.team, 0.0), now + 6.0)
        return None

    def _schematic_task(self, frame: PerceptionFrame, player: PlayerSnapshot, life: _Life,
                        strategic: ModeBotDecision | None, allies: tuple[PlayerSnapshot, ...], *,
                        critical: bool, lane: Vector3 | None) -> _Task | None:
        """Join a team site, or start a mode-appropriate one (budgeted)."""

        if int(C.BLOCK_TOOL) not in player.loadout or player.blocks < 2:
            return None
        now = frame.created_at
        profile = frame.profile
        teamwork = profile.teamwork if profile else .5
        creativity = profile.creativity if profile else .5
        role = strategic.role if strategic is not None else ""
        site = self.sites.site_for(player.team, player.player_id, player.position, requested_only=True)
        score = .92
        vip_mode = canonical_mode_id(frame.mode_id) == "vip"
        if site is None and vip_mode and role in {"vip_rally", "vip_guard_formation"}:
            shelter = next((s for s in self.sites.active_sites(player.team) if s.purpose == "vip_shelter"), None)
            if role == "vip_rally":
                calm = player.last_damage_at <= 0 or now - player.last_damage_at > 6
                if (shelter is None and calm and player.blocks >= 12
                        and self._vip_can_shelter(frame, player)):
                    facing = self._threat_facing(frame, player, strategic)
                    shelter = self._start_site(frame, player, ("vip_shelter",), player.position,
                                               facing, "vip_shelter", exact=True, optional=False,
                                               occupant=True)
                site = shelter if shelter is not None and math.dist(shelter.centre, player.position) <= 6 else None
            else:
                site = shelter
                if site is None:
                    done = next((s for s in self.sites.sites.values() if s.team == player.team
                                 and s.purpose == "vip_shelter" and s.completed_at), None)
                    barriers = sum(1 for s in self.sites.sites.values() if s.team == player.team
                                   and s.purpose == "vip_barrier")
                    if done is not None and barriers < 2 and math.dist(done.centre, player.position) <= 16:
                        fx, fy = done.plan.placement.forward
                        side = 1 if barriers == 0 else -1
                        centre = (done.centre[0] + fx * 5 - fy * 3 * side,
                                  done.centre[1] + fy * 5 + fx * 3 * side, done.centre[2])
                        site = self._start_site(frame, player, ("sandbag_wall", "corner_cover"),
                                                centre, (fx, fy), "vip_barrier", optional=False)
            score = .95
        if site is None and strategic is not None and critical and strategic.posture is ModeBotPosture.DEFEND:
            site = self.sites.site_for(player.team, player.player_id, player.position)
            if site is None and creativity >= .45 and player.blocks >= 20:
                facing = self._threat_facing(frame, player, strategic)
                objective = next((o for o in frame.objectives if o.team == player.team
                                  and o.kind in {"ctf_intel", "mh_hill", "tc_territory", "oc"}
                                  and math.dist(o.position, strategic.position) < 4), None)
                if objective is not None and (player.player_id + int(now / 30)) % 2 == 0:
                    site = self._start_site(frame, player, _OBJECTIVE_SCHEMATICS[:1], objective.position,
                                            facing, "objective", exact=True,
                                            ring_centre=objective.position)
                if site is None:
                    ahead = (player.position[0] + facing[0] * 3, player.position[1] + facing[1] * 3,
                             player.position[2])
                    site = self._start_site(frame, player, _DEFENSIVE_SCHEMATICS, ahead, facing, "defend")
            score = .9
        if site is None and not critical:
            if teamwork >= .4:
                site = self.sites.site_for(player.team, player.player_id, player.position)
                score = .78 + teamwork * .1
            holding = strategic is None or math.dist(player.position, strategic.position) <= strategic.arrival_radius + 4
            threat = life.memory.contact(player.position, now) is not None or player.last_damage_at > 0 and now - player.last_damage_at < 30
            if (site is None and lane is not None and creativity > .55 and player.blocks >= 30
                    and holding and threat and self.sites.optional_started(player.team) < 6
                    and frame.local_safety_complete and life.memory.pressure < .1
                    and _mix(player.player_id, int(now / 20)) < .3):
                facing = self._facing_to(player.position, lane)
                has_sniper = any(tool in player.loadout for tool in (int(C.SNIPER_TOOL), int(C.SNIPER2_TOOL)))
                names = _SNIPER_SCHEMATICS if has_sniper else _GENERIC_SCHEMATICS
                ahead = (player.position[0] + facing[0] * 3, player.position[1] + facing[1] * 3,
                         player.position[2])
                if not self._near_objective(frame, ahead, 10):
                    site = self._start_site(frame, player, names, ahead, facing,
                                            "overwatch" if has_sniper else "cover")
                score = .9
        if site is None or not site.active:
            return None
        lane = lane or (site.centre[0] + site.plan.placement.forward[0] * 10,
                        site.centre[1] + site.plan.placement.forward[1] * 10, player.eye[2])
        task = _Task(frame.frame_id * 256 + player.player_id, "schematic", site.centre, lane, now,
                     now + 30, score, progress_at=now, schematic_id=site.site_id)
        site.builders[identity(player)] = now
        return task

    def _vip_can_shelter(self, frame: PerceptionFrame, player: PlayerSnapshot) -> bool:
        """Box in only near help or home and away from spawn protection."""

        if any(o.kind == "team_anchor" and math.dist(o.position, player.position) < 9
               for o in frame.objectives):
            return False
        helpers = sum(1 for p in frame.players if p.team == player.team and p.alive and p.spawned
                      and p.player_id != player.player_id and math.dist(p.position, player.position) <= 14)
        return helpers >= 1

    @staticmethod
    def _facing_to(origin: Vector3, target: Vector3) -> tuple[float, float]:
        dx, dy = target[0] - origin[0], target[1] - origin[1]
        length = math.hypot(dx, dy)
        return (dx / length, dy / length) if length > 1e-6 else (1.0, 0.0)

    def _threat_facing(self, frame: PerceptionFrame, player: PlayerSnapshot,
                       strategic: ModeBotDecision | None) -> tuple[float, float]:
        threat = strategic.watch_position if strategic is not None else None
        if threat is None:
            threat = next((o.position for o in frame.objectives
                           if o.kind == "team_anchor" and o.team != player.team), None)
        if threat is None and player.last_damage_source_position is not None:
            threat = player.last_damage_source_position
        if threat is None:
            return (player.orientation[0], player.orientation[1]) if any(player.orientation[:2]) else (1.0, 0.0)
        return self._facing_to(player.position, threat)

    def _advance_schematic(self, frame: PerceptionFrame, player: PlayerSnapshot, life: _Life,
                           allies: tuple[PlayerSnapshot, ...]) -> TacticalOrder | None:
        task = life.task
        assert task is not None
        now = frame.created_at
        site = self.sites.sites.get(task.schematic_id)
        if site is None or site.abandoned:
            self._finish(life, now, False, "site_" + (site.abandoned if site else "missing"))
            return None
        me = identity(player)
        if site.completed_at:
            spot = site.plan.placement.occupant
            if (site.purpose == "overwatch" and spot is not None
                    and site.occupant in (None, me) and now - site.completed_at < 25):
                # Use what was built: climb the stair to the platform/step
                # and watch the lane from it for a while.
                if site.occupant is None:
                    site.occupant = me
                    task.best_distance, task.progress_at = math.inf, now
                if math.dist(spot, player.position) > .9:
                    # Lead the climb one tread at a time along the walkable
                    # stand graph of what was built (stairs, steps); progress
                    # is the remaining walk, not straight-line distance.
                    goal = spot
                    remaining = math.dist(spot, player.position) + 50
                    start = node_for_position(self.world.solid, player.position)
                    target = node_for_position(self.world.solid, spot)
                    region = site.region
                    if start is not None and target is not None and region is not None:
                        path = walk_path(self.world.solid, region, start, target)
                        if path:
                            remaining = float(len(path))
                            ground = site.plan.placement.ground_z
                            first_up = next((i for i, node in enumerate(path) if node[2] < ground), None)
                            if start[2] >= ground and first_up:
                                # Ordinary navigation brings it to the foot of
                                # the stair; then one tread per order.
                                base = path[first_up - 1]
                                ahead = base if base[:2] != start[:2] else path[first_up]
                            else:
                                ahead = path[0]
                                if len(path) > 1 and path[1][2] == ahead[2] == start[2]:
                                    ahead = path[1]
                            goal = node_position(ahead)
                    if remaining < task.best_distance - .5:
                        task.best_distance, task.progress_at = remaining, now
                    if now - task.progress_at > 14:
                        self._finish(life, now, False, "occupy_stalled")
                        return None
                    order = TacticalOrder(task.task_id, "schematic_climb", goal, task.lane,
                                          arrival_radius=.4)
                    life.climb_order = order
                    return order
                task.progress_at = now
                return TacticalOrder(task.task_id, "schematic_overwatch", spot, task.lane, hold=True)
            self._finish(life, now, True, "schematic_complete")
            life.next_project = now + 4.0
            return None
        site.builders[me] = now
        solid = self.world.solid
        step = site.plan.steps[task.step_index] if 0 <= task.step_index < len(site.plan.steps) else None
        if step is not None and (task.step_index in site.done or not step_remaining(solid, step)):
            self.sites.release(site, task.step_index, me)
            task.step_index, step = -1, None
        occupant = site.occupant == me
        if step is None:
            if now < task.ready_at:
                return self._schematic_hold(task, player, site, None)
            others = tuple(p.position for p in frame.players[:48] if p.alive and p.spawned
                           and p.player_id != player.player_id
                           and math.dist(p.position, site.centre) < 24)
            claim = self.sites.claim(site, me, player.position, solid, now, others=others,
                                     prefer_inside=occupant, seed=player.player_id * 31 + task.next_action)
            if claim is None:
                if not task.waiting_since:
                    task.waiting_since = now
                # Nothing ready for me: steps are claimed or wait on support
                # from a teammate's line. Hold briefly, then leave the site.
                if now - task.waiting_since > (12.0 if occupant else 6.0):
                    self._finish(life, now, False, "no_open_step")
                    life.next_project = now + 5.0
                    return None
                return self._schematic_hold(task, player, site, None)
            task.waiting_since = 0.0
            step, node = claim
            task.step_index = step.index
            task.stand = node_position(node)
            task.best_distance = math.inf
            task.progress_at = now
            task.expires_at = max(task.expires_at, now + 20)
        assert task.stand is not None
        stand = task.stand
        horizontal = math.hypot(player.position[0] - stand[0], player.position[1] - stand[1])
        distance = math.hypot(horizontal, player.position[2] - stand[2])
        if distance < task.best_distance - .3:
            task.best_distance, task.progress_at = distance, now
        remaining = step_remaining(solid, step)
        near = horizontal <= .75 and abs(player.position[2] - stand[2]) <= .7
        usable = (near and not self._body_in(player, remaining)
                  and all(math.dist(player.eye, cell_centre(cell)) <= PLAN_REACH + .75
                          for cell in (step.start, step.end))
                  and ray_clear(solid, player.eye, step.start, frozenset(step.cells)))
        if not usable:
            if near and now - task.progress_at > 3:
                # At the stand but straddling a planned cell or without a
                # clear view: hand the step back and pick another.
                return self._schematic_step_result(life, task, False, "stand_blocked", now)
            if now - task.progress_at > 8:
                return self._schematic_step_result(life, task, False, "approach_stalled", now)
            radius = .3 if horizontal <= .6 else .35
            order = TacticalOrder(task.task_id, "schematic_approach", stand,
                                  (step.start[0] + .5, step.start[1] + .5, step.start[2] + .5),
                                  arrival_radius=radius)
            life.climb_order = order
            return order
        self.sites.extend_claim(site, task.step_index, me, now)
        look = (step.start[0] + .5, step.start[1] + .5, step.start[2] + .5)
        if player.tool != int(C.BLOCK_TOOL) and task.ready_at <= now - 3.0:
            # Switch to the block tool like a player: a short, varied delay
            # before the first drag (scaled by reaction time).
            reaction = frame.profile.reaction_time if frame.profile else .3
            task.ready_at = now + .22 + .3 * reaction + _mix(player.player_id, task.next_action, 7) * .25
        if now < task.ready_at:
            return TacticalOrder(task.task_id, "schematic_ready", player.position, look,
                                 hold=True, tool_id=int(C.BLOCK_TOOL))
        if not remaining:
            task.step_index = -1
            return None
        if not self.teams.allow_mutation(player.team, now, len(remaining)):
            return TacticalOrder(task.task_id, "schematic_budget_wait", player.position, look,
                                 hold=True, tool_id=int(C.BLOCK_TOOL))
        rgb = PALETTE.get(step.color)
        argument = f"rgb:{rgb:06x}" if rgb is not None else ""
        # Aim just inside the voxel centre; the gateway rounds to the cell.
        aim = (step.start[0] + .49, step.start[1] + .49, step.start[2] + .49)
        if step.is_line:
            action = BotAction(BotActionKind.BUILD_LINE, int(C.BLOCK_TOOL), position=aim,
                               end_position=(step.end[0] + .49, step.end[1] + .49, step.end[2] + .49),
                               argument=argument)
        else:
            action = BotAction(BotActionKind.BUILD, int(C.BLOCK_TOOL), position=aim, argument=argument)
        action_site = ProjectSite("schematic", aim, stand, (), cells=remaining,
                                  required_blocks=len(remaining), tool_id=int(C.BLOCK_TOOL))
        return self._action(frame, player, task, action, action_site)

    @staticmethod
    def _body_in(player: PlayerSnapshot, cells: tuple[tuple[int, int, int], ...]) -> bool:
        """Mirror the authority's body test (0.45 footprint, three cells tall)."""

        if not cells:
            return False
        px, py = player.position[0], player.position[1]
        top = math.floor(player.position[2])
        columns = {(x, y) for x in range(math.floor(px - .45), math.floor(px + .45) + 1)
                   for y in range(math.floor(py - .45), math.floor(py + .45) + 1)}
        return any((x, y) in columns and top <= z <= top + 2 for x, y, z in cells)

    def _schematic_hold(self, task: _Task, player: PlayerSnapshot, site: SchematicSite,
                        step) -> TacticalOrder:
        centre = site.centre
        look = (centre[0] + site.plan.placement.forward[0] * 6,
                centre[1] + site.plan.placement.forward[1] * 6, player.eye[2])
        if site.occupant == identity(player) and site.plan.placement.occupant is not None:
            spot = site.plan.placement.occupant
            inside = (math.floor(player.position[0]), math.floor(player.position[1])) in interior_columns(
                site.plan.placement)
            if not inside and math.dist(spot, player.position) > .8:
                return TacticalOrder(task.task_id, "schematic_occupy", spot, look, arrival_radius=.5)
        return TacticalOrder(task.task_id, "schematic_wait", player.position, look,
                             hold=True, tool_id=int(C.BLOCK_TOOL))

    def _schematic_step_result(self, life: _Life, task: _Task, success: bool, reason: str,
                               now: float) -> TacticalOrder | None:
        site = self.sites.sites.get(task.schematic_id)
        index = task.step_index
        placed = len(task.action_site.cells) if task.action_site and task.action_site.cells else 0
        if site is not None and index >= 0:
            if success:
                self.sites.step_built(site, index, life.key, placed, now)
            else:
                self.sites.step_failed(site, index, life.key, reason, now)
        self.teams.event("schematic_steps_" + ("built" if success else "failed"),
                         task.task_id, reason, now)
        task.pending = None
        task.action_site = None
        task.step_index = -1
        task.stage = TaskStage.APPROACH
        task.progress_at = now
        task.expires_at = max(task.expires_at, now + 25)
        # A human pauses between drags; skilled builders less.
        jitter = _mix(life.key[0], task.next_action, int(now * 10))
        task.ready_at = now + (.18 + .3 * jitter if success else .8 + .4 * jitter)
        if not success:
            task.remaining_cells = max(0, task.remaining_cells) + 1
            if task.remaining_cells >= 3:
                self._finish(life, now, False, "schematic_" + reason)
                life.next_project = now + 6.0
        return None

    def _expire(self, frame: PerceptionFrame, now: float) -> None:
        observed = {(p.player_id, p.generation): (p.alive and p.spawned, p.life_id) for p in frame.players}
        self.teams.expire(now, observed)
        self.sites.refresh(self.world.solid, now, {key: life for key, (alive, life) in observed.items()
                                                   if alive})
        for key, life in tuple(self.lives.items()):
            if now - life.last_seen > 15 or key in observed and observed[key] != (True, life.key[2]):
                self.forget(*key)
        while len(self.lives) > 128:
            self.forget(*min(self.lives, key=lambda key: self.lives[key].last_seen))
        self.patients = {key: value for key, value in self.patients.items() if value[1] > now}

    def _reserved_cells(self, team: int, *, exclude: int = -1) -> frozenset[tuple[int, int, int]]:
        cells = frozenset(cell for p in self.teams.projects.values()
                          if p.team == team and p.project_id != exclude for cell in p.cells)
        sites = self.sites.reserved_cells(team)
        return cells | sites if sites else cells

    @staticmethod
    def _near_objective(frame: PerceptionFrame, position: Vector3, radius: float) -> bool:
        return any(math.dist(position, objective.position) < radius
                   for objective in frame.objectives
                   if objective.kind != "zombie_refuge")

    @staticmethod
    def _refuge(frame: PerceptionFrame, player: PlayerSnapshot) -> Vector3 | None:
        """The team's elected Zombie refuge, the only place ramparts go up."""
        return next((o.position for o in frame.objectives
                     if o.kind == "zombie_refuge" and o.team == player.team), None)

    @staticmethod
    def _fortify_lane(frame: PerceptionFrame, player: PlayerSnapshot,
                      strategic: ModeBotDecision) -> Vector3:
        """Face the horde's side of the refuge when nothing is in sight."""
        threat = strategic.watch_position
        if threat is None:
            threat = next((o.position for o in frame.objectives
                           if o.kind == "team_anchor" and o.team != player.team), None)
        if threat is None:
            return strategic.position
        dx, dy = threat[0] - player.position[0], threat[1] - player.position[1]
        length = math.hypot(dx, dy)
        if length < 1e-6:
            return strategic.position
        return (player.position[0] + dx / length * 12.0,
                player.position[1] + dy / length * 12.0, player.position[2])

    @staticmethod
    def _live_hazard(frame: PerceptionFrame, player: PlayerSnapshot) -> bool:
        # Friendly devices and visible/nearby moving explosions only; do not
        # reveal static enemy mines from the broad replication registry.
        return any(e.alive and e.hazardous and
                   (e.team == player.team or e.kind == "projectile")
                   and math.dist(e.position, player.position) < e.blast_radius + 3
                   for e in frame.entities[:64])
