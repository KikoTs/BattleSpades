# Beta 1.1 welcome and build date

The default private join greeting identifies **BattleSpades Beta 1.1** and the
build's UTC day. Its three MOTD lines identify the server and BattleSpades
client as an open AoS Revival project, link `https://aosplay.net`, and link:

- `https://github.com/KikoTs/BattleSpades`
- `https://github.com/KikoTs/BattleSpadesClient`

Both GitHub URLs were verified from the corresponding local Git remotes. No
remote request or publication is needed to display these links.

The four lines use private system chat after the first successful world
reveal/ClientData. They are not sent during the loading handshake, broadcast
to other players, repeated after respawning, or repeated on a same-connection
scene reload. A fresh connection gets its own welcome. Text is bounded to 90
UTF-8 bytes per line and eight total lines, without splitting UTF-8 characters
or sending control characters.

Operators can replace the defaults in the existing `[server]` section:

```toml
join_greeting = "Welcome, {player}! {release} | Build (UTC): {build_date}"
motd = ["Our server rules", "Build big. Play fair. Have fun!"]
```

`motd` also accepts a multiline string. Supported tokens are `{player}`,
`{release}`, and `{build_date}`; other braces remain ordinary text. An empty
`join_greeting = ""` plus `motd = []` disables both. Omitting either setting
keeps its default. Custom MOTD content replaces the default project lines.

`BattleSpades.spec` writes `build/metadata/build_info.json` when the actual
freeze begins, then bundles it as `_internal/build_info.json`. The existing
portable staging step preserves this file. `SOURCE_DATE_EPOCH`, when supplied
by a reproducible builder, determines the stamp; otherwise it records the UTC
time of the build invocation. Joining players never rewrite the file/date.
Development trees without a stamp show a clearly labeled `source YYYY-MM-DD`
file-date fallback; old unstamped binaries show `binary YYYY-MM-DD`.

The display release label is intentionally separate from `VERSION`, Steam's
`1.0.0.0` compatibility value, and protocol 168. None of those were changed.

Validation: **50 tests passed** across `test_join_greeting.py`,
`test_reversed_spawn_handshake.py`, `test_lobby_config.py`,
`test_runtime_paths.py`, and `test_release_workflow.py`. Tests cover the actual
join/first-input dispatch, partial reveal retry, private packet routing,
same-peer reload, custom/disabled MOTD, limits/UTF-8, deterministic stamping,
date fallbacks, and compatibility version preservation. Greeting tests also
passed in the final 205-test beta verification. The isolated release build
and packaged metadata checks are recorded in `out/bot-beta-1.1/audit.md`.
