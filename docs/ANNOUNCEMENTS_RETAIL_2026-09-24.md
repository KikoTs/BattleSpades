# Retail announcement parity (2026-09-24)

Goal from Kiril's polish brief: a proper announcement system that is 100%
retail, using only packets in the dumped list. This records what the original
client expects, how that was established, and what the server now sends.

## Evidence method

The client's string table (`aoslib/strings/english.py` and the other
languages) holds every announcement template. Which ones the *server* had to
send was settled by searching the client binaries for each id:

```
grep -c -a <ID> aoslib/scenes/main/gameScene.pyd aoslib.hud.hud.pyd
              aoslib.gamemanager.pyd aoslib.scenes.main.player.pyd aos.pkg
```

- An id referenced by a client binary can be rendered locally, but only on
  the trigger that code path has. Check that trigger (IDA xrefs) before
  deciding the server must not also send it: a reference alone proves a
  client path exists, not that it fires for every event.
- An id referenced by no client binary can only reach the screen through
  `LocalisedMessage(50)` with that `string_id`, so the retail server sent it.

Client-local ids (hud.pyd): `NEVER_RESPAWN`, `RESPAWNING_IN` (respawn timer),
`VIP_YOU_ARE_VIP` (boss class change), `ZOMBIE_OUTBREAK_CLASS_SELECT`. The
server does not send these.

Round-result ids `TEAM_DEFEAT`, `GAME_DRAWN`, `ZOMBIE_WIN`, `SURVIVOR_WIN`,
`BASE_DESTROYED`, `END_OF_MAP` are the headline table of packet 73 only.
Re-checked 2026-09-25 with headless IDA on the stock Steam
`aoslib.hud.hud.pyd` (copy in the scratchpad): the Cython string-table
globals for `TEAM_DEFEAT` (0x101123A4), `GAME_DRAWN` (0x10111BC0),
`ZOMBIE_WIN` (0x10112140), `SURVIVOR_WIN` (0x10111FE8), `BASE_DESTROYED`
(0x101118E0) and `END_OF_MAP`
(0x101121FC) are read by exactly two functions, 0x10061280 and 0x1006ED20.
Their only callers are the wrappers 0x100E2E00 / 0x100E5560, whose
PyMethodDef `ml_name` is `set_message` (0x100F6ABC), i.e. the method
`show_text_message` (packet 73) calls while ViewGameStats is open. No other
client code reads these ids. So on the same-map in-place restart (no packet
53, therefore no ViewGameStats) the client never shows a result by itself.

Decision: the server keeps sending `TEAM_DEFEAT` / `GAME_DRAWN` (and the
Zombie `ZOMBIE_WIN` / `SURVIVOR_WIN`, VIP `TEAM_DEFEAT` round lines) as
`LocalisedMessage(50)` at the moment of the win. It is the only way the
result reaches the screen on an in-place restart, and on a map rollover it
precedes the packet-73 headline by the five-second score delay rather than
duplicating an on-screen line. What the retail server itself sent over
packet 50 for these ids is not recorded; this is a presentation choice
within the dumped packet list, not recovered behaviour.

Server-sent ids (absent from every binary): all mode start cues, the
CTF/VIP/Zombie/Diamond/Occupation/Territory/Multi-Hill event lines,
`LAST_MAN_STANDING`, `ZOMBIE_LAST_MAN`, `YOU_HAVE_BEEN_INFECTED`,
`ZOMBIE_VIRUS_RELEASED`, `ZOMBIE_INFECTION_DETECTED`, `PLAYER_JOINED`,
`MAP_VOTED_MESSAGE`, `ONE_MINUTE_LEFT`, `COUNTDOWN_SECONDS`,
`COUNTDOWN_MINUTES`.

Packet 50 resolves `{0}`/`{1}` and, with `localise_parameters`, treats each
parameter as another string id (team ids such as `TEAM1_COLOR` localise;
player names pass through unchanged). Free-form `ChatMessage(49)` text is
English only and was what most modes used before this pass.

## Packets 72 and 73 (ForceShowScores, ShowTextMessage)

IDA on the stock `gameScene.pyd`:

- `process_packet_force_show_scores` (body 0x101A0300) runs
  `self.force_show_scores(packet.forced)`. With forced=1 the client opens the
  ViewScores menu (`manager.set_menu`), stops movement and locks the menu to
  the scene; forced=0 (`clear_force_show_scores`) hands control back.
