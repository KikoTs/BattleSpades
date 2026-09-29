"""Per-mode bot fixes: Demolition dig/repair, CTF drops, Occupation bombs,
Multi-Hill expiry, Territory sieges, Zombie roles, mode aliases, and the
director's gameplay-thread safety."""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import replace
from types import SimpleNamespace

import shared.constants as C

from shared.packet import PlayerLeft

from server.bot_ai.director import BotDirector
from server.bot_ai.messages import (
    BotActionKind,
    MovementAffordance,
    ObjectiveSnapshot,
    PerceptionFrame,
    PlayerSnapshot,
)
from server.bot_ai.policies import (
    ModePolicyMemory,
    canonical_mode_id,
    objective_decision_for,
)
from server.bot_ai.simple_worker import SimpleBotBrain, _BotState


BLUE, GREEN = 2, 3


def _player(player_id, team, position=(100.0, 100.0, 40.0), *, carried=-1,
            class_id=0, is_bot=True, loadout=(2, 5, 6), blocks=50, life_id=1):
    return PlayerSnapshot(
        player_id=player_id, generation=1, team=team, class_id=class_id,
        alive=True, spawned=True, position=position, eye=position,
        orientation=(1.0, 0.0, 0.0), velocity=(0.0, 0.0, 0.0), health=100,
        tool=6, blocks=blocks, ammo_clip=10, ammo_reserve=30, is_bot=is_bot,
        loadout=loadout, carried_entity_id=carried, life_id=life_id,
    )


def _frame(mode, observer, *players, objectives=(), phase="active", now=None):
    return PerceptionFrame(
        frame_id=1, map_epoch=1, mode_epoch=1, topology_version=0,
        observer_id=observer.player_id, observer_generation=1,
        created_at=time.monotonic() if now is None else now, mode_id=mode,
        players=(observer, *players), objectives=tuple(objectives), mode_phase=phase,
    )


# --------------------------------------------------------------- Demolition

# A Blue teammate holding team slot 0 (the base guard), far from everything.
_MATE = _player(0, BLUE, (10.0, 10.0, 40.0))


def _dem_bases(own_state=0, enemy_state=0, *, repair=(), cells=((300, 300, 40),)):
    own = ObjectiveSnapshot("dem_base", BLUE, (100.0, 100.0, 40.0), state=own_state,
                            bounds=(95, 105, 95, 105, 35, 45), repair_cells=repair)
    enemy = ObjectiveSnapshot("dem_base", GREEN, (300.0, 300.0, 40.0), state=enemy_state,
                              bounds=(295, 305, 295, 305, 35, 45), cells=cells)
    return own, enemy


def test_demolition_attacker_is_ordered_to_demolish_an_enemy_base_block():
    attacker = _player(5, BLUE, (250.0, 250.0, 38.0))
    own, enemy = _dem_bases(cells=((310, 310, 40), (290, 290, 40)))
    decision = objective_decision_for(_frame("dem", attacker, _MATE, objectives=(own, enemy)), attacker)
    assert decision.role == "demolition_assault_base"
    assert decision.directive == "demolish"
    # Stands on the nearer hinted objective block.
    assert decision.position == (290.5, 290.5, 40 - 2.25)


def test_demolition_defender_repairs_a_damaged_base_and_needs_blocks():
    guard = _player(5, BLUE, (100.0, 100.0, 38.0))
    own, enemy = _dem_bases(own_state=10, repair=((101, 101, 41),))
    decision = objective_decision_for(_frame("dem", guard, objectives=(own, enemy)), guard)
    assert decision.role == "demolition_repair_base"
    assert decision.directive == "repair"
    empty = replace(guard, blocks=0)
    assert objective_decision_for(
        _frame("dem", empty, _MATE, objectives=(own, enemy)), empty).directive == "demolish"


