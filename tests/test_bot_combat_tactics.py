"""Situational combat choices: exposed aim points, silent guns, cover, grenades."""

from dataclasses import replace
import math
from types import SimpleNamespace

from server.bot_ai.combat_tactics import (
    CROUCH_DROP,
    TORSO_OFFSET,
    Engagement,
    aim_offset,
    find_cover,
    fire_blocked,
    hazard_escape,
    mix,
    stationary_relocation,
    walkable_heading,
)
from server.bot_ai.messages import BotActionKind, EntitySnapshot, ObjectiveSnapshot
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState
from tests.test_simple_bot_tactics import _frame, _player, _profile

_FLOOR = 40  # support layer; a standing head is 2.25 above it
_HEAD = _FLOOR - 2.25


class _GridWorld:
    """Flat dry floor plus explicit solid cells, with the worker's sampled ray."""

    def __init__(self, solids=()):
        self.cells = set(solids)

    def solid(self, x, y, z):
        return z >= _FLOOR or (x, y, z) in self.cells

    def has_line_of_sight(self, origin, target):
        delta = [target[i] - origin[i] for i in range(3)]
        distance = math.sqrt(sum(value * value for value in delta))
        steps = max(1, int(math.ceil(distance * 2)))
        return not any(self.solid(*(int(math.floor(origin[i] + delta[i] * index / steps))
                                    for i in range(3))) for index in range(1, steps))

    def surface(self, x, y, _z, **_kwargs):
        if any((x, y, _FLOOR - offset) in self.cells for offset in (1, 2, 3)):
            return None
        return SimpleNamespace(x=x, y=y, support_z=_FLOOR, position=(x + .5, y + .5, _HEAD))


def _fighter(player_id, team, x, y, **changes):
    player = _player(player_id, team, (x, y, _HEAD), is_bot=player_id == 1)
    return replace(player, eye=player.position, **changes)


def _wall(x, ys, zs):
    return {(x, y, z) for y in ys for z in zs}


def test_hidden_torso_selects_the_exposed_head():
    # A chest-high lip in front of the enemy hides the torso only.
    lip = _wall(28, range(8, 13), (_FLOOR - 1, _FLOOR - 2))
    observer, enemy = _fighter(1, 2, 10.5, 10.5), _fighter(2, 3, 30.5, 10.5)
    assert aim_offset(_GridWorld(), observer, enemy, _profile(), Engagement(), 100.) == TORSO_OFFSET
    assert aim_offset(_GridWorld(lip), observer, enemy, _profile(), Engagement(), 100.) == 0.0


def test_silent_gun_is_noticed_and_accepted_shots_clear_it():
    engagement, observer = Engagement(), _fighter(1, 2, 10.5, 10.5)
    assert not fire_blocked(engagement, observer, True, 100.0, cadence=.5)
    assert not fire_blocked(engagement, observer, True, 101.0, cadence=.5)
    assert fire_blocked(engagement, observer, True, 101.6, cadence=.5)
    assert engagement.head_aim_until > 101.6
    firing = replace(observer, last_action_kind="fire", last_action_accepted=True,
                     last_action_at=110.0)
    fresh = Engagement()
    for step in range(40):
        assert not fire_blocked(fresh, replace(firing, last_action_at=110. + step * .25),
                                True, 110. + step * .25, cadence=.5)


def test_reload_and_burst_pauses_are_not_a_blocked_lane():
    engagement, observer = Engagement(), _fighter(1, 2, 10.5, 10.5)
    fire_blocked(engagement, observer, True, 100.0, cadence=1.8)
    assert not fire_blocked(engagement, observer, True, 102.5, cadence=1.8)
    assert not fire_blocked(engagement, replace(observer, reloading=True), True, 110., cadence=.5)
    assert engagement.wanting_fire_since is None


def test_low_lip_means_duck_and_a_wall_means_move_behind_it():
    observer, enemy = _fighter(1, 2, 10.5, 10.5), _fighter(2, 3, 30.5, 10.5)
    lip = _wall(12, range(8, 13), (_FLOOR - 1, _FLOOR - 2))
    standing = _GridWorld(lip)
    assert standing.has_line_of_sight(observer.eye, enemy.eye)
    assert not standing.has_line_of_sight(
        (observer.eye[0], observer.eye[1], observer.eye[2] + CROUCH_DROP), enemy.eye)
    assert find_cover(standing, observer, enemy.eye, Engagement(), 100.) == (None, True)

    pillar = _wall(9, range(12, 15), range(_FLOOR - 4, _FLOOR)) | _wall(
        10, range(12, 15), range(_FLOOR - 4, _FLOOR))
    world = _GridWorld(pillar)
    position, duck = find_cover(world, observer, enemy.eye, Engagement(), 100.)
    assert not duck and position is not None
    assert not world.has_line_of_sight(position, enemy.eye)
    assert math.dist(position[:2], observer.position[:2]) <= 7.5


