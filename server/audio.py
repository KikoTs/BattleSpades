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

from shared.packet import PlaySound, PlayMusic, StopMusic

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

# Music track names. The client's INGAME_MUSIC table (constants_audio.py:11)
# uses "<base>_001-004" RANGE strings — the client resolves the range and
# picks a random variant. LIVE-VERIFIED: the client accepts both the range
# form and a specific "..._001" via process_packet_play_music.
GAMEPLAY_MUSIC = "last_man_standing_001-004"   # the in-game combat bed
ENDING_MUSIC = "game_ending_001-004"           # last-minute + victory sting
SECONDARY_TRACKS = ["secondary_menu_bed_001", "secondary_menu_bed_002"]

# Timeout music: played once when the round clock crosses this many seconds
# remaining (the game_ending tracks are ~61s and authored to crescendo at
# 0:00). TIME_AFTER_WIN_BEFORE_SCORES = 5.0 (constants_gamemode).
TIMEOUT_MUSIC_SECONDS = 61.0
TIME_AFTER_WIN_BEFORE_SCORES = 5.0
# Re-send the gameplay bed on this cadence so a ~5-min track never leaves the
# round silent; the client crossfades (DEFAULT_MUSIC_FADE_TIME=6.5) each time,
# and the range string re-rolls the variant — "always random funky".
MUSIC_ROTATE_INTERVAL = 130.0
GAME_ENDING_TRACKS = ["game_ending_001", "game_ending_002",
                      "game_ending_003", "game_ending_004"]


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


def play_music(server, name: str, seconds_played: float = 0.0) -> None:
    """Start a music track (music/<name>.ogg) on every in-game client."""
    pkt = PlayMusic()
    pkt.name = str(name)
    pkt.seconds_played = float(seconds_played)
    server.broadcast(bytes(pkt.generate()))


def stop_music(server) -> None:
    pkt = StopMusic()
    server.broadcast(bytes(pkt.generate()))


def play_gameplay_music(server) -> None:
    """Start the in-game music bed (client re-rolls the variant each call)."""
    play_music(server, GAMEPLAY_MUSIC)


def play_ending_music(server) -> None:
    """The victory / last-minute tension track (game_ending range)."""
    play_music(server, ENDING_MUSIC)


def play_timeout_music(server) -> None:
    """The last-minute tension track — the game_ending range string."""
    play_music(server, ENDING_MUSIC)
