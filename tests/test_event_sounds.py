"""Server-triggered retail sound / music cues (docs/SOUNDS_RETAIL.md)."""

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.modules.setdefault("toml", SimpleNamespace(load=lambda *a, **k: {}))

import shared.constants_audio as CA  # noqa: E402
from modes import airstrike  # noqa: E402
from server import audio  # noqa: E402
from server.game_constants import TEAM1, TEAM2, TEAM_SPECTATOR  # noqa: E402
from shared.bytes import ByteReader  # noqa: E402
from shared.packet import PlayMusic, PlaySound  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _sounds(packets):
    return [
        PlaySound(ByteReader(bytes(data)[1:]))
        for data in packets
        if data and bytes(data)[0] == PlaySound.id
    ]


def _sound_ids(packets):
    return [packet.sound_id for packet in _sounds(packets)]


def _music(packets):
    return [
        PlayMusic(ByteReader(bytes(data)[1:])).name
        for data in packets
        if data and bytes(data)[0] == PlayMusic.id
    ]


class _Conn:
    def __init__(self, in_game=True):
        self.in_game = in_game
        self.sent = []

    def send(self, data, reliable=True, **_kwargs):
        self.sent.append(bytes(data))


class _Server:
    def __init__(self):
        self.players = {}
        self.sent = []

    def broadcast(self, data, **_kwargs):
        self.sent.append(bytes(data))


def _p(server, pid, team, *, connection=True, in_game=True):
    conn = _Conn(in_game) if connection else None
    player = SimpleNamespace(id=pid, team=team, connection=conn)
    player.send = (conn.send if conn else (lambda *_a, **_k: None))
    server.players[pid] = player
    return player


# --- the id table -------------------------------------------------------------

def test_every_sound_constant_matches_the_client_sound_map():
    names = {
        "SNOWCAN_IMPACT": "SNOWCAN_IMPACT", "SNOWCAN_BUILD": "SNOWCAN_BUILD",
        "EVENT_POSITIVE": "EVENT_POSITIVE", "EVENT_NEGATIVE": "EVENT_NEGATIVE",
        "DIAMOND_APPEAR": "DIAMOND_APPEAR", "DIAMOND_DISAPPEAR": "DIAMOND_DISAPPEAR",
        "DIAMOND_DROPINBASE": "DIAMOND_DROPINBASE",
        "VIP_YOURS_IS_DEAD": "VIP_YOURSISDEAD", "VIP_KILLED_THEIRS": "VIP_KILLEDTHEIRS",
        "AIRSTRIKE_SIREN": "AIRSTRIKE_SIREN_ONESHOT",
        "AIRSTRIKE_FLYBY": "AIRSTRIKE_FLYBY", "AIRSTRIKE_FLYBY_SPACE": "AIRSTRIKE_FLYBY_SPACE",
        "CLASSIC_PICKUP": "CLASSIC_PICKUP", "CRATE": "CRATE",
        "HEALTHCRATE": "HEALTHCRATE", "CRATE_BLOCKS": "CRATE_BLOCKS",
        "BOMB_PICKUP": "BOMB_PICKUP", "BOMB_EXPLODE_WATER": "BOMB_EXPLODE_WATER",
        "BOMB_EXPLODE": "BOMB_EXPLODE", "DIAMOND_PICKUP": "DIAMOND_PICKUP",
        "FLAG_RETURNED": "FLAG_RETURNED", "BUILD_DYNAMITE": "BUILD_DYNAMITE",
        "BOMB_DROP": "BOMB_DROP", "DIAMOND_DROP": "DIAMOND_DROP",
        "CRATEDROP_FLYBY_POS_WW": "CRATEDROP_FLYBY_POS_WW",
        "CRATEDROP_FLYBY_POS": "CRATEDROP_FLYBY_POS",
        "CRATEDROP_FLYBY_SPACE_POS": "CRATEDROP_FLYBY_SPACE_POS",
        "TUTORIAL_COMPLETE": "TUTORIAL_COMPLETE", "ZOMBIE_BECOME": "ZOMBIE_BECOME",
        "ZOMBIE_TIMER": "ZOMBIE_TIMER_COUNTDOWN", "TURRET_PLACE": "TURRET_PLACE",
        "BUILD_LANDMINE": "BUILD_LANDMINE", "PREFAB_BUILD": "PREFABBUILD",
        "DIG_HIT_BLOCK": "DIG_HIT_BLOCK", "CROWBAR_HIT_BLOCK": "CROWBAR_HIT_BLOCK",
        "KNIFE_HIT_BLOCK": "KNIFE_HIT_BLOCK", "PICKAXE_HIT_BLOCK": "PICKAXE_HIT_BLOCK",
        "SUPER_SPADE_HIT_BLOCK": "SUPER_SPADE_HIT_BLOCK",
        "ZOMBIE_HAND_HIT_BLOCK": "ZOMBIE_HAND_HIT_BLOCK",
        "DIG_HIT_WATER": "DIG_HIT_WATER", "SUPER_SPADE_HIT_WATER": "SUPER_SPADE_HIT_WATER",
        "CROWBAR_HIT_WATER": "CROWBAR_HIT_WATER", "KNIFE_HIT_WATER": "KNIFE_HIT_WATER",
        "PICKAXE_HIT_WATER": "PICKAXE_HIT_WATER",
        "ZOMBIE_HAND_HIT_WATER": "ZOMBIE_HAND_HIT_WATER",
        "BUILD_UGC": "BUILD_UGC", "BUILD": "BUILD", "PAINT": "PAINT_PRIMARY",
    }
    for ours, client in names.items():
        assert getattr(audio, "SND_" + ours) == getattr(CA, client + "_SOUND_ID"), ours
        assert getattr(audio, "SND_" + ours) in CA.SOUND_MAP