def test_demolition_airstrike_escape_flees_the_destroyed_base_even_our_own():
    bot = _player(4, GREEN, (298.0, 300.0, 38.0))
    own_destroyed = ObjectiveSnapshot("dem_base", GREEN, (300.0, 300.0, 40.0), state=100)
    other = ObjectiveSnapshot("dem_base", BLUE, (100.0, 100.0, 40.0), state=20)
    decision = objective_decision_for(
        _frame("dem", bot, objectives=(own_destroyed, other), phase="airstrike"), bot)
    assert decision.role == "demolition_escape_airstrike"
    # Away from the destroyed (own) base, not toward it as before.
    assert decision.position[0] < bot.position[0]


class _Grid:
    """Tiny worker world: explicit solid cells."""

    def __init__(self, cells):
        self.cells = set(cells)

    def solid(self, x, y, z):
        return (int(x), int(y), int(z)) in self.cells

    def has_line_of_sight(self, origin, target):
        return True


def _brain(cells):
    brain = object.__new__(SimpleBotBrain)
    brain.world = _Grid(cells)
    brain._states = {}
    return brain


def test_worker_digs_the_block_its_swing_actually_meets():
    brain = _brain({(302, 300, 40), (301, 300, 40)})
    base = ObjectiveSnapshot("dem_base", GREEN, (300.0, 300.0, 40.0),
                             bounds=(295, 305, 295, 305, 35, 45), cells=((302, 300, 40),))
    eye = (299.5, 300.5, 40.5)
    # The hinted block is behind another block of the base: dig that first.
    assert brain._demolish_cell(eye, base, {}) == (301, 300, 40)
    assert brain._demolish_cell((290.5, 300.5, 40.5), base, {}) is None


def test_worker_repairs_only_face_supported_empty_cells():
    brain = _brain({(101, 101, 42)})
    floating = (104, 104, 38)
    supported = (101, 101, 41)
    base = ObjectiveSnapshot("dem_base", BLUE, (100.0, 100.0, 40.0),
                             repair_cells=(floating, supported))
    observer = _player(1, BLUE, (102.5, 102.5, 39.0))
    frame = _frame("dem", observer, objectives=(base,))
    cell = brain._repair_cell(frame, observer, (102.5, 102.5, 39.0), base, {})
    assert cell == supported


def test_worker_emits_breach_melee_and_build_actions_for_block_work():
    brain = _brain({(301, 300, 40), (101, 101, 42)})
    enemy = ObjectiveSnapshot("dem_base", GREEN, (300.0, 300.0, 40.0),
                              bounds=(295, 305, 295, 305, 35, 45))
    own = ObjectiveSnapshot("dem_base", BLUE, (100.0, 100.0, 40.0),
                            repair_cells=((101, 101, 41),))
    digger = replace(_player(1, BLUE, (299.5, 300.5, 40.5)), eye=(299.5, 300.5, 40.5))
    frame = _frame("dem", digger, _MATE, objectives=(replace(own, repair_cells=()), enemy))
    state = _BotState(1, 1, 1)
    decision = objective_decision_for(frame, digger)
    brain._set_goal = lambda *args, **kwargs: None
    dig = brain._objective_block_work_intent(frame, digger, state, 10.0, decision)
    assert dig.action.kind is BotActionKind.MELEE
    assert dig.movement.affordance is MovementAffordance.BREACH
    assert tuple(int(v) for v in dig.action.position) == (301, 300, 40)
    # Between swings the bot holds its aim instead of wandering off.
    hold = brain._objective_block_work_intent(frame, digger, state, 10.1, decision)
    assert hold.action.kind is BotActionKind.NONE and hold.look is not None

    builder = replace(_player(4, BLUE, (102.5, 102.5, 39.0)), eye=(102.5, 102.5, 39.0))
    frame = _frame("dem", builder, objectives=(replace(own, state=5), enemy))
    decision = objective_decision_for(frame, builder)
    assert decision.directive == "repair"
    build = brain._objective_block_work_intent(frame, builder, _BotState(1, 1, 1), 10.0, decision)
    assert build.action.kind is BotActionKind.BUILD
    assert build.action.position == (101.0, 101.0, 41.0)


