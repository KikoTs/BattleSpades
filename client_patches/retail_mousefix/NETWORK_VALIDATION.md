# Retail relay validation — 2026-10-01

Target: the supported retail `aos.pkg` SHA256 documented in README, original
Python 2.7 x86 client, Windows x64 helper, AppID 224540.

## Passed

* Native production and test helpers compile with MSVC `/W4 /WX /MT`.
* The Python 2 controller launches the native x64 helper and authenticates its
  private IPC channel; the helper initializes under AppID 224540.
* Production helper reaches Steam relay availability `Current` (100).
* Native loopback transport sends and receives seven byte-identical payloads,
  from 1 through 4,096 bytes, through both bridge processes.
* A second local sender cannot redirect the pinned game socket.
* Native ENet endpoints negotiate protocol 168 through the bridge, with range
  compression and a reliable fragmented 16 KiB payload in both directions.
* Cancel, native disconnect cleanup, reconnect and stale host-session rejection
  pass in that same integration run.
* Actual BattleSpades server loads 20thCenturyTown, creates a private Steam
  lobby through the production helper, publishes its local-host record, and
  closes the server/helper/lobby/record cleanly. This test did not advertise a
  public playable server.
* 20 Python 2 networking tests pass, including the installed retail bytecode's
  dynamic-port parser, password prompt and browser row construction/deduplication.
* 85 relevant server tests pass across relay identity, password admission,
  launcher, shutdown, vote-kick and legacy Steam master registration.
* All 59 existing fix tests pass under Python 2 with the installed bundle,
  together with 18 movement tests. The rebuilt x86 WinMM loader passes 10,000
  forwarded timer calls and audio-device/timer-resolution checks.
* The production helper imports only `steam_api64.dll`, `WS2_32.dll` and
  `KERNEL32.dll`; no external C++ runtime installer is required. Valve's
  bundled redistributable has a valid Authenticode signature.
* The ZIP's runtime checksums and archive integrity pass. Original retail EXE,
  PKG and three native gameplay modules still match their recorded hashes.

## Scope of evidence

The separately compiled `aos-retail-relay-test.exe` uses SteamNetworkingSockets
over local IP to exercise the bridge with one account. Its transport switch is
absent from the shipping `aos-retail-relay.exe`. These tests do not measure SDR
Internet routing or prove public lobby discovery between two accounts.

Required live follow-up: two owners on separate Internet connections, retail
UI browse/join/cancel, actual gameplay/map loading and map changes, full host
capacity, poor-network conditions, sustained sessions, overlay/presence, and
minimize/restore. The helper uses a separate Steam runtime, but simultaneous
operation with the original retail process still needs that live gate.

The C++ client was a read-only reference. No file in that project was edited,
and neither its executable nor its bundled server was rebuilt.
