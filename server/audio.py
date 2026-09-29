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
    StopSound,
)

# --- SOUND_ID table (client shared/constants_audio SOUND_MAP) ---------------
# Every *_SOUND_ID below is referenced by NO client binary (headless-IDA /
# binary scan 2026-09-26, docs/SOUNDS_RETAIL.md): the client only resolves
# them through SOUND_MAP when PlaySound(23) arrives, so the retail server
# triggered all of them. The client plays BOMB_PICKUP_SOUND and
# DIAMOND_PICKUP_SOUND itself (player.pyd pickup handler, positioned at the
# carrier), so ids 16 and 19 must never be sent for a pickup.
SND_SNOWCAN_IMPACT = 0
SND_SNOWCAN_BUILD = 1
SND_EVENT_POSITIVE = 2       # generic "good thing" stinger (score/kill)
SND_EVENT_NEGATIVE = 3       # generic "bad thing" stinger (death/loss)
SND_DIAMOND_APPEAR = 4
SND_DIAMOND_DISAPPEAR = 5
SND_DIAMOND_DROPINBASE = 6
SND_VIP_YOURS_IS_DEAD = 7    # VIP_yoursisdead
SND_VIP_KILLED_THEIRS = 8    # VIP_killedtheirs
SND_AIRSTRIKE_SIREN = 9
SND_AIRSTRIKE_FLYBY = 10
SND_AIRSTRIKE_FLYBY_SPACE = 11
SND_CLASSIC_PICKUP = 12
SND_CRATE = 13               # ammo crate pickup
SND_HEALTHCRATE = 14         # health crate pickup
SND_CRATE_BLOCKS = 15        # block crate pickup
SND_BOMB_PICKUP = 16         # CLIENT-LOCAL on pickup: never send for a pickup
SND_BOMB_EXPLODE_WATER = 17
SND_BOMB_EXPLODE = 18
SND_DIAMOND_PICKUP = 19      # CLIENT-LOCAL on pickup: never send for a pickup
SND_FLAG_RETURNED = 20
SND_BUILD_DYNAMITE = 21
SND_BOMB_DROP = 22
SND_DIAMOND_DROP = 23
SND_CRATEDROP_FLYBY_POS_WW = 24
SND_CRATEDROP_FLYBY_POS = 25
SND_CRATEDROP_FLYBY_SPACE_POS = 26
SND_TUTORIAL_COMPLETE = 27
SND_ZOMBIE_BECOME = 28
SND_ZOMBIE_TIMER = 29        # zombie_timer_countdown.ogg, 8.0 s long
SND_TURRET_PLACE = 30
SND_BUILD_LANDMINE = 31
SND_PREFAB_BUILD = 32
SND_DIG_HIT_BLOCK = 33
SND_CROWBAR_HIT_BLOCK = 34
SND_KNIFE_HIT_BLOCK = 35
SND_PICKAXE_HIT_BLOCK = 36
SND_SUPER_SPADE_HIT_BLOCK = 37
SND_ZOMBIE_HAND_HIT_BLOCK = 38
SND_DIG_HIT_WATER = 39
SND_SUPER_SPADE_HIT_WATER = 40
SND_CROWBAR_HIT_WATER = 41
SND_KNIFE_HIT_WATER = 42
SND_PICKAXE_HIT_WATER = 43
SND_ZOMBIE_HAND_HIT_WATER = 44
SND_BUILD_UGC = 45
SND_BUILD = 46
SND_PAINT = 47

# zombie_timer_countdown.ogg runs 7.99 s and ends on the infection sting.
# Retail scheduled it through GAME_MODE_CALLBACK_ZOMBIE_PICK_SOUND, a
# separate callback ahead of GAME_MODE_CALLBACK_ZOMBIE_PICK, so it is started
# this long before the pick rather than when the 60 s clock arms.
ZOMBIE_TIMER_SOUND_LEAD = 8.0

# Skyboxes of the space maps: the airstrike uses its "_space" flyby there.
SPACE_SKYBOXES = frozenset(("LunarBase.txt", "User_Lunar.txt"))
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
               position=None, attenuation: float = 1.0, exclude=None,
               reliable: bool = True) -> None:
    """Broadcast a one-shot sound to every in-game client. With `position`
    it plays 3D-positioned (distance-attenuated); without, full-volume UI.

    ``exclude`` is used for sounds the acting retail client already predicts;
    remote observers still need the authoritative cue, while the actor must
    not hear a doubled sample.

    ``reliable=False`` sends a purely cosmetic cue (block hits, build
    clicks) as sequenced-unreliable ENet traffic so a burst of effects can
    never head-of-line stall reliable gameplay packets. Keep the default for
    stingers and any cue that carries game information.
    """
    data = _sound_packet(sound_id, volume, position, attenuation)
    kwargs = {}
    if exclude is not None:
        kwargs["exclude"] = exclude
    if not reliable:
        kwargs["reliable"] = False
    server.broadcast(data, **kwargs)


