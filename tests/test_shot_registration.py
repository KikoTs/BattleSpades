"""On-target shots must register (Beta 0.1 "sniper does not kill" report).

Two server bugs dropped or deflected legitimate shots:

* The pellet spread of a shot used the NEWEST ClientData zoom bit. The stock
  client un-zooms a sniper right after its shot (Character.shoot drops the
  zoom on the last round, Character.reload cancels it), and ClientData is
  unsequenced while ShootPacket is reliable, so jitter or one lost datagram
  put the zoom-off ClientData ahead of the shot. The server then resolved a
  zoomed sniper shot (accuracy_zoom 0) with the hip spread (0.025, up to
  +/-0.05 rad per axis): a clean miss at range.
* The reload timer starts when WeaponReload(76) arrives. A lost reload
  datagram arrives one retransmission later, and the first shots after the
  client's reload were dropped silently.

``scripts/shot_registration_lab.py`` reproduces both end to end over an
impaired link; the last test runs one short lab case.
"""

import math
import time
from types import SimpleNamespace

import pytest

import shared.constants as C
from server import lag_compensation as lc
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.main import BattleSpadesServer
from server.player import Player
from shared.packet import ShootPacket

EYE_Z = 59.75


class _Connection:
    def __init__(self, server):
        self.server = server
        self.player = None
        self.in_game = True
        self.sent = []
        self.reserved_player_id = None
        self.peer = SimpleNamespace(roundTripTime=0, roundTripTimeVariance=0)

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append((data, reliable))

    def on_disconnect(self):
        pass


def _server():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    server.config.lag_compensation_enabled = False
    return server


def _player(server, *, player_id, team, position, class_id, loadout, tool):
    connection = _Connection(server)
    player = Player(player_id, f"SR{player_id}", team, tool, connection)
    connection.player = player
    player.class_id = int(class_id)
    player.loadout = list(loadout)
    player.spawn(*position)
    player.set_tool(tool, raw=True)
    player.spawned_at = time.monotonic() - 60.0
    server.players[player.id] = player
    server.connections[player.id] = connection
    server.teams[team].add_player(player)
    return player


def _client_data(player, label, *, zoom, aim=(1.0, 0.0, 0.0)):
    """One received ClientData frame (buffered like handle_client_data)."""

    flags = (False,) * 8
    actions = (False, zoom, zoom, False, True, False, False, False, False)
    player.record_input_frame(label, flags, aim, action_flags=actions,
                              received_server_tick=label)
    player.update_action_input(*actions)
    player.set_orientation_vector(*aim)


def _aim(origin, point):
    delta = tuple(point[i] - origin[i] for i in range(3))
    length = math.sqrt(sum(c * c for c in delta))
    return tuple(c / length for c in delta)


def _shot(shooter, direction, *, label, seed=200):
    packet = ShootPacket()
    packet.loop_count = label
    packet.shooter_id = shooter.id
    packet.shot_on_world_update = 0
    packet.x, packet.y, packet.z = shooter.eye
    packet.ori_x, packet.ori_y, packet.ori_z = direction
    packet.damage = 5
    packet.penetration = 2
    packet.affect_shooter = 0
    packet.secondary = 0
    packet.seed = seed
    return packet


def _sniper_duel(distance=90.0):
    server = _server()
    shooter = _player(
        server, player_id=0, team=TEAM1, position=(60.5, 100.5, EYE_Z),
        class_id=C.CLASS_SCOUT, loadout=[C.SNIPER_TOOL, C.PISTOL_TOOL],
        tool=C.SNIPER_TOOL,
    )
    target = _player(
        server, player_id=1, team=TEAM2,
        position=(60.5 + distance, 100.5, EYE_Z),
        class_id=C.CLASS_SOLDIER, loadout=[C.RIFLE_TOOL], tool=C.RIFLE_TOOL,
    )
    damage = []

    def record(amount, source=None, kill_type=0, **_kw):
        damage.append((float(amount), int(kill_type)))
        return False

    target.damage = record
    return server, shooter, target, damage