def test_director_publishes_demolition_bounds_and_cell_hints():
    zone = SimpleNamespace(center=(10.0, 10.0, 5.0), bounds=(8, 12, 8, 12, 4, 6))
    mode = SimpleNamespace(
        mode_code="dem", base_zones={BLUE: zone, GREEN: zone},
        objective_cells={BLUE: {(9, 9, 5), (10, 10, 5), (11, 11, 6)}, GREEN: {(9, 9, 5)}},
        destroyed_cells={BLUE: {(10, 10, 5)}, GREEN: set()},
        _authored_base={BLUE: True, GREEN: False},
    )
    director = object.__new__(BotDirector)
    director.server = SimpleNamespace(mode=mode, world_manager=SimpleNamespace(), players={})
    bases = {item.team: item for item in director._snapshot_objectives()
             if item.kind == "dem_base"}
    assert bases[BLUE].state == 33
    assert bases[BLUE].bounds == zone.bounds
    assert set(bases[BLUE].cells) == {(9, 9, 5), (11, 11, 6)}
    assert bases[BLUE].repair_cells == ((10, 10, 5),)
    assert bases[GREEN].repair_cells == ()
    # A dry fallback base only counts its hinted core: no misleading volume.
    assert bases[GREEN].bounds == () and bases[GREEN].cells == ((9, 9, 5),)


# ---------------------------------------------------------------------- CTF

def test_classic_ctf_bots_retake_a_visible_dropped_intel():
    observer = _player(2, BLUE, (100.0, 100.0, 40.0))
    own_base = ObjectiveSnapshot("ctf_base", BLUE, (20.0, 30.0, 40.0))
    dropped = ObjectiveSnapshot("ctf_intel", GREEN, (160.0, 130.0, 40.0), state=1)
    for mode in ("cctf", "classic_ctf", "Classic CTF", "ClassicCTF"):
        decision = objective_decision_for(
            _frame(mode, observer, objectives=(own_base, dropped)), observer)
        assert decision.role == "classic_ctf_attack_intel", mode
        assert decision.position == dropped.position


def test_ctf_nearest_teammates_recover_a_dropped_friendly_intel():
    near = _player(2, BLUE, (100.0, 100.0, 40.0))
    nearer = _player(3, BLUE, (104.0, 100.0, 40.0))
    far = _player(4, BLUE, (300.0, 300.0, 40.0))
    own_intel = ObjectiveSnapshot("ctf_intel", BLUE, (110.0, 100.0, 40.0), state=1)
    enemy_intel = ObjectiveSnapshot("ctf_intel", GREEN, (400.0, 400.0, 40.0))
    objectives = (own_intel, enemy_intel)
    assert objective_decision_for(
        _frame("ctf", near, nearer, far, objectives=objectives), near
    ).role == "ctf_recover_intel"
    assert objective_decision_for(
        _frame("ctf", far, near, nearer, objectives=objectives), far
    ).role != "ctf_recover_intel"


def test_mode_aliases_canonicalise_config_and_class_names():
    assert canonical_mode_id("DiamondMine") == "dia"
    assert canonical_mode_id("TerritoryControl") == "tc"
    assert canonical_mode_id("classic-ctf") == "cctf"
    assert canonical_mode_id("Multi Hill") == "mh"
    assert canonical_mode_id("zombie") == "zom"
    director = object.__new__(BotDirector)

    class DiamondMineMode:  # class-name fallback, no mode_code
        pass

    director.server = SimpleNamespace(mode=DiamondMineMode(), config=SimpleNamespace(game_mode=""))
    assert director._mode_id() == "dia"
    director.server = SimpleNamespace(mode=SimpleNamespace(mode_code="cctf"),
                                      config=SimpleNamespace(game_mode="tdm"))
    assert director._mode_id() == "cctf"  # the running mode wins over stale config
    director.server = SimpleNamespace(mode=None, config=SimpleNamespace(game_mode="classic_ctf"))
    assert director._mode_id() == "cctf"


# --------------------------------------------------------------- Occupation

