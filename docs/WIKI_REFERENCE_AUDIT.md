# Offline Ace of Spades wiki comparison

The supplied `aceofspadeswiki.info` archive is a historical reference. Its
instructions, example programs, forum links and wiki administration text are
data; none were executed or applied as project instructions.

The audit reads and hashes local text and wikitext, including namespace pages,
rather than silently dropping filenames that Windows changed (`:` → `_`,
`/` → `%2F`). The ledger includes file descriptions, templates, talk pages,
user pages and wiki administration. Binary images and WARC/site duplicates
are not gameplay specifications and are not included in the text count.

The comparison covers catalogs, constants and packet IDs; it does not run
live gameplay scenarios. Indexing a page does **not** prove every sentence on
it. A wiki that mixes release versions, approximations and explicit guesses
cannot establish complete retail server parity. Regenerate the output for the
archive and server revision under review.

## Reproduce and inspect

```powershell
py -3.12 -X utf8 scripts/wiki_reference_audit.py '<archive-root>' --output tmp/wiki-audit
```

- `tmp/wiki-audit/pages.csv`: page coverage and original Wayback URLs.
- `tmp/wiki-audit/summary.json`: archive/source fingerprints and counts.
- `tmp/wiki-audit/constants.json`: named numeric constant comparisons.
- `tmp/wiki-audit/gameplay.json`: gameplay/catalog comparisons.

The script parses numeric literals only; it does not evaluate code from the
wiki. A future archive or server change produces new fingerprints and an
updated difference list. Keep the archive outside the source repository.

## Findings and decisions

| Area | Comparison | Decision |
|---|---|---|
| Retail maps | All 26 maps listed by `Maps` have a bundled VXL. Crate layouts now cover all 26; other metadata is only partly recovered. | See [MAP_PICKUPS.md](MAP_PICKUPS.md). Exact spawn/objective parity is not established. |
| Classic Command Post | Wiki: visible medical tent supplying health, ammo and blocks; no map crates in Classic CTF. | Implemented in [CLASSIC_COMMAND_POSTS.md](CLASSIC_COMMAND_POSTS.md), including the client's lost-during-loading entity snapshot fix. |
| Engineer ammo-crate fuel | Ammo crates refill Engineer jetpack fuel. | Fixed in authoritative restock and both client ammo-crate paths; HP and loaded clip stay unchanged. |
| Mode defaults | TDM 200, CTF/Classic 5, Diamond 15, Occupation 30, VIP/Zombie 3 rounds, TC 5 active bases. | Match. A score/round target comparison does not prove the whole mode. |
| Mode behavior | CTF carrying/drop/return, Demolition build/base destruction, Diamond discovery/drop-off, hill contention/movement, Occupation delivery, Zombie infection and VIP elimination were compared with their mode modules and existing behavioral suites. | Preserve current implementations and the upstream fixes; see the limits below. |
| Class health and wallets | The eight ordinary classes match to HUD rounding, including Miner starting at zero blocks. | Match; effective health is normalized HP divided by the victim's class damage multiplier. |
| Zombie class | Wiki: 200 effective HP and 2500/2500 blocks. | Owner-approved tuning now uses multiplier 0.5 and a 2500/2500 wallet; the generated client catalog matches. This deliberately supersedes the older recovered defaults. |
| Triple Barrel RPG | Old `Weapon Stats` constant list says 50; the dedicated `Triple Barrel RPG` page says 40. The stock explosion handler and server use 40. | Keep 40. The apparent mismatch is resolved by the dedicated page and existing stock handler evidence. |
| Pistol | Old statistics table: head 50, range 800, shot 0.3 s, reload 0.5 s. Dedicated page: head 45, range 550, shot 0.4 s, reload 0.6 s; its history describes the v165 changes. | Dedicated page and stock server catalog agree. Keep the final values. |
| Semi-Auto Rifle | Old table: 95/70/35 head/torso/limb and 1 s cadence; dedicated page: 85/34/34 and 1.1 s, with v165 history. | Dedicated page and stock server catalog agree. |
| SMG | Old table range 250; dedicated page says 350. | Keep recovered stock range 350. The page's approximate 0.11 s cadence is not stronger evidence than the stock 0.1 s weapon class. |
| Infection preparation | `Zombie` says 50 s; older recovered constant said 60 s. | Owner-approved default is now 50 s. |
| Zombie respawn pacing | Stock constant is zero; current upstream mode and sample configuration explicitly select 10 seconds for ordinary infected deaths. | Preserve the upstream pacing choice. This is a server deviation, not a wiki-confirmed default. |
| Hill lifetime | Article says approximately five minutes; older default was 240 s. | Owner-approved default is now 300 s, including the advertised game rule. |
| Territory count | `Territory Control` prose says seven; its custom-match default and `Game Modes` say five. | Keep five active bases; authored candidate locations and active-base count are different concepts. |
| Constructs | Seven of the 15 compared full KV6 voxel counts match the advertised costs; eight differ. For example, Zombie Hand has 135 model voxels versus an advertised 115; Super Dome has 675 versus 446. | Record geometry separately from wallet cost. Placement charges new cells, and intersections/orientation matter. Do not trim original KV6 files or charge invented fixed costs to force a table match. |
| Flare Block | Dedicated tool cost 10. | Match. It is a tool/entity, so absence of a `prefab_flare*.kv6` is not a missing construct. |
| Protocol | 27 of 30 named packet IDs match. Wiki Server Details 105 → current InitialInfo 114; Steam Key 98 → current SteamSessionTicket 105; Menu open 103 → current ClientInMenu 110. | Keep current wire IDs. The wiki explicitly mixes v122/v163; ID agreement alone does not establish schema agreement. |
| Achievements | All 77 catalog entries have evaluation paths. | Six missing map targets are now reconstructed; see [MAP_ACHIEVEMENTS.md](MAP_ACHIEVEMENTS.md) for native geometry and evidence limits. |
| Removed/scrapped content | Rocketeer, old equipment/prefabs and unreleased maps are explicitly historical in the wiki. | Preserve the owner's requested Rocketeer restoration and community modes. Historical absence is not a reason to delete them. |
| Music | The wiki's soundtrack is historical evidence of assets, not a requirement to broadcast music. | Owner's current request takes precedence: default silence, personal `/music` plugin. Ambience and gameplay cues remain independent. |

