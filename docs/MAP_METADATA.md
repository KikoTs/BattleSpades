# Stock map metadata: retail sources, sidecars and fallbacks

VXL files carry voxels only. Everything else a stock map needs (team spawns,
bases, CTF/TC/MH/Diamond/Occupation objectives, crates, fog, skybox, lighting,
ambience, ground palette) came from a per-map *description* module that the
retail server loaded beside the VXL. This page records what of that survives,
how it is turned into server sidecars, and what the server does for every
stock map and retail mode where retail data does not exist.

Code: `server/map_metadata.py` (parser), `tools/map_metadata/` (recovery
tooling), `maps/*.json` and `maps/retail_map_info.json` (generated data),
`tests/test_map_metadata_retail.py` and `tests/test_map_metadata.py`.

## Retail sources that exist

| Source | Where | What it gives |
|---|---|---|
| `maps/<Map>.txtc` | client install `maps/` (Steam, nonsteam builds; identical everywhere) | Compiled Python 2.7 map-description modules, built from `C:\projects\aceofspades\common\maps\<Map>.txt`. Retail shipped exactly four: **DragonIsland, MayanJungle, SpookyMansion, Trenches**. |
| `aos.pkg` -> `playlists.mapinfo` | client install, PyInstaller PYZ inside `aos.pkg` | Retail map catalogue: per-map `invalid_modes`, `max_players`, classic/demo/mafia flags for all 27 stock maps and 9 UGC baseplates. |
| `playlists/*.txt` | client install | Retail official rotations (which maps each mode's playlist served). |
| `png/ui/game_loading/map_images/*.png` | client install | Loading art per map: shows each map's real sky dome. |
| `maps/*.png` | client install | Menu previews (the same top-down art rotated 90 degrees: preview x = map y, preview y = 512 - map x). |
| The VXLs themselves | `maps/*.vxl` | Team-coloured geometry: the paired team markers `#0028BE` (Blue) / `#00BE2A` (Green) and blue/green painted structures. |
| `shared.constants.FOG_COLORS` | client constants | Fog colour per sky dome, which the retail UGC editor uses. |

Searched and **not** present anywhere (client installs, `aceofspades_source`,
`aoslib-reversed/aosdump`, old dedicated-server trees, all PYZ modules): the
description modules of the other 23 stock maps. They lived only on the retail
server (`feature_server/../../common/maps`). No VXL embeds metadata (every
stock VXL ends exactly at its last column).

## Recovery tooling

`tools/map_metadata/py27.py` reads Python 2.7 `marshal` data from Python 3
and replays module bodies on a literal-only stack machine: constants, tuples,
lists, dicts, unary minus and arithmetic. Any other name lookup, call,
attribute or import raises `InertError`; nothing retail is executed. The
output was cross-checked against a real Python 2.7 `exec` of all four modules
(identical).

```
py -3.12 tools/map_metadata/extract_retail.py           # regenerate maps/<Map>.json + maps/retail_map_info.json
py -3.12 tools/map_metadata/extract_retail.py --check   # exit 1 if a committed file is stale
py -3.12 tools/map_metadata/survey_team_sides.py        # team-colour evidence per VXL
py -3.12 tools/map_metadata/coverage.py                 # the map x mode table below
```

`--client-root` defaults to the Steam install, then
`../aceofspades_nonsteam`, then `../aceofspades_source`; the repo's own
(gitignored) `maps/*.txtc` are also read.

## Sidecar format

`maps/<Map>.json` is loaded before `.ugc`, `.txt` and `<Map>.vxl.json`; keys
from later siblings only fill gaps. Generated sidecars copy every retail
assignment verbatim (tuples become lists) plus a `_retail_source` block
(file, sha256, original path). Do not hand-edit them; rerun the tool.

Keys the parser consumes (all modes unless stated):

| Key | Meaning |
|---|---|
| `skybox_texture` / `skybox_name` | Packet 51 sky dome (a `mesh/<Dome>/<Dome>.txt` name). |
| `fog_color`, `light_color`, `light_direction`, `back_light_color`, `back_light_direction`, `ambient_light_color`, `ambient_light_intensity`, `static_light_color0/1`, `ground_colors`, `ambient_sounds`, `gravity` | Atmosphere and lighting. `ground_colors` goes to InitialInfo verbatim. |
| `team_one_spawn_area`, `team_two_spawn_area` | `[(centre, (w, h, d)), ...]` spawn boxes, Blue = TEAM1, Green = TEAM2. The box floor is extended to the bottom of the map: a player dropped into the box lands on the ground under it (SpookyMansion's Blue box sits 1 voxel above its shore, so a strict box never matched any column). |
| `team_one_spawn`, `team_two_spawn` | Discrete retail spawn points (Trenches). Kept in `MapMetadata.spawn_points`; the areas already enclose them. |
| `team_*_base_point`, `team_*_base_w_h_d`, `team_*_min_destruction` | Team bases (Demolition, CTF, team anchors) and Demolition's voxel threshold. |
| `mh_base_points`, `mh_base_w_h_d` | Multi-Hill hills; also Territory Control territories when the map has no `tc_base_points`. |
| `diamond_base_points`, `_w_h_d`, `_teams`, `_capacity` | Diamond Mine drop-offs. |
| `occupation_base_point`, `occupation_base_w_h_d`, `occupation_bomb_points` | Occupation (Green defends the base). |
| `ammo/health/block_crate_drop_points` | Crate spawns. |
| **ctf / cctf only** `ctf_base_points`, `ctf_base_w_h_d` | CTF capture bases, index 0 Blue, index 1 Green. Replace the team bases. |
| **tc only** `tc_base_points`, `tc_base_w_h_d` | TC territories, replacing the hills. |
| **oc only** `oc_team_one_spawn_area`, `oc_team_two_spawn_area` | Occupation attacker/defender spawns. |
| **zom only** `zombie_spawn_area`, `survivor_spawn_area` | Zombie (TEAM1) and survivor (TEAM2) spawns. The retail zombie boxes cover only the z=239 sea ring (zombies rise from the water), which the world manager never spawns on, so the default Blue box follows them as the dry complement. |
| `name`, `cap_limit`, `time_limit` | Kept as `display_name`, `cap_limit`, `time_limit` (DragonIsland: 100 / 480 s); not yet consumed by modes. |
| `ugc_entities` | Map Creator placements (per-mode rows). |

Retail keys kept in the JSON but not consumed: `ac_*` (per-map Steam
achievement volumes), `screenshot_camera_*`, `author`, `description`,
`version`, `is_world_war_map`.

`maps/retail_map_info.json` feeds `MapMetadata.retail_modes`,
`retail_playlist_modes` and `retail_max_players`. `retail_playlist_modes`
is what retail actually SERVED: retail `playlists.PlayList.__init__` crosses
each playlist's modes with its maps and skips a pair when the mode is in the
map's `invalid_modes`, the map's classic/mafia flag differs from the
playlist's, or the map is not `release`. The raw `demolition.txt` names
GreatWall and `occupation.txt` names BranCastle, but `mapinfo` marks GreatWall
invalid for `dem` and BranCastle invalid for `oc`, so retail never served
either pair (an earlier revision of this page wrongly said it did); the
official rotations no longer list them. Loading a map for a mode in its
`invalid_modes` logs `Retail catalogue lists <Map> as invalid for mode <m>`.
`retail_mode_pool(maps_dir, mode)` returns the filtered single-mode playlist
(Territory Control and VIP = Alcatraz + CityOfChicago; Classic CTF = the
seven classic maps) and is the map-vote pool when `lobby.map_rotation` is
empty; `retail_max_players` orders over-full maps last in the vote. Each load also logs a `layout ...`
summary naming the key behind every spawn/objective family, or
`terrain fallback`.

## Atmosphere per stock map

`catalogue` = `STOCK_MAP_SKYBOXES` in `server/map_metadata.py`; fog then comes
from the dome's `FOG_COLORS` entry and ambience from the per-map override or
the dome's family. Lights `yes` means retail light/back-light/ambient values
from the `.txtc`; otherwise StateData carries the retail editor template row
that MayanJungle and Trenches share byte-for-byte (light (236,244,203) dir
(-0.7,0.3,0), back (15,20,10) dir (0,0.7,0.3), ambient (15,30,10) x 0.3;
`server/builders/state_data.py`). The earlier (180,192,220) "London" preset
came from the community reimplementation, not retail.

| Map | Skybox | Source | Fog | Ambience | Lights | Gravity |
|---|---|---|---|---|---|---|
| 20thCenturyTown | WW1.txt | json override | (168, 134, 109) | amb_city | - | 1.0 |
| Alcatraz | Alcatraz.txt | catalogue | (77, 68, 66) | amb_alcatraz | - | 1.0 |
| AncientEgypt | Egypt.txt | catalogue | (195, 116, 77) | amb_desert | - | 1.0 |
| ArcticBase | ArcticBase.txt | json override | (114, 174, 175) | amb_arctic | - | 1.0 |
| Atlantis | Atlantis.txt | catalogue | (61, 100, 214) | amb_harbour | - | 1.0 |
| BlockNess | User_Grassland.txt | catalogue | (111, 215, 223) | amb_ww_lighter | - | 1.0 |
| BranCastle | BranCastle.txt | catalogue | (30, 30, 30) | amb_castula | - | 1.0 |
| CastleWars | Invasion.txt | json override | (61, 61, 53) | amb_castlewars | - | 1.0 |
| CityOfChicago | Chicago.txt | json override | (0, 0, 0) | amb_oldchicago | - | 1.0 |
| Classic | Classic.txt | catalogue | (30, 30, 30) | amb_rural | - | 1.0 |
| Crossroads | WW1.txt | catalogue | (168, 134, 109) | amb_ww_lighter | - | 1.0 |
| DoubleDragon | SecretBase_Night.txt | catalogue | (0, 0, 0) | amb_area51 | - | 1.0 |
| DragonIsland | SecretBase.txt | retail .txtc | (55, 99, 199) | amb_doomwind | yes | 1.0 |
| Frontier | Frontier.txt | catalogue | (163, 111, 65) | amb_western | - | 1.0 |
| GreatWall | GreatWall.txt | catalogue | (163, 153, 138) | amb_high | - | 1.0 |
| Hiesville | WW2.txt | catalogue | (168, 134, 109) | amb_ww_coastalcold | - | 1.0 |
| Invasion | Invasion.txt | catalogue | (61, 61, 53) | amb_invasion | - | 1.0 |
| London | London.txt | catalogue | (33, 32, 27) | amb_city | - | 1.0 |
| LunarBase | LunarBase.txt | catalogue | (62, 59, 57) | amb_moon | - | 0.40625 |
| MayanJungle | MayanJungle.txt | retail .txtc | (69, 76, 39) | amb_jungle,em_river | yes | 1.0 |
| SpookyMansion | BranCastle.txt | retail .txtc | (50, 59, 61) | amb_zombieisland | yes | 1.0 |
| TheColosseum | Colosseum.txt | catalogue | (162, 110, 64) | amb_desert | - | 1.0 |
| TokyoNeon | Tokyo.txt | catalogue | (0, 0, 0) | amb_city | - | 1.0 |
| ToTheBridge | WW2_DockLands.txt | catalogue | (168, 134, 109) | amb_harbour | - | 1.0 |
| Training | Classic_B.txt | catalogue | (111, 215, 223) | amb_rural | - | 1.0 |
| Trenches | WW1.txt | retail .txtc | (168, 134, 109) | amb_ww_lighter | yes | 1.0 |
| WinterValley | ArcticBase.txt | catalogue | (114, 174, 175) | amb_arctic | - | 1.0 |
| WW1 | WW1.txt | catalogue | (168, 134, 109) | amb_ww_lighter | - | 1.0 |

Evidence for the non-retail skybox rows:

* **CastleWars -> Invasion.txt** (was `Classic.txt`): its loading art shows
  a red volcanic sky, mountain ranges and falling fireballs, which only the
  Invasion dome has. `maps/CastleWars.json` was updated to match.
* **DoubleDragon -> SecretBase_Night.txt** (was `GreatWall.txt`): its
  loading art shows a starry night, a moon and a meteor streak; only
  SecretBase_Night has a meteor layer. It is DragonIsland's terrain family at
  night; ambience follows the dome (`amb_area51`).
* **Crossroads -> WW1.txt** (was `User_Grassland.txt`, the UGC editor's
  cyan void): its loading art is sepia smoke haze, the WW dome family.
* **Hiesville -> WW2.txt** (was `User_Grassland.txt`): grey overcast art,
  matching the WW2 gradient (`#656F76`-ish); WW2 is otherwise unused by any
  stock map and Hiesville is the Normandy map.
* All other catalogue rows were already consistent with their loading art.
  BlockNess stays on `User_Grassland.txt`: its teal art matches both
  `User_Grassland` and its identical twin `Classic_B`.
* Fog for non-retail maps is the dome's `FOG_COLORS` value. That table is the
  UGC editor's and is only approximately what the retail server sent: for
  the four retail maps the `.txtc` fog wins, and it differs from the dome
  default for DragonIsland `(55,99,199)` vs `(69,92,100)` and SpookyMansion
  `(50,59,61)` vs `(30,30,30)`. Retail per-map fog for the other maps is
  unknown.

## Team orientation where retail layouts are missing

Without authored spawn areas the world manager picks dry, level columns in a
per-team region and clusters spawns around the one nearest the region centre
(`fallback_spawn_regions`). The default is Blue west `(64,128)-(192,384)`,
Green east `(320,128)-(448,384)`. Seven maps override it because their VXLs
show which side each team owns (`survey_team_sides.py` output):

| Map | Evidence | Blue (TEAM1) region | Green (TEAM2) region |
|---|---|---|---|
| TokyoNeon | markers: Blue x 404-438, Green x 69-103 | east `(320,128)-(448,384)` | west `(64,128)-(192,384)` |
| CastleWars | blue-roofed castle, paint centroid (403,443); green castle (106,64) | south-east castle `(336,336)-(480,480)` | north-west castle `(32,32)-(176,176)` |
| Atlantis | markers: Blue (92-128, 97-133), Green (381-417, 381-417) | north-west jetty `(48,48)-(176,176)` | south-east jetty `(336,336)-(464,464)` |
| DoubleDragon | 12 marker blobs: Blue y 246-493, Green y 18-265; blue paint (146,382) | south-west tower `(160,352)-(288,496)` | north-east tower `(224,16)-(352,160)` |
| WW1 | markers: Blue y 354-467, Green y 49-153 | south trenches `(64,352)-(448,464)` | north trenches `(64,48)-(448,160)` |
| ToTheBridge | blue buildings on the south island, paint centroid (248,385) | south island `(64,336)-(448,480)` | north island `(64,32)-(448,176)` |
| Crossroads | blue-roofed block and bunker north, paint centroid (261,177) | north `(160,80)-(352,208)` | south `(160,304)-(352,432)` |

Validation of the colour rule against real retail data: on Trenches (retail
Blue spawn x 16-91, Blue CTF base (127,255)) the blue paint centroid is
(118,251), on Blue's side. Maps with no usable signal keep the default:
20thCenturyTown (not a retail map), Alcatraz, AncientEgypt, ArcticBase,
BranCastle, Classic, CityOfChicago (blue/green signage only), BlockNess (12
and 21 marker voxels, too few), Frontier, GreatWall, Hiesville, Invasion,
London, LunarBase, TheColosseum, Training, WinterValley.

Modes derive their fallback objectives from those team anchors:

* CTF / Demolition: base = the team anchor (`world_manager.team_base_anchor`,
  from base zones, else spawn zones, else the region). CTF intel sits on the
  retail base point (Classic CTF: 3 blocks toward the enemy); maps without
  recovered bases use `intel_fallback_offset_from_base` along the line
  between the two bases (so north/south maps are handled). See
  docs/RETAIL_VALUES.md for the evidence.
* Multi-Hill, Territory Control, Diamond Mine: dry "corridor" objectives
  spaced between the two anchors (`modes/*: _build_zones` warnings "no ...
  sidecar").
* Occupation: fallback base near the Green anchor and bombs toward midfield.
* TDM, VIP, Zombie: spawns only.

## Map x mode coverage

Generated by `tools/map_metadata/coverage.py` for every stock VXL and every
mode `playlists.mapinfo` allows on it (Training is the scripted tutorial).
`retail` = recovered retail key, `evidence` = team-colour-oriented fallback
region, `fallback` = generic inference. Classic CTF (`cctf`) uses the `ctf`
rows. 190 pairs: 29 with retail spawns (17 of those with a retail
objective too), 57 with evidence-oriented spawns, 104 generic.

| Map | Mode | Spawns | Objectives |
|---|---|---|---|
| 20thCenturyTown | tdm (not a retail map) | fallback (west/east) | - |
| Alcatraz | ctf | fallback (west/east) | fallback (team anchor) |
| Alcatraz | tdm | fallback (west/east) | - |
| Alcatraz | mh | fallback (west/east) | fallback (mode inference) |
| Alcatraz | oc | fallback (west/east) | fallback (mode inference) |
| Alcatraz | tc | fallback (west/east) | fallback (mode inference) |
| Alcatraz | vip | fallback (west/east) | - |
| Alcatraz | zom | fallback (west/east) | - |
| AncientEgypt | tdm | fallback (west/east) | - |
| AncientEgypt | dia | fallback (west/east) | fallback (mode inference) |
| AncientEgypt | mh | fallback (west/east) | fallback (mode inference) |
| AncientEgypt | oc | fallback (west/east) | fallback (mode inference) |
| AncientEgypt | tc | fallback (west/east) | fallback (mode inference) |
| AncientEgypt | vip | fallback (west/east) | - |
| AncientEgypt | zom | fallback (west/east) | - |
| ArcticBase | tdm | fallback (west/east) | - |
| ArcticBase | dia | fallback (west/east) | fallback (mode inference) |
| ArcticBase | oc | fallback (west/east) | fallback (mode inference) |
| ArcticBase | vip | fallback (west/east) | - |
| ArcticBase | zom | fallback (west/east) | - |
| Atlantis | ctf | evidence (team colours) | fallback (team anchor) |
| Atlantis | tdm | evidence (team colours) | - |
| Atlantis | dem | evidence (team colours) | fallback (team anchor) |
| Atlantis | dia | evidence (team colours) | fallback (mode inference) |
| Atlantis | mh | evidence (team colours) | fallback (mode inference) |
| Atlantis | oc | evidence (team colours) | fallback (mode inference) |
| Atlantis | tc | evidence (team colours) | fallback (mode inference) |
| Atlantis | vip | evidence (team colours) | - |
| Atlantis | zom | evidence (team colours) | - |
| BlockNess | ctf | fallback (west/east) | fallback (team anchor) |
| BlockNess | tdm | fallback (west/east) | - |
| BlockNess | dem | fallback (west/east) | fallback (team anchor) |
| BlockNess | dia | fallback (west/east) | fallback (mode inference) |
| BlockNess | mh | fallback (west/east) | fallback (mode inference) |
| BlockNess | oc | fallback (west/east) | fallback (mode inference) |
| BlockNess | tc | fallback (west/east) | fallback (mode inference) |
| BlockNess | vip | fallback (west/east) | - |
| BlockNess | zom | fallback (west/east) | - |
| BranCastle | dia | fallback (west/east) | fallback (mode inference) |
| BranCastle | mh | fallback (west/east) | fallback (mode inference) |
| BranCastle | tc | fallback (west/east) | fallback (mode inference) |
| BranCastle | zom | fallback (west/east) | - |
| CastleWars | ctf | evidence (team colours) | fallback (team anchor) |
| CastleWars | tdm | evidence (team colours) | - |
| CastleWars | dem | evidence (team colours) | fallback (team anchor) |
| CastleWars | dia | evidence (team colours) | fallback (mode inference) |
| CastleWars | mh | evidence (team colours) | fallback (mode inference) |
| CastleWars | tc | evidence (team colours) | fallback (mode inference) |
| CastleWars | vip | evidence (team colours) | - |
| CastleWars | zom | evidence (team colours) | - |
| CityOfChicago | ctf | fallback (west/east) | fallback (team anchor) |
| CityOfChicago | tdm | fallback (west/east) | - |
| CityOfChicago | dem | fallback (west/east) | fallback (team anchor) |
| CityOfChicago | mh | fallback (west/east) | fallback (mode inference) |
| CityOfChicago | tc | fallback (west/east) | fallback (mode inference) |
| CityOfChicago | vip | fallback (west/east) | - |
| CityOfChicago | zom | fallback (west/east) | - |
| Classic | ctf | fallback (west/east) | fallback (team anchor) |
| Classic | tdm | fallback (west/east) | - |
| Classic | vip | fallback (west/east) | - |
| Classic | zom | fallback (west/east) | - |
| Crossroads | ctf | evidence (team colours) | fallback (team anchor) |
| Crossroads | tdm | evidence (team colours) | - |
| Crossroads | dem | evidence (team colours) | fallback (team anchor) |
| Crossroads | mh | evidence (team colours) | fallback (mode inference) |
| Crossroads | oc | evidence (team colours) | fallback (mode inference) |
| Crossroads | tc | evidence (team colours) | fallback (mode inference) |
| Crossroads | vip | evidence (team colours) | - |
| Crossroads | zom | evidence (team colours) | - |
| DoubleDragon | ctf | evidence (team colours) | fallback (team anchor) |
| DoubleDragon | tdm | evidence (team colours) | - |
| DoubleDragon | dem | evidence (team colours) | fallback (team anchor) |
| DoubleDragon | dia | evidence (team colours) | fallback (mode inference) |
| DoubleDragon | mh | evidence (team colours) | fallback (mode inference) |
| DoubleDragon | tc | evidence (team colours) | fallback (mode inference) |
| DoubleDragon | vip | evidence (team colours) | - |
| DoubleDragon | zom | evidence (team colours) | - |
| DragonIsland | tdm | retail team_one_spawn_area/team_two_spawn_area | - |
| DragonIsland | dem | retail team_one_spawn_area/team_two_spawn_area | retail team_one_base_point/team_two_base_point |
| DragonIsland | dia | retail team_one_spawn_area/team_two_spawn_area | retail diamond_base_points |
| DragonIsland | mh | retail team_one_spawn_area/team_two_spawn_area | retail mh_base_points |
| DragonIsland | oc | retail team_one_spawn_area/team_two_spawn_area | retail occupation_base_point + 3 bomb points |
| DragonIsland | tc | retail team_one_spawn_area/team_two_spawn_area | retail mh_base_points |
| DragonIsland | vip | retail team_one_spawn_area/team_two_spawn_area | - |
| DragonIsland | zom | retail team_one_spawn_area/team_two_spawn_area | - |
| Frontier | tdm | fallback (west/east) | - |
| Frontier | dem | fallback (west/east) | fallback (team anchor) |
| Frontier | dia | fallback (west/east) | fallback (mode inference) |
| Frontier | mh | fallback (west/east) | fallback (mode inference) |
| Frontier | oc | fallback (west/east) | fallback (mode inference) |
| Frontier | tc | fallback (west/east) | fallback (mode inference) |
| Frontier | vip | fallback (west/east) | - |
| Frontier | zom | fallback (west/east) | - |
| GreatWall | tdm | fallback (west/east) | - |
| GreatWall | dia | fallback (west/east) | fallback (mode inference) |
| GreatWall | mh | fallback (west/east) | fallback (mode inference) |
| GreatWall | oc | fallback (west/east) | fallback (mode inference) |
| GreatWall | tc | fallback (west/east) | fallback (mode inference) |
| GreatWall | vip | fallback (west/east) | - |
| GreatWall | zom | fallback (west/east) | - |
| Hiesville | ctf | fallback (west/east) | fallback (team anchor) |
| Hiesville | tdm | fallback (west/east) | - |
| Hiesville | dia | fallback (west/east) | fallback (mode inference) |
| Hiesville | mh | fallback (west/east) | fallback (mode inference) |
| Hiesville | oc | fallback (west/east) | fallback (mode inference) |
| Hiesville | tc | fallback (west/east) | fallback (mode inference) |
| Hiesville | vip | fallback (west/east) | - |
| Hiesville | zom | fallback (west/east) | - |
| Invasion | ctf | fallback (west/east) | fallback (team anchor) |
| Invasion | tdm | fallback (west/east) | - |
| Invasion | mh | fallback (west/east) | fallback (mode inference) |
| Invasion | oc | fallback (west/east) | fallback (mode inference) |
| Invasion | tc | fallback (west/east) | fallback (mode inference) |
| Invasion | vip | fallback (west/east) | - |
| Invasion | zom | fallback (west/east) | - |
| London | tdm | fallback (west/east) | - |
| London | dia | fallback (west/east) | fallback (mode inference) |
| London | mh | fallback (west/east) | fallback (mode inference) |
| London | oc | fallback (west/east) | fallback (mode inference) |
| London | tc | fallback (west/east) | fallback (mode inference) |
| London | vip | fallback (west/east) | - |
| London | zom | fallback (west/east) | - |
| LunarBase | tdm | fallback (west/east) | - |
| LunarBase | dem | fallback (west/east) | fallback (team anchor) |
| LunarBase | dia | fallback (west/east) | fallback (mode inference) |
| LunarBase | mh | fallback (west/east) | fallback (mode inference) |
| LunarBase | oc | fallback (west/east) | fallback (mode inference) |
| LunarBase | tc | fallback (west/east) | fallback (mode inference) |
| LunarBase | vip | fallback (west/east) | - |
| MayanJungle | tdm | retail team_one_spawn_area/team_two_spawn_area | - |
| MayanJungle | dia | retail team_one_spawn_area/team_two_spawn_area | retail diamond_base_points |
| MayanJungle | mh | retail team_one_spawn_area/team_two_spawn_area | retail mh_base_points |
| MayanJungle | oc | retail team_one_spawn_area/team_two_spawn_area | retail occupation_base_point + 3 bomb points |
| MayanJungle | tc | retail team_one_spawn_area/team_two_spawn_area | retail mh_base_points |
| MayanJungle | vip | retail team_one_spawn_area/team_two_spawn_area | - |
| MayanJungle | zom | retail team_one_spawn_area/team_two_spawn_area | - |
| SpookyMansion | tdm | retail team_one_spawn_area/team_two_spawn_area | - |
| SpookyMansion | dia | retail team_one_spawn_area/team_two_spawn_area | retail diamond_base_points |
| SpookyMansion | mh | retail team_one_spawn_area/team_two_spawn_area | retail mh_base_points |
| SpookyMansion | oc | retail oc_team_one_spawn_area/oc_team_two_spawn_area | retail occupation_base_point + 3 bomb points |
| SpookyMansion | tc | retail team_one_spawn_area/team_two_spawn_area | retail tc_base_points |
| SpookyMansion | vip | retail team_one_spawn_area/team_two_spawn_area | - |
| SpookyMansion | zom | retail survivor_spawn_area/zombie_spawn_area+team_one_spawn_area | - |
| TheColosseum | tdm | fallback (west/east) | - |
| TheColosseum | dia | fallback (west/east) | fallback (mode inference) |
| TheColosseum | mh | fallback (west/east) | fallback (mode inference) |
| TheColosseum | oc | fallback (west/east) | fallback (mode inference) |
| TheColosseum | tc | fallback (west/east) | fallback (mode inference) |
| TheColosseum | vip | fallback (west/east) | - |
| TheColosseum | zom | fallback (west/east) | - |
| TokyoNeon | ctf | evidence (team colours) | fallback (team anchor) |
| TokyoNeon | tdm | evidence (team colours) | - |
| TokyoNeon | dem | evidence (team colours) | fallback (team anchor) |
| TokyoNeon | dia | evidence (team colours) | fallback (mode inference) |
| TokyoNeon | vip | evidence (team colours) | - |
| TokyoNeon | zom | evidence (team colours) | - |
| ToTheBridge | ctf | evidence (team colours) | fallback (team anchor) |
| ToTheBridge | tdm | evidence (team colours) | - |
| ToTheBridge | dem | evidence (team colours) | fallback (team anchor) |
| ToTheBridge | dia | evidence (team colours) | fallback (mode inference) |
| ToTheBridge | mh | evidence (team colours) | fallback (mode inference) |
| ToTheBridge | oc | evidence (team colours) | fallback (mode inference) |
| ToTheBridge | tc | evidence (team colours) | fallback (mode inference) |
| ToTheBridge | vip | evidence (team colours) | - |
| ToTheBridge | zom | evidence (team colours) | - |
| Training | tut | fallback (west/east) | - |
| Trenches | ctf | retail team_one_spawn_area/team_two_spawn_area | retail ctf_base_points |
| Trenches | tdm | retail team_one_spawn_area/team_two_spawn_area | - |
| Trenches | dia | retail team_one_spawn_area/team_two_spawn_area | retail diamond_base_points |
| Trenches | mh | retail team_one_spawn_area/team_two_spawn_area | retail mh_base_points |
| Trenches | tc | retail team_one_spawn_area/team_two_spawn_area | retail tc_base_points |
| Trenches | vip | retail team_one_spawn_area/team_two_spawn_area | - |
| Trenches | zom | retail team_one_spawn_area/team_two_spawn_area | - |
| WinterValley | ctf | fallback (west/east) | fallback (team anchor) |
| WinterValley | tdm | fallback (west/east) | - |
| WinterValley | dia | fallback (west/east) | fallback (mode inference) |
| WinterValley | mh | fallback (west/east) | fallback (mode inference) |
| WinterValley | oc | fallback (west/east) | fallback (mode inference) |
| WinterValley | tc | fallback (west/east) | fallback (mode inference) |
| WinterValley | vip | fallback (west/east) | - |
| WinterValley | zom | fallback (west/east) | - |
| WW1 | ctf | evidence (team colours) | fallback (team anchor) |
| WW1 | tdm | evidence (team colours) | - |
| WW1 | dem | evidence (team colours) | fallback (team anchor) |
| WW1 | dia | evidence (team colours) | fallback (mode inference) |
| WW1 | mh | evidence (team colours) | fallback (mode inference) |
| WW1 | oc | evidence (team colours) | fallback (mode inference) |
| WW1 | tc | evidence (team colours) | fallback (mode inference) |
| WW1 | vip | evidence (team colours) | - |
| WW1 | zom | evidence (team colours) | - |

## Open items for other owners

* **Indoor retail spawn boxes** (`server/world_manager.py`): the world
  manager only considers each column's topmost surface. Retail boxes inside
  buildings therefore yield no columns: MayanJungle's temple box
  (437,269,187) and four of SpookyMansion's five survivor / Occupation
  defender boxes (mansion floors under the roof). The remaining boxes of the
  same team still supply spawns, so nothing falls back, but spawns are
  concentrated in the open-air boxes.
* **Zombie sea spawns**: retail spawned SpookyMansion zombies in the z=239
  water ring; the server rejects water spawns, so zombies use the retail
  Blue shore box instead.
* **CTF intel offset**: fixed 2026-09-26 (retail base point / line between
  the bases).
* **Official rotations** (`configs/official-*.toml`): demolition includes
  GreatWall and occupation BranCastle. Retail's own single-mode playlists
  served both, although `mapinfo` marks them invalid; kept as retail.
* `cap_limit` / `time_limit` from DragonIsland are parsed but not applied.
