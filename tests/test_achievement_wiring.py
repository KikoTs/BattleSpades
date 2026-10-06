"""The achievement hooks in the real combat path.

test_achievement_rules.py feeds the engine by hand; these tests drive the
server code that calls it (Player.damage/die, the shared blast, the collapse
pass, projectiles, turrets, crates and the tick) so a moved or dropped hook
fails here.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import shared.constants as C

from server import achievements
from server.combat_runtime import CombatSystem
from server.config import ServerConfig
from server.main import BattleSpadesServer
from server.projectiles import Explosion, ProjectileEngine
from tests import test_killfeed_retail as kf
from tests import test_projectiles as pj
from tests import test_rocket_turret as rt
from tests.achievement_helpers import (
    HEADSHOT, TEAM1, TEAM2, WEAPON, announcements, attach_engine, make_player,
    make_server, progress, unlocked,
)

FALL = int(C.FALL_KILL)


class _Registry:
    def all(self):
        return []


class _Server(kf._Server):
    """Real players and world; the real shared blast."""

    _apply_blast = BattleSpadesServer._apply_blast

    def __init__(self):
        super().__init__()
        self.mode = None
        self.config.build_damage = False
        self.entity_registry = _Registry()
        attach_engine(self)

    def _blocked_los(self, *_coordinates):
        return False

    def _build_entity_ctx(self):
        return None


def _player(server, player_id, team, *, tool=C.RIFLE_TOOL, x=None):
    player = kf._player(server, player_id, team)
    player.tool = int(tool)
    # These tests are about the hit, not the spawn shield.
    player.spawn_protection_cancelled = True
    if x is not None:
        player.x = float(x)
    return player


def _respawn(player):
    player.spawn(100.5 + player.id, 100.5, 60.0)
    player.spawn_protection_cancelled = True


# ---------------------------------------------------------------------------
# Player.damage / Player.die
# ---------------------------------------------------------------------------

def test_a_real_headshot_kill_reaches_the_engine_with_the_killers_tool():
    server = _Server()
    killer = _player(server, 1, TEAM1, tool=C.SNIPER_TOOL)
    victim = _player(server, 2, TEAM2)
    assert victim.damage(40, source=killer, kill_type=HEADSHOT) is False
    assert progress(server, killer, "sniper_kill_count") == 0
    assert victim.damage(500, source=killer, kill_type=HEADSHOT) is True
    assert not victim.alive
    assert progress(server, killer, "sniper_kill_count") == 1
    assert server.achievements.faults == {}


def test_the_victims_jetpack_is_read_before_death_clears_it():
    server = _Server()
    killer = _player(server, 1, TEAM1)
    victim = _player(server, 2, TEAM2)
    victim.jetpack_active = True
    victim.damage(500, source=killer, kill_type=WEAPON)
    assert victim.jetpack_active is False
    assert "jetpack_killed_using" in unlocked(server, killer)
    assert announcements(server) == [("ACHIEVEMENT_GAINED", ["P1", "Jet Fighter"])]


def test_five_real_kills_in_a_row_and_the_death_that_resets_them():
    server = _Server()
    killer = _player(server, 1, TEAM1)
    victim = _player(server, 2, TEAM2)
    for _ in range(4):
        victim.damage(500, source=killer, kill_type=WEAPON)
        _respawn(victim)
    killer.damage(500, source=victim, kill_type=WEAPON)
    _respawn(killer)
    assert killer.kill_streak == 0
    for _ in range(4):
        victim.damage(500, source=killer, kill_type=WEAPON)
        _respawn(victim)
    assert unlocked(server, killer) == set()
    victim.damage(500, source=killer, kill_type=WEAPON)
    assert unlocked(server, killer) == {"misc_five_in_a_row"}


def test_low_health_uses_the_killers_real_health():
    server = _Server()
    killer = _player(server, 1, TEAM1)
    victim = _player(server, 2, TEAM2)
    killer.damage(91, source=victim, kill_type=WEAPON)
    assert killer.health == 9
    victim.damage(500, source=killer, kill_type=WEAPON)
    assert progress(server, killer, "low_health_kills") == 1


def test_a_fall_after_a_rocket_hit_is_credited_through_the_real_fall_path():
    server = _Server()
    killer = _player(server, 1, TEAM1)
    victim = _player(server, 2, TEAM2)
    victim.damage(40, source=killer, kill_type=int(C.ROCKET_KILL))
    assert victim.alive
    # World damage: the server credits the last enemy who hurt this life.
    victim.damage(500, source=None, kill_type=FALL)
    assert not victim.alive
    assert unlocked(server, killer) == {"rocket_fall"}

    # A fall with nobody to credit, and a fall after a rifle hit.
    _respawn(victim)
    victim.damage(500, source=None, kill_type=FALL)
    _respawn(victim)
    other = _player(server, 3, TEAM1)
    victim.damage(40, source=other, kill_type=WEAPON)
    victim.damage(500, source=None, kill_type=FALL)
    assert unlocked(server, other) == set()


def test_team_kills_and_class_changes_do_not_count_on_the_real_path():
    server = _Server()
    server.config.friendly_fire = True
    killer = _player(server, 1, TEAM1, tool=C.SNIPER_TOOL)
    mate = _player(server, 2, TEAM1)
    enemy = _player(server, 3, TEAM2)
    mate.die(killer=killer, kill_type=HEADSHOT)
    enemy.die(killer=killer, kill_type=int(C.CLASS_CHANGE_KILL))
    assert progress(server, killer, "sniper_kill_count") == 0


def test_a_modes_death_presentation_does_not_hide_the_weapon():
    """VIP reports a boss's death as VIP_MODE_KILL; the bullet was a headshot."""
    server = _Server()
    server.mode = SimpleNamespace(
        ended=False,
        death_kill_type_for=lambda player, killer, kill_type: int(C.KILL.VIP_MODE_KILL),
    )
    killer = _player(server, 1, TEAM1, tool=C.SNIPER_TOOL)
    boss = _player(server, 2, TEAM2)
    boss.damage(500, source=killer, kill_type=HEADSHOT)
    assert kf._last_kill(server).kill_type == int(C.KILL.VIP_MODE_KILL)
    assert progress(server, killer, "sniper_kill_count") == 1
    # A boss leaving its team is still a transition, not a kill.
    _respawn(boss)
    boss.die(killer=killer, kill_type=int(C.TEAM_CHANGE_KILL))
    assert progress(server, killer, "sniper_kill_count") == 1
    assert server.achievements.tracker(killer).round["kills"] == 1


