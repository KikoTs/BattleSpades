# Parity changes, 2026-09-24 .. 2026-09-27

A change log and parity reference for the work that landed on top of commit
`f3518e2` ("Keep a slot free for humans..."). All of it is still
**uncommitted** in the main checkout: about 150 modified files, around 40 new
modules and roughly 80 new test files.

The ground rules for this work came from Kiril's polish brief
([POLISH_BACKLOG_2026-09-24.md](POLISH_BACKLOG_2026-09-24.md)):

- The server stays 100% retail and uses only the packets in the dumped list
  ([PROTOCOL.md](PROTOCOL.md)).
- Our own client must keep working.
- Every new behaviour is checked against the stock client.
- All work is server-side, apart from the explicitly named client patches.
- RankUps(66) is never sent.

How to read this page: every row says **what** changed, **why** (a short
summary of the retail evidence), **where** (the key files), and which doc
holds the full evidence. When a per-topic doc and this page disagree, trust
the code and the tests. Some of the earlier docs list items as "not
implemented" that were built later in the week; those are marked
**(superseded)** below.

Evidence labels used in the docs:

- **IDA**: headless IDA on stock Steam binaries.
- **live**: tracer dev client plus a console probe.
- **oracle**: stock module executed under Py2.7.
- **inferred**: our reading. No retail server source or capture exists.

---

## Contents