def test_open_ground_has_no_cover_and_searches_are_rate_limited():
    observer, enemy = _fighter(1, 2, 10.5, 10.5), _fighter(2, 3, 30.5, 10.5)
    engagement = Engagement()
    assert find_cover(_GridWorld(), observer, enemy.eye, engagement, 100.) == (None, False)
    assert engagement.next_cover_search_at == 101.
    calls = []
    world = _GridWorld()
    world.has_line_of_sight = lambda *_: calls.append(1) or True
    assert find_cover(world, observer, enemy.eye, engagement, 100.5) == (None, False)
    assert not calls


def test_planted_shooter_displaces_after_its_spell_or_when_hit():
    world = _GridWorld()
    observer, enemy = _fighter(1, 2, 10.5, 10.5), _fighter(2, 3, 60.5, 10.5)
    engagement = Engagement()
    assert stationary_relocation(world, observer, enemy, _profile(), engagement, 100.) is None
    assert stationary_relocation(world, observer, enemy, _profile(), engagement, 102.) is None
    late = stationary_relocation(world, observer, enemy, _profile(), engagement,
                                 100. + engagement.spot_hold + .1)
    assert late is not None and abs(late[1]) > .8  # lateral, not a charge
    assert stationary_relocation(world, observer, enemy, _profile(), engagement,
                                 engagement.relocate_until - .05) == late

    pressured = Engagement()
    stationary_relocation(world, observer, enemy, _profile(), pressured, 200.)
    hit = replace(observer, last_damage_at=200.9)
    assert stationary_relocation(world, hit, enemy, _profile(), pressured, 201.) is not None


def test_relocation_never_strides_into_a_wall_or_pit():
    walls = (_wall(10, (8, 9, 12, 13), range(_FLOOR - 4, _FLOOR))
             | _wall(11, (8, 9, 12, 13), range(_FLOOR - 4, _FLOOR))
             | _wall(9, (8, 9, 12, 13), range(_FLOOR - 4, _FLOOR)))
    world = _GridWorld(walls)
    observer = _fighter(1, 2, 10.5, 10.5)
    assert not walkable_heading(world, observer, (0., 1., 0.))
    assert walkable_heading(world, observer, (1., 0., 0.))


def _grenade(position, velocity=(0., 0., 0.), *, entity_id=7, team=3, kind="projectile",
             detonate_at=101.):
    return EntitySnapshot(entity_id, 11, team, 2, position, kind=kind, velocity=velocity,
                          blast_radius=6., detonate_at=detonate_at, hazardous=True)


def test_noticed_grenade_is_fled_but_rockets_and_hidden_mines_are_not():
    world, observer = _GridWorld(), _fighter(1, 2, 10.5, 10.5)
    sharp = replace(_profile(), skill=1.0)
    frame = replace(_frame(observer), entities=(_grenade((12.5, 10.5, _HEAD)),))
    heading = hazard_escape(world, frame, observer, sharp, Engagement(), 100.)
    assert heading is not None and heading[0] < -.5

    rocket = replace(frame, entities=(_grenade((12.5, 10.5, _HEAD), (75., 0., 0.)),))
    assert hazard_escape(world, rocket, observer, sharp, Engagement(), 100.) is None
    mine = replace(frame, entities=(_grenade((12.5, 10.5, _HEAD), kind="deployable",
                                             detonate_at=0.),))
    assert hazard_escape(world, mine, observer, sharp, Engagement(), 100.) is None
    far = replace(frame, entities=(_grenade((40.5, 10.5, _HEAD)),))
    assert hazard_escape(world, far, observer, sharp, Engagement(), 100.) is None


def test_inattentive_profiles_sometimes_miss_the_grenade():
    world, observer = _GridWorld(), _fighter(1, 2, 10.5, 10.5)
    casual = replace(_profile(), skill=.2)
    noticed = sum(
        hazard_escape(world, replace(_frame(observer), entities=(
            _grenade((12.5, 10.5, _HEAD), entity_id=index),)), observer, casual,
            Engagement(), 100.) is not None
        for index in range(200))
    assert 40 < noticed < 140