def test_an_engine_fault_cannot_break_a_kill():
    server = _Server()
    killer = _player(server, 1, TEAM1)
    victim = _player(server, 2, TEAM2)

    def boom(*_args, **_kwargs):
        raise RuntimeError("achievement bug")

    server.achievements.died = boom
    server.achievements.damaged = boom
    assert victim.damage(500, source=killer, kill_type=WEAPON) is True
    assert not victim.alive and killer.kills == 1
    assert server.achievements.faults == {"damaged": 1, "died": 1}


def test_real_players_without_an_engine_are_untouched():
    server = kf._Server()
    killer = kf._player(server, 1, TEAM1)
    victim = kf._player(server, 2, TEAM2)
    victim.die(killer=killer, kill_type=WEAPON)
    assert not hasattr(killer, "achievement_tracker")
    assert not hasattr(victim, "achievement_tracker")


# ---------------------------------------------------------------------------
# The shared blast
# ---------------------------------------------------------------------------

def test_apply_blast_is_scoped_and_three_real_kills_unlock_the_big_bang():
    assert BattleSpadesServer._apply_blast.__wrapped__.__name__ == "_apply_blast"
    server = _Server()
    thrower = _player(server, 1, TEAM1, x=300.0)
    enemies = [_player(server, index, TEAM2, x=100.5 + 0.2 * index) for index in (2, 3, 4)]
    spot = enemies[1].position
    server._apply_blast(
        spot[0], spot[1], spot[2], 230.0, 0.0, int(C.GRENADE_KILL), thrower,
        blast_radius=4.0,
    )
    assert [enemy.alive for enemy in enemies] == [False, False, False]
    assert "misc_triple_explosion" in unlocked(server, thrower)
    assert server.achievements._blasts == []
    assert server.achievements.faults == {}


