"""Bounded cooperative tasks, fair observations and harmless mischief."""

from __future__ import annotations

from dataclasses import replace
import math
from types import SimpleNamespace

import pytest
import shared.constants as C

from server.bot_ai.cooperative_behavior import CooperativeBehavior
from server.bot_ai.director import BotDirector
from server.bot_ai.messages import BotActionKind, EntitySnapshot, ObjectiveSnapshot, PerceptionFrame
from server.bot_ai.project_sites import _exits
from server.bot_ai.team_tasks import TeamProject, TeamTasks
from server.combat_runtime import CombatSystem
from server.entities.behaviors import RadarStationBehavior
from server.entities.registry import EntityContext, EntityRegistry
from server.map_resources import MapResourceService
from server.rocket_turret import RocketTurretBehavior
from tests.test_bot_project_sites import make_world, player
from tests.test_bot_cooperative_projects import decide, setup_crossing
from tests.test_simple_bot_tactics import _profile


def observer(**changes):
    return player(class_id=int(C.CLASS_SOLDIER), prefabs=(),
                  loadout=(int(C.MINIGUN_TOOL), int(C.BLOCK_TOOL)),
                  deployable_stock=(), **changes)


def frame(bot, *others, now=34.0, **changes):
    return PerceptionFrame(round(now * 10), 1, 1, 1, bot.player_id,
                           bot.generation, now, "tdm", (bot, *others),
                           profile=_profile(), behavior_version="cooperative",
                           **changes)


def friend(**changes):
    return observer(player_id=2, position=(25.5, 20.5, 17.75),
                    eye=(25.5, 20.5, 17.75), **changes)


def test_optional_mischief_places_one_supported_block_and_preserves_both_players_routes():
    world, bot, ally = make_world(), observer(), friend()
    coordinator = CooperativeBehavior(world)
    before = set(world._vxl.solids)
    order = coordinator.decide(frame(bot, ally), bot, None, None)
    assert order is not None and order.role == "mischief_execute"
    assert order.action.kind is BotActionKind.BUILD
    cell = tuple(int(value) for value in order.action.position)
    assert cell not in before and (cell[0], cell[1], cell[2] + 1) in before
    world._vxl.solids.add(cell)  # authoritative single-cell commit
    bot = replace(bot, blocks=bot.blocks - 1, last_task_accepted=True,
                  last_action_request_id=order.action.request_id)
    coordinator.decide(frame(bot, ally, now=34.2), bot, None, None)
    assert world._vxl.solids - before == {cell}
    assert before.issubset(world._vxl.solids)  # no footing was dug away
    assert len(_exits(world, bot.position)) >= 3
    assert _exits(world, ally.position)
    assert coordinator.teams.metrics["tasks_completed"] == 1
    later = coordinator.decide(frame(bot, ally, now=81.0), bot, None, None)
    assert later is None or later.action.kind is BotActionKind.NONE


def test_mischief_preserves_a_nearby_teammates_only_exit():
    world, bot = make_world(), observer()
    ally = observer(player_id=2, position=(25.5, 23.5, 17.75), eye=(25.5, 23.5, 17.75))
    world._vxl.solids.update((x, y, z) for x, y in ((26, 23), (25, 22), (25, 24))
                             for z in (17, 18, 19))
    assert len(_exits(world, ally.position)) == 1
    coordinator = CooperativeBehavior(world)
    order = coordinator.decide(frame(bot, ally), bot, None, None)
    assert order is not None and order.role == "mischief_execute"
    cell = tuple(int(value) for value in order.action.position)
    # The first decorative candidate would block the ally's west exit.
    assert cell != (23, 23, 19)
    world._vxl.solids.add(cell)
    assert _exits(world, ally.position)


