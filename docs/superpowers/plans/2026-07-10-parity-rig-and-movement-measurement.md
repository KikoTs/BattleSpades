# Parity Rig and Movement Measurement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a safe isolated validation server plus two-client measurement harness that reproduces and records strict-authority movement rollback without modifying or restarting the live server.

**Architecture:** A validation launcher loads the normal configuration into memory, applies explicit test-only overrides, rejects the public port, and starts a normal `BattleSpadesServer`. A parity controller launches two dev clients with unique tracer ports, drives their normal input pipeline, and saves correlated JSON artifacts. Movement correction is measured from exact client history/network fields before any further netcode change.

**Tech Stack:** Python 3.12, asyncio, subprocess, pytest, existing `GameConsole`, stock bundled Python 2 client, BattleSpades server.

---

### Task 1: Safe validation-server configuration

**Files:**
- Create: `server/validation.py`
- Create: `tests/test_validation_rig.py`

- [ ] **Step 1: Write the failing configuration tests**

```python
from pathlib import Path

import pytest

from server.config import ServerConfig
from server.validation import build_validation_config


def test_validation_config_overrides_runtime_values_without_mutating_source():
    source = ServerConfig(port=27015, default_map="CityOfChicago", default_mode="tdm")

    result = build_validation_config(source, port=27016, map_name="ArcticBase", mode="tdm")

    assert result.port == 27016
    assert result.default_map == "ArcticBase"
    assert result.default_mode == "tdm"
    assert result.name.endswith("[VALIDATION]")
    assert source.port == 27015
    assert source.default_map == "CityOfChicago"


def test_validation_config_refuses_public_port():
    with pytest.raises(ValueError, match="public server port"):
        build_validation_config(ServerConfig(port=27015), port=27015)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
py -3.12 -m pytest tests/test_validation_rig.py -q
```

Expected: collection fails because `server.validation` does not exist.

- [ ] **Step 3: Implement immutable validation config construction**

```python
from copy import deepcopy

from server.config import ServerConfig


PUBLIC_SERVER_PORT = 27015
DEFAULT_VALIDATION_PORT = 27016


def build_validation_config(
    source: ServerConfig,
    *,
    port: int = DEFAULT_VALIDATION_PORT,
    map_name: str = "ArcticBase",
    mode: str = "tdm",
) -> ServerConfig:
    if int(port) == PUBLIC_SERVER_PORT:
        raise ValueError("validation server cannot use the public server port")
    config = deepcopy(source)
    config.port = int(port)
    config.default_map = str(map_name)
    config.default_mode = str(mode)
    config.name = f"{source.name} [VALIDATION]"
    config.bot_count = 0
    return config
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run:

```powershell
py -3.12 -m pytest tests/test_validation_rig.py -q
```

Expected: 2 tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add server/validation.py tests/test_validation_rig.py
git commit -m "test: add isolated validation server config"
```

### Task 2: Validation server entry point

**Files:**
- Create: `scripts/run_validation_server.py`
- Modify: `tests/test_validation_rig.py`

- [ ] **Step 1: Add a failing CLI-default test**

```python
from scripts.run_validation_server import parse_args


def test_validation_launcher_defaults_are_isolated():
    args = parse_args([])
    assert args.port == 27016
    assert args.map_name == "ArcticBase"
    assert args.mode == "tdm"
    assert args.config == Path("config.toml")
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
py -3.12 -m pytest tests/test_validation_rig.py::test_validation_launcher_defaults_are_isolated -q
```

Expected: import fails because `scripts.run_validation_server` does not exist.

- [ ] **Step 3: Implement the isolated entry point**

The script must parse `--config`, `--port`, `--map`, and `--mode`, call
`load_config`, call `build_validation_config`, instantiate
`BattleSpadesServer`, install the same shutdown handling as `run_server.py`, and
run until interrupted. It must write stdout to the caller-selected destination;
it must not edit `config.toml`.

Use this exact public parser surface:

```python
def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--port", type=int, default=DEFAULT_VALIDATION_PORT)
    parser.add_argument("--map", dest="map_name", default="ArcticBase")
    parser.add_argument("--mode", default="tdm")
    return parser.parse_args(argv)
```

- [ ] **Step 4: Verify the focused tests and CLI help**

Run:

```powershell
py -3.12 -m pytest tests/test_validation_rig.py -q
py -3.12 scripts/run_validation_server.py --help
```

Expected: tests pass; help exits without binding a UDP port.

- [ ] **Step 5: Commit Task 2**

```powershell
git add scripts/run_validation_server.py tests/test_validation_rig.py
git commit -m "feat: add isolated validation server launcher"
```

### Task 3: Two-client launch specification

**Files:**
- Create: `scripts/parity_clients.py`
- Modify: `tests/test_validation_rig.py`

- [ ] **Step 1: Add failing client-spec tests**

