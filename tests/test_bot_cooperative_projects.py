"""Cooperative projects across real voxel geometry and ordinary action services.

Movement completion is supplied as authoritative snapshots after validating a
production route. This is a deterministic task-lifecycle gate, not a native
physics soak. Prefabs and security devices use the real bot gateway/services.
"""
from __future__ import annotations

from dataclasses import replace
import math
from types import SimpleNamespace

import pytest
import shared.constants as C

from server.bot_ai.cooperative_behavior import CooperativeBehavior
from server.bot_ai.gateway import BotActionGateway
from server.bot_ai.messages import BotActionKind, EntitySnapshot, PerceptionFrame
from server.bot_ai.policies import ModeBotDecision
from server.bot_ai.prefab_policy import load_bot_prefab_geometry
from server.bot_ai.team_tasks import TaskStage
from server.config import ServerConfig
from server.deployable_actions import DeployableActionService
from server.entities.registry import EntityRegistry
from server.prefab_actions import PrefabActionService
from tests.test_bot_project_sites import make_world, player


LANE = (40.5, 20.5, 17.75)


def frame(bot, now=100., *others, entities=(), epoch=1):
    return PerceptionFrame(round(now * 10), epoch, epoch, 1, bot.player_id,
        bot.generation, now, "tdm", (bot, *others), entities=entities,
        behavior_version="cooperative", friendly_mischief=False)


def move(bot, position):
    offset = tuple(bot.eye[a] - bot.position[a] for a in range(3))
    return replace(bot, position=position, eye=tuple(position[a] + offset[a] for a in range(3)))


def decide(coordinator, bot, now=100., *others, entities=(), strategic=True):
    return coordinator.decide(frame(bot, now, *others, entities=entities), bot, None,
                               ModeBotDecision(LANE, "known_lane") if strategic else None)


class ProjectAuthority:
    """Minimal composition root with real prefab/deployable authorities."""

    def __init__(self, world, snapshot, monkeypatch):
        self.world = world
        self.remote_packets = []
        self.owner_packets = []
        self.owner = SimpleNamespace(id=snapshot.player_id, team=snapshot.team,
            name="CooperativeProject", class_id=snapshot.class_id,
            is_bot=True, alive=True, spawned=True, tool=snapshot.tool, tool_is_raw=True,
            blocks=snapshot.blocks, prefabs=list(snapshot.prefabs), loadout=list(snapshot.loadout),
            block_color=0xAABBCC, deployable_stock=dict(snapshot.deployable_stock),
            _deployable_next_use={}, send=lambda data, **_kw: self.owner_packets.append(data))
        self.owner.set_tool = lambda tool, raw=True: setattr(self.owner, "tool", tool)
        self.server = SimpleNamespace(world_manager=world._vxl, players={snapshot.player_id: self.owner},
            loop_count=100, config=ServerConfig(), entity_registry=EntityRegistry(),
            teams={snapshot.team: SimpleNamespace(color=(10, 20, 30), infinite_blocks=False)},
            broadcast=lambda data, **_kw: self.remote_packets.append(data),
            broadcast_create_entity=lambda _entity: None, _radar_station_added=lambda _team: None)
        self.server.prefab_actions = PrefabActionService(self.server)
        self.server.deployable_actions = DeployableActionService(self.server)
        self.gateway = BotActionGateway(self.server)
        monkeypatch.setattr("server.prefab_actions.play_sound", lambda *_a, **_kw: None)

    def execute(self, snapshot, order):
        self.owner.x, self.owner.y, self.owner.z = snapshot.position
        assert self.gateway.execute(self.owner, order.action)
        return replace(snapshot, blocks=self.owner.blocks,
            deployable_stock=tuple(sorted(self.owner.deployable_stock.items())),
            last_action_kind=order.action.kind.value, last_action_accepted=True,
            last_action_request_id=order.action.request_id, last_task_accepted=True)

    def entities(self):
        tool_for_type = {int(C.LANDMINE_ENTITY): int(C.LANDMINE_TOOL),
                         int(C.RADAR_STATION_ENTITY): int(C.RADAR_STATION_TOOL)}
        return tuple(EntitySnapshot(entity.entity_id, entity.type, self.owner.team,
            entity.player_id, (entity.x, entity.y, entity.z), tool_id=tool_for_type[entity.type])
            for entity in self.server.entity_registry.all())


