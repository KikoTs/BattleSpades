"""End-of-round sequence orchestration.

Drives BaseMode.on_mode_end and asserts the server emits the full sequence
(victory music -> stats screen -> restart) in order, with asyncio.sleep
shrunk to a bare yield so the delays don't actually block.
"""
import asyncio
import sys
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

from shared.bytes import ByteReader  # noqa: E402
from shared.packet import PlayMusic  # noqa: E402
from modes.base_mode import BaseMode  # noqa: E402

_REAL_SLEEP = asyncio.sleep


async def _fast_sleep(_seconds):
    # Yield control (so the fire-and-forget end task interleaves) but don't wait.
    await _REAL_SLEEP(0)


class _Team:
    def __init__(self, tid):
        self.id = tid
        self.score = 0
        self.name = "T%d" % tid
        self.reset_called = 0

    def reset(self):
        self.score = 0
        self.reset_called += 1


class _Player:
    def __init__(self, pid, team):
        self.id = pid
        self.team = team
        self.connection = object()
        self.score = 0


class _WorldMgr:
    def get_spawn_point(self, team):
        return (256.0, 256.0, 40.0)


class _Server:
    def __init__(self):
        self.broadcast_packets = []
        self.teams = {0: _Team(0), 1: _Team(1)}
        self.players = {7: _Player(7, 0)}
        self.world_manager = _WorldMgr()
        self.respawned = []
        self.tick_rate = 60
        self.state_refreshes = 0
        self.runtime_resets = 0
        self.client_rejoins = 0

    def broadcast(self, data):
        self.broadcast_packets.append(data)

    def respawn_player(self, player):
        self.respawned.append(player.id)

    def broadcast_state_data(self):
        self.state_refreshes += 1

    def reset_round_runtime(self):
        self.runtime_resets += 1

    async def restart_connected_clients(self):
        self.client_rejoins += 1


class _Mode(BaseMode):
    name = "Test"
    score_limit = 5
    time_limit = 0

    async def on_mode_start(self):
        await super().on_mode_start()


def _run(coro):
    return asyncio.run(coro)


def test_end_sequence_emits_music_stats_and_restarts(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    async def scenario():
        srv = _Server()
        mode = _Mode(srv)
        await mode.on_mode_start()
        srv.broadcast_packets.clear()      # drop the round-start music
        srv.respawned.clear()

        await mode.on_mode_end(winner=0)
        for _ in range(8):                 # let the end task run to completion
            await _REAL_SLEEP(0)
        return srv, mode

    srv, mode = _run(scenario())
    ids = [d[0] for d in srv.broadcast_packets if d]
    # Ending music: StopMusic(27) then PlayMusic(26) with a SPECIFIC game_ending
    # track (the client won't override playing music, and won't resolve ranges).
    assert 27 in ids and 26 in ids
    from server import audio as _audio
    first_music = next(d for d in srv.broadcast_packets if d and d[0] == 26)
    assert PlayMusic(ByteReader(first_music[1:])).name in _audio.GAME_ENDING_TRACKS
    # GameStats data is safe in the live GameScene.  The terminal
    # ShowGameStats/MapEnded packets must never be used for a same-map restart:
    # both tear down GameScene in the retail client.
    assert 67 in ids
    assert 53 not in ids
    assert 52 not in ids
    assert 72 not in ids
    # Restart happened: teams reset + everyone respawned; mode revived.
    assert srv.teams[0].reset_called >= 1
    assert srv.respawned == [7]
    assert srv.state_refreshes == 0
    assert srv.runtime_resets == 1
    assert srv.client_rejoins == 0
    assert mode.ended is False


def test_end_sequence_runs_once(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    async def scenario():
        srv = _Server()
        mode = _Mode(srv)
        await mode.on_mode_start()
        await mode.on_mode_end(winner=0)
        await mode._run_end_sequence(0)    # second call — must be guarded
        for _ in range(8):
            await _REAL_SLEEP(0)
        return srv

    srv = _run(scenario())
    # Exactly one restart (one reset), not two.
    assert srv.teams[0].reset_called == 1
