# Kill feed, death screen and environment damage (retail parity, 2026-09-26)

What the stock client does with `KillAction` (46) and `SetHP` (5), how that
was established, and what the server now sends.

## Evidence

Headless IDA 9.1 on the stock Steam binaries (copies in the session
scratchpad, never the install): `aoslib.scenes.main.gameScene.pyd`,
`aoslib.hud.hud.pyd`, `aoslib.world.pyd`. Cython interned-string globals were
named from the string table (entry = `{PyObject **p; char *s; ...}`, so the
dword before each string reference is the global). Obfuscated constants
(`A4xx`, `A98x`, ...) resolve through `shared/constants.py`. Python source
line numbers come from the `mov ebx, <line>` before each error branch.

A constant that appears in `shared/constants.py` but in **no** client binary
(`grep -c -a <name>` over every `aoslib.*.pyd` / `shared.*.pyd`) is
server-side retail logic. That settles two things:

- `MULTIKILLMAXTIMEGAP = 6.0` (A885): absent from the client, so it is the
  retail server's multikill window.
- `TOOLS_KILL_TYPE` (A458): absent from the client, so it is the retail
  server's tool -> kill-type table (melee tools map to `WEAPON_KILL`; the
  HUD draws `WEAPON_KILL` and `MELEE_KILL` identically).

`JETPACK_PROPERTIES` (A2128) is read only by `hud.pyd` (fuel bar). Its
`JETPACK_DAMAGE_MULTIPLIER` / `JETPACK_DEATH_ACCELERATION` fields were
server-side and their use is not recoverable from the client; the server does
not apply them.

## GameScene.process_packet_kill_action (gameScene.pyd 0x10194940)

```
kill_type = packet.kill_type
player = self.get_player_connection(packet.player_id)          # 3668
killer = self.get_player_connection(packet.killer_id)          # 3669
if killer and kill_type in (FORCED_TEAM_CHANGE_KILL, TEAM_CHANGE_KILL):
    killer.dominatingLocalPlayer = killer.dominatedByLocalPlayer = False
if player:
    if player.character and kill_type in (FORCED_TEAM_CHANGE_KILL, TEAM_CHANGE_KILL):
        player.dominatingLocalPlayer = player.dominatedByLocalPlayer = False
    player.character.set_dead(...)
if player.main: self.media.play(<death sound>)                 # 3680
self.hud.add_kill(player, killer, kill_type)                   # 3682
if player == self.player:
    player.character.set_respawn_time(packet.respawn_time)     # 3684
if killer:
    if killer == self.player and killer != player:             # 3688 local kill
        player.running_local_player_kills = 0
        if packet.isRevengeKill:                                # YOU_GOT_REVENGE
            add_big_message(...); player.dominatingLocalPlayer = False; sound
        if packet.isDominationKill:                             # YOU_ARE_DOMINATING
            add_big_message(...); player.dominatedByLocalPlayer = True; sound
        kill_count 2/3/4/5 -> KILL2/KILL3/KILL4/KILL5, >5 -> KILLM.format(n)
    elif player == self.player:                                # 3708 local death
        killer.running_local_player_kills += 1
        if packet.isRevengeKill:                                # THEY_GOT_REVENGE
            add_big_message(...); killer.dominatedByLocalPlayer = False; sound
        if packet.isDominationKill:                             # THEY_ARE_DOMINATING
            add_big_message(...); killer.dominatingLocalPlayer = True; sound
if player == self.player:                                      # 3720+
    camera_manager.controllers[DEATH_CAMERA].set_killer_info(
        kill_type, packet.killer_id, killer.world_object.position,
        killer.running_local_player_kills)
```

So the client decides nothing about multikills or dominations: the server
must supply `kill_count` and both flags. `running_local_player_kills` (the
death-cam streak, `DEATHCAM_STREAK_FOR_*`) is client-local.

Strings: `KILL2` Double Kill, `KILL3` Triple Kill, `KILL4` 4 x Multi Kill,
`KILL5` 5 x Multi Kill, `KILLM` {0} x Multi Kill, `YOU_ARE_DOMINATING`,
`THEY_ARE_DOMINATING`, `YOU_GOT_REVENGE`, `THEY_GOT_REVENGE`.

## HUD.add_kill icons (hud.pyd 0x1008CD70)