def test_occupation_defender_disposes_toward_one_anchored_point():
    target = ObjectiveSnapshot("oc_target", GREEN, (400.0, 250.0, 40.0))
    carrier = _player(8, GREEN, (380.0, 250.0, 40.0), carried=int(C.BOMB_PICKUP), life_id=3)
    memory = ModePolicyMemory()
    first = memory.decide(_frame("oc", carrier, objectives=(target,)), carrier)
    assert first.role == "occupation_dispose_bomb"
    moved = replace(carrier, position=first.position)
    later = objective_decision_for(_frame("oc", moved, objectives=(target,)), moved)
    # Arriving at the point must not push the goal another 30 blocks out.
    assert later.position == first.position
    assert abs(first.position[0] - 400.0) >= 44.0 - 1e-6


def test_occupation_defenders_leave_fresh_bombs_unless_an_attacker_is_near():
    target = ObjectiveSnapshot("oc_target", GREEN, (400.0, 250.0, 40.0))
    bomb = ObjectiveSnapshot("oc_bomb", 1, (250.0, 250.0, 40.0))
    defender = _player(7, GREEN, (380.0, 250.0, 40.0))
    calm = objective_decision_for(_frame("oc", defender, objectives=(target, bomb)), defender)
    assert calm.role == "occupation_defend_base"
    attacker = _player(1, BLUE, (240.0, 250.0, 40.0))
    contested = objective_decision_for(
        _frame("oc", defender, attacker, objectives=(target, bomb)), defender)
    assert contested.role == "occupation_deny_bomb"
    armed = replace(bomb, position=(390.0, 250.0, 40.0), state=1)
    assert objective_decision_for(
        _frame("oc", defender, objectives=(target, armed)), defender
    ).role == "occupation_intercept_live_bomb"


def test_occupation_attackers_escort_their_carrier_and_hunt_a_defender_carrier():
    target = ObjectiveSnapshot("oc_target", GREEN, (400.0, 250.0, 40.0))
    carrier = _player(1, BLUE, (200.0, 250.0, 40.0), carried=int(C.BOMB_PICKUP))
    escort = _player(2, BLUE, (190.0, 250.0, 40.0))
    ours = ObjectiveSnapshot("oc_bomb", BLUE, carrier.position, carrier_id=1)
    assert objective_decision_for(
        _frame("oc", escort, carrier, objectives=(target, ours)), escort
    ).role == "occupation_escort_carrier"
    theirs = ObjectiveSnapshot("oc_bomb", GREEN, (350.0, 250.0, 40.0), carrier_id=9)
    hunt = objective_decision_for(_frame("oc", escort, objectives=(target, theirs)), escort)
    assert hunt.role == "occupation_hunt_carrier"
    assert hunt.position == theirs.position


# ------------------------------------------------------- goal hysteresis (5)

def test_policy_memory_follows_a_moving_hunt_target():
    infected = _player(4, GREEN, (100.0, 100.0, 40.0), class_id=int(C.CLASS_ZOMBIE))
    survivor = _player(1, BLUE, (140.0, 100.0, 40.0))
    memory = ModePolicyMemory()
    now = time.monotonic()
    first = memory.decide(_frame("zom", infected, survivor, now=now), infected)
    assert first.role == "zombie_hunt_survivor"
    fled = replace(survivor, position=(140.0, 160.0, 40.0))
    second = memory.decide(_frame("zom", infected, fled, now=now + 1.0), infected)
    assert second.position == fled.position


# ------------------------------------------------------------------- Zombie

def test_infected_without_survivors_in_sight_breaches_and_never_fortifies():
    infected = _player(4, GREEN, (400.0, 400.0, 40.0), class_id=int(C.CLASS_ZOMBIE))
    own = ObjectiveSnapshot("team_anchor", GREEN, (450.0, 450.0, 40.0))
    enemy = ObjectiveSnapshot("team_anchor", BLUE, (60.0, 60.0, 40.0))
    decision = objective_decision_for(_frame("zom", infected, objectives=(own, enemy)), infected)
    assert decision.role == "zombie_infected_breach"
    assert decision.directive == ""
    assert decision.position[0] < 200.0  # sweeps the survivors' side