```python
from scripts.parity_clients import build_client_specs


def test_two_client_specs_use_unique_tracer_ports():
    specs = build_client_specs("127.0.0.1:27016")
    assert [spec.console_port for spec in specs] == [32896, 32897]
    assert [spec.tracer_port for spec in specs] == [32895, 32898]
    assert all(spec.connect_target == "127.0.0.1:27016" for spec in specs)
    assert len({spec.capture_dir for spec in specs}) == 2
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
py -3.12 -m pytest tests/test_validation_rig.py::test_two_client_specs_use_unique_tracer_ports -q
```

Expected: import fails because `scripts.parity_clients` does not exist.

- [ ] **Step 3: Implement client specifications and launch/stop helpers**

Define an immutable `ClientSpec` with client index, bundled Python path,
working directory, connect target, console port, tracer port, and capture
directory. `build_client_specs` returns exactly two specs. `launch_client`
starts the bundled Python with `run.py +debug +connect <target>`, supplies
`PHYSICS_TRACER_CONSOLE_PORT`, `PHYSICS_TRACER_PORT`, and a distinct capture
directory in the child environment, and starts the process hidden. `stop_client`
terminates only the process object it was given and waits for exit.

- [ ] **Step 4: Run focused tests**

Run:

```powershell
py -3.12 -m pytest tests/test_validation_rig.py -q
```

Expected: all validation-rig unit tests pass without launching a real client.

- [ ] **Step 5: Commit Task 3**

```powershell
git add scripts/parity_clients.py tests/test_validation_rig.py
git commit -m "feat: define isolated two-client parity launch"
```

### Task 4: Correlated scenario artifact

**Files:**
- Create: `scripts/parity_artifact.py`
- Modify: `tests/test_validation_rig.py`

- [ ] **Step 1: Add a failing artifact round-trip test**

```python
import json

from scripts.parity_artifact import ParityArtifact


def test_parity_artifact_preserves_correlated_snapshots(tmp_path):
    artifact = ParityArtifact("movement_walk")
    artifact.record("walk_start", server={"loop": 10}, client_a={"loop": 12}, client_b={"tool": 7})
    path = artifact.write(tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["scenario"] == "movement_walk"
    assert data["samples"][0]["marker"] == "walk_start"
    assert data["samples"][0]["server"]["loop"] == 10
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
py -3.12 -m pytest tests/test_validation_rig.py::test_parity_artifact_preserves_correlated_snapshots -q
```

Expected: import fails because `scripts.parity_artifact` does not exist.

- [ ] **Step 3: Implement deterministic UTF-8 JSON artifact writing**

`ParityArtifact.record` appends timestamped marker dictionaries. `write`
creates the destination, writes `<scenario>-<utc timestamp>.json` with sorted
keys and indentation, and returns the path. No global log file is used.

- [ ] **Step 4: Run focused and full tests**

Run:

```powershell
py -3.12 -m pytest tests/test_validation_rig.py -q
py -3.12 -m pytest -q
```

Expected: validation tests pass and the full suite remains at least 183 tests
with zero failures.

- [ ] **Step 5: Commit Task 4**

```powershell
git add scripts/parity_artifact.py tests/test_validation_rig.py
git commit -m "feat: record correlated parity artifacts"
```

### Task 5: Movement baseline scenario

**Files:**
- Create: `scripts/scenarios/movement_baseline.py`
- Modify: `tests/test_validation_rig.py`

- [ ] **Step 1: Add failing pure-analysis tests**

The analysis function accepts samples containing matched-loop error,
movement-history length, and lerp timer. Tests must prove it counts SNAP when
history is wiped, ADJUST when the lerp timer rearms, and fails when matched-loop
linear error exceeds `0.1` blocks.

- [ ] **Step 2: Run the analysis tests and verify RED**

Run:

```powershell
py -3.12 -m pytest tests/test_validation_rig.py -k movement_analysis -q
```

Expected: import or assertion failure because the analyzer does not exist.

- [ ] **Step 3: Implement scenario collection and analysis**

The scenario connects to both `GameConsole` ports, verifies both clients are in
`GameScene`, records a start marker, drives Client A through W walk, diagonal
walk, sprint, crouch walk, crouch release, jump, and wall-contact segments using
real key events, samples Client A reconciliation fields and Client B observer
state, writes the artifact, and exits nonzero on any SNAP or sustained ADJUST.

- [ ] **Step 4: Run unit tests, then the isolated live scenario**

Run unit tests first. Start `scripts/run_validation_server.py` on 27016, launch
the two client specs, join them with console ports 32896 and 32897, then run the
movement baseline. Preserve the first failing artifact; do not tune production
netcode during this task.

- [ ] **Step 5: Commit Task 5**

```powershell
git add scripts/scenarios/movement_baseline.py tests/test_validation_rig.py
git commit -m "test: capture two-client movement reconciliation baseline"
```
