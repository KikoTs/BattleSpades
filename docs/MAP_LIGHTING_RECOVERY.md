# Original game light blocks

The remaster's lighting, emissive palettes, exposure and renderer settings stay
unchanged. Original-game light blocks use the existing protocol-168 type-13
FlareBlockEntity path. This work does not make ordinary coloured terrain glow.

## Verified client behaviour

The original gameScene.pyd FlareBlockEntity initialization adds a user block
in its network RGB at 5 HP and registers a static point light with radius
5.0. The delete method calls `Entity.delete` and removes the static point
light; it makes no direct terrain-removal call. Terrain updates remain a
separate authority path. The native terrain lighting
kernel is documented in the client parity notes. Existing loading-queue fixes
retain these entities until the world is ready.

## Reviewed prop recovery

Spooky Mansion contains 41 isolated cream light cubes directly on dark-brown
posts and 36 on the brown/gold arms of indoor candelabras. These are the
brown-base/light-top props identified by the owner and inspected in native
VXL geometry. Their light RGB `(250,250,200)` comes from this map's recovered
`static_light_color1`. The owner found the full-strength reconstruction too
bright in the original client, so the reviewed fixtures now use a local
`light_gain = 0.5`, producing packet RGB `(125,125,100)`. The original
marker keeps its recovered palette colour. This gain is a playtest adjustment,
not a recovered original-server intensity.

`SpookyMansion.lights.json` records those 77 existing voxel locations and pins
the original terrain SHA-256. `tools/map_metadata/recover_fixture_lights.py
--check` verifies the inventory: exact light colour, one of the two reviewed
base colours, solid support below, four empty horizontal neighbours and two
air cells above. There is no runtime colour clustering or bright-terrain scan.
The original blue-marker light remains, for 78 total map lights.

The former 122-cluster Spooky reconstruction stays in ignored research
storage. It is not active. Yellow blocks, pale windows,
roof fixtures and ordinary pale terrain are not promoted to emitters by a
blanket palette match. The original map descriptions, sky, ambient lighting,
exposure and remaster emissive palettes remain unchanged.

The server sends standard type-13 coloured entities at the reviewed cubes.
Existing fixtures must still be present; the authority mirrors the packet
colour at those cells without changing their geometry. Their lights are removed if their voxels disappear; a same-map
restart does not resurrect broken props. The engine applies its existing
source-coordinate normalization once.

Live verification used the original Steam `aos.exe`, app 224540, connecting
with `+connect 127.0.0.1:28630`. The server logged 78 static lights. The stock
client rendered the brown-post lights and their pools on the nearby ground;
this was observed in the actual original game, not the remaster capture tool.

Mayan Jungle now adds 20 reviewed torch heads. Every head uses its recovered
`(224,172,29)` source palette, sits on a `(101,62,7)` brown torch mount,
and has exactly one adjoining wall face. Four upper temple torches have a
low roof; their clearance is checked separately from Spooky's free-standing
posts. A 0.5 fixture gain produces `(112,86,14)`. Four original marker lights
remain, for 24 total. Native geometry was reviewed; the user's running Steam
session remains on Spooky Mansion.

These positions are exact existing prop voxels; the original server entity
list is still unavailable. Dragon Island and Trenches have no nearby matches
even within four RGB levels of their recovered light palettes. Other maps'
missing palettes/placements remain open, rather than assuming all maps share
these fixture colours.

## BattleSpades custom-lighting precedence

The original client receives its unchanged type-13 records. Negotiated
BattleSpades clients additionally receive a versioned `BSL1` annotation in
`int_properties`, identifying map ownership and the fixture's original RGB.
Player-placed flares have no map annotation. No shader or profile values change.

In Enhanced mode, an official map's authored emissive profile wins **where it
actually covers that fixture colour and geometry**. The session retains the
original fixture RGB for that profile and omits the additional server point
light. Compatibility mode restores the server RGB and native point light.
Maps without a covering custom fixture profile keep their server lights.
Switching modes updates existing lights without duplicates or rebuilding
destroyed/replaced blocks. Unknown annotations fail closed.

This ownership annotation requires the updated server; old servers do not
identify ownership reliably and retain the old presentation behaviour. Tests
cover original/BattleSpades packet framing, player flares, unknown maps,
profile toggles, source colour retention and destroyed fixtures.

## Native evidence checked on 2026-11-02

Original `gameScene.pyd` SHA-256:
`3c4bae35f955eaa5c3f0cbfdfa5ce7e9bbc277293de849e3e6219c4cba67c1e7`.
REA/IDA analyzed initialization at `0x100de910` and deletion at `0x100df070`.
Instruction-level constant loads at `0x100decac` and `0x100def63` reference
`A1031` (5 HP) and `A2261` (radius 5), corroborating the existing client
constant audit. Creation resolves `add_user_block` and
`add_static_point_light`; deletion resolves `remove_static_point_light`.

Evidence records: `ev_bba441ae89517f708d5a2e4f3d01c8645607807b93888289865d513a180d8d79`
and `ev_40a04fd95302b8fa73c0c2daa1dbdb5c8d2266de7b15d2589e43286b52d21afc`.
The local export is retained under `build/wiki-parity-implementation-20261102/`.
Decompiler output warns about constant memory; the quoted loads were checked
in assembly. This is static evidence, not a stock-server gameplay capture.
