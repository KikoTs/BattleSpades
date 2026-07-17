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

from shared.packet import (
    CreateAmbientSound,
    PlayAmbientSound,
    PlayMusic,
    PlaySound,
    StopMusic,
)

# --- SOUND_ID table (extracted from the live client constants_audio) --------
SND_EVENT_POSITIVE = 2       # generic "good thing" stinger (score/kill)
SND_EVENT_NEGATIVE = 3       # generic "bad thing" stinger (death/loss)
SND_VIP_YOURS_IS_DEAD = 7    # VIP_yoursisdead
SND_VIP_KILLED_THEIRS = 8    # VIP_killedtheirs
SND_AIRSTRIKE_SIREN = 9
SND_CLASSIC_PICKUP = 12
SND_CRATE = 13               # ammo crate pickup
SND_HEALTHCRATE = 14         # health crate pickup
SND_CRATE_BLOCKS = 15        # block crate pickup
SND_FLAG_RETURNED = 20
SND_BUILD_DYNAMITE = 21
SND_TUTORIAL_COMPLETE = 27
SND_ZOMBIE_BECOME = 28
SND_ZOMBIE_TIMER = 29
SND_TURRET_PLACE = 30
SND_BUILD_LANDMINE = 31
SND_PREFAB_BUILD = 32
SND_DIG_HIT_BLOCK = 33
SND_CROWBAR_HIT_BLOCK = 34
SND_KNIFE_HIT_BLOCK = 35
SND_PICKAXE_HIT_BLOCK = 36
SND_SUPER_SPADE_HIT_BLOCK = 37
SND_ZOMBIE_HAND_HIT_BLOCK = 38
SND_BUILD = 46
SND_PAINT = 47
DEFAULT_AMBIENT = "amb_rural"

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
               position=None, attenuation: float = 1.0, exclude=None) -> None:
    """Broadcast a one-shot sound to every in-game client. With `position`
    it plays 3D-positioned (distance-attenuated); without, full-volume UI.

    ``exclude`` is used for sounds the acting retail client already predicts;
    remote observers still need the authoritative cue, while the actor must
    not hear a doubled sample.
    """
    data = _sound_packet(sound_id, volume, position, attenuation)
    if exclude is None:
        server.broadcast(data)
    else:
        server.broadcast(data, exclude=exclude)


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
    """StopMusic THEN PlayMusic on a ``send(bytes)`` sink. The Stop is required
    before replacing an existing gameplay track; normal connection and
    broadcast sinks already use reliable ordered delivery by default."""
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
# Native GameScene constructs AmbientSound(name, points), assigns loop_id, and
# appends it to scene.ambient_sounds. An EMPTY point list is the stock global
# bed. A non-empty list is an authored local emitter set (Mayan's river is the
# canonical example). Never synthesize a map-wide grid: the old z=40 grid sat
# more than HEARING_DISTANCE below normal terrain and also converted global
# beds into the wrong positioned-source behavior.


def _ambient_packet(name: str, loop_id: int, points: list) -> bytes:
    pkt = CreateAmbientSound()
    pkt.name = str(name)
    pkt.loop_id = int(loop_id)
    pkt.points = list(points)
    return bytes(pkt.generate())


def _play_ambient_packet(
    name: str,
    loop_id: int,
    *,
    volume: float,
    position=None,
    attenuation: float = 0.0,
) -> bytes:
    """Start the stream registered by CreateAmbientSound(22).

    CreateAmbientSound only constructs the native AmbientSound controller; it
    does not allocate an audio player. PlayAmbientSound(24) is therefore the
    required second half of the stock sequence.
    """

    pkt = PlayAmbientSound()
    pkt.name = str(name)
    pkt.looping = True
    pkt.positioned = position is not None
    pkt.volume = float(volume)
    pkt.time = 0.0
    pkt.loop_id = int(loop_id)
    if position is not None:
        pkt.x, pkt.y, pkt.z = (float(value) for value in position)
        pkt.attenuation = float(attenuation)
    else:
        pkt.x = pkt.y = pkt.z = 0.0
        pkt.attenuation = 0.0
    return bytes(pkt.generate())


def _ambient_start_position(player, points):
    """Return the safe bootstrap position for a local ambient controller.

    The native media manager refuses to allocate a positioned stream when its
    first position is outside hearing range.  AmbientSound.update can move an
    allocated loop to the nearest authored point, but it cannot recover a
    stream which failed to allocate.  Bootstrap local loops at the listener,
    then let the registered point controller place them on its next update.
    """

    if not points:
        return None
    return (
        float(getattr(player, "x", 0.0)),
        float(getattr(player, "y", 0.0)),
        float(getattr(player, "z", 0.0)),
    )


def send_map_ambient(server, player) -> None:
    """Register all validated map ambience definitions on one retail client.

    This runs after the client's world reveal. Loop IDs are scoped to that
    GameScene and deliberately start at one, matching the live-verified packet
    used by the previous single-bed implementation.
    """

    world = getattr(server, "world_manager", None)
    metadata = getattr(world, "map_metadata", None)
    definitions = list(getattr(metadata, "ambient_sounds", ()) or ())
    if not definitions:
        # Compatibility for focused tests/embedders without MapMetadata.
        from server.map_metadata import MapAmbientSound, default_ambient_sound
        definitions = [MapAmbientSound(default_ambient_sound(
            getattr(world, "map_name", ""),
            getattr(metadata, "skybox_name", None),
        ))]
    for loop_id, definition in enumerate(definitions[:255], start=1):
        points = list(definition.points)
        player.send(_ambient_packet(
            definition.name,
            loop_id,
            points,
        ))
        # Packet 22 registers the controller; packet 24 creates the streaming
        # GameSound and binds its loop id. Bootstrap a local stream at the
        # listener so MediaManager cannot distance-cull it before the native
        # AmbientSound controller moves it to the closest authored point.
        player.send(_play_ambient_packet(
            definition.name,
            loop_id,
            volume=definition.volume,
            position=_ambient_start_position(player, points),
            attenuation=definition.attenuation,
        ))