def setup_outpost(*, security=int(C.LANDMINE_TOOL), stock=1):
    world = make_world(thick=True)
    world.prefab_geometry = {shape.name: shape for shape in load_bot_prefab_geometry(("prefab_small_wall",))}
    bot = player(loadout=(int(C.SNIPER_TOOL), int(C.PREFAB_TOOL), int(C.BLOCK_TOOL), security),
                 deployable_stock=((security, stock),))
    return world, bot, CooperativeBehavior(world)


@pytest.mark.parametrize("security", [int(C.LANDMINE_TOOL), int(C.RADAR_STATION_TOOL)])
def test_outpost_cover_then_equipped_security_then_occupation(security, monkeypatch):
    world, bot, coordinator = setup_outpost(security=security)
    authority = ProjectAuthority(world, bot, monkeypatch)
    before = set(world._vxl.solids)
    cover = decide(coordinator, bot)
    assert cover.action.kind is BotActionKind.PLACE_PREFAB
    bot = authority.execute(bot, cover)
    assert len(world._vxl.solids - before) == 6 and bot.blocks == 94
    security_order = decide(coordinator, bot, 100.2)
    if security == int(C.LANDMINE_TOOL):
        assert security_order.role == "secure_outpost_approach"
        route = world.plan(bot.position, security_order.goal, abilities=frozenset())
        assert route.reached_segment_goal
        bot = move(bot, security_order.goal)
        security_order = decide(coordinator, bot, 101.)
    assert security_order.action.kind is BotActionKind.DEPLOY
    assert security_order.action.tool_id == security
    assert math.dist(bot.position, security_order.action.position) <= (
        float(C.LANDMINE_FAR_RADIUS) if security == int(C.LANDMINE_TOOL) else float(C.RADAR_STATION_FAR_RADIUS))
    bot = authority.execute(bot, security_order)
    assert dict(bot.deployable_stock)[security] == 0
    order = decide(coordinator, bot, 101.2, entities=authority.entities())
    if not order.hold:
        assert order.goal == (20.5, 20.5, 17.75)
        assert world.plan(bot.position, order.goal, abilities=frozenset()).reached_segment_goal
        bot = move(bot, order.goal)
        order = decide(coordinator, bot, 102., entities=authority.entities())
    assert order.role == "outpost_watch" and order.hold
    assert order.action.kind is BotActionKind.NONE
    assert len(authority.entities()) == 1
    assert coordinator.teams.metrics["actions_confirmed"] == 2


def test_outpost_with_empty_security_stock_occupies_without_free_deploy(monkeypatch):
    world, bot, coordinator = setup_outpost(stock=0)
    authority = ProjectAuthority(world, bot, monkeypatch)
    bot = authority.execute(bot, decide(coordinator, bot))
    order = decide(coordinator, bot, 100.2)
    assert order.role == "outpost_watch" and order.action.kind is BotActionKind.NONE
    assert not authority.entities()


def test_human_removes_confirmed_cover_and_project_reacts_without_rebuilding(monkeypatch):
    world, bot, coordinator = setup_outpost(stock=0)
    authority = ProjectAuthority(world, bot, monkeypatch)
    before = set(world._vxl.solids)
    bot = authority.execute(bot, decide(coordinator, bot))
    created = world._vxl.solids - before
    watch = decide(coordinator, bot, 100.2)
    assert watch.role == "outpost_watch"
    world._vxl.solids.difference_update(created)
    result = decide(coordinator, bot, 101.)
    assert result is None or result.task_id != watch.task_id
    assert coordinator.teams.metrics["tasks_failed"] == 1
    assert world._vxl.solids == before


def test_changed_prefab_choice_cancels_pending_old_construct():
    world, bot, coordinator = setup_outpost()
    pending = decide(coordinator, bot)
    assert pending.action.argument == "prefab_small_wall"
    changed = replace(bot, prefabs=("prefab_caltrop",))
    result = decide(coordinator, changed, 100.2, strategic=False)
    assert result is None or result.task_id != pending.task_id
    assert not coordinator.teams.projects


