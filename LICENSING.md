# Licensing

Copyright (c) 2026 Kiril Tsanov (KikoTs)

BattleSpades is free software licensed under the
**GNU Affero General Public License, version 3 or (at your option) any later
version** (SPDX: `AGPL-3.0-or-later`). The full, unmodified license text is in
[`LICENSE`](LICENSE).

This page is a short plain-English summary. It is not legal advice and does not
replace the license; if they disagree, `LICENSE` wins.

## What the AGPL means here

- **You may** use, run, study, copy, modify and share BattleSpades for any
  purpose, including commercially.
- **If you share it** (source or builds), you must pass on the same freedoms:
  license your version under AGPL-3.0-or-later, include the license, keep the
  copyright notices, provide the complete corresponding source, and mark your
  changes.
- **If you run a modified server that other people connect to**, AGPL section
  13 requires you to offer all of those players the complete source of the
  version you are running, free of charge, through a network server. Running
  the unmodified server needs nothing extra: the default join message already
  links the official source.
- There is **no warranty** (sections 15 and 16).

### Where to put the source link on a modified server

The join message is the most prominent place every player sees. Set
`[server].motd` in `config.toml` to a line that links your fork:

```toml
[server]
motd = ["Modified BattleSpades - source: https://github.com/you/your-fork"]
```

`motd` replaces the default lines, which link the upstream repositories
(`SERVER_REPOSITORY` / `CLIENT_REPOSITORY` in `server/build_info.py`); if you
fork the code you can also change those constants so the default message points
at your fork. Adding the link to the server name or description shown in server
lists, or to the website your server list entry points at, is a good extra.
The link must lead to the source of the exact version you run, including any
plugins you load into the server process, and must keep working.

Changing only `config.toml`, maps or mode settings is not a modification of
the program.

## Earlier releases were MIT

BattleSpades releases **up to and including v0.1.0-beta.1** were published
under the MIT License. Copies of those releases remain available under MIT.
Every later version is licensed under AGPL-3.0-or-later. The original MIT notice
is preserved in
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md#earlier-mit-licensed-code)
because code from that period is still part of the project (MIT-licensed code
may be included in an AGPL work as long as its notice is kept).

## Additional permission for Steamworks (AGPL section 7)

As an additional permission under section 7 of the GNU Affero General Public
License version 3, the copyright holder gives you permission to combine or
link BattleSpades with Valve Corporation's Steamworks SDK and its runtime
libraries (such as `steam_api`, `steamclient` and their dependencies), and to
convey the resulting combination, even though those components are not
licensed under the AGPL. You must follow the AGPL for every part of the work
other than those Valve components, and you must follow Valve's own terms for
the Valve components. If you modify BattleSpades, you may extend this
permission to your version, but you are not obliged to.

The ordinary server's legacy master-registration helper loads Valve runtime
files supplied by the operator. The optional retail Steam relay package and
the Steam relay host for dedicated servers (`steam-host/`) each include the
Steamworks SDK's redistributable library (`steam_api64.dll`, or
`libsteam_api.so` on Linux) beside their separate helper. That Valve binary
is not covered by the AGPL; it retains Valve's Steamworks SDK terms. Valve's
`steamclient` runtime is never shipped: the operator supplies it. No original retail game binary is distributed
in the retail patch package.

## Names and trademarks

The AGPL grants no trademark rights. The names **BattleSpades**,
**BattleSpadesClient**, **Ace of Spades Revival**, **AoS Revival** and
**AoSPlay**, and their logos, are not licensed. You may say truthfully that
your server or fork is based on BattleSpades, but please use your own name for
modified versions and don't present them as official or endorsed. "Ace of
Spades" belongs to its respective owners; this project is not affiliated with
them.

## Third-party components and game content

Bundled libraries keep their own (AGPL-compatible) licenses, and Ace of Spades
game content such as the stock maps and prefabs is **not** covered by the AGPL.
See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

## Applying the license in files

New source files may carry this header:

```
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Kiril Tsanov
```

Contributions are accepted under the same license; see
[`CONTRIBUTING.md`](CONTRIBUTING.md#license-of-contributions).

## Questions

- GitHub: [KikoTs](https://github.com/KikoTs)
- Discord: <https://discord.gg/aosbb>