def test_brain_runs_from_a_grenade_before_any_other_task():
    observer = _fighter(1, 2, 10.5, 10.5)
    frame = replace(_frame(observer), profile=replace(_profile(), skill=1.0),
                    entities=(_grenade((12.5, 10.5, _HEAD)),))
    intent = SimpleBotBrain(_GridWorld()).decide(frame)
    assert intent.debug_role == "evade_explosive"
    assert intent.movement.sprint and intent.movement.direction[0] < -.5
    assert intent.action.kind is BotActionKind.NONE


def _anchors():
    return (ObjectiveSnapshot("team_anchor", 2, (60., 256., _HEAD)),
            ObjectiveSnapshot("team_anchor", 3, (440., 256., _HEAD)))


def test_flank_choice_is_stable_within_a_life_and_varies_across_lives():
    brain = SimpleBotBrain(_GridWorld())
    eager = replace(_profile(), creativity=1., caution=1., aggression=0.)
    sides = set()
    for life in range(24):
        observer = replace(_fighter(1, 2, 62.5, 256.5), life_id=life)
        frame = replace(_frame(observer, objectives=_anchors()), profile=eager)
        state = _BotState(1, 1, life)
        # Decided at spawn, but the base is always left by the ordinary route.
        assert brain._flank_approach_goal(frame, observer, state, 100.) is None
        clear = replace(observer, position=(125.5, 256.5, _HEAD))
        first = brain._flank_approach_goal(frame, clear, state, 105.)
        assert brain._flank_approach_goal(frame, clear, state, 106.) == first
        if first is not None:
            assert first.role == "tdm_flank_approach"
            assert abs(first.position[1] - 256.) >= 30.
            sides.add(first.position[1] > 256.)
    assert sides == {True, False}


def test_flank_is_released_on_arrival_contact_or_after_passing_midfield():
    brain = SimpleBotBrain(_GridWorld())
    eager = replace(_profile(), creativity=1., caution=1., aggression=0.)
    life = next(index for index in range(64) if mix(1, index, 41) < .4)
    observer = replace(_fighter(1, 2, 62.5, 256.5), life_id=life)
    frame = replace(_frame(observer, objectives=_anchors()), profile=eager)
    state = _BotState(1, 1, life)
    assert brain._flank_approach_goal(frame, observer, state, 100.) is None  # still in base
    clear = replace(observer, position=(125.5, 256.5, _HEAD))
    assert brain._flank_approach_goal(frame, clear, state, 105.) is not None
    advanced = replace(observer, position=(300.5, 256.5, _HEAD))
    assert brain._flank_approach_goal(frame, advanced, state, 110.) is None
    assert brain._flank_approach_goal(frame, clear, state, 111.) is None  # never re-rolled


def test_aggressive_bots_mostly_take_the_direct_line():
    brain = SimpleBotBrain(_GridWorld())
    blunt = replace(_profile(), creativity=.15, caution=.2, aggression=.9)
    flanks = 0
    for life in range(60):
        observer = replace(_fighter(1, 2, 62.5, 256.5), life_id=life)
        frame = replace(_frame(observer, objectives=_anchors()), profile=blunt)
        flanks += brain._flank_approach_goal(frame, observer, _BotState(1, 1, life), 100.) is not None
    assert flanks <= 12


def test_corridor_detours_far_beyond_the_direct_trip_are_rejected():
    # The recorded AncientEgypt detour: down the east border, along the south
    # edge, then back up to the enemy base.
    around_the_map = (tuple((340., 250. - step * 8., 200.) for step in range(30))
                      + tuple((340. - step * 8., 10., 200.) for step in range(28))
                      + tuple((124., 18. + step * 8., 200.) for step in range(32)))
    assert SimpleBotBrain._corridor_is_absurd(around_the_map, (342., 249., 200.), (128., 267., 200.))
    bend = ((342., 249., 200.), (300., 200., 200.), (128., 267., 200.))
    assert not SimpleBotBrain._corridor_is_absurd(bend, (342., 249., 200.), (128., 267., 200.))


def _rocketeer(**changes):
    import shared.constants as C
    fuel = C.JETPACK_PROPERTIES[int(C.JETPACK2)][C.JETPACK_MAX_FUEL]
    return _fighter(1, 2, 10.5, 10.5, jetpack_id=int(C.JETPACK2), jetpack_fuel=float(fuel),
                    **changes)


