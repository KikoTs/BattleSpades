# Achievements

The original game had 77 Steam achievements. Steam's schema marks every one
of them as set by the game server, and no client binary contains a rule for
any of them: the retail server evaluated them, and that server is not
available. `server/achievements.py` rebuilds the rules from the achievement
descriptions, which are the only specification left.

71 of the 77 are implemented. The other six are map achievements
whose volumes were never recovered ([Not implemented](#not-implemented)).

They are evaluated on **every** server. The engine reads no official, ranked,
identity or write-token setting, so a player's own Create Match server
(`revival.official = false`, no `AOS_MASTER_WRITE_TOKEN`, bots for opponents)
unlocks them like any other. `tests/test_achievements.py` pins that.

This server does not talk to Steam. An unlock is saved in the server's own
store, announced in game, and offered to the AoSPlay master with the round
result.

## How it works

- **Catalog.** `shared/achievement_table.py` holds the 77 schema rows: token,
  API name, displayed name, description, and for 35 of them the Steam
  statistic and threshold. There are 32 statistics; three are shared by two
  tiers (`sniper_kill_count` 25/50, `distance_run` 21000/42000,
  `intel_defence_count` 5/10).
- **Engine.** `AchievementEngine` keeps one integer counter per player and
  statistic and the set of unlocked achievements. `engine.add(player, stat,
  amount)` adds to a counter and unlocks every tier it now reaches;
  `engine.unlock(player, api_name)` unlocks a one-shot achievement;
  `engine.unlocked(identity)` and `engine.progress(identity)` read them back.
  Unlocks are idempotent.
- **Hooks.** Gameplay reports facts through the module-level functions at the
  bottom of `server/achievements.py`; the engine owns the rules, thresholds
  and names. The call sites:

  | Where | What it reports |
  |---|---|
  | `Player.damage`, `Player.die` (`server/player.py`) | the last hit on a life; every death with killer, kill type and whether the victim was jetpacking |
  | `CombatSystem._end_shot_tally`, `_collapse_unsupported` (`server/combat_runtime.py`) | whether a trigger pull damaged anybody; every removed block, the structures that collapsed, and the gun being fired |
  | `BattleSpadesServer._apply_blast` (decorator), `_explode_projectile`, `_apply_drill_contact` (`server/main.py`) | one explosion's scope, direct projectile hits, the drill's bore |
  | `RocketTurretController._fire`, `ProjectileEngine._advance_contact` | which turret fired at whom; which player a projectile struck |
  | `_counted` (`server/map_resources.py`) | what a health or ammo crate restored |
  | `SimulationRuntime.step` | the per-tick clock (distance, air time, last-man time, saves) |
  | `BaseMode.on_mode_start` / `on_mode_end` | match start and end |
  | `modes/zombie.py`, `vip.py`, `diamond_mine.py`, `multi_hill.py`, `demolition.py`, `occupation.py`, `ctf.py` | mode events (rounds, pickups, drops, cash-ins, hill control, bombs, intel) |

- **Unlock.** The unlock is written to the store first. Then the server
  broadcasts the retail `LocalisedMessage` (packet 50) `ACHIEVEMENT_GAINED`,
  `{0} has unlocked the "{1}" achievement`, with the player's name and the
  achievement's displayed name, to every in-game client.
- **Isolation.** Every hook is guarded. A failure is counted in
  `engine.faults`, logged once per hook with its traceback, and never reaches
  the kill, blast or round that called it. A store that cannot be written is
  retried; the unlock is announced only once it is saved.
- **Lifetime.** `BattleSpadesServer.__init__` creates the engine inert: no
  file is opened and every hook is a no-op. `start()` opens the store before
  the first mode starts; `stop()` flushes and closes it.

## Who earns, and when nothing counts

- Only human players earn achievements. Bots never do, and never appear in
  the store.
- Kills of bots count. `count_bot_kills = false` turns that off for every
  kill-based rule, for direct rocket hits on bots and for interceptions of bot
  carriers.
- A kill is a kill the server credits to you against a player of the other
  playable team. Team kills, suicides and team/class-change deaths never
  count.
- Nothing counts on the end screen, in the intermission between Zombie or VIP
  rounds, in the tutorial, or in the Map Creator.

## Scopes

- **For life**: the Steam statistics. They are saved and survive restarts.
- **One match** ("in one game", "in one match", and "in one round" outside
  Zombie and VIP): from a mode start to its end. A same-map restart is a new
  match.
- **One round** in Zombie and VIP: one infection round, one VIP round.
- **Without dying**: the server's kill streak, which a death resets.

Match, round and streak progress is kept on the connection and is lost when
the player leaves.

## Identity and persistence

Progress belongs to an identity, not to a connection. The first that applies:

1. `aosplay:<legacy id>`: the AoSPlay account the master verified for the
   join ticket (the id the round statistics are keyed by).
2. `steam:<SteamID>`: the Steam relay's identity for the peer.
3. `name:<name>`: the name, case-folded. A direct legacy client has nothing
   else. Two people who use one name on a server share its progress, and a
   player renamed for a duplicate name (`Kiko~2`) is a different identity for
   that session.

The store is one SQLite file, `state/achievements.sqlite3` by default:

```
counters(identity, stat, value)                         -- lifetime statistics
unlocks(identity, api_name, unlocked_at, player_name, reported_at)
```

- Counters are written as increments and unlocks with `INSERT OR IGNORE`, so
  several server processes can share one file: they add to each other's
  progress, and an achievement another process already recorded is not
  announced a second time.
- Unlocks are written immediately. Counter increments are batched: every 10 s,
  before each unlock, when a player leaves, at match end and at shutdown. A
  crash can lose up to 10 s of counter progress, never an unlock.
- A player's counters are read when first needed and dropped from memory when
  the player leaves.
- Writes happen on the gameplay thread. They are small, and the file is in
  WAL mode with `synchronous=NORMAL`, so a commit does not wait for a disk
  sync. A file locked by another process fails after 50 ms and is retried.
- If the file cannot be opened the engine logs a warning and uses an
  in-memory store: unlocks are still announced but do not survive a restart.
- The path is resolved against the application root like the other state
  files. The Docker entrypoint rebases a relative path below the data volume
  (`/data/state/achievements.sqlite3`).

Preserve the file across upgrades. It is the only copy of every player's
progress on that server.

## Configuration

```toml
[achievements]
enabled = true
count_bot_kills = true
path = "state/achievements.sqlite3"
```

| Key | Meaning |
|---|---|
| `enabled` | `false` leaves the engine inert: nothing is counted, saved or announced. |
| `count_bot_kills` | Whether kills of bots count. Bots never earn achievements either way. |
| `path` | The SQLite store. `":memory:"` keeps nothing across restarts. |

There is deliberately no official, ranked or token setting.

## How the descriptions are read

The rule applied to each achievement is in the table below. The conventions
behind them:

- **Weapons** are named the way the retail profile statistics label them
  (`server/profile_stats.py`): "the spade" is the spade and the classic spade
  but not the super spade; "the pistol" is the pistol and the snub pistol;
  "the shotgun", "the SMG", "the minigun" and "the classic rifle" are that one
  tool; "the sniper rifle" is not the semi-auto, which has its own
  achievement; a "grenade" is the grenade and the classic grenade; a "rocket
  launcher" is the RPG and the RPG2. The sets are constants at the top of
  `server/achievements.py`.
- **Headshots** are headshot kills. The statistic behind "25 headshots with
  the sniper rifle" is named `sniper_kill_count`.
- **A structure** is one unsupported component the collapse pass brings down
  (`WorldManager.find_unsupported_chunks`); its size is its block count.
- **Destroyed blocks** include the blocks of collapses the player causes.
- **Zombies and survivors** are the two teams of a Zombie round in progress.
- **In the water** is the mover's wading state; **airborne** and **using the
  jetpack** are the server's own flags for the player.

## All 77

| API name | Name | Retail description | Counter | Status | Rule applied |
|---|---|---|---|---|---|
| `zombie_survivor` | Apocalypse Later | Survive to the end of a round of Zombie | one-shot | implemented | Zombie. You are alive on the survivor team when a round ends in a survivor win. |
| `zombie_kills_humans` | Three of a Kind | Take out 3 survivors in 1 round | one-shot | implemented | Zombie. As a zombie, 3 survivor kills in one round. |
| `zombie_mvp` | Stone Cold Killer | Start the round as a zombie, and wipe out the highest number of enemy players | one-shot | implemented | Zombie. You were one of the round's first infected (not a later replacement), made at least one kill, and no player made more kills that round (a tie counts). |
| `zombie_fall` | Dead Drop | Playing as a zombie, make an enemy player fall to their death | one-shot | implemented | Zombie. As a zombie, a survivor's fall death is credited to you. The server credits a fall to the last enemy who damaged that life within 5 s. |
| `vip_mvp` | The Main Man | Start the round as VIP and kill the highest number of enemy players | one-shot | implemented | VIP. You were the VIP chosen at the start of the round, made at least one kill, and no player made more kills that round (a tie counts). |
| `vip_pacifist` | I am the passenger | Start the round as VIP, kill no enemy players, and survive anyway | one-shot | implemented | VIP. You were the VIP chosen at the start of the round, are still that VIP and alive when it ends, and made no kill in it. |
| `diamond_hotfoot` | Diamond Geezer | Pick up a diamond within 5 seconds of it spawning, and make it to a dropoff point without dropping it | one-shot | implemented | Diamond Mine. Pick up a diamond nobody has carried within 5.0 s of its appearing and cash it in without dropping it. |
| `diamond_collector` | Girl's Best Friend | Find and drop off 3 diamonds in one match | one-shot | implemented | Diamond Mine. Cash in 3 diamonds you uncovered yourself, in one match. |
| `diamond_thief` | Feeling Flush | Steal and drop off 5 diamonds found by the enemy team before they pick them up | `diamond_thief_count` >= 5 | implemented | Diamond Mine. Be the first to pick up a diamond an enemy uncovered, and cash it in yourself. |
| `diamond_interceptor` | King of Diamonds | Intercept 10 diamonds carried by the enemy team and make it to a dropoff point | `diamond_interceptor_count` >= 10 | implemented | Diamond Mine. Pick up a diamond the enemy team carried last and cash it in without dropping it. |
| `hill_greedy` | One Man Army | Be the only player to score points from a hill before it vanishes | one-shot | implemented | Multi-Hill. When a hill times out, you are the only player (bots included) who earned any personal hill score from it. |
| `hill_strike_survivor` | Shock and Awe | Trigger 5 airstrikes and survive | `hill_strike_survivor_count` >= 5 | implemented | Multi-Hill. You are on a hill when it times out, which calls the airstrike on it, and are alive once its shells have landed. |
| `hill_strike_stay_put` | Strikebreaker | Trigger an airstrike and survive without leaving the hill | one-shot | implemented | Multi-Hill. The same strike, and you never left the hill's zone before the shells had landed. |
| `hill_interceptor` | Cubic Heir | Take control of 10 contested hills | `hill_interceptor_count` >= 10 | implemented | Multi-Hill. You are on a hill when a contest (both teams on it) ends with only your team left, on a hill your team did not own. |
| `hill_defender` | Tor Defence | Retain control of 10 contested hills | `hill_defender_count` >= 10 | implemented | Multi-Hill. The same, on a hill your team already owned. |
| `hill_kill_shooting_out` | Mountain Casualties | Take out 50 enemy players while you are in an active hill | `hill_kill_shooting_out_count` >= 50 | implemented | Multi-Hill. An enemy kill while you are inside an active hill. |
| `hill_kill_shooting_in` | Butte Hurt | Take out 50 enemy players while they are in an active hill | `hill_kill_shooting_in_count` >= 50 | implemented | Multi-Hill. An enemy kill while the victim is inside an active hill. |
| `bomb_survivor` | Bombs Away! | Drop a bomb in the enemy base and survive | one-shot | implemented | Occupation. As an attacker, the bomb you carried last explodes inside the base and you are alive after the blast. |
| `bomb_hotfoot` | Hotfoot | Pick up a bomb from a spawn point and make it to the enemy base without dropping it | one-shot | implemented | Occupation. As an attacker, pick up a bomb nobody has carried and make its first drop inside the enemy base. |
| `bomb_interceptor` | Bomb Disposal Expert | Intercept 5 bomb carriers and defend the bomb until it detonates early. | `bomb_interceptor_count` >= 5 | implemented | Occupation. Kill an attacker who carries the bomb; the attackers do not pick it up again and it explodes outside the base. |
| `bomb_final_bomber` | Bomb the Base | Be the final player to plant a bomb successfully, and then win the round. | one-shot | implemented | Occupation. You are the last attacker whose bomb exploded inside the base, and your team wins the match. |
| `demolition_repair` | Fort Process | Repair 100 blocks of damage to your base | `demolition_repair_count` >= 100 | implemented | Demolition. Blocks you rebuild into damaged cells of your own base (the mode's repair credit: refilling a hole your own team dug does not count). |
| `demolition_damage_one_round` | Block Party | Cause 100 blocks of damage to the enemy base in one round | one-shot | implemented | Demolition. 100 enemy base cells destroyed in one match, collapses you cause and drill bores included. |
| `demolition_damage_many_rounds` | Attack the Blocks | Cause 500 blocks of damage to the enemy base over multiple rounds | `demolition_damage_many_rounds_count` >= 500 | implemented | Demolition. The same cells, added up for life. |
| `demolition_final_damage` | Put the Boot in | Be the final player to cause damage to the enemy base and win | one-shot | implemented | Demolition. You destroyed the last enemy base cell of the live round and your team wins it. |
| `minigun_demolish` | Riddle Me This | Demolish a structure of 50 blocks or more using the minigun | one-shot | implemented | A minigun bullet removes a block and one unsupported structure of 50 or more blocks collapses. |
| `rocket_fall` | Splashback Cashback | Make someone fall to their death using rocket launcher knockback | one-shot | implemented | An enemy's fall death is credited to you and your last hit on that life was a rocket launcher blast (RPG or RPG2). |
| `pistol_zombie_kill` | Face Off | Kill 5 zombies by shooting them in the face with the pistol | `pistol_zombie_kill_count` >= 5 | implemented | Zombie. A headshot kill of a zombie with the pistol (pistol or snub pistol). |
| `grenade_demolish` | Holey Hand Grenade | Demolish 5 structures of 100 or more blocks using a grenade | `grenade_demolish_count` >= 5 | implemented | Each unsupported structure of 100 or more blocks that collapses from your hand grenade (grenade or classic grenade). |
| `spade_kill` | Dig Deep | Kill 10 enemies with the spade | `spade_kill_count` >= 10 | implemented | A melee kill with the spade (spade or classic spade). |
| `sniper_accuracy` | Hat Trick | 3 kills with sniper rifle without missing a shot | one-shot | implemented | 3 sniper rifle kills in a row with no sniper shot in between that damaged nobody. |
| `sniper_kill` | Head Hunter | 25 headshots with the sniper rifle | `sniper_kill_count` >= 25 | implemented | A headshot kill with the sniper rifle. |
| `landmine_hidden` | Landmine Craft | Kill 5 players with landmines placed on the ground and re-buried | `landmine_hidden_count` >= 5 | implemented | A kill by your landmine whose own cell was solid when it went off: a block had been placed over it. |
| `pickaxe_kill` | Take Your Pick | Kill 10 enemies with the pickaxe | `pickaxe_kill_count` >= 10 | implemented | A melee kill with the pickaxe. |
| `jetpack_kill` | Rocket Man | Kill 20 enemies while using the jetpack | `jetpack_kill_count` >= 20 | implemented | Any enemy kill credited to you while your jetpack is active. |
| `jetpack_killed_using` | Jet Fighter | Kill an enemy while he uses a jetpack | one-shot | implemented | Kill an enemy whose jetpack was active when he died. |
| `jetpack_smg_kill` | Jetpack Drive-by | Kill 10 enemies with the SMG while using the jetpack | `jetpack_smg_kill_count` >= 10 | implemented | An SMG bullet kill while your jetpack is active. |
| `shotgun_headshots` | Heads You Win | 10 headshots with the shotgun | `shotgun_headshots_count` >= 10 | implemented | A headshot kill with the shotgun. |
| `drillgun_demolition` | All Your Base | Destroy 1000 blocks of enemy bases in demolition mode using the drill gun | `drillgun_demolition_count` >= 1000 | implemented | Demolition. Enemy base cells destroyed by your drill: its bore, its final blast and the collapses they cause. |
| `dynamite_below` | Floored Genius | Kill 10 enemies by placing dynamite below them then blowing out the floor | `dynamite_below_count` >= 10 | implemented | A kill by your dynamite whose centre was at least half a block below the victim's feet, or that victim's fall death right after such a hit. |
| `turret_accuracy` | Turret Syndrome | Kill 10 enemies with 1 turret | one-shot | implemented | 10 kills by the rockets of one of your turrets. |
| `turret_evasion` | Collateral Damage | Destroy 100 blocks by evading an enemy turret | one-shot | implemented | In one match, 100 blocks destroyed by enemy turret rockets that were fired at you and did not damage you. |
| `ammo_drop_greedy` | Loaded | Collect a full resupply of ammunition in one game | one-shot | implemented | In one match, ammo crates add to one weapon's reserve at least that weapon's full reserve capacity. |
| `health_drop_greedy` | Well Heeled | Repair 150 health in one game using health drops | one-shot | implemented | In one match, health crates restore 150 HP to you in total. |
| `map_greatwall_destroy` | The War on Terracotta | Destroy the Terracotta Army | one-shot | not yet | no retail volume survives for this map |
| `map_moon_destroy` | Kubrick Equation | Lunar Base: Destroy the monolith with the rocket launcher | one-shot | not yet | no retail volume survives for this map |
| `map_london_destroy` | Death Toll | Destroy the large bell at the top of Big Ben | one-shot | not yet | no retail volume survives for this map |
| `map_maya_kill` | Personal Sacrifice | Melee kill an enemy while standing near the temple altar | one-shot | implemented | MayanJungle. A melee kill while you stand in the recovered altar volume. |
| `map_concrete_destroy` | Pillar Assault | In demolition mode, destroy all the pillars in the enemy building lobby | one-shot | not yet | no retail volume survives for this map; which map "concrete" is was not recovered either |
| `map_egypt_destroy` | Nose Job | Destroy the nose of the Sphinx | one-shot | not yet | no retail volume survives for this map |
| `map_colosseum_kill` | The Undertaker | Kill 10 enemies while hiding in the tunnel under the arena | one-shot | not yet | no retail volume survives for this map |
| `map_zombieisland_zombie_kill` | Brain Drain | Kill 5 zombies while hiding in the basement | one-shot | implemented | SpookyMansion, Zombie. 5 zombie kills in one round as a survivor standing in the recovered basement volume. |
| `map_zombieisland_destroy` | Tower Offense | Playing as a zombie, demolish the mansion's towers | one-shot | implemented | SpookyMansion, Zombie. Every block the two recovered tower volumes held at match start is gone, and you destroyed blocks of both while a zombie. |
| `misc_half_marathon` | Half Marathon | Run 21 kilometers | `distance_run` >= 21000 | implemented | Blocks moved on the ground while alive (1 block = 1 m). Airborne movement and steps over 2 blocks (teleports, respawns) do not count. |
| `misc_marathon` | Marathon Man | Run 42 kilometers | `distance_run` >= 42000 | implemented | The same counter. |
| `misc_five_in_a_row` | Five Alive | Get five kills in a row without dying | one-shot | implemented | Your kill streak reaches 5 in one life. |
| `misc_triple_explosion` | The Big Bang | Kill 3 enemies with 1 explosion | one-shot | implemented | 3 enemy kills by one explosion of yours. |
| `map_isleofdoom_destroy` | Exit The Dragon | Dragon Island: Destroy the stone dragon | one-shot | implemented | DragonIsland. Every block the recovered dragon volume held at match start is gone; everyone who destroyed part of it is credited. |
| `intel_defence_easy` | Stop, thief! | Intercept 5 enemies stealing your intel and defend it until it returns to base. | `intel_defence_count` >= 5 | implemented | CTF and Classic CTF. Kill the enemy who carries your team's intel; it then returns to base (timer or touch) without an enemy picking it up again. |
| `intel_defence_hard` | Undelivered Message Report | Intercept 10 enemies stealing your intel and defend it until it returns to base. | `intel_defence_count` >= 10 | implemented | The same counter. |
| `classic_rifle_headshots` | Open Your Mind | Score 50 headshots with the classic rifle | `classic_rifle_headshot_count` >= 50 | implemented | A headshot kill with the classic rifle. |
| `airborne_rockets` | Sky rockets in flight | Score 5 direct hits with rockets while airborne | `airborne_rocket_count` >= 5 | implemented | Your rocket (RPG or RPG2) strikes an enemy player directly while you are airborne at the moment it hits. |
| `knife_zombies` | Bit Late for an Autopsy | Knife 10 zombies | `knife_zombie_count` >= 10 | implemented | Zombie. A melee kill of a zombie with the knife. |
| `misc_ten_in_a_row` | Streaker | Get ten kills in a row without dying | one-shot | implemented | Your kill streak reaches 10 in one life. |
| `misc_fifteen_in_a_row` | OMG Hax! | Get fifteen kills in a row without dying | one-shot | implemented | Your kill streak reaches 15 in one life. |
| `sniper_kill_hard` | Hey, is that a sni- | 50 headshots with the sniper rifle | `sniper_kill_count` >= 50 | implemented | The same counter as Head Hunter. |
| `low_health_killing` | Riskbreaker | Kill ten players when your health is below 10% over multiple rounds | `low_health_kills` >= 10 | implemented | An enemy kill while you are alive with less than 10% of your maximum health. |
| `lastman_kills_zombies_easy` | Groovy! | As last man standing take out at least 5 zombies in one round | one-shot | implemented | Zombie. 5 zombie kills in one round while you are the marked last survivor. |
| `lastman_kills_zombies_hard` | Come get some! | As last man standing take out at least 10 zombies in one round | one-shot | implemented | Zombie. 10 such kills in one round. |
| `zombie_kills_in_water_ach` | Water on the Brainnnns | Kill 5 players as a zombie in the water | `zombie_kills_in_water` >= 5 | implemented | Zombie. A survivor kill as a zombie while you are wading. |
| `blocks_as_zombie` | Dead Can Dig | Destroy 666 blocks as a zombie | `block_as_zombie_count` >= 666 | implemented | Zombie. Blocks destroyed while a zombie, collapses you cause included. |
| `zombie_lms_one_round` | 28 Seconds Later | Survive for 28 seconds as last man standing in one zombie round | one-shot | implemented | Zombie. 28 s alive as the marked last survivor in one round. |
| `zombie_lms_multi_round` | Survivalist | Survive as last man for 10 minutes total over multiple zombie rounds | `zombie_seconds_as_lms_count` >= 600 | implemented | Zombie. Seconds alive as the marked last survivor, added up for life. |
| `airborne_for_hour` | Time Flies | Spend a total of an hour in the air | `airborne_seconds_count` >= 3600 | implemented | Seconds alive and airborne. |
| `sniper2_rapid_kill` | The Quick and the Dead | Kill 3 enemies in 20 seconds with the semi-auto sniper rifle | one-shot | implemented | 3 kills with the semi-auto sniper rifle within 20.0 s (stock `SNIPER2_RAPID_KILL_ACHIEVE_COUNT` / `_TIME`). |
| `sniper_accuracy_hard` | Wisely Snipes | 6 kills with sniper rifle without missing a shot | one-shot | implemented | 6 such kills in a row. |
| `classic_kills_with_intel` | Courier Killer | Kill 5 players while carrying the intel in classic | `classic_kills_with_intel_count` >= 5 | implemented | Classic CTF. An enemy kill while you carry the intel. |

## Judgement calls

Retail's exact rules are unknowable. Where a description leaves room, the
most literal reading the server can measure was chosen:

- **Airstrikes** (`hill_strike_survivor`, `hill_strike_stay_put`). In this
  server a hill is depleted by its timer and the airstrike follows; no player
  triggers it. The players standing on the hill at that moment are taken as
  the ones who brought the strike down. A strike is over when none of its
  shells is in flight (at least 1 s, at most 15 s after the timeout).
- **Contested hills** (`hill_interceptor`, `hill_defender`). "Contested" is
  the mode's own state: both teams on the hill. The credit goes to the players
  of the team left on it when the contest ends.
- **Scoring from a hill** (`hill_greedy`) is personal hill score (first,
  claim, control, occupy, contest, defend, assault); the team's per-second
  points belong to no player.
- **Intercepting a diamond** (`diamond_interceptor`) is taking over a diamond
  the enemy carried, however they lost it. **Stealing** one (`diamond_thief`)
  is taking a diamond an enemy uncovered before any enemy carried it.
- **Bombs.** Dropping a bomb arms it, and it explodes where it lies when the
  fuse runs out. "In the enemy base" is where it explodes; "detonates early"
  is an explosion outside the base; "without dropping it" is a first drop
  inside the base. Only attackers plant and only kills of attacking carriers
  are interceptions.
- **Intel defence.** The interceptor is credited when the intel is home again,
  by the timer or a teammate's touch; "defend it" is the enemy not picking it
  up again in between.
- **`dynamite_below`.** "Below them" is a charge centred at least half a block
  under the victim's feet (the side or underside of the floor block, not its
  top face). The kill is the blast's, or the fall right after it.
- **`landmine_hidden`.** "Re-buried" is a solid block in the mine's own cell
  when it detonates.
- **`airborne_rockets`.** The shooter must be airborne when the rocket hits.
- **`rocket_fall`, `zombie_fall`.** Falls are credited by the server's
  existing rule: the last enemy who damaged that life within 5 s.
- **`turret_evasion`.** Blocks broken by an enemy turret rocket that was
  fired at you and did not damage you, summed over one match.
- **`ammo_drop_greedy`.** "A full resupply" is read like its sibling
  `health_drop_greedy`: the rounds ammo crates add to one weapon's reserve in
  one match reach that weapon's full reserve capacity.
- **`zombie_mvp`, `vip_mvp`.** "The highest number" is judged against every
  player of that round, bots included; a tie for the highest counts.
- **Map structures.** A recovered destroy volume carries no block count, so
  "destroy" means every block the volume held at match start is gone. Everyone
  of the right team who destroyed part of it is credited when the last block
  goes. A block built back into a destroyed cell is not part of the structure.
- **Round scope.** One-round rules without a statistic (`map_*_kill`,
  `zombie_kills_humans`, the last-man rules) restart each round and do not
  survive a reconnect.
- **`low_health_killing`** uses below 10% of the killer's own maximum health.
  The end-of-round award `MOST_KILLS_AT_LOW_HEALTH` keeps its own 20 HP.

## Not implemented

| API name | Description | Needs |
|---|---|---|
| `map_greatwall_destroy` | Destroy the Terracotta Army | a destroy volume on GreatWall |
| `map_moon_destroy` | Lunar Base: Destroy the monolith with the rocket launcher | a destroy volume on LunarBase |
| `map_london_destroy` | Destroy the large bell at the top of Big Ben | a destroy volume on London |
| `map_concrete_destroy` | In demolition mode, destroy all the pillars in the enemy building lobby | the map this is on, and its destroy volumes |
| `map_egypt_destroy` | Destroy the nose of the Sphinx | a destroy volume on AncientEgypt |
| `map_colosseum_kill` | Kill 10 enemies while hiding in the tunnel under the arena | a kill volume on TheColosseum with `ac_kills = 10` |

Retail shipped these volumes in each map's metadata. Only DragonIsland,
MayanJungle and SpookyMansion were recovered with theirs, so the server has
nothing to test a position or a destroyed block against. The engine already
evaluates any volume a stock map's JSON names (see below), with the rocket
launcher condition for `map_moon_destroy` and the Demolition condition for
`map_concrete_destroy`. Authoring the volume is the missing part, and an
authored volume is a guess at retail's until one is recovered.

## Map volumes (`ac_*`)

`server/map_metadata.py` parses the stock parallel arrays into
`MapMetadata.achievement_regions`:

| Key | Meaning |
|---|---|
| `ac_ids` | achievement API name of each volume; one achievement may have several |
| `ac_types` | `1` kill volume (`ACH_KILL_REGION`), `2` destroy volume (`ACH_BLOCK_DESTROY_REGION`), `3` jump volume (not evaluated: no recovered map uses it) |
| `ac_centres`, `ac_w_h_d` | centre and size of the box |
| `ac_teams` | optional: `0` either team, `1` Blue, `2` Green |
| `ac_kills` | optional: kills needed in one round (default 1) |
| `ac_weapons` | optional: kept, not evaluated. Its one recovered value (`1` on the MayanJungle altar) does not identify a weapon by itself; the melee condition comes from the description. |

- A **kill volume** counts the kills a player of the right team makes while
  standing inside the box.
- A **destroy volume** remembers the blocks it holds when the match starts.
- Volumes are only honoured on stock maps, so a custom map cannot hand out a
  retail achievement.
- A same-map restart keeps the terrain. A structure that is partly gone starts
  the next match with what is left, and one with a volume already empty cannot
  be earned until the map is loaded again.

## Master round result

When the AoSPlay master bridge is active, each player row of the round event
(`POST /api/master/stats`) gains an optional field with the API names that
identity unlocked since they were last reported:

```json
{"steamid": "900000000000001", "name": "Kiko", "total": [1, 30], "stats": {},
 "achievements": ["misc_five_in_a_row", "spade_kill"]}
```

The field is absent when there is nothing new, and a master that predates it
ignores it. Without the bridge (no write token, `revival.enabled = false`) no
event is built and nothing is marked as reported.

## Known limits

- Multi-Hill ends at 100 team points, one per second of uncontested
  ownership, and a hill times out after 240 s. Under the default settings a
  hill held by one team ends the match before it times out, so the four rules
  tied to a timeout (`hill_greedy`, both airstrike rules) are only reachable
  on hills that are fought over or left alone for most of their window.
- Name identities are only as stable as the name.
- Steam is not informed of unlocks.

## Tests

- `tests/test_achievements.py`: catalog, store, identity, persistence,
  configuration, the Create Match case, the bot-kill switch, the master field.
- `tests/test_achievement_rules.py`: every rule's boundary at the engine.
- `tests/test_achievement_modes.py`: the rules driven through the real modes.
- `tests/test_achievement_wiring.py`: the hooks in the real combat path.
