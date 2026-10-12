# Hardcore modes

Hardcore is a first-party variant beside each standard mode in the host GUI.
It keeps that mode's objectives, maps and bot objective policy while replacing
the human class roster, movement and equipment rules.

## Start a match

Reopen the server GUI to load the updated catalog, select a **Hardcore** entry,
choose a compatible map and start. Standalone example:

```powershell
py -3.12 run_server.py --config configs/hardcore/hc_tdm.toml
```

The ten presets enable six-player backfill with up to eight bots. Mode codes:
`hc_tdm`, `hc_ctf`, `hc_cctf`, `hc_zom`, `hc_vip`, `hc_mh`, `hc_tc`, `hc_dia`,
`hc_dem`, `hc_oc`. The longer `hardcore_` prefix is accepted too. Objective
settings continue to use the original `[modes.<base-code>]` section.

**Hardcore requires the updated BattleSpadesClient.** Retail and older native
clients are refused with the client-update reason. Standard and unofficial
modes retain their existing client compatibility. The capability is BSCF v3;
InitialInfo advertises `BATTLESPADES_HARDCORE=1`. Flight remains BSFP v2.
Known native hosts and Hardcore browser entries advertise support immediately.
An unknown direct-IP host receives a stock ticket first; an update refusal
before InitialInfo triggers one retry with the capability trailer.

## Classes

| Role / visual model | Weapons | Distinction | Walk / sprint, blocks per second |
| --- | --- | --- | --- |
| Soldier / Deuce | Assault rifle, SMGs; pistol | Extra spawn ammunition | 4.46 / 6.69 |
| Heavy / Commando | LMG, double barrel; pistol | 20% less body-bullet damage | 3.85 / 5.78 |
| Recon / Marksman | Sniper rifles; pistol or automatic pistol | Long-range kit | 4.46 / 6.69 |
| Demolitionist / Miner | SMGs, pump/classic/auto shotgun; pistol | C4 or dynamite | 4.29 / 6.43 |
| Medic / Medic | SMGs, pump/classic/auto shotgun; pistol | Medpacks | 4.46 / 6.69 |

Everyone has blocks, a shovel, a parachute, and a choice of frag or incendiary
grenades. Normal tool ammunition capacities apply. Objective carry items remain
available. Equipment changes apply on the next life. The client menu shows role
names, short role descriptions and a Grenades row.

All five roles have 100 displayed HP. Heavy has approximately 125 effective HP
against body bullets; headshots, melee and explosions bypass that mitigation.
Stock model weaknesses and helmet bonuses do not carry into these human roles.
Medpacks wait three seconds after damage and share a two-second healing cooldown
per recipient, so several packs cannot heal simultaneously.

No jetpacks, miniguns, drills, block suckers, super spades, disguise tools,
turrets, chemical equipment or prefab loadouts. The authoritative server validates
the same class kits it advertises in the menu, including bot kits.

## Movement and scoring

All five human models share walk/sprint/crouch ratios, gravity, water friction,
fall-damage thresholds and a jump impulse. They automatically step one block,
jump onto two-block ledges and cannot clear a three-block wall by crouch jumping.
Role speed is applied once, rounded to the protocol's 1/64 units. Movement is
predicted by the updated client and simulated by the server with matching values.
Zombie mode retains the infected class and its normal movement; survivors use
the five Hardcore roles.

Gravestones and corpse explosions are off. Body tracking remains available for
teabag scoring: three crouches near an enemy body, each less than half a second
apart, award two personal points once per player for that death. Minimap, death camera,
spectators and colour picker are enabled; sniper beams are disabled.

Join messages explain the objective and rules. `/hcrules` repeats them. VIP
promotion preserves a living player's class and health instead of replacing the
body. All ten variants retain the existing mode-specific bot objective logic.

## Scope and verification

Hardcore rules are a scoped overlay; global retail class tables and saved normal
rules are unchanged. The extra runtime work is class/profile selection, small
combat/healing checks and existing corpse tracking. There is no additional bot
worker or terrain scan per player. No new full-server capacity benchmark has
been claimed.

Regression coverage includes all ten mode starts, bot admissions, native menu
packet round-trips, forged kit filtering, healing, damage, death effects,
teabag scoring, VIP promotion, Zombie transitions and per-class movement walls.
Native tests cover the predictor, protocol capability and role presentation.
These checks do not replace multiplayer balance playtesting.