def test_rejected_action_is_not_retried_and_death_clears_team_reservation():
    world, bot, coordinator = setup_outpost()
    pending = decide(coordinator, bot)
    rejected = replace(bot, last_action_request_id=pending.action.request_id,
        last_action_accepted=False, last_task_accepted=False,
        last_action_reason="spawn or objective zone")
    assert decide(coordinator, rejected, 100.2) is None
    assert not coordinator.teams.projects and coordinator.teams.metrics["tasks_failed"] == 1
    assert decide(coordinator, rejected, 101.) is None
    # Another life may plan later; observing its death releases the worksite.
    alive = replace(bot, life_id=1)
    assert decide(coordinator, alive, 103.) is not None
    partner = replace(player(), player_id=2, blocks=0, loadout=(int(C.SMG_TOOL),), prefabs=())
    decide(coordinator, partner, 103.2, replace(alive, alive=False), strategic=False)
    assert not coordinator.teams.projects


def setup_crossing(kind):
    world = make_world()
    bot = player(class_id=int(C.CLASS_MINER),
        loadout=(int(C.SUPERSPADE_TOOL), int(C.BLOCK_TOOL)), prefabs=(), deployable_stock=())
    if kind == "bridge":
        world._vxl.solids.difference_update((x, y, 20) for x in range(21, 25) for y in range(5, 36))
    else:
        world._vxl.solids.update((x, y, z) for x in (23, 24) for y in range(18, 23) for z in range(16, 20))
    partner = replace(player(), player_id=2, position=(17.5, 23.5, 17.75), eye=(17.5, 23.5, 17.75),
                       blocks=0, loadout=(int(C.SMG_TOOL),), prefabs=(), deployable_stock=())
    return world, bot, partner, CooperativeBehavior(world)


@pytest.mark.parametrize("kind", ["bridge", "breach"])
def test_crossing_requires_committed_geometry_then_builder_and_partner_use(kind):
    world, bot, partner, coordinator = setup_crossing(kind)
    first = decide(coordinator, bot, 100., partner)
    task = coordinator.lives[(bot.player_id, bot.generation)].task
    assert task.kind == kind and task.site.landing is not None
    site, task_id = task.site, task.task_id
    before = set(world._vxl.solids)
    guarding = decide(coordinator, partner, 100.2, bot, strategic=False)
    assert guarding.role == "cover_teammate"
    if kind == "bridge":
        assert first.action.kind is BotActionKind.BUILD_LINE
        waiting = decide(coordinator, bot, 100.3, partner)
        assert waiting.role == "bridge_confirm" and world._vxl.solids == before
        world._vxl.solids.update(site.cells)
        assert decide(coordinator, bot, 100.4, partner).role == "bridge_confirm"
        bot = replace(bot, last_action_request_id=first.action.request_id, last_task_accepted=True)
    else:
        assert first.role == "squad_breach"
        world._vxl.solids.difference_update(site.cells)
    crossing = decide(coordinator, bot, 100.5, partner)
    assert crossing.goal == site.landing
    assert world.plan(bot.position, crossing.goal, abilities=frozenset()).reached_segment_goal
    assert coordinator.teams.metrics["tasks_completed"] == 0
    # A participant can catch up before the builder finishes. USE must point
    # to the far bank immediately, never count reaching the original worksite.
    project = coordinator.teams.projects[task_id]
    assert project.stage is TaskStage.USE and project.position == site.landing
    near_partner = move(partner, site.approach)
    premature = decide(coordinator, near_partner, 100.6, bot, strategic=False)
    assert premature.role == "squad_advance" and premature.goal == site.landing
    assert coordinator.teams.metrics["routes_used_by_partner"] == 0
    # The former 1.75/2-block arrival radii ended ownership on the last bridge
    # cell, allowing unrelated navigation to pull a follower into the void.
    near_landing = (site.landing[0] - 1, site.landing[1], site.landing[2])
    decide(coordinator, move(bot, near_landing), 100.7, partner)
    decide(coordinator, move(partner, near_landing), 100.8, bot, strategic=False)
    assert coordinator.teams.metrics["tasks_completed"] == 0
    assert coordinator.teams.metrics["routes_used_by_partner"] == 0
    bot = move(bot, site.landing)
    decide(coordinator, bot, 101., partner)
    assert coordinator.teams.metrics["tasks_completed"] == 1
    project = coordinator.teams.projects[task_id]
    assert project.stage is TaskStage.USE and project.position == site.landing
    following = decide(coordinator, partner, 101.2, bot, strategic=False)
    assert following.role == "squad_advance" and following.goal == site.landing
    assert world.plan(partner.position, following.goal, abilities=frozenset()).reached_segment_goal
    partner = move(partner, site.landing)
    decide(coordinator, partner, 102., bot, strategic=False)
    assert coordinator.teams.metrics["routes_used_by_partner"] == 1