@pytest.mark.parametrize("reason", ("disabled", "truncated", "objective", "damage", "poor", "alone", "enemy"))
def test_optional_mischief_yields_to_useful_or_unsafe_context(reason):
    world, bot, ally = make_world(), observer(), friend()
    kwargs, others, visible = {}, (ally,), None
    if reason == "disabled":
        kwargs["friendly_mischief"] = False
    elif reason == "truncated":
        kwargs["local_safety_complete"] = False
    elif reason == "objective":
        kwargs["objectives"] = (ObjectiveSnapshot("flag", 2, bot.position),)
    elif reason == "damage":
        bot = replace(bot, last_damage_at=33.9)
    elif reason == "poor":
        bot = replace(bot, blocks=2)
    elif reason == "alone":
        others = ()
    else:
        visible = replace(ally, player_id=9, team=3, position=(40.5, 20.5, 17.75))
    order = CooperativeBehavior(world).decide(frame(bot, *others, **kwargs), bot, visible, None)
    assert order is None or not order.role.startswith("mischief")


def test_mischief_requires_friendly_activity_nearby_not_just_someone_on_same_team():
    world, bot = make_world(), observer()
    distant = observer(player_id=2, position=(45.5, 20.5, 17.75), eye=(45.5, 20.5, 17.75))
    order = CooperativeBehavior(world).decide(frame(bot, distant), bot, None, None)
    assert order is None or not order.role.startswith("mischief")


def test_static_hidden_enemy_mine_and_hidden_roster_motion_do_not_change_intention():
    world, bot, ally = make_world(), observer(), friend()
    enemy = observer(player_id=9, team=3, position=(40.5, 20.5, 17.75))
    ordinary = CooperativeBehavior(world).decide(frame(bot, ally, enemy), bot, None, None)
    mine = EntitySnapshot(7, int(C.LANDMINE_ENTITY), 3, 9, bot.position,
                          kind="deployable", tool_id=int(C.LANDMINE_TOOL),
                          hazardous=True, blast_radius=3)
    moved = replace(enemy, position=(22.5, 20.5, 17.75), eye=(22.5, 20.5, 17.75))
    hidden = CooperativeBehavior(world).decide(frame(bot, ally, moved, entities=(mine,)),
                                               bot, None, None)
    assert ordinary == hidden


def test_recent_small_mutations_cannot_evict_a_large_charge_and_reopen_budget():
    team = TeamTasks()
    assert team.allow_mutation(2, 100.0, 128)
    team.record_mutation(2, 100.0, 128)
    for index in range(32):
        now = 100.01 + index * .01
        assert team.allow_mutation(2, now, 1)
        team.record_mutation(2, now, 1)
    # 160 committed/requested cells remain inside the rolling ten-second span.
    assert not team.allow_mutation(2, 101.0, 128)
    assert team.allow_mutation(2, 111.0, 128)


def test_project_membership_never_exceeds_one_owner_and_three_helpers():
    world, builder, partner, coordinator = setup_crossing("bridge")
    decide(coordinator, builder, 100.0)
    followers = [replace(partner, player_id=value) for value in range(2, 9)]
    accepted = []
    for index, follower in enumerate(followers):
        order = decide(coordinator, follower, 100.1 + index * .01, builder,
                       *[other for other in followers if other is not follower], strategic=False)
        if order is not None and order.role == "cover_teammate":
            accepted.append(follower)
    assert len(accepted) == 3
    assert all(1 + len(project.participants) <= 4 for project in coordinator.teams.projects.values())


def test_one_human_leader_does_not_collect_more_than_three_following_bots():
    world = make_world()
    leader = observer(player_id=20, is_bot=False, position=(30.5, 20.5, 17.75))
    followers = [observer(player_id=value) for value in range(1, 9)]
    coordinator = CooperativeBehavior(world)
    accepted = []
    for index, bot in enumerate(followers):
        order = coordinator.decide(frame(bot, leader, *[p for p in followers if p is not bot],
                                         now=100.0 + index * .01, friendly_mischief=False),
                                   bot, None, None)
        if order is not None and order.role == "join_player_push":
            accepted.append(bot)
    assert len(accepted) <= 3