def test_client_local_pickup_sounds_are_never_sent_by_modes():
    """player.pyd plays BOMB/DIAMOND_PICKUP itself on the pickup packet."""
    for path in list((ROOT / "modes").glob("*.py")) + [ROOT / "server" / "pickups.py"]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"SND_(BOMB|DIAMOND)_PICKUP\b", text), path.name


# --- audiences ----------------------------------------------------------------

def test_team_relative_cue_skips_spectators_loading_clients_and_bots():
    server = _Server()
    blue = _p(server, 1, TEAM1)
    green = _p(server, 2, TEAM2)
    spec = _p(server, 3, TEAM_SPECTATOR)
    loading = _p(server, 4, TEAM1, in_game=False)
    _p(server, 5, TEAM2, connection=False)  # bot

    audio.play_team_relative(server, TEAM1)

    assert _sound_ids(blue.connection.sent) == [audio.SND_EVENT_POSITIVE]
    assert _sound_ids(green.connection.sent) == [audio.SND_EVENT_NEGATIVE]
    assert spec.connection.sent == []
    assert loading.connection.sent == []
    assert _sounds(blue.connection.sent)[0].positioned is False


def test_personal_cue_is_not_sent_to_a_loading_client():
    server = _Server()
    loading = _p(server, 1, TEAM1, in_game=False)
    audio.play_sound_to(loading, audio.SND_CRATE)
    assert loading.connection.sent == []


def test_vip_death_cue_reaches_each_team_once_and_no_spectator():
    from tests.test_vip import _new_mode, _player

    server, mode = _new_mode()
    blue = _player(server, 1, TEAM1)
    green = _player(server, 2, TEAM2)
    spec = _player(server, 3, TEAM1)
    server.teams[TEAM1].remove_player(spec) if hasattr(
        server.teams[TEAM1], "remove_player") else None
    spec.team = TEAM_SPECTATOR
    asyncio.run(mode.on_tick(1))
    for p in (blue, green, spec):
        p.connection.sent.clear()

    asyncio.run(mode._kill_vip(TEAM1, mode.vips[TEAM1], None))

    assert _sound_ids(blue.connection.sent) == [audio.SND_VIP_YOURS_IS_DEAD]
    assert _sound_ids(green.connection.sent) == [audio.SND_VIP_KILLED_THEIRS]
    assert _sound_ids(spec.connection.sent) == []


# --- airstrike ----------------------------------------------------------------

def test_airstrike_sounds_siren_and_flyby_once_each():
    server = _Server()
    airstrike.trigger_airstrike(server, (100.0, 100.0, 60.0))
    assert _sound_ids(server.sent) == [audio.SND_AIRSTRIKE_SIREN, audio.SND_AIRSTRIKE_FLYBY]
    assert all(packet.positioned for packet in _sounds(server.sent))


def test_prewarned_airstrike_does_not_repeat_the_siren():
    server = _Server()
    airstrike.sound_airstrike_siren(server, (10.0, 20.0, 30.0), warn_ahead=True)
    airstrike.trigger_airstrike(server, (10.0, 20.0, 30.0))
    airstrike.trigger_airstrike(server, (10.0, 20.0, 30.0))
    assert _sound_ids(server.sent) == [
        audio.SND_AIRSTRIKE_SIREN,
        audio.SND_AIRSTRIKE_FLYBY,
        audio.SND_AIRSTRIKE_SIREN,
        audio.SND_AIRSTRIKE_FLYBY,
    ]


def test_lunar_maps_use_the_space_flyby():
    server = _Server()
    server.world_manager = SimpleNamespace(
        map_metadata=SimpleNamespace(skybox_name="LunarBase.txt")
    )
    airstrike.trigger_airstrike(server, (1.0, 2.0, 3.0))
    assert _sound_ids(server.sent)[-1] == audio.SND_AIRSTRIKE_FLYBY_SPACE