def _settled(player) -> bool:
    """True when ``player`` has a GameScene that can play a cue.

    A still-loading connection must not receive gameplay packets (the same
    rule ``server.broadcast`` applies); bots have no connection at all.
    """
    connection = getattr(player, "connection", None)
    if connection is None:
        return False
    return bool(getattr(connection, "in_game", True))


def play_sound_to(player, sound_id: int, *, volume: float = 1.0,
                  position=None, attenuation: float = 1.0,
                  reliable: bool = True) -> None:
    """Play a one-shot sound for a single player (personal stingers)."""
    connection = getattr(player, "connection", None)
    if connection is not None and not getattr(connection, "in_game", True):
        return
    data = _sound_packet(sound_id, volume, position, attenuation)
    if reliable:
        player.send(data)
    else:
        player.send(data, reliable=False)


def play_sound_to_team(server, team: int, sound_id: int, *,
                       exclude=None, volume: float = 1.0, position=None,
                       attenuation: float = 1.0) -> int:
    """Play one cue to every settled member of ``team``; returns the count.

    ``exclude`` skips one player (the actor who already got a personal cue).
    Spectators are only reached when ``team`` is the spectator team.
    """
    data = _sound_packet(sound_id, volume, position, attenuation)
    team = int(team)
    exclude_id = int(getattr(exclude, "id", -1)) if exclude is not None else -1
    sent = 0
    for player in list(getattr(server, "players", {}).values()):
        if int(getattr(player, "team", -99)) != team:
            continue
        if exclude_id >= 0 and int(getattr(player, "id", -2)) == exclude_id:
            continue
        if not _settled(player):
            continue
        player.connection.send(data, reliable=True)
        sent += 1
    return sent


def play_team_relative(server, good_team: int, *,
                       good: int = SND_EVENT_POSITIVE,
                       bad: int = SND_EVENT_NEGATIVE,
                       volume: float = 1.0) -> None:
    """Objective stinger pair: ``good_team`` hears ``good``, the other
    playable team hears ``bad``. Spectators get neither, since a
    team-relative cue has no meaning for a neutral observer."""
    from server.game_constants import TEAM1, TEAM2

    good_team = int(good_team)
    if good_team not in (TEAM1, TEAM2):
        return
    bad_team = TEAM2 if good_team == TEAM1 else TEAM1
    play_sound_to_team(server, good_team, good, volume=volume)
    play_sound_to_team(server, bad_team, bad, volume=volume)


def is_space_map(server) -> bool:
    world = getattr(server, "world_manager", None)
    metadata = getattr(world, "map_metadata", None)
    return getattr(metadata, "skybox_name", None) in SPACE_SKYBOXES


def airstrike_flyby_sound(server) -> int:
    """AIRSTRIKE_FLYBY, or its "_space" take on the lunar maps."""
    return SND_AIRSTRIKE_FLYBY_SPACE if is_space_map(server) else SND_AIRSTRIKE_FLYBY


def explosion_sound(position, dry: int, wet: int) -> int:
    """Pick the water variant when the blast sits at or below the water plane
    (the client does the same for its own *_EXPLODE / *_WATER_EXPLODE pairs)."""
    from server.game_constants import WATER_LEVEL

    try:
        z = float(position[2])
    except (TypeError, ValueError, IndexError):
        return dry
    return wet if z > float(WATER_LEVEL) else dry


def _stop_sound_bytes(loop_id: int) -> bytes:
    """Build the retail packet-25 loop teardown.

    ``loop_id`` is the byte-sized identity assigned by PlaySound(23) or
    PlayAmbientSound(24).  The native receiver looks the id up in its media
    manager and deliberately ignores a missing id, so callers may use this for
    idempotent cleanup without probing client state.
    """

    loop_id = int(loop_id)
    if not 0 <= loop_id <= 0xFF:
        raise ValueError("sound loop_id must fit in one byte")
    packet = StopSound()
    packet.loop_id = loop_id
    return bytes(packet.generate())


def stop_sound(server, loop_id: int) -> None:
    """Stop one server-owned looping sound on every connected client."""

    server.broadcast(_stop_sound_bytes(loop_id))


def stop_sound_to(player, loop_id: int) -> None:
    """Stop one server-owned looping sound for a single client."""

    player.send(_stop_sound_bytes(loop_id))


def _stop_music_bytes() -> bytes:
    return bytes(StopMusic().generate())


def _play_music_bytes(name: str, seconds_played: float = 0.0) -> bytes:
    pkt = PlayMusic()
    pkt.name = str(name)
    pkt.seconds_played = float(seconds_played)
    return bytes(pkt.generate())


