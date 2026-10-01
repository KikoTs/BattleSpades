"""Inbound firewall rule for the server, per operating system.

Windows: one elevated ``netsh advfirewall`` call through the normal UAC prompt.
Linux/macOS: the window shows the short command to run, because adding a
rule there needs a password prompt in a terminal.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

RULE_PREFIX = "BattleSpades Server"


def rule_name(ports: Iterable[int]) -> str:
    return f"{RULE_PREFIX} (UDP {','.join(str(p) for p in sorted(set(ports)))})"


def netsh_add_arguments(program: Path, ports: Iterable[int]) -> list[str]:
    ports = sorted({int(p) for p in ports})
    return [
        "advfirewall", "firewall", "add", "rule",
        f"name={rule_name(ports)}",
        "dir=in", "action=allow", "protocol=UDP",
        f"localport={','.join(map(str, ports))}",
        f"program={program}",
        "profile=any",
        "enable=yes",
    ]


def _quote_windows(argument: str) -> str:
    if "=" in argument:
        key, value = argument.split("=", 1)
        if " " in value or "(" in value:
            return f'{key}="{value}"'
        return argument
    return f'"{argument}"' if " " in argument else argument


def windows_command_line(program: Path, ports: Iterable[int]) -> str:
    return "netsh " + " ".join(_quote_windows(a) for a in netsh_add_arguments(program, ports))


@dataclass
class FirewallResult:
    ok: bool
    message: str


def add_windows_rule(program: Path, ports: Iterable[int], timeout_ms: int = 60000) -> FirewallResult:
    """Run netsh elevated (UAC prompt) and wait for its exit code."""

    if sys.platform != "win32":
        return FirewallResult(False, "Only available on Windows.")
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
            ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE),
        ]

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    parameters = " ".join(_quote_windows(a) for a in netsh_add_arguments(program, ports))
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "netsh.exe"
    info.lpParameters = parameters
    info.nShow = 0
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == 1223:
            return FirewallResult(False, "Cancelled at the Windows permission prompt.")
        return FirewallResult(False, f"Could not start netsh (error {error}).")
    try:
        kernel32.WaitForSingleObject(info.hProcess, timeout_ms)
        code = wintypes.DWORD()
        kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    finally:
        kernel32.CloseHandle(info.hProcess)
    if code.value == 0:
        return FirewallResult(True, f"Windows Firewall now allows inbound UDP {', '.join(map(str, sorted(set(ports))))} for {program.name}.")
    return FirewallResult(False, f"netsh failed (exit code {code.value}).")


def windows_rule_exists(ports: Iterable[int]) -> bool | None:
    """True/False if netsh can tell, None when unknown."""

    if sys.platform != "win32":
        return None
    try:
        completed = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={rule_name(ports)}"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.returncode == 0 and rule_name(ports) in completed.stdout


def instructions(platform: str, program: Path, ports: Iterable[int]) -> str:
    """Short, copyable steps for the current operating system."""

    ports = sorted({int(p) for p in ports})
    joined = ",".join(map(str, ports))
    if platform == "win32":
        return ("Press 'Add firewall rule' and accept the Windows prompt, or run this in an "
                "administrator terminal:\n" + windows_command_line(program, ports))
    if platform == "darwin":
        return ("macOS asks 'Accept incoming network connections?' the first time the server starts: "
                "choose Allow. If the firewall is on and you missed it, run:\n"
                f"sudo /usr/libexec/ApplicationFirewall/socketfilterfw --add \"{program}\"\n"
                f"sudo /usr/libexec/ApplicationFirewall/socketfilterfw --unblockapp \"{program}\"")
    lines = ["Most desktop Linux systems have no firewall enabled. If yours does, run one of:"]
    lines += [f"sudo ufw allow {p}/udp" for p in ports]
    lines.append("# or, with firewalld:")
    lines += [f"sudo firewall-cmd --permanent --add-port={p}/udp" for p in ports]
    lines.append("sudo firewall-cmd --reload")
    if len(ports) > 1:
        lines.append(f"# ports: {joined}")
    return "\n".join(lines)
