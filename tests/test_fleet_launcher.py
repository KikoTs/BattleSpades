"""Multi-config fleet launcher contracts."""

from __future__ import annotations

from pathlib import Path

import pytest

from server import launcher
from server.fleet_launcher import (
    FleetInstance,
    instance_command,
    load_fleet_manifest,
)
from server.runtime_paths import RuntimePaths


def _write_config(path: Path, port: int) -> Path:
    """Write one minimal dedicated-server profile."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"[server]\nname = \"Test\"\nport = {port}\n",
        encoding="utf-8",
    )
    return path


def test_manifest_resolves_unicode_relative_configs_and_ports(
    tmp_path: Path,
) -> None:
    """Windows usernames/folders do not pass through a shell or byte codec."""

    config = _write_config(tmp_path / "сървъри" / "тдм.toml", 30101)
    manifest = tmp_path / "fleet.toml"
    manifest.write_text(
        "\n".join(
            (
                "[fleet]",
                "shutdown_timeout_seconds = 7",
                "[[instances]]",
                'name = "Официален TDM"',
                'config = "сървъри/тдм.toml"',
                "enabled = true",
            )
        ),
        encoding="utf-8",
    )

    loaded = load_fleet_manifest(manifest)

    assert loaded.shutdown_timeout_seconds == 7.0
    assert loaded.instances == (
        FleetInstance("Официален TDM", config.resolve(), 30101),
    )
    command = instance_command(
        loaded.instances[0],
        source_entry=tmp_path / "run_server.py",
        executable=tmp_path / "python.exe",
        frozen=False,
    )
    assert command == [
        str((tmp_path / "python.exe").resolve()),
        str((tmp_path / "run_server.py").resolve()),
        "--config",
        str(config.resolve()),
        "--port",
        "30101",
        "--control-stdin",
    ]


def test_manifest_rejects_duplicate_effective_ports_before_launch(
    tmp_path: Path,
) -> None:
    """A fleet cannot partially start with two instances on one UDP port."""

    _write_config(tmp_path / "one.toml", 30101)
    _write_config(tmp_path / "two.toml", 30101)
    manifest = tmp_path / "fleet.toml"
    manifest.write_text(
        "\n".join(
            (
                "[[instances]]",
                'name = "One"',
                'config = "one.toml"',
                "[[instances]]",
                'name = "Two"',
                'config = "two.toml"',
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate fleet UDP port"):
        load_fleet_manifest(manifest)


def test_manifest_port_override_is_process_local(tmp_path: Path) -> None:
    """One profile can be reused with explicit unique per-instance ports."""

    config = _write_config(tmp_path / "shared.toml", 30101)
    manifest = tmp_path / "fleet.toml"
    manifest.write_text(
        "\n".join(
            (
                "[[instances]]",
                'name = "One"',
                'config = "shared.toml"',
                "port = 30103",
                "[[instances]]",
                'name = "Two"',
                'config = "shared.toml"',
                "port = 30105",
            )
        ),
        encoding="utf-8",
    )

    loaded = load_fleet_manifest(manifest)

    assert [instance.config for instance in loaded.instances] == [
        config.resolve(),
        config.resolve(),
    ]
    assert [instance.port for instance in loaded.instances] == [30103, 30105]


def test_server_cli_rejects_fleet_mixed_with_single_instance_flags(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ambiguous fleet/single-server arguments fail without starting children."""

    result = launcher.run(
        ["--fleet", "fleet.toml", "--port", "30101"],
        paths=RuntimePaths.from_root(tmp_path),
    )

    assert result == 2
    assert "--fleet cannot be combined" in capsys.readouterr().err
