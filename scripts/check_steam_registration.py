"""Verify one BattleSpades host in Valve's current game-server registry.

Steam registration, direct query reachability, and the retail client's local
category filters are separate checks. This tool checks the first two:

1. ``ISteamApps/GetServersAtAddress`` contains the AoS app/game-dir record.
2. The advertised public query port answers a valid A2S_INFO request.

It uses only the standard library and should be run from another network to
verify public reachability. A pass alone does not prove retail UI visibility.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen


STEAM_APP_ID = 224540
STEAM_GAME_DIR = "aceofspades"
_MASTER_URL = (
    "https://api.steampowered.com/"
    "ISteamApps/GetServersAtAddress/v1/"
)
_A2S_INFO_REQUEST = b"\xff\xff\xff\xffTSource Engine Query\x00"


class ProbeError(RuntimeError):
    """Raised when registry or query-port evidence is missing or malformed."""


def matching_master_records(payload: Any) -> list[dict[str, Any]]:
    """Return only public Ace of Spades records from a Valve API payload."""

    if not isinstance(payload, dict):
        raise ProbeError("Valve registry response is not an object")
    response = payload.get("response")
    if not isinstance(response, dict) or response.get("success") is not True:
        raise ProbeError("Valve registry response did not report success")
    servers = response.get("servers", [])
    if not isinstance(servers, list):
        raise ProbeError("Valve registry servers field is not a list")
    return [
        record
        for record in servers
        if isinstance(record, dict)
        and int(record.get("appid", 0)) == STEAM_APP_ID
        and str(record.get("gamedir", "")).casefold() == STEAM_GAME_DIR
        and not bool(record.get("lan", False))
    ]


def parse_a2s_info(packet: bytes) -> dict[str, Any]:
    """Decode the stable prefix of one Source A2S_INFO response."""

    if len(packet) < 7 or packet[:5] != b"\xff\xff\xff\xffI":
        raise ProbeError("query port did not return A2S_INFO")
    offset = 5
    protocol = packet[offset]
    offset += 1

    def read_string() -> str:
        nonlocal offset
        end = packet.find(b"\x00", offset)
        if end < 0:
            raise ProbeError("truncated A2S_INFO string")
        value = packet[offset:end].decode("utf-8", "replace")
        offset = end + 1
        return value

    name = read_string()
    map_name = read_string()
    folder = read_string()
    game = read_string()
    if len(packet) < offset + 9:
        raise ProbeError("truncated A2S_INFO fixed fields")
    app_id16 = struct.unpack_from("<H", packet, offset)[0]
    offset += 2
    players, max_players, bots = packet[offset : offset + 3]
    offset += 3
    server_type = chr(packet[offset])
    environment = chr(packet[offset + 1])
    password = bool(packet[offset + 2])
    vac = bool(packet[offset + 3])
    offset += 4
    version = read_string()
    result = {
        "protocol": protocol,
        "name": name,
        "map": map_name,
        "folder": folder,
        "game": game,
        "app_id16": app_id16,
        "players": players,
        "max_players": max_players,
        "bots": bots,
        "server_type": server_type,
        "environment": environment,
        "password": password,
        "vac": vac,
        "version": version,
    }
    if offset < len(packet):
        flags = packet[offset]
        offset += 1

        def read_number(fmt: str) -> int:
            nonlocal offset
            size = struct.calcsize(fmt)
            if offset + size > len(packet):
                raise ProbeError("truncated A2S_INFO extra field")
            value = struct.unpack_from(fmt, packet, offset)[0]
            offset += size
            return value

        if flags & 0x80:
            result["game_port"] = read_number("<H")
        if flags & 0x10:
            result["steam_id"] = read_number("<Q")
        if flags & 0x40:
            result["spectator_port"] = read_number("<H")
            result["spectator_name"] = read_string()
        if flags & 0x20:
            result["tags"] = read_string()
        if flags & 0x01:
            result["game_id"] = read_number("<Q")
    return result


def fetch_master_records(address: str, timeout: float) -> list[dict[str, Any]]:
    """Fetch current Valve records for a public IP or IP:query-port."""

    url = f"{_MASTER_URL}?{urlencode({'addr': address})}"
    with urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed host
        payload = json.load(response)
    return matching_master_records(payload)


def query_a2s(host: str, port: int, timeout: float) -> dict[str, Any]:
    """Query A2S_INFO, including Valve's optional challenge round-trip."""

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((host, int(port)))
        sock.send(_A2S_INFO_REQUEST)
        packet = sock.recv(4096)
        if packet[:5] == b"\xff\xff\xff\xffA":
            if len(packet) < 9:
                raise ProbeError("truncated A2S challenge")
            sock.send(_A2S_INFO_REQUEST + packet[5:9])
            packet = sock.recv(4096)
    return parse_a2s_info(packet)


def _query_port(record: dict[str, Any]) -> int:
    try:
        return int(str(record["addr"]).rsplit(":", 1)[1])
    except (KeyError, ValueError, IndexError) as exc:
        raise ProbeError("Valve record has no valid query port") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", help="public IPv4 address or DNS name")
    parser.add_argument("--query-port", type=int, default=0)
    parser.add_argument("--game-port", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)

    try:
        records = fetch_master_records(args.address, args.timeout)
        if args.game_port:
            records = [
                record
                for record in records
                if int(record.get("gameport", 0)) == args.game_port
            ]
        if not records:
            raise ProbeError("no public app 224540 / aceofspades record found")
        record = records[0]
        query_port = args.query_port or _query_port(record)
        info = query_a2s(args.address, query_port, args.timeout)
        if info["folder"].casefold() != STEAM_GAME_DIR:
            raise ProbeError(
                f"A2S folder is {info['folder']!r}, expected {STEAM_GAME_DIR!r}"
            )
    except (OSError, ValueError, ProbeError) as exc:
        print(f"Steam registration check FAILED: {exc}", file=sys.stderr)
        return 1

    print("Steam registration check PASSED")
    print(json.dumps({"registry": record, "a2s": info}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