def _seed_spreading_hip_shot_off_target(shooter, target):
    """A seed whose HIP pellet misses the target (zoomed it is exact)."""

    combat = shooter.connection.server.combat
    torso = (target.x, target.y, target.z + 0.75)
    direction = _aim(shooter.eye, torso)
    for seed in range(1, 256):
        packet = _shot(shooter, direction, label=0, seed=seed)
        import random

        rng = random.Random(seed)
        accuracy = 0.025
        pellet = tuple(
            direction[i] + (rng.random() * 4.0 - 2.0) * accuracy for i in range(3)
        )
        norm = math.sqrt(sum(c * c for c in pellet))
        pellet = tuple(c / norm for c in pellet)
        if combat._ray_hits_target(shooter.eye, pellet, 200.0, target) is None:
            return seed, direction
    raise AssertionError("no spreading seed found")


@pytest.mark.parametrize("newest_zoom", [False])
def test_zoomed_sniper_shot_uses_its_own_frames_zoom(newest_zoom):
    """A ClientData that overtook the shot (zoom already dropped) must not
    turn the zoomed shot into a hip shot."""

    server, shooter, target, damage = _sniper_duel()
    seed, direction = _seed_spreading_hip_shot_off_target(shooter, target)
    for label in range(100, 111):
        _client_data(shooter, label, zoom=True)
    # The shot is labelled 110. Frames 111-112 (post-shot: zoom dropped by
    # the last round / reload) arrived before the reliable shot.
    _client_data(shooter, 111, zoom=newest_zoom)
    _client_data(shooter, 112, zoom=newest_zoom)
    assert shooter.input.zoom is newest_zoom

    server.combat.handle_shot(shooter, _shot(shooter, direction, label=110, seed=seed))

    assert damage, "zoomed on-target sniper shot did not register"
    assert damage[0][0] == pytest.approx(50.0)


def test_clientdata_sampled_after_the_shot_still_counts_as_zoomed():
    """Frame L's own ClientData may already carry the dropped zoom; L-1
    proves the scope was up when Character.shoot ran."""

    server, shooter, target, damage = _sniper_duel()
    seed, direction = _seed_spreading_hip_shot_off_target(shooter, target)
    for label in range(100, 110):
        _client_data(shooter, label, zoom=True)
    _client_data(shooter, 110, zoom=False)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=110, seed=seed))
    assert damage


def test_hip_fired_sniper_keeps_the_stock_hip_spread():
    server, shooter, target, damage = _sniper_duel()
    seed, direction = _seed_spreading_hip_shot_off_target(shooter, target)
    for label in range(100, 115):
        _client_data(shooter, label, zoom=False)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=114, seed=seed))
    assert not damage


def test_zoom_for_action_falls_back_to_newest_state_without_history():
    server, shooter, _target, _damage = _sniper_duel()
    shooter.input.zoom = True
    assert shooter.zoom_for_action(500) is True
    shooter.input.zoom = False
    assert shooter.zoom_for_action(None) is False


# --- reload: the client's labels prove the reload finished ---------------

def _clock(monkeypatch, start=1000.0):
    import server.player as player_module

    state = {"now": float(start)}
    monkeypatch.setattr(player_module.time, "monotonic", lambda: state["now"])
    return state


def _fire(server, shooter, label, direction=(1.0, 0.0, 0.0)):
    before = shooter.ammo_clip
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=label))
    return shooter.ammo_clip < before or (before == 0 and shooter.ammo_clip == 0
                                          and not shooter.reloading)


def _stream(shooter, start, end):
    """Scoped-in ClientData frames (zoomed sniper shots are exact)."""

    for label in range(start, end + 1):
        _client_data(shooter, label, zoom=True)


def test_sniper_shot_after_late_reload_packet_is_accepted(monkeypatch):
    clock = _clock(monkeypatch)
    server, shooter, target, damage = _sniper_duel(distance=20.0)
    direction = _aim(shooter.eye, (target.x, target.y, target.z + 0.75))
    _stream(shooter, 1000, 1010)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=1010))
    assert len(damage) == 1 and shooter.ammo_clip == 0
    # Client: weapon_shoot animation 1.0 s (60 frames), then a 2.0 s reload
    # (120 frames), then it fires at label 1190. Its WeaponReload datagram
    # was lost and arrived 0.6 s late, so the server reload ends 0.6 s
    # after the client's.
    clock["now"] += 1.0 + 0.6
    assert shooter.start_reload()
    clock["now"] += 2.0 - 0.6
    _stream(shooter, 1011, 1190)
    assert shooter.reloading  # 0.6 s left on the server timer
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=1190))
    assert len(damage) == 2, "post-reload shot was dropped"
    assert not shooter.reloading
    assert shooter.ammo_clip == 0 and shooter.ammo_reserve == 6


