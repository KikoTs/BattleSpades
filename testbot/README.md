# testbot — headless py2 wire-conformance bot

A minimal Python 2.7 (32-bit) ENet client that uses the **original game's**
compiled modules at `G:\AoSRevival\aceofspades_nonsteam\` to talk to our
BattleSpades server, runs a scripted scenario, and prints structured JSON
events to stdout.

> **Always run with `py2`.** This package will not import under Python 3.
> The whole point is to use the original `shared.packet.pyd`,
> `shared.bytes.pyd`, `shared.lzf.pyd`, `enet.pyd` — so if our server emits
> something the original packet decoder cannot parse, the bot breaks **the
> same way a real client does**.

## Direct usage (manual)

```powershell
# Bot only — assumes a server is already running on the configured port
py2 testbot\run.py --scenario connect_only --port 27015
```

Stdout: one JSON event per line.
Stderr: human-readable log.

## Driven by harness

Most of the time you don't run the bot directly — let `py harness.py` do it:

```powershell
py harness.py --scenario connect_only
```

That builds cython if stale, starts the server in the background, runs the
bot as a subprocess, asserts on the JSON stream, tears the server down, and
reports pass/fail.

## Scenarios

See `testbot/scenarios/`. Each scenario is one Python file exporting:

```python
NAME    = "connect_only"
TIMEOUT = 10.0           # seconds — bot exits with timeout if exceeded
def script(c):           # c is a testbot.client.Client
    c.connect()
    c.expect("InitialInfo", timeout=5.0)
    c.disconnect()
```

The framework around `script()` handles the JSON event log, the ENet pump,
and the wire layer.
