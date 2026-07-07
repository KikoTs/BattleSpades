"""harness.py — Python 3 driver for the testbot.

Starts the BattleSpades server in the background, runs the py2 testbot as a
subprocess, drains its JSON event stream, asserts on the scenario outcome,
and tears the server down.

Usage:
    py harness.py --scenario connect_only
    py harness.py --scenario full_handshake --keep-logs
    py harness.py --list
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
TESTBOT = ROOT / 'testbot' / 'run.py'
SERVER_ENTRY = ROOT / 'run_server.py'
TMP_DIR = ROOT / 'tmp'
TMP_DIR.mkdir(exist_ok=True)


# --- Color output (Windows ANSI fallback) -------------------------------

def _ansi_enable_windows():
    if sys.platform != 'win32':
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)  # STD_OUTPUT_HANDLE | ENABLE_VT
    except Exception:
        pass

_ansi_enable_windows()
GREEN = '\033[32m'
RED = '\033[31m'
YELLOW = '\033[33m'
DIM = '\033[2m'
RESET = '\033[0m'


# --- Port helpers --------------------------------------------------------

def _udp_port_in_use(port: int, host: str = '127.0.0.1') -> bool:
    """Best-effort UDP port-busy check. UDP can't 'refuse' so we bind+release."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((host, port))
        s.close()
        return False
    except OSError:
        return True


def _read_port_from_config() -> int:
    cfg = ROOT / 'config.toml'
    try:
        for line in cfg.read_text().splitlines():
            line = line.strip()
            if line.startswith('port') and '=' in line:
                # naive: `port = 27015`
                val = line.split('=', 1)[1].split('#', 1)[0].strip()
                return int(val)
    except Exception:
        pass
    return 27015


# --- Server lifecycle ----------------------------------------------------

class ServerProcess:
    """Manages the BattleSpades server as a subprocess."""

    def __init__(self, port: int, log_path: Path, log_level: str = 'INFO'):
        self.port = port
        self.log_path = log_path
        self.log_level = log_level
        self.proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._started_event = threading.Event()
        self._stop_reading = threading.Event()
        self._tail: list[str] = []

    def start(self, ready_timeout: float = 15.0) -> None:
        creationflags = 0
        if sys.platform == 'win32':
            # Allow CTRL_BREAK_EVENT to be sent for graceful shutdown.
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

        env = os.environ.copy()
        env['PYTHONUNBUFFERED'] = '1'

        # Truncate any prior log
        self.log_path.write_text('')

        cmd = [sys.executable, str(SERVER_ENTRY)]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=creationflags,
        )

        # Background reader: tee server stdout to log file + ring buffer.
        def _reader():
            assert self.proc and self.proc.stdout
            with self.log_path.open('ab') as f:
                for line_b in self.proc.stdout:
                    if self._stop_reading.is_set():
                        return
                    f.write(line_b)
                    f.flush()
                    try:
                        line = line_b.decode('utf-8', errors='replace').rstrip()
                    except Exception:
                        line = repr(line_b)
                    self._tail.append(line)
                    if len(self._tail) > 200:
                        self._tail = self._tail[-150:]
                    if 'Server started:' in line or 'A2S/LAN intercept registered' in line:
                        self._started_event.set()

        self._reader_thread = threading.Thread(target=_reader, daemon=True)
        self._reader_thread.start()

        if not self._started_event.wait(ready_timeout):
            self.stop()
            raise RuntimeError(
                f'server did not start within {ready_timeout}s '
                f'(see {self.log_path})')

    def stop(self, grace: float = 4.0) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.poll() is None:
                if sys.platform == 'win32':
                    try:
                        self.proc.send_signal(signal.CTRL_BREAK_EVENT)
                    except Exception:
                        pass
                else:
                    self.proc.send_signal(signal.SIGINT)
                try:
                    self.proc.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
        finally:
            self._stop_reading.set()
            self.proc = None

    def tail(self, n: int = 30) -> list[str]:
        return list(self._tail[-n:])


# --- Bot lifecycle -------------------------------------------------------