def test_label_reload_cannot_shorten_the_stock_cycle(monkeypatch):
    clock = _clock(monkeypatch)
    server, shooter, target, damage = _sniper_duel(distance=20.0)
    direction = _aim(shooter.eye, (target.x, target.y, target.z + 0.75))
    _stream(shooter, 1000, 1010)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=1010))
    clock["now"] += 0.2
    assert shooter.start_reload()  # a modified client skipping the animation
    clock["now"] += 1.0
    # 100 frames after the earliest stock reload start: too early.
    _stream(shooter, 1011, 1170)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=1170))
    assert len(damage) == 1
    assert shooter.reloading


def test_manual_reload_with_rounds_left_keeps_arrival_timing(monkeypatch):
    clock = _clock(monkeypatch)
    server = _server()
    shooter = _player(
        server, player_id=0, team=TEAM1, position=(60.5, 100.5, EYE_Z),
        class_id=C.CLASS_ROCKETEER, loadout=[C.SMG_TOOL], tool=C.SMG_TOOL,
    )
    _stream(shooter, 1000, 1010)
    server.combat.handle_shot(shooter, _shot(shooter, (1.0, 0.0, 0.0), label=1010))
    assert shooter.ammo_clip == 24
    clock["now"] += 0.5
    assert shooter.start_reload()
    _stream(shooter, 1011, 1400)
    clock["now"] += 0.3  # far from the 1.25 s reload end
    server.combat.handle_shot(shooter, _shot(shooter, (1.0, 0.0, 0.0), label=1400))
    assert shooter.reloading and shooter.ammo_clip == 24


def test_sniper_stock_cycle_earliest_shot_is_interval_plus_reload(monkeypatch):
    """Single-shot sniper (clip 1), retail cycle (weapon.py use_primary,
    character.pyd update_alive/end_reload): the round at L schedules the
    reload, which starts when weapon_shoot ends (L + shoot_interval) and
    lasts reload_time. With the WeaponReload on time, a shot halfway through
    the reload is refused and the stock post-reload shot is accepted."""

    clock = _clock(monkeypatch)
    server, shooter, target, damage = _sniper_duel(distance=20.0)
    direction = _aim(shooter.eye, (target.x, target.y, target.z + 0.75))
    _stream(shooter, 1000, 1010)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=1010))
    assert len(damage) == 1 and shooter.ammo_clip == 0
    clock["now"] += 1.0  # weapon_shoot (shoot_interval 1.0 s) ends
    assert shooter.start_reload()
    clock["now"] += 1.0  # halfway through the 2.0 s reload
    _stream(shooter, 1011, 1130)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=1130))
    assert len(damage) == 1 and shooter.reloading
    clock["now"] += 1.0
    _stream(shooter, 1131, 1190)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=1190))
    assert len(damage) == 2, "the stock post-reload sniper shot was dropped"
    assert shooter.ammo_clip == 0 and shooter.ammo_reserve == 6


@pytest.mark.parametrize("label, accepted", [(1188, False), (1189, True)])
def test_sniper_label_floor_is_interval_plus_reload(monkeypatch, label, accepted):
    """With the reload packet late, only the frame labels can prove the
    reload finished: the earliest provable frame is shot + shoot_interval +
    reload_time (60 + 120 frames, one frame of timer slack)."""

    clock = _clock(monkeypatch)
    server, shooter, target, damage = _sniper_duel(distance=20.0)
    direction = _aim(shooter.eye, (target.x, target.y, target.z + 0.75))
    _stream(shooter, 1000, 1010)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=1010))
    clock["now"] += 1.0 + 0.9  # the WeaponReload arrived 0.9 s late
    assert shooter.start_reload()
    clock["now"] += 1.1
    _stream(shooter, 1011, label)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=label))
    assert (len(damage) == 2) is accepted


