"""Map-escape / hiding reveal (server/escape_watch.py)."""

import asyncio
from types import SimpleNamespace

import pytest

import shared.constants as C
from modes.tdm import TDMMode
from server import escape_watch
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2, TEAM_SPECTATOR
from server.main import BattleSpadesServer
from shared.packet import ChangePlayer

GROUND = 62  # generate_flat_map's plateau


def _marker(player_id, visible):
    packet = ChangePlayer()
    packet.player_id = int(player_id)
    packet.type = int(C.SET_HIGH_MINIMAP_VISIBILITY)
    packet.high_minimap_visibility = int(bool(visible))
    return bytes(packet.generate())


@pytest.fixture
def ctx():
    async def build():
        server = BattleSpadesServer(ServerConfig())
        server.world_manager.generate_flat_map()
        server.mode = TDMMode(server)
        await server.mode.on_mode_start()
        return server

    server = asyncio.run(build())
    sent = []
    server.broadcast = lambda data, reliable=False, **kw: sent.append(bytes(data))
    return SimpleNamespace(server=server, world=server.world_manager, sent=sent)


def _player(server, pid, position, team=TEAM1):
    player = SimpleNamespace(
        id=pid, name=f"p{pid}", team=team, alive=True, spawned=True,
        position=tuple(float(v) for v in position),
        orientation=(1.0, 0.0, 0.0),
    )
    server.players[pid] = player
    return player


def _standing(x, y, ground=GROUND):
    return (x + 0.5, y + 0.5, ground - float(C.PLAYER_STANDING_POS_ABOVE_GROUND))


def _box(world, x, y, z_top, z_bottom, *, open_side=False):
    """Seal cells (x, y, z_top+1 .. z_bottom-1) inside solid blocks."""

    for z in range(z_top, z_bottom + 1):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                inner = dx == 0 and dy == 0 and z_top < z < z_bottom
                if not inner:
                    world.set_block(x + dx, y + dy, z, True, 0x808080)
    if open_side:
        world.set_block(x + 1, y, z_bottom - 1, False)
        world.set_block(x + 1, y, z_bottom - 2, False)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------

def test_classify_map_escapes(ctx):
    world = ctx.world
    cases = {
        "out_of_bounds": (-3.0, 100.0, 50.0),
        "below_floor": (100.5, 100.5, 239.0),
        "sky": (100.5, 100.5, -6.0),
    }
    for expected, position in cases.items():
        player = SimpleNamespace(position=position)
        assert escape_watch.classify(world, player, objective=False) == expected


def test_normal_ground_trench_and_open_hut_are_never_flagged(ctx):
    world = ctx.world
    standing = SimpleNamespace(position=_standing(50, 50))
    assert escape_watch.classify(world, standing, objective=True) is None
    # A 3-deep trench open to the sky.
    for z in range(GROUND, GROUND + 3):
        world.set_block(60, 60, z, False)
    trench = SimpleNamespace(position=_standing(60, 60, GROUND + 3))
    assert escape_watch.classify(world, trench, objective=True) is None
    # A roofed hut with a doorway.
    _box(world, 80, 80, GROUND - 4, GROUND, open_side=True)
    hut = SimpleNamespace(position=_standing(80, 80, GROUND))
    assert escape_watch.classify(world, hut, objective=True) is None


def test_sealed_pocket_only_matters_for_objective_players(ctx):
    world = ctx.world
    _box(world, 90, 90, GROUND - 4, GROUND)
    sealed = SimpleNamespace(position=_standing(90, 90, GROUND))
    assert escape_watch.classify(world, sealed, objective=True) == "entombed"
    assert escape_watch.classify(world, sealed, objective=False) is None


def test_body_inside_blocks_is_embedded(ctx):
    world = ctx.world
    for z in range(GROUND - 4, GROUND):
        world.set_block(70, 70, z, True, 0x808080)
    stuck = SimpleNamespace(position=(70.5, 70.5, GROUND - 3.5))
    assert escape_watch.classify(world, stuck, objective=False) == "embedded"


# ---------------------------------------------------------------------------
# marker lifecycle
# ---------------------------------------------------------------------------

def test_sky_escape_is_revealed_after_hold_then_cleared(ctx):
    server = ctx.server
    player = _player(server, 5, (100.5, 100.5, -8.0))
    escape_watch.tick(server, now=100.0, force=True)
    assert ctx.sent == []  # under the 5 s hold
    escape_watch.tick(server, now=106.0, force=True)
    assert ctx.sent == [_marker(5, True)]
    assert escape_watch.is_flagged(server, player) == "sky"

    player.position = _standing(100, 100)
    escape_watch.tick(server, now=107.0, force=True)
    assert ctx.sent == [_marker(5, True)]  # hysteresis: one clean check
    escape_watch.tick(server, now=108.0, force=True)
    assert ctx.sent == [_marker(5, True), _marker(5, False)]
    assert escape_watch.is_flagged(server, player) is None


def test_out_of_bounds_is_immediate_and_throttled_to_the_interval(ctx):
    server = ctx.server
    _player(server, 6, (600.0, 100.0, 50.0))
    escape_watch.tick(server, now=10.0)
    assert ctx.sent == [_marker(6, True)]
    ctx.sent.clear()
    escape_watch.tick(server, now=10.2)  # within 1 s: no work
    escape_watch.tick(server, now=10.4)
    assert ctx.sent == []