@dataclass
class BotResult:
    exit_code: int
    events: list[dict] = field(default_factory=list)
    stderr_lines: list[str] = field(default_factory=list)
    duration_s: float = 0.0
    timed_out: bool = False


def run_bot(scenario: str, port: int, host: str, name: str,
            timeout: float, log_path: Path) -> BotResult:
    """Run the py2 testbot as a subprocess; collect its JSON event stream."""
    py2 = _find_py2()
    if py2 is None:
        raise RuntimeError(
            "couldn't find a 'py2' command — install 32-bit Python 2.7 and put "
            "it on PATH as 'py2', or set the PY2 env var to its full path")

    cmd = [py2, str(TESTBOT),
           '--scenario', scenario,
           '--host', host,
           '--port', str(port),
           '--name', name]

    log_path.write_text('')
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    events: list[dict] = []
    stderr_lines: list[str] = []
    start = time.time()
    timed_out = False

    def _read_stderr():
        assert proc.stderr
        for line_b in proc.stderr:
            line = line_b.decode('utf-8', errors='replace').rstrip()
            stderr_lines.append(line)

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    assert proc.stdout
    with log_path.open('ab') as f:
        try:
            for line_b in proc.stdout:
                f.write(line_b)
                f.flush()
                try:
                    text = line_b.decode('utf-8', errors='replace').rstrip()
                except Exception:
                    text = repr(line_b)
                if not text:
                    continue
                try:
                    rec = json.loads(text)
                    events.append(rec)
                except Exception:
                    events.append({'evt': 'non_json_stdout', 'raw': text})
                if time.time() - start > timeout:
                    timed_out = True
                    proc.kill()
                    break
        except Exception as e:
            stderr_lines.append(f'<harness reader error: {e!r}>')

    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
    stderr_thread.join(timeout=1.0)

    return BotResult(
        exit_code=proc.returncode if not timed_out else 124,
        events=events,
        stderr_lines=stderr_lines,
        duration_s=time.time() - start,
        timed_out=timed_out,
    )


def _find_py2() -> str | None:
    candidates = [os.environ.get('PY2'), 'py2']
    for cand in candidates:
        if not cand:
            continue
        try:
            r = subprocess.run([cand, '-c',
                                'import sys; print("ok" if sys.version_info[0]==2 else "no")'],
                                capture_output=True, text=True, timeout=4)
            if r.returncode == 0 and r.stdout.strip() == 'ok':
                return cand
        except Exception:
            continue
    return None


# --- Build cache --------------------------------------------------------

def _maybe_build() -> None:
    """Rebuild Cython extensions if any .pyx is newer than its .pyd."""
    pyx_files = list(ROOT.glob('shared/*.pyx')) + list(ROOT.glob('aoslib/*.pyx'))
    if not pyx_files:
        return
    needs_build = False
    for pyx in pyx_files:
        # Find any matching .pyd next to it
        pyds = list(pyx.parent.glob(pyx.stem + '*.pyd'))
        if not pyds:
            needs_build = True
            break
        latest_pyd = max(p.stat().st_mtime for p in pyds)
        if pyx.stat().st_mtime > latest_pyd:
            needs_build = True
            break
    if not needs_build:
        return
    print(f'{YELLOW}[harness] rebuilding cython extensions...{RESET}')
    r = subprocess.run([sys.executable, 'setup.py', 'build_ext', '--inplace'],
                       cwd=str(ROOT))
    if r.returncode != 0:
        raise RuntimeError(f'cython build failed (exit {r.returncode})')


# --- Run + assert -------------------------------------------------------