def test_bomb_explosion_picks_the_water_variant_below_the_water_plane():
    from server.game_constants import WATER_LEVEL

    dry, wet = audio.SND_BOMB_EXPLODE, audio.SND_BOMB_EXPLODE_WATER
    assert audio.explosion_sound((0, 0, WATER_LEVEL - 5), dry, wet) == dry
    assert audio.explosion_sound((0, 0, WATER_LEVEL + 1), dry, wet) == wet


# --- music --------------------------------------------------------------------

def test_last_man_music_does_not_restart_a_last_man_bed():
    server = _Server()
    audio.play_gameplay_music(server)
    before = len(server.sent)
    assert audio.play_last_man_music(server) is False
    assert len(server.sent) == before

    audio.play_timeout_music(server)
    assert audio.play_last_man_music(server) is True
    assert _music(server.sent)[-1].startswith("last_man_standing_")


# --- zombie -------------------------------------------------------------------

def test_zombie_pick_countdown_sound_ends_on_the_pick(monkeypatch):
    from modes.zombie import ZombieMode, ZombiePhase
    from tests.test_zombie import _player, _Server as _ZServer

    now = [1000.0]
    monkeypatch.setattr("modes.zombie.time.time", lambda: now[0])
    server = _ZServer()
    _player(server, 1)
    _player(server, 2)
    _player(server, 3)
    mode = ZombieMode(server)
    server.mode = mode
    mode.infection_delay = 60.0
    asyncio.run(mode.on_mode_start())
    assert mode.phase is ZombiePhase.COUNTDOWN
    assert audio.SND_ZOMBIE_TIMER not in _sound_ids(server.packets)

    now[0] += 51.0  # 9 s left
    asyncio.run(mode.on_tick(1))
    assert audio.SND_ZOMBIE_TIMER not in _sound_ids(server.packets)

    now[0] += 1.5  # 7.5 s left
    asyncio.run(mode.on_tick(2))
    asyncio.run(mode.on_tick(3))
    assert _sound_ids(server.packets).count(audio.SND_ZOMBIE_TIMER) == 1

    now[0] += 8.0
    asyncio.run(mode.on_tick(4))
    assert mode.phase is ZombiePhase.ACTIVE
    ids = _sound_ids(server.packets)
    assert ids.count(audio.SND_ZOMBIE_TIMER) == 1
    assert ids.count(audio.SND_ZOMBIE_BECOME) == 1


def test_zombie_survivor_round_win_plays_ending_track_then_bed(monkeypatch):
    from modes.zombie import SURVIVOR_TEAM, ZombieMode
    from tests.test_zombie import _player, _Server as _ZServer

    server = _ZServer()
    for pid in (1, 2, 3):
        _player(server, pid)
    mode = ZombieMode(server)
    server.mode = mode
    mode.score_limit = 3
    mode.round_intermission = 999.0
    asyncio.run(mode.on_mode_start())
    asyncio.run(mode.on_tick(1))
    server.packets.clear()

    async def win():
        await mode._finish_round(SURVIVOR_TEAM, "SURVIVOR_WIN")
        await mode._cancel_round_task()

    asyncio.run(win())
    assert _music(server.packets) and _music(server.packets)[-1].startswith("game_ending_")

    server.packets.clear()
    mode._restore_gameplay_music()
    assert _music(server.packets) and _music(server.packets)[-1].startswith(
        "last_man_standing_"
    )


# --- CTF ------------------------------------------------------------------------

def test_ctf_intel_cues():
    from modes.ctf import CTFMode
    from tests.test_ctf_end_and_departure import _player as ctf_player, _started

    server, mode = _started(CTFMode)
    carrier = ctf_player(server, 7, TEAM1, mode.intel_positions[TEAM2])
    blue = ctf_player(server, 8, TEAM1, (0.0, 0.0, 0.0))
    green = ctf_player(server, 9, TEAM2, (0.0, 0.0, 0.0))
    blue.connection = _Conn()
    green.connection = _Conn()
    server.packets.clear()

    asyncio.run(mode._pickup_intel(carrier, TEAM2))
    pickup = [s for s in _sounds(server.packets) if s.sound_id == audio.SND_CLASSIC_PICKUP]
    assert len(pickup) == 1 and pickup[0].positioned

    asyncio.run(mode._capture_intel(carrier, TEAM2))
    assert _sound_ids(blue.connection.sent) == [audio.SND_EVENT_POSITIVE]
    assert _sound_ids(green.connection.sent) == [audio.SND_EVENT_NEGATIVE]

    server.packets.clear()
    asyncio.run(mode._return_intel(TEAM1))
    assert _sound_ids(server.packets) == [audio.SND_FLAG_RETURNED]