- `process_packet_show_text_message` (body 0x101A0490) runs
  `self.show_text_message(packet.message_id, packet.duration)`, which only
  calls `set_message` when the active menu is ViewGameStats. The nine ids
  (`NEXT_MAP_MESSAGE`..`TEAM_SCORES_DRAW`, shared/constants.py) pick the
  HUD-local strings listed above. It is the headline of the terminal
  statistics screen, not a free-text overlay.

Live check 2026-09-24 with the tracer dev client on a 50 s TDM round
(`scripts/auto_join.py` + a console poller): at the win the client moved to
its scores scene (`aoslib.scenes.frontend.menuScene.MenuScene` host, still
connected, HUD objects retained) and the in-place restart's forced=0 put it
straight back into the same GameScene; two consecutive rounds, no crash.
With the headline alone nothing visible changes on a same-map restart, as
the decompilation predicts. A map rollover (Atlantis to ArcticBase, vote
resolved) then sent 72(1) -> 53 -> 73 -> 52 in that order; the client went to
its scores scene, received InitialInfo and the new map and rebuilt the
ArcticBase skydome, i.e. the headline between 53 and 52 does not disturb
the reload.

What the server does now:

- Every round end: `ForceShowScores(1)` with the victory music, released with
  `ForceShowScores(0)` when the in-place restart begins
  (`lobby.end_round_scoreboard`, default true).
- Map rollover: `ShowTextMessage(mode id, dwell)` right after
  `ShowGameStats(53)` so the statistics screen carries the retail headline
  (`lobby.end_round_headline`, default true). Mode ids: Zombie
  `ZOMBIE_WIN_MESSAGE`/`SURVIVOR_WIN_MESSAGE`, VIP `VIP_TEAM1/2_WIN_MESSAGE`,
  Occupation `OCCUPATION_WIN_MESSAGE`, Demolition `DEMOLITION_END_MESSAGE`,
  everything else `TEAM_SCORES_MESSAGE` / `TEAM_SCORES_DRAW`.
- The packet-50 `TEAM_DEFEAT`/`GAME_DRAWN` top line stays as before.

One client access violation happened during the first live run 15 s after
the join, before any round-end packet (crash dump 17:08:54, EIP in ntdll,
Python frames only, NULL call). It did not reproduce in the following three
runs and is recorded here as unexplained.


## Correction 2026-09-26: packet 53 under the forced scoreboard

The 2026-09-24 rollover check only read the client's scene class. Screenshots
of the framebuffer (tracer console, `pyglet.image.get_buffer_manager()`)
show that with ForceShowScores(1) active the client stays on the plain
ViewScores scoreboard: `force_show_scores(True)` sets
`manager.locked_to_scene = ViewScores`, and a following
`show_game_statistics(False)` (packet 53) sets `game_statistics_active` but
cannot switch the menu. The GameStats screen, the packet-73 headline and
the stats menu's own music therefore never appeared ("I didn't see the end
results"). Releasing the hold first works: `clear_force_show_scores()`
followed by `show_game_statistics(False)` opens ViewGameStats.

The rollover now sends, after the 5 s score delay:
`PlaySound flush -> ForceShowScores(0) -> ShowGameStats(53) ->
ShowTextMessage(73)`, holds for `end_screen_seconds`, then
`PlaySound flush -> MapEnded(52)`. Same-map restarts are unchanged (72(1)
at the win, 72(0) on restart, no 53). Test:
`tests/test_retail_end_screen.py::test_rollover_releases_the_forced_scoreboard_before_show_game_stats`.

## What the server sends now

