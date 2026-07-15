# Server performance and capacity

BattleSpades has an executable 50-player capacity gate:

```powershell
py scripts\server_capacity.py --players 50 --seconds 30 --port 27016
```

The gate runs the real 60 Hz server loop with 50 simulated players, bot
combat/terrain LOS, entity and mode ticks, 50 WorldUpdate recipients, packet
framing, and ENet packet allocation. It fails when fewer than 50 players spawn,
the achieved rate drops below 58 Hz, tick p99 exceeds 12 ms, or gameplay input
is dropped.

## Current release soak (2026-07-11)

Windows/Python 3.12, CityOfChicago TDM, 50 players, 900-second run:

| Metric | Result |
|---|---:|
| Achieved simulation rate | 59.999 Hz |
| Average tick | 2.662 ms |
| Tick p95 | 3.818 ms |
| Tick p99 | 4.842 ms |
| Maximum tick | 10.642 ms |
| Gameplay packet drops | 0 |
| Logging queue drops | 0 |
| Peak pending gameplay packets | 0 |
| Memory growth | 17.453 MiB |
| Approximate outbound rate | 4.063 MiB/s |
| Process CPU | 0.332 cores |

The run completed 53,999 fixed ticks, sent 1,411,350 outbound packets, and met
every release threshold. Player simulation was the largest measured subsystem
at 2.439 ms p99; bot work was 2.756 ms p99. No subsystem exhausted a bounded
queue.

## Short development baseline

Windows/Python 3.12, CityOfChicago TDM, 50 players, 30-second run:

| Metric | Result |
|---|---:|
| Achieved simulation rate | 59.966 Hz |
| Average tick | 2.777 ms |
| Tick p95 | 3.998 ms |
| Tick p99 | 5.088 ms |
| Maximum tick | 7.178 ms |
| Gameplay packet drops | 0 |
| Logging queue drops | 0 |
| Approximate outbound rate | 4.144 MiB/s |
| Process CPU | 0.237 cores |

The 2026-07-11 post-stabilization release soak ran 900.001 seconds with 50
players: 59.999 Hz, 2.726 ms average tick, 4.915 ms p99, 8.782 ms maximum,
zero gameplay/logging drops, zero entity deferrals, no pending backlog, and
17.453 MiB memory growth. A final 30-second current-tree gate (including the
mode-event FIFO bound) passed at 59.966 Hz and 4.669 ms p99 with
`dropped_mode_events = 0`.

The initial audit baseline failed at 15.37 Hz with 64.9 ms average ticks. The
profile attributed about 84% of samples to synchronized bot LOS acquisition.

## Isolated 12-bot worker baseline (2026-07-14)

Windows/Python 3.12, CityOfChicago TDM, 12 server-owned bots, 900-second strict
gate after DetourCrowd, affordances, bounded prefabs, and mutation ordering:

| Metric | Result |
|---|---:|
| Achieved simulation rate | 60.000 Hz |
| Tick p99 / maximum | 0.846 / 3.175 ms |
| Main-thread bot p99 | 0.420 ms |
| Prefab subsystem p99 | 0.001 ms |
| Worker CPU | 0.150 core |
| Worker memory peak | 58.758 MiB |
| Server memory growth | 23.348 MiB |
| World mutations committed / expired / rejected | 2 / 0 / 0 |
| Gameplay/mode/terrain/world drops | 0 / 0 / 0 / 0 |

This includes spawned-process supervision, staggered perception, native
Recast/Detour/DetourCrowd queries, behavior trees, fair sound sampling,
aim/locomotion motors, class affordances, bounded prefabs, and ordinary
replication. It passes the bot-specific 0.75 ms p99 main-thread budget. Forced
child termination during a 12-bot match also passed supervised one-second
recovery, and all bots continued moving. The automated performance/worker
recovery gates are complete; clean retail observation is still required for
rendered animation and end-to-end objective acceptance.

## Runtime design

- Physics and gameplay simulate at a fixed 60 Hz.
- WorldUpdate uses the retail 30 Hz network cadence.
- Production includes each recipient's grounded self row at the 30 Hz cadence;
  airborne local rows use a bounded 10 Hz cadence while observer snapshots
  remain 30 Hz.
  Omitting it lets the stock client reuse a stale CreatePlayer spawn anchor
  during jump correction. This includes block-tool frames; suppressing them
  was live-reproduced as a 62.759-block rollback.
- ENet service work, pending gameplay packets, and per-tick draining are
  bounded. Excess traffic cannot monopolize the event loop indefinitely.
- Plugin callbacks, entity behavior `on_tick` work, and late-join mutation
  journals are bounded. Overflows increment metrics; a non-contiguous join
  catch-up disconnects the joining client instead of admitting terrain desync.
- Mode callbacks use a bounded FIFO and per-tick drain budget; saturation is
  exposed as `dropped_mode_events`.
- Ordinary logging uses a bounded queue. Formatting and console/file I/O happen
  on the listener thread; a saturated sink drops logs instead of stalling play.
- Full packet parsing/hex traces require `logging.packet_trace = true` and are
  disabled for production.
- Expensive native movement snapshots require
  `debug.movement_debug_capture = true` and are disabled for production.
  Self-row diagnostics use the bounded debug writer queue and must stay off for
  capacity measurements.
- Bot target/LOS decisions, behavior trees, Recast tile builds, and path
  searches run in a supervised child process. The gameplay thread only builds
  staggered immutable snapshots and drains bounded expiring intents.
- The ten-second health line reports per-subsystem average/maximum time and
  separates stale/duplicate ClientData from true input-history overflow.

## Required verification before a release

```powershell
py -m pytest -q
py scripts\server_capacity.py --players 50 --seconds 30 --port 27016
```

Also run one stock-client movement reconciliation scenario and one complete
end-round cycle. The client must remain in `GameScene`, show zero visible
rollback/hard SNAP events, and produce no new crash dump.
