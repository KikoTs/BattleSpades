"""Hardcore integration: native wire menus, legal kits, combat and objectives."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import shared.constants as C
from aoslib.vxl import VXL
from modes import canonical_mode_code, get_mode_class
from server import hardcore as HC, mode_data
from server.builders.initial_info import build_initial_info
from server.builders.state_data import build_state_data
from server.class_selection import normalize_server_selection, ClassSelection
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2, WEAPON_CATALOG
from server.game_rules import get_rules
from server.main import BattleSpadesServer
from server.map_metadata import MapMetadata, canonical_mode_code as map_mode
from shared.bytes import ByteReader
from shared.packet import InitialInfo
from tests.test_unofficial_modes import add_player


def game(code="hc_tdm"):
    config = ServerConfig(default_mode=code)
    config.achievements.enabled = False
    config.mode_start_music = False
    server = BattleSpadesServer(config)
    wm = server.world_manager
    wm.map = VXL(-1, b"", 0, 2)
    for x in range(244, 270):
        for y in range(244, 270):
            wm.map.set_point(x, y, 62, True, 0x7f607050)
    wm.map_metadata = MapMetadata()
    wm.map_name = "test_flat"
    wm._refresh_world()
    server.packets = []
    server.broadcast = lambda data, **kwargs: server.packets.append(bytes(data))
    server.mode = get_mode_class(code)(server)
    return server


def human(server, cid, pid=0, team=TEAM1):
    player = add_player(server, pid, team)
    selection = normalize_server_selection(server.config, cid)
    selection = server.mode.prepare_join_selection(team, selection)
    player.apply_class_selection(selection)
    player.spawn(252.5 + pid, 252.5, 59.75)
    return player


@pytest.mark.parametrize("base", HC.BASE_MODES)
def test_every_mode_has_native_wire_identity_menu_and_map_compatibility(base):
    code = "hc_" + base
    assert canonical_mode_code("hardcore_" + base) == code
    assert map_mode(code) == base
    server = game(code)
    initial = build_initial_info(server)
    raw = bytes(initial.generate())
    decoded = InitialInfo(ByteReader(raw[1:]))
    assert decoded.mode_key == mode_data.get(base).mode_id
    assert decoded.loadout_overrides == HC.loadout_overrides()
    assert decoded.enable_corpse_explosion == 0
    assert dict(decoded.custom_game_rules)["RULE_ENABLE_GRAVESTONES"] == "0"
    assert decoded.classic == 0
    state = build_state_data(server)
    assert state.team2_classes == list(HC.CLASS_IDS)
    assert state.team1_classes == ([C.CLASS_ZOMBIE] if base == "zom" else list(HC.CLASS_IDS))


@pytest.mark.parametrize("role", HC.ROLES, ids=lambda r: r.key)
def test_all_advertised_primary_choices_survive_join_commit_and_spawn(role):
    server = game()
    player = human(server, role.class_id)
    for primary in role.primary:
        choice = normalize_server_selection(server.config, role.class_id, [primary, C.MOLOTOV_TOOL])
        assert server.mode.allows_class_selection(player, choice)
        player.apply_class_selection(choice)
        player.spawn(252.5, 252.5, 59.75)
        assert primary in player.loadout
        assert C.MOLOTOV_TOOL in player.loadout
        assert C.GRENADE_TOOL not in player.loadout
        assert C.SPADE_TOOL in player.loadout
        assert player.parachute_id == C.A370
        assert not player.jetpack_id
        assert player.max_health == player.health == 100
        assert not player.prefabs


@pytest.mark.parametrize("role", HC.ROLES, ids=lambda r: r.key)
def test_forged_kit_cannot_smuggle_other_class_weapons_or_gadgets(role):
    server = game()
    player = human(server, role.class_id)
    player.apply_class_selection(ClassSelection(role.class_id, tuple(range(73))))
    permitted = {v for items in HC.slots(role.class_id).values() for v in items}
    assert set(player.loadout) <= permitted
    assert len(set(player.loadout) & set(role.primary)) == 1
    assert len(set(player.loadout) & set(HC.GRENADES)) == 1
    assert C.DISGUISE_TOOL not in player.loadout


def test_hardcore_rules_are_live_scoped_and_do_not_mutate_normal_rules():
    config = ServerConfig(default_mode="hc_tdm")
    before = dict(config.game_rules.values)
    rules = get_rules(config)
    assert rules.enabled("RULE_POINTS_FROM_TEABAGGING")
    assert not rules.enabled("RULE_ENABLE_CORPSE_EXPLOSION")
    assert not rules.enabled("RULE_ENABLE_GRAVESTONES")
    assert rules.enabled("RULE_ENABLE_WEAPON_CLASSIC_SMG")
    config.game_rules.apply({"RULE_BLOCK_HEALTH": 2.0})
    assert get_rules(config).get("RULE_BLOCK_HEALTH") == 2.0
    config.default_mode = "tdm"
    assert get_rules(config) is config.game_rules
    assert get_rules(config).enabled("RULE_ENABLE_CORPSE_EXPLOSION")
    assert not get_rules(config).enabled("RULE_POINTS_FROM_TEABAGGING")
    before["RULE_BLOCK_HEALTH"] = 2.0
    assert config.game_rules.values == before


@pytest.mark.parametrize("base", [code for code in HC.BASE_MODES if code != "zom"])
def test_non_zombie_hardcore_join_replaces_forged_zombie_class(base):
    server = game("hc_" + base)
    selection = normalize_server_selection(server.config, C.CLASS_ZOMBIE)
    joined = server.mode.prepare_join_selection(TEAM1, selection)
    assert joined.class_id in HC.CLASS_IDS
    assert C.ZOMBIEHAND_TOOL not in joined.loadout
    player = human(server, C.CLASS_ZOMBIE)
    assert player.class_id in HC.CLASS_IDS and player.health == 100


def test_heavy_armour_only_affects_body_bullets_and_resets_stock_class_resistance():
    from server.combat_runtime import CombatSystem
    from server.weapons_retail import class_damage_multiplier, class_headshot_multiplier
    server = game()
    combat = CombatSystem(server)
    attacker = human(server, C.CLASS_CLASSIC_SOLDIER)
    gun = WEAPON_CATALOG[C.ASSAULT_RIFLE_TOOL]
    spade = WEAPON_CATALOG[C.SPADE_TOOL]
    for index, role in enumerate(HC.ROLES, 1):
        target = human(server, role.class_id, index, TEAM2)
        torso = combat._calculate_damage(attacker, gun, False, target)
        bare = combat._calculate_damage(attacker, gun, False)
        assert torso == pytest.approx(bare * (.8 if role.key == "heavy" else 1.0))
        assert combat._calculate_damage(attacker, gun, True, target) == combat._calculate_damage(attacker, gun, True)
        assert combat._calculate_damage(attacker, spade, False, target) == combat._calculate_damage(attacker, spade, False)
        assert class_damage_multiplier(target) == class_headshot_multiplier(target) == 1.0


def test_medpacks_pause_after_damage_and_share_one_recipient_cooldown(monkeypatch):
    from server.entities.behaviors import MedpackBehavior
    server = game()
    p = human(server, C.CLASS_MEDIC)
    p.health = 25
    p._last_damage_at = 100.0
    ctx = SimpleNamespace(server=server)
    first, second = MedpackBehavior(TEAM1, 25, 3), MedpackBehavior(TEAM1, 25, 3)
    monkeypatch.setattr(time, "time", lambda: 102.0)
    assert not first.on_touch(None, p, ctx)
    assert first.uses == 3
    monkeypatch.setattr(time, "time", lambda: 104.0)
    assert first.on_touch(None, p, ctx)
    assert p.health == 50
    assert not second.on_touch(None, p, ctx)
    monkeypatch.setattr(time, "time", lambda: 106.0)
    assert second.on_touch(None, p, ctx)
    assert p.health == 75


def test_death_retains_body_without_grave_or_explosion_and_teabag_pays_once():
    from server.combat_scores import record_teabag_crouch
    server = game()
    server.mode.started = True
    killer = human(server, C.CLASS_CLASSIC_SOLDIER)
    victim = human(server, C.CLASS_SOLDIER, 1, TEAM2)
    victim.die(killer)
    assert not any(ent.kind == "grave" for ent in server.entity_registry._entities.values())
    assert not any(raw[0] == 36 for raw in server.packets)
    score = killer.score
    assert not record_teabag_crouch(server, killer, now=100)
    assert not record_teabag_crouch(server, killer, now=100.2)
    assert record_teabag_crouch(server, killer, now=100.4)
    assert killer.score == score + 2
    for moment in (101, 101.2, 101.4):
        assert not record_teabag_crouch(server, killer, now=moment)
    assert killer.score == score + 2


def test_vip_promotion_preserves_living_class_health_and_deaths():
    server = game("hc_vip")
    p = human(server, C.CLASS_MEDIC)
    p.health = 63
    before = (p.class_id, p.health, p.deaths, p.replication_generation)
    server.mode._promote_vip(TEAM1, p)
    assert (p.class_id, p.health, p.deaths, p.replication_generation) == before
    assert p.alive


def test_zombie_infection_and_next_round_restore_hardcore_soldier():
    from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM
    server = game("hc_zom")
    p = human(server, C.CLASS_CLASSIC_SOLDIER, team=SURVIVOR_TEAM)
    assert p.class_id == C.CLASS_CLASSIC_SOLDIER
    asyncio.run(server.mode._infect(p, patient_zero=False))
    assert p.team == ZOMBIE_TEAM and p.class_id == C.CLASS_ZOMBIE
    server.mode._assign_survivor(p)
    assert p.team == SURVIVOR_TEAM and p.class_id == C.CLASS_CLASSIC_SOLDIER
    assert C.ASSAULT_RIFLE_TOOL in p.loadout and C.ZOMBIEHAND_TOOL not in p.loadout


def test_gui_places_hardcore_beside_each_standard_mode():
    from server_gui.catalog import mode_choices, mode_pool
    choices = mode_choices()
    for index, base in enumerate(HC.BASE_MODES):
        assert choices[2 * index].code == base
        assert choices[2 * index + 1].code == "hc_" + base
        assert "Unofficial" not in choices[2 * index + 1].title
        directory = Path(__file__).resolve().parents[1] / "maps"
        assert mode_pool(directory, "hc_" + base) == mode_pool(directory, base)


@pytest.mark.parametrize("base", HC.BASE_MODES)
def test_every_objective_starts_ticks_and_admits_bots_with_legal_roles(base):
    from tests.test_unofficial_bots import director_for
    async def scenario():
        server = game("hc_" + base)
        director = director_for(server)
        server.bots = director
        try:
            await server.mode.on_mode_start()
            for team in (TEAM1, TEAM2):
                bot = await director.add_bot(team=team)
                assert bot is not None
                assert bot.class_id in HC.CLASS_IDS or (base == "zom" and bot.class_id == C.CLASS_ZOMBIE)
                assert bot.tool in bot.loadout
            await server.mode.on_tick(1)
            assert director._mode_id() == base
            assert not getattr(server.mode, "unofficial", False)
        finally:
            await server.mode.deactivate()
            await director.close()
    asyncio.run(scenario())


def test_soldier_extra_ammunition_waits_for_reveal_and_is_once_per_life():
    server = game()
    p = human(server, C.CLASS_CLASSIC_SOLDIER)
    p.connection.in_game = False
    calls = []
    p.restock_ammo = lambda kind: calls.append(kind)
    asyncio.run(server.mode.on_player_spawn(p))
    assert not calls
    p.connection.in_game = True
    asyncio.run(server.mode.on_player_ready(p))
    asyncio.run(server.mode.on_player_spawn(p))
    assert calls == [C.AMMO_CRATE]


@pytest.mark.parametrize("role", HC.ROLES, ids=lambda r: r.key)
def test_native_speed_matches_advertised_scalar_and_heavy_is_slowest(role):
    from tests.test_reversed_movement_engine import make_player, advance_player
    from server.class_data import rule_speed_scale
    measured = []
    for sprint in (False, True):
        p = make_player()
        p.connection.server.config = ServerConfig(default_mode="hc_tdm")
        p.class_id = role.class_id
        for x in range(95, 160):
            for y in range(98, 103):
                p.connection.server.world_manager.map.set_point(x, y, 62, True, 0x7f607050)
        p.set_orientation_vector(1, 0, 0)
        p.update_input(True, False, False, False, False, False, False, sprint)
        advance_player(p, 60)
        start = p.x
        advance_player(p, 60)
        measured.append(p.x - start)
    scalar = rule_speed_scale(ServerConfig(default_mode="hc_tdm"), role.class_id)
    assert measured[0] == pytest.approx(8 * .7 * scalar, abs=.03)
    assert measured[1] == pytest.approx(8 * 1.05 * scalar, abs=.04)
    assert measured[1] > measured[0]
    if role.key != "heavy":
        heavy = HC.BY_NAME["heavy"]
        heavy_scale = rule_speed_scale(ServerConfig(default_mode="hc_tdm"), heavy.class_id)
        assert measured[0] > 8 * .7 * heavy_scale
        assert measured[1] > 8 * 1.05 * heavy_scale


@pytest.mark.parametrize("role", HC.ROLES, ids=lambda r: r.key)
@pytest.mark.parametrize("height,jump,crouch", [(1, False, False), (2, True, False), (3, True, False), (3, True, True)])
def test_every_role_steps_one_jumps_two_and_cannot_jump_three(role, height, jump, crouch):
    from tests.test_reversed_movement_engine import make_player, advance_player, add_step
    p = make_player()
    p.connection.server.config = ServerConfig(default_mode="hc_tdm")
    p.class_id = role.class_id
    add_step(p.connection.server.world_manager, 100, 100, 62, height, width=16)
    p.set_orientation_vector(1, 0, 0)
    for tick in range(120):
        p.update_input(True, False, False, False, jump and tick >= 3,
                       crouch and tick > 5, False, False)
        advance_player(p, 1)
    assert (p.x > 101.5) == (height <= 2)


def test_hardcore_gravity_is_normal_even_on_low_gravity_maps():
    server = game()
    server.world_manager.map_metadata.gravity = .25
    server.world_manager._refresh_world()
    assert server.world_manager.world.get_gravity() == 1.0
    assert build_state_data(server).gravity == 1.0


def test_hardcore_requires_v3_capability_before_sending_any_world_state():
    import struct
    from server.flight_profile import ticket_flight_capability
    from tests.test_reversed_spawn_handshake import DummyServer, make_connection
    assert ticket_flight_capability(bytes([105]) + struct.pack("<i", 0) + b"BSCF\x03") == 3
    conn = make_connection(DummyServer())
    conn.server.config.default_mode = "hc_tdm"
    rejected = []
    conn.disconnect = lambda reason: rejected.append(reason)
    with pytest.raises(ConnectionError, match="Hardcore requires"):
        asyncio.run(conn.send_info())
    assert rejected == [C.DISCONNECT.ERROR_CLIENT_OUT_OF_DATE]