def test_two_real_blasts_of_two_are_not_one_of_three():
    server = _Server()
    thrower = _player(server, 1, TEAM1, x=300.0)
    enemies = [_player(server, index, TEAM2, x=100.5 + 0.2 * index) for index in (2, 3)]
    for _ in range(2):
        spot = enemies[0].position
        server._apply_blast(
            spot[0], spot[1], spot[2], 230.0, 0.0, int(C.GRENADE_KILL), thrower,
            blast_radius=4.0,
        )
        assert not any(enemy.alive for enemy in enemies)
        for enemy in enemies:
            _respawn(enemy)
            enemy.x = 100.5 + 0.2 * enemy.id
    assert "misc_triple_explosion" not in unlocked(server, thrower)


def test_a_real_dynamite_blast_from_under_the_floor():
    server = _Server()
    thrower = _player(server, 1, TEAM1, x=300.0)
    victim = _player(server, 2, TEAM2)
    feet = victim.z + float(C.PLAYER_STANDING_POS_ABOVE_GROUND)
    # Skip the terrain between the charge and the body: the rule under test
    # is where the charge was, not the blast's line of sight.
    server._apply_blast(
        victim.x, victim.y, feet + 1.0, 300.0, 0.0, int(C.DYNAMITE_KILL), thrower,
        blast_radius=6.0, ignore_player_los=True,
    )
    assert not victim.alive
    assert progress(server, thrower, "dynamite_below_count") == 1
    _respawn(victim)
    server._apply_blast(
        victim.x, victim.y, victim.z - 1.0, 300.0, 0.0, int(C.DYNAMITE_KILL), thrower,
        blast_radius=6.0, ignore_player_los=True,
    )
    assert not victim.alive
    assert progress(server, thrower, "dynamite_below_count") == 1


# ---------------------------------------------------------------------------
# Terrain removal and the shot tally
# ---------------------------------------------------------------------------

def _tower_server():
    server = _Server()
    world = server.world_manager
    # The one-voxel debug ground is itself "floating"; keep collapse to the
    # structure under test.
    detect = world.find_unsupported_chunks
    world.find_unsupported_chunks = lambda removed: [
        chunk for chunk in detect(removed) if len(chunk) < 1000
    ]
    tower = [(100, 100, z) for z in range(2, 62)]
    for cell in tower:
        assert world.set_block(*cell, True, 0x7F808080)
    return server, world, tower


def test_a_minigun_bullet_that_fells_a_tower_reports_the_collapse():
    server, world, tower = _tower_server()
    gunner = _player(server, 1, TEAM1, tool=C.MINIGUN_TOOL)
    combat = CombatSystem(server)
    base = tower[-1]
    assert world.destroy_blocks([base])
    combat._begin_shot_tally(gunner)
    combat._collapse_unsupported(gunner, [base])
    combat._end_shot_tally(gunner)
    assert not any(world.get_solid(*cell) for cell in tower)
    assert "minigun_demolish" in unlocked(server, gunner)


def test_the_same_collapse_outside_a_minigun_shot_is_not_the_minigun():
    server, world, tower = _tower_server()
    gunner = _player(server, 1, TEAM1, tool=C.MINIGUN_TOOL)
    other = _player(server, 2, TEAM1, tool=C.MINIGUN_TOOL)
    combat = CombatSystem(server)
    base = tower[-1]
    assert world.destroy_blocks([base])
    # Someone else's shot is being resolved while this removal is committed.
    combat._begin_shot_tally(other)
    combat._collapse_unsupported(gunner, [base])
    combat._end_shot_tally(other)
    assert unlocked(server, gunner) == set() == unlocked(server, other)


