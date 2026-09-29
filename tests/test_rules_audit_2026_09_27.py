"""Server fixes from the 2026-09-27 gameplay-rules parity audit.

Ground truth for each item is the stock Steam client (aos.pkg class attributes
and the stock constants, several of them referenced by no client binary and
therefore retail SERVER rules). See docs/PARITY_CHANGES_2026-09.md section 24.
"""

from types import SimpleNamespace

import pytest

import shared.constants as C
from server.class_selection import normalize_class_selection
from server.game_constants import TEAM1, TEAM2
from server.player import (
    MAX_FALL_DAMAGE_AIR_TIME,
    ORIENTED_STOCK_AMMO,
    Player,
    ZERO_FALL_DAMAGE_AIR_TIME,
)


def _bare_player():
    player = Player.__new__(Player)
    player.blocks = 0
    player.is_bot = False
    Player._reset_equipment_state(player)
    return player


# -- #1 / #16 / #27: oriented stock ------------------------------------------


def test_spawn_stock_is_the_stock_initial_values_and_rpg2_is_six():
    player = _bare_player()
    stock = player.oriented_stock
    assert stock[int(C.GRENADE_TOOL)] == 2
    assert stock[int(C.MOLOTOV_TOOL)] == 3
    assert stock[int(C.STICKY_GRENADE_TOOL)] == 2
    assert stock[int(C.CHEMICALBOMB_TOOL)] == 2
    assert stock[int(C.RPG_TOOL)] == 4
    # RPG2Weapon.ammo = (3, 3, 3, 3, 3): 3 loaded + 3 reserve.
    assert stock[int(C.RPG2_TOOL)] == 6
    assert stock[int(C.DRILLGUN_TOOL)] == 2
    assert stock[int(C.GRENADE_LAUNCHER_WEAPON_TOOL)] == 4
    assert stock[int(C.MINE_LAUNCHER_TOOL)] == 4


def _crate_expected(tool, total, magazine):
    """Stock Tool/Weapon.restock(AMMO_CRATE) on a (magazine, reserve) pair,
    written the way the native WeaponReplicationState predicts it."""
    mag_max, _mi, reserve_max, _ri, restock = ORIENTED_STOCK_AMMO[tool]
    if reserve_max == 0:
        return min(total + restock, mag_max)
    reserve = total - magazine
    return magazine + min(reserve + restock, reserve_max)


@pytest.mark.parametrize("tool", sorted(ORIENTED_STOCK_AMMO))
def test_ammo_crate_tops_up_instead_of_resetting(tool):
    player = _bare_player()
    player.oriented_stock[tool] = 0
    player._restock_oriented_crate(now=100.0)
    first = player.oriented_stock[tool]
    mag_max, _mi, _rm, _ri, _rs = ORIENTED_STOCK_AMMO[tool]
    assert first == _crate_expected(tool, 0, 0)
    player._restock_oriented_crate(now=100.0)
    # Clip never reloaded by a crate; a second crate only tops up further.
    assert player.oriented_stock[tool] >= first


def test_crate_gives_four_grenades_not_the_spawn_two():
    player = _bare_player()
    player.oriented_stock[int(C.GRENADE_TOOL)] = 1
    player._restock_oriented_crate(now=5.0)
    assert player.oriented_stock[int(C.GRENADE_TOOL)] == 4
    assert player.grenades == 4
    for tool in (C.STICKY_GRENADE_TOOL, C.CHEMICALBOMB_TOOL):
        player.oriented_stock[int(tool)] = 2
    player._restock_oriented_crate(now=5.0)
    assert player.oriented_stock[int(C.STICKY_GRENADE_TOOL)] == 4
    assert player.oriented_stock[int(C.CHEMICALBOMB_TOOL)] == 4