def test_launcher_reload_packet_never_starts_the_gun_reload(monkeypatch):
    """RPGWeapon is a stock Weapon, so its auto reload sends WeaponReload.
    The server must not start the reload of the last-held gun for it."""

    clock = _clock(monkeypatch)
    server = _server()
    player = _player(
        server, player_id=0, team=TEAM1, position=(60.5, 100.5, EYE_Z),
        class_id=C.CLASS_ROCKETEER, loadout=[C.RPG_TOOL, C.SMG_TOOL],
        tool=C.SMG_TOOL,
    )
    player.ammo_clip = 3  # the SMG has room to reload
    gun_ammo = (player.ammo_clip, player.ammo_reserve)
    player.set_tool(C.RPG_TOOL, raw=True)
    # Full rocket clip: the stock client cannot reload (is_reloadable).
    assert not player.start_reload()
    assert player.consume_oriented_item(C.RPG_TOOL, now=clock["now"])
    clock["now"] += 0.7  # weapon_shoot ends, Character.reload runs
    assert player.start_reload()  # relayed: the rocket reload is stock
    assert not player.reloading
    assert (player.ammo_clip, player.ammo_reserve) == gun_ammo
    # The rocket cycle itself stays on the time-inferred launcher model.
    assert not player.can_use_oriented_item(C.RPG_TOOL, now=clock["now"] + 1.0,
                                            report_violation=False)
    assert player.can_use_oriented_item(C.RPG_TOOL, now=clock["now"] + 1.5,
                                        report_violation=False)


@pytest.mark.parametrize(
    "tool, class_id",
    [
        (C.RIFLE_TOOL, C.CLASS_SOLDIER),
        (C.PISTOL_TOOL, C.CLASS_SOLDIER),
        (C.SHOTGUN_TOOL, C.CLASS_SOLDIER),
        (C.SNIPER2_TOOL, C.CLASS_SCOUT),
        (C.SNUB_PISTOL_TOOL, C.CLASS_SOLDIER),
    ],
)
def test_held_trigger_cadence_is_accepted_at_the_shoot_interval(monkeypatch, tool, class_id):
    """Retail has no semi-automatic weapons: Character.update_weapon fires
    every update while shoot_primary is set, limited by shoot_delay alone.
    A held trigger's rounds land exactly one shoot_interval apart on the
    client clock and must all register; two frames sooner must not (one
    frame is the action clock's FIRE_RATE_GRACE for dt accumulation)."""

    from server import action_clock
    from server.game_constants import WEAPON_PROFILES

    clock = _clock(monkeypatch)
    server = _server()
    shooter = _player(
        server, player_id=0, team=TEAM1, position=(60.5, 100.5, EYE_Z),
        class_id=class_id, loadout=[tool], tool=tool,
    )
    profile = WEAPON_PROFILES[int(tool)]
    frames = action_clock.interval_frames(float(profile.fire_interval))
    label = 1000
    _stream(shooter, label - 10, label)
    rounds = int(profile.clip_size)
    for shot in range(rounds):
        if shot:
            label += frames
            clock["now"] += frames / 60.0
            _stream(shooter, label - frames + 1, label)
        before = shooter.ammo_clip
        server.combat.handle_shot(shooter, _shot(shooter, (1.0, 0.0, 0.0), label=label))
        assert shooter.ammo_clip == before - 1, f"held round {shot} was dropped"
    assert shooter.ammo_clip == 0

    # Two frames early is refused.
    server2 = _server()
    early = _player(
        server2, player_id=0, team=TEAM1, position=(60.5, 100.5, EYE_Z),
        class_id=class_id, loadout=[tool], tool=tool,
    )
    _stream(early, 2000, 2010)
    server2.combat.handle_shot(early, _shot(early, (1.0, 0.0, 0.0), label=2010))
    clock["now"] += (frames - 2) / 60.0
    _stream(early, 2011, 2010 + frames - 2)
    before = early.ammo_clip
    server2.combat.handle_shot(early, _shot(early, (1.0, 0.0, 0.0), label=2010 + frames - 2))
    assert early.ammo_clip == before


# --- diagnostics ---------------------------------------------------------