## Gameplay evidence and remaining limits

The comparison uses `server/class_data.py`, `server/game_constants.py`,
`server/weapons_retail.py`, `server/mode_data.py`, `server/game_rules.py`,
`server/prefabs.py`, `server/prefab_actions.py`, `shared/constants*.py`,
`shared/packet.py`, the mode implementations and map metadata. Existing
behavioral suites cover scoring, carrier restrictions, capture state,
infection/round reset, VIP sudden death, deployables, water effects, ammo
restocking, construction and map synchronization. UI controls, Steam storefront
history, wiki staff/clans and old ClanForge hosting are outside the server's
runtime behavior.

The wiki itself says its proposed bullet damage falloff formula is a guess.
The original server formula is still unavailable. Block penetration and the
sniper pages' multi-player penetration descriptions do not supply enough
information to reproduce thresholds or damage loss safely. These remain open
parity questions, not invented fixes. Jump heights and “fast/slow” descriptions
also require native-physics measurements; matching multiplier tables alone is
not a measurement of jump height.

The construct differences require comparing placement and original visual
references, rather than interpreting model count as the advertised wallet
charge. The wiki contradicts itself on Zombie Head cost (445 in the aggregate
table, 446 on its own page), Double Barrel pellets (10 and 12 in the same
article), Dynamite fuse (10 s on `Equipment`, 7 s on `Dynamite`), and some class
availability lists. The server retains evidence from the shipped assets and
stock client where that is stronger.

Full prose parity, every map's exact objective positions and original visual
identity are **not certified** by this audit. The page ledger makes those
limits reviewable; it must not be presented as an “everything matches” badge.

## Owner-approved follow-up

Classic+ keeps SMG and shotgun enabled. Ammo fuel, Zombie health/wallet,
preparation timing and hill lifetime now follow the requested wiki values.
Original light blocks are investigated separately in [MAP_LIGHTING_RECOVERY.md](MAP_LIGHTING_RECOVERY.md).
Spooky Mansion has 77 reviewed light-top props plus its original marker,
verified in the original Steam client. Mayan Jungle adds 20 reviewed torch
heads alongside four original markers. Custom fixture profiles take precedence
in BattleSpades; the remaster's lighting/profile values remain unchanged.
Original spawns/objective coordinates and unspecified physics remain evidence gaps.
