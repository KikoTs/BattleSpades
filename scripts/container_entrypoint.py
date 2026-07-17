#!/usr/bin/env python3
"""Generate one container-owned BattleSpades config and launch the server.

The repository's ``config.toml`` remains the complete documented template.
Each container copies that template, applies a deliberately small set of
validated environment overrides, and writes the effective configuration under
the instance data directory. Secrets are never printed or written back into
the source tree.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

import toml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SAFE_MAP = re.compile(r"^[A-Za-z0-9_. -]{1,96}$")

# Direct script execution puts only ``scripts/`` on sys.path. The container
# launches this file by path, so make the application packages importable
# without depending on the current working directory.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


class ContainerConfigurationError(ValueError):
    """Raised when a container environment value is unsafe or unusable."""


def _table(
    document: MutableMapping[str, Any],
    name: str,
) -> MutableMapping[str, Any]:
    """Return a TOML table, creating it when the template omits it."""

    value = document.setdefault(name, {})
    if not isinstance(value, MutableMapping):
        raise ContainerConfigurationError(f"{name!r} must be a TOML table")
    return value


def _optional_text(
    environment: Mapping[str, str],
    name: str,
    *,
    maximum: int,
) -> str | None:
    """Read a bounded printable environment value when present."""

    raw = environment.get(name)
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        raise ContainerConfigurationError(f"{name} cannot be empty")
    if len(value) > maximum:
        raise ContainerConfigurationError(
            f"{name} must be at most {maximum} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ContainerConfigurationError(
            f"{name} cannot contain control characters"
        )
    return value


def _optional_integer(
    environment: Mapping[str, str],
    name: str,
    *,
    minimum: int,
    maximum: int,
) -> int | None:
    """Read one bounded base-10 integer environment value."""

    raw = environment.get(name)
    if raw is None:
        return None
    try:
        value = int(raw.strip(), 10)
    except ValueError as exc:
        raise ContainerConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ContainerConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _optional_boolean(
    environment: Mapping[str, str],
    name: str,
) -> bool | None:
    """Read a strict human-readable boolean environment value."""

    raw = environment.get(name)
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ContainerConfigurationError(
        f"{name} must be true/false, yes/no, on/off, or 1/0"
    )


def _safe_token(value: str, name: str) -> str:
    """Validate a short mode or region token."""

    if not _SAFE_TOKEN.fullmatch(value):
        raise ContainerConfigurationError(
            f"{name} may contain only letters, digits, _, ., and -"
        )
    return value


def _safe_map_name(value: str) -> str:
    """Validate a map stem without allowing path traversal."""

    if (
        not _SAFE_MAP.fullmatch(value)
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).name != value
    ):
        raise ContainerConfigurationError(
            "BATTLESPADES_MAP must be a map name, not a path"
        )
    return value


def load_template(path: Path) -> dict[str, Any]:
    """Load a required TOML deployment template."""

    try:
        document = toml.load(path)
    except (OSError, toml.TomlDecodeError) as exc:
        raise ContainerConfigurationError(
            f"cannot load BattleSpades config template: {path}"
        ) from exc
    if not isinstance(document, dict):
        raise ContainerConfigurationError("config template must contain a TOML document")
    return document


def build_runtime_config(
    template: Mapping[str, Any],
    environment: Mapping[str, str],
    *,
    data_directory: Path,
) -> dict[str, Any]:
    """Apply the supported container environment contract to a TOML document."""

    # Round-trip through TOML to obtain a plain, independent mapping without
    # retaining references into a caller-owned template.
    document = toml.loads(toml.dumps(dict(template)))
    server = _table(document, "server")
    game = _table(document, "game")
    bots = _table(document, "bots")
    admin = _table(document, "admin")
    steam = _table(document, "steam")
    revival = _table(document, "revival")

    server_name = _optional_text(
        environment,
        "BATTLESPADES_SERVER_NAME",
        maximum=64,
    )
    if server_name is not None:
        server["name"] = server_name

    port = _optional_integer(
        environment,
        "BATTLESPADES_PORT",
        minimum=1,
        maximum=65535,
    )
    if port is not None:
        server["port"] = port

    max_players = _optional_integer(
        environment,
        "BATTLESPADES_MAX_PLAYERS",
        minimum=1,
        maximum=255,
    )
    if max_players is not None:
        server["max_players"] = max_players

    mode = _optional_text(environment, "BATTLESPADES_MODE", maximum=32)
    if mode is not None:
        game["default_mode"] = _safe_token(mode.lower(), "BATTLESPADES_MODE")

    map_name = _optional_text(environment, "BATTLESPADES_MAP", maximum=96)
    if map_name is not None:
        game["default_map"] = _safe_map_name(map_name)

    bot_count = _optional_integer(
        environment,
        "BATTLESPADES_BOT_COUNT",
        minimum=0,
        maximum=254,
    )
    if bot_count is not None:
        game["bot_count"] = bot_count
        bots["enabled"] = bot_count > 0
        bots["population_mode"] = "fixed"
        bots["fill_target"] = bot_count
        bots["max_bots"] = bot_count

    admin_password = _optional_text(
        environment,
        "BATTLESPADES_ADMIN_PASSWORD",
        maximum=128,
    )
    if admin_password is not None:
        if len(admin_password) < 12:
            raise ContainerConfigurationError(
                "BATTLESPADES_ADMIN_PASSWORD must contain at least 12 characters"
            )
        admin["password"] = admin_password
    admin["bans_path"] = str(data_directory / "bans.json")

    region = _optional_text(environment, "BATTLESPADES_REGION", maximum=32)
    if region is not None:
        revival["region"] = _safe_token(region.lower(), "BATTLESPADES_REGION")

    official = _optional_boolean(environment, "BATTLESPADES_OFFICIAL")
    if official is not None:
        revival["official"] = official

    require_identity = _optional_boolean(
        environment,
        "BATTLESPADES_REQUIRE_IDENTITY",
    )
    if require_identity is not None:
        revival["require_identity"] = require_identity

    revival_enabled = _optional_boolean(
        environment,
        "BATTLESPADES_REVIVAL_ENABLED",
    )
    if revival_enabled is not None:
        revival["enabled"] = revival_enabled

    # The Steam browser bridge owns a Windows x86 Steamworks runtime. Linux
    # containers deliberately use the Revival registry instead. Retail Steam
    # players remain supported through the server-side identity bridge.
    steam["enabled"] = False
    steam["public"] = False
    steam["require_registration"] = False
    steam["runtime_dir"] = ""
    steam["steamclient_dir"] = ""
    steam["helper_path"] = ""

    return document


def validate_launch_config(
    document: Mapping[str, Any],
    arguments: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    """Refuse an internet-facing launch with the sample administrator secret."""

    if "--check" in arguments or "--version" in arguments:
        return
    allow_insecure = _optional_boolean(
        environment,
        "BATTLESPADES_ALLOW_INSECURE_DEFAULTS",
    )
    admin = document.get("admin", {})
    password = admin.get("password") if isinstance(admin, Mapping) else None
    if password == "changeme" and not allow_insecure:
        raise ContainerConfigurationError(
            "set BATTLESPADES_ADMIN_PASSWORD before starting the server"
        )


def write_runtime_config(document: Mapping[str, Any], path: Path) -> None:
    """Atomically write the effective config with owner-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".config-",
        suffix=".toml",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            toml.dump(dict(document), stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main(arguments: Sequence[str] | None = None) -> int:
    """Generate the effective config and enter the normal server lifecycle."""

    argv = list(sys.argv[1:] if arguments is None else arguments)
    template_path = Path(
        os.environ.get(
            "BATTLESPADES_CONFIG_TEMPLATE",
            str(PROJECT_ROOT / "config.toml"),
        )
    ).expanduser().resolve()
    data_directory = Path(
        os.environ.get("BATTLESPADES_DATA_DIR", "/data")
    ).expanduser().resolve()
    runtime_config = Path(
        os.environ.get(
            "BATTLESPADES_RUNTIME_CONFIG",
            str(data_directory / "runtime" / "config.toml"),
        )
    ).expanduser().resolve()

    try:
        data_directory.mkdir(parents=True, exist_ok=True)
        document = build_runtime_config(
            load_template(template_path),
            os.environ,
            data_directory=data_directory,
        )
        validate_launch_config(document, argv, os.environ)
        write_runtime_config(document, runtime_config)
    except (ContainerConfigurationError, OSError) as exc:
        print(f"BattleSpades container configuration error: {exc}", file=sys.stderr)
        return 2

    server = document.get("server", {})
    game = document.get("game", {})
    print(
        "BattleSpades container configuration ready: "
        f"name={server.get('name')!r} "
        f"port={server.get('port')} "
        f"mode={game.get('default_mode')} "
        f"map={game.get('default_map')}",
        flush=True,
    )

    from server.launcher import run
    from server.runtime_paths import RuntimePaths

    paths = RuntimePaths(
        root=PROJECT_ROOT,
        config=runtime_config,
        maps=PROJECT_ROOT / "maps",
        prefabs=PROJECT_ROOT / "prefabs",
        plugins=PROJECT_ROOT / "plugins",
        logs=data_directory / "logs",
        bans=data_directory / "bans.json",
    )
    return run(argv, paths=paths)


if __name__ == "__main__":
    raise SystemExit(main())
