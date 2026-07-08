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

# Timeout music: played once when the round clock crosses this many seconds
# remaining (TIMEOUT_MUSIC_LENGTH from constants_gamemode — the tracks are
# authored to land their crescendo exactly at 0:00).
TIMEOUT_MUSIC_SECONDS = 61.0
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


def play_timeout_music(server) -> None:
    """The last-minute tension track — pick one of the four endings."""
    play_music(server, random.choice(GAME_ENDING_TRACKS))
