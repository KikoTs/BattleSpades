# Polish and stabilisation backlog (started 2026-09-24)

Working list for the autonomous polish loop. Rules given by Kiril: the server
must stay 100% retail using only the packets in the dumped list
(docs/PROTOCOL.md), our client must keep working, every new feature is checked
against the original client's behaviour, all work is server-side for now,
RankUps(66) is never sent (the Revival client has its own XP path), bots must
get safer and keep their teamwork.

Status: `[ ]` open, `[~]` in progress, `[x]` done (with the verifying test or
measurement).

## A. Bots

- [x] Zombie survivors stop moving mid-round; they never build up or climb to
  high ground to escape. Done 2026-09-24, see docs/BOT_ZOMBIE_REFUGE_2026-09-24.md:
  the director elects one high, flat, dry refuge per survivor team
  (`server/bot_ai/zombie_refuge.py`), the policy routes the squad there and
  orders `fortify`, and cooperative behaviour walls the refuge with grounded
  BlockLine runs shared across the squad (`find_rampart_segment`, task kind
  `rampart`). Measured with `scripts/bot_runtime_smoke.py --mode zom --map
  ArcticBase --bots 10 --seconds 150 --full-runtime --seed 7`: survivors travel
  ~80 blocks to a mountain refuge, 22 wall runs built (world_mutations 0 -> 22),
  0 survivor deaths, tick p99 1.0 ms. Tests: tests/test_zombie_refuge.py,
  tests/test_zombie_rampart.py.
- [~] Bot safety: navigation native crash seen in tests
  (`simple_navigation._neighbors`, `navigation_atlas._dry_connected`) — bound
  the worker so a planner fault can never stall the 60 Hz tick; audit
  exception isolation in `director.py` / `supervisor.py`.
  2026-09-24 findings: with `-X faulthandler` the "crash" surfaces as
  order-dependent Python errors inside `server/bot_ai/compact_vxl.py`
  (`SystemError: error return without exception set` from `int.from_bytes` on
  CastleWars in tests/test_bot_map_matrix.py, `TypeError` at
  `_raw_vxl_size` in tests/test_surface_corridor.py[narrow_corner-0]); both
  pass alone, so the map-snapshot bytes handed to the worker are being
  corrupted by an earlier test in the same process (native/threading state,
  not a planner bug). The thread supervisor already rebuilds on exceptions
  (`thread_supervisor.py` "Bounded AI thread batch failed; rebuilding"). Next:
  isolate the producer of the snapshot bytes (`map_snapshot_vxl_bytes`) under
  a process-restart test and decide whether the fleet should run
  `bots.worker = "process"` (the shipped profiles already do).
