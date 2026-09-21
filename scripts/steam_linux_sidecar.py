"""Advertise an existing BattleSpades server through Valve's Linux runtime.

Run in a separate, unprivileged x86-64 Linux process. The operator supplies
steamclient.so from Valve's SteamCMD package; no Valve binaries are bundled.
The versioned ABI is ISteamClient017 / SteamGameServer012 (SDK 1.37).
The public game port must already route to the upstream game server.
"""

from __future__ import annotations

import argparse
import ctypes as c
import json
import os
from pathlib import Path
import platform
import signal
import socket
import struct
import sys
import time
from typing import Any

if __package__:
    from .check_steam_registration import ProbeError, query_a2s
else:
    from check_steam_registration import ProbeError, query_a2s


class Callback(c.Structure):
    """Valve CallbackMsg_t, with the Linux x86-64 natural alignment."""

    _fields_ = [
        ("user", c.c_int), ("callback_id", c.c_int),
        ("data", c.c_void_p), ("size", c.c_int),
    ]


def vcall(obj: int, index: int, result: Any, types: list[Any], *args: Any) -> Any:
    """Call one method of a verified, versioned Linux C++ interface."""
    table = c.cast(obj, c.POINTER(c.POINTER(c.c_void_p))).contents
    return c.CFUNCTYPE(result, c.c_void_p, *types)(table[index])(obj, *args)


def advertisement(info: dict[str, Any], mode: str, region: str) -> dict[str, Any]:
    """Convert live A2S metadata into the retail browser's fields."""
    if info["folder"].casefold() != "aceofspades":
        raise ProbeError("upstream is not an Ace of Spades server")
    if not 0 <= info["bots"] <= info["players"] <= info["max_players"] <= 255:
        raise ProbeError("upstream population is invalid")
    # Old BattleSpades releases put the gameplay ID in this category field.
    # Rebuild it explicitly rather than copying the incompatible old tags.
    tags = ["v168", "playlist=8"]
    if region:
        tags.append(f"region={region}")
    tags.append("mode=0001")
    for tag in str(info.get("tags", "")).split(";"):
        if tag == "classic" or tag.startswith("skin="):
            tags.append(tag)
    encoded_tags = ";".join(tags)
    if len(encoded_tags.encode()) >= 128:
        raise ProbeError("retail tags exceed the Steam limit")
    map_name = str(info["map"])
    prefix = mode.upper() + "_"
    if not map_name.upper().startswith(prefix):
        map_name = prefix + map_name
    words = map_name.split(" ")
    map_name = words[0] + "".join(word[:1].upper() + word[1:] for word in words[1:])
    return {
        "name": info["name"], "map": map_name[:79],
        "players": info["players"], "bots": info["bots"],
        "max_players": info["max_players"], "password": info["password"],
        "tags": encoded_tags,
    }


def query_players(host: str, port_number: int) -> list[tuple[str, int]]:
    """Read names/scores through the upstream's public A2S_PLAYER interface."""
    prefix = b"\xff\xff\xff\xff"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(1)
        sock.connect((host, port_number))
        sock.send(prefix + b"U" + prefix)
        packet = sock.recv(65535)
        if packet[:5] == prefix + b"A" and len(packet) >= 9:
            sock.send(prefix + b"U" + packet[5:9])
            packet = sock.recv(65535)
    if len(packet) < 6 or packet[:5] != prefix + b"D":
        raise ProbeError("upstream returned no unsplit A2S_PLAYER response")
    rows = []
    offset = 6
    for _ in range(packet[5]):
        offset += 1  # wire row index
        end = packet.find(b"\0", offset)
        if end < 0 or end + 9 > len(packet):
            raise ProbeError("truncated A2S_PLAYER response")
        name = packet[offset:end].decode("utf-8", "replace")
        score = struct.unpack_from("<i", packet, end + 1)[0]
        rows.append((name, max(0, score)))
        offset = end + 9
    return rows