# --------------------------------------------------------------- Multi-Hill

def test_multihill_raiders_prefer_a_hostile_hill_over_the_nearest_owned_one():
    guard = _player(2, BLUE, (100.0, 100.0, 40.0))
    raider = _player(3, BLUE, (104.0, 100.0, 40.0))
    owned = ObjectiveSnapshot("mh_hill", BLUE, (100.0, 104.0, 40.0))
    hostile = ObjectiveSnapshot("mh_hill", GREEN, (200.0, 100.0, 40.0))
    frame = _frame("mh", raider, guard, objectives=(owned, hostile))
    raid = objective_decision_for(frame, raider)
    assert raid.role == "multihill_claim" and raid.position == hostile.position
    assert objective_decision_for(
        _frame("mh", guard, raider, objectives=(owned, hostile)), guard
    ).role == "multihill_defend"


def test_multihill_bots_clear_a_hill_before_its_expiry_airstrike():
    bot = _player(2, BLUE, (101.0, 100.0, 40.0))
    hill = ObjectiveSnapshot("mh_hill", BLUE, (100.0, 100.0, 40.0), expires_in=4.0)
    evade = objective_decision_for(_frame("mh", bot, objectives=(hill,)), bot)
    assert evade.role == "multihill_evade_airstrike"
    far = _player(2, BLUE, (200.0, 100.0, 40.0))
    later = replace(hill, team=GREEN, expires_in=10.0)
    fresh = ObjectiveSnapshot("mh_hill", GREEN, (300.0, 100.0, 40.0), expires_in=10.0)
    assert objective_decision_for(
        _frame("mh", far, objectives=(later, fresh)), far
    ).role == "multihill_await_rotation"


def test_director_publishes_multihill_expiry():
    zone = SimpleNamespace(index=0, center=(1.0, 2.0, 3.0), bounds=(0, 2, 0, 4, 1, 5))
    mode = SimpleNamespace(active_zones=[zone], zone_owner={0: None}, zone_contested={0: False},
                           _next_rotation_at=time.time() + 30.0)
    director = object.__new__(BotDirector)
    director.server = SimpleNamespace(mode=mode, world_manager=SimpleNamespace(), players={})
    hill = next(item for item in director._snapshot_objectives() if item.kind == "mh_hill")
    assert 28.0 <= hill.expires_in <= 30.0
    assert hill.bounds == zone.bounds


# ---------------------------------------------------------- Territory Control

def test_territory_defender_relieves_an_uncontested_siege():
    defender = _player(4, BLUE, (50.0, 50.0, 40.0))
    quiet = ObjectiveSnapshot("tc_territory", BLUE, (60.0, 50.0, 40.0))
    besieged = ObjectiveSnapshot("tc_territory", BLUE, (200.0, 50.0, 40.0),
                                 attacker=GREEN, progress=0.2)
    decision = objective_decision_for(
        _frame("tc", defender, objectives=(quiet, besieged)), defender)
    assert decision.role == "territory_relieve_siege"
    assert decision.position == besieged.position


def test_director_publishes_territory_progress_and_attacker():
    territory = SimpleNamespace(zone=SimpleNamespace(center=(5.0, 6.0, 7.0)), owner=BLUE,
                                attacker=GREEN, progress=0.3, contested=False)
    mode = SimpleNamespace(mode_code="tc", territories=[territory])
    director = object.__new__(BotDirector)
    director.server = SimpleNamespace(mode=mode, world_manager=SimpleNamespace(), players={})
    item = next(item for item in director._snapshot_objectives() if item.kind == "tc_territory")
    assert item.attacker == GREEN and abs(item.progress - 0.3) < 1e-9


# ------------------------------------------------------------ director safety

