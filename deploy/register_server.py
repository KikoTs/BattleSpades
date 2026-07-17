#!/usr/bin/env python3
"""Register one already-running BattleSpades instance with the Revival API.

Run this command on the game node itself. The registry intentionally trusts the
HTTP source address rather than a caller-supplied IP, then probes the same
public address over UDP before publishing the listing.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True, slots=True)
class Registration:
    """Validated public metadata for one game-server instance."""

    port: int
    name: str
    map_name: str
    game_mode: str
    max_players: int
    region: str

    def payload(self) -> dict[str, Any]:
        """Return the public registration request body."""

        return {
            "port": self.port,
            # BattleSpades handles A2S through ENet's intercept on the game
            # socket, so its query port is the game port in container mode.
            "query_port": self.port,
            "name": self.name,
            "map": self.map_name,
            "game_mode": self.game_mode,
            "mode_tla": self.game_mode.lower(),
            "max_players": self.max_players,
            "region": self.region,
            "version": "1.0.0.0",
            "tags": ["battlespades", "container"],
        }


def bounded_integer(value: str, minimum: int, maximum: int) -> int:
    """Parse a command-line integer with clear bounds."""

    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise argparse.ArgumentTypeError(
            f"must be between {minimum} and {maximum}"
        )
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the side-effect-free registration argument parser."""

    parser = argparse.ArgumentParser(
        description="Register a live BattleSpades UDP server with AoS Revival",
    )
    parser.add_argument("--port", required=True, type=lambda value: bounded_integer(value, 1, 65535))
    parser.add_argument("--name", required=True)
    parser.add_argument("--map", dest="map_name", default="Unknown")
    parser.add_argument("--mode", dest="game_mode", default="TDM")
    parser.add_argument(
        "--max-players",
        default=24,
        type=lambda value: bounded_integer(value, 1, 255),
    )
    parser.add_argument("--region", default="europe")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AOS_MASTER_URL", "https://aosplay.net"),
    )
    parser.add_argument("--timeout", default=10.0, type=float)
    return parser


def post_registration(
    base_url: str,
    registration: Registration,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    """Submit a registration and return its HTTP status plus JSON payload."""

    endpoint = base_url.rstrip("/") + "/api/master/servers/register"
    request = Request(
        endpoint,
        data=json.dumps(registration.payload(), separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "BattleSpades-Registration/1",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            raw = response.read()
    except HTTPError as exc:
        status = int(exc.code)
        raw = exc.read()
    except (OSError, TimeoutError, URLError) as exc:
        raise RuntimeError(f"registration request failed: {exc}") from exc

    try:
        decoded = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("registry returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("registry returned a non-object response")
    return status, decoded


def main() -> int:
    """Register one port and print the one-time server-scoped credential."""

    arguments = build_parser().parse_args()
    if not 1.0 <= arguments.timeout <= 60.0:
        print("--timeout must be between 1 and 60 seconds", file=sys.stderr)
        return 2
    registration = Registration(
        port=arguments.port,
        name=arguments.name.strip(),
        map_name=arguments.map_name.strip(),
        game_mode=arguments.game_mode.strip(),
        max_players=arguments.max_players,
        region=arguments.region.strip(),
    )
    if not registration.name:
        print("--name cannot be empty", file=sys.stderr)
        return 2

    try:
        status, payload = post_registration(
            arguments.base_url,
            registration,
            arguments.timeout,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if status not in {200, 201} or not payload.get("registered"):
        detail = payload.get("detail") or payload.get("error") or payload
        print(f"registration rejected ({status}): {detail}", file=sys.stderr)
        return 1

    token = str(payload.get("server_token") or "")
    print(f"identifier={payload.get('identifier')}")
    print(f"status={payload.get('status')}")
    print(f"verified={str(bool(payload.get('verified'))).lower()}")
    if payload.get("validation_error"):
        print(f"validation_error={payload['validation_error']}")
    if token:
        print(f"AOS_MASTER_WRITE_TOKEN={token}")
        print("Store this token now; the API will not return it again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
