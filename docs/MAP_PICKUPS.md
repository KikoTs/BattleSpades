# Stock map crate placements

All 26 bundled maps with annotated wiki layouts now have authored health, ammo and block crate positions. Twenty maps previously used the generic nine-crate fallback; their 313 wiki-derived placements replace those fallback layouts. The six previously authored layouts are retained, for 415 crates overall.

## Source precision

The wiki's `MapIcon` template records integer percentages, not exact world coordinates. We rendered each original archived article and measured the centres of its 16px crate icons relative to the map image. The map has a 20px frame around a 512px world grid. The actual rendered container is 552 by 581.390625 pixels, so percentage Y must use the container height rather than the bitmap height. These measurements describe the archived page faithfully; they do not recover the retail server's missing XYZ tables.

For the twenty missing layouts, points were placed within those icon footprints on supported terrain with four clear voxels above the drop. The largest horizontal adjustment from a rendered icon centre is 4.517 blocks. Indoor floor levels and bridges were reviewed against the bundled VXL; the four clearly marked water drops on Classic and BlockNess retain their water columns. **These twenty layouts are a wiki/terrain reconstruction, not a claim of exact retail coordinates.** The reviewed runtime positions, source percentages, source-page hashes, map hashes and floor classifications are recorded in [wiki_pickups.json](../tools/map_metadata/wiki_pickups.json).

The importer writes source-VXL Z values; the existing loader adds the map's vertical normalization once. Maps such as Classic (+159), Crossroads (+39), and London (+0) therefore use the same runtime placements after loading. Player spawns, bases, mode objectives, atmosphere, terrain and UGC rows are not edited.

## Existing source data retained

* AncientEgypt and Alcatraz keep their existing UGC crate coordinates. Alcatraz retains Dmitry's corrected drop and normalized runtime Z values.
* DragonIsland, MayanJungle, SpookyMansion and Trenches keep the complete tables recovered from the original `.txtc` modules. They remain reproducible with `tools/map_metadata/extract_retail.py --check`.
* The wiki omits one DragonIsland ammo drop and one of each crate type on SpookyMansion relative to those original tables. Those original drops are retained. The rounded wiki pins also differ from some MayanJungle/Trenches source points; the recovered coordinates take precedence.
* CityOfChicago's existing VIP spawn rows are retained; its new JSON contains only crate data and provenance. No player spawn reconstruction is included.

## Runtime fix

A settled indoor crate used to jump onto the topmost authored roof on its next tick, even when its map coordinates were correct. The crate update now respects the nearest original ceiling. Player-built platforms in the same room still lift a crate; platforms above the original roof do not pull an indoor crate out of the room. Outdoor builds, respawn air drops, original horizontal points and water drops keep their existing behavior. Settled native crates skip unchanged topology checks.

## Coverage

Counts are health / ammo / blocks. `Retained` means an existing authored layout; `Wiki + terrain` means a new reconstruction.

| Map | Health | Ammo | Blocks | Source |
|---|---:|---:|---:|---|
| AncientEgypt | 3 | 3 | 3 | Retained |
| ArcticBase | 5 | 4 | 3 | Wiki + terrain |
| Atlantis | 5 | 5 | 4 | Wiki + terrain |
| BlockNess | 5 | 4 | 3 | Wiki + terrain |
| BranCastle | 3 | 3 | 2 | Wiki + terrain |
| CastleWars | 6 | 6 | 6 | Wiki + terrain |
| Classic | 5 | 5 | 2 | Wiki + terrain |
| Crossroads | 8 | 16 | 3 | Wiki + terrain |
| TheColosseum | 2 | 3 | 2 | Wiki + terrain |
| DoubleDragon | 8 | 8 | 5 | Wiki + terrain |
| DragonIsland | 5 | 5 | 2 | Retained |
| Frontier | 5 | 7 | 4 | Wiki + terrain |
| GreatWall | 4 | 5 | 4 | Wiki + terrain |
| Hiesville | 5 | 5 | 5 | Wiki + terrain |
| Invasion | 10 | 15 | 5 | Wiki + terrain |
| London | 6 | 6 | 3 | Wiki + terrain |
| LunarBase | 5 | 5 | 2 | Wiki + terrain |
| MayanJungle | 7 | 7 | 7 | Retained |
| SpookyMansion | 6 | 6 | 6 | Retained |
| TokyoNeon | 5 | 5 | 5 | Wiki + terrain |
| ToTheBridge | 4 | 4 | 4 | Wiki + terrain |
| Trenches | 8 | 8 | 8 | Retained |
| WinterValley | 7 | 7 | 2 | Wiki + terrain |
| WW1 | 3 | 3 | 2 | Wiki + terrain |
| Alcatraz | 5 | 10 | 3 | Retained |
| CityOfChicago | 10 | 14 | 6 | Wiki + terrain |

Training and 20thCenturyTown have no matching annotated pickup layout in the supplied archive and are outside this change. Custom/community map fallbacks are unchanged.

## Regeneration and checks

```powershell
py -3.12 tools/map_metadata/import_wiki_pickups.py --check
py -3.12 tools/map_metadata/extract_retail.py --check
py -3.12 -m pytest tests/test_wiki_pickups.py tests/test_crates_retail.py tests/test_map_resources.py tests/test_map_metadata.py tests/test_map_metadata_retail.py tests/test_pickups.py
```

The native checks cover the new points' support/headroom, non-overlap, compact-map Z normalization, stable initial placement and full respawn/fall cycles on every reconstructed map. All 26 maps were also instantiated through the native resource service and remained at their authored coordinates after ticks. The metadata comparison against the previous commit confirms that all 188 supported map/mode combinations retain their non-pickup metadata.
