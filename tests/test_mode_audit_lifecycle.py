"""Independent lifecycle boundaries found in the 68c36ca mode audit."""

import asyncio
from types import SimpleNamespace

import pytest
import shared.constants_gamemode as CG
from modes.multi_hill import MultiHillMode
from modes.zombie import SURVIVOR_TEAM, ZOMBIE_TEAM, ZombiePhase
from server.game_constants import TEAM1
from tests.test_multi_hill import _Server as HillServer
from tests.test_zombie_rounds import _active_mode
from tests.test_diamond_mine_fixes import _mode as diamond_mode
from tests.test_recovered_objective_modes import _Player


@pytest.mark.parametrize("event", ["infection", "last_survivor_kill"])
def test_zombie_queued_combat_cannot_change_a_finished_match(event):
    server, mode = _active_mode(2)
    survivor = next(p for p in server.players.values() if p.team == SURVIVOR_TEAM)
    zombie = next(p for p in server.players.values() if p.team == ZOMBIE_TEAM)

    async def suppress_presentation(_winner):
        pass

    mode._run_end_sequence = suppress_presentation

    async def scenario():
        if event == "infection":
            # Admin/endround calls this directly during the active outbreak.
            await mode.on_mode_end(SURVIVOR_TEAM)
        else:
            # A map transition retires the mode before detaching its roster.
            assert mode.last_survivor_id == survivor.id
            mode.begin_retirement()
        assert mode.ended and mode.phase is ZombiePhase.ACTIVE
        original = (survivor.team, survivor.score, zombie.score)
        server.packets.clear()
        if event == "infection":
            survivor.alive = survivor.spawned = False
            await mode.on_player_death(survivor, zombie, 0)
        else:
            zombie.alive = zombie.spawned = False
            await mode.on_player_kill(survivor, zombie, 0)
        assert (survivor.team, survivor.score, zombie.score) == original
        assert server.packets == []

    asyncio.run(scenario())


def test_multihill_stalled_tick_cannot_score_beyond_hill_expiry(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("modes.multi_hill.time.time", lambda: now[0])
    monkeypatch.setattr("modes.multi_hill.trigger_airstrike", lambda *_args: None)
    server = HillServer()
    server.config.mode_settings["mh"].update(score_limit=1000, base_active_time=10)
    mode = MultiHillMode(server)
    asyncio.run(mode.on_mode_start())
    player = SimpleNamespace(id=7, team=TEAM1, alive=True, spawned=True,
                             position=mode.active_zones[0].center, score=0)
    server.players[player.id] = player
    asyncio.run(mode.on_tick(0))
    claim_score = player.score

    # One delayed callback crosses the configured active-time boundary.
    now[0] = 135.0
    asyncio.run(mode.on_tick(1))

    assert mode.phase == "intermission"
    assert server.teams[TEAM1].score == 10 * int(CG.MH_TEAM_SCORE_PER_TICK)
    assert player.score - claim_score == 2 * int(CG.MH_SCORE_OCCUPY)


def test_multihill_first_arrival_after_expiry_cannot_claim_or_score(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("modes.multi_hill.time.time", lambda: now[0])
    monkeypatch.setattr("modes.multi_hill.trigger_airstrike", lambda *_args: None)
    server = HillServer()
    server.config.mode_settings["mh"].update(score_limit=1000, base_active_time=10)
    mode = MultiHillMode(server)
    asyncio.run(mode.on_mode_start())
    player = SimpleNamespace(id=7, team=TEAM1, alive=True, spawned=True,
                             position=mode.active_zones[0].center, score=0)
    server.players[player.id] = player
    now[0] = 135.0

    asyncio.run(mode.on_tick(1))

    assert mode.phase == "intermission"
    assert server.teams[TEAM1].score == 0
    assert player.score == 0


@pytest.mark.parametrize("retirement", ["expiry", "deactivate"])
def test_retired_diamond_releases_its_miner_history(monkeypatch, retirement):
    now = [100.0]
    server, mode = diamond_mode(monkeypatch, now)
    asyncio.run(mode.on_mode_start())
    miner = _Player(1, TEAM1, (0, 0, 50))
    server.players[miner.id] = miner
    diamond = mode._spawn_diamond((120.5, 100.5, 50.5), now=now[0], uncovered_by=miner)
    assert mode._diamond_history[diamond.serial][0] is miner
    server.players.clear()

    if retirement == "expiry":
        now[0] = diamond.expires_at + 0.1
        asyncio.run(mode.on_tick(1))
    else:
        asyncio.run(mode.deactivate())

    assert not mode.ground_diamonds
    assert not mode._diamond_history


def test_picked_up_diamond_keeps_miner_history_until_cash_in(monkeypatch):
    now = [100.0]
    server, mode = diamond_mode(monkeypatch, now)
    asyncio.run(mode.on_mode_start())
    miner = _Player(1, TEAM1, (120.5, 100.5, 50.5))
    server.players[miner.id] = miner
    diamond = mode._spawn_diamond(miner.position, now=now[0], uncovered_by=miner)
    mode._pickup_diamond(miner, diamond)
    assert not mode.ground_diamonds
    assert mode._diamond_history[diamond.serial] == (miner, {TEAM1})

    miner.set_position(mode.active_dropoffs[0].zone.center)
    asyncio.run(mode.on_tick(1))

    assert server.teams[TEAM1].score == 1
    assert not mode._diamond_history
