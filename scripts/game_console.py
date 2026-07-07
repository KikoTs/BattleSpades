"""game_console.py - py3 client for the in-game physics_tracer console.

The tracer (aceofspades_nonsteam/physics_tracer.py) runs a localhost TCP
console inside the live game process (default port 32896). Code sent there
executes ON THE GAME THREAD between frames, with helpers in scope:

    player          - the local player object (or None)
    manager / scene - the GameManager and active scene
    state           - tracer state singleton
    attr_dump(obj)  - dump readable scalar attrs of any object
    keyboard_flags()- live WASD/space/ctrl/shift state
    tag('name')     - tag subsequent capture frames (scenario markers)
    capture(False)  - pause/resume NDJSON capture
    log('msg')      - write to physics_tracer.log

Usage:
    py scripts/game_console.py "repr(player)"           # one-shot eval
    py scripts/game_console.py --file probe.py          # exec a script
    py scripts/game_console.py --repl                   # interactive
    py scripts/game_console.py --wait 60 "1+1"          # wait for game boot
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import time

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 32896


class ConsoleError(RuntimeError):
    pass


class GameConsole:
    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 timeout: float = 15.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._next_id = 1

    def connect(self, wait_seconds: float = 0.0) -> "GameConsole":
        deadline = time.monotonic() + wait_seconds
        last_error: Exception | None = None
        while True:
            try:
                s = socket.create_connection((self.host, self.port), timeout=self.timeout)
                s.settimeout(self.timeout)
                self._sock = s
                return self
            except OSError as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    raise ConsoleError(
                        f"cannot reach game console at {self.host}:{self.port} "
                        f"({exc}). Is the game running with the tracer loaded?"
                    ) from exc
                time.sleep(1.0)

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self):
        if self._sock is None:
            self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def run(self, code: str) -> str:
        """Execute code in the game; return repr() of the result.

        Raises ConsoleError with the in-game traceback on failure.
        """
        if self._sock is None:
            self.connect()
        req_id = self._next_id
        self._next_id += 1
        line = json.dumps({"id": req_id, "code": code}) + "\n"
        assert self._sock is not None
        self._sock.sendall(line.encode("utf-8"))
        response = self._read_response()
        if response.get("id") not in (req_id, None):
            raise ConsoleError(f"response id mismatch: {response}")
        if not response.get("ok"):
            raise ConsoleError(response.get("error", "unknown console error"))
        return response.get("result", "")

    def _read_response(self) -> dict:
        assert self._sock is not None
        while b"\n" not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConsoleError("game console closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("code", nargs="?", help="expression/statements to run in-game")
    ap.add_argument("--file", type=str, help="run a local .py file in-game")
    ap.add_argument("--repl", action="store_true", help="interactive mode")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--wait", type=float, default=0.0,
                    help="seconds to wait for the console to come up")
    args = ap.parse_args()

    console = GameConsole(args.host, args.port)
    try:
        console.connect(wait_seconds=args.wait)
    except ConsoleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        if args.file:
            with open(args.file, "r", encoding="utf-8") as f:
                # py2's compile() rejects coding declarations in unicode
                # strings, so strip them before shipping the code in-game.
                code = "".join(
                    line for line in f
                    if not (line.startswith("#") and "coding" in line[:30])
                )
            print(console.run(code))
            return 0
        if args.code:
            print(console.run(args.code))
            return 0
        if args.repl:
            print(f"connected to game console at {args.host}:{args.port} "
                  "(Ctrl-C or 'exit' to quit)")
            while True:
                try:
                    code = input(">>> ")
                except (EOFError, KeyboardInterrupt):
                    break
                if code.strip() in ("exit", "quit"):
                    break
                if not code.strip():
                    continue
                try:
                    print(console.run(code))
                except ConsoleError as exc:
                    print(f"error: {exc}", file=sys.stderr)
            return 0
        ap.print_help()
        return 2
    except ConsoleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        console.close()


if __name__ == "__main__":
    raise SystemExit(main())