| Event | Before | Now (packet 50 unless noted) |
|---|---|---|
| Player finished joining | nothing | `PLAYER_JOINED` (name, team id) to everyone already in game |
| Round clock | nothing | `COUNTDOWN_MINUTES` 5/2, `ONE_MINUTE_LEFT`, `COUNTDOWN_SECONDS` 30/10, each once inside a 5 s window |
| Time-limit draw | free text | `GAME_DRAWN` |
| TDM | "X leads by N" every 60 s (invented) | removed; `TEAM_DEATHMATCH_START` on reveal |
| Zombie countdown | "Zombie outbreak in N seconds!" | `ZOMBIE_VIRUS_RELEASED` |
| Zombie outbreak | "X is Patient Zero!" | infected: `YOU_HAVE_BEEN_INFECTED`; survivors: `ZOMBIE_INFECTION_DETECTED` |
| Zombie infection later | nothing | `YOU_HAVE_BEEN_INFECTED` to the infected player |
| Zombie last human | "X is the last survivor!" | all: `LAST_MAN_STANDING`; the human: `ZOMBIE_LAST_MAN` |
| Zombie round end | free text | `SURVIVOR_WIN` / `ZOMBIE_WIN` |
| VIP selection | "Choosing VIPs..." | `VIP_AWAITING_CHOICE` |
| VIP chosen | "X is the Blue VIP!" | teammates: `VIP_NAME_IS_VIP` (name); the boss's HUD shows `VIP_YOU_ARE_VIP` itself |
| VIP round live | "Protect the VIP!" | `VIP_START` |
| VIP killed | "Blue VIP has been killed! No more respawns!" | bereaved team: `VIP_KILLED_VIP_YOURTEAM`; other: `VIP_KILLED_VIP_OPPOSITION` |
| VIP elimination | nothing | `VIP_LAST_MAN_STANDING` (team), `VIP_TEAM_WIPED_OUT` (team) |
| VIP round end | free text | `TEAM_DEFEAT` (team) / `GAME_DRAWN`; match end keeps base-mode `TEAM_DEFEAT` once |
| CTF pickup | "X has the Blue intel!" | carrier: `CTF_YOU_HAVE_FLAG`; carrier's team: `CTF_TEAM_HAS_FLAG`; owners: `CTF_ENEMY_HAS_FLAG` |
| CTF capture | "X captured the Blue intel!" | `CTF_TEAM_SCORE` / `CTF_ENEMY_SCORE` per team |
| CTF drop | free text | nothing (retail has no drop string) |
| CTF return by touch | "The Blue intel returned to base!" | `CTF_FLAG_RETURNED` (name, team id); timer returns stay silent |
| Diamond uncovered | nothing | `DIAMOND_UNCOVERED` (name) |
| Diamond picked up | nothing | `DIAMOND_PICKEDUP_YOURTEAM` / `_OPPOSITION` (name) per team |
| Diamond cashed | "X cashed in a diamond!" | `DIAMOND_CASHED_IN_YOURTEAM` / `_OPPOSITION` (name) per team |
| Occupation bomb spawn | nothing | attackers: `TAKE_BOMB_TO_ENEMY_BASE`; defenders: `STOP_BOMB_REACHING_BASE` |
| Occupation bomb carried | nothing | attacker: `PLAYER_HAS_BOMB_ATTACK`/`_DEFEND`; defender: `DEFENDER_HAS_THE_BOMB_*`, or `PLAYER_HAS_BOMB_KILL`/`_HIDE` once intercepted |
| Occupation result | "Bomb detonated in Green base!" | `BOMB_SUCCESSFUL` / `BOMB_FAIL` (team id) |

Unchanged and already retail: `MAP_VOTED_MESSAGE` (vote result), Demolition
`DEMOLITION_START`/`REPAIR_BASE`, Diamond/Occupation start cues on reveal,
kill feed `KillAction(46)`, join/leave roster packets, vote overlays.
Territory Control and Multi-Hill: see the next section (TC sent no packet 50
at all before 2026-09-25).

Inferred, not recovered: the countdown schedule (5/2/1 min, 30/10 s) and the
pairing of the two Zombie survivor lines. Arena still uses free text (it is
not a retail mode).

Tests: `tests/test_retail_announcements.py`; mode suites (zombie, VIP, TDM,
CTF, recovered objective modes, scoreboard, commands) pass.

## Territory Control and Multi-Hill (2026-09-25)

Templates (client `aoslib/strings/english.py`, referenced by no client
binary, so server-sent as packet 50):

```
TC_START                         Claim enemy territory!
TC_ENTER_BASE_PLAYER             Occupying territory {0}! Hold position to control it!
TC_ENTER_BASE_TEAMMATES          Territory {0} occupied by {1}! Join them to take it faster!
TC_ENTER_BASE_OPPOSITION         Territory {0} occupied by {1}! Stop them from controlling it!
TC_CAPTURED_CAPTURINGTEAM        Territory {0} controlled! {1} of {2} left!
TC_CAPTURED_LOSINGTEAM           Territory {0} lost! {1} of {2} left!
TC_NEUTRALCAPTURED_CAPTURINGTEAM Neutral territory {0} claimed! {1} of {2} left!
TC_NEUTRALCAPTURED_LOSINGTEAM    Neutral territory {0} taken by the enemy! {1} of {2} left!
MULTI_HILL_START                 Claim the hill for your team!
MULTIHILL_OCCUPIED_YOU/_FRIENDLY/_ENEMY  Hill occupied by {0} - ...
MULTIHILL_CONTESTED              Hill contested!
MULTIHILL_LOST                   Hill lost!
```

