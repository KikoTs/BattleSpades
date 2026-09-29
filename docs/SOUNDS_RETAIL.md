# Server-triggered sounds and music (2026-09-26)

What the server sends over PlaySound(23) / PlayMusic(26) / StopMusic(27) for
game events, how the retail split between client-local and server-sent cues
was established, and what is still missing. Map ambience (22/24) is covered in
docs/PROTOCOL.md.

## Evidence

- The PlaySound ids are the client's `SOUND_MAP` keys
  (`shared/constants_audio.py`, `*_SOUND_ID = range(61)`); `server/audio.py`
  now carries all 48 named ids and `tests/test_event_sounds.py` checks each
  against the client table.
- Binary scan of the stock client (`aoslib/**/*.pyd`, `aoslib/**/*.py`,
  `aos.pkg`), for the plain and obfuscated (`A2xxx`) name of every sound
  constant: no `*_SOUND_ID` is referenced anywhere. The client resolves those
  ids only when PlaySound arrives, so every one of them was a server cue.
- Client-local sounds that must not be duplicated:
  - `BOMB_PICKUP_SOUND` and `DIAMOND_PICKUP_SOUND`: headless IDA on
    `aoslib.scenes.main.player.pyd`, one function (0x1000F150, the pickup
    setter that reads `PICKUPS`, `pickup_id`, `burdened`, `set_tool`) plays
    them positioned at the carrier for `BOMB_PICKUP` / `DIAMOND_PICKUP`. It
    has no sound for `INTEL_PICKUP`. Ids 16 and 19 are therefore never sent
    for a pickup (a test enforces this).
  - `AIRSTRIKE_EXPLODE_SOUND` and `CRATEDROP_LAND_SOUND`: gameScene.pyd.
  - Weapon, tool, footstep, build-error and flare sounds: the weapon/tool
    modules.
- Music: no client binary references `INGAME_MUSIC` (last man, ending,
  tutorial), so those tracks were server-started. `constants_gamemode` lists
  the retail server's callbacks, including `GAME_MODE_CALLBACK_TIMER_MUSIC`,
  `_TIMEOUT_MUSIC`, `_ZOMBIE_PICK_SOUND` (separate from `_ZOMBIE_PICK`) and
  `_SURVIVOR_WIN_MUSIC`.
- Clip lengths (Ogg granule / rate): `zombie_timer_countdown` 7.99 s,
  `zombie_become` 3.25 s, `airstrike_siren_oneshot` 11.15 s,
  `airstrike_flyby` 6.5 s, `game_ending_00x` 62-71 s,
  `last_man_standing_00x` 169-196 s.

Which event used which id, and for which audience, is not recorded anywhere:
there is no retail server binary or capture. The mapping below comes from the
sound names and the paired announcement audiences. It is inferred, and each
row says so where it matters.

## Cue table

2D = unpositioned UI sound; 3D = positioned at the event.

| Event | Cue | Audience | Status |
|---|---|---|---|
| CTF / Classic CTF intel pickup | 12 `classic_pickup`, 3D at carrier | everyone | new (the one pickup the client does not sound) |
| Intel capture | 2 `event_positive` / 3 `event_negative`, 2D | capturing team / intel owners | new |
| Intel returned (touch or 60 s timer) | 20 `flag_returned`, 2D | everyone | new |
| Intel drop | none (no drop id exists) | - | - |
| VIP killed | 7 `VIP_yoursisdead` / 8 `VIP_killedtheirs`, 2D | bereaved team / other team; spectators nothing | fixed: spectators used to get 8 |
| VIP chosen | none (the boss HUD shows `VIP_YOU_ARE_VIP` itself) | - | - |
| Occupation bomb pickup | 16, client-local | - | never sent |
| Occupation bomb drop | 22 `bomb_drop`, 3D | everyone | new |
| Occupation bomb detonation | 18 `bomb_explode` (17 below the water plane), 3D | everyone | new; no client code plays it |
| Diamond uncovered | 4 `diamond_appear`, 2D | everyone (with `DIAMOND_UNCOVERED`) | new |
| Diamond pickup | 19, client-local | - | never sent |
| Diamond dropped | 23 `diamond_drop`, 3D | everyone | new |
| Ground diamond expires | 5 `diamond_disappear`, 3D | everyone | new |
| Diamond cashed in | 6 `diamond_dropinbase` / 3 `event_negative`, 2D | cashing team / other team | new |
| Territory captured | 2 / 3, 2D | capturing team / losing team | new |
| Hill claimed (Multi-Hill) | 2 / 3, 2D | claiming team / other team | new |
| Demolition base destroyed | 9 siren, 3D on the base (attenuation 0.25) | everyone | moved: it now sounds when the base falls, `DEM_TIME_TO_WAIT_FOR_AIRSTRIKE` (5 s) before the shells, not with them |
| Objective airstrike launch (Demolition, Multi-Hill expiry) | 10 `airstrike_flyby`, 11 on LunarBase/User_Lunar skyboxes, 3D | everyone | new; impacts stay client-local |
| Zombie pick countdown | 29 `zombie_timer_countdown`, 2D | everyone | fixed: now 8 s before the pick so it ends on it; it used to play when the 60 s clock armed |
| Zombie outbreak | 28 `zombie_become`, 2D | everyone, once per outbreak | unchanged |
| Zombie last survivor | music `last_man_standing_00N` | everyone | new; skipped while the bed is already a last-man track |
| Zombie survivors win a non-final round | music `game_ending_00N` | everyone | new; the next round restores the bed |
| Final 61 s of a timed round | music `game_ending_00N` | everyone; mid-minute joiners get it on reveal | unchanged |
| Round / match end | music `game_ending_00N` | everyone | only when no game_ending track is already playing (a timed end keeps the final-minute track, which peaks at 0:00) |
| Map-rollover stats screen | client-local `secondary_menu_bed_00N` | - | the stock ViewGameStats menu starts it itself once packet 53 can open that menu (see below) |
| Tutorial complete / tutorial music | 27 / `tutorial_music_001` | the tutorial player | unchanged |
| Crate pickups | 13 / 14 / 15 | the picker | unchanged |
| TDM kill | 2 to the killer / 3 to the victim (volume 0.6) | two players | unchanged, inferred |
| Prefab build, tool block hits, build | 32, 33-38, 46 | observers | unchanged (combat/prefab owners) |

