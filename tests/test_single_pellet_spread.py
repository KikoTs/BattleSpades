"""P2-18: single-pellet hit-scan guns resolve the retail SEEDED direction.

Retail ``Character.shoot`` (character.pyd sub_10049DB0) sends the unspread
aim once and expands ``weapon.pellets`` directions from ``ShootPacket.seed``
-- including ``pellets == 1``. The server used to resolve single-pellet guns
exactly on the crosshair while every client drew/predicted the seeded ray.
"""

import math
import random

import shared.constants as C
import server.combat_runtime as combat_runtime
from server.combat_runtime import get_combat_system
from server.game_constants import TEAM1, TEAM2, WEAPON_CATALOG
from tests.test_reversed_combat import (
    DummyServer,
    make_player,
    make_shoot_packet,
    normalize,
)


def _seeded(direction, seed, accuracy, count=1, zoomed=False):
    """Independent re-statement of the retail/native-client expansion."""
    scale, center = (2.0, 1.0) if zoomed else (4.0, 2.0)
    rng = random.Random(int(seed) & 0xFF)
    out = []
    for _ in range(count):
        out.append(normalize(tuple(
            axis + (rng.random() * scale - center) * accuracy
            for axis in direction
        )))
    return out


def _record_rays(combat):
    rays = []

    def record(_attacker, direction, _origin=None):
        rays.append(tuple(direction))
        return False

    combat._resolve_hitscan = record
    return rays


def _close(a, b, tol=1e-9):
    return all(math.isclose(x, y, abs_tol=tol) for x, y in zip(a, b))


def _shooter(tool, position=(100.5, 100.5, 60.0), server=None):
    server = server or DummyServer()
    attacker, _ = make_player(server, 0, "Attacker", TEAM1, tool, position)
    attacker.set_tool(tool)
    attacker.set_orientation_vector(1.0, 0.0, 0.0)
    return server, attacker


def test_single_pellet_shot_resolves_the_seeded_direction_not_the_crosshair():
    for tool in (C.RIFLE_TOOL, C.PISTOL_TOOL, C.SNIPER_TOOL, C.SNUB_PISTOL_TOOL):
        server, attacker = _shooter(tool)
        profile = attacker.get_weapon_profile()
        assert profile.pellet_count == 1
        combat = get_combat_system(server)
        rays = _record_rays(combat)
        packet = make_shoot_packet(attacker, orientation=(1.0, 0.0, 0.0), seed=42)
        packet.loop_count = 900

        combat.handle_shot(attacker, packet)

        assert len(rays) == 1, tool
        expected = _seeded((1.0, 0.0, 0.0), 42, profile.spread)[0]
        assert _close(rays[0], expected), tool
        assert not _close(rays[0], (1.0, 0.0, 0.0), tol=1e-4), tool


def test_snub_pistol_uses_the_stock_class_accuracy():
    # SNUB_PISTOL_ACCURACY (A1138) = 0.01; the row used to leave it at 0.0,
    # which would have kept the snub exactly on the crosshair.
    assert WEAPON_CATALOG[int(C.SNUB_PISTOL_TOOL)].spread == 0.01


def test_pistol_seed_42_matches_the_native_client_golden_contact():
    """Cross-check with BattleSpadesClient tests/test_replicated_shot.cpp.

    ``packet_seed_replays_cpython_axis_spread``: a hip PISTOL (17) shot with
    seed 42 from (100.5, 100.5, 100.5) heading -x into a wall at x = 5
    contacts cell (5, 97, 99) in the native client's replica.
    """
    server, attacker = _shooter(C.PISTOL_TOOL)
    combat = get_combat_system(server)
    direction = combat._seeded_pellet_directions(
        attacker, (-1.0, 0.0, 0.0), attacker.get_weapon_profile(),
        make_shoot_packet(attacker, seed=42), 1000.0,
    )
    assert len(direction) == 1
    dx, dy, dz = direction[0]
    travel = (6.0 - 100.5) / dx  # the wall's +x face at x = 6
    assert int(math.floor(100.5 + dy * travel)) == 97
    assert int(math.floor(100.5 + dz * travel)) == 99


def test_zoomed_sniper_uses_accuracy_zoom_zero_and_stays_on_the_crosshair():
    server, attacker = _shooter(C.SNIPER_TOOL)
    attacker.input.zoom = True
    combat = get_combat_system(server)
    rays = _record_rays(combat)
    packet = make_shoot_packet(attacker, orientation=(1.0, 0.0, 0.0), seed=42)
    packet.loop_count = 900

    combat.handle_shot(attacker, packet)

    assert len(rays) == 1
    assert _close(rays[0], (1.0, 0.0, 0.0))


def test_zoomed_rifle_keeps_hip_accuracy_with_the_tighter_mapping():
    # ClassicRifleWeapon has no accuracy_zoom (None): zoom only switches the
    # random mapping to (random()*2 - 1), exactly like the native replica.
    server, attacker = _shooter(C.RIFLE_TOOL)
    attacker.input.zoom = True
    combat = get_combat_system(server)
    rays = _record_rays(combat)
    packet = make_shoot_packet(attacker, orientation=(1.0, 0.0, 0.0), seed=9)
    packet.loop_count = 900

    combat.handle_shot(attacker, packet)

    expected = _seeded((1.0, 0.0, 0.0), 9, 0.003, zoomed=True)[0]
    assert len(rays) == 1 and _close(rays[0], expected)


