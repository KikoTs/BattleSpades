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

    def broadcast(self, data):
        self.broadcast_packets.append(data)

    def respawn_player(self, player):
        self.respawned.append(player.id)


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
    # Ending music (PlayMusic 26) up front, the game_ending range string.
    assert 26 in ids
    first_music = next(d for d in srv.broadcast_packets if d and d[0] == 26)
    assert PlayMusic(ByteReader(first_music[1:])).name == "game_ending_001-004"
    # Stats screen packets present, GameStats(67) before ShowGameStats(53); MapEnded(52) too.
    assert 67 in ids and 53 in ids and 52 in ids
    assert ids.index(67) < ids.index(53)
    # Restart happened: teams reset + everyone respawned; mode revived.
    assert srv.teams[0].reset_called >= 1
    assert srv.respawned == [7]
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