Every team or personal cue goes only to settled GameScenes (`in_game`);
bots and loading clients get nothing. Team-relative pairs
(`audio.play_team_relative`) never reach spectators. Global cues do.


## Music that silently never started (2026-09-26)

Kiril noticed no music in the final minute or on the end screen. The
server was sending the tracks; the stock client refused them.

- `aoslib.audio.Sound` opens music and ambience with
  `alureCreateStreamFromFile`, which refuses to run while any older OpenAL
  error is still pending: the client log shows `Could not load sound:
  music\game_ending_004.ogg Existing OpenAL error` for the timeout track,
  `mainmenu.ogg` at every rollover, and after one join burst
  `ambients/amb_city.ogg` plus 191 `Error starting source` lines.
- The client has 128 OpenAL sources (probed with `alGenSources` until
  `AL_INVALID_VALUE`). The join reveal replays terrain as one burst
  (1,550 Damage, 336 builds in one measured join), which exhausts them;
  sounds created on a failed source then leave `AL_INVALID_NAME` behind
  (`alSourcef` from the media manager's fades, caught by wrapping the
  ctypes calls in the tracer console).
- A buffered `Sound.play` calls `alGetError()` before playing. Measured in
  the console: with an error pending, a music load fails; the same state
  plus one silent `media.play(...)` first, the track loads.

Server changes:

- Every music switch (`_switch_music`) and every ambience registration
  (`send_map_ambient`) is preceded by `PlaySound(BUILD, volume 0)`, which
  clears the pending error (`audio.al_error_flush_bytes`). The same flush
  goes out right before ShowGameStats(53) (its menu starts music) and
  before MapEnded(52) (the loader swaps music).
- The world reveal starts the joiner's ambience and music **before** the
  terrain/roster catch-up (`BattleSpadesServer._send_join_audio`), once per
  scene epoch. The mode chooses the track (`BaseMode.join_music_track`:
  the game_ending track in the final minute or on the end screen).
- `play_ending_music` no longer restarts a different game_ending track
  over the one the final minute started.

The stats-screen music was also missing because ShowGameStats(53) never
opened ViewGameStats: see docs/ANNOUNCEMENTS_RETAIL_2026-09-24.md,
"Correction 2026-09-26".

## Rollover freeze (2026-09-26)

The same pending-error state froze the client after a map change. The
stock `Sound.close()` calls `alureDestroyStream` without stopping the
stream first, and that call is also refused while an error is pending. The
stream then stays in ALURE's 50 ms async-play list on a deleted source, and
the next `alurePlaySource` never returns. After a rollover the loader
retires the in-game music, nothing else plays to clear the error, and the
first sound after START (SelectTeam's `mu_start_game`) hangs: main-thread
stack `loadingMenu.start_pressed -> menuScene.set_menu ->
selectTeam.on_start -> media.play -> audio.play`. Reproduced twice by
pressing START on the loader, and on demand in the console by destroying a
playing stream with an error pending and then playing any buffered sound.

The server flushes before 52 narrow the window. The real fix is
client-side: `client_patches/session_transition_patch.py` now clears the
pending error before each ALURE stream call
(`install_audio_guard`); with it the same console experiment destroys the
stream cleanly and the next sound plays.

## Not sent yet (outside the mode files)

- 21 `dynamite_place`, 30 `turret_place`, 31 `landmine_place`: no client
  code plays a placement sound, so today nobody hears a dynamite, turret or
  landmine being placed. Belongs in the deployable placement handlers.
- 39-44 `hitwater_*`: tool hits on water. Belongs with the block-hit cue
  in `combat_runtime`.
- 0/1 `snowcan_*`, 45 `ugc_place`, 47 `ugc_colour_singleblock`: snowblower
  and Map Creator tools.
- 24-26 `cratedrop_flyby_*`: there is no supply-drop system.

## Deviation kept on purpose

The server starts a random `last_man_standing` track as a looping bed at
every round start and for every joiner. Retail had no in-round bed: the
`INGAME_MUSIC` table only has last-man, ending and tutorial music. The bed
was added earlier at Kiril's request for non-silent rounds. It is left in
place here, and it is why the last-man cue is usually a no-op.

Since 2026-09-27 it is a switch: `[audio] mode_start_music` (default
`true`, the deviation). `false` gives the retail silence: round starts only
send StopMusic (ending the previous round's track), joiners get no bed, and
the final-minute `game_ending`, victory, Zombie last-man and Tutorial
tracks still play (`server/audio.py` `gameplay_bed_track`).

Tests: `tests/test_event_sounds.py`.