def test_one_broken_mode_block_costs_only_its_objective():
    class Exploding:
        def __iter__(self):
            raise RuntimeError("mode refactor")

    mode = SimpleNamespace(
        mode_code="tc", territories=[SimpleNamespace(zone=None)],
        active_zones=[SimpleNamespace(index="x", center=None)], zone_owner={},
        vips={2: SimpleNamespace(id=4, alive=True, spawned=True, position=(1.0, 2.0, 3.0))},
        bombs=Exploding(),
    )
    director = object.__new__(BotDirector)
    director.server = SimpleNamespace(mode=mode, world_manager=SimpleNamespace(), players={})
    kinds = [item.kind for item in director._snapshot_objectives()]
    assert kinds == ["vip"]


def test_refuge_region_atlas_loads_off_the_gameplay_thread(tmp_path, monkeypatch):
    import threading
    from server.bot_ai import navigation_atlas

    gate = threading.Event()
    calls = []

    def slow_read(*args, **kwargs):
        calls.append(threading.current_thread().name)
        gate.wait(5.0)
        raise ValueError("no cache")

    monkeypatch.setattr(navigation_atlas.NavigationAtlas, "from_cache_bytes", slow_read)
    (tmp_path / "Map.botnav").write_bytes(b"x")
    monkeypatch.setattr(navigation_atlas, "cache_path", lambda directory, name: tmp_path / "Map.botnav")
    director = object.__new__(BotDirector)
    director.server = SimpleNamespace(world_manager=SimpleNamespace(
        map_name="Map", map_file_crc=1, map_raw_bytes=b"vxl", maps_path=str(tmp_path)))
    started = time.perf_counter()
    assert director._refuge_region_lookup() is None
    assert time.perf_counter() - started < 1.0  # did not wait for the read
    assert director._refuge_region_lookup() is None  # still loading
    gate.set()
    director._refuge_regions_loading[1]["done"].wait(5.0)
    assert director._refuge_region_lookup() is None  # no cache: heights only
    assert calls and calls[0] == "BotRefugeRegions"


def test_bot_removal_runs_mode_leave_hook_before_player_left():
    from modes.tdm import TDMMode
    from server.config import ServerConfig
    from server.main import BattleSpadesServer

    async def scenario():
        config = ServerConfig()
        config.bots.max_bots = 1
        supervisor = SimpleNamespace(start=lambda _: None, discard_timeline=lambda: None,
                                     request_restart=lambda: None, close=lambda: None)
        server = BattleSpadesServer(config)
        server.world_manager.generate_flat_map()
        server.mode = TDMMode(server)
        await server.mode.on_mode_start()
        director = BotDirector(server, supervisor=supervisor)
        server.bots = director
        await director.start(initial_count=1)
        order = []
        original_broadcast = server.broadcast

        def broadcast(data, *args, **kwargs):
            if data and data[0] == PlayerLeft.id:
                order.append("left")
            return original_broadcast(data, *args, **kwargs)

        async def leave(player):
            order.append("mode")

        server.broadcast = broadcast
        server.mode.on_player_leave = leave
        try:
            assert await director.remove_bot(director.bots[0], force=True)
        finally:
            await director.close()
        return order

    order = asyncio.run(scenario())
    assert order == ["mode", "left"]


def test_release_gate_checks_the_production_brain():
    from server import release_check

    source = inspect.getsource(release_check._check_worker_spawn)
    assert "simple_worker import run_worker" in source
    assert "server.bot_ai.worker import" not in source


def test_role_splits_are_per_team_not_global_player_id_parity():
    # Bots join alternately: Blue gets even ids, Green odd ids. A global
    # ``id % 4`` split gave Blue every base guard and Green none.
    players = [_player(pid, BLUE if pid % 2 == 0 else GREEN, (150.0 + pid, 150.0, 40.0))
               for pid in range(10)]
    own_blue, own_green = _dem_bases()
    guards = {BLUE: 0, GREEN: 0}
    for observer in players:
        others = [player for player in players if player is not observer]
        decision = objective_decision_for(
            _frame("dem", observer, *others, objectives=(own_blue, own_green)), observer)
        guards[observer.team] += decision.role == "demolition_defend_base"
    assert guards[BLUE] >= 1 and guards[GREEN] >= 1
