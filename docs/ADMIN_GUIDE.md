# Operator and Extension Guide

This is the operator-facing reference for `config.toml`, chat
commands, and trusted Python plugins. The distributed `config.toml` is an
executable example: every supported Match Lobby rule is present with comments.

## Configuration lifecycle

The server reads TOML once at startup. A missing file uses defaults; a TOML
parse failure prints a warning and also falls back to defaults. Semantic
validation rejects unknown rules, unsafe map names, and unsupported lobby
slider values. Validate the selected configuration with `--check` before use.
Runtime paths are resolved relative to the executable bundle, not the current
shell directory. A local override file must be selected with `--config`.

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
- `port`: ENet and A2S listen port. Default `27015`.
- `max_players`: 1–255. The retail lobby presets are 2, 4, …, 24. Default `24`.

A key missing from the file falls back to the value in the shipped
`config.toml`: `port` 27015, `max_players` 24, `[game] default_mode` `tdm`,
`default_map` `MayanJungle`. Older builds fell back to 32887, 50, `ctf` and the
unshipped map `classicgen`. Port 32887 is an explicit choice for the
unmodified Steam browser (see `[steam]`), not a default.
- `tick_rate`: bounded to 10–240; production must remain 60 for retail physics.
- `password`: optional join password, at most 64 UTF-8 bytes; blank is public.
  Requires the native BattleSpades client. `password_max_attempts` (3),
  `password_timeout_seconds` (60), and `password_lockout_seconds` (60) bound
  failed joins. A password server advertises A2S visibility 1 and the master
  `password` tag. Password packets and their lengths are excluded from logs.

### `[steam]`

- `enabled`: starts the isolated legacy Steam registrar. It is off by default;
  direct ENet/A2S hosting remains available without Valve binaries.
- `app_id`: fixed at `224540`. Startup rejects Spacewar ID `480` because the
  retail browser queries only the Ace of Spades application.
- `runtime_dir`: directory containing the original x86 `steam_api.dll`; blank
  uses `./steam-runtime` or `BATTLESPADES_STEAM_RUNTIME`.
- `steamclient_dir`: compatible x86 `steamclient.dll`, `tier0_s.dll`, and
  `vstdlib_s.dll`; blank discovers the installed Steam directory.
- `helper_path`: optional x86 bridge override. Windows releases bundle the
  helper under `_internal/steam` but never bundle Valve DLLs.
- `use_supplied_steamclient`: unsafe compatibility opt-in. Keep false for the
  small legacy client-tree DLL, which was measured hanging during init.
- `steam_port`: Steam updater/master UDP port (the retail server used `8766`).
- `query_port`: Steam A2S UDP port; zero means game port + 1. All three ports
  must be distinct.
- `public`: public anonymous listing (`true`) versus LAN/no-master mode.
- `secure`: requests the registrar's VAC mode. Leave the default disabled;
  accepting the legacy packet-105 ticket/XOR handshake is not VAC validation.
  Revival's separately consumed join tickets provide account identity when
  that integration is enabled.
- `region`, `playlist_id`, `protocol_version`, `texture_skin`: inputs to the
  exact retail tags `v...;playlist=...;region=...;mode=%04d[;classic][;skin=...]`.
- `game_version`: original value `1.0.0.0`.
- `require_registration`: when false, a Steam outage never stops gameplay;
  when true, startup fails unless anonymous logon completes within
  `startup_timeout_seconds`.
- `publish_interval_seconds`: coalesced live name/map/population refresh rate;
  it runs outside the 60 Hz simulation.

The stock server-row implementation ignores Steam's returned game port and
always connects to UDP `32887`. Use `[server].port = 32887` when compatibility
with an unmodified browser row matters. Registration and retail list discovery
are separate paths: a healthy registrar/A2S probe does not establish that the
2015 client's legacy list endpoint works. Verify the intended client and public
endpoint using the checks in [RUNBOOK.md](RUNBOOK.md).

### `[revival]` and durable match results