- [x] Rocketeer jump pack (66): 2026-09-24, 60 Hz capture alignment showed
  the retail owner applies each transition row 2-3 frames after it is sent
  (phase-dependent, per boundary). New knobs `jetpack_activation_defer_frames`
  (2) and `jetpack_exhaustion_tail_frames` (3, was 1) replace the unwired
  `jetpack_owner_*handoff*` knobs; exhaustion is now exact or a forward nudge,
  never the 0.5-block rollback (docs/RETAIL_JUMP_RESTORE.md, "Jump pack
  boundaries"). Residual: one ~0.26-block forward nudge per burn in the
  delay-3 phase.
- [ ] Jump pack residual nudge: learn the client's actual thrust frame from
  the velocity kick in its ClientData (2 vs 3 frames after the transition
  row, plus ping) and re-simulate the frames in between server-side, so both
  boundaries are exact in every phase. Needs a bounded authoritative rewind of
  1-3 input frames.

## B. Announcements (retail parity)

- [x] Inventory every announcement the original server produced against the
  client's string table and fill the gaps. Done 2026-09-24, see
  docs/ANNOUNCEMENTS_RETAIL_2026-09-24.md: ids absent from every client binary
  are server-sent, so Zombie/VIP/CTF/Diamond/Occupation/TDM events, the round
  clock, the time-limit draw and PLAYER_JOINED now go out as LocalisedMessage(50)
  with the retail ids (team-relative where retail is); invented TDM lead spam
  removed. Tests: tests/test_retail_announcements.py.

## C. Unimplemented packets and features (docs/PROTOCOL.md "Planned")

For each: read the client handler in IDA, decide whether the original server
sent it and when, implement with a test, or record why it stays unused.

- [x] ForceShowScores(72) + ShowTextMessage(73): 2026-09-24, IDA + live
  check; 72 holds the retail scores scene at every round end and releases on
  the in-place restart, 73 is the ViewGameStats headline sent after
  ShowGameStats(53) on rollovers (docs/ANNOUNCEMENTS_RETAIL_2026-09-24.md,
  tests/test_retail_end_screen.py).
- [x] LockTeam(79), TeamLockClass(80): Zombie publishes them at the
  countdown/outbreak boundary (StateData keeps the join truth); survivors'
  class is locked during the outbreak like retail. TeamLockScore(81),
  TeamInfiniteBlocks(82), TimeScale(75): decompiled (`teams[id].locked_score`,
  `.infinite_blocks`, `self.time_scale`); no mode changes them mid-round, so
  they stay unused (PROTOCOL.md rows).
- [x] POIFocus(18) = camera focus controller (Demolition explosion watch),
  MinimapBillboard(41/42) = `hud.minimap.add/remove_billboard`,
  EntityUpdates(3) = `process_entity_updates`, BlockOccupy(34) =
  `block_manager.occupy_block`, BlockManagerState(38) =
  `receive_block_manager_state`, ServerBlockAction(39) = client no-op: all
  decompiled and recorded in PROTOCOL.md; none is needed by a current mode
  (18 waits for the Demolition state machine in section D).
- [x] ChangePlayer(17) is already sent (high-minimap visibility).
  Password(111-113): the stock client cannot use it. `LoadingMenu.packet_received`
  has the PasswordNeeded branch commented out and `send_password_callback`
  builds `False` instead of a packet, so a password-gated join would strand
  every retail client; retail passwords were Steam-lobby side
  (`SteamSendPassword`). PackStart/Response/Chunk(61-63): no handler in
  any client module (gameScene, network, Python scenes). Both stay unused.
- [ ] RankUps(66): deliberately NOT sent (Revival XP lives elsewhere).

## D. Objective modes (audit, not skeletons)

Correction 2026-09-24: Multi-Hill (363 lines), Territory Control (457),
Diamond Mine (580), Demolition (429) and Occupation (613) all carry recovered
objective state machines, scoring and win conditions
(`tests/test_recovered_objective_modes.py`); `modes/lobby_skeletons.py` is only
a compatibility import module. What remains is a retail-rules audit per mode.

- [ ] Audit each mode against the recovered gamemode constants and the client
  handlers (TeamProgress(117), bases, diamonds, bombs): timings, score values,
  base activation cadence, the Demolition explosion camera (POIFocus 18), and
  the bots' objective policies for each.

## E. Stability and performance

- [x] Per-column fill colour for map memory: 2026-09-24, MayanJungle load
  286 MB -> 27 MB and 1.8 s -> 0.7 s (docs/MAP_MEMORY_2026-09-24.md,
  tests/test_vxl_column_fill.py; Cython rebuild required).
- [ ] Intermittent native crash of the test process (not the server): seen
  twice on 2026-09-24 in full-suite runs, once before and once after the VXL
  memory change, each time while loading stock maps (`runtime_vxl.py:126
  _iter_explicit_voxels`, pure Python `int.from_bytes`) or parsing a worker
  snapshot (`compact_vxl.py`), i.e. heap corruption by earlier native code.
  Isolated runs and verbose full runs pass (7447 passed). `aoslib/vxl.pyx` is
  compiled with boundscheck/wraparound off; audit every list/bytearray index
  reached without an `_in_bounds` check (`color_block`, `block_line`,
  prefab place/erase, `get_z`). The VXL-heavy suites (spawn, map sync,
  sync stress, workers, compact VXL, column fill, line safety) already pass
  under `PYTHONMALLOC=debug` (46 tests), so the next suspects are the
  bot navigation natives and pyenet in the same process. Third sighting
  the same evening: `SystemError: error return without exception set`
  from `int.from_bytes` in `runtime_vxl._iter_explicit_voxels` while
  loading Frontier in `test_tdm_spawn`, during a full run that overlapped
  the live jump-pack captures (CPU contention). Every isolated rerun
  passes; the interpreter error state, not the map bytes, is what breaks.
  Diagnosis 2026-09-24 night: the full suite passes under `PYTHONMALLOC=debug`
  (7449) and under a bounds-checked build (`BATTLESPADES_CHECKED_BUILD=1
  py -3.12 setup.py build_ext --inplace`, 7449, no IndexError). A later
  bot-matrix run still took an access violation inside pure-Python
  `has_line_of_sight`, and right after it MSVC `cl.exe` itself crashed with
  the same access violation (C1001) while parsing a Windows SDK header; the
  identical rebuild succeeded on retry. Unrelated processes faulting the
  same way under load points at the machine (memory stability), not our
  extensions. Suggested: Windows Memory Diagnostic / disable XMP, then
  re-run the full suite and bot matrix a few times.
- [~] ENet head-of-line freeze of remote players after a lost reliable packet:
  2026-09-24 the per-second countdown refresh is now unreliable, removing the
  only steady reliable traffic of a quiet round (docs/RETAIL_INPUT_LOSS.md,
  Mitigation). A full fix needs a second ENet channel the stock client does
  not read, so busy-play stalls behind lost event packets remain.