`{0}` for TC is the base letter from `TC_BASENAMES` (A..J, the same index as
the `ZONE_ICON_TERRITORY_*` minimap icon); `{1}` is a player name. Parameters
are sent raw (`localise_parameters` off).

| Event | Audience | Sent |
|---|---|---|
| TC settled GameScene | the joiner | `TC_START` (reveal, like TDM/Diamond) |
| A team steps onto a territory it does not own (its occupant count goes 0 -> n) | entering team inside: `TC_ENTER_BASE_PLAYER`; rest of that team: `TC_ENTER_BASE_TEAMMATES` (letter, first entrant by id); other team: `TC_ENTER_BASE_OPPOSITION` | once per (territory, team) per `TC_NEW_TEAM_ENTERS_SHOUT_COOLDOWN` (5 s) |
| Ownership flips to a team, last owner was the enemy | capturer: `TC_CAPTURED_CAPTURINGTEAM`; other: `TC_CAPTURED_LOSINGTEAM` | once per flip |
| Ownership flips to a team from neutral | capturer / other: `TC_NEUTRALCAPTURED_*` | once per flip |
| MH settled GameScene | the joiner | `MULTI_HILL_START` |
| Hill claimed | claimant: `_OCCUPIED_YOU`; team (minus claimant): `_OCCUPIED_FRIENDLY`; other team: `MULTIHILL_LOST` if it owned the hill, else `_OCCUPIED_ENEMY` | once per flip |
| Hill becomes contested | everyone | `MULTIHILL_CONTESTED`, at most once per hill per 5 s |

Inferred, not recovered: the `{1} of {2} left` count. We send, for the
capturing team, the territories it still has to take (total minus owned),
and for the other team the territories it still holds. The retail server
source is not available; if a capture shows otherwise, change
`TerritoryControlMode._announce_capture`. The TC enter shout firing only for a
non-owner team, and the 5 s contested cooldown in MH, are also our reading
(the cooldown constant itself is retail).

All per-team/per-player sends go through the BaseMode helpers, so loading
peers (`in_game` false) and bots get nothing and an oversized name cannot
raise out of the tick. Spectators get no MH claim line (before they got the
"enemy" line).

Related fixes the same day: TC `score_limit` is now the active territory
count (the win rule), so StateData/HUD agree; MH personal scores now use the
recovered `MH_SCORE_OCCUPY`/`CONTEST` (every 5 s, like TC occupy/contend),
`MH_SCORE_DEFEND`/`ASSAULT` (kills on a hill, like TC) and
`MH_SCORE_CONTROL` for taking a hill from the enemy (TC_SCORE_CONTROL
analogue; `MH_SCORE_FIRST` stays for the first claim of a fresh hill;
`MH_SCORE_CLAIM` has no event left and is unused). MH restarts clear the
previous hill icons, and both teams reaching the limit on one tick is
decided by the higher score (exact tie: `GAME_DRAWN`). Contested rule is the
same in both modes: the larger side still progresses/flips, an even split
holds.

Tests: `tests/test_territory_control.py`, `tests/test_multi_hill.py`.

## Late additions (2026-09-27)

Evidence: the same binary-grep rule as above. `ZOMBIE_START_SURVIVOR`,
`ZOMBIE_START_ZOMBIE`, `BASE_DEPLETED`, `BASE_OCCUPIED_ATTACK`,
`BASE_OCCUPIED_DEFEND`, `DIAMOND_BASE` and the `KICK_DENIED_*` ids exist in
`aoslib/strings/english.py` and are sent by no client code path of their own,
so the retail server sent them as packet 50. **The trigger moments are
inferred from the text** unless stated; the retail server source is not
available.