def test_motionless_unreachable_partner_is_released_instead_of_renewed_forever():
    world, bot = make_world(), observer()
    leader = observer(player_id=2, is_bot=False, position=(30.5, 20.5, 17.75))
    coordinator = CooperativeBehavior(world)
    first = coordinator.decide(frame(bot, leader, now=100.0, friendly_mischief=False), bot, None, None)
    assert first is not None and first.role == "join_player_push"
    later = []
    for now in range(101, 133):
        order = coordinator.decide(frame(bot, leader, now=float(now), friendly_mischief=False),
                                   bot, None, None)
        later.append(order)
    assert any(order is None or order.role != "join_player_push" for order in later[8:])


def test_existing_bot_leader_does_not_create_a_follow_chain_when_human_appears():
    world = make_world()
    loadout = (int(C.LIGHT_MACHINE_GUN_TOOL), int(C.MEDPACK_TOOL))
    lead = player(player_id=1, class_id=int(C.CLASS_MEDIC), loadout=loadout,
                  prefabs=(), deployable_stock=((int(C.MEDPACK_TOOL), 2),))
    follower = replace(lead, player_id=2, position=(26.5, 20.5, 17.75))
    human = replace(lead, player_id=20, is_bot=False, position=(30.5, 20.5, 17.75))
    coordinator = CooperativeBehavior(world)
    coordinator.decide(frame(lead, follower, now=100.0, friendly_mischief=False), lead, None, None)
    order = coordinator.decide(frame(follower, lead, now=100.1, friendly_mischief=False),
                               follower, None, None)
    assert order is not None and order.role == "medic_partner"
    coordinator.decide(frame(lead, follower, human, now=101.0, friendly_mischief=False),
                       lead, None, None)
    for life in coordinator.lives.values():
        if life.partner is not None:
            target = coordinator.lives.get(life.partner[:2])
            assert target is None or target.partner is None