def test_pressured_pack_owner_leaps_to_a_validated_landing_and_keeps_watching():
    from server.bot_ai.messages import MovementAffordance
    brain, enemy = SimpleBotBrain(_GridWorld()), _fighter(2, 3, 40.5, 10.5)
    observer = _rocketeer(last_damage_at=99.8)
    state = _BotState(1, 1, observer.life_id)
    intent = brain._combat_jetpack_hop(_frame(observer, enemy), observer, enemy, state,
                                       _profile(), 100.)
    assert intent is not None and intent.debug_role == "combat_jetpack_takeoff"
    assert intent.movement.affordance is MovementAffordance.JETPACK
    assert intent.look.target == enemy.eye
    landing = state.flight_step.waypoint
    assert 4.5 <= math.dist(landing[:2], observer.position[:2]) <= 8.
    assert abs(landing[1] - observer.position[1]) > 3.  # sideways, not a charge
    # One leap at a time, and never again within the cooldown.
    assert brain._combat_jetpack_hop(_frame(observer, enemy), observer, enemy, state,
                                     _profile(), 101.) is None


def test_no_leap_without_a_pack_under_a_roof_or_at_knife_range():
    enemy = _fighter(2, 3, 40.5, 10.5)
    walker = _fighter(1, 2, 10.5, 10.5, last_damage_at=99.8)
    assert SimpleBotBrain(_GridWorld())._combat_jetpack_hop(
        _frame(walker, enemy), walker, enemy, _BotState(1, 1, 0), _profile(), 100.) is None
    roof = {(x, y, _FLOOR - 5) for x in range(0, 24) for y in range(0, 24)}
    observer = _rocketeer(last_damage_at=99.8)
    assert SimpleBotBrain(_GridWorld(roof))._combat_jetpack_hop(
        _frame(observer, enemy), observer, enemy, _BotState(1, 1, 0), _profile(), 100.) is None
    close = _fighter(2, 3, 14.5, 10.5)
    assert SimpleBotBrain(_GridWorld())._combat_jetpack_hop(
        _frame(observer, close), observer, close, _BotState(1, 1, 0), _profile(), 100.) is None


def _scout(distance_ammo, held):
    import shared.constants as C
    sniper, pistol = int(C.SNIPER_TOOL), int(C.PISTOL_TOOL)
    player = _player(1, 2, (10.5, 10.5, _HEAD), is_bot=True,
                     loadout=(int(C.PICKAXE_TOOL), sniper, pistol), weapon_tool=held)
    return replace(player, weapon_ammo=((sniper, *distance_ammo[0]), (pistol, *distance_ammo[1])))


def test_sidearm_is_drawn_when_the_primary_is_dry_or_the_enemy_is_close():
    import shared.constants as C
    from server.bot_ai.simple_worker import _weapon_tool, _weapon_wallet
    sniper, pistol = int(C.SNIPER_TOOL), int(C.PISTOL_TOOL)
    loaded = _scout(((1, 7), (6, 30)), sniper)
    assert _weapon_tool(loaded) == sniper
    assert _weapon_tool(loaded, 80.) == sniper
    assert _weapon_tool(loaded, 9.) == pistol
    # Separate draw and holster distances: no flipping at one boundary.
    assert _weapon_tool(loaded, 18.) == sniper
    assert _weapon_tool(_scout(((1, 7), (6, 30)), pistol), 18.) == pistol
    assert _weapon_tool(_scout(((1, 7), (6, 30)), pistol), 30.) == sniper
    # Empty chamber with an enemy near: the pistol is faster than a reload.
    assert _weapon_tool(_scout(((0, 7), (6, 30)), sniper), 20.) == pistol
    assert _weapon_tool(_scout(((0, 7), (6, 30)), sniper), 90.) == sniper
    dry = _scout(((0, 0), (6, 30)), sniper)
    assert _weapon_tool(dry) == pistol and _weapon_wallet(dry, pistol) == (6, 30)


def test_a_dry_primary_fights_on_with_the_sidearm_instead_of_a_pickaxe():
    import shared.constants as C
    observer = replace(_scout(((0, 0), (6, 30)), int(C.SNIPER_TOOL)), eye=(10.5, 10.5, _HEAD))
    enemy = _fighter(2, 3, 30.5, 10.5)
    intent = SimpleBotBrain(_GridWorld()).decide(_frame(observer, enemy))
    assert intent.tool_id == int(C.PISTOL_TOOL)
    assert intent.action.kind is BotActionKind.FIRE