def test_crate_tops_up_launcher_reserve_and_keeps_clip_and_cadence():
    player = _bare_player()
    gl = int(C.GRENADE_LAUNCHER_WEAPON_TOOL)
    assert player.consume_oriented_item(gl, now=10.0)  # 4 -> 3, clip empty
    next_use = player._oriented_next_use[gl]
    player._restock_oriented_crate(now=10.05)
    # clip 0 (no reload fits yet) + min(3 + 5, 5) reserve
    assert player.oriented_stock[gl] == 5
    assert player._oriented_next_use[gl] == next_use
    assert player._launcher_rounds[gl][0] == 0


def test_restock_ammo_crate_path_keeps_state_and_spawn_path_resets():
    player = Player(3, "Crate", TEAM1, C.RIFLE_TOOL, None)
    player.apply_class_selection(
        normalize_class_selection(C.CLASS_SOLDIER, [])
    )
    player.spawn(100.5, 100.5, 60.0)
    player.oriented_stock[int(C.GRENADE_TOOL)] = 0
    player.restock_ammo(int(C.AMMO_CRATE))
    assert player.oriented_stock[int(C.GRENADE_TOOL)] == 4
    player.restock_ammo()
    assert player.oriented_stock[int(C.GRENADE_TOOL)] == 2


def test_rpg2_reload_loads_one_rocket_per_reload_time():
    player = _bare_player()
    rpg2 = int(C.RPG2_TOOL)
    for index in range(3):
        assert player.consume_oriented_item(rpg2, now=10.0 + 0.75 * index)
    last = 10.0 + 1.5
    assert player._launcher_clip_left(rpg2, last + 0.2) == 0
    # First round lands one reload (1.0 s) after the 0.75 s cadence.
    assert player._launcher_clip_left(rpg2, last + 1.5) == 1
    assert player._launcher_clip_left(rpg2, last + 2.5) == 2
    assert player._launcher_clip_left(rpg2, last + 3.5) == 3
    assert player._launcher_clip_left(rpg2, last + 30.0) == 3


def test_magazine_launchers_still_reload_the_whole_clip():
    player = _bare_player()
    rpg = int(C.RPG_TOOL)
    assert player.consume_oriented_item(rpg, now=10.0)
    assert player._launcher_clip_left(rpg, 10.1) == 0
    assert player._launcher_clip_left(rpg, 13.0) == 1


# -- #3: RPG2 knockback --------------------------------------------------------


def test_rpg2_has_no_self_knockback_override():
    from server.projectiles import PROJECTILE_SPECS

    spec = PROJECTILE_SPECS[int(C.RPG2_TOOL)]
    assert spec.self_knockback_min is None
    assert spec.self_knockback_max is None
    assert (spec.knockback_min, spec.knockback_max) == (0.0, 0.25)


# -- #5: water rule --------------------------------------------------------------


