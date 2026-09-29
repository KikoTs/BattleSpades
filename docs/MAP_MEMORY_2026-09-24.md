# Map memory: per-column fill colour for the implicit interior (2026-09-24)

## Problem

`aoslib/vxl.pyx` (the server's Cython VXL) stored a colour-table entry for
every solid voxel, including the implicit underground below each column's
last span and the solid interior between spans. The VXL file format never
stores those voxels: everything below the last span's surface run is solid by
definition, and the client colours exposed interior itself. Storing them cost
8 bytes each in `_ColorTable`:

| Map | Load RSS before | Load time before |
|---|---|---|
| MayanJungle | 286 MB | 1.8 s |

That is what starved the 512 MB fleet hosts (see the Beta 0.1 notes).

## Change

- `VXL._column_fill`: one `uint32` per column (1 MB), set by the loader to the
  deepest surface colour, exactly the colour the interior fill used before.
- The loader marks interior voxels solid without a colour entry
  (`_store_fill`); authored surface/bottom runs stay explicit, including
  authored colour 0 (`_store_explicit`), so runs re-serialize intact.
- `_color_at` (behind `get_color`, the overview and the serializers) returns
  the explicit colour, else the column fill for a solid voxel, else 0. Terrain
  repair packets therefore still carry the surface colour for exposed interior
  and never a colour-0 (invisible) block.
- Serializers (`serialize_columns`, `get_chunk`, `generate_vxl`): the top run
  of a span now ends at the last authored colour above the bottom run; the
  implicit stretch below the last span stays implicit, as in the file. Converted
  maps that encode a mid-column colour with an empty final span re-serialize
  into one span with the same geometry and every authored colour (checked
  against ArcticBase in `tests/test_vxl_column_fill.py`).
- New probes: `color_entries()`, `column_fill_color(x, y)`,
  `has_explicit_color(x, y, z)`.

| Map | Load RSS after | Load time after | Explicit colours |
|---|---|---|---|
| MayanJungle | 27 MB | 0.68 s | 423 k |
| ArcticBase | 29 MB | 0.59 s | 362 k |
| CityOfChicago | 31 MB | 0.78 s | 453 k |

Dirty-column MapSync records shrink accordingly: a pristine single-span column
is byte-identical to the file instead of carrying its whole underground as
explicit colours.

## Contract change in the sync tests

`tests/test_vxl_sync_stress.py` compared reconstructed client columns with the
server voxel by voxel, colours included. Implicit interior colours are
client-owned and not transmitted, so the comparison now requires exact
solidity everywhere and exact colours wherever the server holds an explicit
colour (`_assert_column_matches`).

## Test harness caveat

The server loader normalizes short legacy *files* by shifting them so their
deepest referenced z becomes 239. A single dirty-column record whose last span
ends above the floor (implicit interior, as in every map file) must not be
decoded standalone through that loader or it is mistaken for a short file;
the tests pad such records to a 2x2 source with empty columns
(`_decode_column_bytes`). The client never applies that normalization to
MapSync columns.

## Rebuild

`py -3.12 setup.py build_ext --inplace` with the server stopped (the fleet's
release pipeline rebuilds all six platforms).
