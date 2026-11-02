# Classic CTF command posts

The archived wiki's `Command_Post` article and `CommandPost.png` identify the
medical tent as the Classic CTF team's capture and resupply point. It uses the
existing `kv6/cp.kv6` model. The wiki's `Crates` article explicitly excludes
Classic CTF from map supply crates.

Classic CTF now creates two team-owned `CAPTURE_POINT_ENTITY` (25) tents.
The original retail client supports that entity and model. Legacy `BASE` (1)
remains private in ordinary CTF because the retail scene's entity table does
not support it. No unsupported entity or new packet is sent to retail peers.

Living players within three horizontal blocks and two vertical blocks of
their own tent's floor can resupply when the line of sight is clear. The
server restores health, block wallet, reserve ammunition and grenade stock.
It preserves the loaded magazine and an active reload. The Classic shotgun
requires up to three ordinary ammo refill packets to reach its 45-round
reserve cap; a spawn/general Restock would reset it to only 20 reserve rounds.
These are normal supported ammo, health and block updates.

Contact refills immediately, then at most once every three seconds. That
throttle follows the surviving community Classic CTF implementation; the
wiki does not specify an exact refill interval. It is not a recovered retail
server timing value. Dead players, enemies, players behind walls or on other
floors, and ended matches do not receive supplies. Round restart, disconnect
and team change clear the relevant contact history.

The 0.75/0.76 tent is owned by the remote legacy server. The client adapter
already maps its base into a local CP-model entity. Its initial creation was
discarded when it arrived before the asynchronous world existed. The frontend
now retains ordered entity lifecycle events until the scene is ready,
clearing them at map change. Merely showing the tent grants no local
supplies: the remote server's refill permissions and packets stay authoritative.

Checks: `tests/test_classic_command_posts.py`, the existing Classic CTF/CTF
entity and ammo suites, client frontend/Classic adapter regressions, and
offscreen world renders of the same CP asset used by both paths.
