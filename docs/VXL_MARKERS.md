# VXL chroma markers and team-coloured art

Checked 2026-09-26 against the stock Steam `aoslib.vxl.pyd`
(md5 `bcef66b8e40520036a9d18fa39c448a3`, identical to the dev client's copy).
Every `maps/*.vxl` except 20thCenturyTown is byte-identical to the retail
install's `maps/` folder, so the server streams exactly the bytes a retail
client loads.

## What the retail client strips

`sub_10029FD0` (called once from the map finaliser at `0x1002A3AF`) walks every
voxel and compares `colour & 0x00F0F0F0` with a two-entry table at
`0x1003D0D4`: `0x000000FF` (blue, static-light slot 1) and `0x0000FF00`
(green, slot 0). A match is removed only when the two cells above it are air.
It then gives the newly exposed voxel below a neighbour's colour. It records
no positions and creates no lights.

The server does the same. `ServerVXL` removes those markers from collision,
and `MapResourceService` turns each one into a type-13 `FlareBlockEntity` with
the map's static-light colour. On the client that entity puts the voxel back
with that colour and adds a point light. `WorldManager.restore_static_light_block`
makes the same cell solid again on the server. MapSync still sends the raw
spans, so the map CRC does not change and each client does its own cleanup.
Stock palette fallback: slot 0 `(255,255,82)`, slot 1 `(250,250,200)`.

| Map | marker words | removed (exposed) |
|---|---|---|
| 20thCenturyTown | 2534 blue | 524 (2010 embedded blue stay, as on retail) |
| AncientEgypt | 16 green | 16 |
| ArcticBase | 42 mixed | 42 |
| Frontier | 12 + 12 | 24 |
| GreatWall | 14 + 16 | 30 |
| MayanJungle | 4 green | 4 |
| SpookyMansion | 1 blue | 1 |
| TheColosseum | 16 + 16 | 32 |
| TokyoNeon | 3 + 3 | 6 |
| Training | 24 green | 24 |

Live (Training): the client reports all 24 marker cells solid with about
`(255,255,82)` and 24 `FlareBlockEntity`. No pure-green voxel remains.

## What is not a marker

The bright blue/green voxels `#0028BE` and `#00BE28`/`#00BE2A` are authored
team-coloured art: 7-tall posts at Atlantis's jetties, 3x5 flags on
DoubleDragon, 12x4 banners on TokyoNeon, 2-tall posts along WW1's trenches and
small pieces on BlockNess. Other saturated greens (CityOfChicago `#A7F35C`,
GreatWall `#25E12B`) are also art. The native table does not match any of
them, and no retail binary treats them specially. The byte hits in other pyds
are Cython line-number immediates. Live (Atlantis), the stock client reads
`(117,97,192)` as solid `(0,40,190)` and `(406,381,192)` as solid `(0,190,40)`.
They are meant to render as bright blocks. Changing them would break 1:1 with
retail.

`tests/test_vxl_markers.py` pins the colour table, the marker inventory, the
green-marker removal and the Atlantis team posts.

## Known gap

The bot worker's `CompactVoxelMap` repeats the removal half but not the
flare restore, because restores are not published as navigation deltas. On
stock maps, bots therefore treat each restored flare cell as air (a
one-voxel bump or lamp top). The server and clients agree that the cell is
solid.