1. [Per-topic evidence docs](#1-per-topic-evidence-docs-written-this-week)
2. [Netcode and smoothness](#2-netcode-and-smoothness)
3. [Map memory and VXL](#3-map-memory-and-vxl)
4. [Game modes](#4-game-modes)
5. [Scoring](#5-scoring)
6. [Announcements and end screen](#6-announcements-and-end-screen)
7. [Anticheat and conduct](#7-anticheat-and-conduct)
8. [Packets](#8-packets)
9. [Blocks, colours, prefabs](#9-blocks-colours-prefabs)
10. [Spawns and objective guards](#10-spawns-and-objective-guards)
11. [Weapons and explosions](#11-weapons-and-explosions)
12. [Kill feed](#12-kill-feed)
13. [Crates and classes](#13-crates-and-classes)
14. [Sounds, music, spectators](#14-sounds-music-spectators)
15. [Parachute](#15-parachute)
16. [Bots](#16-bots)
17. [Team balance, map vote, player count, scoreboard](#17-team-balance-map-vote-player-count-scoreboard)
18. [Soak findings and fixes](#18-soak-findings-and-fixes)
19. [Retail values changed](#19-retail-values-changed)
20. [Known deviations and open decisions](#20-known-deviations-and-open-decisions)
21. [Remaining known issues](#21-remaining-known-issues)
22. [Config keys added](#22-config-keys-added)
23. [How to verify](#23-how-to-verify)
24. [Late additions (2026-09-27)](#24-late-additions-2026-09-27)

---

## 1. Per-topic evidence docs written this week

These docs were new or changed in `docs/` on or after 2026-09-24.

| Doc | Topic |
|---|---|
| [RETAIL_JUMP_RESTORE.md](RETAIL_JUMP_RESTORE.md) | Client jump rollback (launch-frame restore patch) and jump-pack thrust boundaries |
| [RETAIL_INPUT_LOSS.md](RETAIL_INPUT_LOSS.md) | Unsequenced ClientData, lost-frame refill, ENet head-of-line stalls |
| [LAG_COMPENSATION.md](LAG_COMPENSATION.md) | Target-hitbox rewind for hitscan and melee |
| [MAP_SYNC_JOIN.md](MAP_SYNC_JOIN.md) | Late-join terrain: MapSync already complete, air catch-up off |
| [MAP_MEMORY_2026-09-24.md](MAP_MEMORY_2026-09-24.md) | Per-column fill colour (MayanJungle 286 MB to 27 MB) |
| [VXL_MARKERS.md](VXL_MARKERS.md) | Chroma marker stripping, flare restore, team-coloured art |
| [MAP_METADATA.md](MAP_METADATA.md) | Retail `.txtc` recovery, sidecars, skyboxes, map x mode coverage |
| [ANNOUNCEMENTS_RETAIL_2026-09-24.md](ANNOUNCEMENTS_RETAIL_2026-09-24.md) | LocalisedMessage ids, end screen 72/53/73/52, TC/MH lines |
| [KILLFEED_RETAIL.md](KILLFEED_RETAIL.md) | KillAction multikill, domination and revenge; SetHP burn type |
| [WEAPONS_RETAIL.md](WEAPONS_RETAIL.md) | Stock weapon values, per-part damage, explosion curve, fire and goo |
| [CRATES_CLASSES_RETAIL.md](CRATES_CLASSES_RETAIL.md) | Parachuted crate drops, class deployables, radar |
| [RETAIL_VALUES.md](RETAIL_VALUES.md) | Every tunable compared with retail; the deliberate deviations |
| [SOUNDS_RETAIL.md](SOUNDS_RETAIL.md) | Server-sent sound and music cues, OpenAL error flush, rollover freeze |
| [PARACHUTE.md](PARACHUTE.md) | SPACE-deploy rule, owner handoff, fall damage |
| [BOT_ZOMBIE_REFUGE_2026-09-24.md](BOT_ZOMBIE_REFUGE_2026-09-24.md) | Survivor refuge election and ramparts |
| [SOAK_2026-09-26.md](SOAK_2026-09-26.md) | 94-minute pre-release soak and its ranked findings |
| [POLISH_BACKLOG_2026-09-24.md](POLISH_BACKLOG_2026-09-24.md) | Working backlog and its status |
| [PROTOCOL.md](PROTOCOL.md) (updated) | Packet table rows for 2, 7, 10-15, 18, 29-42, 49, 51, 64, 65, 72-83, 85, 96, 100, 116 |
| [ADMIN_GUIDE.md](ADMIN_GUIDE.md) (updated) | Every new config table, admin login, conduct, anticheat report |
| [RUNBOOK.md](RUNBOOK.md) (updated) | Input gap refill, internet-ping gate recipe, prefab budgets |
| [ARCHITECTURE.md](ARCHITECTURE.md) (updated) | Competitive prefabs commit whole in one tick |

---

## 2. Netcode and smoothness

| Change | Why (evidence) | Where | Doc |
|---|---|---|---|
| **Client jump restore patch.** A byte patch jumps over `Character.update_alive`'s launch-frame `set_position(network_position)`, and the `.reloc` entry is neutralised. | IDA: on the jump frame the stock client resets its position to an old self row, so every jump caused one ADJUST. After the patch: 0 ADJUST, maximum error 0.0014 blocks. The stock Steam client keeps one ADJUST per jump. | `aceofspades_revival/tools/patch_character_jump_restore.py` | RETAIL_JUMP_RESTORE |
| **Lost-frame refill.** The server simulates a missing ClientData label with the held input, one per tick, for gaps up to `input_gap_fill_limit` (8, hard cap 9). It never stamps an owner self row with a refilled label, and the next real packet uses the midpoint of the two aims. | Sniffing showed that ClientData is ENet `SEND_UNSEQUENCED`, not reliable. IDA showed that the client labels are contiguous (clock sync only jumps when the gap is over 10). One lost packet left the server one frame behind for good. At ~80 ms RTT with 0.5% loss, 145 corrections fell to 0-1. | `server/player.py` (`_synthesize_missing_frame`), `server/replication.py` | RETAIL_INPUT_LOSS |
| **Unreliable countdown refresh.** The once-per-second `DisplayCountdown(84)` is now sent unreliably. Round starts and transitions stay reliable. | A lost reliable packet on channel 0 holds every later WorldUpdate (ENet head-of-line blocking); one run froze for 69 loops. The countdown was the only steady reliable traffic in a quiet round. | simulation runtime scheduler, `send_round_timer(reliable=False)` | RETAIL_INPUT_LOSS |
| `unreliable_throttle_deceleration = 0` | A guard so ENet never throttles WorldUpdates. The measured throttle stayed at 32/32 either way. | `server/config.py`, `[network]` | ADMIN_GUIDE |
| **Jump-pack thrust boundaries.** New keys `jetpack_activation_defer_frames` = 2 and `jetpack_exhaustion_tail_frames` = 3. The old `jetpack_owner_*handoff*` knobs are not wired to anything. | 60 Hz capture alignment showed that the owner applies each transition row 2-3 frames late. With 2/3 the residual is always a forward nudge and never the 0.5-block rollback. | `server/player.py`, `[debug]` | RETAIL_JUMP_RESTORE |
| **Lag compensation.** Hitscan, pellet and melee hit tests use target hitboxes rewound by about one RTT, clamped by `max_ms` and `RTT + extra_ms`. Terrain, damage and knockback use the live world. Stale or future snapshot claims are logged only. | IDA: the client stamps `shot_on_world_update` and extrapolates remote players (no interpolation buffer), so the view is about one RTT old. | `server/lag_compensation.py`, hook in `combat_runtime._find_first_player_hit` | LAG_COMPENSATION |
| **Late-join terrain.** `map_air_catchup_enabled = false` by default. | Live: 1310 probed dug cells, 0 mismatches without the replay. MapSync already overlays the dirty columns. The replay made joiners watch terrain "sync" after they spawned. | `server/world_manager.py`, `[network]` | MAP_SYNC_JOIN |
| **Parachute owner handoff.** See [section 15](#15-parachute). | | `server/player.py` | PARACHUTE |

---

## 3. Map memory and VXL

| Change | Why | Where | Doc |
|---|---|---|---|
| **Per-column fill colour.** Implicit interior voxels no longer get a colour-table entry. New probes: `column_fill_color`, `has_explicit_color`, `color_entries`. | The VXL format never stores interior voxels, and the client colours them itself. MayanJungle: 286 MB to 27 MB RSS, and 1.8 s to 0.68 s to load. This is what starved the 512 MB hosts. | `aoslib/vxl.pyx` (rebuild needed) | MAP_MEMORY |
| **Sync test contract.** Solidity must match exactly everywhere. Colours only need to match where the server holds an explicit colour. | Interior colours belong to the client and are never transmitted. | `tests/test_vxl_sync_stress.py` | MAP_MEMORY |
| **nogil loader.** The Cython parse, floor fill, size scan and marker scan run with the GIL released. The surface scan is now a C column probe. | Soak finding 2: the "off-thread" preflight froze the whole server for 60-400 ms at every `/map` or `/mode`. | `aoslib/vxl.pyx`, `server/runtime_vxl.py` | SOAK_2026-09-26 |
| **C collapse walk.** `find_unsupported_chunks` runs in C and keeps the GIL for the small walks. | Soak finding 3: grave craters spiked ticks to 5-37 ms, because each destroyed cell ran a Python flood. Releasing the GIL per walk also cost ~15.6 ms per switch under bot load. A detonation now costs under 5 ms. | `aoslib/vxl.pyx`, `server/world_manager.py` | WEAPONS_RETAIL, SOAK |
| **Chroma markers.** Only `0x0000FF` and `0x00FF00` (masked with `0xF0F0F0`) are stripped when exposed. Each stripped marker becomes a type-13 `FlareBlockEntity` that puts the voxel back with the static-light colour. Team art (`#0028BE`, `#00BE28` and similar) stays solid. | IDA on `aoslib.vxl.pyd` `sub_10029FD0`. | `server/runtime_vxl.py`, `MapResourceService`, `WorldManager.restore_static_light_block` | VXL_MARKERS |
| **Flare cells reach bots.** Restored flare cells are published to the bot worker map (`_publish_static_lights`, `static_light_listeners`). | VXL_MARKERS "known gap": bots treated restored flares as air. | `server/bot_ai/director.py`, `gateway.py`, `compact_vxl.py` | VXL_MARKERS |
| **Retail map metadata.** An inert Py2.7 marshal replayer extracts the four retail `.txtc` maps and the `playlists.mapinfo` catalogue into `maps/*.json` and `maps/retail_map_info.json`. Skybox and orientation fixes for maps with no retail data: CastleWars uses Invasion, DoubleDragon uses SecretBase_Night, Crossroads uses WW1. | Retail shipped descriptions for only DragonIsland, MayanJungle, SpookyMansion and Trenches. The fixes are based on loading art and team paint. | `tools/map_metadata/`, `server/map_metadata.py`, `maps/*.json` | MAP_METADATA |

---

## 4. Game modes

| Mode | Change | Where / tests |
|---|---|---|
| All modes | **Lifecycle contracts:** leave hook runs before PlayerLeft, dead-on-join when respawn is forbidden, alias canonicalisation (`[modes.zombie]` becomes `zom`, and an unknown `default_mode` is fatal), failed-transition recovery, isolated-runtime command refusal (`/map`, `/mode`, `/restart`, `/endround` in Tutorial and UGC), `_end_by_time` with any team count. A retiring mode never finishes the match or opens a vote. An end-screen joiner gets ForceShowScores(1) and the ending track. | `modes/base_mode.py`, `server/match/__init__.py`; `test_mode_lifecycle_contracts`, `test_mode_fixes_round2_*` |
| Zombie | **Rounds:** `RULE_ZOMBIE_NOOF_ROUNDS` restarts on the same map, and only the last round ends the match. **Population collapse:** if patient zero or the last survivor leaves a two-player round, the round aborts to waiting. Spectators do not count toward the minimum. Spectator joins get the right kit. Survivor win pays the retail individual bonus. There is no generic kill score. **Team locks:** LockTeam(79) and TeamLockClass(80) at the countdown/outbreak boundary, and survivor class changes are refused during the outbreak. **Patient zero:** 0.5 s spawn protection (`FIRST_ZOMBIE_SPAWN_PROTECTION_TIME`). | `modes/zombie.py`; `test_zombie_rounds`, `test_retail_rules_modes`, `test_retail_end_screen` |
| Zombie bots | **Refuge and ramparts.** See [section 16](#16-bots). | BOT_ZOMBIE_REFUGE |
| VIP | **Sudden death:** after a VIP dies, the bereaved team stops respawning and drains 1 HP/s (from `CG.VIP_SUDDEN_DEATH_*`), using `VIP_MODE_KILL` and SetHP type 4. A dead sudden-death player cannot trade for a respawn through a team switch; joiners on that team start dead. **Score events:** VIP Defend, Distraction, Close to VIP, VIP Assault. A departed VIP gets no marker packets, and a reused id never inherits one. Deaths during the end screen change nothing. | `modes/vip.py`; `test_vip_sudden_death_rules`, `test_retail_rules_modes` |
| CTF / Classic CTF | Objectives freeze during the end screen: no capture, pickup or touch-return. A match-winning capture stops the rest of the tick. A departed carrier drops without player-bound packets. **Intel home** sits on the authored base point (it used to be 12 blocks toward the enemy); Classic CTF uses the 3-block minimum radius. **Carrier minimap exposure** after `INTEL_MINIMAP_EXPOSURE_TIME` (30 s) instead of for the whole carry, and a quick capture sends no marker. **Base pit rule** (`ctf_base_pit_depth`). Score target is 5. | `modes/ctf.py`, `modes/classic_ctf.py`; `test_ctf_end_and_departure`, `test_retail_intel_radar`, `test_score_events` |
| Territory Control | Retail `TC_*` announcements, which TC never sent before. `score_limit` = the number of active territories (the win rule). Capture-point resupply every `CAPTURE_POINT_REFILL_TIME` on owned territories (`capture_point_resupply` in `[modes.tc]`). AFK and escape-flagged players do not count toward holding a zone. | `modes/territory_control.py`; `test_territory_control`, `test_retail_rules_modes` |
| Multi-Hill | `MULTIHILL_*` announcements. Personal scores for occupy, contest, defend, assault and control. **First to Hill and Claim Hill are separate awards.** Held-hill resupply. A restart clears the old hill icons. If both teams hit the limit in the same tick, the higher score wins (an exact tie is `GAME_DRAWN`). Bots clear a hill before its expiry airstrike. | `modes/multi_hill.py`; `test_multi_hill`, `test_retail_rules_modes` |
| Occupation | Bomb announcements. Team-switch credit. No drops after the end. Spawn cues. Defender bot disposal. Restart entity-id hygiene. | `modes/occupation.py`; `test_occupation_fixes` |
| Demolition | Personal destroy and repair awards (via the build/blast mode-event hooks). Timeout result cue. The siren sounds when the base falls, 5 s before the shells. Airstrike landing delay. Simultaneous destruction is resolved fairly. POIFocus(18) at the airstrike. | `modes/demolition.py`; `test_demolition_fixes`, `test_block_mode_events` |
| Diamond Mine | Carrier cash-in cue. Discovery cooldown. No drops after the end. Restart entity-id hygiene. Score target fallback is 15. | `modes/diamond_mine.py`; `test_diamond_mine_fixes` |
| Arena (not a retail mode) | Respawn path, leave, scores and restart fixes. | `modes/arena.py`; `test_arena` |
| Airstrike | Objective airstrike shells no longer claim a real player slot. | `modes/airstrike.py`; `test_airstrike` |

---

## 5. Scoring

| Change | Why | Where / tests |
|---|---|---|
| **Generic kill score** in `BaseMode.award_generic_kill_score`: `GENERIC_SCORE_KILL`, `_HEADSHOT`, `_MELEE`, `_SUICIDE`, `_TEAMKILL` with the retail reasons. CTF and Classic CTF keep 100/150. Tutorial and UGC get no generic combat scores, and Zombie gets no generic assist. | These are retail constants that no server code used before. | `modes/base_mode.py`; `test_mode_fixes_round2_scoring`, `test_mode_lifecycle_contracts` |
| **Revenge and payback bonuses:** `GENERIC_SCORE_REVENGE` (you kill whoever dominates you) and `GENERIC_SCORE_PAYBACK` (you kill your last killer). | [KILLFEED_RETAIL.md](KILLFEED_RETAIL.md) listed them as unused **(superseded)**. | `server/player.py`, `modes/base_mode.py` |
| **Objective score events:** CTF, Diamond and Occupation intercept, carrier defend, distract, defend, assault, carry and escort (with hysteresis), survive the bomb blast, close to bomb, and **teabag** (three quick crouches over a fresh enemy corpse). Teabag points only count when `RULE_POINTS_FROM_TEABAGGING` is on. Spectators, departed and retiring players, and events after the round end are never paid. | Retail `SCORE_REASONS` and `CG.*` values. | `server/combat_scores.py`; `test_score_events` |
| **Commendations.** `COM_*` values are retail leaderboard aggregates (`SCORE_REASONS_FOR_TOTALS`), not in-match medals. Every reason and `*_TOTAL` counter rolls into its COM stat. Bots score but keep no profile. | Stock `LeaderboardMenu` columns. | `server/profile_stats.py`; `test_commendations` |
| **Round awards (GameStats 67)** for every mode. Ties go to higher score, then fewer deaths, then lower id. Leavers and spectators are excluded. Bots are eligible. Suicides and transitions do not count. | | `test_round_awards` |
| **Transition deaths.** A `/kill` or a team or class change within 5 s of enemy damage counts as that enemy's kill. A class change that would kill again within 3 s is staged for the next life. | This closes kill-denial exploits. | `commands/player.py`, handlers; `test_anticheat_commands` |

---

## 6. Announcements and end screen

| Change | Why | Where / tests | Doc |
|---|---|---|---|
| Mode events now go out as **LocalisedMessage(50) with retail string ids**, team-relative where retail is: CTF, VIP, Zombie, Diamond, Occupation, TC, MH, `PLAYER_JOINED`, the round clock (`COUNTDOWN_MINUTES` 5/2, `ONE_MINUTE_LEFT`, `COUNTDOWN_SECONDS` 30/10) and the `GAME_DRAWN` draw. The invented TDM "leads by N" line is removed. | Any id that no client binary references can only reach the screen through packet 50, so the retail server must have sent it (binary grep plus IDA xrefs). Client-local ids such as `RESPAWNING_IN` and `VIP_YOU_ARE_VIP` are never sent. | `modes/*.py`, BaseMode helpers; `test_retail_announcements`, `test_territory_control` | ANNOUNCEMENTS |
| **End screen.** Every round end sends ForceShowScores(72)=1 with the victory music, then 72=0 on the in-place restart. **Rollover order:** after the 5 s score delay, `PlaySound flush -> 72(0) -> ShowGameStats(53) -> ShowTextMessage(73 headline)`, a hold for `end_screen_seconds`, then `flush -> MapEnded(52)`. | IDA: 73 only renders on ViewGameStats. **Correction 2026-09-26:** under 72(1), packet 53 cannot switch the menu, so the hold must be released first. Otherwise "I didn't see the end results". | `server/match/`, `modes/base_mode.py`; `test_retail_end_screen` | ANNOUNCEMENTS |
| **Vote-kick GenericVoteMessage(47)** uses the retail tuple format: `('VOTE_TO_KICK_TITLE', ())` and `(('VOTE_TO_KICK_DESCRIPTION',(1,)), (target,'KICK_REASON_ABUSE',starter))`. | IDA on `GenericVotingHUD.decode_string` (`literal_eval` then `get_by_id` then `format`). | `server/handlers/social.py`; `test_votekick_retail_format` | - |
| Inferred, not recovered: the countdown schedule, the TC "{1} of {2} left" count, the TC enter shout only for a non-owner team, and the 5 s MH contested cooldown. Arena still uses free text. | | | ANNOUNCEMENTS |

---

## 7. Anticheat and conduct

| Change | Why | Where / tests |
|---|---|---|
| **`server/anticheat.py`** is a shared accounting layer. `report()` is a rate-limited counter and log. `protocol_violation()` kicks with `ERROR_KICK_HACKING` (when `kick_on_protocol_violation` is on) for data the stock client can never produce: NaN, forged packet 35, and so on. | | `test_anticheat_*` |
| **Validation.** Combat and terrain checks: shot origin, melee reach, occluded build, dig or placement, packet 35 outside UGC, skipped Blocksucker warm-up, unmounted MG, UseOrientedItem origin reach. Movement checks: input starvation and input backlog. Anything that could misfire on a legitimate client under packet loss is **log-only** until its `enforce_*` switch is set. | Shooting through open doorways, hugging a wall and building against a wall must keep working. | `server/combat_runtime.py`, `server/player.py`, handlers; `test_anticheat_combat`, `test_anticheat_movement`, `test_input_validation_combat` |
| **Statistical report** (`anticheat_report.py`). Signals: headshot kill share, headshot hit share, per-weapon accuracy against the human population percentile, aim snap onto a head, reaction time, shotgun pellet-seed skew, and sustained log-only violations. The report writes JSON lines to `logs/anticheat.jsonl` and is reviewed with `/acreport` and `/acstats`. **It never kicks.** Bots are never analysed. | Detection only, for fleet review. | `server/anticheat_report.py`, `commands/anticheat_admin.py`; `test_anticheat_report` |
| **Admin login hardening.** `/admin` is disabled while the password is `changeme`, empty or shorter than 12 characters (with a startup WARNING). The compare is constant-time. Three failures lead to a kick and a 10-minute IP ban. Slash commands are rate-limited (burst of 5, then 1/s; admins are exempt). `/team` shares every rule with ChangeTeam(77). | | `server/config.py`, `commands/admin.py`; `test_anticheat_commands` |
| **Conduct: grief.** Grief points for team damage and team kills, including indirect harm (whoever sets off a teammate's landmine is charged). Points decay, each incident is capped, then a private warning, then `ERROR_KICK_GRIEFING`. | | `server/conduct.py`; `test_conduct` |
| **Conduct: AFK.** Only real input resets the idle clock, which pauses while loading, dead or between rounds. Warning at 540 s, kick (`ERROR_AFK_TIMEOUT`) at 600 s. Spectators get 1800 s. | The soak verified it exactly with a real idle client. | `server/conduct.py` |
| **Names.** NFKC normalisation, invisible characters stripped, homoglyph-skeleton uniqueness (`~N` suffix), admin lookalikes become `Player~N`, reserved staff and server names become `Player`. Humans and bots share one namespace. The 15-byte limit is kept. | | `server/player_names.py`; `test_player_names` |
| **Chat and packet hardening.** A client cannot forge SYSTEM or BIG chat. TEAM chat reaches only teammates. Lines are cut to 200 characters with a token bucket. SetColor relays are throttled. Per-connection drain fairness. NewPlayerConnection is ignored before the handshake and for duplicates. Debug ids 241-243 are removed from the game channel. | | `test_packet_delivery_fixes` |

---

## 8. Packets

Full rows are in [PROTOCOL.md](PROTOCOL.md). `server/hud_packets.py` owns the
new HUD senders.

| Packet | Status now | Evidence |
|---|---|---|
| 18 POIFocus | **Sent** by Demolition at the airstrike. It has no expiry, so it is only sent where a death, respawn or map change follows, and never to spectators. | IDA + live |
| 41 / 42 MinimapBillboard / Clear | API ready (id-keyed, with late-join replay). No mode uses it, to avoid duplicating packet-43 zones and self-drawn entities. `key` is never read by the client, and an unknown icon name raises in the client. | live |
| 65 ProgressBar | **Blocked.** The stock client crashes on the first draw (`draw_progress_bar` gets 7 arguments but takes 5), and no hide sentinel exists. `set_progress` and `clear_progress` are no-ops. | live, byte-identical to Steam |
| 72 / 73 | **Sent.** See [section 6](#6-announcements-and-end-screen). | IDA + live |
| 79 / 80 LockTeam / TeamLockClass | **Sent** by Zombie. | IDA |
| 81 / 82 TeamLockScore / TeamInfiniteBlocks | **Sent** by the admin commands `/lockscore` and `/infiniteblocks`, with a joiner replay. | live |
| 83 TeamMapVisibility | **Unused.** Radar no longer sends it: the client detects radar targets itself, and the old packet named the wrong team. | live |
| 38 BlockManagerState | **Sent.** Wire format recovered. It merges damaged, user and occupied rows and carries block health and shading to joiners, batched by `prefab_health_state_batch`. | client round trip + live |
| 30 BuildPrefabAction | Competitive prefabs are echoed as **one packet 30 to every in-game client**, and each client expands the KV6 itself. Cells are committed whole in one tick. | live (superminibunker: 68 voxels, wallet -68, health 9.0) |
| 32 / 33 / 40 | Health model: 32 and 40 builds are 9.0, 33 is 3.0 with no wallet change, map voxels are 5.0. Observers get 33 plus a 38 user row. | live |
| 37 Damage | Exact per-type footprints and seeded random draws for all 44 types. | live fixture |
| 47 vote-kick | Retail tuple format. | IDA |
| 2 WorldUpdate | Real ping in the row ping field (scoreboard). Turret rows use the CreateEntity signed id. | IDA |
| 14, 34, 39, 75, 96, 111-113, 61-63 | Reversed and deliberately unused. PROTOCOL.md gives the reason for each. | IDA + live |
| 66 RankUps | Never sent (the Revival XP path handles it). | policy |

---

## 9. Blocks, colours, prefabs

| Change | Why | Where / tests |
|---|---|---|
| **One block-health model** shared by the server, builders, observers and joiners. Map voxels are 5, user and prefab blocks 9, packet-33 cells 3, all scaled by `RULE_BLOCK_HEALTH`. A spade deals 5 per cell: map voxels break at once, built cells need two swings. | Live capture of `add_damage` and `rnd_generator` for all 44 types (seed 77). Python 3's Mersenne Twister reproduces the Py2 draws. | `server/block_damage_model.py`, `WorldManager.apply_block_damage`; `test_block_health_model`, `test_prefab_block_health`, fixture `tests/fixtures/block_damage_footprints_live.json` |
| **Block darkening.** Joiners see the exact compounding `dim()` shade that live clients show. | IDA: `shared.common.dim` and the colour flow in `BlockManager.add_damage`. | `test_block_darkening` |
| **Colour convention.** One RGB helper set. SetColor, BlockLine and BlockBuild echoes are pinned so builder, observer, joiner and server agree. Implicit interior cells are never painted. | Live two-console measurement of how the stock client applies the sender's palette. | `server/colors.py`; `test_colour_consistency` |
| **Prefabs commit instantly.** A competitive prefab commits whole after physics, capped per tick across players by `prefab_competitive_cell_budget` (never split). The full model voxel count is charged. Reach, line of sight, body overlap and out-of-map checks are enforced. | Retail `PrefabManager.build_prefab` expands locally, so a trickle commit desynced. | `server/prefab_actions.py`; `test_prefab_instant_replication` |

---

## 10. Spawns and objective guards

| Change | Why | Where / tests |
|---|---|---|
| **Smart spawns.** Spawn columns are scored by enemy distance and line of sight, recent teammate deaths, stacking and squad support, with jitter. When the base is fully camped, spawns move to a safer part of the team's side. Bots hold fire on spawn-protected enemies. | Anti spawn-camp. The soak measured `choose_team_spawn` at p99 3.6 ms. | `server/spawn_selection.py`; `test_smart_spawns` |
| **Spawn protection** ends when the player attacks (firing, not digging) or picks up an objective. The shipped value is 3 s. | Retail rule value. | `test_smart_spawns`, `test_objective_abuse` |
| **Water lock fix.** A spawn area dug down to the water now falls back to the nearest dry land from a cached coarse grid. Only as a last resort is the player placed in water at the base, clear of solid blocks. | Live report: a dug-out base meant nobody could spawn, and the old walk took seconds per life. | `emergency_spawn` and `rescue_spawn`; `test_spawn_lock` |
| **Retail spawn boxes** are volumes: indoor storeys spawn inside buildings (MayanJungle temple, SpookyMansion floors), and SpookyMansion zombies rise from the sea ring. | Recovered `.txtc` layouts. | `server/map_metadata.py`, world manager; `test_retail_spawn_boxes` |
| **Escape watch.** Players out of bounds, below the floor, in the sky for 5 s, embedded for 3 s, or (objective players only) entombed for 5 s get the retail high-minimap marker. | Anti hiding. Uses the same icon as CTF carriers and VIPs. | `server/escape_watch.py`; `test_escape_watch` |
| **Objective guard.** Pickups need line of sight. Buried, sealed or floating objectives are resettled or returned home after 5 s. AFK zone holders do not count. | Anti abuse. | `modes/objective_guard.py`; `test_objective_abuse` |

---

## 11. Weapons and explosions

Full tables are in [WEAPONS_RETAIL.md](WEAPONS_RETAIL.md).

- **Stock values.** Values were read by executing the stock `aos.pkg` weapon
  classes under Py2.7. The nonsteam decompile's trailing named-constant block
  is a mod and is **not** used. Examples: the pistol is 20/45, 0.4 s, 0.6 s
  reload, range 550; the spade interval is 0.8 s; the pickaxe is 40 damage
  at 0.6 s.
- **Per-part damage.** The damage tuple order is `(TORSO, HEAD, ARMS, LEGS,
  LEGS)`. Limbs no longer take torso damage.
  - The head multiplier and `CLASS_DAMAGE_MULTIPLIER` now apply to the
    **victim** (they used to apply to the attacker).
  - Entity damage uses `*_DAMAGE_ENTITY`.
  - The minigun spin model now includes secondary-button pre-spin.
- **Explosions** now follow the stock `ExplosionDamageManager` curve
  `D*(R²-d²)/R²`.
  - Three line-of-sight rays weighted 0.5, 0.3 and 0.2.
  - Self and neutral damage are halved.
  - Knockback is scaled by line of sight.
  - The stock radius is enforced: dynamite 8, landmine 6, RPG 6.
- **Jetpack damage multiplier.** Damage taken while flying is scaled by
  `JETPACK_PROPERTIES` field 7 (inferred from the field's name).
- **Ammo.** Tested in `test_retail_ammo`.
  - Spawn grants the initial reserve.
  - A crate adds its restock amount, capped at the maximum.
  - `clip_reload` guns load one round per reload.
- **Molotov fire and Chemical Bomb goo.**
  - BlockFire (28) and BlockGoo (31) are surface entities.
  - Goo dissolves blocks with Damage type 43 and does no blast.
  - Contact burn uses SetHP type 3.
  - Goo timers reuse `BLOCKFIRE_*` (inferred).
  - Code: `server/chemical_goo.py`. Tests: `test_chemical_goo`, `test_fire`.
- **Explosion imports.** `server.explosions` and `server.kill_feed` are
  imported at startup. Before, the first blast of a match paid 100+ ms to
  import them.
- **Not applied, because no value was recovered:** range falloff and block
  penetration.

Key files: `server/weapons_retail.py`, `server/combat_runtime.py`,
`server/main.py` (`_apply_blast`), the weapon catalog, `server/dig_profiles.py`.

---

## 12. Kill feed

Full evidence is in [KILLFEED_RETAIL.md](KILLFEED_RETAIL.md). Code is in
`server/kill_feed.py`; tests are in `test_killfeed_retail`.

| Field or behaviour | Before | Now |
|---|---|---|
| `kill_count` (multikill banner) | the killer's life streak | kills chained within `MULTIKILLMAXTIMEGAP` = 6.0 s (a server-only constant) |
| `isDominationKill` | always 0 | 4th unanswered kill on one enemy (4 is the convention; the retail value is not recoverable) |
| `isRevengeKill` | always 0 | killing an enemy who dominates you |
| Relations reset | - | on team-change kill, disconnect (id reuse) and round reset |
| Late-joiner replay | carried stale banners | banner fields are zeroed |
| Burn damage | SetHP type 1 (hit arrow) | SetHP type 3 (burn indicator) |
| Spree plugin | loaded by default | renamed `plugins/_example_plugin.py`, so it is not loaded (its lines are not retail) |

---

## 13. Crates and classes

| Change | Why | Where / tests |
|---|---|---|
| **Parachute crate drops.** A respawned crate is re-created at the top of the world and falls on the client's own `GenericMovement` with its chute. The server mirrors the fall (30 blocks/s², 0.75 slowdown between 10 and 2 blocks above the support) and the pickup works mid-fall. | Live trace of the stock `Crate`. The drop altitude is inferred. | `PickupCrateBehavior(airdrop=True)`, `MapResourceService`; `test_crates_retail` |
| **Crate fly-by sound.** Ids 24/25/26 play at the drop start, with the variant chosen by skybox. | These are server-only sound ids. The choice of variant and the falloff are inferred. | same |
| **Crate respawn is 25 s.** | `CRATE_SPAWN_DELAY`. | [section 19](#19-retail-values-changed) |
| **Radar station.** 45 s lifetime (sent as the packet-21 fuse). The client detects within 250 blocks itself, so packet 83 is no longer sent. A new placement replaces the owner's old station. | IDA: `A1900` (250) is only read by `can_detect_player`, so the old "250 s lifetime" label was wrong. The AoS wiki confirms 45 s and replacement. | `server/main.py`, `server/deployable_actions.py`; `test_radar_retail`, `test_retail_intel_radar` |
| **Dynamite** has a one-point shell: a hit or a nearby blast detonates it. | `DYNAMITE_HEALTH` 1. | `test_class_abilities_retail` |
| **Disguise restock.** An ammo crate adds 3, capped at 3. | `DISGUISE_RESTOCK_AMOUNT` (listed as "missing" in the CRATES doc, **superseded**). | `server/player.py` |
| **Medpack** heals teammates only and spends its uses. | | `test_class_abilities_retail` |
| **Placement sounds** for dynamite, landmine and turret (ids 21/31/30). | No client code plays them (listed as "not sent" in the SOUNDS doc, **superseded**). | `server/deployable_actions.py` (`_placement_sound`) |

---

## 14. Sounds, music, spectators

Full cue table is in [SOUNDS_RETAIL.md](SOUNDS_RETAIL.md).

- **New cues:**
  - intel pickup, capture and return;
  - bomb drop and detonation;
  - diamond appear, drop, disappear and cash-in;
  - TC and MH capture;
  - objective airstrike fly-by;
  - zombie countdown timed to end exactly on the pick;
  - last-survivor and survivor-win music.
- **Never sent:** pickup sounds the client plays itself (ids 16 and 19).
- **Audiences:** team-relative pairs never reach spectators.
- **Music that never started.** The stock ALURE refused to open a stream
  while an older OpenAL error was pending. The client has only 128 sources,
  and the join burst exhausted them. The fix has two parts:
  - The server sends `PlaySound(BUILD, volume 0)` before every music switch,
    ambience registration, packet 53 and packet 52.
  - Join audio starts **before** the terrain catch-up.
- **Rollover freeze.** `alureDestroyStream` was refused while an error was
  pending, which left a stream on a deleted source. The real fix is in the
  client: `client_patches/session_transition_patch.py`, `install_audio_guard`.
- **Spectators** are admitted on `ClientInMenu(110, 0)`, because a team-0
  client never sends ClientData. Leaving spectator re-sends
  NewPlayerConnection(15) and joins the team. A spectator joining does not
  make a bot leave. Tests: `test_spectator_retail`.

---

## 15. Parachute

Full evidence is in [PARACHUTE.md](PARACHUTE.md). Code is the `_update_parachute`
family in `server/player.py`; tests are in `test_parachute`.

- **Trigger.** A fresh SPACE press while airborne, or Z/hover on the native
  client or for bots.
  - Why SPACE: stock clients have no parachute key, and `world.pyd` keeps
    airborne SPACE alive only for jetpack and parachute holders.
  - The chute only opens while descending.
  - It needs at least 6 blocks of clearance.
  - One deploy per fall.
- **Closes on:** landing, water, death, unequip, picking up a jetpack, 30 s
  open, or being lifted upward.
- **No stacking:** a player carrying any jetpack cannot open it.
- **Fall damage** equals that of a free fall that reaches the same landing
  speed. This stops a last-frame canopy from erasing the fall.
- **Owner handoff.** Canopy physics switches on at
  `max(S+3, N+2) + RTT·60`, when the owner first moves with the new state.
  Measured exact onset: 8/10 deploys on loopback, 6/10 at 80 ms RTT (the
  rest missed by one frame).

---

## 16. Bots

| Change | Why | Where / tests |
|---|---|---|
| **Zombie refuge and ramparts.** Each survivor team elects a high, flat, dry refuge in its own walkable region (from the `.botnav` atlas) and walls it with grounded BlockLine runs on a 9x9 ring. A new refuge is elected when the old one is breached or when nobody reaches it in 45 s. | Report: survivors stood still and never built. Smoke run: 0 to 22 wall runs, 0 survivor deaths. On MayanJungle, 0 to 39 runs after the reachability filter. | `server/bot_ai/zombie_refuge.py`, `policies.py`, `cooperative_behavior.py`, `project_sites.py`; `test_zombie_refuge`, `test_zombie_rampart` |
| **Mode fixes.** Demolition dig and repair orders. Retaking dropped CTF intel. Occupation escort, hunt and disposal. MH clears hills before the expiry airstrike. TC defenders relieve sieges. Infected bots never fortify. Role splits are per team. One broken mode block costs only its own objective. The bot leave hook runs before PlayerLeft. | | `server/bot_ai/*`; `test_bot_mode_fixes` |
| **Jetpack climbing.** Rocketeer and Engineer bots use their packs on ledges (66 bursts, 68 climbs; 67 never lifts more than a jump). | Measured on server physics. | `test_bot_jetpack_climb` |
| **Names.** Bot nicknames read as human. | | `server/bot_ai/profiles.py`; `test_bot_jetpack_climb`, `test_player_names` |
| **Skill balance.** Per-team bot aim and reaction ease or sharpen with how the humans are doing. Bounded by `skill_balance_*` and kept inside the configured difficulty band. | Bots should neither stomp nor feed the humans. | `server/bot_ai/skill_balance.py`; `test_bot_skill_balance` |
| **Perception budget.** Decorative flares are excluded, the overflow ranking is cached (hazards are always fresh), and cold refuge elections are spread over several refreshes. | Soak finding 4: 20thCenturyTown's 524 flares cost 2,388 overflow entities per second. Finding 5: a one-off 53 ms spike at the start of Zombie. | `director.py`; `test_bot_perception_budget` |
| **Dig rates.** Bots plan swings at the stock melee cadence that the server enforces. Plans budget the extra swings that player-built (health 9) blocks need. | The modded constants made bots swing too fast and blacklist edges. | `server/dig_profiles.py`, `simple_navigation.py`; `test_bot_dig_rates`, `test_bot_built_block_breach` |
| **Worker safety.** The bounded AI thread survives corrupt map snapshots. The release gate checks the production `SimpleBotBrain`. | Intermittent native faults in test processes (see the dev machine note). | `thread_supervisor.py`, `server/release_check.py`; `test_bot_worker_safety` |

---

## 17. Team balance, map vote, player count, scoreboard

| Change | Where / tests |
|---|---|
| **Join and switch balance.** A joiner is placed on the smaller side when the lead is at least `balance_threshold`. Unbalancing switches are refused, and switches have a 5 s cooldown. Zombie and host-assigned teams are exempt. | `server/connection.py`, `server/handlers/team.py`; `test_packet_delivery_fixes` |
| **Mid-match drift repair.** After the grace period: a dead bot switches sides; then a bot is retired and replaced; then the most recently joined dead human is moved (never a live player, carrier, VIP or last survivor, and never the same person twice within the cooldown). The moved player sees the `TEAM_FULL` overlay. | `server/team_balance.py`; `test_team_balance` |
| **Map vote fit.** Candidates are sized to the lobby (area from `.botnav`), never include the current map, and put recent maps last. | `server/match/`; `test_map_vote_fit` |
| **One population count** (`ServerPopulation`) shared by A2S, LAN, Steam and Revival. It counts loading humans and humans reloading during a map change, and keeps `humans + bots <= max`. | `server/steam_master.py`, `a2s_query.py`, `revival_master.py`; `test_player_count` |
| **Scoreboard PING.** The WorldUpdate row ping carries real values: ENet RTT for humans and a synthetic value for bots. | `server/replication.py` (`wire_ping_ms`); `test_scoreboard_retail_ping` |
| **Rollover keeps team-select peers.** They are no longer kicked with `ERROR_MATCH_ENDED` (soak finding 1). | `server/match/__init__.py`; `test_rollover_team_select` |

---

## 18. Soak findings and fixes

Source: [SOAK_2026-09-26.md](SOAK_2026-09-26.md). The run lasted 94 minutes
with 14 bots, 45 transitions, all 28 maps and all 9 modes. It had 0 errors,
p99 of 1.4-1.7 ms with no creep, and 0 anticheat or conduct false positives.
The AFK kick fired at exactly 600 s.

| # | Finding | Status |
|---|---|---|
| 1 | A rollover disconnected humans still on team select | **Fixed** (`test_rollover_team_select`) |
| 2 | The VXL preflight held the GIL, freezing the server for up to ~0.4 s | **Fixed**: nogil loader and C surface probe (`test_map_load_stall`) |
| 3 | Grave explosions caused 5-37 ms tick spikes | **Fixed**: C collapse flood, under 5 ms (`test_grave_blast_cost`) |
| 4 | 20thCenturyTown flares flooded bot perception | **Fixed**: decorative entities excluded (`test_bot_perception_budget`) |
| 5 | One-off `bots_perception` spike at the start of Zombie | **Mitigated**: the cold refuge election is spread over refreshes |
| 6 | RSS floor stepped up ~40 MB once, then plateaued | **Open**: needs a 6-hour soak with `thread_names` |
| 7 | `WorkerStatus.restarts` also counts planned recycles | Open (observability only) |
| 8 | Bots bypass packet validation, so the anticheat checks are unexercised | Coverage gap: validate with real clients through `udp_lag_proxy` |

---

## 19. Retail values changed

These values changed in code defaults, `config.toml` and the fleet profiles.
Enforced by `tests/test_retail_values.py`; see
[RETAIL_VALUES.md](RETAIL_VALUES.md).

| Setting | Before | Now (retail) |
|---|---|---|
| `RULE_CTF_SCORE_TARGET` | 10 | **5** |
| `RULE_CRATES_SPAWN_TIME` | 15 s | **25 s** |
| `[modes.zom] infection_delay` (sample config) | 30 s | **60 s** |
| `RULE_SPAWN_PROTECTION_TIME` (sample config) | OFF | **3** (ends early on attack or objective pickup) |
| `radar_station_lifetime_seconds` | 35 s | **45 s** |
| `mode_data` score fallbacks | ctf 10, dia 10, oc 100, tc 100, zom 1, dem 5 | ctf 5, dia 15, oc 30, tc = base count, zom 3, dem 1 |
| Weapon, melee and explosive figures | modded decompile values | stock Steam values ([section 11](#11-weapons-and-explosions)) |

After the next deploy, official CTF plays to 5 captures and crates respawn
after 25 s.

---

## 20. Known deviations and open decisions

| Item | Retail | Ours | Why / decision needed |
|---|---|---|---|
| Respawn time | 10 s | **5 s** | Deliberate: the historic BattleSpades pace. Revert to 10 if full parity is wanted. |
| In-round music bed | no bed (`INGAME_MUSIC` has only last-man, ending and tutorial) | a random `last_man_standing` loop every round | Added earlier at Kiril's request. It makes the last-man cue mostly a no-op. Since 2026-09-27 a switch: `[audio] mode_start_music` (default true; false = retail silence). |
| Flare blocks rule | ON | hidden unless configured | This client injects flare tool 22 as a fake prefab tile. |
| VIP round intermission | none specific (generic 5 s) | 7 s | Unsourced server choice. |
| Demolition end hold | none | 3 s after the airstrike | Lets every client see the impacts. |
| Multi-Hill score limit | no retail rule | 100 | Server choice. |
| TC capture speed | not recovered | 0.05 of the bar per second per net occupant | Confirm against a retail capture if one ever turns up. |
| Melee kill type | `WEAPON_KILL` (`TOOLS_KILL_TYPE`) | `MELEE_KILL` | Same icon and death cam. Kept for melee scoring. |
| Auto-balance and zombie conversion kill type | `FORCED_TEAM_CHANGE_KILL` exists | `TEAM_CHANGE_KILL` | Looks identical on screen. |
| Domination threshold | not recoverable | 4 | Convention. |
| Round-result `LocalisedMessage` (`TEAM_DEFEAT` / `GAME_DRAWN`) | not recorded | sent at the win | The only way a result shows on an in-place restart. |
| Friendly fire | InitialInfo flag only | off | Believed retail, not verified. |
| Maximum players code default | lobby 2-24 | 50 (the shipped value is 24) | Only applies without TOML. |
| Inferred values | - | crate drop altitude and fly-by choice, goo timers, jetpack damage field, explosion class multiplier, countdown schedule, TC "left" count, parachute rule | Each is marked **inferred** in its doc. |

---

## 21. Remaining known issues

- **Memory step in the soak.** The RSS floor rose ~40 MB once (90 to
  130-150 MB) and then plateaued. Python objects stayed flat, and both VXL
  parsing and the refuge atlas are ruled out. Run a 6-hour soak before
  calling the 512 MB hosts safe.
- **Lag compensation is untested under real ping.** Only unit tests and local
  measurements exist. Watch the `lag_comp_*_snapshot` reports (log-only),
  because ENet throttling can make honest clients trip the stale-snapshot
  report.
- **Log-only anticheat checks have not been validated with real clients
  under loss.** Bots never exercise these checks. Leave the `enforce_*`
  switches off until real-client logs are reviewed.
- **Zombie smoke: pursuit.** The zombie pursuit in the Zombie bot smoke run
  is still an open item. Survivors are safe on refuges, but zombies stay in
  `route_breach` and cannot climb (BOT_ZOMBIE_REFUGE).
- **DoubleDragon water test.** The navigation scenario in
  `test_bot_voxel_navigation.py` lets native bodies touch the DoubleDragon
  water plane and bounds recovery at under 6 s. It is platform-sensitive
  (ARM/Windows/Linux trajectories differ) and is still being watched.
- **Windows Steam bridge bot count.** Reported open: the bot count that the
  Windows Steam bridge advertises. The shared `ServerPopulation` count is the
  intended single source; confirm on the Windows fleet host.
- **Jump-pack residual.** There is still one ~0.26-block forward nudge per
  burn in the delay-3 phase. Removing it needs a bounded 1-3 frame
  authoritative rewind.
- **Lost button edges.** At 0.5% loss a lost press or release still causes a
  few 0.1-0.4 block corrections per minute. Removing that needs reliable
  ClientData plus burst consumption.
- **ENet head-of-line freeze.** Busy play can still stall remote players
  behind a lost reliable event packet. Fixing it needs a second channel,
  which the stock client does not read.
- **Parachute.** Transitions ride the ordinary owner cadence and are not on
  the urgent path. Bots never deploy a canopy.
- **Zombie rampart ring has no door.** Survivors dig out when the refuge is
  breached.
- **Dev machine instability.** Intermittent access violations in pytest and
  `cl.exe` point at RAM or XMP rather than our code (POLISH_BACKLOG, section E).

---

## 22. Config keys added

`config.toml` documents every key. `tests/test_config_keys.py` pins that each
`getattr`-read key is registered in `server/config.py` and that the defaults
agree.

| Section | Key(s) | Default |
|---|---|---|
| `[network]` | `map_air_catchup_enabled` | `false` |
| | `require_protocol_version` (2026-09-28) | true |
| | `unreliable_throttle_deceleration` | 0 |
| | `prefab_competitive_cell_budget` | 2048 |
| | `prefab_health_state_batch` | 128 |
| | `lag_compensation_enabled` / `_max_ms` / `_extra_ms` / `_view_delay_ms` | true / 250 / 50 / 0 |
| `[game]` | `radar_station_lifetime_seconds` | 45.0 (was 35) |
| `[lobby]` | `end_round_scoreboard`, `end_round_headline` | true, true |
| | `votekick_cooldown_seconds`, `votekick_cancelled_cooldown_seconds`, `votekick_min_team_players` (2026-09-27) | 300, 45, 3 |
| | `map_vote_size_fit`, `map_vote_area_per_player`, `map_vote_min_area` | true, 8000, 24000 |
| | `map_vote_recent_exclude`, `map_vote_bot_weight`, `map_size_overrides` | 2, 1.0, `{}` |
| | `map_rotation_shuffle`, `map_vote_retail_max_players` (2026-09-28) | true, true |
| `[bots]` | `skill_balance`, `skill_balance_max_shift`, `_rate`, `_deadband`, `_min_events` | true, 0.35, 0.03, 0.15, 6 |
| `[teams]` | `balance_mid_match`, `balance_grace_seconds`, `balance_player_cooldown` | true, 5.0, 600 |
| | `balance_bot_wait_seconds`, `balance_check_interval` | 10.0, 1.0 |
| `[objectives]` (new) | `escape_watch_enabled`, `escape_watch_interval` | true, 1.0 |
| | `escape_watch_sky_seconds`, `_embedded_seconds`, `_entomb_seconds` | 5, 3, 5 |
| | `objective_entomb_seconds`, `objective_pickup_requires_los` | 5, true |
| | `objective_pickup_ends_spawn_protection`, `objective_afk_seconds`, `ctf_base_pit_depth` | true, 60, 24 |
| `[anticheat]` (new) | `enforce_shot_origin` / `shot_origin_tolerance` | false / 1.5 |
| | `enforce_aim_direction` / `aim_direction_tolerance_deg` | false / 10.0 |
| | `enforce_input_starvation`, `starvation_airborne_ticks`, `starvation_timeout_seconds` | false, 24, 8.0 |
| | `enforce_input_backlog` / `backlog_max_frames` | false / 6 |
| | `kick_on_protocol_violation`, `admin_login_attempts`, `summary_interval_seconds` | true, 3, 60 |
| | `report_enabled`, `report_path`, `report_max_bytes`, `report_backups`, `flag_min_score` | true, `logs/anticheat.jsonl`, 5000000, 3, 1.0 |
| | report thresholds: `headshot_*`, `accuracy_*`, `snap_*`, `reaction_*`, `engage_cone_deg`, `pellet_seed_*`, `sustained_*` | see `config.toml` and ADMIN_GUIDE |
| `[conduct]` (new) | `grief_kick_enabled`, `grief_kick_points`, `grief_warn_points` | true, 10, 5 |
| | `grief_decay_seconds`, `grief_team_kill_points`, `grief_team_damage_points` | 60, 3, 1 |
| | `grief_incident_seconds`, `grief_incident_max_points`, `grief_exempt_admins` | 3, 6, true |
| | `afk_kick_seconds`, `afk_warn_seconds`, `afk_spectator_kick_seconds` | 600, 540, 1800 |
| | `afk_exempt_admins`, `announce_kicks`, `reserved_names` | true, true, `[]` |
| `[audio]` (new, 2026-09-27) | `mode_start_music` | true (the deviation; false = retail) |
| `[debug]` | `input_gap_fill_limit` | 8 (0 disables, capped at 9) |
| | `jetpack_activation_defer_frames`, `jetpack_exhaustion_tail_frames` | 2, 3 |
| `[modes.tc]` / `[modes.mh]` | `capture_point_resupply` | true |

**Behaviour changes to existing keys:**

- `[modes.<alias>]` tables such as `[modes.zombie]` now apply to the short
  code. `[modes.zom]` wins when both exist.
- An unknown `default_mode` stops the server at startup.
- `[admin] password` disables `/admin` while it is weak; the environment
  variable is `BATTLESPADES_ADMIN_PASSWORD`.
- (2026-09-28) `[network] timeout_ms` / `bandwidth_limit` and `[world]
  water_level` / `water_damage` were never read; they are now documented as
  ignored, warn when set, and left `config.toml`. An empty `lobby.map_rotation`
  now means the mode's filtered retail playlist, not every VXL.

---

## 23. How to verify

### Tests per area

Run with `py -3.12 -m pytest <files> -q`. After pulling `aoslib/vxl.pyx`
changes, first rebuild with `py -3.12 setup.py build_ext --inplace`, with the
server stopped.

| Area | Test files |
|---|---|
| Netcode | `test_movement_jitter`, `test_simulation_order`, `test_reversed_world_update`, `test_lag_compensation`, `test_drill_rejoin_air_catchup`, `test_join_mutation_catchup` |
| VXL / map | `test_vxl_column_fill`, `test_vxl_sync_stress`, `test_vxl_markers`, `test_map_load_stall`, `test_grave_blast_cost`, `test_collapse_parity`, `test_map_metadata_retail`, `test_map_metadata` |
| Modes | `test_mode_lifecycle_contracts`, `test_mode_fixes_round2_{lifecycle,modes,scoring}`, `test_zombie_rounds`, `test_zombie`, `test_vip_sudden_death_rules`, `test_retail_rules_modes`, `test_ctf_end_and_departure`, `test_territory_control`, `test_multi_hill`, `test_occupation_fixes`, `test_demolition_fixes`, `test_diamond_mine_fixes`, `test_arena`, `test_airstrike`, `test_tdm` |
| Scoring | `test_score_events`, `test_commendations`, `test_round_awards`, `test_combat_scores` |
| Announcements | `test_retail_announcements`, `test_retail_end_screen`, `test_votekick_retail_format` |
| Anticheat / conduct | `test_anticheat_combat`, `test_anticheat_movement`, `test_anticheat_commands`, `test_anticheat_report`, `test_input_validation_combat`, `test_conduct`, `test_player_names`, `test_packet_delivery_fixes` |
| Packets / HUD | `test_hud_packets`, `test_block_mode_events` |
| Blocks / prefabs / colours | `test_block_health_model`, `test_block_darkening`, `test_prefab_block_health`, `test_prefab_instant_replication`, `test_colour_consistency`, `test_construction_actions` |
| Spawns / objectives | `test_smart_spawns`, `test_spawn_lock`, `test_retail_spawn_boxes`, `test_tdm_spawn`, `test_escape_watch`, `test_objective_abuse` |
| Weapons | `test_weapons_retail`, `test_weapon_catalog`, `test_retail_ammo`, `test_chemical_goo`, `test_fire`, `test_projectiles`, `test_machine_gun` |
| Kill feed / crates / classes | `test_killfeed_retail`, `test_crates_retail`, `test_radar_retail`, `test_retail_intel_radar`, `test_class_abilities_retail` |
| Sounds / spectators / parachute | `test_event_sounds`, `test_spectator_retail`, `test_parachute`, `test_client_session_transition_patch` |
| Bots | `test_zombie_refuge`, `test_zombie_rampart`, `test_bot_mode_fixes`, `test_bot_jetpack_climb`, `test_bot_skill_balance`, `test_bot_perception_budget`, `test_bot_dig_rates`, `test_bot_built_block_breach`, `test_bot_worker_safety`, `test_bot_policies`, `test_bot_project_sites` |
| Balance / vote / counts | `test_team_balance`, `test_map_vote_fit`, `test_player_count`, `test_scoreboard_retail_ping`, `test_rollover_team_select` |
| Config / values | `test_config_keys`, `test_retail_values` |

### Live-test recipes

These live-test recipes are the ones the evidence docs used.

- **Jump and movement gate.** Details in RETAIL_JUMP_RESTORE and RUNBOOK.
  1. Patch the client:
     `aceofspades_revival/tools/patch_character_jump_restore.py --check`.
  2. Run `scripts/scenarios/movement_stress.py --launch` with segments
     `settle,sprint,jump_in_place,jump_run,turn_left`.
  3. Pass condition: 0 ADJUST and 0 SNAP.
- **Internet ping.**
  1. Start the proxy:
     `py scripts\udp_lag_proxy.py --listen 27018 --target 127.0.0.1:27016 --delay-ms 40 --jitter-ms 8 --loss 0.005`.
  2. Point `movement_stress.py` at port 27018.
  3. Add `--sniff` to histogram the ENet command types.
- **Jump-pack boundaries.**
  1. Run `--class-id 2 --segments settle,rocketeer_jump_pack_hold --client-frame-capture`.
  2. Point it at a server started with `--debug-selfrow --debug-parity`.
- **Late-join terrain.**
  1. Start `scripts/run_validation_server.py` with the dig plugin.
  2. Compare the client's `map.get_solid` against the server
     (MAP_SYNC_JOIN).
- **Joins and crashes.**
  1. Start the server with `scripts/run_validation_server.py`, then join with
     `scripts/auto_join.py`.
  2. For a two-client rig, set `PHYSICS_TRACER_CONSOLE_PORT` and pass
     `--console-port`.
  3. Never `tuple()` a glm Vector3 in the console.
- **Stock-client checks.** Use the Steam `aos.exe`; the patched dev client
  masks some bugs.
- **Bot smoke runs.**
  - Zombie:
    `py -3.12 scripts/bot_runtime_smoke.py --mode zom --map ArcticBase --bots 10 --seconds 150 --full-runtime --seed 7`
    (and `scripts/bot_zombie_smoke.py`).
  - Motion and fun: `scripts/bot_motion_review.py` and
    `scripts/bot_fun_review.py`.

### Soak harness

Details in [SOAK_2026-09-26.md](SOAK_2026-09-26.md), "Re-running".

```
py -3.12 scripts/soak/soak_server.py --minutes 90 --sweep --out logs/soak/<run>
py -3.12 scripts/soak/soak_client.py --server 127.0.0.1:27020 --hold-minutes 12 --rejoin --out logs/soak/<run>
py -3.12 scripts/soak/soak_server.py --minutes 16 --round-seconds 1500 --modes tdm --bots 6 --start-map London --out logs/soak/<afk>
py -3.12 scripts/soak/soak_server.py ... --profile-entities --profile-stalls
py -3.12 scripts/soak/analyze_soak.py logs/soak/<run> --md logs/soak/<run>/report.md
```

### Map metadata tooling

```
py -3.12 tools/map_metadata/extract_retail.py --check
py -3.12 tools/map_metadata/coverage.py
```

---

## 24. Late additions (2026-09-27)

These fixes landed after the sections above were written.

| Area | What changed | Why (evidence) | Key files | Tests / doc |
|---|---|---|---|---|
| Chemical Bomb | Replaced the single 50-damage blast and 27-block crater with retail goo. Every exposed solid block within radius 3 gets a goo patch (entity 31, max 48 per bomb) that lasts 4 s. The patch dissolves its block with type-43 damage (0.7 every 0.4 s) and drops onto the block below when that block goes. A player touching goo takes 2.5 HP every 0.3 s, is killed with CHEMICALBOMB_KILL and gets the burn flash. Throw speed is now 50, up from 40. | IDA on stock `gameScene.pyd`/`character.pyd`: `BlockGooEntity` is a copy of `BlockFireEntity`; the explosion manager has no chem-bomb handler; the unread `CHEMICALBOMB_EXPLOSION_RADIUS` list is the goo footprint. **Inferred:** goo timings reuse the block-fire lifespan and damage. | `server/chemical_goo.py` (new), `server/projectiles.py`, `server/simulation_runtime.py`, `server/round_lifecycle.py`, `server/player.py` | `tests/test_chemical_goo.py`; WEAPONS_RETAIL.md "Molotov fire and Chemical Bomb goo" |
| WorldUpdate bit 0x08 | Now set only while a player touches goo. It used to be set when wading, so every wading player played the chem-burn loop for everyone watching. | Stock client: 0x08 = `set_touching_goo`. | `server/player.py`, `server/chemical_goo.py` | `tests/test_reversed_world_update.py` |
| Molotov | Teammates are spared when friendly fire is off. Ignite range is measured to the nearest point of the body. Fire falls when its block disappears. Late joiners get the remaining fuse. | Stock `BLOCKFIRE_*` constants and the handler (50 damage, radius 4). | `server/fire.py` | `tests/test_fire.py` |
| Explosion spikes | The native collapse walk no longer releases the GIL per step. That cost about 15.6 ms per switch against the bot thread, so explosions took 15–340 ms and now take 0.3–8 ms. The explosion and kill modules are now loaded at startup. | Measured under a competing CPU-bound thread. | `aoslib/vxl.pyx` (rebuilt), `server/main.py` | `tests/test_collapse_parity.py`, `tests/test_projectiles.py` |
| End screen | The server now sends ForceShowScores(0) after a silent flush, right before ShowGameStats(53). Without it, 72(1) kept the client locked to ViewScores and the stats screen, "X WINS!" headline and stats music never showed. New order: 72(1) → 5 s → flush, 72(0), 53, 73 → 12 s → flush, 52. | Framebuffer screenshots from the live client; `manager.locked_to_scene`. | `server/match/__init__.py` | `tests/test_retail_end_screen.py`; ANNOUNCEMENTS_RETAIL_2026-09-24.md correction |
| Rollover freeze | Stock client bug: `Sound.close()` destroys a playing ALURE stream while an OpenAL error is pending. The stream is left orphaned and the next `alurePlaySource` never returns, which froze START on team select after a map change. Client fix: `install_audio_guard` in the session transition patch clears the error before every stream call. Server fix: a silent volume-0 PlaySound before 53 and before 52. | Reproduced deliberately in the console with the identical stack. | `client_patches/session_transition_patch.py`, `server/match/__init__.py` | `tests/test_client_session_transition_patch.py`; SOUNDS_RETAIL.md |
| Join/ending music | Every music switch and ambience registration is preceded by the error flush. The joiner's music and ambience now go out before the terrain catch-up burst, which could use up all 128 client sound sources. A joiner in the final minute gets the ending track. The 0:00 ending cue no longer restarts over a final-minute track that is already playing. | Client log: "Could not load sound … Existing OpenAL error", 191 × "Error starting source". | `server/audio.py`, `server/main.py`, `server/connection.py`, `modes/base_mode.py` | `tests/test_join_mutation_catchup.py`, `tests/test_audio_voting.py` |
| TC join crash | TerritoryBaseState(106) now uses the 3-byte short form for actions 3, 4, 6 and 7 (ENTERING, LEAVING, CONTENDED, UNCONTENDED). The 4 extra bytes of the 7-byte form were read as an EntityUpdates(3) packet, so the client died with NoDataLeft. | Retail `TerritoryBaseState.read/write` (packet.pyd 0x10082B20/0x100830D0) skips the detail for `TC_DETAIL_NOT_REQUIRED` = [3,4,6,7]. | `shared/packet.pyx` (rebuilt) | `tests/test_recovered_objective_packets.py`; PROTOCOL.md |
| Engineer constructs | Engineer offers the stock 7 constructs (caltrop, supertower, ultrabarrier, platform, superminibunker, superdome, fort_wall). Super Bridge and Super Pole are gone from its list, and SetClassLoadout validation, the build allow-list and bot prefab picks follow because they all read `PREFAB_LISTS`. The other 17 classes were re-checked slot by slot and already matched. | Stock alias A475 (`aceofspades_source/shared/backup/constants-copy.py:935`); the renamed decompile has 9 (alias trap). Audit: class_data_check (18 classes x every slot). | `shared/constants.py` | `tests/test_class_selection.py`; CRATES_CLASSES_RETAIL.md section 4 |
| Dynamite wallet | Miner dynamite is now max 1 / initial 1 / +1 per ammo crate, down from max 3 / +3. | Stock `DynamiteWeapon.ammo = (A1627, A1628, None, None, A1629)`, `A1627 = 1` (`constants-copy.py:2601-2603`); 3 was the nonsteam MOD block. | `shared/constants.py` (STOCK RESTORE) | `tests/test_deployable_inventory.py`; WEAPONS_RETAIL.md, CRATES_CLASSES_RETAIL.md |
| Class lists per mode | TDM and the Zombie survivors offer the stock six classes in stock card order (Soldier, Scout, Engineer, Miner, Specialist, Medic), without Rocketeer. Bots pick from the same six, filtered by operator class rules and the mode list. | `DEFAULT_TEAM_CLASSES` (alias A93); the retail lobby lists those six (`gameRulesPanel.py:181-188`). | `server/mode_data.py`, `modes/zombie.py`, `server/bot_ai/director.py` | `tests/test_mode_data.py`, `tests/test_zombie.py` |
| Vote-kick denials | A refused kick now sends the retail string to the starter: `KICK_DENIED_FOR_SPECTATOR`, `_REASON_SELF_KICK`, `_REASON_KICK_HOST` (Map Creator host), `_REASON_VOTE_IN_PROGRESS`, `_REASON_VOTE_TOO_SOON` (seconds left), `KICK_NOT_ENOUGH_PLAYERS` (fewer than 3 on the starter's team). Refusals used to be silent. Cooldowns are 300 s between a starter's kick votes and 45 s after a cancelled one (was 60 s); both are configurable. | IDA on stock hud.pyd: `KickVotePlayerSelect.packet_received` closes the menu 0.5 s after these ids, and `GameScene.initiate_kick` validates nothing. Constants `MIN_TIME_BETWEEN_KICK_VOTES = 5*60`, `MIN_TIME_BETWEEN_CANCELLED_KICK_VOTES = 45` (C:5269-5273). **Inferred:** the check order, the per-starter scope and the team counted for the 3-player rule. | `server/voting.py`, `server/config.py` | `tests/test_votekick_retail_format.py`; ADMIN_GUIDE `[lobby]` |
| Mode start cues | TDM, TC, MH, Diamond and Occupation now send their start cue to every in-game player at every round start, in-place restarts included; before, only joiners got it. Zombie sends `ZOMBIE_START_SURVIVOR` when the outbreak clock arms (with `ZOMBIE_VIRUS_RELEASED` queued behind it) and `ZOMBIE_START_*` by team to joiners. | These strings are referenced by no client binary, so they are server-sent. **Inferred:** the timing. | `modes/base_mode.py` (`start_cue_for` / `broadcast_start_cue`), mode files | `tests/test_parity_late_additions.py`; ANNOUNCEMENTS_RETAIL_2026-09-24.md "Late additions" |
| Unsent retail lines | `BASE_DEPLETED` is sent when a Multi-Hill hill times out (airstrike). `BASE_OCCUPIED_ATTACK`/`_DEFEND` are sent when a Blue bomb carrier enters the Occupation target (at most every 5 s). `DIAMOND_BASE` is sent when a new drop-off opens mid-round. | Strings EN:149/329/330/345 exist, and no client code sends them. **Inferred:** the triggers. | `modes/multi_hill.py`, `modes/occupation.py`, `modes/diamond_mine.py` | `tests/test_parity_late_additions.py` |
| Diamond Mine map vote | The ballot opens when a team reaches 12 of 15 diamonds, keeping that 3-diamond lead for other targets. The before-the-limit ballot (10 s since the rules audit, was 60 s) remains as a fallback. | `DIA_DIAMONDS_TO_TRIGGER_MAP_VOTE = DIA_DIAMONDS_TO_GET_FOR_MAP_ROTATION - 3`. | `modes/diamond_mine.py` | `tests/test_parity_late_additions.py` |
| Demolition build clock | The 30 s build phase now drives DisplayCountdown(84); the round clock follows when the bases unlock. | No retail string exists for the build phase. **Inferred:** a timer is the only possible cue. | `modes/demolition.py` | `tests/test_parity_late_additions.py` |
| HeadCount | Zombie StateData now uses `team_headcount_type` 0 (team player counts: zombies vs survivors) instead of 6 (hidden scores). Other modes keep 6. | Retail enum 0-3 (`TEAM_PLAYERS_COUNT_VALUE`..`TEAM_SCORE_INACTIVE`); the per-mode value is server-side and unrecovered. **VERIFY** with a retail capture. | `modes/zombie.py`, `server/builders/state_data.py` | `tests/test_parity_late_additions.py` |
| In-round music switch | The `last_man_standing` bed at round start and join is now `[audio] mode_start_music` (default true, keeping the requested music). With false, round starts only send StopMusic, as in retail. | Retail had no in-round bed (`INGAME_MUSIC` = last-man/ending/tutorial). The deviation is kept on the owner's request. | `server/audio.py`, `server/main.py`, `modes/base_mode.py`, `config.toml` | `tests/test_parity_late_additions.py`, `tests/test_config_keys.py`; SOUNDS_RETAIL.md, ADMIN_GUIDE `[audio]` |
| Full-server refusal | A full server now refuses a joiner with `DISCONNECT.ERROR_FULL` (4). f3518e2 had sent `disconnect(reason=3)`, which the stock client shows as `ERROR_SERVER_OUT_OF_DATE`; Beta.1 hosts still send 3 until they are updated. | Stock `DISCONNECT` enum (`shared/constants.py`); the server never has a reason to send 3. | `server/connection.py` | `tests/test_bot_human_slots.py::test_full_server_refuses_with_retail_error_full_not_out_of_date`; PROTOCOL.md (Transport) |
| Vote kick with bots | Bots no longer count toward the 3-player team minimum. A human alone with bot teammates now gets `KICK_NOT_ENOUGH_PLAYERS` (and the kick menu closes) instead of silence when picking a bot. | Bots cannot vote; retail had no bots. Seen live 2026-09-27 on the native client. | `server/voting.py` | `tests/test_votekick_retail_format.py::test_bots_do_not_make_a_team_big_enough_to_kick` |
| Forced-winner headline | When the round winner is not the team with the higher score (an admin or plugin end), ShowTextMessage(73) now carries `VIP_TEAM1/2_WIN_MESSAGE` (6/7, the same "{0} wins!" text) instead of `TEAM_SCORES_MESSAGE` (1). Id 1 makes the stock client name the score leader, so a forced Blue win over a leading Green used to read "GREEN WINS!". Normal ends still send 1. | ViewGameStats/ViewScores.set_message (hud.pyd 0x1006ED20): id 1 computes the winner from team scores, 6/7 name team1/team2. | `modes/base_mode.py` (`end_message_id`) | `tests/test_retail_end_screen.py::test_score_headline_names_a_forced_winner_explicitly` |
| TC capture bar | TerritoryBaseState(106) `capture_amount` is now the retail 0..100 percentage of the way to the next ownership change, with `attacked_by` the team the progress leans toward. It used to carry the internal 0..1 position (0 Blue, 0.5 neutral, 1 Green), so the stock HUD's `capture_amount / 100` plate never visibly filled. | TerritoryBasesHud stretches the attacker plate to `capture_amount/100` (HUD_RECOVERY 3.1). Verified live against the retail client on CityOfChicago: C bar fills in both clients. | `modes/territory_control.py` (`_wire_capture`) | `tests/test_territory_control.py::test_capture_amount_is_the_retail_attacker_percentage`, `::test_capture_update_packets_carry_a_growing_percentage` |

### Rules audit fixes (2026-09-27)

From the gameplay-rules audit. "Server-only" constants are stock constants no
stock client pyc or pyd reads, so only the retail server used them.

| Area | What changed | Why (evidence) | Key files | Tests / doc |
|---|---|---|---|---|
| Ammo crate on oriented tools | A crate TOPS UP grenades (to 4), sticky/chem (+2, max 4), molotov, RPG/RPG2/drill/GL/ML reserves (to max) and keeps the loaded clip and cadence. It used to reset everything to the spawn values, so the client showed 4 grenades and throws 3-4 were refused ghosts. | Stock `Tool.restock` / `Weapon.restock(AMMO_CRATE)` and the per-class tuples (stock_weapons dump); the native client already predicts the top-up. | `server/player.py` (`ORIENTED_STOCK_AMMO`, `_restock_oriented_crate`) | `tests/test_rules_audit_2026_09_27.py`; WEAPONS_RETAIL.md "Oriented tools" |
| RPG2 per life / reload | 6 rockets per life (3 + 3), not 9; one rocket loads per 1.0 s reload cycle. | Stock `RPG2Weapon.ammo = (3,3,3,3,3)`, `clip_reload = True`; `RPG2_AMMO_MAX = 6` is the mod tail. | `server/player.py` | same |
| RPG2 self-knockback | The thrower gets the ordinary 0/0.25 push, not 1.0/1.5. | Stock `handle_rocket2_damage` passes 0/0.25; the SELF_KNOCKBACK pair is mod-tail only. | `server/projectiles.py` | same; `tests/test_projectiles.py` |
| Blast push timing | Every blast whose terrain Damage(37) type is in the stock explosion manager's `damage_functions` table now applies its push on the 3rd accepted input frame (the verified Snowball timing), and that packet is sent even when no solid cell is hit. | Oracle run of the stock `explosionDamageManager.pyd`: `handle_damage` dispatches on the packet type alone, so stock clients push themselves on that packet. **Inferred:** the Snowball frame delay holds for every type. | `server/main.py` (`STOCK_EXPLOSION_DAMAGE_TYPES`) | same; PROTOCOL.md "Snowball Damage/Destroy ordering" |
| TC capture | Stock A2550 table `[(0,0),(1,1),(5,4),(10,7),(15,9)]` = capture % per 0.5 s tick by capturing players (1 player: 50 s per step); a contested base is frozen; Claim/Control pays every capturer (Multi-Hill: every claimer, and a contested hill is held). The constant is restored in `constants_gamemode`. | Stock constant dump. **Inferred:** per-tick %, interpolation, freeze, all-occupant pay. | `modes/territory_control.py`, `modes/multi_hill.py`, `shared/constants_gamemode.py` | same; `tests/test_territory_control.py`, `tests/test_retail_rules_modes.py` |
| Fall damage rules | `RULE_ENABLE_FALL_ON_WATER_DAMAGE` no longer switches off land falls; x0.2 on a landing after an own RPG/RPG2 self-push; damage scaled 0..1 over 1/60..4/60 s of air; a lethal fall credits the enemy who hit the victim within 5 s (world damage no longer erases that record). Assists use the same 5 s window (was 10). | Server-only `ROCKET_JUMP_FALL_DAMAGE_MULTIPLIER`, `ZERO/MAX_FALL_DAMAGE_AIR_TIME`, `PLAYER_INTERACTION_EXPIRY_SECONDS`. **Inferred:** triggers. | `server/config.py`, `server/player.py`, `server/main.py`, `server/combat_scores.py`, `server/handlers/team.py` | same |
| No respawns | KillAction carries `NEVER_RESPAWN_TIME` 255 for a dying VIP and every sudden-death-team member (joiners included), so the stock HUD shows "No respawns!". | Stock hud `never_respawn`; `NEVER_RESPAWN = 'No respawns!'`. | `modes/vip.py` | same; `tests/test_vip.py` |
| VIP rounds | `RULE_VIP_NOOF_ROUNDS` counts sub-rounds PLAYED (winner by rounds won, tie = draw), like Zombie's twin rule. | `VIP_NOOF_ROUNDS_BEFORE_NEXT_MAP`. **Inferred;** VERIFY with a retail capture. | `modes/vip.py` | same |
| Disguise | Walking/jumping input, 0.5+ block of horizontal drift, a block build/line or a deployable placement ends it. | "- Must remain stationary"; the stock client only sends the activation. **Inferred:** thresholds. | `server/player.py`, `server/handlers/blocks.py`, `server/handlers/deployables.py`, `server/deployable_actions.py` | same |
| Kill events | CTF: grabbing an intel from its home pays "First to Claim Flag" 100; a touch-return's +1 is unlabelled (it wrongly said "First to Claim Flag"). TDM: Reloading Kill, Defend and Distraction (50 each). Demolition: Defend Base 100 / Assault Base 50 (20-block threat radius). | Constants/reasons in `constants_gamemode`; the commendation table groups Distract with Assist. **Inferred:** triggers. | `modes/ctf.py`, `modes/base_mode.py`, `modes/tdm.py`, `modes/demolition.py` | same; `tests/test_ctf_entities.py` |
| Build rules | Block builds/lines closer than `MIN_BLOCK_INTERVAL` 0.1 s are counted (`[anticheat] enforce_block_interval`, default log-only; bots pace themselves). Classic CTF build/paint reach is 5 (+ slack), not 10. | Server-only A1016; A1012 in `BlockToolCommon`. | `server/combat_runtime.py`, `server/bot_ai/gateway.py`, `server/config.py` | same; ADMIN_GUIDE `[anticheat]` |
| Damage rules | `RULE_ONE_HIT_KILL` only for `ONE_HIT_KILL_WEAPONS` kill types; hit damage rounded once; the Snowblower honours TeamInfiniteBlocks; a deployable placement ends spawn protection. | Server-only A2382; stock `SnowBlowerWeapon.get_has_enough_ammo`. | `server/player.py`, `server/combat_runtime.py`, `server/handlers/deployables.py` | same |
| Small constants | CTF base box +0.5 (`BASE_ZONE_DISTANCE_TOLERANCE`); the map ballot opens 10 s before the clock ends and runs 10 s (`TIME_AFTER_MAP_VOTE_START_BEFORE_END`, was 60/15; the Diamond Mine score trigger is unchanged). | Server-only constants; name reading. | `modes/ctf.py`, `server/voting.py`, `modes/base_mode.py` | same; `tests/test_ctf_entities.py` |
| Diamond fuse | A ground diamond's CreateEntity(21) now carries the diamond lifetime (`RULE_DIAMOND_LIFETIME`, default 60 s) as its fuse, and the mode keeps the entity's fuse at the remaining lifetime every tick so a late joiner's replay counts down from the live value. It used to be 0, so the native client's new 3D diamond label showed "0" for the whole lifetime. | Retail sends the lifetime as the packet fuse (native client doc, Round 2 E7; `Entity.update_3dText` ceil-formats the fuse). Same pattern as the radar station lifetime. | `modes/diamond_mine.py` (`_spawn_diamond`, `on_tick`) | `tests/test_diamond_mine_fixes.py::test_diamond_create_entity_carries_lifetime_as_fuse` |
| Left unchanged | Burn/goo ticks skip the class multiplier (no evidence); `RECENT_KILLS_EXPIRY_SECONDS` sits in the achievement block, not domination; `MAX_CHAT_SIZE` 90 is a server rule of unrecovered meaning (the code comment calling it a wrap width was wrong); `CAPTURE_POINT_DISTANCE` 3.0 needs the capture-point entity (TC flag or Classic tent?); `HEALTHCRATE_HP` (an enum slot), `HIT_TOLERANCE`, `MAX_DAMAGE`, `WEAPON_DAMAGE_MULTIPLIER_THRESHOLD`, `VIP_CORPSE_EXPLOSION_*`, Block Sucker A1986/A1987 and the UGC drill "destroyed" variant stay informational. | - | - | RETAIL_VALUES.md |

**Native client follow-ups from this pass:** predict the blast push on
Damage(37) for the `damage_functions` types (the server now applies it on the
3rd accepted frame); grenades/launchers top up on a crate (already
predicted); RPG2 is 6 per life and reloads one rocket per second; honour the
0.1 s block spacing (the stock tool's 0.5 s cadence already does) and the
Classic 5-block reach (already done); bridge placement, the wallet
multiplier and the block refusal hints are client-only audit items.

### Round 3 server fixes (2026-09-28)

From the round-3 server/world audit (`audit3/server.md`, `world.md`,
`network.md` L7/L10, `verify.md` V8). "Inferred" marks a policy the retail
binaries do not pin down.

| Area | What changed | Why (evidence) | Key files | Tests / doc |
|---|---|---|---|---|
| Classic / UGC block health | Every user block (build, line, prefab, BlockBuildColored) is stored at DEFAULT_BLOCK_HEALTH 5 in Classic CTF, and is untracked (map default, no packet 38 user row, no observer row) in the UGC Map Creator. It used to be 9 everywhere, so a classic built block took two spade hits instead of one. | Stock gameScene `BlockManager.add_user_block` (0x10070530): `if is_in_ugc_mode(): user_blocks.pop(key)`; `if is_in_classic_mode(): health = DEFAULT_BLOCK_HEALTH`. | `server/world_manager.py` (`user_block_health`), `server/combat_runtime.py` | `tests/test_user_block_mode_health.py` |
| Retail invalid pairs | GreatWall left the Demolition rotation and BranCastle the Occupation rotation. `retail_playlist_modes` is now the pairs retail actually served, and loading a map for a mode in its `invalid_modes` always warns. The earlier doc and test claimed retail served both pairs; that premise was wrong. | Retail `playlists.PlayList.__init__` skips `mode in map_data['invalid_modes']` even for a single-mode playlist; mapinfo marks GreatWall `dem` and BranCastle `oc` invalid. | `tools/map_metadata/extract_retail.py` (regenerated `maps/retail_map_info.json`), `server/map_metadata.py`, `configs/official-demolition.toml`, `configs/official-occupation.toml` | `tests/test_map_metadata_retail.py`; MAP_METADATA.md |
| Vote pool | With an empty `lobby.map_rotation` the vote uses the mode's retail playlist filtered like retail (invalid_modes, classic/mafia flag, release): TC/VIP = Alcatraz + CityOfChicago, Classic = the 7 classic maps, and no Training/classic/mafia/invalid maps in the other modes. An explicit rotation drops stock maps retail marks invalid for the mode (warning). | `playlists/__init__.py:36-45`. | `server/map_metadata.py` (`retail_mode_pool`), `server/voting.py` | `tests/test_map_vote_fit.py`; ADMIN_GUIDE `[lobby]` |
| Rotation shuffle | The map list is shuffled once at startup (`lobby.map_rotation_shuffle`, default true). | Retail `baseSquadLobbyMenu`: `random.shuffle(map_list)` at lobby creation. | `server/voting.py`, `server/config.py`, `config.toml` | `tests/test_map_vote_fit.py` |
| Per-map max players | A map whose mapinfo `max_players` (DragonIsland 16, London/LunarBase 20, BlockNess/SpookyMansion 24) is below the human count is offered after every map that fits (`lobby.map_vote_retail_max_players`). | mapinfo field; **inferred** use (the retail server-side consumer is lost). | `server/voting.py` | `tests/test_map_vote_fit.py` |
| GameStats awards | The three end-screen rows per team are a random sample of ALL 30 retail stat types someone on the team earned, listed in stat-id order; it used to be a fixed priority over 10 types (almost always Kills/Assists/Headshots). New truthful round counters: distance ran, time in air, time on fire, health/ammo/block crates, kills at <= 20 HP, blocks placed/destroyed, highest block, biggest collapse, longest ranged kill, snipers killed, airstrikes survived, damage taken, headshots received, kill steals, dominations/dominated, fewest shots fired (>= 3 kills). Bots count too. | 30 `GAME_STAT_TYPES` all rendered by the stock client; `NOOF_GAME_STATS_TO_SHOW` is server-only. **Inferred:** the random sample, the thresholds. | `server/scoreboard.py`, `server/combat_scores.py`, hooks in `server/player.py`, `server/combat_runtime.py`, `server/map_resources.py`, `server/simulation_runtime.py` | `tests/test_round_awards.py`, `tests/test_commendations.py` |
| PLAYER_LEFT | "{0} has disconnected" goes to every in-game player before PlayerLeft(64), for humans on a playable team; not for bots, spectators, loading peers, map-rollover reloads or shutdown. | EN:596 right after PLAYER_JOINED, referenced by no client binary. | `server/main.py` (`_announce_player_left`), `server/connection.py` | `tests/test_server_retail_lines.py` |
| Team-change refusals | Private LocalisedMessage for the ChangeTeam(77) menu path and `/team`: cooldown `TEAM_SWITCH_WAIT` (`/team` also gets the seconds), mode lock `TEAM_LOCKED` when the target is locked in StateData/LockTeam(79) else `TEAM_SWITCH_NOT_ALLOWED`, spectators off `TEAM_SWITCH_NOT_ALLOWED`, unbalancing `TEAM_FULL`. A joiner the join balance moved gets `TEAM_FULL` once in the GameScene. The menu path used to be silent and `/team` got English. | EN:116-122 server-sent block; `client=-` for all four ids. | `server/handlers/team.py`, `server/connection.py` | `tests/test_anticheat_commands.py`, `tests/test_server_retail_lines.py` |
| COUNTDOWN_FROM_TEN | "{0}!" for 9..1 after the 10 s cue, one per second; a missed second stays quiet. | EN:116 in the same block as COUNTDOWN_SECONDS/_MINUTES. **Inferred:** the 9..1 schedule. | `modes/base_mode.py` | `tests/test_retail_announcements.py` |
| Timed bans | A timed `/ban` and a reconnect during it use `ERROR_TEMP_BANNED` (19); permanent bans keep 1. | Retail DISCONNECT enum. | `commands/admin.py`, `server/main.py` | `tests/test_commands.py`, `tests/test_network_transport_parity.py` |
| `/kill` | Uses `CLASS_CHANGE_KILL`, not `TEAM_CHANGE_KILL`: the feed no longer shows the team-change icon and a dominated player can no longer wipe the domination icon / pending revenge. Still no death or suicide penalty. | Stock `process_packet_kill_action` clears the domination pair only on TEAM_CHANGE kills. | `commands/player.py` | `tests/test_anticheat_commands.py` |
| `/me` | Sends "* action"; the stock HUD already prefixes "Name: ". | `HUD.create_line`. | `commands/player.py` | `tests/test_server_retail_lines.py` |
| Team names in commands | Command replies resolve TEAM1_COLOR/TEAM2_COLOR/ZOMBIE_TEAM/SURVIVOR_TEAM to Blue/Green/Zombie/Survivor (`/score`, `/players`, `/lockscore`, ...). | Packet 49 shows ids raw. | `commands/command_handler.py`, `server/announcements.py` | `tests/test_server_retail_lines.py` |
| Admin `/balance` | Uses the auto-balancer's rules (bots first, only dead players, never carriers/VIPs/last survivors/mode-locked players, TEAM_FULL to each mover) and reports the count; it used to kill live players and ignore locks. | `server/team_balance.py` policy. | `commands/server_commands.py`, `server/team_balance.py` (`force_balance`) | `tests/test_commands.py` |
| PLAYER_JOINED names | An identifier-shaped name (every string-table key is one) gets one trailing space, so `localise_parameters` can still translate the team id without translating a player called "OK" or "MAP". | Stock `strings.get_by_id` = `globals()[id]`. | `server/connection.py`, `server/announcements.py` | `tests/test_server_retail_lines.py` |
| Server name | Capped at 31 characters on every wire (InitialInfo, A2S, masters) with a load-time warning; three official names shortened. | Server-only `MAX_SERVER_NAME_SIZE` = 31. | `server/config.py`, `configs/official-*.toml` | `tests/test_server_retail_lines.py` |
| Collapse budget | The flood gives up after 10,000,000 NODES (probe budget = 18 x that), not 10,000,000 probes (~555k nodes). | Retail vxl.pyd `sub_10036470` visited counter vs 0x989680. | `server/world_manager.py` | `tests/test_collapse_parity.py` |
| Fallback lighting | Maps without metadata get the lighting row MayanJungle and Trenches share byte-for-byte (the retail editor template), not the community server's (180,192,220) "London" preset. | Retail `.txtc` rows. | `server/builders/state_data.py` | `tests/test_server_retail_lines.py`; MAP_METADATA.md |
| Transport | The ClockSync reply is ENet UNSEQUENCED (retail sends the request unsequenced). ENet connect data other than 168 is refused with 10/3. `[network] timeout_ms`/`bandwidth_limit` and `[world] water_level`/`water_damage` are ignored with a warning and removed from `config.toml` (retail keeps ENet defaults; the water plane is fixed and has no damage). VoiceData(103) gets a bytes-safe runtime decoder (voice itself stays out of scope). ClientData needs no change for the native client going unsequenced: the queue is transport-agnostic (lost-label refill, stale/duplicate drop), exactly as for retail. | net 0x10008d50 `send_packet(packet, unreliable)`, `GameClient.__init__` connect data, net 0x100057d0 host 0/0. WorldUpdate stays sequenced-unreliable pending a udp_lag_proxy A/B. | `server/connection.py`, `server/main.py`, `server/config.py`, `protocol/runtime_packets.py` | `tests/test_network_transport_parity.py`; PROTOCOL.md, ADMIN_GUIDE `[network]` |
| AFK | Documented as non-retail: only `ERROR_AFK_TIMEOUT` is retail; the 540/600/1800 s times and warning are ours. | SRC constants hold no idle threshold. | `docs/ADMIN_GUIDE.md`, `config.toml` | - |
| Left unchanged | Server text stays on the CHAT_SYSTEM lane: which lane the retail server used for informational lines has no capture (VERIFY). PLAYER_JOINED/LEFT stay on the big lane for the same reason. | - | - | - |

**Native client follow-up:** Classic built blocks are 5 HP on the client too
(retail `add_user_block`), and the UGC editor keeps no user-block entry.

### Config and hosting fixes (2026-09-28)

Found while writing the server hosting guides. None of them changes retail
gameplay values.

| Area | What changed | Why (evidence) | Key files | Tests / doc |
|---|---|---|---|---|
| Docker state | The container entrypoint now puts `[revival] results_path` (pending AoSPlay round results) at `/data/state/round-results.sqlite3` and `[anticheat] report_path` at `/data/logs/anticheat.jsonl`, next to the bans and logs that were already there. A relative template path is rebased below `/data`, an absolute one is kept, and one that uses `..` is refused. Non-Docker launches are unchanged. | The hardened Compose example mounts `/app` read-only, so the results database failed to open and was lost with the container. | `scripts/container_entrypoint.py`, `deploy/docker-compose.example.yml` (a TDM example used the unshipped map `classicgen`) | `tests/test_container_entrypoint.py`; RUNBOOK "Container contract" |
| `[weapons]` keys | `rifle_damage`, `smg_damage`, `shotgun_damage`, `spade_damage` and `grenade_damage` are deprecated. They were parsed but never read. Setting any of them now logs a warning, and the table was removed from `config.toml`. They were not wired up because retail damage comes from per-weapon, per-body-part tables and one flat number would break parity. | No gameplay code read them; damage lives in `server/weapons_retail.py` and `shared/constants.py`. | `server/config.py`, `config.toml` | `tests/test_config_keys.py`; ADMIN_GUIDE `[weapons]` |
| `[world] map_size_*` | Now documented as informational. A value other than 512/512/240 logs a warning. | Every retail VXL is 512x512x240 and the loader never reads these keys. | `server/config.py`, `config.toml` | `tests/test_config_keys.py`; ADMIN_GUIDE `[world]` |
| Omitted-key defaults | A missing key now falls back to the shipped `config.toml` value: `port` 27015 (was 32887), `max_players` 24 (was 50), `default_mode` tdm (was ctf), `default_map` MayanJungle (was the unshipped `classicgen`). The fleet launcher's port fallback and the InitialInfo filename fallback follow. 32887 is still the documented opt-in for the unmodified Steam browser. Every official profile and fleet config sets these keys explicitly, so no running server changes. | The old defaults came from early development and disagreed with the sample config and the docs. | `server/config.py`, `server/fleet_launcher.py`, `server/builders/initial_info.py` | `tests/test_config_keys.py`, `tests/test_server_capacity.py` |
| Multi-Hill score limit | Multi-Hill no longer looks up the nonexistent `RULE_MH_SCORE_TARGET`. The target is `[modes.mh] score_limit`, with a default of 100 from `mode_data`. Retail has no Multi-Hill score rule: the Match Lobby `mh` rules are only `RULE_MULTIHILL_MAX_ACTIVE_BASES` and `RULE_BASE_ACTIVE_TIME`. Behaviour is unchanged, because the overlay was already applied before the failed lookup. | `GAME_RULES_NAMES["mh"]` (`shared/constants_matchmaking.py`); RETAIL_VALUES.md "Multi-Hill: Score limit, no retail rule". | `modes/multi_hill.py` | `tests/test_multi_hill.py` |
| `config.toml` wording | The comment above `[modes.mh]` … `[modes.oc]` no longer calls the five objective modes "skeletons". They have had full state machines since the mode audit. | GAMEPLAY.md and the mode sources. | `config.toml` | - |

### Round 5: Map Creator & tutorial (2026-09-29)

From `audit4/ugc_tutorial.md` and `tutorial_part.md` (headless idalib on the
stock gameScene.pyd, vxl.pyd and ugc_data.pyd). The native client half of this
round is recorded in the client's gap doc under "Round 5: Map Creator &
tutorial".

| Area | What changed | Why (evidence) | Key files | Tests / doc |
|---|---|---|---|---|
| UGC-1 stock markers | Every Game Data placement is also a real entity: CreateEntity(21) type `UGC_ENTITY` (29) at the raw voxel position, `state` = wire team from `UGC_ENTITY_TEAMS` (neutral otherwise), face 4, `ugc_mode` = the placement's mode id, `int_properties = [item_id]`. Removal sends DestroyEntity(19). Sent to every in-game peer through the entity registry's per-connection create/destroy ledger; joiners and ReqestUGCEntities(99) get them once from `reveal_to`. The native client ignores type 29 (it draws markers from 97/98). | gameScene `process_packet_create_entity` 0x10178b80 sets `mode_placed_in = packet.ugc_mode`; UGCEntity (0x100a2fd0) sets `ugc_item_id = packet.int_properties[0]` and picks `UGC_ENTITY_MODELS[ugc_item_id]`. gameScene has no receive handler for 97/98, so a stock editor saw no markers and could never replace or remove one. | `modes/ugc.py`, `server/entities/registry.py` (`MapEntity.ugc_mode`/`int_properties`, zero for every other entity) | `tests/test_ugc_round5.py` |
| UGC-5 marker modes | Crate drop points are `nor`, the Occupation bomb point is always `oc`, zone items take the current target mode. | `UGCEntity.get_ugc_mode` 0x100a7970; the draw loop 0x10151a20 shows only the edited mode and `nor`. | `server/ugc_project.py` `authored_mode_for_item` | same |
| UGC-12 removal tolerance | A removal takes the newest placement within distance² ≤ 1, preferring one visible in the edited mode, then the same item id. The 97 echo carries the REMOVED placement's stored position and item. | `weapons/ugcTool.py:171-221`: remove/replace send `ghost_position`, which may be up to 1.0 from the entity (`is_object_on_entity_of_class` radius 1.0). | `server/ugc_project.py` `remove`, `modes/ugc.py` `place_object` | same |
| UGC-11 top faces | A PlaceUGC placement needs a solid cell directly below the marker cell (removals are unchanged; reach stays 12). | `UGCTool.draw_ghosting` → `can_place_object(..., can_place_vertical=False)` (`ugcTool.py:223-229`, gameScene 0x101270b0) accepts top faces only. | `server/handlers/ugc.py` | same |
| UGC-7 skydome fog | Choosing a skydome that is a `FOG_COLORS` key also broadcasts FogColor(74) to every editor including the host and sets the live map fog (joiners' StateData). A hosted/published project's fog follows its `.ugc` skydome over the baseplate `.txt` `fog_color` pin. | `GameScene.set_skybox_name` 0x1012d4c0 sends SkyboxData(51) and, for FOG_COLORS names, FogColor(74) with `make_color(FOG_COLORS[name])`. | `modes/ugc.py` `set_skybox`, `server/map_metadata.py` | `tests/test_ugc_map_creator.py` |
| UGC-8 capacity | The Map Creator mirrors `is_space_to_add_blocks`: full only when solids ≥ 2,800,000 AND non-empty 16³ chunks ≥ 3,200. Counters are built once from the project VXL at mode start (about 0.4 s) and kept current from the mutation feed with a 240-bit mask per column. BlockBuild(32), BlockLine(40), BuildPrefabAction(30) and the snowblower's UseOrientedItem(10) are refused while full; removals are always allowed. Baseplates: Desert 2,820,321/1,137, Mountain 1,180,703/672, Water 262,144/1,024 (none is full). | vxl.pyd 0x10019c00 (`cmp [+0x3EB34CC], 2AB980h` and `cmp [+0x3EB34D0], 0C80h`), recount 0x10005600 over the 512×512×240 solid byte grid and 15,360 chunks, deltas in `set_point` 0x10029da0. gameScene 0x10075490 consults it only when `is_in_ugc_mode()`. | `server/ugc_capacity.py`, `modes/ugc.py`, `server/handlers/blocks.py`, `server/handlers/world.py` | `tests/test_ugc_round5.py` |
| UGC-9 paint cue | An accepted single-cell UGC paint plays PlaySound 47 (`PAINT_PRIMARY_SOUND`, `ugc_colour_singleblock_001-003`) at the cell centre, unreliable, at most once per editor per 0.1 s. The RMB surface spray (ClientData secondary held) plays nothing server-side. | `constants_audio.py:517` has no client-side caller in any retail .py or .pyd, so it is a server cue. | `modes/ugc.py` `on_single_paint`, `server/handlers/blocks.py`, `server/handlers/movement.py` | same |
| Save request | UGCMessage(100) `UGC_CONVERT_TO_GAME` (0) from the host now flushes the VXL and sidecar at once and answers only the requester with LocalisedMessage(50) `UGC_MAP_SAVE_SUCCESSFULLY`, or `UGC_MAP_SAVE_ERROR` on failure (chat type 3, no parameters, override 1). Requests coalesce; guests are ignored. `generate_vxl` runs on the gameplay thread (0 ms clean, about 160-200 ms after edits on a baseplate, once per explicit save); the writes are off-thread. | No shipped client sends CONVERT_TO_GAME, so the native F10 quick save and Esc SAVE use it without a new packet id. The strings are retail (`english.py:1872-1873`). | `server/handlers/ugc.py`, `modes/ugc.py` `request_save` | same |
| UGC-14 sidecar | New titles default to `Untitled UGC` and descriptions to `Undescribed UGC`. Every save rewrites `tags` as `["map", <mode code of every publishable target mode>]`. The last edited target mode is kept in a new `ugc_target_mode` key, because tags no longer name it. | `save_ugc_file` 0x10174e80; ugc_data.pyd `set_tags_from_supported_gamemodes` 0x1000e230 = `get_publishable_game_modes` mapped through `MODE_IDS_MODE` (A2451). | `server/ugc_project.py`, `server/ugc_launcher.py` | same |
| T5 climb | CLIMB completes on the lane's tower dome: horizontal distance ≤ 9.5 from local (118.5, 51.5) and player z ≤ 200. It used to complete after two built blocks anywhere. | CLIMB1 "Dig and build your way to the top of the tower!". Training.vxl: the dome top is z 193 and z ≤ 197 within r 8.5, with a ledge ring at z 207 (the same in all 12 lanes). **Reconstructed.** | `modes/tutorial.py` | `tests/test_tutorial.py` |
| T7 targets | The first damage to a live target's red cell (a `block_damage` entry, or a destroyed red cell) removes the whole 21-voxel disc with checked kill Damage(37) per cell and counts the target at once, matching the native offline tutorial. | The Training server was never shipped; one rule is now shared by both implementations. **Reconstructed.** | `modes/tutorial.py` | same |
| T6 rules | Only `enable_colour_picker = 0` is overridden (the palette stays on for the climb). Minimap, death cam, spectators and fall-on-water keep their defaults; the launcher no longer forces them off. The player score stays hidden (deliberate: the tutorial has no score). | `playlists/tutorial.txt` sets only `RULE_ENABLE_COLOUR_PICKER: OFF`. | `modes/tutorial.py`, `server/tutorial_launcher.py` | same |
| T10 lane reuse | Every cell changed inside a lane (dig, build, paint, collapsed discs) is tracked (bounded at 200,000 per lane) and restored from a pristine Training.vxl copy when the lane is reassigned. Solid restores replay as BlockBuildColored and removals as checked kill Damage(37) once the new occupant is known. | `_restore_lane_targets` used to repaint only the bullseyes. | `modes/tutorial.py` | same |
| T4 / T8 loadouts | Unchanged and confirmed: nothing below SHOOTING, a pistol at SHOOTING, and pistol/block/spade at CLIMB equipping the last item (spade). The internal spawn weapon stays the pistol below SHOOTING because the weapon profile needs a real id; the owner's CreatePlayer loadout is empty, so nothing is held. | SetClassLoadout `instant=1` equips the final list item. | `modes/tutorial.py` | same |
| T9 shared script | `tests/fixtures/tutorial_script.json` pins stages, message ids, gate thresholds, timings, loadouts, the tower gate, targets and lane origins. The native client keeps a byte-identical copy. | Two independent lesson scripts had drifted. | `tests/fixtures/tutorial_script.json` | `tests/test_tutorial.py` |


**Client-side parity gaps** for the native BattleSpades client are catalogued separately in `G:\AoSRevival\BattleSpadesClient\docs\RETAIL_PARITY_GAPS_2026-09-27.md` (75 items). The bit 0x08 interaction (P1-05/P1-06) is closed: the native client now reads 0x08 as goo and derives wading from the water plane. The client's integration passes are recorded there under "Final integration 2026-09-27" and "Round 2 integration (2026-09-27)".

**Note:** the retail client plays no server music while `music_volume` is 0 in its `config.txt`.

### Round 5: single-pellet seeded spread (P2-18)

Retail `Character.shoot` (character.pyd `sub_10049DB0`) sends the unspread aim once in ShootPacket(6) and then expands `weapon.pellets` directions from `ShootPacket.seed`, three `random()` draws per pellet. `Weapon.pellets` defaults to 1, so a rifle, SMG or pistol shot is also one seeded direction. Our server resolved `pellet_count <= 1` exactly on the crosshair, while the shooter's own client and every observer (ShootFeedback 8) drew and predicted the seeded ray.

| Area | What changed | Why (evidence) | Key files | Tests / doc |
|---|---|---|---|---|
| Single-pellet hit registration | Every hit-scan gun now goes through `_seeded_pellet_directions`: `Random(seed & 0xFF)`, then per axis `(random()*4-2)*accuracy` for hip fire or `(random()*2-1)*accuracy` when zoomed. Each pellet is resolved by `_resolve_hitscan`, so damage, headshots, block damage, hit events, lag compensation and the shot tally are unchanged. The client-chosen seed of single-pellet shots now also goes to the `pellet_seed_skew` detector (log-only), because it now steers those shots too. | IDA character.pyd `sub_10049DB0`; native client `replicated_hitscan_pellets` (src/world/replicated_shot.cpp) runs the same loop for `pellet_count == 1`. The client's golden test `packet_seed_replays_cpython_axis_spread` (pistol, seed 42, contact cell (5,97,99)) is reproduced by the server. | `server/combat_runtime.py` (`handle_shot`, `_seeded_pellet_directions`) | `tests/test_single_pellet_spread.py` |
| Accuracy model | Replaced the shotgun-only "level" curve with retail `Weapon.prep_shoot`/`shot_weapon`. Variable-accuracy guns lerp `accuracy_min..accuracy_max` over the `accuracy_spread` bloom reached before this round's increase. The bloom recovers at `accuracy_spread_reduction_speed` per second and resets on a weapon switch. Other guns use their constant class `accuracy`. A zoomed shot uses `accuracy_zoom` where the class sets one (SNIPER/SNIPER2 = 0.0, so zoomed sniper shots stay on the crosshair). Shotgun numbers are identical to before (a test pins the old curve). | Stock weapon classes (`aoslib/weapons/*.py` with stock `shared/constants.py`) match the native client's generated `RetailAimTuning` row for row. The native client's `observe_hitscan_bloom`/`WeaponRuntime::current_accuracy` use the same model. | `server/combat_runtime.py` (`RETAIL_ACCURACY_SPREAD`, `RETAIL_ACCURACY_ZOOM`) | same |
| Snub pistol accuracy | `spread` 0.0 -> 0.01. | Stock `SNUB_PISTOL_ACCURACY` (A1138) = 0.01; the native client catalog agrees. | `server/game_constants.py` | same |

Precision note: the native client adds each offset in `float`, the server in `double`. The difference is about 1e-7 of the direction and has no gameplay effect.

No existing test needed changing: all of the combat, anticheat and lag-compensation tests still pass with the seeded single-pellet ray.

Suite (`py -3.12 -m pytest -q -p no:cacheprovider`, run as 3 concurrent parts). Part 1 (files before `test_surface_corridor.py`, excluding the map matrix): 9541 passed, 1 skipped, 3 failed. The failures were `test_map_load_stall` [MayanJungle] and [Classic] (31.2 ms against a 30 ms gap budget) and `test_launcher::test_source_release_check_passes` (worker-spawn timeout). All three are load and timing failures from running the parts at the same time, and all 8 affected tests pass on a rerun. Part 2: 458 passed. Part 3 (`test_bot_map_matrix.py`): 36 passed.
