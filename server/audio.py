"""Server-driven audio cues — the "funky, non-silent" gameplay feel.

The client plays weapon/footstep sounds itself; the SERVER drives event and
mode audio via:
  PlaySound(23)        — sound_id from the client's SOUND_ID table (0-47,
                         dumped live from constants_audio 2026-07-08).
  PlayAmbientSound(24) — name-based ambient loops.
  PlayMusic(26)        — music/<name>.ogg by base name.
  StopSound(25) / StopMusic(27).

Music catalog (client music/ dir): game_ending_001..004 (the ~61s timeout
tracks — TIMEOUT_MUSIC_LENGTH=61.0, played when the round clock crosses 61s),
last_man_standing_001..004 (zombie), mainmenu, secondary_menu_bed_001/002,
tutorial_music_001.
"""
from __future__ import annotations

import random

from shared.packet import PlaySound, PlayMusic, StopMusic, CreateAmbientSound

# --- SOUND_ID table (extracted from the live client constants_audio) --------
SND_EVENT_POSITIVE = 2       # generic "good thing" stinger (score/kill)
SND_EVENT_NEGATIVE = 3       # generic "bad thing" stinger (death/loss)
SND_AIRSTRIKE_SIREN = 9
SND_CLASSIC_PICKUP = 12
SND_CRATE = 13               # ammo crate pickup
SND_HEALTHCRATE = 14         # health crate pickup
SND_CRATE_BLOCKS = 15        # block crate pickup
SND_FLAG_RETURNED = 20
SND_BUILD_DYNAMITE = 21
SND_TUTORIAL_COMPLETE = 27
SND_ZOMBIE_TIMER = 29
SND_TURRET_PLACE = 30
SND_BUILD_LANDMINE = 31
SND_PREFAB_BUILD = 32

# Music track names. CRITICAL (reversed + live-verified 2026-07-08):
#  1. The wire PlayMusic.name must be a SPECIFIC track ("last_man_standing_003")
#     — the client only resolves the "..._001-004" range for its own internal
#     list-form names; a plain range string maps to a nonexistent .ogg and
#     silently fails to load.
#  2. process_packet_play_music does NOT override music that is already playing
#     (the leftover 'mainmenu' menu track blocks it). Send StopMusic(27) FIRST.
#  3. media.play_music uses loops=0 -> alure infinite loop, so ONE track loops
#     forever; no re-send/rotation needed within a round.
GAMEPLAY_TRACKS = ["last_man_standing_001", "last_man_standing_002",
                   "last_man_standing_003", "last_man_standing_004"]
GAME_ENDING_TRACKS = ["game_ending_001", "game_ending_002",
                      "game_ending_003", "game_ending_004"]
SECONDARY_TRACKS = ["secondary_menu_bed_001", "secondary_menu_bed_002"]

# Timeout music: swapped in when the round clock crosses this many seconds
# remaining (the game_ending tracks are ~61s, authored to crescendo at 0:00).
# TIME_AFTER_WIN_BEFORE_SCORES = 5.0 (constants_gamemode).
TIMEOUT_MUSIC_SECONDS = 61.0
TIME_AFTER_WIN_BEFORE_SCORES = 5.0


def _sound_packet(sound_id: int, volume: float = 1.0,
                  position=None, attenuation: float = 1.0) -> bytes:
    pkt = PlaySound()
    pkt.sound_id = int(sound_id)
    pkt.looping = False
    pkt.positioned = position is not None
    pkt.volume = float(volume)
    pkt.time = 0.0
    pkt.loop_id = 0
    if position is not None:
        pkt.x, pkt.y, pkt.z = (float(v) for v in position)
        pkt.attenuation = float(attenuation)
    else:
        pkt.x = pkt.y = pkt.z = 0.0
        pkt.attenuation = 0.0
    return bytes(pkt.generate())


def play_sound(server, sound_id: int, *, volume: float = 1.0,
               position=None, attenuation: float = 1.0) -> None:
    """Broadcast a one-shot sound to every in-game client. With `position`
    it plays 3D-positioned (distance-attenuated); without, full-volume UI."""
    server.broadcast(_sound_packet(sound_id, volume, position, attenuation))


def play_sound_to(player, sound_id: int, *, volume: float = 1.0,
                  position=None, attenuation: float = 1.0) -> None:
    """Play a one-shot sound for a single player (personal stingers)."""
    player.send(_sound_packet(sound_id, volume, position, attenuation))