def test_dropped_shot_is_logged_with_reason(caplog, monkeypatch):
    clock = _clock(monkeypatch)
    server, shooter, target, damage = _sniper_duel(distance=20.0)
    direction = _aim(shooter.eye, (target.x, target.y, target.z + 0.75))
    _stream(shooter, 1000, 1010)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=1010))
    caplog.set_level("INFO", logger="combat.shots")
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=1011))
    lines = [r.getMessage() for r in caplog.records if r.name == "combat.shots"]
    assert lines and "reason=empty_clip" in lines[0]
    assert "distance=20.0" in lines[0] and "tool=18" in lines[0]


def test_near_miss_of_a_sniper_is_logged(caplog):
    server, shooter, target, damage = _sniper_duel(distance=40.0)
    _stream(shooter, 1000, 1010)
    caplog.set_level("INFO", logger="combat.shots")
    # Aim 0.9 blocks beside the torso centre: a near miss.
    direction = _aim(shooter.eye, (target.x, target.y + 0.9, target.z + 0.75))
    for label in range(1000, 1011):
        _client_data(shooter, label, zoom=True)
    server.combat.handle_shot(shooter, _shot(shooter, direction, label=1010))
    if damage:
        pytest.skip("geometry hit the arm box")
    lines = [r.getMessage() for r in caplog.records if r.name == "combat.shots"]
    assert lines and lines[0].startswith("shot missed")
    assert "zoom=True" in lines[0]


# --- end to end ------------------------------------------------------------

def test_lab_zoomed_sniper_registers_over_a_jittery_link():
    import asyncio
    import sys
    from pathlib import Path

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import shot_registration_lab as lab

    result = asyncio.run(lab.run_case(
        lab.WEAPONS["sniper"], lab.PROFILES["ping200"], "strafe", 90.0,
        seconds=14.0, seed=1, clientdata_after_shot=True,
    ))
    assert result.sent >= 4
    assert result.reasons.get("miss:server_unzoomed", 0) == 0
    assert not any(reason.startswith("dropped") for reason in result.reasons)
    assert result.hits == result.resolved


# --- lag compensation of a retransmitted (late) shot ------------------------

def test_retransmitted_shot_is_rewound_to_the_frame_that_fired_it():
    """A shot datagram lost once arrives ~300 ms late. Its frame's ClientData
    (unsequenced, not held back) dates it; the rewind adds that delay."""

    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    shooter = _player(
        server, player_id=0, team=TEAM1, position=(100.5, 100.5, EYE_Z),
        class_id=C.CLASS_SOLDIER, loadout=[C.RIFLE_TOOL], tool=C.RIFLE_TOOL,
    )
    shooter.connection.peer = SimpleNamespace(
        roundTripTime=100, roundTripTimeVariance=5)
    target = _player(
        server, player_id=1, team=TEAM2, position=(95.5, 130.5, EYE_Z),
        class_id=C.CLASS_SOLDIER, loadout=[C.RIFLE_TOOL], tool=C.RIFLE_TOOL,
    )
    damage = []
    target.damage = lambda amount, source=None, kill_type=0, **_k: damage.append(amount)
    speed = 0.3  # blocks per tick across the line of fire

    def x_at(label):
        return 95.5 + speed * label

    # 40 ticks of strafing; ClientData label L arrives on the tick it is made.
    for tick in range(1, 41):
        server.loop_count = tick
        lc.record_player(target)
        target.set_position(x_at(tick), 130.5, EYE_Z)
        _client_data(shooter, 1000 + tick, zoom=False, aim=(0.0, 1.0, 0.0))
    # The shot of label 1022 (tick 22) saw the body 6 ticks (100 ms) old.
    seen = (x_at(16), 130.5, EYE_Z + 0.75)
    direction = _aim(shooter.eye, seen)
    server.loop_count = 41  # its datagram was retransmitted: 19 ticks late
    packet = _shot(shooter, direction, label=1022)
    packet.shot_on_world_update = 16
    ctx = lc.rewind_targets(server, shooter, packet)
    assert ctx is not None
    assert ctx.rewind_ms == pytest.approx(100.0 + 18 * 1000.0 / 60.0, abs=1.0)
    server.combat.handle_shot(shooter, packet)
    assert damage, "late shot missed the body the shooter saw"
    counts = getattr(shooter, "anticheat_counts", {})
    assert counts.get("lag_comp_late_shot:observed", 0) >= 1