def test_mischief_does_not_ignore_the_25th_nearby_teammates_only_exit():
    world, bot = make_world(), observer()
    crowded = [observer(player_id=value,
                        position=(12.5 + (value - 2) % 6, 10.5 + (value - 2) // 6, 17.75))
               for value in range(2, 26)]
    vulnerable = observer(player_id=26, position=(25.5, 23.5, 17.75),
                          eye=(25.5, 23.5, 17.75))
    world._vxl.solids.update((x, y, z) for x, y in ((26, 23), (25, 22), (25, 24))
                             for z in (17, 18, 19))
    assert len(_exits(world, vulnerable.position)) == 1
    order = CooperativeBehavior(world).decide(frame(bot, *crowded, vulnerable), bot, None, None)
    if order is not None and order.role == "mischief_execute":
        cell = tuple(int(value) for value in order.action.position)
        world._vxl.solids.add(cell)
    assert _exits(world, vulnerable.position)


def test_depleted_block_wallet_seeks_a_real_block_crate_not_ammo():
    world, bot = make_world(), observer(blocks=0)
    ammo = EntitySnapshot(10, int(C.AMMO_CRATE), 0, -1, (24.5, 20.5, 17.75))
    blocks = EntitySnapshot(11, int(C.BLOCK_CRATE), 0, -1, (28.5, 20.5, 17.75))
    order = CooperativeBehavior(world).decide(frame(bot, now=100.0, entities=(ammo, blocks),
                                                    friendly_mischief=False), bot, None, None)
    assert order is not None and order.goal == blocks.position


def test_bridge_lease_expiry_does_not_claim_success_without_using_crossing():
    world, bot, partner, coordinator = setup_crossing("bridge")
    first = decide(coordinator, bot, 100.0, partner)
    task = coordinator.lives[(bot.player_id, bot.generation)].task
    world._vxl.solids.update(task.site.cells)
    bot = replace(bot, last_action_request_id=first.action.request_id, last_task_accepted=True)
    assert decide(coordinator, bot, 100.2, partner).role == "cross_bridge"
    # The geometry exists, but movement never reaches the validated landing.
    for now in range(101, 127):
        decide(coordinator, bot, float(now), partner, strategic=False)
    assert coordinator.teams.metrics["tasks_completed"] == 0
    assert coordinator.teams.metrics["tasks_failed"] >= 1


@pytest.mark.parametrize("epoch_field", ("map_epoch", "mode_epoch"))
def test_epoch_change_drops_pending_actions_and_old_project_reservations(epoch_field):
    world, bot, partner, coordinator = setup_crossing("bridge")
    pending = decide(coordinator, bot, 100.0, partner)
    old_task = pending.task_id
    changed = replace(frame(bot, partner, now=100.2, friendly_mischief=False), **{epoch_field: 2})
    order = coordinator.decide(changed, bot, None, None)
    assert order is None or order.task_id != old_task
    assert not coordinator.teams.projects
    assert coordinator.lives[(bot.player_id, bot.generation)].task is None


def test_expired_and_dead_participant_leases_are_released_without_mutation():
    tasks = TeamTasks()
    project = TeamProject(1, 2, "cover", (1, 1, 0), (20., 20., 18.), (40., 20., 18.),
                          100.0, 125.0, participants={(2, 1, 0): 103., (3, 1, 0): 120.})
    assert tasks.reserve(project)
    tasks.expire(104.0, {(1, 1): (True, 0), (3, 1): (False, 0)})
    assert not project.participants
    assert tasks.projects
    tasks.expire(104.1, {(1, 1): (True, 1)})  # new owner life
    assert not tasks.projects


def device_fixture(kind="radar", *, team=3):
    """Use production hit metadata and its real director serialization."""

    server = SimpleNamespace(entity_registry=EntityRegistry(), players={}, rocket_turrets={})
    if kind == "radar":
        behavior, entity_type = RadarStationBehavior(team), C.RADAR_STATION_ENTITY
    else:
        behavior = RocketTurretBehavior(SimpleNamespace(server=server))
        entity_type = C.ROCKET_TURRET_ENTITY
    entity = server.entity_registry.place(entity_type, 35., 20., 19.,
                                         behavior=behavior, kind="deployable", player_id=9)
    if kind == "turret":
        server.rocket_turrets[entity.entity_id] = SimpleNamespace(team=team)
    snapshots = BotDirector._snapshot_entities(SimpleNamespace(server=server))
    return server, entity, snapshots


def armed_observer(**changes):
    return observer(tool=int(C.MINIGUN_TOOL), weapon_tool=int(C.MINIGUN_TOOL), **changes)


@pytest.mark.parametrize("kind", ("radar", "turret"))
def test_sabotage_uses_visible_real_device_hit_volume_after_reaction_delay(kind):
    world, bot = make_world(), armed_observer()
    server, entity, snapshots = device_fixture(kind)
    assert snapshots[0].team == 3
    assert snapshots[0].hit_position == entity.behavior.get_hit_center(entity)
    coordinator = CooperativeBehavior(world)
    observed = replace(frame(bot, now=100., entities=snapshots, friendly_mischief=False),
                       profile=replace(_profile(), reaction_time=.25))
    first = coordinator.decide(observed, bot, None, None)
    assert first is not None and first.role == "sabotage_device"
    assert first.action.kind is BotActionKind.NONE
    order = coordinator.decide(replace(observed, frame_id=1010, created_at=101.), bot, None, None)
    assert order is not None and order.action.kind is BotActionKind.FIRE
    assert order.action.tool_id == bot.weapon_tool
    direction = tuple(end - start for end, start in zip(order.action.position, bot.eye))
    magnitude = math.sqrt(sum(value * value for value in direction))
    direction = tuple(value / magnitude for value in direction)
    hit = CombatSystem(server)._find_first_entity_hit(bot.eye, direction, 40.)
    assert hit is not None and hit[0] is entity


@pytest.mark.parametrize("reason", ("occluded", "friendly_near", "friendly_device", "empty", "unknown_hit_shape"))
def test_sabotage_rejects_unsafe_or_unobservable_devices(reason):
    world, bot = make_world(), armed_observer()
    _, _, snapshots = device_fixture(team=2 if reason == "friendly_device" else 3)
    allies = ()
    if reason == "occluded":
        world._vxl.solids.update((28, y, z) for y in range(18, 23) for z in range(14, 20))
    elif reason == "friendly_near":
        allies = (observer(player_id=2, position=(35.5, 22.5, 17.75)),)
    elif reason == "empty":
        bot = replace(bot, ammo_clip=0, ammo_reserve=0)
    elif reason == "unknown_hit_shape":
        snapshots = (replace(snapshots[0], hit_position=None, hit_radius=0),)
    coordinator = CooperativeBehavior(world)
    order = coordinator.decide(frame(bot, *allies, now=100., entities=snapshots,
                                     friendly_mischief=False), bot, None, None)
    assert order is None or order.role != "sabotage_device"
    assert coordinator.teams.metrics["tasks_started"] == 0


@pytest.mark.parametrize("reason", ("contact_lost", "friendly_enters", "ammo_exhausted"))
def test_active_sabotage_stops_when_contact_or_safety_changes_without_claiming_kill(reason):
    world, bot = make_world(), armed_observer()
    _, _, snapshots = device_fixture()
    coordinator = CooperativeBehavior(world)
    assert coordinator.decide(frame(bot, now=100., entities=snapshots,
                                    friendly_mischief=False), bot, None, None) is not None
    allies = ()
    if reason == "contact_lost":
        snapshots = ()
    elif reason == "friendly_enters":
        allies = (observer(player_id=2, position=(35.5, 22.5, 17.75)),)
    else:
        bot = replace(bot, ammo_clip=0, ammo_reserve=0)
    order = coordinator.decide(frame(bot, *allies, now=101., entities=snapshots,
                                     friendly_mischief=False), bot, None, None)
    assert order is None or order.action.kind is not BotActionKind.FIRE
    assert coordinator.teams.metrics["tasks_completed"] == 0
    assert coordinator.teams.metrics["tasks_failed"] == 1


def test_resupply_requires_actual_crate_touch_before_reporting_restored_blocks():
    from tests.test_equipment_handlers import _server_player

    server, actual, _ = _server_player(C.MINIGUN_TOOL, [C.MINIGUN_TOOL, C.BLOCK_TOOL])
    actual.is_bot = True
    actual.set_position(20.5, 20.5, 17.75)
    actual.grounded = True
    actual.blocks = 0
    director = BotDirector(server, supervisor=SimpleNamespace())
    kind, behavior = MapResourceService._behaviors()[int(C.BLOCK_CRATE)]
    crate = server.entity_registry.place(C.BLOCK_CRATE, 28.5, 20.5, 17.75,
                                          kind=kind, behavior=behavior)
    bot = director._snapshot_player(actual)
    coordinator = CooperativeBehavior(make_world())
    order = coordinator.decide(frame(bot, now=100., entities=director._snapshot_entities(),
                                     friendly_mischief=False), bot, None, None)
    assert order is not None and order.role == "resupply" and order.goal == crate.home
    assert actual.blocks == 0
    destroyed = []
    context = EntityContext(.1, 100.2, [actual], server=server, destroy=destroyed.append)
    server.entity_registry.tick(context)
    assert actual.blocks == 0 and crate.alive  # planning and distant ticks grant nothing
    coordinator.decide(frame(bot, now=100.2, entities=director._snapshot_entities(),
                             friendly_mischief=False), bot, None, None)
    assert coordinator.teams.metrics["tasks_completed"] == 0

    actual.set_position(*crate.home)
    context.now = 100.5
    server.entity_registry.tick(context)  # normal authoritative proximity/pickup path
    assert actual.blocks > 0 and actual.blocks <= actual._block_wallet_max()
    assert not crate.alive and destroyed == [crate.entity_id]
    restored = director._snapshot_player(actual)
    coordinator.decide(frame(restored, now=100.5, entities=director._snapshot_entities(),
                             friendly_mischief=False), restored, None, None)
    assert coordinator.teams.metrics["tasks_completed"] == 1
    held_blocks = actual.blocks
    context.now = 100.6
    server.entity_registry.tick(context)
    assert actual.blocks == held_blocks  # dead crate cannot grant a second refill