def test_destroyed_bridge_cancels_use_without_claiming_success():
    world, bot, partner, coordinator = setup_crossing("bridge")
    order = decide(coordinator, bot, 100., partner)
    site = coordinator.lives[(bot.player_id, bot.generation)].task.site
    world._vxl.solids.update(site.cells)
    bot = replace(bot, last_action_request_id=order.action.request_id, last_task_accepted=True)
    assert decide(coordinator, bot, 100.2, partner).role == "cross_bridge"
    world._vxl.solids.discard(site.cells[-1])
    decide(coordinator, bot, 100.4, partner)
    assert coordinator.teams.metrics["tasks_failed"] == 1
    assert coordinator.teams.metrics["tasks_completed"] == 0


def test_cover_partner_keeps_its_role_between_candidate_evaluation_ticks():
    world, bot, partner, coordinator = setup_crossing("bridge")
    decide(coordinator, bot, 100., partner)
    first = decide(coordinator, partner, 100.2, bot, strategic=False)
    assert first.role == "cover_teammate"
    retained = decide(coordinator, partner, 100.3, bot, strategic=False)
    assert retained is not None and retained.role == first.role and retained.task_id == first.task_id


@pytest.mark.parametrize("kind", ["bridge", "breach"])
def test_partner_does_not_adopt_a_completed_crossing_destroyed_or_rebuilt_by_human(kind):
    world, bot, partner, coordinator = setup_crossing(kind)
    order = decide(coordinator, bot, 100., partner)
    task = coordinator.lives[(bot.player_id, bot.generation)].task
    site, task_id = task.site, task.task_id
    if kind == "bridge":
        world._vxl.solids.update(site.cells)
        bot = replace(bot, last_action_request_id=order.action.request_id, last_task_accepted=True)
    else:
        world._vxl.solids.difference_update(site.cells)
    decide(coordinator, bot, 100.2, partner)
    bot = move(bot, site.landing)
    decide(coordinator, bot, 101., partner)
    assert coordinator.teams.projects[task_id].stage is TaskStage.USE
    if kind == "bridge":
        world._vxl.solids.discard(site.cells[0])
    else:
        world._vxl.solids.add(site.cells[0])
    following = decide(coordinator, partner, 101.2, bot, strategic=False)
    assert following is None or following.role != "squad_advance"
    assert task_id not in coordinator.teams.projects


@pytest.mark.parametrize("change", ["stock", "support"])
def test_mine_approach_revalidates_resources_and_support_before_deploy(change, monkeypatch):
    world, bot, coordinator = setup_outpost()
    authority = ProjectAuthority(world, bot, monkeypatch)
    bot = authority.execute(bot, decide(coordinator, bot))
    approach = decide(coordinator, bot, 100.2)
    assert approach.role == "secure_outpost_approach"
    if change == "stock":
        bot = replace(bot, deployable_stock=((int(C.LANDMINE_TOOL), 0),))
    else:
        mine = coordinator.lives[(bot.player_id, bot.generation)].task.action_site
        world._vxl.solids.discard(mine.support_cells[0])
    bot = move(bot, approach.goal)
    result = decide(coordinator, bot, 101.)
    assert result is None or result.action.kind is not BotActionKind.DEPLOY


def test_stalled_breach_does_not_claim_success():
    world, bot, partner, coordinator = setup_crossing("breach")
    decide(coordinator, bot, 100., partner)
    decide(coordinator, bot, 109., partner)
    assert coordinator.teams.metrics["tasks_failed"] == 1
    assert coordinator.teams.metrics["tasks_completed"] == 0
