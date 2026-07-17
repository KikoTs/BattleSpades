#!/usr/bin/env python3
"""Perform a bounded A2S_INFO health probe against BattleSpades."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import socket
import struct
import sys


INFO_REQUEST = b"\xff\xff\xff\xffTSource Engine Query\x00"
CHALLENGE_RESPONSE = 0x41
INFO_RESPONSE = 0x49


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """Health-relevant fields decoded from one A2S_INFO response."""

    protocol: int
    name: str
    map_name: str
    game_directory: str
    players: int
    max_players: int
    bots: int


def _cstring(payload: bytes, offset: int) -> tuple[str, int]:
    """Decode one bounded UTF-8 C string from an A2S response."""

    end = payload.find(b"\x00", offset)
    if end < 0:
        raise ValueError("A2S response contains an unterminated string")
    return payload[offset:end].decode("utf-8", "replace"), end + 1


def decode_info(payload: bytes) -> ServerInfo:
    """Decode the fixed A2S_INFO prefix and population fields."""

    if len(payload) < 7 or payload[:4] != b"\xff\xff\xff\xff":
        raise ValueError("invalid A2S response prefix")
    if payload[4] != INFO_RESPONSE:
        raise ValueError(f"unexpected A2S response type 0x{payload[4]:02x}")
    protocol = payload[5]
    name, offset = _cstring(payload, 6)
    map_name, offset = _cstring(payload, offset)
    game_directory, offset = _cstring(payload, offset)
    _, offset = _cstring(payload, offset)
    if len(payload) < offset + 5:
        raise ValueError("truncated A2S population fields")
    offset += 2  # historical uint16 AppID
    players, max_players, bots = payload[offset : offset + 3]
    return ServerInfo(
        protocol=protocol,
        name=name,
        map_name=map_name,
        game_directory=game_directory,
        players=players,
        max_players=max_players,
        bots=bots,
    )


def probe(host: str, port: int, timeout: float) -> ServerInfo:
    """Challenge and validate one UDP A2S endpoint."""

    endpoint = (host, port)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(timeout)
        client.sendto(INFO_REQUEST, endpoint)
        response, _ = client.recvfrom(4096)
        if (
            len(response) >= 9
            and response[:4] == b"\xff\xff\xff\xff"
            and response[4] == CHALLENGE_RESPONSE
        ):
            challenge = struct.unpack("<i", response[5:9])[0]
            client.sendto(INFO_REQUEST + struct.pack("<i", challenge), endpoint)
            response, _ = client.recvfrom(4096)
    info = decode_info(response)
    if info.protocol != 168:
        raise ValueError(f"expected protocol 168, received {info.protocol}")
    if info.game_directory != "aceofspades":
        raise ValueError(
            f"expected game directory aceofspades, received {info.game_directory!r}"
        )
    return info


def main() -> int:
    """Run a CLI probe suitable for rollout health gates."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--timeout", default=2.0, type=float)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not 0.1 <= arguments.timeout <= 30.0:
        parser.error("--timeout must be between 0.1 and 30 seconds")

    try:
        info = probe(arguments.host, arguments.port, arguments.timeout)
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"A2S probe failed: {exc}", file=sys.stderr)
        return 1
    if arguments.json:
        print(json.dumps(asdict(info), separators=(",", ":")))
    else:
        print(
            f"A2S healthy: {info.name} map={info.map_name} "
            f"players={info.players}/{info.max_players} bots={info.bots}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
