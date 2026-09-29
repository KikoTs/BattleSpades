"""Statistical anti-cheat detection (server/anticheat_report.py) + admin commands.

Synthetic stats only: a normal player is not flagged, a clear aimbot pattern
is, small samples never are, and bots are neither flagged nor part of the
accuracy population. Detection never kicks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import Counter
from types import SimpleNamespace

import pytest

import shared.constants as C
import commands.anticheat_admin as ac_cmds
from commands.command_handler import CommandContext, handle_command
from server import anticheat_report as acr
from server.config import ServerConfig
from server.profile_stats import ProfileStats

RIFLE = 6
SMG = 7


# --- fakes -------------------------------------------------------------------


def _server(tmp_path=None):
    config = ServerConfig()
    if tmp_path is not None:
        config.anticheat.report_path = str(tmp_path / "anticheat.jsonl")
    server = SimpleNamespace(config=config, players={}, world_manager=None)
    server.get_player_by_name = lambda name: next(
        (p for p in server.players.values() if p.name.lower() == name.lower()), None
    )
    return server


def _stats(weapons: dict) -> dict:
    stats = {
        "shots": 0, "hits": 0, "headshots": 0, "pellet_hits": 0, "weapons": {},
        "origin_error": Counter(), "origin_error_fallback": Counter(),
        "aim_angle": Counter(), "aim_angle_fallback": Counter(),
        "pellet_seeds": Counter(), "rejected": Counter(),
    }
    for tool, (shots, hits, heads) in weapons.items():
        stats["weapons"][tool] = {"shots": shots, "hits": hits, "headshots": heads}
        stats["shots"] += shots
        stats["hits"] += hits
        stats["headshots"] += heads
    return stats


class _Conn:
    def __init__(self):
        self.sent = []
        self.disconnected = None

    def send(self, data, reliable=True, prefix=0x30):
        self.sent.append(bytes(data))


def _player(server, pid, *, team=1, bot=False, weapons=None, kills=None,
            eye=(100.0, 100.0, 30.0), tool=RIFLE):
    player = SimpleNamespace(
        id=pid, name=f"P{pid}", team=team, is_bot=bot, alive=True, spawned=True,
        tool=tool, eye=eye, orientation=(1.0, 0.0, 0.0), kills=0, deaths=0,
        admin=False, connection=_Conn(),
        anticheat_stats=_stats(weapons or {}), anticheat_counts=Counter(),
    )
    player.send = player.connection.send
    player.disconnect = lambda reason=0: setattr(player.connection, "disconnected", reason)
    if kills is not None:
        total, heads = kills
        profile = ProfileStats()
        profile.add(C.KILL_SCORE_REASON, total)
        if heads:
            profile.add(C.KILL_SCORE_HEADSHOT_REASON, heads)
        player.profile_stats = profile
        player.kills = total
    server.players[pid] = player
    return player


def _population(server, count, *, start=100, bot=False):
    """Humans with rifle accuracy spread over 20%..40%."""
    for index in range(count):
        hits = 200 + (index * 200) // max(1, count - 1)  # 200..400 of 1000
        _player(server, start + index, bot=bot, weapons={RIFLE: (1000, hits, hits // 5)})


def _checks(report):
    return {reason["check"] for reason in report["reasons"]}


# --- analysis ------------------------------------------------------------------


def test_normal_player_is_not_flagged():
    server = _server()
    _population(server, 25)
    normal = _player(server, 1, weapons={RIFLE: (800, 280, 70)}, kills=(60, 18))

    report = acr.analyze_player(server, normal)

    assert report["reasons"] == []
    assert report["score"] == 0 and not report["flagged"]


def test_clear_aimbot_pattern_is_flagged():
    server = _server()
    _population(server, 25)
    cheat = _player(server, 1, weapons={RIFLE: (600, 540, 430)}, kills=(45, 41))

    report = acr.analyze_player(server, cheat)

    assert report["flagged"]
    assert {"headshot_kills", "headshot_hits", f"accuracy:{RIFLE}"} <= _checks(report)
    assert report["score"] >= 3.0


def test_small_samples_are_never_flagged():
    server = _server()
    _population(server, 25)
    lucky = _player(server, 1, weapons={RIFLE: (40, 40, 40)}, kills=(12, 12))
    aim = acr.aim_stats(lucky)
    aim["engaged"], aim["snaps"] = 10, 10
    aim["acquisition_ms"].extend([16.7] * 5)
    lucky.anticheat_stats["pellet_seeds"] = Counter({7: 20})
    lucky.anticheat_counts["shot_origin_drift:observed"] = 500  # but no minutes yet

    report = acr.analyze_player(server, lucky)

    assert report["reasons"] == [] and not report["flagged"]


def test_accuracy_needs_a_big_enough_human_population():
    server = _server()
    _population(server, 5)
    sharp = _player(server, 1, weapons={RIFLE: (600, 540, 50)})

    assert not acr.analyze_player(server, sharp)["flagged"]


def test_bots_are_never_flagged_nor_in_the_population():
    server = _server()
    # Many bots and a few humans: the population is too small (bots excluded).
    _population(server, 30, start=200, bot=True)
    _population(server, 3, start=100)
    human = _player(server, 1, weapons={RIFLE: (600, 540, 50)})
    bot = _player(server, 2, bot=True, weapons={RIFLE: (5000, 5000, 5000)},
                  kills=(500, 500))

    assert acr.analyze_player(server, bot) is None
    assert not acr.analyze_player(server, human)["flagged"]
    reports = acr.analyze_all(server)
    assert all(r["player_id"] != bot.id and r["player_id"] < 200 for r in reports)
    population = acr.population_accuracies(server)
    assert all(not getattr(owner, "is_bot", False) for owner, _ in population[RIFLE])


def test_departed_humans_join_the_accuracy_population():
    server = _server()
    _population(server, 25)
    acr.tick(server, now=0.0)
    for pid in [p for p in server.players if p >= 100]:
        del server.players[pid]
    acr.tick(server, now=1.0)  # archives the departed
    cheat = _player(server, 1, weapons={RIFLE: (600, 540, 50)})

    report = acr.analyze_player(server, cheat)
    assert f"accuracy:{RIFLE}" in _checks(report)


def test_pellet_seed_skew_and_sustained_violations():
    server = _server()
    player = _player(server, 1)
    seeds = Counter({s: 1 for s in range(10, 50)})
    seeds[3] = 60
    player.anticheat_stats["pellet_seeds"] = seeds
    player.anticheat_counts["aim_direction_mismatch:observed"] = 120
    acr.tick(server, now=0.0)

    report = acr.analyze_player(server, player, now=600.0)  # 10 minutes

    assert {"pellet_seed", "sustained:aim_direction_mismatch"} <= _checks(report)
    # A short burst (packet loss spike) over a long session is not sustained.
    player.anticheat_counts["aim_direction_mismatch:observed"] = 25
    later = acr.analyze_player(server, player, now=3600.0)
    assert "sustained:aim_direction_mismatch" not in _checks(later)


# --- observe_shot: snaps and acquisition time ----------------------------------------


def _aim_toward(src, dst):
    delta = [dst[i] - src[i] for i in range(3)]
    length = math.sqrt(sum(c * c for c in delta))
    return tuple(c / length for c in delta)


def _rotate_z(vector, degrees):
    r = math.radians(degrees)
    x, y, z = vector
    return (x * math.cos(r) - y * math.sin(r), x * math.sin(r) + y * math.cos(r), z)


def _shooter_with_history(server, history):
    shooter = _player(server, 1, team=1, eye=(100.0, 100.0, 30.0))
    shooter.orientation_at_loop = lambda loop: history.get(int(loop))
    return shooter


def _shoot(server, shooter, loop, direction, now):
    packet = SimpleNamespace(
        loop_count=loop, x=shooter.eye[0], y=shooter.eye[1], z=shooter.eye[2],
        ori_x=direction[0], ori_y=direction[1], ori_z=direction[2],
    )
    acr.observe_shot(server, shooter, packet, now=now)


def test_snap_onto_head_is_counted_and_human_tracking_is_not():
    server = _server()
    enemy = _player(server, 2, team=2, eye=(130.0, 100.0, 30.0))
    on_head = _aim_toward((100.0, 100.0, 30.0), enemy.eye)

    # Aimbot: 60 degrees off for a second, then on the head in one frame.
    history = {loop: _rotate_z(on_head, 60.0) for loop in range(0, 100)}
    history[100] = on_head
    shooter = _shooter_with_history(server, history)
    _shoot(server, shooter, 100, on_head, now=10.0)

    aim = acr.aim_stats(shooter)
    assert aim["engaged"] == 1 and aim["on_head"] == 1 and aim["snaps"] == 1
    assert list(aim["acquisition_ms"]) == [pytest.approx(1000.0 / 60.0)]

    # Human: sweeps 30 degrees over 20 frames (~333 ms), settles, then fires.
    server2 = _server()
    enemy2 = _player(server2, 2, team=2, eye=(130.0, 100.0, 30.0))
    target = _aim_toward((100.0, 100.0, 30.0), enemy2.eye)
    history2 = {loop: _rotate_z(target, 30.0) for loop in range(0, 80)}
    for step in range(20):
        history2[80 + step] = _rotate_z(target, 30.0 * (1 - (step + 1) / 20.0))
    for loop in range(100, 106):
        history2[loop] = target  # settles for a few frames, then fires
    human = _shooter_with_history(server2, history2)
    _shoot(server2, human, 105, target, now=10.0)
    aim2 = acr.aim_stats(human)
    assert aim2["on_head"] == 1 and aim2["snaps"] == 0
    assert aim2["acquisition_ms"][0] > 150.0


def test_repeated_snaps_flag_aim_snap_and_reaction():
    server = _server()
    enemy = _player(server, 2, team=2, eye=(130.0, 100.0, 30.0))
    on_head = _aim_toward((100.0, 100.0, 30.0), enemy.eye)
    history = {}
    shooter = _shooter_with_history(server, history)
    for engagement in range(25):
        base = engagement * 200
        for loop in range(base, base + 100):
            history[loop] = _rotate_z(on_head, 45.0)
        history[base + 100] = on_head
        _shoot(server, shooter, base + 100, on_head, now=engagement * 10.0)

    report = acr.analyze_player(server, shooter)
    assert {"aim_snap", "reaction"} <= _checks(report)
    assert report["flagged"]


def test_observe_shot_ignores_bots_melee_teammates_and_bad_input():
    server = _server()
    _player(server, 3, team=1, eye=(130.0, 100.0, 30.0))  # teammate only
    shooter = _shooter_with_history(server, {})
    _shoot(server, shooter, 5, (1.0, 0.0, 0.0), now=1.0)
    assert acr.aim_stats(shooter)["engaged"] == 0

    bot = _player(server, 4, bot=True)
    acr.observe_shot(server, bot, SimpleNamespace(loop_count=1), now=1.0)
    assert not hasattr(bot, "anticheat_aim")

    shooter.tool = 2  # spade
    _shoot(server, shooter, 6, (1.0, 0.0, 0.0), now=2.0)
    assert acr.aim_stats(shooter)["shots"] == 1  # only the rifle shot counted
    # Garbage never raises.
    acr.observe_shot(server, shooter, SimpleNamespace(loop_count="x", ori_x=float("nan")))


# --- periodic summary -------------------------------------------------------------


def test_tick_logs_one_line_per_flagged_player_and_writes_jsonl(tmp_path, caplog):
    server = _server(tmp_path)
    server.config.anticheat.summary_interval_seconds = 60.0
    _population(server, 25)
    _player(server, 1, weapons={RIFLE: (800, 280, 70)}, kills=(60, 18))
    cheat = _player(server, 2, weapons={RIFLE: (600, 540, 430)}, kills=(45, 41))

    with caplog.at_level(logging.WARNING, logger="anticheat"):
        assert acr.tick(server, now=0.0) == []
        assert acr.tick(server, now=30.0) == []
        flagged = acr.tick(server, now=61.0)

    assert [r["player_id"] for r in flagged] == [cheat.id]
    lines = [r.getMessage() for r in caplog.records if "suspect" in r.getMessage()]
    assert len(lines) == 1 and "name='P2'" in lines[0]
    records = [json.loads(line) for line in
               (tmp_path / "anticheat.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1 and records[0]["player_id"] == cheat.id
    assert records[0]["stats"]["weapons"][str(RIFLE)]["hits"] == 540
    assert cheat.connection.disconnected is None  # never punishes


def test_jsonl_is_rotated_when_it_grows(tmp_path):
    server = _server(tmp_path)
    server.config.anticheat.report_max_bytes = 200
    server.config.anticheat.report_backups = 2
    _population(server, 25)
    _player(server, 2, weapons={RIFLE: (600, 540, 430)}, kills=(45, 41))
    acr.tick(server, now=0.0)
    for index in range(1, 6):
        acr.tick(server, now=index * 61.0)

    path = tmp_path / "anticheat.jsonl"
    assert path.exists()
    assert (tmp_path / "anticheat.jsonl.1").exists()
    assert (tmp_path / "anticheat.jsonl.2").exists()
    assert not (tmp_path / "anticheat.jsonl.3").exists()


def test_tick_disabled_by_config(tmp_path):
    server = _server(tmp_path)
    server.config.anticheat.report_enabled = False
    _population(server, 25)
    _player(server, 2, weapons={RIFLE: (600, 540, 430)}, kills=(45, 41))
    for index in range(3):
        assert acr.tick(server, now=index * 100.0) == []
    assert not (tmp_path / "anticheat.jsonl").exists()


# --- admin commands ------------------------------------------------------------------


@pytest.fixture
def said(monkeypatch):
    lines = []

    async def fake_send(server, player, message):
        lines.append(message)

    monkeypatch.setattr(ac_cmds, "send_message", fake_send)
    return lines


def _ctx(server, player, *args):
    return CommandContext(server=server, player=player, args=list(args),
                          raw_args=" ".join(args))


def test_acreport_lists_top_suspects_and_details(said):
    server = _server()
    _population(server, 25)
    admin = _player(server, 1, weapons={RIFLE: (800, 280, 70)}, kills=(60, 18))
    admin.admin = True
    _player(server, 2, weapons={RIFLE: (600, 540, 430)}, kills=(45, 41))

    asyncio.run(ac_cmds.cmd_acreport(_ctx(server, admin)))
    assert said[0].startswith("AC top 1 of")
    assert said[1].startswith("!#2 P2 score")

    said.clear()
    asyncio.run(ac_cmds.cmd_acreport(_ctx(server, admin, "P2")))
    assert "FLAGGED" in said[0]
    assert any("headshot_kills" in line for line in said)

    said.clear()
    asyncio.run(ac_cmds.cmd_acreport(_ctx(server, admin, "nobody")))
    assert said == ["Player not found: nobody"]


def test_acstats_shows_raw_counters(said):
    server = _server()
    admin = _player(server, 1)
    target = _player(server, 2, weapons={RIFLE: (100, 40, 10)}, kills=(5, 2))
    target.anticheat_counts["shot_origin_drift:observed"] = 3
    target.input_queue_delay_stats = lambda: {"samples": 9, "p50": 1, "max": 4}

    asyncio.run(ac_cmds.cmd_acstats(_ctx(server, admin, "P2")))
    text = "\n".join(said)
    assert "tool 6: 100 shots 40 hits (40%) 10 hs" in text
    assert "shot_origin_drift:observed=3" in text
    assert "p50=1 max=4" in text


def test_ac_commands_are_admin_only(monkeypatch):
    server = _server()
    server.config.log_commands = False
    player = _player(server, 1)
    sent = []

    async def fake_send(server, player, message):
        sent.append(message)

    monkeypatch.setattr("commands.command_handler.send_message", fake_send)
    for command in ("acreport", "acstats P1"):
        asyncio.run(handle_command(server, player, command))
    assert sent == ["You don't have permission to use this command."] * 2
