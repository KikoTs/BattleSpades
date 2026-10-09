# Importing Classic VXL maps

Place an uncompressed AoS 0.75 or 0.76 `.vxl` file in the configured maps directory
and select its basename in the normal server map rotation or hosting interface.
No conversion tool or separate Classic map package is required. The game mode
still comes from the host's mode selection; importing terrain does not change it.

The server recognizes a complete 512 × 512 column stream with 64-high span
coordinates as Classic. Classic source Z coordinates move by exactly +176 into
the 240-high BattleSpades world: source bedrock 63 becomes 239, and the water
surface remains at the corresponding height. Empty water columns, caves, bottom
surface runs, and implicit solid interiors retain their geometry. Blue and green
terrain are ordinary colors in Classic, not retail light markers.

VXL has no format header. Some older retail maps also use 64-high coordinates,
so shipped retail maps retain their retail interpretation, including
`20thCenturyTown`. An unusual custom retail map can override automatic detection
with a companion `MapName.json`:

```json
{"vxl_format": "retail"}
```

The accepted values are `auto`, `retail`, and `classic64`. An explicit `classic64`
requires a full 512 × 512 map whose coordinates fit the Classic height. The same
key works in an existing `.ugc` JSON sidecar, or as
`vxl_format = "classic64"` in an assignment-style `.txt` sidecar. Sidecar priority
is `.json`, `.ugc`, `.txt`, then `.vxl.json`; the first definition wins. Metadata is
parsed as inert values: importing a map does not execute piqueserver map scripts
or copy server-specific callbacks and plugins.

Map loading validates span lengths, column count, color runs, and coordinate
bounds. A malformed or empty file fails loading before replacing the previous
world. Native collision and bot navigation share the selected interpretation;
Classic imports do not reuse navigation caches built under older retail rules.

Network transfer keeps the source file CRC and shifts source spans only once.
MapSync contains the finalized world, including removed retail marker cells and
later colored blocks, for both full downloads and matching local-file deltas.
This avoids treating vivid Classic colors as retail light markers on join.

Tests: `tests/test_legacy_vxl_import.py` covers shallow terrain, water, caves,
color preservation, malformed data, format overrides, failed-load rollback, and
full/delta network transfer. Existing stock marker, map synchronization, and bot
navigation regression suites cover retail behavior.