def _stop_music_bytes() -> bytes:
    return bytes(StopMusic().generate())


def _play_music_bytes(name: str, seconds_played: float = 0.0) -> bytes:
    pkt = PlayMusic()
    pkt.name = str(name)
    pkt.seconds_played = float(seconds_played)
    return bytes(pkt.generate())


def _switch_music(send, track: str) -> None:
    """StopMusic THEN PlayMusic on a `send(bytes)` sink. The Stop is required —
    process_packet_play_music refuses to override music that's already playing
    (e.g. the leftover 'mainmenu' track). The track then alure-loops forever."""
    send(_stop_music_bytes())
    send(_play_music_bytes(track))


def play_music(server, name: str, seconds_played: float = 0.0) -> None:
    """Broadcast a StopMusic+PlayMusic(specific track) to every client."""
    _switch_music(server.broadcast, str(name))


def stop_music(server) -> None:
    server.broadcast(_stop_music_bytes())


def play_music_to(connection, track: str) -> None:
    """Start a specific track on ONE client (mid-round joiners)."""
    _switch_music(connection.send, track)


def play_gameplay_music(server) -> None:
    """Start the in-game music bed — a random specific gameplay track that
    loops for the round. Broadcast StopMusic+PlayMusic."""
    _switch_music(server.broadcast, random.choice(GAMEPLAY_TRACKS))


def play_ending_music(server) -> None:
    """The victory / last-minute track — a random specific game_ending track."""
    _switch_music(server.broadcast, random.choice(GAME_ENDING_TRACKS))


def play_timeout_music(server) -> None:
    """The last-minute tension track (a random specific game_ending track)."""
    _switch_music(server.broadcast, random.choice(GAME_ENDING_TRACKS))


# --- World ambience (CreateAmbientSound 22) ---------------------------------
# The client streams ambients/<name>.ogg as a looping positional source and
# plays it near the registered points. LIVE-VERIFIED: process_packet_create_
# ambient_sound registers it (scene.ambient_sounds grows, name set).
#
# Map display/file name -> ambient track (files in the client ambients/ dir).
MAP_AMBIENT = {
    "ArcticBase": "amb_arctic",
    "CastleWars": "amb_castlewars",
    "CityOfChicago": "amb_oldchicago",
    "20thCenturyTown": "amb_city",
    "London": "amb_city",
    "Alcatraz": "amb_alcatraz",
    "Invasion": "amb_invasion",
    "MayanJungle": "amb_jungle",
    "LunarBase": "amb_moon",
}
DEFAULT_AMBIENT = "amb_rural"   # generic outdoor bed for unmapped maps

# The ambient is heard within HEARING_DISTANCE (~50) of a point, so a grid of
# points across the 512x512 map keeps it audible everywhere.
_AMBIENT_GRID_STEP = 64
_AMBIENT_GRID_Z = 40


def _ambient_for_map(server) -> str:
    wm = getattr(server, "world_manager", None)
    name = getattr(wm, "map_name", "") if wm is not None else ""
    return MAP_AMBIENT.get(name, DEFAULT_AMBIENT)


def _ambient_grid_points(server) -> list:
    wm = getattr(server, "world_manager", None)
    sx = int(getattr(wm, "map_size_x", 512)) if wm is not None else 512
    sy = int(getattr(wm, "map_size_y", 512)) if wm is not None else 512
    pts = []
    half = _AMBIENT_GRID_STEP // 2
    for x in range(half, sx, _AMBIENT_GRID_STEP):
        for y in range(half, sy, _AMBIENT_GRID_STEP):
            pts.append((x, y, _AMBIENT_GRID_Z))
    return pts


def _ambient_packet(name: str, loop_id: int, points: list) -> bytes:
    pkt = CreateAmbientSound()
    pkt.name = str(name)
    pkt.loop_id = int(loop_id)
    pkt.points = list(points)
    return bytes(pkt.generate())


def send_map_ambient(server, player) -> None:
    """Register the map's ambient bed on one player's client so the world is
    never silent. A grid of points keeps it audible across the whole map."""
    name = _ambient_for_map(server)
    points = _ambient_grid_points(server)
    if not points:
        points = [(256, 256, _AMBIENT_GRID_Z)]
    player.send(_ambient_packet(name, 1, points))