def test_between_fights_a_half_empty_magazine_is_topped_up_once():
    import shared.constants as C
    smg = int(C.SMG_TOOL)
    player = _player(1, 2, (10.5, 10.5, _HEAD), is_bot=True, loadout=(smg,), weapon_tool=smg)
    from tests.test_simple_bot_tactics import _TacticalWorld
    low = replace(player, ammo_clip=6, ammo_reserve=50, weapon_ammo=((smg, 6, 50),))
    frame = _frame(low, objectives=_anchors())
    assert SimpleBotBrain(_TacticalWorld()).decide(frame).action.kind is BotActionKind.RELOAD
    busy = replace(low, reloading=True)
    assert SimpleBotBrain(_TacticalWorld()).decide(_frame(busy, objectives=_anchors())
                                                   ).action.kind is BotActionKind.NONE
    full = replace(player, ammo_clip=25, ammo_reserve=50, weapon_ammo=((smg, 25, 50),))
    assert SimpleBotBrain(_TacticalWorld()).decide(_frame(full, objectives=_anchors())
                                                   ).action.kind is BotActionKind.NONE


def test_shotgun_doctrine_is_clamped_to_the_real_barrel_range():
    import shared.constants as C
    from server.bot_ai.combat_profiles import envelope_for
    from server.game_constants import WEAPON_PROFILES
    for tool in WEAPON_PROFILES:
        envelope = envelope_for(tool)
        assert envelope.hard_max <= max(WEAPON_PROFILES[tool].max_range, 1.) + 1e-6
        assert envelope.ideal_min < envelope.ideal_max <= envelope.hard_max
    assert envelope_for(int(C.SHOTGUN2_TOOL)).hard_max < 20.


def test_empty_clip_at_arms_length_swings_the_spade_instead_of_reloading():
    import shared.constants as C
    observer = replace(_fighter(1, 2, 10.5, 10.5), ammo_clip=0, ammo_reserve=50)
    near, far = _fighter(2, 3, 12.5, 10.5), _fighter(2, 3, 30.5, 10.5)
    swing = SimpleBotBrain(_GridWorld()).decide(_frame(observer, near))
    assert swing.action.kind is BotActionKind.MELEE
    assert swing.tool_id == int(C.SPADE_TOOL)
    reload = SimpleBotBrain(_GridWorld()).decide(_frame(observer, far))
    assert reload.action.kind is BotActionKind.RELOAD


def _ctf_frame(now, *players):
    from tests.test_bot_policies import _frame as policy_frame
    objectives = (ObjectiveSnapshot("ctf_base", 2, (60., 256., 30.)),
                  ObjectiveSnapshot("ctf_intel", 2, (60., 256., 30.)),
                  ObjectiveSnapshot("ctf_base", 3, (440., 256., 30.)),
                  ObjectiveSnapshot("ctf_intel", 3, (440., 256., 30.)))
    return replace(policy_frame("ctf", *players, objectives=objectives), created_at=now)


def _squad(positions):
    from tests.test_bot_policies import _player as policy_player
    return [policy_player(index * 3, 2, position) for index, position in enumerate(positions)]


def test_ctf_keeps_one_guard_near_home_and_sends_everyone_else_forward():
    from server.bot_ai.policies import objective_decision_for
    team = _squad([(70., 256., 30.), (200., 250., 30.), (260., 256., 30.),
                   (300., 260., 30.), (180., 240., 30.)])
    frame = _ctf_frame(100., *team)
    roles = [objective_decision_for(frame, bot).role for bot in team]
    assert roles.count("ctf_defend") == 1 and roles[0] == "ctf_defend"
    assert roles.count("ctf_attack_intel") == 4
    # Ids 0, 3, 6, 9 were all permanent sentries under the old id % 3 rule.
    pair = _squad([(70., 256., 30.), (200., 250., 30.)])
    assert all(objective_decision_for(_ctf_frame(100., *pair), bot).role == "ctf_attack_intel"
               for bot in pair)


def test_a_fresh_respawn_relieves_the_guard_who_then_attacks():
    from server.bot_ai.policies import objective_decision_for
    team = _squad([(95., 256., 30.), (200., 250., 30.), (260., 256., 30.), (300., 260., 30.)])
    assert objective_decision_for(_ctf_frame(100., *team), team[0]).role == "ctf_defend"
    team[2] = replace(team[2], position=(62., 256., 30.))
    frame = _ctf_frame(101., *team)
    assert objective_decision_for(frame, team[2]).role == "ctf_defend"
    assert objective_decision_for(frame, team[0]).role == "ctf_attack_intel"