def test_carrier_entombed_is_revealed_but_teammates_in_bunkers_are_not(ctx, monkeypatch):
    server = ctx.server
    _box(ctx.world, 90, 90, GROUND - 4, GROUND)
    _box(ctx.world, 95, 95, GROUND - 4, GROUND)
    carrier = _player(server, 7, _standing(90, 90, GROUND))
    camper = _player(server, 8, _standing(95, 95, GROUND))
    monkeypatch.setattr(
        server.mode, "escape_watch_objective_player", lambda p: p is carrier, raising=False
    )
    escape_watch.tick(server, now=0.0, force=True)
    escape_watch.tick(server, now=6.0, force=True)
    assert ctx.sent == [_marker(7, True)]
    assert escape_watch.is_flagged(server, camper) is None


def test_mode_owned_marker_is_never_cleared_and_is_reasserted(ctx, monkeypatch):
    server = ctx.server
    player = _player(server, 9, (-4.0, 10.0, 50.0))
    marks = {"on": True}
    monkeypatch.setattr(
        server.mode, "mode_marks_player", lambda p: marks["on"], raising=False
    )
    escape_watch.tick(server, now=0.0, force=True)
    assert ctx.sent == []  # the mode already shows the marker
    marks["on"] = False  # e.g. the intel was captured
    escape_watch.tick(server, now=1.0, force=True)
    assert ctx.sent == [_marker(9, True)]
    # Resolved while the mode marks again: the watch must not clear it.
    marks["on"] = True
    player.position = _standing(10, 10)
    escape_watch.tick(server, now=2.0, force=True)
    escape_watch.tick(server, now=3.0, force=True)
    assert ctx.sent == [_marker(9, True)]


def test_death_and_departure_release_the_marker_safely(ctx):
    server = ctx.server
    dead = _player(server, 11, (-4.0, 10.0, 50.0))
    gone = _player(server, 12, (-4.0, 12.0, 50.0))
    escape_watch.tick(server, now=0.0, force=True)
    assert len(ctx.sent) == 2
    ctx.sent.clear()
    dead.alive = False
    del server.players[12]
    # A new joiner reuses id 12: never address it with the old marker.
    _player(server, 12, _standing(20, 20))
    escape_watch.tick(server, now=1.0, force=True)
    assert ctx.sent == [_marker(11, False)]
    assert escape_watch.is_flagged(server, gone) is None


def test_late_joiner_receives_current_markers(ctx):
    server = ctx.server
    _player(server, 13, (-4.0, 10.0, 50.0))
    escape_watch.tick(server, now=0.0, force=True)
    received = []
    connection = SimpleNamespace(send=lambda data, reliable=False: received.append(bytes(data)))
    server.mode.reveal_to(connection)
    assert _marker(13, True) in received


def test_spectators_and_disabled_modes_are_ignored(ctx, monkeypatch):
    server = ctx.server
    _player(server, 14, (-4.0, 10.0, 50.0), team=TEAM_SPECTATOR)
    escape_watch.tick(server, now=0.0, force=True)
    assert ctx.sent == []
    _player(server, 15, (-4.0, 10.0, 50.0), team=TEAM2)
    monkeypatch.setattr(server.mode, "escape_watch_enabled", False, raising=False)
    escape_watch.tick(server, now=5.0, force=True)
    assert ctx.sent == []


def test_tutorial_and_ugc_opt_out():
    from modes.tutorial import TutorialMode
    from modes.ugc import UGCMode

    assert TutorialMode.escape_watch_enabled is False
    assert UGCMode.escape_watch_enabled is False


def test_base_mode_tick_drives_the_watch(ctx):
    server = ctx.server
    _player(server, 16, (-4.0, 10.0, 50.0))
    asyncio.run(server.mode.on_tick(1))
    assert _marker(16, True) in ctx.sent


def test_sealed_zombie_survivor_earns_no_survival_score(ctx, monkeypatch):
    from modes.zombie import ZombieMode

    mode = ZombieMode(ctx.server)
    sealed = SimpleNamespace(id=21, name="hider")
    free = SimpleNamespace(id=22, name="runner")
    monkeypatch.setattr(mode, "_living_survivors", lambda: [sealed, free])
    monkeypatch.setattr(
        escape_watch, "is_flagged", lambda server, player: player is sealed
    )
    awarded = []
    monkeypatch.setattr(
        mode, "_award_player", lambda player, points, **kw: awarded.append(player)
    )
    mode._next_survival_score_at = 100.0
    mode._next_last_man_score_at = None
    mode._award_periodic_survival_score(101.0)
    assert awarded == [free]
    # Everyone sealed: nothing paid, and the clock does not bank the time.
    awarded.clear()
    monkeypatch.setattr(escape_watch, "is_flagged", lambda server, player: True)
    due = mode._next_survival_score_at
    mode._award_periodic_survival_score(due + 1.0)
    assert awarded == []
    assert mode._next_survival_score_at > due + 1.0