def test_a_real_grenade_collapse_counts_a_felled_structure():
    server, world, tower = _tower_server()
    for cell in [(101, 100, z) for z in range(2, 61)]:
        assert world.set_block(*cell, True, 0x7F808080)
    thrower = _player(server, 1, TEAM1, x=300.0)
    combat = CombatSystem(server)
    base = tower[-1]
    engine = server.achievements
    assert world.destroy_blocks([base])
    scope = engine.blast_begin(thrower, int(C.GRENADE_KILL), (100.5, 100.5, 61.5))
    combat._collapse_unsupported(thrower, [base])
    engine.blast_end(scope)
    # 59 cells of the first column plus the 59 hanging beside it.
    assert progress(server, thrower, "grenade_demolish_count") == 1


def test_the_shot_tally_reports_a_sniper_miss():
    server = _Server()
    sniper = _player(server, 1, TEAM1, tool=C.SNIPER_TOOL)
    victim = _player(server, 2, TEAM2)
    combat = CombatSystem(server)
    engine = server.achievements
    for _ in range(2):
        combat._begin_shot_tally(sniper)
        victim.damage(500, source=sniper, kill_type=HEADSHOT)
        combat._note_player_hit(sniper, True)
        combat._end_shot_tally(sniper)
        _respawn(victim)
    assert engine.tracker(sniper).sniper_streak == 2
    combat._begin_shot_tally(sniper)
    combat._end_shot_tally(sniper)          # a shot that hit nobody
    assert engine.tracker(sniper).sniper_streak == 0


@pytest.mark.parametrize("live_entity,expected", [(True, 81), (False, 1)])
def test_a_real_drill_bore_is_attributed_to_the_drill_gun(live_entity, expected):
    """The bore is not a blast and never reaches the mode's queued hook."""
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    server.world_manager.find_unsupported_chunks = lambda _positions: []
    attach_engine(server)
    server.mode = SimpleNamespace(
        ended=False, mode_code="dem",
        enemy_objective_damage=lambda player, cells: (TEAM2, len(cells)),
    )
    connection = pj.RecordingConnection()
    connection.server = server
    owner = pj.Player(3, "Miner", TEAM1, C.DRILLGUN_TOOL, connection)
    connection.player = owner
    server.players[owner.id] = owner
    server.connections[owner.id] = connection

    block = (100, 100, 50)
    for position in pj.drill_contact_cells(block) if live_entity else (block,):
        server.world_manager.set_block(*position, True, 0x123456)
    projectile = server.projectile_engine.spawn(
        int(C.DRILLGUN_TOOL), (99.0, 100.0, 50.0), (20.0, 0.0, 0.0), 0.0, owner.id, now=0.0,
    )
    if live_entity:
        entity = server.entity_registry.place(
            int(C.DRILL_ENTITY), 99.0, 100.0, 50.0, kind="projectile", player_id=owner.id,
        )
        projectile.entity_id = entity.entity_id
    server._apply_drill_contact(pj.DrillContact(projectile, block))

    assert progress(server, owner, "drillgun_demolition_count") == expected
    assert progress(server, owner, "demolition_damage_many_rounds_count") == expected
    assert server.achievements._blasts == []
    assert server.achievements.faults == {}


# ---------------------------------------------------------------------------
# Projectiles and turrets
# ---------------------------------------------------------------------------

def _fly(engine, world, players):
    time = 0.0
    for _ in range(30):
        time += pj.DT
        events = engine.update(pj.DT, world, now=time, players=players)
        if events:
            return events
    return []


def test_a_rocket_records_the_player_it_struck_directly():
    engine = ProjectileEngine()
    engine.spawn(int(C.RPG_TOOL), (100.0, 100.0, 30.0), (75.0, 0.0, 0.0), 0.0, 1, now=0.0)
    target = SimpleNamespace(id=2, alive=True, spawned=True, x=106.0, y=100.0, z=29.0,
                             input=SimpleNamespace(crouch=False))
    (explosion,) = _fly(engine, pj.OpenWorld(), [target])
    assert isinstance(explosion, Explosion)
    assert explosion.contact_player_id == 2
    assert explosion.turret is None and explosion.turret_target_id is None


