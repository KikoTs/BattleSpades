# Map sync on join: what a late joiner receives

Collision verified live 2026-09-24 on ArcticBase with the tracer dev client.
Surface-color serialization corrected 2026-09-30 after a stock retail late
join exposed a gap in that collision-only check.

## Exposed faces on a late join

The retail VXL loader stores solidity separately from explicit surface
colors. Implicit span interiors become solid without color-table entries.
After live digging the retail mutation function (`vxl.pyd` `0x10029DA0`)
creates entries for the six face neighbors. MapSync finalization only
shades existing entries (apart from floor, outer boundary and marker
cleanup); it does not create all newly exposed underground surfaces.

The server previously removed a voxel without materializing these neighbor
colors. Full sync sent pristine spans for neighboring columns; delta sync
omitted them. The resulting walls had correct collision but missing visible
faces. `WorldManager` now materializes each newly exposed solid neighbor
using its existing canonical interior RGB and marks that column dirty.
Authored and painted colors remain intact. Batch excavation finishes before
the mutation is published, and snapshot capture remains immutable.

`tests/test_map_sync_exposed_surfaces.py` independently decodes both solidity
and explicit colors from full and delta payloads. It covers single cuts,
lines, blast cavities, XY boundaries, painted colors, concurrent join
catch-up, and fresh `Connection.send_map_data` joins after excavation on
MayanJungle and vertically shifted 20thCenturyTown. The original failing
assertion was a solid side wall with no explicit color entry.

## Before

After the retail client finished `MapSync` and entered `GameScene`, its first
`ClientData` triggered `replay_map_air_overrides`: the server re-sent one
reliable `Damage(37)` packet for **every cell destroyed since the map was
loaded** (256 per input frame, player gameplay-gated meanwhile). On a
well-dug round this is thousands of reliable packets and the joiner watches
terrain "sync" after spawning, which is what Kiril described as tunnels and
dug blocks appearing after the join instead of during loading.

The replay was added as a belt-and-braces repair ("the retail VXL worker can
occasionally retain stale native collision after a heavily drilled column
merge") without a reproduction.

## Measurement

`scripts/run_validation_server.py` with a validation-only plugin that digs
434 cells at load (a 3x3 shaft 40 deep, a 30-long tunnel 20 below the
surface, a 60-deep column with every other cell removed, an 8x8x3 underground
room, and a surface crater), then a fresh client joined with the replay
disabled and its `map.get_solid` was compared with the server for all 1310
dug cells plus face neighbours.

| Probed cells | Mismatches | MapSync size (pristine → dug) |
| ---: | ---: | --- |
| 1310 | 0 | 1,459,176 → 1,460,367 bytes |

The `MapSync` stream already substitutes every edited column's current spans
(`MapSyncSnapshot.build_chunks` overlays `serialize_columns(dirty_columns)`),
so the client's collision was complete when the loading screen ended.
That measurement did not inspect surface-color entries and therefore did
not rule out the late-join rendering defect corrected above. It also did
not establish the original server's implementation.

## Now

`[network] map_air_catchup_enabled` (default `false`) gates the post-join
replay. Edits committed between the joiner's MapSync snapshot and its first
`ClientData` are still replayed from the canonical cell journal
(`replay_map_mutations`), which is small and exact. When the knob is on the
server logs `Join air catch-up armed for <peer>: N cells in M columns`.

Tests: `tests/test_drill_rejoin_air_catchup.py` opts in explicitly;
`tests/test_join_mutation_catchup.py` and `tests/test_vxl_sync_stress.py`
cover the snapshot/journal path.
