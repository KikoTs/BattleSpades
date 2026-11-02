# Reconstructed map achievement targets

The six previously missing rules now have explicit targets, reconstructed
from the bundled VXLs and original screenshots in the [firsthand achievement
guide](https://steamcommunity.com/sharedfiles/filedetails/?id=135523055).
These are not recovered original server volumes. Existing recovered regions
on Dragon Island, Mayan Jungle and Spooky Mansion remain unchanged.

| Map / achievement | Reviewed runtime target | Conditions |
|---|---|---|
| Ancient Egypt / Nose Job | Two nose voxels: `(260,250,195..196)` | Destroy both; the face and beard do not count |
| London / Death Toll | 75 bell voxels, x348..354, y235..241, z115..120 | Destroy the bell, directly or through collapse; tower walls are excluded |
| Lunar Base / Kubrick Equation | 36 black monolith voxels, x304..307, y458, z221..229 | Rocket participation required |
| Great Wall / The War on Terracotta | Nine figures, 234 voxels, x309/312/315, y191/197/203, z178..186 | Participate in destroying every figure |
| Castle Wars / Pillar Assault | Twelve wooden support cores per castle, 24 voxels per core | Demolition only, enemy castle only; each team's objective is independent |
| The Colosseum / The Undertaker | 17,359 original air cells below the covered arena floors | Ten enemy kills from the tunnel mask per round; surface, solid walls and outside positions excluded |

The guide says six terracotta statues. The bundled map actually has a 3×3
formation. The reconstruction follows the terrain and the achievement's
“Destroy the Terracotta Army” description. Castle Wars is identified by the
guide, rather than guessing a map from the old `map_concrete_destroy` API name.

`tools/map_metadata/author_achievement_regions.py --check` regenerates the
targets deterministically in memory and checks the sidecars. Terrain hashes
prevent silently authoring targets against a changed VXL. `ac_cells` stores
exact source-space cells so unrelated blocks inside a bounding box do not
become destruction requirements. The engine shifts both bounds and masks
once: Lunar Base by 10, The Colosseum by 111 blocks.

Destroyed or collapsed target cells count as gone. Construction does not add
new target cells. A same-map restart cannot award an already razed objective;
one empty castle disables only that castle's eligible-team group. Overlapping
kill regions count an enemy once. Existing weapon, mode, live-round and
human-contributor checks remain in force.

Native-map tests exercise completion, team separation, coordinate shifts,
outside/above-ground rejection, overlapping masks, empty targets, and strict
mask parsing. Screenshot interpretation cannot certify the original server's
exact hidden volume boundaries or its rules for partial participation.