def test_the_guard_walks_a_beat_with_a_forward_picket_instead_of_freezing():
    from server.bot_ai.policies import objective_decision_for
    team = _squad([(62., 256., 30.), (200., 250., 30.), (260., 256., 30.)])
    posts = {objective_decision_for(_ctf_frame(100. + 16. * leg, *team), team[0]).position
             for leg in range(6)}
    assert len(posts) >= 4
    reach = sorted(math.dist(post[:2], (60., 256.)) for post in posts)
    assert reach[0] <= 16. and reach[-1] >= 24.  # close posts and a picket toward the enemy
    assert max(post[0] for post in posts) > 80.


def test_a_lone_raider_waits_at_midfield_only_for_an_ally_who_is_about_to_arrive():
    from server.bot_ai.policies import objective_decision_for
    lead, follower, guard = _squad([(250., 256., 30.), (200., 256., 30.), (62., 256., 30.)])
    frame = _ctf_frame(100., lead, follower, guard)
    assert objective_decision_for(frame, lead).role == "ctf_rally"
    assert objective_decision_for(frame, follower).role == "ctf_attack_intel"  # keeps coming
    # Alongside: both go. Nobody near behind: go alone. Under fire: never stand still.
    together = _ctf_frame(101., lead, replace(follower, position=(240., 250., 30.)), guard)
    assert objective_decision_for(together, lead).role == "ctf_attack_intel"
    alone = _ctf_frame(101., lead, replace(follower, position=(90., 256., 30.)), guard)
    assert objective_decision_for(alone, lead).role == "ctf_attack_intel"
    shot = replace(lead, last_damage_at=99.)
    assert objective_decision_for(_ctf_frame(100., shot, follower, guard), shot).role == "ctf_attack_intel"
    # Past the rally band the raid is committed.
    deep = replace(lead, position=(320., 256., 30.))
    assert objective_decision_for(_ctf_frame(100., deep, follower, guard), deep).role == "ctf_attack_intel"


def test_a_raider_beside_the_flag_dives_for_it_while_returning_fire():
    from server.bot_ai.policies import ModeBotDecision, ModeBotPosture
    from tests.test_simple_bot_tactics import _TacticalWorld
    from server.bot_ai.simple_navigation import RouteStep
    from server.bot_ai.messages import MovementAffordance
    observer = _fighter(1, 2, 10.5, 10.5)
    guard = _fighter(2, 3, 16.5, 14.5)
    flag = (14.5, 10.5, _HEAD)
    world = _TacticalWorld(route_step=RouteStep(flag, MovementAffordance.WALK))
    brain = SimpleBotBrain(world)
    decision = ModeBotDecision(flag, "ctf_attack_intel", arrival_radius=2.0,
                               posture=ModeBotPosture.ASSAULT, objective_priority=.82)
    state = _BotState(1, 1, observer.life_id)
    intent = brain._combat_intent(_frame(observer, guard), observer, guard, state,
                                  _profile(), 100., decision)
    assert intent.debug_role == "combat_objective:ctf_attack_intel"
    assert intent.movement.direction[0] > .9          # toward the flag, not the guard
    assert intent.action.kind is BotActionKind.FIRE    # and still shooting
    assert intent.look.target_player_id == guard.player_id


def test_a_started_flank_is_not_cancelled_by_stepping_back_over_the_base_line():
    brain = SimpleBotBrain(_GridWorld())
    eager = replace(_profile(), creativity=1., caution=1., aggression=0.)
    life = next(index for index in range(64) if mix(1, index, 41) < .4)
    observer = replace(_fighter(1, 2, 62.5, 256.5), life_id=life)
    frame = replace(_frame(observer, objectives=_anchors()), profile=eager)
    state = _BotState(1, 1, life)
    brain._flank_approach_goal(frame, observer, state, 100.)
    across = replace(observer, position=(99.5, 256.5, _HEAD))   # 10.4 % of the way
    behind = replace(observer, position=(96.5, 256.5, _HEAD))   # 9.6 %: route bends back
    goal = brain._flank_approach_goal(frame, across, state, 105.)
    assert goal is not None
    for step in range(20):
        actor = behind if step % 2 else across
        assert brain._flank_approach_goal(frame, actor, state, 106. + step) == goal