def test_water_fall_rule_does_not_switch_off_land_fall_damage(tmp_path):
    from server.config import load_config

    path = tmp_path / "c.toml"
    path.write_text(
        "[game_rules]\nRULE_ENABLE_FALL_ON_WATER_DAMAGE = false\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.fall_damage is True
    assert config.game_rules.enabled("RULE_ENABLE_FALL_ON_WATER_DAMAGE") is False


# -- #6 / #18: fall scaling --------------------------------------------------------


def test_normal_fall_is_unscaled_and_airtime_artefacts_are_zeroed():
    player = _bare_player()
    player._rocket_jump_blast_at = None
    assert player.scaled_fall_damage(40, 1.0) == pytest.approx(40)
    assert player.scaled_fall_damage(40, MAX_FALL_DAMAGE_AIR_TIME) == pytest.approx(40)
    assert player.scaled_fall_damage(40, ZERO_FALL_DAMAGE_AIR_TIME) == pytest.approx(0)
    mid = (ZERO_FALL_DAMAGE_AIR_TIME + MAX_FALL_DAMAGE_AIR_TIME) / 2
    assert player.scaled_fall_damage(40, mid) == pytest.approx(20)


def test_own_rocket_push_scales_the_landing_by_point_two(monkeypatch):
    import server.player as player_module

    player = _bare_player()
    player._rocket_jump_blast_at = None
    player._airborne_since = 100.0
    monkeypatch.setattr(player_module.time, "monotonic", lambda: 100.2)
    player.note_own_blast_push(int(C.KILL.ROCKET_KILL))
    assert player.scaled_fall_damage(50, 2.0) == pytest.approx(10)
    # Grenades are not rocket jumps.
    player._rocket_jump_blast_at = None
    player.note_own_blast_push(int(C.KILL.GRENADE_KILL))
    assert player.scaled_fall_damage(50, 2.0) == pytest.approx(50)
    # A blast long before this take-off does not count.
    player._rocket_jump_blast_at = 90.0
    assert player.scaled_fall_damage(50, 2.0) == pytest.approx(50)


# -- #7: fall-death credit ---------------------------------------------------------


class _Mode:
    def __init__(self):
        self.deaths = []


def _duel_server():
    from server.config import ServerConfig
    from server.main import BattleSpadesServer

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    return server


def _spawned(server, player_id, team):
    player = Player(player_id, f"P{player_id}", team, C.RIFLE_TOOL, None)
    player.spawn(100.5 + player_id, 100.5, 59.75)
    server.players[player_id] = player
    return player


def test_lethal_fall_after_an_enemy_hit_credits_that_enemy():
    server = _duel_server()
    victim = _spawned(server, 1, TEAM1)
    enemy = _spawned(server, 2, TEAM2)
    victim.connection = SimpleNamespace(server=server, send=lambda *a, **k: None,
                                        in_game=True)
    killers = []
    victim.die = lambda killer=None, kill_type=0: killers.append((killer, kill_type))
    victim.damage(10, source=enemy, kill_type=int(C.KILL.WEAPON_KILL))
    victim.damage(500, source=None, kill_type=int(C.KILL.FALL_KILL))
    assert killers == [(enemy, int(C.KILL.FALL_KILL))]


def test_fall_without_a_recent_enemy_hit_stays_unattributed():
    server = _duel_server()
    victim = _spawned(server, 1, TEAM1)
    victim.connection = SimpleNamespace(server=server, send=lambda *a, **k: None,
                                        in_game=True)
    killers = []
    victim.die = lambda killer=None, kill_type=0: killers.append((killer, kill_type))
    victim.damage(500, source=None, kill_type=int(C.KILL.FALL_KILL))
    assert killers == [(None, int(C.KILL.FALL_KILL))]


def test_interaction_windows_use_the_stock_expiry():
    from server import combat_scores
    from server.handlers import team

    assert combat_scores.ASSIST_WINDOW_SECONDS == 5.0
    assert team.TRANSITION_DEATH_CREDIT_WINDOW_SECONDS == 5.0


# -- #9: blast impulse timing --------------------------------------------------------


class _QueuedTarget:
    def __init__(self, player_id, team, x):
        self.id = player_id
        self.team = team
        self.x, self.y, self.z = x, 0.0, 0.0
        self.alive = self.spawned = True
        self.input = SimpleNamespace(crouch=False)
        self.velocity = (0.0, 0.0, 0.0)
        self.queued = []
        self.damage_calls = []

    @property
    def position(self):
        return (self.x, self.y, self.z)

    def queue_explosion_impulse(self, frames, origin, radius, kmin, kmax):
        self.queued.append(frames)
        return 1

    def damage(self, amount, source=None, kill_type=0):
        self.damage_calls.append(amount)


def _blast_server(targets, terrain_sent):
    from server.main import BattleSpadesServer

    class _Registry:
        def all(self):
            return []

    class _Server:
        _apply_blast = BattleSpadesServer._apply_blast

        def __init__(self):
            self.players = {t.id: t for t in targets}
            self.config = SimpleNamespace(build_damage=True, friendly_fire=False)
            self.entity_registry = _Registry()

        def _apply_blast_terrain(self, *a, **k):
            return terrain_sent

        def _blocked_los(self, *a):
            return False

        def _build_entity_ctx(self):
            return None

    return _Server()


def test_stock_blast_push_waits_for_the_damage_packet_frames():
    from server.main import _BLAST_PREDICTION_OBSERVED_FRAMES

    target = _QueuedTarget(2, TEAM2, 2.0)
    server = _blast_server([target], terrain_sent=True)
    server._apply_blast(0, 0, 0, 140, 5, int(C.KILL.ROCKET_KILL), None,
                        terrain_damage_type=8)
    assert target.queued == [_BLAST_PREDICTION_OBSERVED_FRAMES]
    assert target.velocity == (0.0, 0.0, 0.0)


def test_blast_without_a_damage_packet_pushes_immediately():
    target = _QueuedTarget(2, TEAM2, 2.0)
    server = _blast_server([target], terrain_sent=False)
    server._apply_blast(0, 0, 0, 140, 5, int(C.KILL.ROCKET_KILL), None,
                        terrain_damage_type=8)
    assert target.queued == []
    assert target.velocity[0] > 0.0


# -- #22 / #23: damage rules -----------------------------------------------------------


def test_one_hit_kill_only_for_the_stock_weapon_list():
    from server.player import _ONE_HIT_KILL_TYPES

    assert _ONE_HIT_KILL_TYPES == frozenset(
        (0, 1, 2, 3, 4, 5, 6, 21, 22, 23, 24, 18, 13, 14, 15, 16, 17, 19)
    )
    assert int(C.KILL.BLOCKFIRE_KILL) not in _ONE_HIT_KILL_TYPES
    assert int(C.KILL.STICKY_GRENADE_KILL) not in _ONE_HIT_KILL_TYPES


def test_hit_damage_is_not_rounded_before_player_damage():
    from tests.test_weapons_retail import _duel

    _server, attacker, target, combat = _duel(C.SMG_TOOL, C.CLASS_SCOUT)
    value = combat._calculate_damage(
        attacker, attacker.get_weapon_profile(), False, target=target,
        part=C.PART_TORSO,
    )
    assert isinstance(value, float)
    assert value != int(value)


# -- #2 / #19: Territory Control capture ----------------------------------------------


def test_tc_capture_rate_is_the_stock_table():
    import shared.constants_gamemode as CG
    from modes.territory_control import tc_capture_percent_per_tick

    assert CG.TC_CAPTURE_RATE == [(0, 0.0), (1, 1.0), (5, 4.0), (10, 7.0), (15, 9.0)]
    assert tc_capture_percent_per_tick(0) == 0.0
    assert tc_capture_percent_per_tick(1) == 1.0
    assert tc_capture_percent_per_tick(3) == pytest.approx(2.5)
    assert tc_capture_percent_per_tick(5) == 4.0
    assert tc_capture_percent_per_tick(10) == 7.0
    assert tc_capture_percent_per_tick(15) == 9.0
    assert tc_capture_percent_per_tick(30) == 9.0


def test_tc_contested_base_is_frozen_and_every_capturer_is_paid(monkeypatch):
    import asyncio

    import shared.constants_gamemode as CG
    from server.game_constants import TEAM_NEUTRAL
    from tests import test_territory_control as tc_tests

    now = [100.0]
    server, mode = tc_tests._mode(monkeypatch, now)
    middle = mode.territories[1]
    blue_a = tc_tests._player(server, 1, TEAM1, middle.zone.center)
    blue_b = tc_tests._player(server, 2, TEAM1, middle.zone.center)
    green = tc_tests._player(server, 3, TEAM2, middle.zone.center)
    asyncio.run(mode._capture_tick(30.0))
    assert middle.progress == 0.5 and middle.owner == TEAM_NEUTRAL
    green.position = (0.0, 0.0, 0.0)
    # Two capturers: 1.75 % per tick -> a step needs 57.2 ticks (28.6 s).
    asyncio.run(mode._capture_tick(28.0))
    assert middle.owner == TEAM_NEUTRAL
    asyncio.run(mode._capture_tick(1.0))
    assert middle.owner == TEAM1
    assert blue_a.score == int(CG.TC_SCORE_CLAIM)
    assert blue_b.score == int(CG.TC_SCORE_CLAIM)


# -- #8: NEVER_RESPAWN_TIME -------------------------------------------------------------


def test_vip_sudden_death_team_gets_the_never_respawn_sentinel():
    from modes.vip import VIPMode, VIPPhase
    from tests import test_vip as vip_tests

    server = vip_tests._Server()
    mode = VIPMode(server)
    member = SimpleNamespace(id=4, team=TEAM1)
    mode.phase = VIPPhase.ACTIVE
    mode.respawn_enabled = {TEAM1: False, TEAM2: True}
    assert mode.respawn_time_for(member) == float(C.NEVER_RESPAWN_TIME) == 255.0
    # A sub-round reset revives the team shortly: plain zero timer.
    mode.phase = VIPPhase.RESETTING
    assert mode.respawn_time_for(member) == 0.0


# -- #15: VIP rounds played ---------------------------------------------------------------


def test_vip_match_ends_after_the_configured_rounds_played():
    import asyncio

    from modes.vip import VIPMode, VIPPhase
    from tests import test_vip as vip_tests

    server = vip_tests._Server()
    mode = VIPMode(server)
    mode.score_limit = 3
    ends = []

    async def end_by_time():
        ends.append(mode.rounds_played)

    mode._end_by_time = end_by_time

    async def play():
        for winner in (TEAM1, None, TEAM2):
            mode.phase = VIPPhase.ACTIVE
            await mode._finish_round(winner)
            task = getattr(mode, "_round_task", None)
            if task is not None:
                task.cancel()
                mode._round_task = None

    asyncio.run(play())
    assert mode.rounds_played == 3
    assert ends == [3]
    assert server.teams[TEAM1].score == 1 and server.teams[TEAM2].score == 1


# -- #12: CTF claim ---------------------------------------------------------------------


def test_ctf_claim_is_paid_only_for_a_home_grab():
    import asyncio

    import shared.constants_gamemode as CG
    from modes.ctf import CTFMode
    from tests.test_ctf_entities import _Server as CtfServer

    server = CtfServer()
    mode = CTFMode(server)
    asyncio.run(mode.on_mode_start())
    player = SimpleNamespace(
        id=7, name="Blue", team=TEAM1, alive=True, spawned=True,
        x=200.0, y=210.0, z=40.0, vx=0.0, vy=0.0, vz=0.0,
        pickup_id=None, pickup_burdensome=False, pickup_state=None,
        _world_object=None, captures=0, score=0,
    )
    server.players[player.id] = player
    asyncio.run(mode._pickup_intel(player, TEAM2))
    assert player.score == int(CG.CTF_SCORE_CLAIM)
    asyncio.run(mode._drop_intel(player, TEAM2))
    asyncio.run(mode._pickup_intel(player, TEAM2))
    assert player.score == int(CG.CTF_SCORE_CLAIM)


# -- #13: TDM team-play events -------------------------------------------------------------


def _teamplay_mode(monkeypatch, players):
    from modes.tdm import TDMMode

    mode = TDMMode.__new__(TDMMode)
    mode.server = SimpleNamespace(players={p.id: p for p in players})
    paid = []
    monkeypatch.setattr(
        TDMMode, "_add_generic_score",
        lambda self, player, amount, reason: paid.append((player.id, amount, reason)),
    )
    return mode, paid


def test_tdm_reloading_kill_defend_and_distraction(monkeypatch):
    import time as _time

    import shared.constants_gamemode as CG
    from server.combat_scores import DamageContribution

    killer = SimpleNamespace(id=1, team=TEAM1, alive=True)
    mate = SimpleNamespace(id=2, team=TEAM1, alive=True, damage_contributions={})
    victim = SimpleNamespace(id=3, team=TEAM2, alive=False, died_reloading=True)
    mate.damage_contributions[3] = DamageContribution(
        victim, TEAM2, 0, 30.0, _time.monotonic()
    )
    mode, paid = _teamplay_mode(monkeypatch, [killer, mate, victim])
    total = mode._award_teamplay_kill_events(killer, victim)
    assert (1, int(CG.GENERIC_SCORE_RELOAD), int(C.KILL_SCORE_RELOAD_REASON)) in paid
    assert (1, int(CG.GENERIC_SCORE_DEFEND), int(C.KILL_SCORE_DEFEND_REASON)) in paid
    assert (2, int(CG.TDM_SCORE_DISTRACT), int(C.KILL_SCORE_DISTRACT_REASON)) in paid
    assert total == int(CG.GENERIC_SCORE_RELOAD) + int(CG.GENERIC_SCORE_DEFEND)


def test_tdm_plain_kill_pays_no_teamplay_event(monkeypatch):
    killer = SimpleNamespace(id=1, team=TEAM1, alive=True)
    victim = SimpleNamespace(id=3, team=TEAM2, alive=False, died_reloading=False)
    mode, paid = _teamplay_mode(monkeypatch, [killer, victim])
    assert mode._award_teamplay_kill_events(killer, victim) == 0
    assert paid == []


# -- #14: Demolition defend / assault -------------------------------------------------------


@pytest.mark.parametrize("killer_at, victim_at, event", [
    ((10.0, 0.0, 0.0), (5.0, 0.0, 0.0), "defend"),
    ((300.0, 0.0, 0.0), (100.0, 0.0, 0.0), "assault"),
    ((150.0, 0.0, 0.0), (150.0, 40.0, 0.0), None),
])
def test_demolition_kill_events(monkeypatch, killer_at, victim_at, event):
    import asyncio

    from modes import base_mode
    from modes.demolition import DemolitionMode
    from server import combat_scores

    mode = DemolitionMode.__new__(DemolitionMode)
    mode.server = SimpleNamespace(players={})
    mode.ended = False
    mode.phase = "active"
    mode.base_zones = {
        TEAM1: SimpleNamespace(center=(0.0, 0.0, 0.0)),
        TEAM2: SimpleNamespace(center=(300.0, 0.0, 0.0)),
    }

    async def no_generic(self, *_a):
        return None

    monkeypatch.setattr(base_mode.BaseMode, "on_player_kill", no_generic)
    seen = []
    monkeypatch.setattr(
        combat_scores, "award_kill_event",
        lambda server, player, ev, amounts, reasons, mode=None: seen.append(
            (ev, amounts.get(ev), reasons.get(ev))
        ),
    )
    kx, ky, kz = killer_at
    vx, vy, vz = victim_at
    killer = SimpleNamespace(id=1, team=TEAM1, x=kx, y=ky, z=kz, alive=True)
    victim = SimpleNamespace(id=2, team=TEAM2, x=vx, y=vy, z=vz, alive=False)
    asyncio.run(mode.on_player_kill(killer, victim, int(C.KILL.WEAPON_KILL)))
    assert seen[0][0] == event
    if event == "defend":
        assert seen[0][1:] == (100, int(C.SCORE_REASON.DEM_DEFEND_SCORE_REASON))
    if event == "assault":
        assert seen[0][1:] == (50, int(C.SCORE_REASON.DEM_ASSAULT_SCORE_REASON))


# -- #17 / #21: block interval and Classic reach ------------------------------------------


def _combat_with_config(**anticheat):
    from server.combat_runtime import get_combat_system
    from tests.test_reversed_combat import DummyServer

    server = DummyServer()
    server.config.anticheat = SimpleNamespace(**anticheat)
    return server, get_combat_system(server)


def test_block_interval_is_log_only_by_default(monkeypatch):
    import server.combat_runtime as runtime

    server, combat = _combat_with_config(enforce_block_interval=False)
    clock = [10.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    player = SimpleNamespace(id=1, name="B")
    assert combat._block_interval_ok(player, [(1, 1, 1)])
    clock[0] += 0.03
    assert combat._block_interval_ok(player, [(1, 1, 2)])
    assert player.anticheat_counts["block_interval:observed"] == 1


def test_block_interval_enforced_rejects_and_repairs(monkeypatch):
    import server.combat_runtime as runtime

    server, combat = _combat_with_config(enforce_block_interval=True)
    clock = [10.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    repaired = []
    monkeypatch.setattr(combat, "_queue_canonical_terrain_repair",
                        lambda cells: repaired.extend(cells))
    player = SimpleNamespace(id=1, name="B")
    assert combat._block_interval_ok(player, [(1, 1, 1)])
    clock[0] += 0.05
    assert not combat._block_interval_ok(player, [(1, 1, 2)])
    assert repaired == [(1, 1, 2)]
    clock[0] += runtime.MIN_BLOCK_INTERVAL
    assert combat._block_interval_ok(player, [(1, 1, 3)])


def test_bots_pace_their_builds_to_the_block_interval(monkeypatch):
    import server.bot_ai.gateway as gateway

    clock = [50.0]
    monkeypatch.setattr(gateway.time, "monotonic", lambda: clock[0])
    bot = SimpleNamespace(_last_block_build_at=49.95)
    assert not gateway.BotActionGateway._block_interval_ready(bot)
    clock[0] = 50.06
    assert gateway.BotActionGateway._block_interval_ready(bot)


def test_classic_build_reach_is_five_blocks_plus_slack():
    from server.combat_runtime import BUILD_REACH, CLASSIC_BUILD_REACH

    server, combat = _combat_with_config()
    server.mode = SimpleNamespace(mode_code="ctf")
    assert combat._build_reach() == BUILD_REACH
    server.mode = SimpleNamespace(mode_code="cctf")
    assert combat._build_reach() == CLASSIC_BUILD_REACH
    assert CLASSIC_BUILD_REACH == BUILD_REACH - 5.0


# -- #10 / #25: disguise and spawn protection -------------------------------------------------


def test_disguise_breaks_on_walking_or_drift():
    player = _bare_player()
    player.x, player.y, player.z = 10.0, 10.0, 50.0
    player.input = SimpleNamespace(up=False, down=False, left=False, right=False,
                                   jump=False)
    player.disguised = True
    player._disguise_anchor = (10.0, 10.0, 50.0)
    player._check_disguise_stationary()
    assert player.disguised is True
    player.x = 10.3
    player._check_disguise_stationary()
    assert player.disguised is True
    player.input.up = True
    player._check_disguise_stationary()
    assert player.disguised is False
    player.input.up = False
    player.disguised = True
    player.x = 10.8
    player._check_disguise_stationary()
    assert player.disguised is False


def test_deployable_placement_breaks_disguise_and_spawn_protection():
    from server.handlers.deployables import _committed_placement

    calls = []
    player = SimpleNamespace(
        break_disguise=lambda: calls.append("disguise"),
        end_spawn_protection=lambda: calls.append("protection"),
    )
    assert _committed_placement(player, False) is False
    assert calls == []
    assert _committed_placement(player, True) is True
    assert calls == ["disguise", "protection"]


# -- #26: snowblower under infinite blocks ---------------------------------------------------


def test_snowblower_is_free_under_team_infinite_blocks(monkeypatch):
    import server.hud_packets as hud

    player = _bare_player()
    player.team = TEAM1
    player.connection = SimpleNamespace(server=SimpleNamespace())
    monkeypatch.setattr(hud, "team_infinite_blocks", lambda server, team: True)
    player.blocks = 0
    assert player.can_use_oriented_item(C.SNOWBLOWER_TOOL, now=1.0)
    assert player.consume_oriented_item(C.SNOWBLOWER_TOOL, now=1.0)
    assert player.blocks == 0


# -- #29: map vote window ----------------------------------------------------------------------


def test_map_vote_opens_ten_seconds_before_the_end():
    from server import voting

    assert voting.MAP_VOTE_LEAD_SECONDS == 10.0
    assert voting.MAP_VOTE_DURATION == 10.0