| kill_type | icon |
|---|---|
| 0 WEAPON, 2 MELEE | killer's currently held tool (`character.get_weapon` / `get_tool().tool_id`); none -> "no image for no weapon found kill" |
| 1 HEADSHOT | `KILL_IMAGES['headshot']` |
| 3 GRENADE / 22 CLASSIC_GRENADE / 23 ANTIPERSONNEL | GrenadeTool / ClassicGrenadeTool / AntipersonnelGrenadeTool |
| 4 ROCKET | RocketTurretWeapon if killer's class loadout holds ROCKET_TURRET_TOOL, else RPGWeapon |
| 5 ROCKET2 / 6 DRILL | RPG2Weapon / DrillgunWeapon |
| 7 FALL | `KILL_IMAGES['fall']` |
| 8 FORCED_TEAM_CHANGE, 9 TEAM_CHANGE | `KILL_IMAGES['change_team']` |
| 10 CLASS_CHANGE | `KILL_IMAGES['change_class']` |
| 11 ENTITY | none ("no image for entity kill") |
| 12 CORPSE / 13 GRAVE | `corpse` / `grave` |
| 14 LANDMINE / 15 DYNAMITE | LandmineWeapon / DynamiteWeapon |
| 16 AIRSTRIKE / 19 SHRAPNEL | `airstrike` / `shrapnel` |
| 17 BOMB / 18 ROCKET_TURRET | BombTool / RocketTurretWeapon |
| 21 SNOWBALL | SnowBlowerWeapon |
| 24 MOLOTOV, 25 BLOCKFIRE | MolotovWeapon |
| 26 VIP_MODE | `sudden_death` |
| 31..36 | ChemicalBomb / GrenadeLauncher / RadarStation / StickyGrenade / MineLauncher / C4 weapon images |
| 20, 27-30 | no branch (UGC types are the client's own gap) |

The same table is `server/kill_feed.py:HUD_KILL_ICONS`.

## Server death paths (kill_type sent)

| Death | Retail expectation | Server | Status |
|---|---|---|---|
| Hitscan body shot | WEAPON_KILL | WEAPON_KILL | ok |
| Hitscan headshot (all rifles/snipers) | HEADSHOT_KILL | HEADSHOT_KILL | ok |
| Melee (spade/pickaxe/knife/crowbar/...) | WEAPON_KILL (TOOLS_KILL_TYPE) | MELEE_KILL | same icon and death cam; kept for melee scoring |
| Grenade family / RPG / RPG2 / drill / snowball / sticky / chemical / GL / mine / C4 | own types | own types (projectiles.py) | ok |
| Dynamite / landmine | DYNAMITE_KILL / LANDMINE_KILL | same | ok |
| Rocket turret rocket | ROCKET_KILL (TOOLS_KILL_TYPE) or ROCKET_TURRET_KILL | ROCKET_TURRET_KILL | same icon; not a death-cam type |
| Entity destruction blast (MG, turret) | ENTITY_KILL | ENTITY_KILL | ok (client draws no icon) |
| Corpse / grave explosion | CORPSE_KILL / GRAVE_KILL | same | ok |
| Airstrike / bomb | AIRSTRIKE_KILL / BOMB_KILL | same | ok |
| Molotov impact / burning | MOLOTOV_KILL / BLOCKFIRE_KILL | same | ok |
| Fall | FALL_KILL, killer = victim | same | ok |
| Team change / auto-balance | TEAM_CHANGE_KILL / FORCED_TEAM_CHANGE_KILL | TEAM_CHANGE_KILL for both | same icon and flag reset |
| Class change | CLASS_CHANGE_KILL | same | ok |
| `/kill` (server command, no retail equivalent) | - | TEAM_CHANGE_KILL | change_team icon |
| VIP sudden death | VIP_MODE_KILL + SetHP type 4 | same | ok |
| Disconnect | no KillAction (PlayerLeft) | same | ok |
| Drowning / water damage / lava | does not exist in the client | none | ok |

## SetHP.damage_type (process_packet_set_hp, gameScene.pyd 0x10191E90)

`A981..A985 = 0..4`: 1 hit (hit sound, `character.hit_time`, direction
arrow from `source_x/y`), 2 heal (`heal_hp_added`), 3 burn (burn sound,
`character.burn_time`, `BURN_INDICATOR_TIME`), 4 sudden death
(`sudden_death_damage_time`); 0 is a plain HP update.

## Environment damage

- Fall: `aoslib/world.pyx` matches world.pyd sub_10012B80 (checked again):
  `fall = fall_distance * gravity`, x0.75 for jetpack types 1-4 while passive,
  ratio `(fall - min) / (max - min)` clamped to [0, 1], damage
  `int(max_damage * ratio)`, x class fall-on-water multiplier above z 237,
  returned even on a soft (<= 0.24) landing. Per-class min/max/damage come
  from the class tables; `RULE_ENABLE_FALL_ON_WATER_DAMAGE` off passes a zero
  water multiplier.
- Burning: 10 s (`BLOCKFIRE_CHARACTER_DURATION`), 2.5 HP every 0.3 s,
  refreshed while within 3 blocks of block fire, ends in water (`wade`).
- No drowning, water or lava damage exists in the retail client.

## Fixes (2026-09-26)

1. `kill_count` was the killer's life streak; it is now the multikill chain
   (gaps <= 6.0 s, reset when the killer dies), `server/kill_feed.py`.
2. `isDominationKill` / `isRevengeKill` were always 0. Domination = 4th
   unanswered kill on one enemy (retail threshold not recoverable; 4 is the
   convention for this HUD model); revenge = killing an enemy who dominates
   you. Relations drop on TEAM_CHANGE / FORCED_TEAM_CHANGE kills (as the
   client does), on disconnect (id reuse) and on round reset.
3. The stored `last_kill_action_data` (roster replay to late joiners) zeroes
   the banner fields so a joiner that inherits a freed id never gets a stale
   "Double Kill" / domination banner.
4. Burn damage sent SetHP type 1 (hit arrow toward the thrower); it now sends
   type 3 (burn indicator). `Player.damage(..., hp_damage_type=)`.
5. A fractional mode respawn delay is rounded up in `respawn_time`.

Live check (dev client, console 32914, two bots): Double/Triple/4 x Multi
Kill, "You are dominating", "... got revenge on you", "... is dominating
you", "You got revenge on ..." all shown by the stock handler; after the
sequence every scoreboard flag was consistent; burning set
`character.burn_time` and left `hit_time` untouched.

## Not retail, outside this pass

- `plugins/example_plugin.py` is loaded by default (`[plugins] enabled`,
  empty allowlist) and broadcasts "X is on a spree! (3 kills)" /
  "X is dominating! (5 kills)" big messages on top of the retail banners.
- `GENERIC_SCORE_REVENGE` / `KILL_SCORE_REVENGE_REASON` ('Death Revenge')
  and `GENERIC_SCORE_PAYBACK` are not awarded by any mode.
- Auto-balance and zombie conversion send TEAM_CHANGE_KILL where the
  constant FORCED_TEAM_CHANGE_KILL exists (identical on screen).
