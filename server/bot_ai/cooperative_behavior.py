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
from .policies import ModeBotDecision, mode_objective_committed
from .project_sites import (
    ProjectSite, find_bridge_project, find_decorative_site,
    find_mine_approach, find_prefab_cover, find_sniper_outpost,
)
from .simple_navigation import SimpleVoxelWorld
from .team_tasks import Identity, TacticalOrder, TaskStage, TeamProject, TeamTasks


def identity(player: PlayerSnapshot) -> Identity:
    return player.player_id, player.generation, player.life_id


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


class CooperativeBehavior:
    """One shared worker coordinator, with small per-life and per-team state."""

    def __init__(self, world: SimpleVoxelWorld) -> None:
        self.world = world
        self.epoch: tuple[int, int] = (-1, -1)
        self.lives: dict[tuple[int, int], _Life] = {}
        self.teams = TeamTasks()
        self.patients: dict[Identity, tuple[Identity, float]] = {}

    def reset(self) -> None:
        self.lives.clear()
        self.patients.clear()
        self.teams = TeamTasks()

    def forget(self, player_id: int, generation: int) -> None:
        life = self.lives.pop((player_id, generation), None)
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
            self.reset()
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
            return None
        allies = tuple(p for p in frame.players if p.alive and p.spawned and p.team == player.team)
        # Role urgency is not permission for optional construction/formation
        # to replace the mode's actual winning job.
        critical = player.carried_entity_id >= 0 or mode_objective_committed(strategic)
        combat_visible = visible if self._combat_relevant(frame, player, visible) else None
        danger = visible is not None and math.dist(player.position, visible.position) < 10
        defending = bool(life.task and life.task.kind in {"outpost", "cover", "strongpoint"}
            and life.task.phase == "occupy" and life.task.stage is TaskStage.USE
            and life.task.site and math.dist(player.position, life.task.site.approach) <= 1.5)
        combat_interrupt = (combat_visible is not None and life.task is not None
                            and life.task.kind != "heal" and not defending)
        objective_support = bool(critical and life.task and strategic is not None
            and self._supports_objective(life.task, player, strategic, now))
        if life.task and (critical and not objective_support or danger and life.task.kind != "heal"
                          or combat_interrupt or self._live_hazard(frame, player)):
            self._finish(life, now, False, "combat_contact" if combat_interrupt else "urgent_interrupt")
        if critical:
            # Retiring the ownership is essential: otherwise a completed
            # objective resumes an obsolete human-follow lease immediately.
            life.partner = None
            life.partner_anchor = None
            life.partner_holding = False
            life.partner_until = 0.0
            if life.task is not None:
                return self._advance(frame, player, combat_visible, life, allies)
            if (strategic is not None and self._can_stop_for_supplies(strategic)
                    and combat_visible is None and now >= life.next_evaluate
                    and not self._live_hazard(frame, player)
                    and (player.health < 55 or player.ammo_clip + player.ammo_reserve == 0)):
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
            if has_sniper and math.dist(player.position, lane) > 18:
                site = find_sniper_outpost(self.world, player, lane, friendly_positions=friends)
                kind = "outpost"
            elif has_miner and combat_visible is None:
                site = find_bridge_project(self.world, player, lane, reserved_cells=reserved)
                kind = "bridge"
                if site is None:
                    from .project_sites import find_breach_project
                    site = find_breach_project(self.world, player, lane)
                    kind = "breach"
            elif player.blocks >= 6 and (combat_visible is None or player.reloading) and creativity > .35:
                site = find_prefab_cover(self.world, player, lane,
                    friendly_positions=friends, reserved_cells=reserved)
                kind = "strongpoint" if int(C.ROCKET_TURRET_TOOL) in player.loadout else "cover"
            if site is not None and not self._near_objective(frame, site.position, 10):
                score = .66 + creativity * .18 + teamwork * .08
                score -= life.memory.penalty(kind, site.position, now)
                if score > .35:
                    patience = 24 + 48 * (profile.caution if profile else .5)
                    candidates.append(_Task(task_id, kind, site.approach, lane, now,
                        now + (patience if kind == "outpost" else 25), score, site=site,
                        progress_at=now))
        if visible is None and contact is not None and math.dist(player.position, contact.position) > 4:
            score = .4 + creativity * .1 - life.memory.penalty("investigate", contact.position, now)
            if score > .2:
                candidates.append(_Task(task_id, "investigate", contact.position, contact.position,
                    now, min(contact.expires_at, now + 6), score, progress_at=now))
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
    def _can_stop_for_supplies(strategic: ModeBotDecision) -> bool:
        return not strategic.role.endswith("_passive") and strategic.role not in {
            "demolition_escape_airstrike", "occupation_dispose_bomb",
            "zombie_last_survivor_escape", "vip_retreat",
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
        if player.ammo_clip + player.ammo_reserve < 6 or depleted_medical:
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
            if task.kind == "heal":
                task.phase = "use_pack"
            elif task.kind == "outpost":
                task.phase = "security" if task.phase == "cover" else "occupy"
            elif task.kind == "strongpoint" and task.phase == "prepare":
                task.phase = "security"
            else:
                task.phase = "occupy"
            task.stage = TaskStage.USE
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

    def _expire(self, frame: PerceptionFrame, now: float) -> None:
        observed = {(p.player_id, p.generation): (p.alive and p.spawned, p.life_id) for p in frame.players}
        self.teams.expire(now, observed)
        for key, life in tuple(self.lives.items()):
            if now - life.last_seen > 15 or key in observed and observed[key] != (True, life.key[2]):
                self.forget(*key)
        while len(self.lives) > 128:
            self.forget(*min(self.lives, key=lambda key: self.lives[key].last_seen))
        self.patients = {key: value for key, value in self.patients.items() if value[1] > now}

    def _reserved_cells(self, team: int, *, exclude: int = -1) -> frozenset[tuple[int, int, int]]:
        return frozenset(cell for p in self.teams.projects.values()
                         if p.team == team and p.project_id != exclude for cell in p.cells)

    @staticmethod
    def _near_objective(frame: PerceptionFrame, position: Vector3, radius: float) -> bool:
        return any(math.dist(position, objective.position) < radius for objective in frame.objectives)

    @staticmethod
    def _live_hazard(frame: PerceptionFrame, player: PlayerSnapshot) -> bool:
        # Friendly devices and visible/nearby moving explosions only; do not
        # reveal static enemy mines from the broad replication registry.
        return any(e.alive and e.hazardous and
                   (e.team == player.team or e.kind == "projectile")
                   and math.dist(e.position, player.position) < e.blast_radius + 3
                   for e in frame.entities[:64])