def test_late_shot_allowance_is_capped():
    server = BattleSpadesServer(ServerConfig())
    server.world_manager.generate_flat_map()
    server.config.lag_compensation_late_shot_ms = 200.0
    shooter = _player(
        server, player_id=0, team=TEAM1, position=(100.5, 100.5, EYE_Z),
        class_id=C.CLASS_SOLDIER, loadout=[C.RIFLE_TOOL], tool=C.RIFLE_TOOL,
    )
    shooter.connection.peer = SimpleNamespace(roundTripTime=100)
    server.loop_count = 10
    _client_data(shooter, 500, zoom=False)
    server.loop_count = 70  # one second later: a held-back ("backtrack") shot
    packet = _shot(shooter, (1.0, 0.0, 0.0), label=500)
    assert lc.late_shot_ms(server, shooter, packet, 1000.0 / 60.0) == 200.0
    server.config.lag_compensation_late_shot_ms = 0.0
    assert lc.late_shot_ms(server, shooter, packet, 1000.0 / 60.0) == 0.0


def test_zoom_decided_by_newest_frame_before_the_shot_when_neighbours_lag():
    """Jitter: the shot's own (post-shot, zoom-off) ClientData arrived, the
    three frames before it have not; an older scoped frame decides."""

    _server_, shooter, _target, _damage = _sniper_duel()
    for label in range(100, 106):
        _client_data(shooter, label, zoom=True)
    _client_data(shooter, 110, zoom=False)
    assert shooter.zoom_for_action(110) is True
    for label in range(111, 116):
        _client_data(shooter, label, zoom=False)
    assert shooter.zoom_for_action(115) is False


def test_bloom_recovers_on_client_frame_labels_not_arrival_time():
    """Two SMG shots one second apart on the client, delivered together after
    a retransmission: the second must not get the bunched-up bloom."""

    server = _server()
    shooter = _player(
        server, player_id=0, team=TEAM1, position=(60.5, 100.5, EYE_Z),
        class_id=C.CLASS_ROCKETEER, loadout=[C.SMG_TOOL], tool=C.SMG_TOOL,
    )
    for label in range(1000, 1071):
        _client_data(shooter, label, zoom=False)
    combat = server.combat
    profile = shooter.get_weapon_profile()
    combat._seeded_pellet_directions(shooter, (1.0, 0.0, 0.0), profile,
                                     _shot(shooter, (1.0, 0.0, 0.0), label=1005), 50.0)
    combat._seeded_pellet_directions(shooter, (1.0, 0.0, 0.0), profile,
                                     _shot(shooter, (1.0, 0.0, 0.0), label=1065), 50.0)
    # SMG: min 1, +0.2 per shot, -1.0/s: one second of labels fully recovers.
    assert combat._pellet_spread[shooter.id]["spread"] == pytest.approx(1.2)


def test_empty_shotgun_first_shell_proven_by_labels(monkeypatch):
    clock = _clock(monkeypatch)
    server = _server()
    shooter = _player(
        server, player_id=0, team=TEAM1, position=(60.5, 100.5, EYE_Z),
        class_id=C.CLASS_MINER, loadout=[C.SHOTGUN_TOOL], tool=C.SHOTGUN_TOOL,
    )
    _stream(shooter, 1000, 1001)
    label = 1001
    for _ in range(int(shooter.ammo_clip)):
        server.combat.handle_shot(shooter, _shot(shooter, (1.0, 0.0, 0.0), label=label))
        label += 60
        clock["now"] += 1.0
        _stream(shooter, label - 59, label)
    assert shooter.ammo_clip == 0
    # The client starts its shell reload right after the 1.0 s animation;
    # the reload packet arrives 0.4 s late. One shell (0.5 s) later it fires.
    clock["now"] += 0.4
    assert shooter.start_reload()
    fire = label - 60 + 60 + 30
    _stream(shooter, label + 1, fire)
    clock["now"] += 0.1
    server.combat.handle_shot(shooter, _shot(shooter, (1.0, 0.0, 0.0), label=fire))
    assert shooter.ammo_clip == 0 and not shooter.reloading
    assert shooter.ammo_reserve == 19
