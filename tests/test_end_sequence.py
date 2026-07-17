"""End-of-round sequence orchestration.

Drives BaseMode.on_mode_end and asserts the server emits the full sequence
(victory music -> stats screen -> restart) in order, with asyncio.sleep
shrunk to a bare yield so the delays don't actually block.
"""
import asyncio
import sys
import time
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


def test_timed_round_opens_map_vote_during_final_minute(monkeypatch):
    calls = []
    srv = _Server()
    srv.vote_manager = SimpleNamespace(
        ensure_map_vote=lambda now: calls.append(now) or True
    )
    mode = _Mode(srv)
    mode.time_limit = 120
    mode.started = True
    mode.start_time = time.time() - 61.0
    monkeypatch.setattr("server.audio.play_timeout_music", lambda _server: None)

    asyncio.run(mode.on_tick(1))

    assert len(calls) == 1


def test_end_sequence_consumes_voted_map_instead_of_same_map_restart(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    class _Transition:
        def __init__(self):
            self.maps = []
            self.restarts = 0

        async def change_map(self, map_name):
            self.maps.append(map_name)
            return SimpleNamespace(ok=True, message="ok")

        async def restart_round(self):
            self.restarts += 1
            return SimpleNamespace(ok=True, message="ok")

    async def scenario():
        srv = _Server()
        transition = _Transition()
        srv.match_transition = transition
        srv.vote_manager = SimpleNamespace(
            ensure_map_vote=lambda _now: False,
            consume_next_map=lambda: "CastleWars",
        )
        mode = _Mode(srv)
        await mode.on_mode_start()
        await mode.on_mode_end(0)
        for _ in range(8):
            await _REAL_SLEEP(0)
        return transition

    transition = asyncio.run(scenario())

    assert transition.maps == ["CastleWars"]
    assert transition.restarts == 0


def test_end_sequence_waits_for_vote_then_uses_configured_screen_dwell(
    monkeypatch,
):
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)
    order = []

    class _Vote:
        async def wait_for_map_result(self):
            order.append("vote-resolved")
            return "CastleWars"

        def consume_next_map(self):
            order.append("vote-consumed")
            return "CastleWars"

    class _Transition:
        def __init__(self):
            self.restarts = 0

        async def change_map_after_end_screen(
            self,
            map_name,
            *,
            end_screen_seconds,
        ):
            order.append((map_name, end_screen_seconds))
            return SimpleNamespace(ok=True, message="ok")

        async def restart_round(self):
            self.restarts += 1
            return SimpleNamespace(ok=True, message="ok")

    async def scenario():
        srv = _Server()
        srv.config = SimpleNamespace(
            default_map="London",
            end_screen_seconds=23.0,
        )
        srv.vote_manager = _Vote()
        transition = _Transition()
        srv.match_transition = transition
        mode = _Mode(srv)
        await mode.on_mode_start()
        await mode.on_mode_end(0)
        for _ in range(8):
            await _REAL_SLEEP(0)
        return transition

    transition = asyncio.run(scenario())

    assert order == [
        "vote-resolved",
        "vote-consumed",
        ("CastleWars", 23.0),
    ]
    assert transition.restarts == 0


def test_invalid_voted_map_falls_back_to_safe_same_map_restart(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    class _Transition:
        def __init__(self):
            self.restarts = 0

        async def change_map_after_end_screen(self, *_args, **_kwargs):
            return SimpleNamespace(
                ok=False,
                message="Map not found: Missing",
                reconnect_required=False,
            )

        async def restart_round(self):
            self.restarts += 1
            return SimpleNamespace(ok=True, message="ok")

    async def scenario():
        srv = _Server()
        srv.config = SimpleNamespace(
            default_map="London",
            end_screen_seconds=12.0,
        )
        transition = _Transition()
        srv.match_transition = transition
        srv.vote_manager = SimpleNamespace(
            ensure_round_end_map_vote=lambda _now: False,
            consume_next_map=lambda: "Missing",
        )
        mode = _Mode(srv)
        await mode.on_mode_start()
        await mode.on_mode_end(0)
        for _ in range(8):
            await _REAL_SLEEP(0)
        return transition, mode

    transition, mode = asyncio.run(scenario())

    assert transition.restarts == 1
    assert mode._end_sequence_running is True


def test_same_map_vote_never_enters_terminal_transition(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _fast_sleep)

    class _Transition:
        def __init__(self):
            self.map_changes = 0
            self.restarts = 0

        async def change_map_after_end_screen(self, *_args, **_kwargs):
            self.map_changes += 1
            return SimpleNamespace(ok=True, message="ok")

        async def restart_round(self):
            self.restarts += 1
            return SimpleNamespace(ok=True, message="ok")

    async def scenario():
        srv = _Server()
        srv.config = SimpleNamespace(
            default_map="London",
            end_screen_seconds=12.0,
        )
        transition = _Transition()
        srv.match_transition = transition
        srv.vote_manager = SimpleNamespace(
            ensure_round_end_map_vote=lambda _now: False,
            consume_next_map=lambda: "lOnDoN",
        )
        mode = _Mode(srv)
        await mode.on_mode_start()
        await mode.on_mode_end(0)
        for _ in range(8):
            await _REAL_SLEEP(0)
        return transition

    transition = asyncio.run(scenario())

    assert transition.map_changes == 0
    assert transition.restarts == 1


def test_admin_transition_can_cancel_end_screen_timer(monkeypatch):
    entered_sleep = asyncio.Event()
    release_sleep = asyncio.Event()

    async def blocking_sleep(_seconds):
        entered_sleep.set()
        await release_sleep.wait()

    monkeypatch.setattr(asyncio, "sleep", blocking_sleep)

    async def scenario():
        srv = _Server()
        mode = _Mode(srv)
        await mode.on_mode_start()
        await mode.on_mode_end(0)
        await entered_sleep.wait()
        await mode.cancel_end_sequence()
        return mode, srv

    mode, srv = asyncio.run(scenario())

    assert mode._end_task is None
    assert mode._end_sequence_running is False
    assert srv.runtime_resets == 0