def test_variable_accuracy_single_pellet_gun_blooms_like_prep_shoot(monkeypatch):
    # SMG: accuracy 0.02..0.04 over accuracy_spread 1..6, +0.2/shot, -1.0/s.
    server, attacker = _shooter(C.SMG_TOOL)
    combat = get_combat_system(server)
    rays = _record_rays(combat)
    times = iter((10.0, 10.11))
    monkeypatch.setattr(combat_runtime.time, "monotonic", lambda: next(times))

    for loop, seed in ((900, 5), (907, 6)):
        packet = make_shoot_packet(attacker, orientation=(1.0, 0.0, 0.0), seed=seed)
        packet.loop_count = loop
        assert combat.handle_shot(attacker, packet) is False

    # Shot 1 uses the bloom BEFORE its increase (spread 1.0 -> acc 0.02).
    # Shot 2: spread 1.2 recovered 0.11 s -> 1.09 -> 0.02 + 0.018 * 0.02.
    second_accuracy = 0.02 + ((1.09 - 1.0) / 5.0) * (0.04 - 0.02)
    assert len(rays) == 2
    assert _close(rays[0], _seeded((1.0, 0.0, 0.0), 5, 0.02)[0])
    assert _close(rays[1], _seeded((1.0, 0.0, 0.0), 6, second_accuracy)[0])


def _open_air_duel(tool, seed):
    """Shooter on the flat debug plateau (ground z = 62), aiming +x."""
    server = DummyServer()
    server.world_manager.generate_flat_map()
    server, attacker = _shooter(tool, (100.5, 100.5, 58.0), server=server)
    combat = get_combat_system(server)
    eye = tuple(attacker.eye)
    direction = combat._seeded_pellet_directions(
        attacker, (1.0, 0.0, 0.0), attacker.get_weapon_profile(),
        make_shoot_packet(attacker, seed=seed), 0.0,
    )[0]
    combat._pellet_spread.clear()
    return server, attacker, combat, eye, direction


def _fire(combat, attacker, seed):
    packet = make_shoot_packet(attacker, orientation=(1.0, 0.0, 0.0), seed=seed)
    packet.loop_count = 900
    return combat.handle_shot(attacker, packet)


def test_target_just_off_the_crosshair_is_hit_along_the_seeded_ray():
    seed, distance = 42, 60.0
    server, attacker, combat, eye, direction = _open_air_duel(C.PISTOL_TOOL, seed)
    travel = distance / direction[0]
    # Put the target's torso (z + 0.6) exactly on the seeded ray.
    target, _ = make_player(
        server, 1, "Target", TEAM2, C.RIFLE_TOOL,
        (eye[0] + distance,
         eye[1] + direction[1] * travel,
         eye[2] + direction[2] * travel - 0.6),
    )
    lateral = abs(direction[1] * travel)
    assert lateral > 1.0  # clearly off the raw crosshair ray
    raw = combat._trace_authoritative_hit(
        attacker, eye, (1.0, 0.0, 0.0), attacker.get_weapon_profile().max_range
    )
    assert raw is None or raw[1] is not target  # crosshair alone would miss

    _fire(combat, attacker, seed)

    assert target.health < 100


def test_target_exactly_on_the_crosshair_is_missed_when_the_seed_deviates():
    seed, distance = 42, 60.0
    server, attacker, combat, eye, _direction = _open_air_duel(C.PISTOL_TOOL, seed)
    target, _ = make_player(
        server, 1, "Target", TEAM2, C.RIFLE_TOOL,
        (eye[0] + distance, eye[1], eye[2] - 0.6),
    )
    raw = combat._trace_authoritative_hit(
        attacker, eye, (1.0, 0.0, 0.0), attacker.get_weapon_profile().max_range
    )
    assert raw is not None and raw[1] is target  # old behaviour: a hit

    _fire(combat, attacker, seed)

    assert target.health == 100


def test_multi_pellet_expansion_is_unchanged():
    server, attacker = _shooter(C.SHOTGUN_TOOL)
    profile = attacker.get_weapon_profile()
    combat = get_combat_system(server)
    rays = _record_rays(combat)
    packet = make_shoot_packet(attacker, orientation=(1.0, 0.0, 0.0), seed=37)
    packet.loop_count = 900

    combat.handle_shot(attacker, packet)

    expected = _seeded((1.0, 0.0, 0.0), 37, profile.spread, profile.pellet_count)
    assert len(rays) == profile.pellet_count == 10
    for actual, wanted in zip(rays, expected):
        assert _close(actual, wanted)


def test_shotgun_bloom_matches_the_previous_compact_level_curve():
    """The generic prep_shoot model reproduces the old shotgun-only curve."""
    server, attacker = _shooter(C.SHOTGUN_TOOL)
    combat = get_combat_system(server)
    profile = attacker.get_weapon_profile()
    packet = make_shoot_packet(attacker, seed=11)
    level, previous = 0.0, None
    for now in (0.0, 1.0, 1.2, 4.0):
        if previous is not None:
            level = max(0.0, level - (now - previous) / 3.0)
        accuracy = profile.spread * (1.0 + level)
        got = combat._seeded_pellet_directions(
            attacker, (1.0, 0.0, 0.0), profile, packet, now
        )
        wanted = _seeded((1.0, 0.0, 0.0), 11, accuracy, profile.pellet_count)
        assert all(_close(a, b, tol=1e-12) for a, b in zip(got, wanted))
        level = min(1.0, level + 0.5 / 3.0)
        previous = now


def test_single_pellet_seed_feeds_the_skew_detector():
    from server.combat_runtime import anticheat_stats

    server, attacker = _shooter(C.RIFLE_TOOL)
    combat = get_combat_system(server)
    _record_rays(combat)
    packet = make_shoot_packet(attacker, orientation=(1.0, 0.0, 0.0), seed=77)
    packet.loop_count = 900

    combat.handle_shot(attacker, packet)

    assert anticheat_stats(attacker)["pellet_seeds"][77] == 1
