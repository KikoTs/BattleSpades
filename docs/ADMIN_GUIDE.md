# Operator and Extension Guide

This is the complete operator-facing reference for `config.toml`, chat
commands, and trusted Python plugins. The distributed `config.toml` is an
executable example: every supported Match Lobby rule is present with comments.

## Configuration lifecycle

The server reads TOML once at startup. Invalid TOML, an unknown `RULE_*` name,
an unsafe map name, or an unsupported lobby slider value stops startup with a
specific error. Runtime paths are resolved relative to the executable bundle,
not the current shell directory.

Precedence is narrowest first:

1. A `[modes.<code>]` compatibility override.
2. A `[game_rules]` Match Lobby value.
3. `[lobby].match_length_minutes` for clocks only.
4. The recovered retail playlist default.

Older `[game]` keys remain compatible. `respawn_time` and `fall_damage` are
mirrored into the new rule service when their `RULE_*` equivalents are absent.

## Configuration tables

### `[server]`

- `name`: server-browser display name.
- `port`: ENet and A2S listen port.
- `max_players`: 1–255. The retail lobby presets are 2, 4, …, 24.
- `tick_rate`: bounded to 10–240; production must remain 60 for retail physics.

### `[network]`

- `timeout_ms`, `max_connections`, `bandwidth_limit`: ENet limits; zero
  bandwidth means unlimited.
- `event_budget`, `max_pending_packets`, `packet_drain_budget`: receive queue
  and per-tick drain bounds.
- `plugin_event_budget_ms`: total synchronous plugin time allowed per event.
- `max_map_mutation_journal`: terrain changes retained during a joining
  client's map snapshot.
- `entity_tick_batch_limit`, `mode_event_queue_limit`,
  `mode_event_drain_budget`: bounded entity/mode work.
- `world_mutation_queue_limit`, `world_mutation_batch_limit`,
  `world_mutation_cell_budget`, `world_mutation_timeout_ticks`: post-physics
  block-edit admission and commit limits.
- `prefab_queue_limit`, `prefab_cell_batch_limit`: incremental KV6 expansion.
- `terrain_repair_enabled`, `terrain_repair_queue_limit`,
  `terrain_repair_batch_limit`, `terrain_repair_interval_ticks`,
  `terrain_repair_delay_ticks`: delayed canonical voxel repair.
- `transition_grace_seconds`: time between `MapEnded(52)` and disconnect reason
  18 during a map/mode rollover.

### `[game]` and `[lobby]`

- `default_mode`: `tdm`, `ctf`, `cctf`, `zom`, `vip`, `mh`, `tc`, `dia`,
  `dem`, `oc`, or the non-retail extension `arena`.
- `default_map`: `.vxl` basename without a path.
- `movement_authority`: `server` (production) or diagnostic `client` echo.
- `map_sync_mode`: production `full`; `auto` remains experimental.
- `same_team_collision`, `friendly_fire`, `fall_damage`, `build_damage`:
  authoritative simulation switches.
- `bot_count`: legacy fixed bot count, used only when `[bots]` is absent.
- `respawn_time`: compatibility alias for `RULE_RESPAWN_TIMES`.
- `lobby.match_length_minutes`: optional stock choice: 5, 10, 15, 20, 25,
  30, 35, 40, 45, 50, 55, 60, or 90.
- `lobby.map_rotation`: ordered map basenames used by voting; `[]` discovers
  every VXL under `world.maps_path`.

### `[game_rules]`

Keys deliberately match the shipped client. Boolean rules accept `true/false`
or `ON/OFF`; percent sliders accept either strings such as `"150%"` or their
numeric multiplier. The server sends client-visible switches in `InitialInfo`
and enforces the same class/tool/action rule authoritatively.

General toggles:

- `RULE_ENABLE_BLOCKS`, `RULE_ENABLE_FLARE_BLOCKS`, `RULE_ENABLE_PREFABS`
- `RULE_ONE_HIT_KILL`, `RULE_ENABLE_GRAVESTONES`,
  `RULE_ENABLE_CORPSE_EXPLOSION`
- `RULE_ENABLE_SNIPER_BEAM`, `RULE_ENABLE_DEATH_CAM`,
  `RULE_ENABLE_MINI_MAP`, `RULE_ENABLE_SPECTATORS`
- `RULE_ENABLE_FALL_ON_WATER_DAMAGE`, `RULE_ENABLE_COLOUR_PICKER`
- `RULE_POINTS_FROM_TEABAGGING`

General sliders:

- `RULE_RESPAWN_TIMES`: 0–60 seconds, step 5.
- `RULE_BLOCK_HEALTH`, `RULE_WEAPON_DAMAGE`,
  `RULE_CHARACTER_BLOCK_WALLETS`: 50%, 100%, 200%.
- `RULE_CHARACTER_SPEED`: 50%, 100%, 150%, 200%.
- `RULE_SPAWN_PROTECTION_TIME`: OFF, 1, 2, 3 seconds.
- `RULE_CRATES_SPAWN_TIME`: 10–60 seconds, step 5.
- `RULE_VOTES_REQUIRED_FOR_KICK`: 25%, 50%, 75% (recovered hidden rule).

Class toggles are `RULE_ENABLE_CLASS_` plus `COMMANDO`, `MARKSMAN`, `MINER`,
`ENGINEER`, `ROCKETEER`, `SPECIALIST`, or `MEDIC`.

Equipment toggles are `RULE_ENABLE_EQUIPMENT_` plus:

`CLASSIC_SPADE`, `CLASSIC_GRENADE`, `SPADE`, `GRENADE`,
`ANTIPERSONNEL_GRENADE`, `SNOWBLOWER`, `PICKAXE`, `LANDMINE`,
`ROCKET_TURRET`, `GLIDE_JETPACK`, `JUMP_JETPACK`, `JETPACK`, `SUPER_SPADE`,
`DRILL_CANNON`, `DYNAMITE`, `MEDPACK`, `CHEMICALBOMB`, `RADAR_STATION`, `C4`,
`DISGUISE`, and hidden mapping `PARACHUTE_NORMAL`.

Weapon toggles are `RULE_ENABLE_WEAPON_` plus:

`KNIFE`, `MINIGUN`, `RPG`, `TRIPLE_BARREL_RPG`, `PISTOL`, `SNIPER_RIFLE`,
`SNIPER_RIFLE2`, `RIFLE`, `DOUBLE_BARREL_SHOTGUN`, `PUMP_ACTION_SHOTGUN`,
`SMG`, `CLASSIC_SHOTGUN`, `CLASSIC_SMG`, `TOMMYGUN`, `SNUB_PISTOL`,
`CROWBAR`, `MOLOTOV`, `RIOTSTICK`, hidden `RIOTSHIELD`, `MACHETE`,
`AUTOPISTOL`, `GRENADE_LAUNCHER`, `STICKY_GRENADE`, `MINE_LAUNCHER`,
`ASSAULTRIFLE`, `LIGHTMACHINEGUN`, `AUTOSHOTGUN`, and `BLOCKSUCKER`.

Mode rules and accepted choices:

| Mode | Rules |
|---|---|
| TDM | `RULE_TDM_SCORE_TARGET`: OFF, 5–50 presets, 60–100 by 10, or 200 |
| CTF/CCTF | shoot with intel, return on touch, auto-return booleans; hidden own-intel-at-base boolean; score 1–10 |
| Zombie | rounds 1–5, first infected 1–5, class speed 50/100/200%, zombie damage 50/100/200% |
| VIP | rounds 1–5, VIP health 50/100/200%, sudden death boolean |
| Multi-Hill | active bases 1–5; base time 30/60/90, then 120–600 presets |
| Territory Control | active bases 2–5; capture rate 50/100/200% |
| Diamond Mine | bases 1–5, score 5–60 step 5, diamonds 1–5, lifetime 10–60 step 10 |
| Demolition | build state OFF or 10–120 seconds step 10 |
| Occupation | score OFF, 3/6/9/15/30/45/60/75/90/150; bombs 1–3; fuse 5/10/15/20 |

The exact keys are visible in `config.toml` and defined in
`server/game_rules.py`; tests require that catalog to contain all 102 recovered
visible and hidden entries.

### Remaining tables

- `[bots]`: `enabled`; `population_mode` (`backfill`, `fixed`, `admin`);
  `fill_target`; `max_bots`; `reserve_human_slots`; `difficulty` (`casual`,
  `normal`, `hard`, `mixed`); process worker rates/budgets; `seed`; and bounded
  `debug_visualization`.
- `[modes.<code>]`: `time_limit` seconds plus legacy aliases shown in the
  sample. These override `[game_rules]` only for that mode.
- `[teams]`: localized team-name string IDs, RGB colors, automatic balance and
  threshold.
- `[weapons]`: base damage compatibility values used by the recovered weapon
  profiles.
- `[world]`: map dimensions, water level/damage, fallback skybox, and content
  paths. Map metadata overrides atmosphere and authored entities.
- `[plugins]`: `enabled`, `path`, `allowlist`, `denylist`.
- `[admin]`: password and command logging.
- `[logging]`: level, file, console, packet-trace opt-in, queue capacity, and
  suppressed packet IDs.
- `[debug]`: reverse-engineering-only parity, reconciliation, capture, and
  WorldUpdate cadence controls. Keep the shipped values in production.

## Commands

Anyone may use `/help [command]`, `/kill`, `/team <team1|team2|spectator>`,
`/score`, `/players`, `/pm <player> <message>`, `/me <action>`, `/ping`, and
`/stats [player]`.

Authenticate with `/admin <password>`. Admin commands are:

- `/kick <player> [reason]`
- `/ban <player> [duration] [reason]`
- `/mute <player>` and `/unmute <player>`
- `/tp <player> [target]` or `/tp <x> <y> <z>`
- `/god [player]`
- `/map <mapname>` and `/mode <code>`; both use the crash-safe reconnect flow
- `/restart`, `/endround [team]`, `/say <message>`
- `/fog <r> <g> <b>`, `/time [seconds]`, `/balance`
- `/netcode [selfrow on|off] [offset n] [interval n]` (diagnostics only)
- `/bots status|fill n|add n [team]|remove n|name|all|difficulty ...|debug ...`

Unknown commands, missing arguments, invalid RGB/coordinates, unavailable
maps, and unregistered modes fail without mutating live match state.

## Plugins

Plugins are trusted code, not a sandbox. Put one public `.py` file in the
configured plugin directory and define a `BasePlugin` subclass. The loader
rejects path escapes, imports files under unique module identities, skips
malformed plugins, prevents duplicate plugin names, and applies allow/deny
filters before `on_load`.

Available asynchronous hooks are `on_load`, `on_unload`, `on_enable`,
`on_disable`, `on_player_connect`, `on_player_join`, `on_player_leave`,
`on_player_spawn`, `on_player_kill`, `on_player_chat`, `on_block_build`,
`on_block_destroy`, and `on_tick`. Gameplay-thread callbacks share the bounded
`network.plugin_event_budget_ms`; do not perform blocking file/network I/O or
unbounded searches in a hook. Use authoritative public services instead of
mutating VXL, entities, inventory, or packet queues directly.

`plugins/example_plugin.py` is the maintained minimal template.