`server/config.py` defines the settings: `enabled`, `base_url`, `public_host`,
`server_id`, `region`, `official`, `require_identity`,
`heartbeat_interval_seconds`, `request_timeout_seconds`, and `results_path`.
The commented defaults in `config.toml` are the configuration reference.
`AOS_MASTER_URL`, `AOS_MASTER_WRITE_TOKEN`, `AOS_SERVER_ID`, and public endpoint
environment overrides are consumed by `server/revival_master.py`.

The master bridge validates join tickets, advertises the server, and submits
round-result snapshots. `server/profile_stats.py` records authoritative activity;
`server/result_outbox.py` persists reports off the game loop in SQLite until an
acknowledged upload. A retry retains the original event ID and payload. Shutdown
captures unfinished participation before closing the bridge.

`results_path` defaults to `state/round-results.sqlite3`, resolved through the
runtime paths service. Preserve it across bundle replacement. In the Docker
image the entrypoint moves it to `/data/state/round-results.sqlite3` (and
`[anticheat] report_path` to `/data/logs/anticheat.jsonl`), so it lives on the
instance volume even when `/app` is read-only. See RUNBOOK "Container
contract". For relay-hosted
matches, `AOS_RELAY_LOBBY_ID` enables the optional credential-free JSON mirror
at `AOS_MATCH_RESULTS_DIRECTORY`; keep that directory outside disposable server
sessions. Do not treat either pending-result store as a build cache.

XP rates, account eligibility, crate awards, and deployment/migration state are
owned by the master/backend and native client, not this server reference.
Local coverage is in `tests/test_revival_master.py`, `tests/test_profile_stats.py`,
`tests/test_result_outbox.py`, and `tests/test_account_progression_evidence.py`.
The optional cosmetic envelope is documented in [PROTOCOL.md](PROTOCOL.md).

### `[network]`

- `max_connections`: ENet peer limit.
- `timeout_ms`, `bandwidth_limit`: accepted for old configs but ignored (a
  non-default value logs a warning). Retail creates its ENet host with 0/0
  bandwidth and never overrides the peer timeout, and so does this server.
- `require_protocol_version` (default `true`): refuse an ENet connect whose
  data is not protocol 168 (the stock client sends `game_version()` = 168,
  the native client 168) with `ERROR_CLIENT_OUT_OF_DATE` (10) when lower or
  `ERROR_SERVER_OUT_OF_DATE` (3) when higher.
- `event_budget`, `max_pending_packets`, `packet_drain_budget`: receive queue
  and per-tick drain bounds.
- `plugin_event_budget_ms`: total synchronous plugin time allowed per event.
- `max_map_mutation_journal`: terrain changes retained during a joining
  client's map snapshot.
- `map_air_catchup_enabled` (default `false`): re-send every destroyed cell
  since map load as a per-cell packet after the joiner enters GameScene. The
  MapSync stream already carries every edited column, and a live comparison
  found no client/server mismatch without it (docs/MAP_SYNC_JOIN.md); leave
  it off unless you are chasing a specific stale-collision report.
- `map_air_catchup_batch_limit`: exact destroyed voxels replayed per input
  frame while that catch-up is enabled and a reconnect is still gated.
- `unreliable_throttle_deceleration` (default `0`): ENet's per-peer unreliable
  packet throttle step. With the library default (2) a peer whose round trips
  look worse than the recent variance drops a share of unreliable packets
  (the 30 Hz WorldUpdates) for up to five seconds; 0 keeps them all flowing.
  `tick stats:` shows each player's `thr=<n>/32` and `rtt=` so you can watch
  it; in our lossy-link runs it stayed at 32/32 either way.
- `entity_tick_batch_limit`, `mode_event_queue_limit`,
  `mode_event_drain_budget`: bounded entity/mode work.
- `world_mutation_queue_limit`, `world_mutation_batch_limit`,
  `world_mutation_cell_budget`, `world_mutation_timeout_ticks`: post-physics
  block-edit admission and commit limits.
- `prefab_queue_limit`, `prefab_cell_batch_limit`: admitted prefab actions and
  maximum UGC editor prefab cells committed per tick.
- `prefab_competitive_cell_budget` (default 2048, 64-8192): cells of
  competitive prefabs committed per tick across all players. Each prefab
  commits whole in one tick so clients see it at once; prefabs beyond the
  budget wait a tick and are never split (one oversized prefab still commits
  alone).