def al_error_flush_bytes() -> bytes:
    """A silent, unpositioned one-shot that clears the client's AL error.

    Live 2026-09-26 (tracer console on the dev client): the stock
    ``audio.Sound`` opens every music/ambience stream with
    ``alureCreateStreamFromFile``, which refuses to run while an older
    OpenAL error is still pending ("Could not load sound: music\\...ogg
    Existing OpenAL error"). Errors are left behind whenever the client's
    128-source pool is exhausted (a burst of hit/build effects) and by its
    per-frame volume fades on the invalid sources that follow; the track
    then silently never starts. A buffered ``Sound.play`` calls
    ``alGetError()`` first, so one PlaySound at volume 0 immediately before
    the stream packets clears that state (measured: poisoned state, no
    flush -> load fails; poisoned state + this packet -> track loads).
    """
    return _sound_packet(SND_BUILD, volume=0.0)


def _switch_music(send, track: str) -> None:
    """StopMusic THEN PlayMusic on a ``send(bytes)`` sink. The Stop is required
    before replacing an existing gameplay track; normal connection and
    broadcast sinks already use reliable ordered delivery by default. A silent
    AL-error flush precedes them so a stale OpenAL error cannot block the
    stream (see ``al_error_flush_bytes``)."""
    send(al_error_flush_bytes())
    send(_stop_music_bytes())
    send(_play_music_bytes(track))


def _music_family(track) -> str | None:
    """Track family ("last_man_standing", "game_ending", ...) of a name."""
    if not track:
        return None
    base, _, suffix = str(track).rpartition("_")
    return base if base and suffix.isdigit() else str(track)


def _broadcast_track(server, track: str) -> None:
    _switch_music(server.broadcast, track)
    try:
        server.audio_music_family = _music_family(track)
    except AttributeError:
        pass


def play_music(server, name: str, seconds_played: float = 0.0) -> None:
    """Broadcast a StopMusic+PlayMusic(specific track) to every client."""
    _broadcast_track(server, str(name))


def stop_music(server) -> None:
    server.broadcast(_stop_music_bytes())
    try:
        server.audio_music_family = None
    except AttributeError:
        pass


def play_music_to(connection, track: str) -> None:
    """Start a specific track on ONE client (mid-round joiners)."""
    _switch_music(connection.send, track)


def mode_start_music_enabled(server) -> bool:
    """``[audio] mode_start_music`` (default on).

    The in-round bed is a deliberate deviation: retail rounds were silent
    (map ambience only) until the final 61 s, and ``last_man_standing_00N``
    was the Zombie last-survivor track only. Off = retail silence.
    """
    return bool(getattr(getattr(server, "config", None), "mode_start_music", True))


def gameplay_bed_track(server) -> str | None:
    """A random gameplay-bed track, or None when the bed is switched off."""
    if not mode_start_music_enabled(server):
        return None
    return random.choice(GAMEPLAY_TRACKS)


def play_gameplay_music(server) -> None:
    """Start the in-game music bed — a random specific gameplay track that
    loops for the round. Broadcast StopMusic+PlayMusic.

    With ``[audio] mode_start_music = false`` this only stops the previous
    round's music (the retail in-round silence).
    """
    track = gameplay_bed_track(server)
    if track is None:
        stop_music(server)
        return
    _broadcast_track(server, track)


LAST_MAN_TRACKS = GAMEPLAY_TRACKS


def play_last_man_music(server) -> bool:
    """INGAME_MUSIC_LAST_MAN: the Zombie last-survivor track. No client
    binary references INGAME_MUSIC, so only the server can start it.

    Returns False (and sends nothing) while a last-man track is already the
    broadcast music, e.g. the round bed, so the music never restarts.
    """
    family = _music_family(LAST_MAN_TRACKS[0])
    if getattr(server, "audio_music_family", None) == family:
        return False
    _broadcast_track(server, random.choice(LAST_MAN_TRACKS))
    return True


def play_ending_music(server) -> bool:
    """The victory / last-minute track — a random specific game_ending track.

    A timed round already started one at TIMEOUT_MUSIC_SECONDS; the tracks
    are authored to peak at 0:00, so restarting a different one on the win
    cut that ending off. Returns False (nothing sent) while a game_ending
    track is already the broadcast music.
    """
    if getattr(server, "audio_music_family", None) == _music_family(
        GAME_ENDING_TRACKS[0]
    ):
        return False
    _broadcast_track(server, random.choice(GAME_ENDING_TRACKS))
    return True


def play_timeout_music(server) -> None:
    """The last-minute tension track (a random specific game_ending track)."""
    _broadcast_track(server, random.choice(GAME_ENDING_TRACKS))


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
    # The ambience is a stream too: clear a stale OpenAL error first.
    player.send(al_error_flush_bytes())
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
