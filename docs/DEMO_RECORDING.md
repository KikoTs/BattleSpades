# Recording server matches

The native BattleSpades client supports packet demos with `--record-demo FILE`
and local playback with `--play-demo FILE`. A demo contains the initial map and
timestamped incoming game packets, so it can be played without the original
game server or master server. Installed game assets are still required.

Native packages include **BattleSpadesDemoRecorder**, a console companion that
connects as a spectator and records without a window, renderer, installed game
graphics/audio assets, or master-server login:

```powershell
.\BattleSpadesDemoRecorder.exe +connect 127.0.0.1:27015 --record-demo demos\match.bsdem --duration 3600
```

Create `demos` first and keep the recorder with the package's shared libraries.
Omit `--duration` to run until Ctrl+C or SIGTERM; both close the recording cleanly.
The duration is measured in seconds after the first map loads (1..86400).
Optional `--name`, `--password` and `--protocol auto|168|0.75|0.76` select the
spectator's name, server password and transport. `--help` prints all options.
Use the regular client with `--play-demo demos\match.bsdem` to watch the result.

The recorder occupies one spectator slot, follows the server's spectator
visibility, and sends neutral readiness messages rather than gameplay input.
It cannot enter servers with spectators disabled. It performs no Steam or master
account login: use a normally authenticated client with `--record-demo FILE` for
servers requiring verified identity. No server plugin, master registration or
relay is required for direct LAN recording. An empty server can be recorded
unattended while the companion remains connected; the server binary itself does
not record when no recorder is connected.

The client requests a full map transfer when recording to make the file
self-contained. Map rotations within one connection remain in that file. A
disconnect/reconnect ends that recording; choose a new name for the next one,
because existing files are never intentionally overwritten. Recordings omit
outgoing credentials and inputs, but include incoming chat/player data visible
to that spectator. Review those before publishing.

See the native client's [demo documentation](https://github.com/KikoTs/BattleSpadesClient/blob/main/docs/DEMOS.md)
for the format, legacy ZeroSpades import, playback controls and current limits.
A future autonomous server recorder needs an initial map/roster snapshot and
well-defined visibility; a dump of broadcasts alone does not provide those.