- `prefab_validation_batch_limit`: already-prepared UGC prefab cells checked
  against the live world per tick. Retail KV6 loading, rotation, and raw-color
  expansion happen on the editor's single preparation worker; this value
  bounds only authoritative main-thread contact/erase validation and must not
  be made unbounded.
- `prefab_health_state_batch` (default 128, 1-4096): damaged block/prefab
  health rows per reliable BlockManager-state packet sent to a joiner.
- `lag_compensation_enabled` (default `true`), `lag_compensation_max_ms`
  (default 250, 0-1000), `lag_compensation_extra_ms` (default 50, 0-250),
  `lag_compensation_view_delay_ms` (default 0, 0-250): hitscan and melee hits
  are checked against target hitboxes where the shooter saw them, one round
  trip plus the view delay ago, but never further back than RTT plus
  `lag_compensation_extra_ms` or `lag_compensation_max_ms`. Only hitboxes are
  rewound; terrain and damage use the live world. The retail client
  extrapolates remote players, so the view delay stays 0
  (docs/LAG_COMPENSATION.md).
- `worldupdate_delivery` (default `"split"`; or `"sequenced"`): how the 30 Hz
  WorldUpdate is sent. The stock client opens one ENet channel, and on it an
  ordered unreliable packet waits behind any lost reliable packet sent before
  it, which froze every remote player until the retransmission arrived.
  `split` sends the rows of other players, entities and turrets unsequenced
  (nothing can hold them) in packets that fit one datagram, and keeps each
  player's own row on the ordered stream. `sequenced` restores the single
  ordered packet.
- `worldupdate_reorder_guard` (default `true`): neither client orders
  WorldUpdates itself. When a player's own input stream shows that the link
  swaps datagrams two frames apart, that player's unsequenced snapshots are
  spaced wider than the measured displacement (down to 10 Hz, ordered
  delivery beyond that), so a stale snapshot is never applied over a newer
  one. `tick stats:` is unchanged; the measurement is per player.
- `terrain_repair_enabled`, `terrain_repair_queue_limit`,
  `terrain_repair_batch_limit`, `terrain_repair_interval_ticks`,
  `terrain_repair_delay_ticks`: delayed canonical repair of rejected client
  prediction.
- `terrain_collapse_repair_batch_limit`,
  `terrain_collapse_repair_delay_ticks`: faster exact-air confirmation for
  server-derived unsupported collapses. The stock checked Damage animation is
  still sent first; these values only bound the stale-geometry safety net.
- `transition_grace_seconds`: optional dwell after `MapEnded(52)` before
  `InitialInfo(114)` opens the stock loader on the retained peer (0–5 seconds).
  The subsequent `MapDataValidation` response gates terrain transfer; no
  custom-client loader acknowledgement is required.

### `[game]` and `[lobby]`

- `default_mode`: `tdm`, `ctf`, `cctf`, `zom`, `vip`, `mh`, `tc`, `dia`,
  `dem`, `oc`, or the non-retail extension `arena`. Aliases (`zombie`,
  `occupation`, `diamond`, `classic_ctf`, ...) are stored as the short code.
  An unknown value stops the server at startup with the list of accepted
  names.
