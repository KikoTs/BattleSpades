"""Health-probe and registration helper tests."""

from __future__ import annotations

import argparse
from pathlib import Path
import struct

import pytest

from deploy.a2s_probe import decode_info
from deploy.register_server import Registration, bounded_integer


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_a2s_probe_decodes_battlespades_identity_and_population() -> None:
    payload = (
        b"\xff\xff\xff\xff"
        + bytes([0x49, 168])
        + b"AoS Revival EU\x00"
        + b"MayanJungle\x00"
        + b"aceofspades\x00"
        + b"Ace of Spades\x00"
        + struct.pack("<H", 224540 & 0xFFFF)
        + bytes([7, 24, 3])
    )

    info = decode_info(payload)

    assert info.protocol == 168
    assert info.name == "AoS Revival EU"
    assert info.map_name == "MayanJungle"
    assert info.game_directory == "aceofspades"
    assert info.players == 7
    assert info.max_players == 24
    assert info.bots == 3


def test_registration_uses_same_game_and_query_port_without_claiming_an_ip() -> None:
    payload = Registration(
        port=27015,
        name="AoS Revival EU / CTF",
        map_name="MayanJungle",
        game_mode="CTF",
        max_players=24,
        region="europe",
    ).payload()

    assert payload["port"] == 27015
    assert payload["query_port"] == 27015
    assert "ip" not in payload
    assert "host" not in payload


@pytest.mark.parametrize("value", ["0", "65536", "not-a-port"])
def test_registration_rejects_invalid_ports(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        bounded_integer(value, 1, 65535)


def test_container_initializes_provider_volume_then_drops_privileges() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    initializer = (
        PROJECT_ROOT / "scripts" / "container_init.sh"
    ).read_text(encoding="utf-8")

    assert "ca-certificates gosu tini" in dockerfile
    assert (
        'ENTRYPOINT ["/usr/bin/tini", "--", "/app/scripts/container_init.sh"]'
        in dockerfile
    )
    assert "USER 10001:10001" not in dockerfile
    assert 'case "$data_dir" in' in initializer
    assert 'chown battlespades:battlespades "$data_dir"' in initializer
    assert "exec gosu battlespades:battlespades" in initializer