def test_a_rocket_that_hits_terrain_struck_nobody():
    engine = ProjectileEngine()
    engine.spawn(int(C.RPG_TOOL), (100.0, 100.0, 30.0), (75.0, 0.0, 0.0), 0.0, 1, now=0.0)
    bystander = SimpleNamespace(id=2, alive=True, spawned=True, x=104.0, y=120.0, z=29.0,
                                input=SimpleNamespace(crouch=False))
    (explosion,) = _fly(engine, pj.WallWorld(105), [bystander])
    assert explosion.contact_player_id is None


def test_a_real_airborne_rocket_hit_counts_through_explode_projectile():
    server = _Server()
    server.projectile_engine = ProjectileEngine()
    server.goo_controller = server.fire_controller = None
    shooter = _player(server, 1, TEAM1, x=300.0)
    target = _player(server, 2, TEAM2)
    shooter.airborne = True
    projectile = server.projectile_engine.spawn(
        int(C.RPG_TOOL), (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 0.0, shooter.id, now=0.0,
    )
    projectile.x, projectile.y, projectile.z = target.position
    projectile.contact_player_id = target.id
    BattleSpadesServer._explode_projectile(server, Explosion(projectile))
    assert progress(server, shooter, "airborne_rocket_count") == 1
    assert server.achievements.faults == {}


def test_a_turret_rocket_carries_its_turret_and_its_target():
    server = rt.Server()
    owner = rt.Player(1, 2, (10.0, 10.0, 10.0))
    enemy = rt.Player(2, 3, (20.0, 10.0, 10.0))
    server.players = {1: owner, 2: enemy}
    controller = rt.RocketTurretController(server)
    turret = controller.place(owner, (10.0, 10.0, 10.0), yaw=0.0, now=0.0)
    controller.update(1.0, now=2.0)
    projectile = server.projectile_engine.spawned[0][5]
    assert projectile.turret is turret
    assert projectile.turret_target_id == enemy.id


def test_real_projectiles_start_without_achievement_evidence():
    engine = ProjectileEngine()
    projectile = engine.spawn(
        int(C.GRENADE_TOOL), (1.0, 1.0, 1.0), (0.0, 0.0, 0.0), 1.0, 1, now=0.0,
    )
    assert (projectile.contact_player_id, projectile.turret, projectile.turret_target_id) == (
        None, None, None,
    )


# ---------------------------------------------------------------------------
# Crates, tick, server object
# ---------------------------------------------------------------------------

def test_a_crate_refill_reports_what_it_restored():
    from server.map_resources import _counted

    server = make_server()
    player = make_player(server, 1, TEAM1, health=30)
    service = SimpleNamespace(server=server)

    def heal(target):
        target.health = 100

    refill = _counted(service, C.MOST_HEALTH_CRATES_COLLECTED, heal)
    refill(player)
    assert server.achievements.tracker(player).match == {"health_from_drops": 70}
    player.health = 20
    refill(player)
    assert "health_drop_greedy" in unlocked(server, player)


def test_a_real_ammo_crate_restock_counts_the_rounds_it_added():
    server = _Server()
    player = _player(server, 1, TEAM1)
    from server.game_constants import WEAPON_PROFILES
    from server.map_resources import _counted

    capacity = int(WEAPON_PROFILES[player.weapon].reserve_ammo)
    refill = _counted(
        SimpleNamespace(server=server), C.MOST_AMMO_CRATES_COLLECTED,
        lambda target: target.restock_ammo(int(C.AMMO_CRATE)),
    )
    total = 0
    for _ in range(40):
        player.ammo_reserve = 0
        player._weapon_ammo[player.weapon] = (player.ammo_clip, 0)
        refill(player)
        total += player.ammo_reserve
        assert player.ammo_reserve > 0
        key = f"ammo_from_drops:{int(player.weapon)}"
        assert server.achievements.tracker(player).match[key] == total
        if total >= capacity:
            break
    assert "ammo_drop_greedy" in unlocked(server, player)


def test_the_simulation_step_ticks_the_engine():
    from server.simulation_runtime import SimulationRuntime

    server = make_server()
    server.tick_interval = 1.0 / 60.0
    flyer = make_player(server, 1, TEAM1, airborne=True)
    runtime = SimulationRuntime(server)
    for _ in range(60):
        runtime._tick_achievements()
    assert progress(server, flyer, "airborne_seconds_count") == 1


def test_a_new_server_holds_an_inert_engine_and_opens_no_file(tmp_path):
    config = ServerConfig()
    config.achievements.path = str(tmp_path / "state" / "achievements.sqlite3")
    server = BattleSpadesServer(config)
    engine = server.achievements
    assert isinstance(engine, achievements.AchievementEngine)
    assert engine.active is False and achievements.engine_of(server) is None
    assert not (tmp_path / "state").exists()
    # start() is what BattleSpadesServer.start runs before the mode begins.
    assert engine.start() is True
    assert (tmp_path / "state" / "achievements.sqlite3").is_file()
    engine.close()
    assert engine.active is False


def test_a_disabled_server_never_starts_its_engine(tmp_path):
    config = ServerConfig()
    config.achievements.enabled = False
    config.achievements.path = str(tmp_path / "achievements.sqlite3")
    server = BattleSpadesServer(config)
    assert server.achievements.start() is False
    assert achievements.engine_of(server) is None
    assert not (tmp_path / "achievements.sqlite3").exists()


def test_the_server_starts_the_engine_before_its_first_mode_and_ticks_it():
    import inspect

    from server.simulation_runtime import SimulationRuntime

    source = inspect.getsource(BattleSpadesServer.start)
    started = source.index("self.achievements.start()")
    assert source.index("self.world_manager.load_map(") < started
    assert started < source.index("await self.mode.on_mode_start()")
    assert "self._tick_achievements" in inspect.getsource(SimulationRuntime.step)


def test_a_real_disconnect_saves_the_leavers_counters(tmp_path):
    from server.player import Player
    from tests import test_mode_lifecycle_contracts as lc

    path = tmp_path / "achievements.sqlite3"
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    attach_engine(server, store=achievements.AchievementStore(path))
    connection = lc._Conn()
    player = Player(0, "Leaver", TEAM1, C.RIFLE_TOOL, connection)
    connection.player = player
    player.spawn(100.5, 100.5, 59.75)
    server.players[0] = player
    server.teams[TEAM1].add_player(player)
    peer = object()
    server.connections[peer] = connection
    server.broadcast = lambda data, **_kwargs: None
    server.mode = lc._PlainMode(server)

    server.achievements.add(player, "distance_run", 12)
    reader = achievements.AchievementStore(path)
    assert reader.counters("name:leaver") == {}
    server._on_disconnect_sync(peer)
    assert 0 not in server.players
    assert reader.counters("name:leaver") == {"distance_run": 12}
    assert server.achievements._progress == {}
    reader.close()
    server.achievements.close()


def test_stopping_the_server_flushes_and_closes_the_store(tmp_path):
    import asyncio

    from tests import test_server_shutdown as shutdown

    path = tmp_path / "achievements.sqlite3"

    async def scenario():
        events: list[str] = []
        config = ServerConfig()
        config.achievements.path = str(path)
        server = BattleSpadesServer(config)
        server.running = True
        server.mode = shutdown._ShutdownMode(server, events)
        server.bots = None
        server.steam_master = shutdown._AsyncCloser(events, "steam-close")
        server.revival_master = shutdown._AsyncCloser(events, "revival-close")
        server.debug_parity = shutdown._SyncCloser(events, "debug-close")
        server.prefab_actions = shutdown._SyncCloser(events, "prefab-close")
        assert server.achievements.start() is True
        player = SimpleNamespace(id=1, name="Kiko", team=TEAM1, is_bot=False)
        server.achievements.add(player, "distance_run", 7)
        await server.stop()
        return server

    server = asyncio.run(scenario())
    assert server.achievements.active is False
    reader = achievements.AchievementStore(path)
    assert reader.counters("name:kiko") == {"distance_run": 7}
    reader.close()
