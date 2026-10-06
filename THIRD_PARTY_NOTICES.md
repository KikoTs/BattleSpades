# Third-party notices — BattleSpades server

BattleSpades itself is licensed under the
[GNU Affero General Public License v3.0 or later](LICENSE)
(`AGPL-3.0-or-later`). The components below keep their own licenses,
reproduced or linked here. Where a component's license text lives in the
repository, that file is the authoritative copy. Every component that is
compiled into or shipped with the server uses a license that is compatible with
AGPL-3.0-or-later; the last column records that check.

Portable release archives ship [`release/THIRD_PARTY_NOTICES.txt`](release/THIRD_PARTY_NOTICES.txt),
which carries the full notices for everything the frozen server contains.

## Summary

| Component | Version | Where | How it is used | License | AGPL-3.0 compatible |
|---|---|---|---|---|---|
| [ENet](https://github.com/lsalzman/enet) | 1.3.17 (`e0e7045`) | `vendor/pyenet/enet/` | Compiled into the `enet` extension; shipped in releases | MIT — [`vendor/pyenet/enet/LICENSE`](vendor/pyenet/enet/LICENSE) | Yes |
| [pyenet](https://github.com/piqueserver/pyenet) | 1.3.17 (`1bd4e84`) | `vendor/pyenet/` | Cython binding for ENet; shipped in releases | BSD-3-Clause — [`vendor/pyenet/LICENSE`](vendor/pyenet/LICENSE) | Yes |
| [Recast & Detour](https://github.com/recastnavigation/recastnavigation) | 1.6.0 (`6dc1667`) | `vendor/recastnavigation/` | Bot navigation meshes (`server/bot_ai/recast`); shipped in releases | Zlib — [`vendor/recastnavigation/License.txt`](vendor/recastnavigation/License.txt) | Yes |
| [CPython](https://www.python.org/) | 3.12 | release archives only | Bundled interpreter and standard library | PSF-2.0 (plus the licenses of CPython's own bundled libraries, e.g. OpenSSL, libffi, zlib, bzip2, xz, SQLite, listed in CPython's `LICENSE`) | Yes (PSF-2.0 is GPL-compatible) |
| [Cython](https://cython.org/) | 3.0.12 | build dependency | Generates C/C++ for the compiled core | Apache-2.0 | Yes (Apache-2.0 is compatible with GPLv3/AGPLv3) |
| [toml](https://github.com/uiri/toml) | 0.10.2 | runtime dependency | Config parsing; shipped in releases | MIT | Yes |
| [py_trees](https://github.com/splintered-reality/py_trees) | 2.5.0 | runtime dependency | Bot behaviour trees; shipped in releases | BSD-3-Clause | Yes |
| [pydot](https://github.com/pydot/pydot) / [pyparsing](https://github.com/pyparsing/pyparsing) | per lock | dependencies of py_trees | Shipped in releases when collected | MIT | Yes |
| [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) | 5.2.2 | runtime dependency of `BattleSpadesServer` | Desktop window widgets; shipped in releases | MIT | Yes |
| [darkdetect](https://github.com/albertosottile/darkdetect) / [packaging](https://github.com/pypa/packaging) | 0.8.0 / 25.0 | dependencies of CustomTkinter | Shipped in releases | BSD-3-Clause / Apache-2.0 or BSD-2-Clause | Yes |
| [tomlkit](https://github.com/python-poetry/tomlkit) | 0.13.2 | runtime dependency of `BattleSpadesServer` | Comment-preserving `config.toml` edits; shipped in releases | MIT | Yes |
| [Tcl/Tk](https://www.tcl.tk/) | 8.6 (from CPython) | release archives only | Toolkit under the desktop window | Tcl/Tk license (BSD-style) | Yes |
| [Barlow Condensed](https://github.com/jpt/barlow) | 1.4 | `server_gui/fonts/` | Heading font of the desktop window; shipped in releases | SIL OFL 1.1 — [`server_gui/fonts/OFL.txt`](server_gui/fonts/OFL.txt) | Yes |
| [setuptools](https://github.com/pypa/setuptools) | 80.9.0 | build dependency | Extension build | MIT | Yes (build tool only) |
| [PyInstaller](https://pyinstaller.org/) | 6.11.1 | release build tool | Produces the portable launchers; its bootloader is embedded in each executable | GPL-2.0-or-later **with the PyInstaller bootloader exception**, which permits distributing the resulting executables under any terms | Yes (build tool; bootloader exception) |
| [pytest](https://pytest.org/) / pytest-asyncio | per `requirements*.txt` | development only | Test suite; not shipped | MIT | n/a (not shipped) |

Valve's Steamworks runtime (`steam_api.dll`, `steamclient.dll`, …) is **not**
part of this repository. Optional master-server registration loads
operator-supplied copies at run time; those files are Valve's property and
are governed by Valve's terms (see
[`release/STEAM_RUNTIME.txt`](release/STEAM_RUNTIME.txt)). Release archives
that carry a Steam helper include one Valve file beside it, the Steamworks
SDK's redistributable API library (`steam_api64.dll` / `libsteam_api.so`,
unmodified, under Valve's Steamworks SDK terms); `steamclient` itself is
never included. They are
proprietary and **not** AGPL-compatible on their own; combining BattleSpades
with them is allowed by the section 7 additional permission in
[`LICENSING.md`](LICENSING.md#additional-permission-for-steamworks-agpl-section-7).

## Earlier MIT-licensed code

BattleSpades releases up to and including **v0.1.0-beta.1** were published
under the MIT License. Those copies remain available under MIT. MIT is compatible with the AGPL, so code from that
period that is still present in later versions (including community
contributions adopted before the license change, such as PR #1 by
@TylerJaacks) is now distributed as part of the AGPL work and keeps the
following notice, as the MIT License requires:

```
MIT License

Copyright (c) 2026 KikoTs and the BattleSpades contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Game content and data (not covered by any project license)

"Ace of Spades" was created by Ben Aksoy and published by Jagex. All original
game content belongs to its respective owners. The AGPL license of BattleSpades
covers **none** of the following and grants no rights in them, and you are responsible for having the
right to use or redistribute them. Where they travel with the program they
are separate works in an "aggregate" (AGPL section 5), not part of the
AGPL-licensed program:

- **`maps/*.vxl`** — stock Ace of Spades map voxel data (and, for
  `20thCenturyTown`, a non-retail community map). Owned by their respective
  creators/rights holders. Shipped in the repository and release archives for
  preservation and compatibility.
- **`maps/*.json`, `maps/*.botnav`** — spawn/metadata sidecars and bot
  navigation meshes generated from those maps. Where they reproduce retail map
  metadata (see [`docs/MAP_METADATA.md`](docs/MAP_METADATA.md)), that content
  belongs to its owners.
- **`prefabs/*.kv6`** — prefab models used by the retail construction system.
- **Protocol, packet, weapon, class and string-id tables** in `shared/`,
  `protocol/` and `docs/` were measured or reverse-engineered for
  interoperability. The facts they describe are not claimed by this project;
  any retail text reproduced verbatim belongs to its owners.
- **`client_patches/`** contains project-written patch scripts. The retail
  client they patch is not included and is not licensed by this project.
- The retail client, its executables, and its assets are never distributed by
  this repository. The Map Creator and tutorial read baseplates and KV6
  catalogues from a legally installed copy supplied by the operator.

## Full license texts

### pyenet — BSD-3-Clause

```
Copyright (C) 2003, Scott Robinson <scott@tranzoa.com>
Copyright (c) 2009,2010 Andrew Resch <andrewresch@gmail.com>
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

    * Redistributions of source code must retain the above copyright notice,
      this list of conditions and the following disclaimer.
    * Redistributions in binary form must reproduce the above copyright
      notice, this list of conditions and the following disclaimer in the
      documentation and/or other materials provided with the distribution.
    * Neither the name of the <ORGANIZATION> nor the names of its
      contributors may be used to endorse or promote products derived from
      this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
```

### ENet — MIT

```
Copyright (c) 2002-2020 Lee Salzman

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Recast & Detour — Zlib

```
Copyright (c) 2009 Mikko Mononen memon@inside.org

This software is provided 'as-is', without any express or implied
warranty.  In no event will the authors be held liable for any damages
arising from the use of this software.

Permission is granted to anyone to use this software for any purpose,
including commercial applications, and to alter it and redistribute it
freely, subject to the following restrictions:

1. The origin of this software must not be misrepresented; you must not
claim that you wrote the original software. If you use this software
in a product, an acknowledgment in the product documentation would be
appreciated but is not required.
2. Altered source versions must be plainly marked as such, and must not be
misrepresented as being the original software.
3. This notice may not be removed or altered from any source distribution.
```

The remaining components (CPython, Cython, toml, py_trees, pydot, pyparsing,
setuptools, PyInstaller) are unmodified upstream packages; their license texts
are included in their installed distributions (`*.dist-info/LICENSE*`) and at
the upstream links above.