class SteamServer:
    """Own the Steam connection and its public query socket."""

    def __init__(self, runtime: Path, game_port: int, query_port: int) -> None:
        if sys.platform != "linux" or platform.machine() != "x86_64":
            raise RuntimeError("This helper requires Linux x86-64")
        os.environ["SteamAppId"] = os.environ["SteamGameId"] = "224540"
        self.library = c.CDLL(str(runtime.resolve() / "steamclient.so"), mode=c.RTLD_GLOBAL)
        self.library.CreateInterface.argtypes = [c.c_char_p, c.POINTER(c.c_int)]
        self.library.CreateInterface.restype = c.c_void_p
        self.client = self.library.CreateInterface(b"SteamClient017", None)
        if not self.client:
            raise RuntimeError("SteamClient017 unavailable")
        self.pipe = c.c_int()
        self.user = vcall(self.client, 3, c.c_int, [c.POINTER(c.c_int), c.c_int],
                          c.byref(self.pipe), 3)
        self.server = vcall(self.client, 6, c.c_void_p,
                            [c.c_int, c.c_int, c.c_char_p],
                            self.user, self.pipe.value, b"SteamGameServer012")
        if not self.user or not self.pipe.value or not self.server:
            raise RuntimeError("SteamGameServer012 unavailable")
        initialized = vcall(
            self.server, 0, c.c_bool,
            [c.c_uint32, c.c_uint16, c.c_uint16, c.c_uint32, c.c_uint32, c.c_char_p],
            0, game_port, query_port, 4 | 8, 224540, b"1.0.0.0",
        )
        if not initialized:
            raise RuntimeError("InitGameServer failed; check query port availability")
        self.library.Steam_BGetCallback.argtypes = [c.c_int, c.POINTER(Callback)]
        self.library.Steam_BGetCallback.restype = c.c_bool
        self.library.Steam_FreeLastCallback.argtypes = [c.c_int]
        self.library.Steam_FreeLastCallback.restype = None
        for index, text in [(1, "aos"), (2, "Ace of Spades"), (3, "aceofspades")]:
            self.text(index, text)
        vcall(self.server, 4, None, [c.c_bool], True)
        self.users: list[int] = []

    def text(self, index: int, value: str) -> None:
        """Set a UTF-8 string field."""
        vcall(self.server, index, None, [c.c_char_p], value.encode("utf-8"))

    def update(self, data: dict[str, Any], rows: list[tuple[str, int]]) -> None:
        """Mirror actual population; this legacy path has no ticket integration."""
        self.text(14, data["name"])
        self.text(15, data["map"])
        self.text(21, data["tags"])
        vcall(self.server, 12, None, [c.c_int], data["max_players"])
        # SetBotPlayerCount alone does not update total occupancy in SDK 1.37.
        # Mirror only real upstream slots using the unauthenticated-user API.
        # These are not authenticated Steam sessions; there is no VAC claim.
        while len(self.users) > data["players"]:
            vcall(self.server, 26, None, [c.c_uint64], self.users.pop())
        while len(self.users) < data["players"]:
            user = vcall(self.server, 25, c.c_uint64, [])
            if not user:
                raise RuntimeError("Steam failed to create an occupancy slot")
            self.users.append(user)
        for index, user in enumerate(self.users):
            name, score = rows[index] if index < len(rows) else ("", 0)
            vcall(self.server, 27, c.c_bool,
                  [c.c_uint64, c.c_char_p, c.c_uint32], user, name.encode("utf-8"), score)
        vcall(self.server, 13, None, [c.c_int], data["bots"])
        vcall(self.server, 16, None, [c.c_bool], data["password"])

    def start(self, region: str) -> None:
        """Log on anonymously and enable normal master-server heartbeats."""
        self.text(23, region)
        vcall(self.server, 39, None, [c.c_bool], True)
        vcall(self.server, 6, None, [])
        vcall(self.server, 41, None, [])

    def poll(self) -> tuple[bool, int, int]:
        """Drain callbacks and return login state, server SteamID and public IP."""
        callback = Callback()
        while self.library.Steam_BGetCallback(self.pipe.value, c.byref(callback)):
            if callback.callback_id in (101, 102, 103, 115):
                print(json.dumps({"callback": callback.callback_id}), flush=True)
            self.library.Steam_FreeLastCallback(self.pipe.value)
        return (
            vcall(self.server, 8, c.c_bool, []),
            vcall(self.server, 10, c.c_uint64, []),
            vcall(self.server, 36, c.c_uint32, []),
        )

    def close(self) -> None:
        """Withdraw the advertisement and release the isolated Steam pipe."""
        vcall(self.server, 39, None, [c.c_bool], False)
        vcall(self.server, 7, None, [])
        vcall(self.client, 4, None, [c.c_int, c.c_int], self.pipe.value, self.user)
        vcall(self.client, 1, c.c_bool, [c.c_int], self.pipe.value)


def port(value: str) -> int:
    """Validate a non-privileged UDP port."""
    result = int(value)
    if not 1024 <= result <= 65534:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65534")
    return result


def main() -> int:
    """Keep a live advertisement, withdrawing it when its upstream fails."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--source-host", default="127.0.0.1")
    parser.add_argument("--source-port", type=port, required=True)
    parser.add_argument("--game-port", type=port, default=32887)
    parser.add_argument("--query-port", type=port, default=32888)
    parser.add_argument("--mode", choices=["tdm", "ctf", "zom", "vip", "tc", "dia"], required=True)
    parser.add_argument("--region", default="europe", choices=["europe", "america", "asia", "oceania", ""])
    args = parser.parse_args()
    if args.game_port == args.query_port:
        parser.error("game and query ports must differ")
    running = True

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    upstream = advertisement(query_a2s(args.source_host, args.source_port, 1), args.mode, args.region)
    steam = SteamServer(args.runtime, args.game_port, args.query_port)
    try:
        rows = query_players(args.source_host, args.source_port)
        steam.update(upstream, rows)
        steam.start(args.region)
        print(json.dumps({"advertisement": upstream}), flush=True)
        next_update = time.monotonic() + 5
        last_logged_on = time.monotonic()
        state = None
        failures = 0
        while running:
            current = steam.poll()
            if current != state:
                print(json.dumps({"logged_on": current[0], "steam_id": current[1],
                                  "public_ip_integer": current[2]}), flush=True)
                state = current
            now = time.monotonic()
            if current[0]:
                last_logged_on = now
            elif now - last_logged_on > 120:
                raise RuntimeError("Steam disconnected for 120 seconds")
            if now >= next_update:
                next_update = now + 5
                try:
                    update = advertisement(query_a2s(args.source_host, args.source_port, 1), args.mode, args.region)
                except (OSError, ValueError, ProbeError) as error:
                    failures += 1
                    print(json.dumps({"upstream_error": str(error), "failures": failures}), flush=True)
                    if failures >= 3:
                        raise RuntimeError("Withdrawing listing: upstream failed three probes") from error
                else:
                    failures = 0
                    try:
                        rows = query_players(args.source_host, args.source_port)
                    except (OSError, ProbeError) as error:
                        print(json.dumps({"player_query_error": str(error)}), flush=True)
                        rows = []
                    steam.update(update, rows)
                    if update != upstream:
                        print(json.dumps({"advertisement": update}), flush=True)
                        upstream = update
            time.sleep(0.1)
    finally:
        steam.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