def assert_events(events: list[dict]) -> tuple[bool, list[str]]:
    """Default assertions for any scenario:
      - 'bot_start' must be the first event
      - 'scenario_done result=ok' must be present (or 'scenario_failed' = fail)
      - 'exit code=0' must be present
    """
    failures = []
    if not events or events[0].get('evt') != 'bot_start':
        failures.append('bot_start was not the first event')
    if any(e.get('evt') == 'scenario_failed' for e in events):
        failed = next(e for e in events if e.get('evt') == 'scenario_failed')
        failures.append(f"scenario_failed: kind={failed.get('kind')} error={failed.get('error')}")
        for line in failed.get('traceback', [])[-3:]:
            failures.append('  ' + line)
    if not any(e.get('evt') == 'scenario_done' and e.get('result') == 'ok' for e in events):
        if not any(e.get('evt') == 'scenario_failed' for e in events):
            failures.append('no scenario_done event seen')
    if not any(e.get('evt') == 'exit' for e in events):
        failures.append('no bot exit event')
    return (not failures), failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--scenario')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=None,
                    help='server port; default reads config.toml')
    ap.add_argument('--name', default='TestBot')
    ap.add_argument('--timeout', type=float, default=45.0)
    ap.add_argument('--keep-logs', action='store_true')
    ap.add_argument('--no-build', action='store_true')
    ap.add_argument('--list', action='store_true', help='list scenarios')
    ap.add_argument('--server-log-level', default='INFO')
    ap.add_argument('--ready-timeout', type=float, default=15.0)
    args = ap.parse_args()

    if args.list:
        py2 = _find_py2()
        if py2 is None:
            print('py2 not found; cannot list scenarios')
            return 1
        return subprocess.run([py2, str(TESTBOT), '--list']).returncode

    if not args.scenario:
        ap.error('--scenario is required (use --list)')

    port = args.port if args.port is not None else _read_port_from_config()

    if not args.no_build:
        _maybe_build()

    if _udp_port_in_use(port, args.host):
        print(f'{RED}[harness] port {port} is already in use; refusing to start server{RESET}')
        return 2

    run_id = time.strftime('%Y%m%d-%H%M%S')
    server_log = TMP_DIR / f'server-{run_id}.log'
    bot_log = TMP_DIR / f'bot-{run_id}-{args.scenario}.jsonl'

    print(f'{DIM}[harness] scenario={args.scenario} port={port} '
          f'server-log={server_log.name} bot-log={bot_log.name}{RESET}')

    server = ServerProcess(port=port, log_path=server_log,
                            log_level=args.server_log_level)
    result: BotResult | None = None
    try:
        server.start(ready_timeout=args.ready_timeout)
        print(f'{DIM}[harness] server up. running bot...{RESET}')
        result = run_bot(scenario=args.scenario, port=port, host=args.host,
                         name=args.name, timeout=args.timeout, log_path=bot_log)
    finally:
        server.stop()

    assert result is not None
    ok, failures = assert_events(result.events)
    if result.timed_out:
        ok = False
        failures.append(f'bot wall-clock timeout after {args.timeout}s')
    if result.exit_code != 0 and not result.timed_out:
        # A non-zero exit without a scenario_failed event means the bot
        # crashed before logging structured failure.
        if not any(e.get('evt') == 'scenario_failed' for e in result.events):
            ok = False
            failures.append(f'bot exited {result.exit_code} without scenario_failed event')

    print()
    if ok:
        print(f'{GREEN}PASS{RESET}  {args.scenario}  ({result.duration_s:.1f}s, '
              f'{len(result.events)} events)')
        rc = 0
    else:
        print(f'{RED}FAIL{RESET}  {args.scenario}  ({result.duration_s:.1f}s, '
              f'{len(result.events)} events)')
        for f in failures:
            print(f'  {RED}•{RESET} {f}')
        # Print last few interesting events
        print(f'  {DIM}-- last bot events:{RESET}')
        for e in result.events[-12:]:
            print(f'    {e}')
        if result.stderr_lines:
            print(f'  {DIM}-- bot stderr (last 5):{RESET}')
            for line in result.stderr_lines[-5:]:
                print(f'    {line}')
        print(f'  {DIM}-- last server log:{RESET}')
        for line in server.tail(15):
            print(f'    {line}')
        rc = 1

    if not args.keep_logs and rc == 0:
        try:
            server_log.unlink()
        except Exception:
            pass
        try:
            bot_log.unlink()
        except Exception:
            pass
    else:
        print(f'{DIM}  logs: {server_log} {bot_log}{RESET}')

    return rc


if __name__ == '__main__':
    sys.exit(main())