```
ZOMBIE_START_SURVIVOR  Escape the zombies!                          EN:208
ZOMBIE_START_ZOMBIE    You have been infected! Kill the survivors!  EN:209
BASE_DEPLETED          Hill depleted! 
Airstrike incoming!          EN:149
BASE_OCCUPIED_ATTACK   Detonate the bomb inside the enemy base!     EN:329
BASE_OCCUPIED_DEFEND   Stop the bomb detonating inside your base!   EN:330
DIAMOND_BASE           Bring diamonds here!                         EN:345
```

| Event | Audience | Sent |
|---|---|---|
| Round start (every `on_mode_start`, in-place restarts included) | every in-game human | the mode's start cue: `TEAM_DEATHMATCH_START`, `TC_START`, `MULTI_HILL_START`, `DIAMOND_START`, `OCCUPATION_START_ATTACK`/`_DEFEND` by team. Before, only joiners got it (`reveal_to`), so players who stayed through a restart never saw it again. Joiners still get it on reveal. |
| Zombie outbreak clock arms (every round) | survivors | `ZOMBIE_START_SURVIVOR` (override), then `ZOMBIE_VIRUS_RELEASED` queued behind it (override now off) |
| Zombie late joiner | by team | `ZOMBIE_START_SURVIVOR` / `ZOMBIE_START_ZOMBIE` |
| Multi-Hill hill times out (airstrike launched) | everyone | `BASE_DEPLETED` |
| Occupation: an attacking (Blue) bomb carrier enters the target volume | attackers `BASE_OCCUPIED_ATTACK`, defenders `BASE_OCCUPIED_DEFEND` | edge-triggered, at most once per 5 s (TC shout cooldown reused) |
| Occupation round start | per team | the start cue, then `TAKE_BOMB_TO_ENEMY_BASE`/`STOP_BOMB_REACHING_BASE` queued (not overriding) |
| Diamond Mine: a depleted drop-off is replaced mid-round | everyone | `DIAMOND_BASE` (the round-start drop-off is covered by `DIAMOND_START`) |
| Vote-kick refused | the starter only | `KICK_DENIED_FOR_SPECTATOR`, `KICK_DENIED_REASON_SELF_KICK`, `KICK_DENIED_REASON_KICK_HOST` (Map Creator host), `KICK_DENIED_REASON_VOTE_IN_PROGRESS`, `KICK_DENIED_REASON_VOTE_TOO_SOON` (`{0}` = seconds left), `KICK_NOT_ENOUGH_PLAYERS`. IDA on the stock hud.pyd: `KickVotePlayerSelect.packet_received` closes the kick menu 0.5 s after four of these ids; the client never validates a kick itself. Check order is ours. |

Other round-start HUD fixes the same day:

- Diamond Mine opens the next-map ballot when a team reaches
  `DIA_DIAMONDS_TO_TRIGGER_MAP_VOTE` (12 of 15; defined as the target minus
  3, and that lead is kept for other targets). The 60 s-before-time-limit
  ballot stays as the fallback.
- Demolition: the build phase (30 s) now drives DisplayCountdown(84); the
  round clock takes over when the bases unlock. No retail string exists for
  the build phase, so the HUD timer is the cue (our reading).
- Zombie StateData `team_headcount_type` = 0 (`TEAM_PLAYERS_COUNT_VALUE`):
  the HeadCount shows zombies vs survivors instead of the hidden scores.
  Retail's per-mode value is server-side and unrecovered (**VERIFY** with a
  capture); every other mode keeps 6 (draws like `TEAM_SCORE_VALUE`).

Not sent (evidence too weak for a trigger): `BASE_ACTIVATED`
"Base activated", `BASE_OCCUPIED` "Hill occupied!" (the `MULTIHILL_OCCUPIED_*`
lines already cover claims), `TAKE_BOMB_TO_ENEMY_BASE_FAIL`,
`DIAMOND_SUPPORT`.

Implemented in the 2026-09-29 feature recovery: `VIP_ALREADY_DEAD` is private
to a joining/team-switching player locked out because their team's VIP died.
`DIAMOND_CASHED_IN_LOOSE_YOURTEAM` / `_OPPOSITION` announce a loose diamond
cashed in for its last carrier's team. The retail strings are recovered;
these server-side triggers are inferred, with dedicated regression coverage
in `test_vip_already_dead.py` and `test_diamond_loose_cash_in.py`.

Tests: `tests/test_parity_late_additions.py`,
`tests/test_votekick_retail_format.py`, `tests/test_retail_announcements.py`,
`tests/test_occupation_fixes.py`.