- `default_map`: `.vxl` basename without a path. Default `MayanJungle`.
- `movement_authority`: `server` (production) or diagnostic `client` echo.
- `map_sync_mode`: production `full`; `auto` remains experimental.
  See [PROTOCOL.md](PROTOCOL.md#map-validation-and-the-fast-join-2026-09-29)
  for the tested CRC/delta behavior and outstanding retail visual check.
- `same_team_collision`, `friendly_fire`, `fall_damage`, `build_damage`:
  authoritative simulation switches.
- `bot_count`: legacy fixed bot count, used only when `[bots]` is absent.
- `respawn_time`: compatibility alias for `RULE_RESPAWN_TIMES`.
- `lobby.match_length_minutes`: optional stock choice: 5, 10, 15, 20, 25,
  30, 35, 40, 45, 50, 55, 60, or 90.
- `lobby.map_rotation`: map basenames used by voting. `[]` uses the mode's
  retail playlist filtered exactly like retail `playlists.PlayList`
  (`invalid_modes`, classic/mafia flag, `release`; Territory Control and VIP =
  Alcatraz + CityOfChicago), falling back to every VXL under
  `world.maps_path` when none of those maps is installed. An explicit list
  wins, except that stock maps retail marks invalid for the running mode
  (GreatWall in Demolition, BranCastle in Occupation, ...) are dropped with a
  warning.
- `[lobby]` `map_rotation_shuffle` (default `true`): shuffle the map list once at
  startup, like retail's `random.shuffle(map_list)` at lobby creation.
- `lobby.map_prefabs`: a table of map names to extra construct names for
  capable native clients, e.g. `{ MayanJungle = ["prefab_ladder"] }`.
  It replaces the defaults: London's taxi/postbox/duck and LunarBase's
  cargo/shield/steps. `{}` disables extras. Each map allows up to eight names;
  missing models are omitted. Classes without MAP_PREFABS cannot carry them.
  These defaults are a server choice, not recovered retail map catalogs.
- `[lobby]` `map_vote_retail_max_players` (default `true`): a map whose retail
  `mapinfo` `max_players` (DragonIsland 16, London/LunarBase 20,
  BlockNess/SpookyMansion 24) is below the connected human count is offered
  only after every map that fits. Inferred: retail's server-side consumer of
  that field is not recovered.
- `lobby.end_screen_seconds`: `0.0` to `120.0`, default `12.0`. After the
  next-map ballot resolves, an official map holds the native scores/credits
  overlay for this duration before the validated map loader starts. Custom
  maps without a bundled retail level screenshot keep a safe in-scene hold.
- Map-vote ranking (`[lobby]`). Candidates never include the current map.
  The last `map_vote_recent_exclude` maps (default 2, 0-32) are offered last.
  With `map_vote_size_fit` on (default `true`), maps whose playable area suits
  the lobby come first: the ideal area is the player count times
  `map_vote_area_per_player` standable columns (default 8000), never below
  `map_vote_min_area` (default 24000). Bots count as `map_vote_bot_weight`
  of a player (default 1.0, 0-1). Areas come from each map's `.botnav` cache;
  `map_size_overrides` pins a map to an area or a class, e.g.
  `map_size_overrides = { WW1 = "large", DragonIsland = 27000 }` (`small`,
  `medium`, `large` = 50000, 130000, 230000 columns).
- Kick votes (`[lobby]`). `votekick_cooldown_seconds` (default `300.0`, retail
  `MIN_TIME_BETWEEN_KICK_VOTES`) is how long a starter (keyed by address)
  waits before opening another kick vote; `votekick_cancelled_cooldown_seconds`
  (default `45.0`, retail `MIN_TIME_BETWEEN_CANCELLED_KICK_VOTES`) replaces it
  after the starter cancels their own vote. `votekick_min_team_players`
  (default `3`, `0` disables) is the retail "at least 3 players on a team"
  rule, counted on the starter's team with bots included. Every refusal sends
  the stock string to the starter (`KICK_DENIED_FOR_SPECTATOR`,
  `KICK_DENIED_REASON_SELF_KICK`, `KICK_DENIED_REASON_KICK_HOST`,
  `KICK_DENIED_REASON_VOTE_IN_PROGRESS`, `KICK_DENIED_REASON_VOTE_TOO_SOON`
  with the seconds left, `KICK_NOT_ENOUGH_PLAYERS`), which also closes the
  stock kick menu.

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
| Multi-Hill | active bases 1–5; base time 30/60/90, then 120–600 presets. Retail has no Multi-Hill score rule; set the target with `[modes.mh] score_limit` (default 100) |
| Territory Control | active bases 2–5; capture rate 50/100/200% |
| Diamond Mine | bases 1–5, score 5–60 step 5, diamonds 1–5, lifetime 10–60 step 10 |
| Demolition | build state OFF or 10–120 seconds step 10 |
| Occupation | score OFF, 3/6/9/15/30/45/60/75/90/150; bombs 1–3; fuse 5/10/15/20 |

Diamond Mine spawns diamonds when blocks are mined; none is placed at round
start. `[modes.dia] discovery_guarantee_blocks` defaults to `300`: when no
diamond is active, that many mined blocks since the last find guarantee the
next discovery after the spawn cooldown. Set `0` for retail chance only.
Bots now hold their mining target until the block breaks and seek a new site
when no block is within reach. Once-a-minute logs report mined blocks and
discoveries. `loose_cash_in = true` scores a dropped diamond resting in an
open drop-off for its last carrier's team; set it to `false` to disable this
inferred trigger. Throw velocity is currently accepted but ground diamonds
are anchored to the surface immediately; authoritative pickup flight has not
been recovered.

The exact keys are visible in `config.toml` and defined in
`server/game_rules.py`; tests require that catalog to contain all 102 recovered
visible and hidden entries.

### Remaining tables

- `[bots]`: `enabled`; `population_mode` (`backfill`, `fixed`, `admin`);
  `fill_target`; `max_bots`; `reserve_human_slots`; `difficulty` (`casual`,
  `normal`, `hard`, `mixed`); `worker` (`thread` by default, or `process`);
  worker rates/budgets; `seed`;
  `clean_slate_games` (defaults to `3`, `0` disables only the periodic worker
  recycle); and bounded `debug_visualization`. Per-round path, coordination,
  stuck, motor, and queued-intent state is always discarded.
  Per-team bot skill balance: with `skill_balance` on (default `true`), when
  one team's humans clearly out-kill the other side, that team's bots are
  eased and the other team's sharpened. Profile fields shift by at most
  `skill_balance_max_shift` (default 0.35, 0-0.9), at `skill_balance_rate`
  per second (default 0.03), ignoring edges inside `skill_balance_deadband`
  (default 0.15, 0-0.95) and teams with fewer than `skill_balance_min_events`
  kills and deaths (default 6).
- `[modes.<code>]`: `time_limit` seconds plus legacy aliases shown in the
  sample. These override `[game_rules]` only for that mode. An alias table
  name (`[modes.zombie]`) applies to its short code; `[modes.zom]` wins when
  both exist.
- `[teams]`: localized team-name string IDs, RGB colors, automatic balance and
  threshold. With `auto_balance = true` a joiner who asks for a team that
  already has `balance_threshold` or more players than the other is placed on
  the smaller team. Modes that assign teams themselves (Zombie) and
  host-assigned Revival teams are not balanced. Mid-match switches (the team
  menu and `/team` share one rule set) that would unbalance the teams are
  refused, as are switches inside the 5-second team-change cooldown and
  switches the mode locks.
  Mid-match drift (players leaving) is repaired while `auto_balance` and
  `balance_mid_match` (default `true`) are on, checked every
  `balance_check_interval` seconds (default 1.0). A lead must last
  `balance_grace_seconds` (default 5.0) so a reconnect or bot backfill can fix
  it first. Then a dead bot on the bigger side switches; after
  `balance_bot_wait_seconds` (default 10.0) a bot is retired and replaced on
  the smaller side; last, the most recently joined dead human moves (never a
  live player, carrier, VIP or last survivor, and nobody twice within
  `balance_player_cooldown` seconds, default 600).
- `[weapons]` (deprecated): `rifle_damage`, `smg_damage`, `shotgun_damage`,
  `spade_damage` and `grenade_damage` are still parsed but are ignored, with a
  warning when set. Weapon damage comes from the retail per-weapon and
  per-body-part tables (`server/weapons_retail.py`,
  [WEAPONS_RETAIL.md](WEAPONS_RETAIL.md)). To scale damage, use
  `RULE_WEAPON_DAMAGE`. The table is no longer in `config.toml`.
- `[world]`: fallback skybox and content paths. `map_size_x`/`map_size_y`/
  `map_size_z` are informational: every retail VXL is 512x512x240 and the
  loader never reads them, so any other value is ignored with a warning.
  `water_level`/`water_damage` are also ignored with a warning, because the
  retail water plane is fixed at z 238/239 and retail has no water damage.
  Map metadata overrides atmosphere and authored entities.
- `[plugins]`: `enabled`, `path`, `allowlist`, `denylist`.
- `[admin]`: password, command logging and `bans_path`; see
  [Admin login and password](#admin-login-and-password).
- `[logging]`: level, file, console, packet-trace opt-in, queue capacity,
  rotating-file `max_bytes`/`backup_count`, and
  suppressed packet IDs.
- `[debug]`: reverse-engineering-only parity, reconciliation, capture, and
  WorldUpdate cadence controls. Keep the shipped values in production.

### `[objectives]`: escape watch and objective guards

**Escape watch** (`server/escape_watch.py`). With `escape_watch_enabled`
on (default `true`), every `escape_watch_interval` seconds (default 1.0,
0.1-60) the server looks for players who left the playable map and gives
them the retail high-minimap marker so the enemy can hunt them: outside the
512x512 map or below the floor at once, above the map top for
`escape_watch_sky_seconds` (default 5), inside solid terrain for
`escape_watch_embedded_seconds` (default 3), and, for objective carriers,
VIPs, Zombie survivors and zone holders only, sealed in a tiny air pocket for
`escape_watch_entomb_seconds` (default 5). Ordinary bunkers and tunnels open
to the surface are never marked. Tutorial and UGC disable the watch.

**Objective guards** (`modes/objective_guard.py`):

- `objective_entomb_seconds` (default 5): a ground intel, diamond or bomb
  buried, sealed in a pocket or left floating this long returns home or is
  resettled onto the surface, so it can never be made unobtainable.
- `objective_pickup_requires_los` (default `true`): objective pickups need a
  clear voxel line from the player to the objective (no grabbing through a
  wall or floor).
- `objective_pickup_ends_spawn_protection` (default `true`): picking up an
  objective ends the carrier's spawn protection.
- `objective_afk_seconds` (default 60, `0` disables): a player without real
  input for this long no longer counts toward holding a TC/MH zone.
- `ctf_base_pit_depth` (default 24): a CTF carrier in an open-sky pit dug up
  to this many blocks below its own base still scores, so defenders cannot
  make captures impossible by excavating the base.

### `[anticheat]`

Enforcement switches: `enforce_shot_origin` with `shot_origin_tolerance`,
`enforce_aim_direction` with `aim_direction_tolerance_deg`,
`enforce_input_starvation` with `starvation_airborne_ticks` and
`starvation_timeout_seconds`, `enforce_input_backlog` with
`backlog_max_frames`, `enforce_block_interval` (enabled in the sample config;
omitting the key retains the older log-only default), plus `kick_on_protocol_violation`,
`admin_login_attempts` and `summary_interval_seconds`. Checks that could
reject legitimate play under packet loss ship log-only.

Block cadence uses the stock server-only `MIN_BLOCK_INTERVAL` (0.1 seconds)
for builds, lines and flares. Plausible client frame labels preserve spacing
when reliable packets arrive together; a bounded budget replenished by server
time still limits the sustained rate. Packets with absent or invalid labels
share that budget and must satisfy arrival-time spacing. Refused builds get a
canonical repair when enforcement is enabled and otherwise increment
`block_interval:observed`. Bots retain their arrival-time pacing.

Shots use the same frame-label and server-time budget approach, with a shared
fire lane across tools and mouse buttons. Changing tools cannot shorten the
previous shot's cooldown or turn fast-tool credit into extra slow-tool shots.
Reloads still complete on server time: reload packet 76 has no action label.
See `tests/test_action_clock.py` and `tests/test_action_clock_recovery_review.py`
for packet bunching, mixed-label, forged-rate and delayed-burst regressions.

**Suspicion report** (`server/anticheat_report.py`). Every
`summary_interval_seconds` the server scores each human against statistical
signals and appends flagged players (score at least `flag_min_score`,
default 1.0) as JSON lines to `report_path` (default
`logs/anticheat.jsonl`), rotated at `report_max_bytes` (default 5000000)
keeping `report_backups` files (default 3). It never kicks; review it with
`/acreport` and `/acstats`. Setting `report_enabled` to `false` turns it
off. Signals and
their thresholds (defaults in `config.toml`):

- Headshot share of hitscan kills: `headshot_kill_ratio` (0.60, 0-1) after
  `headshot_kill_min_kills` (30).
- Headshot share of single-ray hits: `headshot_hit_ratio` (0.55, 0-1) after
  `headshot_hit_min_hits` (60).
- Per-weapon accuracy against the human population: above the
  `accuracy_percentile` (99, 0-100) cutoff and at least `accuracy_min_margin`
  (0.15) above the median, after `accuracy_min_shots` (100), once
  `accuracy_min_population` (20) other players have data.
- Aim snaps: a turn of at least `snap_min_deg` (20) within `snap_frames` (2)
  onto a head (`snap_head_radius` 0.35, `snap_margin_deg` 0.75), flagged at
  `snap_ratio` (0.10) of `snap_min_engaged` (20) engagements with at least
  `snap_min_events` (4) snaps.
- Reaction time: target acquisition within `reaction_acquire_deg` (10) using
  `reaction_history_frames` (60), a median under `reaction_median_ms` (90)
  over `reaction_min_samples` (15); a target counts as new after
  `reaction_reengage_seconds` (3.0); `engage_cone_deg` (4.0) is the
  engagement cone.
- Shotgun pellet-seed skew: `pellet_seed_top_share` (0.2) after
  `pellet_seed_min_shots` (64).
- Sustained log-only violations: at least `sustained_min_count` (20) over
  `sustained_min_minutes` (5.0) at `sustained_per_minute` (3.0).

### `[audio]`: in-round music

- `mode_start_music` (default `true`): at every round start, and for every
  joiner, the server starts a looping in-round bed (a random
  `last_man_standing_00N` track). This is a **deliberate deviation** from
  retail, kept because the owner wants background music: retail rounds were
  silent apart from map ambience until the final 61 s (`game_ending_00N`),
  and `last_man_standing` played only for the Zombie last survivor. Set it
  to `false` for the retail silence; round starts then only stop the
  previous round's track. The final-minute, victory, Zombie last-man and
  Tutorial tracks are unaffected.

### `[conduct]`: team griefing, AFK and names

Implemented in `server/conduct.py`; every kick is logged on the `conduct`
logger (`conduct kick player=.. name=.. reason=..`) and, with
`announce_kicks = true`, announced in system chat.

**Team griefing.** A player earns `grief_team_damage_points` per 100 HP they
remove from teammates plus `grief_team_kill_points` per team kill. Indirect
kills count: whoever shoots or blasts a teammate's landmine *instigated* the
explosion, so the owner's resulting death and any teammate it hurts are
charged to them. With friendly fire off that owner is the only teammate a blast
can hurt, so it is the usual way to grief. The kill feed and team-kill
scoring then name the instigator instead of recording a suicide for the owner.
Chain reactions keep the first instigator. An enemy setting off your mine is
never grief. One point decays every `grief_decay_seconds`. A single burst,
meaning everything within `grief_incident_seconds`, is capped at
`grief_incident_max_points`, so one accidental grenade into a group cannot
kick on its own. At `grief_warn_points` the player gets a private warning,
and at `grief_kick_points` they are disconnected with the retail
`ERROR_KICK_GRIEFING` reason. `grief_kick_enabled = false` keeps the
accounting and warnings but never kicks. Bots and, by default, logged-in
admins (`grief_exempt_admins`) are never charged. Destroying teammates'
builds is not scored, because digging and rebuilding is normal play.

With the defaults a team kill costs about 4 points, so roughly three team
kills within a few minutes lead to a kick.

**AFK.** Only real input resets the idle clock: a change of movement keys,
fire, aim or hover, or a visible orientation change. The retail client keeps
streaming identical ClientData while idle, so packets alone do not count. The
clock pauses while the client is loading, while the player is dead and waiting
to respawn, and while the round has not started or has ended (end screen). A
private warning goes out at `afk_warn_seconds` (default 9 minutes). At
`afk_kick_seconds` (default 10 minutes; `0` disables AFK kicks) the player is
disconnected with `ERROR_AFK_TIMEOUT`. Spectators use
`afk_spectator_kick_seconds` (default 30 minutes; `0` exempts them). Admins
are exempt while `afk_exempt_admins = true`.

Retail parity note: only the disconnect code is retail. The stock client has
`ERROR_AFK_TIMEOUT` ("Kicked due to being idle"), but no retail idle time,
warning text or kick broadcast was recovered (the shared constants hold no
idle threshold). The 540/600/1800 s defaults, the English warning and the
"was kicked for being AFK" top-screen line are this server's own choices;
every other player also sees the retail `PLAYER_LEFT` line when the idle
player is disconnected.

**Names** (`server/player_names.py`, always on). Names are NFKC-normalized,
and zero-width, bidi and other invisible characters are removed. Uniqueness is
checked on a homoglyph skeleton that folds case, accents, Cyrillic and Greek
lookalikes, `0/O`, `1/l/I`, `rn/m` and `vv/w`. A joiner whose name looks like
an online player's (`Κiko` against `Kiko`) gets a `~N` suffix. A joiner
whose name looks like a logged-in admin's becomes `Player~N` instead. Staff
and server names (`Server`, `Admin`, `Console`, `System`, `Moderator`,
`BattleSpades`, `[Admin]x` and `(MOD)x` tags, and so on) and any entry in
`reserved_names` become `Player`. Bot and human names share one namespace:
a human cannot take a live bot's name, and bots are never renamed. Names stay
within the retail 15-byte limit.

## Commands

Anyone may use `/help [command]`, `/kill`, `/team <team1|team2|spectator>`,
`/score`, `/players`, `/pm <player> <message>`, `/me <action>`, `/ping`, and
`/stats [player]`.

Player-command rules that close cheating shortcuts:

- Slash commands are rate-limited per player (burst of 5, then one per
  second; excess commands are dropped with a "slow down" notice). A player
  logged in with `/admin` is exempt.
- `/team` goes through exactly the same checks as the in-game team menu
  (spectator rule, mode team locks, 5-second cooldown, `auto_balance`,
  deployable retirement).
- Suicide and team/class transitions no longer deny kills: `/kill`, a team
  change, or a class/loadout change that ends a live Character within
  5 seconds of taking damage from an enemy counts as that enemy's kill (the
  victim's death counts, the killer gets score and the killfeed entry). The
  team or class change itself still happens.
- A class/loadout change that would kill a live Character again within
  3 seconds of the previous one is staged for the next respawn instead.
  Choosing a class while dead (the normal menu flow) is always instant.
- `/me` and `/pm` respect mute and the 200-character chat limit.

### Admin login and password

Authenticate with `/admin <password>` (alias `/login`). Set the secret in
`config.toml`:

```toml
[admin]
password = "a-long-unique-secret"
```

Container deployments set `BATTLESPADES_ADMIN_PASSWORD` instead (the
entrypoint refuses to start with the default and requires 12+ characters).

- `/admin` login is **disabled** while the password is the shipped
  `"changeme"`, empty, or shorter than 12 characters. Startup logs a
  `WARNING` ("In-game /admin login is DISABLED ...") and players who try
  `/admin` are told admin login is disabled. Nothing else about the server is
  affected; change the password and restart to enable it.
- The password is compared in constant time and never written to the command
  log. Everything after `/admin ` (trimmed) is the attempt, so passwords may
  contain spaces but not leading/trailing whitespace.
- After `[anticheat] admin_login_attempts` (default 3) wrong guesses from one
  address within 10 minutes, the player is kicked and the address is banned
  for 10 minutes (an expiring entry in `bans_path`, default `bans.json`;
  remove it early by deleting the entry). Each failure is also counted in the
  anticheat log as `admin_login_failed`.
- An admin session lasts until the player disconnects.

Admin commands are:

- `/kick <player> [reason]`
- `/ban <player> [duration] [reason]`
- `/mute <player>` and `/unmute <player>`
- `/tp <player> [target]` or `/tp <x> <y> <z>`
- `/god [player]`
- `/map <mapname>` and `/mode <code>`; both use the crash-safe reconnect flow
- `/restart`, `/endround [team]`, `/say <message>`
  (`/map`, `/mode`, `/restart` and `/endround` are refused in the Map
  Creator and Tutorial sessions, which own their map and mode)
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

`plugins/_example_plugin.py` is the maintained minimal template. Files starting with `_` are not loaded: copy it to a public name (e.g. `plugins/my_plugin.py`) to enable it. It is disabled by default because its spree/domination chat lines are not retail.
