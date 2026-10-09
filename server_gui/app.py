"""BattleSpades Server: the desktop window (CustomTkinter on Tk).

All decisions live in the toolkit-free modules next to this one; this file
only lays out widgets and moves results between worker threads and Tk.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

import customtkinter as ctk

from server_gui import catalog, console, firewall, host_settings, logparse, network, paths as host_paths, theme
from server_gui.config_doc import ConfigDocument, KeySpec, RESTART_NOTE, coerce, format_toml_value, load_defaults
from server_gui.gui_state import GuiState, SingleInstance
from server_gui.process import ServerProcess, build_command, format_uptime, read_status

APP_TITLE = "BattleSpades Server"
PORTFORWARD_GUIDE = "https://portforward.com/router.htm"
SITE = "https://www.aosplay.net"
MAX_LOG_LINES = 6000
TAB_NAMES = ("Host", "Network", "Console", "Advanced")


# --------------------------------------------------------------------------
# Small helpers


def heading_font(size: int = 20) -> ctk.CTkFont:
    return ctk.CTkFont(family=theme.HEADING_FAMILY if _HAS_HEADING else theme.body_family(), size=size, weight="bold")


def body_font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=theme.body_family(), size=size, weight=weight)


def mono_font(size: int = 12) -> ctk.CTkFont:
    return ctk.CTkFont(family=theme.mono_family(), size=size)


_HAS_HEADING = False


def open_path(path: Path) -> None:
    """Show a folder in the system file manager (created if missing)."""

    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 - opening a folder the user asked for
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Card(ctk.CTkFrame):
    """A titled panel."""

    def __init__(self, master, title: str, subtitle: str = "", **kwargs) -> None:
        super().__init__(master, fg_color=theme.PANEL, corner_radius=10, border_width=1,
                         border_color=theme.BORDER, **kwargs)
        self.grid_columnconfigure(0, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(12, 4))
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkFrame(header, width=4, height=20, fg_color=theme.OLIVE_BRIGHT, corner_radius=2).grid(row=0, column=0, padx=(0, 8))
        ctk.CTkLabel(header, text=title.upper(), font=heading_font(19), text_color=theme.KHAKI, anchor="w").grid(row=0, column=1, sticky="w")
        self.header = header
        if subtitle:
            ctk.CTkLabel(self, text=subtitle, font=body_font(12), text_color=theme.MUTED, anchor="w",
                         justify="left", wraplength=560).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 6))
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.grid(row=2, column=0, sticky="nsew", padx=16, pady=(4, 14))
        self.body.grid_columnconfigure(1, weight=1)


def field_label(master, text: str, row: int, hint: str = "") -> None:
    ctk.CTkLabel(master, text=text, font=body_font(13, "bold"), text_color=theme.TEXT, anchor="w").grid(
        row=row, column=0, sticky="nw", pady=(8, 0), padx=(0, 14))
    if hint:
        ctk.CTkLabel(master, text=hint, font=body_font(11), text_color=theme.FAINT, anchor="w").grid(
            row=row + 1, column=0, sticky="nw", padx=(0, 14))


def accent_button(master, text: str, command: Callable, **kwargs) -> ctk.CTkButton:
    options = dict(fg_color=theme.OLIVE, hover_color=theme.OLIVE_HOVER, text_color="#f4f1e4",
                   font=body_font(13, "bold"), corner_radius=8, height=34)
    options.update(kwargs)
    return ctk.CTkButton(master, text=text, command=command, **options)


def quiet_button(master, text: str, command: Callable, **kwargs) -> ctk.CTkButton:
    options = dict(fg_color=theme.PANEL_ALT, hover_color=theme.BORDER, text_color=theme.TEXT,
                   font=body_font(12), corner_radius=8, height=30, border_width=1, border_color=theme.BORDER)
    options.update(kwargs)
    return ctk.CTkButton(master, text=text, command=command, **options)


def entry(master, variable=None, **kwargs) -> ctk.CTkEntry:
    options = dict(fg_color=theme.PANEL_ALT, border_color=theme.BORDER, text_color=theme.TEXT,
                   font=body_font(13), corner_radius=6, height=32)
    options.update(kwargs)
    return ctk.CTkEntry(master, textvariable=variable, **options)


def option_menu(master, values, variable=None, command=None, **kwargs) -> ctk.CTkOptionMenu:
    options = dict(fg_color=theme.PANEL_ALT, button_color=theme.BORDER, button_hover_color=theme.OLIVE,
                   text_color=theme.TEXT, dropdown_fg_color=theme.PANEL_ALT, dropdown_hover_color=theme.OLIVE,
                   dropdown_text_color=theme.TEXT, font=body_font(13), dropdown_font=body_font(13),
                   corner_radius=6, height=32, dynamic_resizing=False)
    options.update(kwargs)
    return ctk.CTkOptionMenu(master, values=list(values) or [""], variable=variable, command=command, **options)


def segmented(master, values, variable=None, command=None, **kwargs) -> ctk.CTkSegmentedButton:
    options = dict(fg_color=theme.PANEL_ALT, selected_color=theme.OLIVE, selected_hover_color=theme.OLIVE_HOVER,
                   unselected_color=theme.PANEL_ALT, unselected_hover_color=theme.BORDER, text_color=theme.TEXT,
                   font=body_font(12), corner_radius=6, height=30)
    options.update(kwargs)
    return ctk.CTkSegmentedButton(master, values=list(values), variable=variable, command=command, **options)


def switch(master, text: str, variable, command=None, **kwargs) -> ctk.CTkSwitch:
    options = dict(progress_color=theme.OLIVE_BRIGHT, button_color=theme.KHAKI, button_hover_color="#ffffff",
                   fg_color=theme.BORDER, text_color=theme.TEXT, font=body_font(13))
    options.update(kwargs)
    return ctk.CTkSwitch(master, text=text, variable=variable, command=command, **options)


def note(master, text: str = "", colour: str = theme.MUTED, size: int = 12, wrap: int = 520) -> ctk.CTkLabel:
    return ctk.CTkLabel(master, text=text, font=body_font(size), text_color=colour, anchor="w",
                        justify="left", wraplength=wrap)


def draw_logo(canvas: tk.Canvas, x: int, y: int, s: int) -> None:
    """An isometric olive block with a spade notch: our own mark."""

    top = [(x, y + s * 0.5), (x + s, y), (x + 2 * s, y + s * 0.5), (x + s, y + s)]
    left = [(x, y + s * 0.5), (x + s, y + s), (x + s, y + 2.1 * s), (x, y + 1.6 * s)]
    right = [(x + s, y + s), (x + 2 * s, y + s * 0.5), (x + 2 * s, y + 1.6 * s), (x + s, y + 2.1 * s)]
    canvas.create_polygon(top, fill="#a7bd62", outline="")
    canvas.create_polygon(left, fill="#5f7a2e", outline="")
    canvas.create_polygon(right, fill="#3f521e", outline="")
    # spade blade cut into the right face
    cx, cy = x + 1.5 * s, y + 1.15 * s
    canvas.create_polygon([(cx - 0.22 * s, cy - 0.1 * s), (cx + 0.22 * s, cy - 0.32 * s),
                           (cx + 0.22 * s, cy + 0.18 * s), (cx, cy + 0.42 * s), (cx - 0.22 * s, cy + 0.38 * s)],
                          fill="#e4dcc0", outline="")
    canvas.create_line(cx, cy - 0.21 * s, cx + 0.0, cy - 0.62 * s, fill="#e4dcc0", width=max(2, int(s * 0.09)))


# --------------------------------------------------------------------------
# The window


class App(ctk.CTk):
    def __init__(self, paths: host_paths.HostPaths, state: GuiState, lock: SingleInstance,
                 *, offline: bool = False, master_url: str | None = None) -> None:
        super().__init__(fg_color=theme.BG)
        self.offline = offline
        self.master_url = master_url
        self.paths = paths
        self.state_data = state
        self.lock = lock
        self.calls: queue.Queue[Callable[[], None]] = queue.Queue()
        self.server: ServerProcess | None = None
        self.server_state = "stopped"
        self.status: dict | None = None
        self.events = logparse.SessionEvents()
        self.forwarder = network.PortForwarder()
        self.forward_result: network.ForwardResult | None = None
        self.public_ip: str | None = None
        self.lan_ip: str | None = network.local_ip()
        self.master_latency: str = "checking..."
        self.doc_version = 0
        self.restart_needed = False
        self.closing = False
        self.version = _read_version(paths.root)

        self.title(APP_TITLE)
        self.minsize(980, 640)
        self._set_icon()
        self.doc = self._load_config()
        self.defaults = load_defaults(paths.bundled_defaults())
        if self.defaults is None and self.doc is not None and not paths.defaults_snapshot.exists():
            try:
                paths.defaults_snapshot.write_text(self.doc.original_text, encoding="utf-8")
                self.defaults = ConfigDocument(self.doc.original_text, paths.defaults_snapshot)
            except OSError:
                pass
        self._apply_first_run_defaults()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self._build_header()
        self._build_tabbar()
        self.pages: dict[str, ctk.CTkFrame] = {}
        container = ctk.CTkFrame(self, fg_color="transparent")
        container.grid(row=2, column=0, sticky="nsew", padx=18, pady=(6, 0))
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)
        self.host_page = HostPage(container, self)
        self.network_page = NetworkPage(container, self)
        self.console_page = ConsolePage(container, self)
        self.advanced_page = AdvancedPage(container, self)
        for name, page in zip(TAB_NAMES, (self.host_page, self.network_page, self.console_page, self.advanced_page)):
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[name] = page
        self._build_footer()

        self._restore_geometry()
        self.show_tab(state.tab if state.tab in TAB_NAMES else "Host")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(50, self._drain_calls)
        self.after(1000, self._poll_status)
        self.background(self._refresh_addresses)
        self.background(self._ping_master_loop)
        self.background(_warm_up_validator)
        self.refresh_dirty()

    # ---- infrastructure ---------------------------------------------------
    def call_soon(self, function: Callable[[], None]) -> None:
        """Run ``function`` on the Tk thread (safe from any thread)."""

        self.calls.put(function)

    def background(self, function: Callable[[], Any], done: Callable[[Any], None] | None = None) -> None:
        def run() -> None:
            try:
                result = function()
            except Exception as exc:  # surfaced to the caller's callback
                result = exc
            if done is not None:
                self.call_soon(lambda: done(result))

        threading.Thread(target=run, daemon=True).start()

    def _drain_calls(self) -> None:
        for _ in range(400):
            try:
                function = self.calls.get_nowait()
            except queue.Empty:
                break
            try:
                function()
            except Exception as exc:  # keep the window alive
                self.console_page.append_local(f"Window error: {exc!r}", "ERROR")
        if not self.closing or self.server is not None:
            self.after(50, self._drain_calls)

    def _set_icon(self) -> None:
        assets = Path(__file__).resolve().parent / "assets"
        try:
            if sys.platform == "win32" and (assets / "battlespades-server.ico").is_file():
                self.iconbitmap(default=str(assets / "battlespades-server.ico"))
            icon = tk.PhotoImage(file=str(assets / "battlespades-server.png"))
            self.iconphoto(True, icon)
            self._icon = icon
        except tk.TclError:
            pass

    def _load_config(self) -> ConfigDocument | None:
        try:
            return ConfigDocument.load(self.paths.config)
        except FileNotFoundError:
            messagebox.showerror(APP_TITLE, f"config.toml was not found in\n{self.paths.root}\n\n"
                                            "Extract the complete release zip and start the server window from that folder.")
            return None
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"config.toml could not be read:\n{exc}\n\n"
                                            "Fix the file in a text editor, then start again.")
            return None

    def _apply_first_run_defaults(self) -> None:
        """The GUI hosts on the retail game port so the stock browser connects.

        It also never leaves the shipped "changeme" admin password in place:
        a fresh random one is filled in (saved with the other first-run
        changes), so in-game /admin works without sharing a known secret.
        """

        if self.doc is None:
            return
        self._generated_admin_password = host_settings.ensure_admin_password(self.doc)
        if self.state_data.port_default_applied:
            return
        if int(self.doc.get("server", "port", host_settings.SHIPPED_SAMPLE_PORT)) == host_settings.SHIPPED_SAMPLE_PORT:
            self.doc.set("server", "port", host_settings.RETAIL_GAME_PORT)
            self._first_run_port_note = True
        self.state_data.port_default_applied = True

    # ---- layout -----------------------------------------------------------
    def _build_header(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=theme.PANEL, corner_radius=0, height=74)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_columnconfigure(1, weight=1)
        canvas = tk.Canvas(bar, width=64, height=64, bg=theme.PANEL, highlightthickness=0)
        canvas.grid(row=0, column=0, rowspan=2, padx=(18, 8), pady=6)
        draw_logo(canvas, 6, 4, 26)
        ctk.CTkLabel(bar, text="BATTLESPADES", font=heading_font(30), text_color=theme.KHAKI, anchor="w").grid(
            row=0, column=1, sticky="sw", pady=(10, 0))
        ctk.CTkLabel(bar, text=f"DEDICATED SERVER  ·  v{self.version}", font=heading_font(14),
                     text_color=theme.OLIVE_BRIGHT, anchor="w").grid(row=1, column=1, sticky="nw", pady=(0, 8))
        self.header_state = ctk.CTkLabel(bar, text="●  STOPPED", font=heading_font(20), text_color=theme.MUTED)
        self.header_state.grid(row=0, column=2, rowspan=2, padx=22)
        ctk.CTkFrame(self, height=2, fg_color=theme.OLIVE, corner_radius=0).grid(row=0, column=0, sticky="sew")

    def _build_tabbar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=18, pady=(12, 0))
        self.tab_buttons: dict[str, ctk.CTkButton] = {}
        for index, name in enumerate(TAB_NAMES):
            button = ctk.CTkButton(bar, text=name.upper(), font=heading_font(17), width=130, height=36, corner_radius=8,
                                   fg_color=theme.PANEL, hover_color=theme.PANEL_ALT, text_color=theme.MUTED,
                                   command=lambda n=name: self.show_tab(n))
            button.grid(row=0, column=index, padx=(0, 6))
            self.tab_buttons[name] = button
        self.dirty_label = ctk.CTkLabel(bar, text="", font=body_font(12), text_color=theme.WARN)
        bar.grid_columnconfigure(10, weight=1)
        self.dirty_label.grid(row=0, column=11, padx=(10, 8))
        self.save_button = accent_button(bar, "Save settings", self.save_settings, width=130)
        self.save_button.grid(row=0, column=12)

    def _build_footer(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=theme.PANEL, corner_radius=0, height=40)
        bar.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        bar.grid_columnconfigure(0, weight=1)
        self.footer_message = ctk.CTkLabel(bar, text="", font=body_font(12), text_color=theme.MUTED, anchor="w")
        self.footer_message.grid(row=0, column=0, sticky="ew", padx=16, pady=6)
        items = (
            ("Server folder", lambda: open_path(self.paths.root)),
            ("Logs", lambda: open_path(self.paths.logs)),
            ("Check for updates", self.check_updates),
            ("aosplay.net", lambda: webbrowser.open(SITE)),
        )
        for index, (label, command) in enumerate(items, start=1):
            quiet_button(bar, label, command, height=26, font=body_font(12)).grid(row=0, column=index, padx=(0, 8), pady=6)

    def show_tab(self, name: str) -> None:
        page = self.pages[name]
        # A CTkScrollableFrame is gridded through its outer frame.
        getattr(page, "_parent_frame", page).tkraise()
        for tab, button in self.tab_buttons.items():
            selected = tab == name
            button.configure(fg_color=theme.OLIVE if selected else theme.PANEL,
                             text_color="#f4f1e4" if selected else theme.MUTED,
                             hover_color=theme.OLIVE_HOVER if selected else theme.PANEL_ALT)
        self.state_data.tab = name
        if hasattr(page, "on_show"):
            page.on_show()

    def footer(self, text: str, colour: str = theme.MUTED) -> None:
        self.footer_message.configure(text=text, text_color=colour)

    # ---- geometry ---------------------------------------------------------
    def _restore_geometry(self) -> None:
        geometry = self.state_data.geometry
        if geometry:
            try:
                self.geometry(geometry)
            except tk.TclError:
                self.geometry("1180x780")
        else:
            self.geometry("1180x780")
        if self.state_data.zoomed and sys.platform == "win32":
            self.after(100, lambda: self.state("zoomed"))

    def _remember_geometry(self) -> None:
        try:
            zoomed = self.state() == "zoomed"
            self.state_data.zoomed = zoomed
            if not zoomed:
                self.state_data.geometry = self.geometry()
        except tk.TclError:
            pass

    # ---- config document --------------------------------------------------
    def doc_changed(self, source: str = "") -> None:
        self.doc_version += 1
        self.refresh_dirty()
        if source != "advanced":
            self.advanced_page.invalidate()
        if source != "network" and self.state_data.tab == "Network":
            self.network_page.refresh_from_doc()

    def refresh_dirty(self) -> None:
        dirty = self.doc is not None and self.doc.dirty
        if dirty:
            self.dirty_label.configure(text="● Unsaved changes", text_color=theme.WARN)
            self.save_button.configure(state="normal", fg_color=theme.OLIVE)
        else:
            self.dirty_label.configure(text="All changes saved", text_color=theme.FAINT)
            self.save_button.configure(state="disabled", fg_color=theme.PANEL_ALT, text_color_disabled=theme.FAINT)

    def save_settings(self, after: Callable[[bool], None] | None = None) -> None:
        if self.doc is None:
            return
        self.host_page.push_to_doc()
        problems = host_settings.problems(host_settings.read(self.doc))
        if problems:
            messagebox.showwarning(APP_TITLE, "\n".join(problems))
            if after:
                after(False)
            return
        self.footer("Checking settings...")
        self.save_button.configure(state="disabled")

        def work():
            return self.doc.save(maps_dir=self.maps_dir())

        def done(result) -> None:
            if isinstance(result, Exception):
                messagebox.showerror(APP_TITLE, f"Could not save config.toml:\n{result}")
                self.refresh_dirty()
                if after:
                    after(False)
                return
            if not result.ok:
                messagebox.showerror(APP_TITLE, "These settings were not saved, because the server would refuse them:\n\n"
                                                f"{result.error}")
                self.footer("Settings not saved: " + result.error, theme.DANGER)
                self.refresh_dirty()
                if after:
                    after(False)
                return
            self.state_data.save(self.paths.gui_state)
            message = "Settings saved to config.toml."
            if self.server_state in ("running", "starting"):
                self.restart_needed = True
                message += " " + RESTART_NOTE
            if result.warnings:
                message += "  Note: " + result.warnings[0]
            self.footer(message, theme.OK if not result.warnings else theme.WARN)
            for warning in result.warnings:
                self.console_page.append_local("Config note: " + warning, "WARNING")
            self.refresh_dirty()
            self.host_page.refresh_status()
            self.advanced_page.invalidate()
            if after:
                after(True)

        self.background(work, done)

    def revert_settings(self) -> None:
        if self.doc is None or not self.doc.dirty:
            return
        if not messagebox.askyesno(APP_TITLE, "Discard all unsaved changes and reload config.toml?"):
            return
        self.doc = ConfigDocument.load(self.paths.config)
        self.host_page.load_from_doc()
        self.doc_changed()
        self.footer("Reloaded config.toml from disk.")

    # ---- server lifecycle -------------------------------------------------
    def steam_p2p_readiness(self) -> host_settings.Readiness:
        helper = host_paths.steam_relay_helper(self.paths.root)
        return host_settings.steam_p2p_readiness(self.paths.root, self.doc, helper=helper,
                                                 steam_running=host_settings.steam_running())

    def start_server(self) -> None:
        if self.doc is None or self.server_state != "stopped":
            return
        self.set_server_state("starting")

        def after_save(ok: bool) -> None:
            if not ok:
                self.set_server_state("stopped")
                return
            self._launch()

        if self.doc.dirty or not self.paths.config.exists():
            self.save_settings(after_save)
        else:
            self.host_page.push_to_doc()
            if self.doc.dirty:
                self.save_settings(after_save)
            else:
                self._launch()

    def _launch(self) -> None:
        readiness = self.steam_p2p_readiness()
        use_p2p = bool(self.state_data.steam_p2p and readiness.available and not getattr(self, "offline", False))
        try:
            self.paths.status_file.unlink()
        except OSError:
            pass
        custom_config = None
        if self.paths.config.resolve() != (self.paths.root / "config.toml").resolve():
            custom_config = self.paths.config
        command = build_command(self.paths.server_command(), steam_p2p=use_p2p, status_file=self.paths.status_file,
                                config_path=custom_config, offline=getattr(self, "offline", False),
                                master_url=getattr(self, "master_url", None))
        self.events = logparse.SessionEvents()
        self.status = None
        self.restart_needed = False
        self.server = ServerProcess(
            command=command, cwd=self.paths.root,
            on_line=lambda line: self.call_soon(lambda l=line: self._on_server_line(l)),
            on_exit=lambda code: self.call_soon(lambda c=code: self._on_server_exit(c)),
        )
        self.console_page.append_local("Starting: " + " ".join(f'"{c}"' if " " in c else c for c in command), "COMMAND")
        try:
            self.server.start()
        except OSError as exc:
            self.server = None
            self.set_server_state("stopped")
            messagebox.showerror(APP_TITLE, f"The server could not be started:\n{exc}")
            return
        if self.state_data.first_run:
            self.state_data.first_run = False
            self.host_page.hide_first_run()
        self.state_data.save(self.paths.gui_state)
        if self.state_data.auto_forward and not getattr(self, "offline", False):
            self.forward_ports()

    def stop_server(self, then: Callable[[], None] | None = None) -> None:
        if self.server is None:
            if then:
                then()
            return
        self.set_server_state("stopping")
        self.console_page.append_local("Stopping the server (graceful shutdown)...", "COMMAND")
        server = self.server

        def done(_code) -> None:
            if then:
                self.call_soon(then)

        server.stop_async(done)

    def restart_server(self) -> None:
        self.stop_server(then=lambda: self.after(400, self.start_server))

    def toggle_server(self) -> None:
        if self.server_state == "stopped":
            self.start_server()
        elif self.server_state in ("running", "starting"):
            humans = int((self.status or {}).get("humans", 0) or 0)
            if humans and not messagebox.askyesno(APP_TITLE, f"{humans} player(s) are online. Stop the server?"):
                return
            self.stop_server()

    def _on_server_line(self, line: str) -> None:
        parsed = self.console_page.append_server(line)
        self.events.feed(parsed)
        if self.events.started and self.server_state == "starting":
            self.set_server_state("running")

    def _on_server_exit(self, code: int) -> None:
        expected = self.server is not None and self.server.stopping
        self.server = None
        self.status = None
        if self.forwarder.mapped:
            self.unforward_ports(quiet=True)
        self.set_server_state("stopped")
        if expected or code == 0:
            self.console_page.append_local(f"Server stopped (exit code {code}).", "COMMAND")
            self.footer("Server stopped.")
        else:
            hint = ""
            if self.events.port_in_use:
                hint = "\n\nThe port is already in use: another server (or another program) is using it."
            self.console_page.append_local(f"Server exited unexpectedly (exit code {code}).", "ERROR")
            self.footer(f"Server stopped unexpectedly (exit code {code}). See the Console tab.", theme.DANGER)
            if not self.closing:
                messagebox.showerror(APP_TITLE, f"The server stopped unexpectedly (exit code {code}).{hint}\n\n"
                                                "The Console tab shows its last messages.")
        if self.closing:
            self._finish_close()

    def set_server_state(self, state: str) -> None:
        self.server_state = state
        colours = {"stopped": theme.MUTED, "starting": theme.WARN, "running": theme.OK, "stopping": theme.WARN}
        self.header_state.configure(text=f"●  {state.upper()}", text_color=colours.get(state, theme.MUTED))
        self.host_page.refresh_status()
        self.console_page.refresh_state()

    def _poll_status(self) -> None:
        if self.server is not None:
            status = read_status(self.paths.status_file)
            if status is not None:
                self.status = status
                if status.get("state") == "running" and self.server_state == "starting":
                    self.set_server_state("running")
            if self.forwarder.renew_due():
                self.background(self.forwarder.renew)
        self.host_page.refresh_status()
        if not self.closing:
            self.after(1000, self._poll_status)

    # ---- network helpers --------------------------------------------------
    def _refresh_addresses(self) -> None:
        public = None if getattr(self, "offline", False) else network.public_ip()
        lan = network.local_ip()

        def apply() -> None:
            self.public_ip, self.lan_ip = public, lan
            self.network_page.refresh_addresses()

        self.call_soon(apply)

    def _ping_master_loop(self) -> None:
        if getattr(self, "offline", False):
            self.call_soon(lambda: setattr(self, "master_latency", "offline"))
            return
        while not self.closing:
            base = "https://www.aosplay.net"
            if self.doc is not None:
                base = str(self.doc.get("revival", "base_url", base) or base).rstrip("/")
            base = getattr(self, "master_url", None) or base
            started = time.perf_counter()
            try:
                request = urllib.request.Request(base + "/updates/stable.json", method="HEAD",
                                                 headers={"User-Agent": "BattleSpades-Server-GUI"})
                with urllib.request.urlopen(request, timeout=6):
                    pass
                text = f"{(time.perf_counter() - started) * 1000:.0f} ms"
            except urllib.error.HTTPError:
                # Any HTTP answer proves the site is reachable.
                text = f"{(time.perf_counter() - started) * 1000:.0f} ms"
            except Exception:
                text = "unreachable"
            self.call_soon(lambda t=text: setattr(self, "master_latency", t))
            for _ in range(60):
                if self.closing:
                    return
                time.sleep(1)

    def maps_dir(self) -> Path:
        """``[world] maps_path`` resolved like the server does (relative to the root)."""

        configured = str(self.doc.get("world", "maps_path", "maps") or "maps") if self.doc is not None else "maps"
        path = Path(configured).expanduser()
        return path if path.is_absolute() else self.paths.root / path

    def game_ports(self) -> list[int]:
        return host_settings.server_ports(self.doc) if self.doc is not None else [host_settings.RETAIL_GAME_PORT]

    def forward_ports(self) -> None:
        if getattr(self, "offline", False):
            self.footer("Port forwarding is disabled by --offline.", theme.MUTED)
            return
        ports = self.game_ports()
        self.network_page.set_forward_status("Asking the router to forward UDP " + ", ".join(map(str, ports)) + "...", theme.MUTED)

        def done(result) -> None:
            if isinstance(result, Exception):
                result = network.ForwardResult(False, message=str(result))
            self.forward_result = result
            colour = theme.OK if result.ok else theme.WARN
            self.network_page.set_forward_status(result.message, colour)
            self.console_page.append_local("Port forwarding: " + result.message, "INFO" if result.ok else "WARNING")

        self.background(lambda: self.forwarder.open(ports), done)

    def unforward_ports(self, quiet: bool = False) -> None:
        def done(result) -> None:
            if isinstance(result, Exception):
                result = network.ForwardResult(False, message=str(result))
            self.forward_result = None
            if result.message:
                self.network_page.set_forward_status(result.message, theme.MUTED if result.ok else theme.WARN)
                if not quiet:
                    self.console_page.append_local("Port forwarding: " + result.message, "INFO")

        self.background(self.forwarder.close, done)

    def copy_join_info(self) -> None:
        if self.doc is None:
            return
        status = self.status or {}
        text = host_settings.join_info(
            name=str(self.doc.get("server", "name", "")),
            public_ip=self.public_ip, lan_ip=self.lan_ip,
            port=int(self.doc.get("server", "port", host_settings.RETAIL_GAME_PORT)),
            password=bool(self.doc.get("server", "password", "")),
            steam_hosted=bool((status.get("steam_p2p") or {}).get("hosted")) or bool(self.events.steam_lobby),
            steam_lobby=self.events.steam_lobby,
        )
        self.clipboard_clear()
        self.clipboard_append(text)
        self.footer("Join info copied to the clipboard.", theme.OK)

    def check_updates(self) -> None:
        if getattr(self, "offline", False):
            self.footer("Update checks are disabled by --offline.", theme.MUTED)
            return
        from server_gui import updates

        self.footer("Checking for updates...")
        url = str(self.doc.get("updates", "update_manifest_url", "")) if self.doc else None

        def done(result) -> None:
            if isinstance(result, Exception):
                self.footer(f"Update check failed: {result}", theme.WARN)
                return
            self.footer(result.message, theme.OK if result.ok and not result.newer else theme.WARN)
            if result.newer:
                if messagebox.askyesno(APP_TITLE, result.message + "\n\nOpen aosplay.net now?"):
                    webbrowser.open(SITE)

        self.background(lambda: updates.check(self.paths.root, url or None), done)

    # ---- closing ----------------------------------------------------------
    def on_close(self) -> None:
        if self.closing:
            return
        if self.server is not None:
            humans = int((self.status or {}).get("humans", 0) or 0)
            question = (f"{humans} player(s) are online.\n\nStop the server and close?" if humans
                        else "The server is running.\n\nStop it and close the window?")
            if not messagebox.askyesno(APP_TITLE, question, icon="warning" if humans else "question"):
                return
        if self.doc is not None and self.doc.dirty:
            answer = messagebox.askyesnocancel(APP_TITLE, "Save your changes to config.toml before closing?")
            if answer is None:
                return
            if answer:
                self.save_settings(lambda ok: self._begin_close() if ok else None)
                return
        self._begin_close()

    def _begin_close(self) -> None:
        self.closing = True
        self._remember_geometry()
        self.state_data.save(self.paths.gui_state)
        if self.server is not None:
            self.footer("Stopping the server before closing...", theme.WARN)
            self.stop_server()
        else:
            self._finish_close()

    def _finish_close(self) -> None:
        if self.forwarder.mapped:
            try:
                self.forwarder.close()
            except Exception:
                pass
        self.lock.release()
        self.destroy()


# --------------------------------------------------------------------------
# Host tab


class HostPage(ctk.CTkFrame):
    def __init__(self, master, app: App) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.loading = False
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0, minsize=360)
        self.grid_rowconfigure(0, weight=1)

        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent", scrollbar_button_color=theme.BORDER)
        scroll.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        scroll.grid_columnconfigure(0, weight=1)
        self.form_parent = scroll

        self.name_var = tk.StringVar()
        self.mode_var = tk.StringVar()
        self.map_var = tk.StringVar()
        self.rotation_var = tk.StringVar(value="Retail playlist")
        self.players_var = tk.StringVar()
        self.bots_var = tk.BooleanVar()
        self.botmode_var = tk.StringVar(value="Fill empty slots")
        self.bot_target_var = tk.IntVar(value=12)
        self.difficulty_var = tk.StringVar(value="Mixed")
        self.password_var = tk.StringVar()
        self.admin_var = tk.StringVar()
        self.auto_admin_var = tk.StringVar()
        self.p2p_var = tk.BooleanVar(value=app.state_data.steam_p2p)
        self.custom_rotation: list[str] = []
        self.mode_codes = {m.title: m.code for m in catalog.mode_choices()}
        self.map_display: dict[str, str] = {}

        self.first_run = ctk.CTkFrame(scroll, fg_color="#2c3a17", corner_radius=10, border_width=1, border_color=theme.OLIVE)
        ctk.CTkLabel(self.first_run, text="YOUR SERVER IS READY", font=heading_font(22), text_color=theme.KHAKI,
                     anchor="w").grid(row=0, column=0, sticky="w", padx=16, pady=(12, 0))
        port_note = ""
        if getattr(app, "_first_run_port_note", False):
            port_note = f" It will use UDP port {host_settings.RETAIL_GAME_PORT}, the original game's port."
        note(self.first_run, "Pick a name, a mode and a map, then press START. Friends on Steam can join without any "
                             "router setup while Steam P2P is on." + port_note, theme.TEXT, 13, 640).grid(
            row=1, column=0, sticky="w", padx=16, pady=(2, 12))
        if app.state_data.first_run:
            self.first_run.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        self._build_match_card(scroll)
        self._build_bots_card(scroll)
        self._build_access_card(scroll)
        self._build_status_column()
        self.load_from_doc()
        for variable in (self.name_var, self.map_var, self.rotation_var, self.players_var, self.bots_var,
                         self.botmode_var, self.bot_target_var, self.difficulty_var, self.password_var, self.admin_var,
                         self.auto_admin_var):
            variable.trace_add("write", lambda *_: self.on_change())

    # ---- cards ------------------------------------------------------------
    def _build_match_card(self, parent) -> None:
        card = Card(parent, "Match", "What players see in the server list and what they play.")
        card.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        body = card.body
        field_label(body, "Server name", 0, "up to 31 characters")
        self.name_entry = entry(body, self.name_var)
        self.name_entry.grid(row=0, column=1, sticky="ew", pady=(8, 0))
        self.name_count = note(body, "", theme.FAINT, 11)
        self.name_count.grid(row=1, column=1, sticky="e")

        field_label(body, "Game mode", 2)
        self.mode_menu = option_menu(body, list(self.mode_codes), self.mode_var, command=lambda _v: self.on_mode_change())
        self.mode_menu.grid(row=2, column=1, sticky="ew", pady=(8, 0))

        field_label(body, "Start map", 3)
        map_row = ctk.CTkFrame(body, fg_color="transparent")
        map_row.grid(row=3, column=1, sticky="ew", pady=(8, 0))
        map_row.grid_columnconfigure(0, weight=1)
        self.map_menu = option_menu(map_row, [""], self.map_var)
        self.map_menu.grid(row=0, column=0, sticky="ew")
        quiet_button(map_row, "Workshop maps…", self.import_workshop, width=210, height=32).grid(
            row=0, column=1, padx=(10, 0))

        field_label(body, "Next maps", 4, "voted at the end of a round")
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.grid(row=4, column=1, sticky="ew", pady=(8, 0))
        self.rotation_buttons = segmented(row, ("Retail playlist", "This map only", "Custom"), self.rotation_var,
                                          command=lambda _v: self.on_rotation_change())
        self.rotation_buttons.grid(row=0, column=0, sticky="w")
        self.custom_button = quiet_button(row, "Choose maps...", self.choose_rotation, width=120)
        self.custom_button.grid(row=0, column=1, padx=(10, 0))
        self.rotation_note = note(body, "", theme.FAINT, 11)
        self.rotation_note.grid(row=5, column=1, sticky="w")

        field_label(body, "Max players", 6)
        self.players_menu = option_menu(body, [str(n) for n in catalog.player_count_options()], self.players_var, width=120)
        self.players_menu.grid(row=6, column=1, sticky="w", pady=(8, 0))

    def _build_bots_card(self, parent) -> None:
        card = Card(parent, "Bots", "Computer-controlled players. They leave to make room when people join.")
        card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        body = card.body
        field_label(body, "Bots", 0)
        switch(body, "Add bots", self.bots_var, command=self.refresh_bot_widgets).grid(row=0, column=1, sticky="w", pady=(8, 0))
        field_label(body, "How many", 1)
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        row.grid_columnconfigure(1, weight=1)
        self.botmode_buttons = segmented(row, ("Fill empty slots", "Fixed number"), self.botmode_var,
                                         command=lambda _v: self.refresh_bot_widgets())
        self.botmode_buttons.grid(row=0, column=0, sticky="w")
        self.bot_slider = ctk.CTkSlider(row, from_=0, to=32, number_of_steps=32, variable=self.bot_target_var,
                                        progress_color=theme.OLIVE_BRIGHT, button_color=theme.KHAKI,
                                        button_hover_color="#ffffff", fg_color=theme.BORDER)
        self.bot_slider.grid(row=0, column=1, sticky="ew", padx=(14, 8))
        self.bot_value = ctk.CTkLabel(row, text="", font=heading_font(20), text_color=theme.KHAKI, width=36)
        self.bot_value.grid(row=0, column=2)
        self.bot_note = note(body, "", theme.FAINT, 11)
        self.bot_note.grid(row=2, column=1, sticky="w")
        field_label(body, "Difficulty", 3)
        self.difficulty_buttons = segmented(body, [label for _code, label in catalog.DIFFICULTIES], self.difficulty_var)
        self.difficulty_buttons.grid(row=3, column=1, sticky="w", pady=(8, 0))

    def _build_access_card(self, parent) -> None:
        card = Card(parent, "Access", "Who can join and who can run admin commands.")
        card.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        body = card.body
        field_label(body, "Steam P2P", 0)
        self.p2p_switch = switch(body, "Friends can join through Steam without port forwarding",
                                 self.p2p_var, command=self.on_p2p_change)
        self.p2p_switch.grid(row=0, column=1, sticky="w", pady=(8, 0))
        self.p2p_note = note(body, "", theme.FAINT, 11)
        self.p2p_note.grid(row=1, column=1, sticky="w")

        field_label(body, "Join password", 2, "optional")
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.grid(row=2, column=1, sticky="ew", pady=(8, 0))
        row.grid_columnconfigure(0, weight=1)
        self.password_entry = entry(row, self.password_var, show="•", placeholder_text="leave empty for a public server")
        self.password_entry.grid(row=0, column=0, sticky="ew")
        self.show_password = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text="Show", variable=self.show_password, font=body_font(12), text_color=theme.MUTED,
                        fg_color=theme.OLIVE, hover_color=theme.OLIVE_HOVER, border_color=theme.BORDER, width=60,
                        command=self._toggle_password).grid(row=0, column=1, padx=(10, 0))
        note(body, "Only the BattleSpades client can answer a password prompt.", theme.FAINT, 11).grid(row=3, column=1, sticky="w")

        field_label(body, "Admin password", 4, "for /admin in game")
        admin_row = ctk.CTkFrame(body, fg_color="transparent")
        admin_row.grid(row=4, column=1, sticky="ew", pady=(8, 0))
        admin_row.grid_columnconfigure(0, weight=1)
        self.admin_entry = entry(admin_row, self.admin_var, show="•", placeholder_text="12+ characters")
        self.admin_entry.grid(row=0, column=0, sticky="ew")
        quiet_button(admin_row, "Generate", self._generate_admin_password, width=90).grid(row=0, column=1, padx=(10, 0))
        self.admin_note = note(body, "", theme.FAINT, 11)
        self.admin_note.grid(row=5, column=1, sticky="w")

        field_label(body, "Auto-admin", 6, "optional")
        self.auto_admin_entry = entry(body, self.auto_admin_var,
                                      placeholder_text="steam:7656119... or aosplay:ply_..., comma separated")
        self.auto_admin_entry.grid(row=6, column=1, sticky="ew", pady=(8, 0))
        note(body, "These verified Steam / AoSPlay accounts become admin when they join. Names are never trusted. "
                   "You already have admin rights in the Console tab.", theme.FAINT, 11).grid(row=7, column=1, sticky="w")

    def _build_status_column(self) -> None:
        column = ctk.CTkFrame(self, fg_color="transparent")
        column.grid(row=0, column=1, sticky="nsew")
        column.grid_columnconfigure(0, weight=1)
        self.start_button = ctk.CTkButton(column, text="START SERVER", font=heading_font(28), height=74,
                                          corner_radius=12, fg_color=theme.OLIVE, hover_color=theme.OLIVE_HOVER,
                                          text_color="#f6f3e6", command=self.app.toggle_server)
        self.start_button.grid(row=0, column=0, sticky="ew")
        self.restart_banner = ctk.CTkFrame(column, fg_color="#3b3215", corner_radius=8, border_width=1, border_color=theme.WARN)
        note(self.restart_banner, "Saved changes apply after a restart.", theme.WARN, 12, 220).grid(row=0, column=0, padx=10, pady=8, sticky="w")
        quiet_button(self.restart_banner, "Restart now", self.app.restart_server, width=100).grid(row=0, column=1, padx=(0, 10))

        card = Card(column, "Live status")
        card.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        body = card.body
        self.status_rows: dict[str, ctk.CTkLabel] = {}
        for index, label in enumerate(("State", "Players", "Map", "Mode", "Uptime", "Steam P2P", "aosplay.net")):
            ctk.CTkLabel(body, text=label, font=body_font(12), text_color=theme.MUTED, anchor="w").grid(
                row=index, column=0, sticky="w", pady=3, padx=(0, 12))
            value = ctk.CTkLabel(body, text="-", font=body_font(13, "bold"), text_color=theme.TEXT, anchor="w",
                                 justify="left", wraplength=210)
            value.grid(row=index, column=1, sticky="w", pady=3)
            self.status_rows[label] = value
        buttons = ctk.CTkFrame(column, fg_color="transparent")
        buttons.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        buttons.grid_columnconfigure((0, 1), weight=1)
        accent_button(buttons, "Copy join info", self.app.copy_join_info).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        quiet_button(buttons, "Open console", lambda: self.app.show_tab("Console"), height=34).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        self.join_preview = note(column, "", theme.FAINT, 11, 330)
        self.join_preview.grid(row=4, column=0, sticky="w", pady=(10, 0))

    # ---- data -------------------------------------------------------------
    def load_from_doc(self) -> None:
        doc = self.app.doc
        if doc is None:
            return
        self.loading = True
        try:
            settings = host_settings.read(doc)
            self.name_var.set(settings.name)
            title = catalog.mode_title(settings.mode)
            if title not in self.mode_codes:
                self.mode_codes[title] = settings.mode
                self.mode_menu.configure(values=list(self.mode_codes))
            self.mode_var.set(title)
            self._fill_maps(settings.mode, settings.start_map)
            self.rotation_var.set({"retail": "Retail playlist", "single": "This map only", "custom": "Custom"}[settings.rotation])
            self.custom_rotation = list(settings.custom_rotation)
            values = [str(n) for n in catalog.player_count_options()]
            if str(settings.max_players) not in values:
                values.append(str(settings.max_players))
                values.sort(key=int)
                self.players_menu.configure(values=values)
            self.players_var.set(str(settings.max_players))
            self.bots_var.set(settings.bots_enabled)
            self.botmode_var.set("Fixed number" if settings.bot_mode == "fixed" else "Fill empty slots")
            self.bot_target_var.set(min(32, settings.bot_target))
            self.difficulty_var.set(dict(catalog.DIFFICULTIES).get(settings.difficulty, "Mixed"))
            self.password_var.set(settings.password)
            self.admin_var.set("" if settings.admin_password == "changeme" else settings.admin_password)
            self.auto_admin_var.set(", ".join(settings.auto_admin))
        finally:
            self.loading = False
        self.refresh_bot_widgets()
        self.on_rotation_change(push=False)
        self.on_p2p_change(save=False)
        self._refresh_hints()

    def _fill_maps(self, mode: str, current: str | None) -> None:
        pool = catalog.mode_pool(self.app.maps_dir(), mode) or catalog.available_maps(self.app.maps_dir())
        if current and current not in pool and current in catalog.available_maps(self.app.maps_dir()):
            pool = [current] + pool
        self.map_display = {catalog.map_title(name): name for name in pool}
        self.map_menu.configure(values=list(self.map_display) or [""])
        if current and current in pool:
            self.map_var.set(catalog.map_title(current))
        elif pool:
            self.map_var.set(catalog.map_title(pool[0]))

    def collect(self) -> host_settings.HostSettings:
        doc = self.app.doc
        rotation = {"Retail playlist": "retail", "This map only": "single", "Custom": "custom"}.get(self.rotation_var.get(), "retail")
        difficulty = {label: code for code, label in catalog.DIFFICULTIES}.get(self.difficulty_var.get(), "mixed")
        try:
            players = int(self.players_var.get())
        except ValueError:
            players = 24
        return host_settings.HostSettings(
            name=self.name_var.get(),
            mode=self.mode_codes.get(self.mode_var.get(), "tdm"),
            start_map=self.map_display.get(self.map_var.get(), self.map_var.get()),
            rotation=rotation,
            custom_rotation=list(self.custom_rotation),
            max_players=players,
            bots_enabled=bool(self.bots_var.get()),
            bot_mode="fixed" if self.botmode_var.get() == "Fixed number" else "backfill",
            bot_target=int(self.bot_target_var.get()),
            difficulty=difficulty,
            password=self.password_var.get(),
            admin_password=self.admin_var.get(),
            auto_admin=host_settings.parse_auto_admin(self.auto_admin_var.get()),
            port=int(doc.get("server", "port", host_settings.RETAIL_GAME_PORT)) if doc else host_settings.RETAIL_GAME_PORT,
        )

    def push_to_doc(self) -> None:
        if self.app.doc is None or self.loading:
            return
        host_settings.apply(self.app.doc, self.collect())

    def on_change(self) -> None:
        if self.loading:
            return
        self.push_to_doc()
        self._refresh_hints()
        self.app.doc_changed("host")

    def on_mode_change(self) -> None:
        mode = self.mode_codes.get(self.mode_var.get(), "tdm")
        self._fill_maps(mode, self.map_display.get(self.map_var.get()))
        self.on_change()

    def on_rotation_change(self, push: bool = True) -> None:
        rotation = self.rotation_var.get()
        self.custom_button.configure(state="normal" if rotation == "Custom" else "disabled")
        if rotation == "Retail playlist":
            text = "Players vote between the maps the original game used for this mode."
        elif rotation == "This map only":
            text = "The same map every round."
        else:
            text = f"{len(self.custom_rotation)} map(s): " + ", ".join(catalog.map_title(m) for m in self.custom_rotation[:6]) + (
                "..." if len(self.custom_rotation) > 6 else "")
        self.rotation_note.configure(text=text)
        if push and not self.loading:
            if rotation == "Custom" and not self.custom_rotation:
                self.choose_rotation()
            self.on_change()

    def choose_rotation(self) -> None:
        dialog = RotationDialog(self.app, catalog.available_maps(self.app.maps_dir()), self.custom_rotation,
                                catalog.mode_pool(self.app.maps_dir(), self.mode_codes.get(self.mode_var.get(), "tdm")))
        self.app.wait_window(dialog)
        if dialog.result is not None:
            self.custom_rotation = dialog.result
            self.on_rotation_change(push=False)
            self.on_change()

    def import_workshop(self) -> None:
        dialog = WorkshopDialog(self.app, self.app.maps_dir())
        self.app.wait_window(dialog)
        if not dialog.imported:
            return
        mode = self.mode_codes.get(self.mode_var.get(), "tdm")
        current = self.map_display.get(self.map_var.get(), self.map_var.get())
        self._fill_maps(mode, current)
        names = ", ".join(catalog.map_title(s) for s in dialog.imported)
        self.app.footer(f"Imported {names}. Select it as the start map or add it to a custom rotation.", theme.OK)

    def refresh_bot_widgets(self) -> None:
        enabled = bool(self.bots_var.get())
        state = "normal" if enabled else "disabled"
        for widget in (self.botmode_buttons, self.bot_slider, self.difficulty_buttons):
            widget.configure(state=state)
        count = int(self.bot_target_var.get())
        self.bot_value.configure(text=str(count), text_color=theme.KHAKI if enabled else theme.FAINT)
        if not enabled:
            text = "No bots. Admins can still add some with the console's bot commands."
        elif self.botmode_var.get() == "Fixed number":
            text = f"Always {count} bots (one slot always stays free for a joining player)."
        else:
            text = f"Bots fill the teams until {count} are playing, and leave as people join."
        self.bot_note.configure(text=text)

    def on_p2p_change(self, save: bool = True) -> None:
        readiness = self.app.steam_p2p_readiness() if self.app.doc is not None else host_settings.Readiness(False)
        if not readiness.available:
            self.p2p_switch.configure(state="disabled")
            self.p2p_var.set(False)
            self.p2p_note.configure(text=readiness.reason, text_color=theme.FAINT)
        else:
            self.p2p_switch.configure(state="normal")
            if save:
                self.app.state_data.steam_p2p = bool(self.p2p_var.get())
                self.app.state_data.save(self.app.paths.gui_state)
                if self.app.server_state in ("running", "starting"):
                    self.app.restart_needed = True
            else:
                self.p2p_var.set(self.app.state_data.steam_p2p)
            message = readiness.warning or ("Players with the Steam relay drop-in see it as [Steam] in the "
                                            "in-game server browser. Needs Steam running on this PC.")
            self.p2p_note.configure(text=message, text_color=theme.WARN if readiness.warning else theme.FAINT)
        self.refresh_status()

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.show_password.get() else "•")
        self.admin_entry.configure(show="" if self.show_password.get() else "•")

    def _generate_admin_password(self) -> None:
        self.show_password.set(True)
        self._toggle_password()
        self.admin_var.set(host_settings.generate_admin_password())

    def _refresh_hints(self) -> None:
        name = self.name_var.get()
        self.name_count.configure(text=f"{len(name)}/31" + ("  (longer names are cut off)" if len(name) > 31 else ""),
                                  text_color=theme.WARN if len(name) > 31 else theme.FAINT)
        admin = self.admin_var.get()
        doc = self.app.doc
        if not admin and doc is not None and host_settings.admin_password_is_shipped_default(doc):
            self.admin_note.configure(
                text='WARNING: still the shipped default "changeme", so in-game /admin is disabled. '
                     "Press Generate and save.", text_color=theme.WARN)
        elif not admin:
            self.admin_note.configure(text="Not set: in-game /admin stays disabled (the console still works).", text_color=theme.FAINT)
        elif getattr(self.app, "_generated_admin_password", None) == admin:
            self.admin_note.configure(
                text="A random admin password was generated for you (tick Show to see it). Save to keep it.",
                text_color=theme.KHAKI)
        elif len(admin) < 12:
            self.admin_note.configure(text="Too short: needs at least 12 characters.", text_color=theme.WARN)
        else:
            self.admin_note.configure(text="Players type /admin <password> in chat to log in.", text_color=theme.FAINT)

    def hide_first_run(self) -> None:
        self.first_run.grid_forget()

    # ---- status column ----------------------------------------------------
    def refresh_status(self) -> None:
        app = self.app
        state = app.server_state
        labels = {"stopped": "START SERVER", "starting": "STARTING...", "running": "STOP SERVER", "stopping": "STOPPING..."}
        colours = {"stopped": (theme.OLIVE, theme.OLIVE_HOVER), "starting": (theme.PANEL_ALT, theme.PANEL_ALT),
                   "running": (theme.DANGER, theme.DANGER_HOVER), "stopping": (theme.PANEL_ALT, theme.PANEL_ALT)}
        fg, hover = colours[state]
        self.start_button.configure(text=labels[state], fg_color=fg, hover_color=hover,
                                    state="disabled" if state == "stopping" else "normal")
        if app.restart_needed and state in ("running", "starting"):
            self.restart_banner.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        else:
            self.restart_banner.grid_forget()
        status = app.status or {}
        doc = app.doc
        rows = self.status_rows
        state_colour = {"running": theme.OK, "starting": theme.WARN, "stopping": theme.WARN}.get(state, theme.MUTED)
        state_text = state.capitalize()
        if status.get("stale") and state == "running":
            state_text, state_colour = "Not responding", theme.DANGER
        rows["State"].configure(text=state_text, text_color=state_colour)
        if status:
            bots = int(status.get("bots", 0) or 0)
            rows["Players"].configure(text=f"{status.get('humans', 0)} / {status.get('max_players', '?')}" + (f"  (+{bots} bots)" if bots else ""))
            rows["Map"].configure(text=catalog.map_title(str(status.get("map", "-"))))
            rows["Mode"].configure(text=catalog.mode_title(str(status.get("mode", "-"))))
            rows["Uptime"].configure(text=format_uptime(float(status.get("uptime_seconds", 0) or 0)))
        else:
            max_players = doc.get("server", "max_players", "-") if doc else "-"
            rows["Players"].configure(text=f"0 / {max_players}" if state != "stopped" else "-")
            rows["Map"].configure(text=catalog.map_title(str(doc.get("game", "default_map", "-"))) if doc else "-")
            rows["Mode"].configure(text=catalog.mode_title(str(doc.get("game", "default_mode", "-"))) if doc else "-")
            rows["Uptime"].configure(text="-")
        p2p = status.get("steam_p2p") or {}
        if state == "stopped":
            p2p_text = "On at start" if self.p2p_var.get() else "Off"
        elif not p2p.get("enabled") and status:
            p2p_text = "Off"
        elif p2p.get("hosted") or app.events.steam_lobby:
            p2p_text = "Hosting" + (f" (lobby {app.events.steam_lobby})" if app.events.steam_lobby else "")
        else:
            p2p_text = "Connecting to Steam..." if status else "-"
        rows["Steam P2P"].configure(text=p2p_text)
        revival = status.get("revival") or {}
        listed = ""
        if revival.get("registering"):
            listed = "  · listed"
        elif status:
            listed = "  · not listed"
        rows["aosplay.net"].configure(text=f"{app.master_latency}{listed}")
        port = doc.get("server", "port", "?") if doc else "?"
        address = app.public_ip or app.lan_ip or "your address"
        self.join_preview.configure(text=f"Direct join: {address}:{port}")


class RotationDialog(ctk.CTkToplevel):
    def __init__(self, master, maps: list[str], selected: list[str], recommended: list[str]) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.title("Map rotation")
        self.geometry("520x620")
        self.result: list[str] | None = None
        self.transient(master)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        note(self, "Tick the maps players can vote for. Maps the original game used for this mode are marked ★.",
             theme.TEXT, 12, 480).grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        frame = ctk.CTkScrollableFrame(self, fg_color=theme.PANEL)
        frame.grid(row=1, column=0, sticky="nsew", padx=16)
        chosen = {name.casefold() for name in selected}
        self.vars: list[tuple[str, tk.BooleanVar]] = []
        recommended_set = {name.casefold() for name in recommended}
        for index, name in enumerate(maps):
            variable = tk.BooleanVar(value=name.casefold() in chosen)
            label = catalog.map_title(name) + ("  ★" if name.casefold() in recommended_set else "")
            ctk.CTkCheckBox(frame, text=label, variable=variable, font=body_font(13), text_color=theme.TEXT,
                            fg_color=theme.OLIVE, hover_color=theme.OLIVE_HOVER, border_color=theme.BORDER).grid(
                row=index, column=0, sticky="w", pady=3, padx=6)
            self.vars.append((name, variable))
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=2, column=0, sticky="ew", padx=16, pady=14)
        quiet_button(buttons, "Recommended", lambda: self._set(recommended_set)).pack(side="left")
        quiet_button(buttons, "None", lambda: self._set(set())).pack(side="left", padx=8)
        accent_button(buttons, "Use these maps", self._ok, width=140).pack(side="right")
        quiet_button(buttons, "Cancel", self.destroy, width=90).pack(side="right", padx=8)
        self.after(100, self.grab_set)

    def _set(self, names: set[str]) -> None:
        for name, variable in self.vars:
            variable.set(name.casefold() in names)

    def _ok(self) -> None:
        self.result = [name for name, variable in self.vars if variable.get()]
        self.destroy()


class WorkshopDialog(ctk.CTkToplevel):
    """Browse maps immediately; load thumbnails and the selected gallery off Tk."""

    def __init__(self, master: App, maps_dir: Path) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.app = master
        self.maps_dir = maps_dir
        self.preview_cache = master.paths.state_dir / "workshop-previews"
        self.preview_images: list[ctk.CTkImage] = []
        self.preview_labels: dict[str, ctk.CTkLabel] = {}
        self.cards: dict[str, ctk.CTkFrame] = {}
        self.catalog_cache: dict = {}
        self.gallery_cache: dict = {}
        self.generation = 0
        self.preview_cancel = threading.Event()
        self.gallery_cancel = threading.Event()
        self.selected = None
        self.gallery_index = 0
        self.gallery_image = None
        self.installing = False
        self.extra_folders: list[Path] = []
        self.items: list = []
        self.vars: list[tuple[Any, tk.BooleanVar]] = []
        self.imported: list[str] = []
        self.pending_message: tuple[str, str] | None = None
        self.busy = False
        self.cancel = threading.Event()
        self.close_after_work = False
        self.page = 0
        self.more = False
        self.public = True
        self.title("Workshop maps")
        self.geometry("1080x760")
        self.minsize(980, 680)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(self, text="STEAM WORKSHOP MAPS", font=heading_font(22), text_color=theme.KHAKI).grid(
            row=0, column=0, sticky="w", padx=16, pady=(14, 0))
        note(self, "Download public Ace of Spades maps without Steam or import maps already on this computer. "
                   f"Installed into: {maps_dir}\nChoose an installed map as the start map or add it to a custom rotation.",
             theme.MUTED, 12, 780).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))
        search = ctk.CTkFrame(self, fg_color="transparent")
        search.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 8))
        search.grid_columnconfigure(0, weight=1)
        self.source = segmented(search, values=["Public Workshop", "On this computer"], command=self.change_source)
        self.source.set("Public Workshop")
        self.source.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.query = entry(search, placeholder_text="Search maps or paste a Steam Workshop link / ID", height=32)
        self.query.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        self.query.bind("<Return>", lambda _event: self.search())
        self.search_button = quiet_button(search, "Search", self.search, width=90, height=32)
        self.search_button.grid(row=1, column=1)
        from server_gui import workshop_public as wp
        filters = ctk.CTkFrame(search, fg_color="transparent")
        filters.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.sort = option_menu(filters, values=list(wp.SORTS), command=lambda _v: self.search(), width=175)
        self.period = option_menu(filters, values=list(wp.PERIODS), command=lambda _v: self.search(), width=150)
        self.mode = option_menu(filters, values=["All modes", *wp.MODES], command=lambda _v: self.search(), width=135)
        for index, (label, widget) in enumerate((("Order", self.sort), ("Period", self.period), ("Game mode", self.mode))):
            ctk.CTkLabel(filters, text=label, text_color=theme.MUTED).grid(row=0, column=index * 2, padx=(0, 7))
            widget.grid(row=0, column=index * 2 + 1, padx=(0, 18))
        content = ctk.CTkFrame(self, fg_color="transparent")
        content.grid(row=3, column=0, sticky="nsew", padx=16)
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)
        self.list_frame = ctk.CTkScrollableFrame(content, fg_color=theme.PANEL, corner_radius=10,
                                                 scrollbar_button_color=theme.BORDER)
        self.list_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        self.list_frame.grid_columnconfigure(0, weight=1)
        detail = ctk.CTkFrame(content, fg_color=theme.PANEL, corner_radius=10, width=354)
        detail.grid(row=0, column=1, sticky="nsew")
        detail.grid_columnconfigure(0, weight=1)
        detail.grid_rowconfigure(4, weight=1)
        self.detail_title = ctk.CTkLabel(detail, text="Select a map", font=heading_font(21),
                                        wraplength=330, text_color=theme.KHAKI, anchor="w")
        self.detail_title.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        self.detail_preview = ctk.CTkLabel(detail, text="SCREENSHOTS", width=330, height=186,
                                          fg_color="#191a14", text_color=theme.FAINT)
        self.detail_preview.grid(row=1, column=0, padx=12)
        gallery_bar = ctk.CTkFrame(detail, fg_color="transparent")
        gallery_bar.grid(row=2, column=0, sticky="ew", padx=12, pady=5)
        self.gallery_previous = quiet_button(gallery_bar, "‹", lambda: self.turn_image(-1), width=40)
        self.gallery_previous.pack(side="left")
        self.gallery_next = quiet_button(gallery_bar, "›", lambda: self.turn_image(1), width=40)
        self.gallery_next.pack(side="right")
        self.gallery_count = ctk.CTkLabel(gallery_bar, text="", text_color=theme.MUTED)
        self.gallery_count.pack(expand=True)
        self.detail_stats = note(detail, "", theme.OLIVE_BRIGHT, 11, 330)
        self.detail_stats.grid(row=3, column=0, sticky="ew", padx=12, pady=3)
        self.detail_text = ctk.CTkTextbox(detail, width=330, height=90, wrap="word", fg_color="transparent",
                                          text_color=theme.TEXT, font=body_font(12))
        self.detail_text.grid(row=4, column=0, sticky="nsew", padx=8, pady=(0, 6))
        self.detail_text.configure(state="disabled")
        self.detail_download = accent_button(detail, "Download this map", lambda: self.do_import(self.selected))
        self.detail_download.grid(row=5, column=0, sticky="ew", padx=12, pady=(4, 12))
        self.status = note(self, "Looking for Workshop maps...", theme.MUTED, 12, 720)
        self.status.grid(row=4, column=0, sticky="ew", padx=16, pady=(8, 0))
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=5, column=0, sticky="ew", padx=16, pady=12)
        self.folder_button = quiet_button(buttons, "Pick folder…", self.pick_folder, width=105)
        self.folder_button.pack(side="left")
        self.refresh_button = quiet_button(buttons, "Refresh", self.scan, width=75)
        self.refresh_button.pack(side="left", padx=6)
        self.previous_button = quiet_button(buttons, "Previous", lambda: self.turn_page(-1), width=75)
        self.previous_button.pack(side="left")
        self.next_button = quiet_button(buttons, "Next", lambda: self.turn_page(1), width=65)
        self.next_button.pack(side="left", padx=6)
        self.import_button = accent_button(buttons, "Download selected", self.do_import, width=160)
        self.import_button.pack(side="right")
        quiet_button(buttons, "Close", self.close, width=65).pack(side="right", padx=6)
        self.cancel_button = quiet_button(buttons, "Cancel", self.cancel_work, width=65)
        self.cancel_button.pack(side="right")
        self._grab_after = self.after(100, self.grab_set)
        self.scan()

    def destroy(self) -> None:
        self.cancel.set()
        self.preview_cancel.set()
        self.gallery_cancel.set()
        if getattr(self, "_grab_after", None) is not None:
            self.after_cancel(self._grab_after)
            self._grab_after = None
        super().destroy()

    def close(self) -> None:
        if self.busy:
            self.close_after_work = True
            self.cancel_work()
        else:
            self.destroy()

    def cancel_work(self) -> None:
        self.cancel.set()
        self.status.configure(text="Cancelling; waiting for the current operation to finish...", text_color=theme.MUTED)

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        locked = busy and self.installing
        state = "disabled" if locked else "normal"
        for widget in (self.source, self.folder_button, self.refresh_button):
            widget.configure(state=state)
        for widget in (self.query, self.search_button):
            widget.configure(state="normal" if self.public and not locked else "disabled")
        for widget in (self.sort, self.period, self.mode):
            widget.configure(state="normal" if self.public and not locked else "disabled")
        if self.sort.get() != "Most popular":
            self.period.configure(state="disabled")
        self.import_button.configure(state="disabled" if busy else "normal")
        self.detail_download.configure(state="normal" if not busy and self.selected and self.selected.ok else "disabled")
        self.previous_button.configure(state="normal" if self.public and self.page > 0 and not busy else "disabled")
        self.next_button.configure(state="normal" if self.public and self.more and not busy else "disabled")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.import_button.configure(text="Download selected" if self.public else "Import selected")
        if not busy and not any(item.ok for item, _var in self.vars):
            self.import_button.configure(state="disabled")

    def change_source(self, value: str) -> None:
        if self.installing:
            return
        self.public = value == "Public Workshop"
        self.page = 0
        self.scan()

    def search(self) -> None:
        if not self.installing:
            self.page = 0
            self.scan()

    def turn_page(self, delta: int) -> None:
        if not self.busy and (delta < 0 and self.page > 0 or delta > 0 and self.more):
            self.page += delta
            self.scan()

    def pick_folder(self) -> None:
        if self.busy:
            return
        folder = filedialog.askdirectory(parent=self, title="Folder with Workshop items or Subscribed_* maps")
        if folder:
            self.extra_folders.append(Path(folder))
            self.public = False
            self.source.set("On this computer")
            self.scan()

    def scan(self) -> None:
        from server_gui import workshop_import, workshop_public
        from copy import deepcopy

        if self.installing:
            return
        self.cancel.set()
        self.preview_cancel.set()
        self.gallery_cancel.set()
        self.generation += 1
        generation = self.generation
        self.cancel = threading.Event()
        self._set_busy(True)
        self.status.configure(text="Looking for Workshop maps...", text_color=theme.MUTED)
        folders = list(self.extra_folders)
        query, page, cancel = self.query.get(), self.page, self.cancel
        public = self.public
        sort = workshop_public.SORTS[self.sort.get()]
        days = workshop_public.PERIODS[self.period.get()]
        tag = "" if self.mode.get() == "All modes" else self.mode.get()
        key = (query, page, sort, days, tag, public, tuple(folders))
        cached = self.catalog_cache.get(key)
        if cached and cached[0] > time.monotonic():
            self._show(deepcopy(cached[1]))
            return

        def work():
            return (workshop_public.browse(query, page, cancel, sort=sort, days=days, tag=tag) if public else
                      workshop_import.find_items(folders, self.maps_dir))

        def done(result) -> None:
            if not self.winfo_exists() or generation != self.generation:
                return
            if not isinstance(result, Exception) and not cancel.is_set():
                if len(self.catalog_cache) >= 16:
                    self.catalog_cache.pop(next(iter(self.catalog_cache)))
                self.catalog_cache[key] = (time.monotonic() + 60, deepcopy(result))
            self._show(result)
        self.app.background(work, done)

    def _show(self, items) -> None:
        if not self.winfo_exists():
            return
        self._set_busy(False)
        if self.close_after_work:
            self.destroy()
            return
        from server_gui import workshop_import, workshop_public

        for child in self.list_frame.winfo_children():
            child.destroy()
        self.preview_images.clear()
        self.preview_labels.clear()
        self.cards.clear()
        self.selected = None
        self.gallery_cancel.set()
        self.vars = []
        self.items = []
        if isinstance(items, Exception):
            self.more = False
            self._set_busy(False)
            self.status.configure(text=f"Could not load maps: {items}", text_color=theme.WARN)
            return
        if isinstance(items, workshop_public.WorkshopPage):
            self.more = items.more
            items = items.items
            installed = workshop_import.load_index(self.maps_dir)
            for item in items:
                stem = installed.get(item.published_id, "")
                item.imported_as = stem if stem and (self.maps_dir / f"{stem}.vxl").is_file() else ""
        else:
            self.more = False
        self.items = items
        self.select_map(None)
        if not items:
            note(self.list_frame, "No maps found. Search the Public Workshop or paste an item link / ID.\n\n"
                                  "For local copies, use 'Pick folder…' and choose a Steam Workshop folder or "
                                  "the BattleSpades client's ugc/maps folder.", theme.TEXT, 13, 440).grid(
                row=0, column=0, columnspan=2, sticky="w", padx=14, pady=14)
            self.status.configure(text="No matching downloadable maps. Direct item links also work.")
            self._set_busy(False)
            return
        self.import_button.configure(state="normal")
        from PIL import Image

        for index, item in enumerate(items):
            variable = tk.BooleanVar(value=not self.public and item.ok and not item.imported_as)
            card = ctk.CTkFrame(self.list_frame, fg_color=theme.PANEL_ALT, corner_radius=8,
                                border_width=1, border_color=theme.BORDER)
            card.grid(row=index, column=0, sticky="ew", padx=4, pady=(4, 6))
            card.grid_columnconfigure(2, weight=1)
            self.cards[item.published_id] = card
            box = ctk.CTkCheckBox(card, text="", variable=variable, width=24, fg_color=theme.OLIVE,
                                  hover_color=theme.OLIVE_HOVER, border_color=theme.BORDER)
            box.grid(row=0, column=0, rowspan=3, padx=(12, 8), pady=12)
            if not item.ok:
                box.configure(state="disabled")
            preview_image = None
            if item.preview_path:
                try:
                    with Image.open(item.preview_path, formats=("PNG",)) as image:
                        if image.size == (640, 360):
                            preview_image = ctk.CTkImage(light_image=image.copy(), dark_image=image.copy(), size=(120, 68))
                            self.preview_images.append(preview_image)
                except (OSError, ValueError):
                    pass
            preview_label = ctk.CTkLabel(card, text="" if preview_image else "LOADING" if item.preview_url else "NO PREVIEW", image=preview_image,
                                          width=120, height=68, fg_color="#191a14", corner_radius=4,
                                          text_color=theme.FAINT, font=body_font(11))
            preview_label.grid(row=0, column=1, rowspan=3, padx=(0, 14), pady=10)
            if item.ok:
                preview_label.bind("<Button-1>", lambda _event, selected=item: self.select_map(selected))
            self.preview_labels[item.published_id] = preview_label
            title_label = ctk.CTkLabel(card, text=item.display_title, font=body_font(14, "bold"), wraplength=275,
                         text_color=theme.TEXT if item.ok else theme.FAINT, anchor="w")
            title_label.grid(
                row=0, column=2, sticky="w", padx=(0, 12), pady=(10, 0))
            title_label.bind("<Button-1>", lambda _event, selected=item: self.select_map(selected))
            badge = ""
            if item.imported_as:
                badge = f"Imported as {item.imported_as}"
            details = [f"{item.size / (1024 * 1024):.1f} MB" if item.size else "",
                       "modes: " + ", ".join(m.upper() for m in item.modes) if item.modes else ""]
            text = "  ·  ".join(d for d in details if d)
            if item.error:
                text = f"Cannot import: {item.error}"
            note(card, text, theme.MUTED if item.ok else theme.WARN, 11, 275).grid(
                row=1, column=2, sticky="w", padx=(0, 12), pady=(2, 4))
            if badge:
                ctk.CTkLabel(card, text=badge, font=body_font(11, "bold"), text_color=theme.OLIVE_BRIGHT,
                             height=20).grid(row=2, column=2, pady=(0, 8), sticky="w")
            self.vars.append((item, variable))
        usable = sum(1 for item in items if item.ok)
        done = sum(1 for item in items if item.imported_as)
        self.status.configure(text=f"{len(items)} map(s) found, {usable} importable, {done} already imported. "
                                   f"{'Page ' + str(self.page + 1) + '. ' if self.public else ''}"
                                   "Select maps to install or update.", text_color=theme.MUTED)
        self._set_busy(False)
        self._load_thumbnails()
        self.select_map(items[0])
        if self.pending_message:
            text, colour = self.pending_message
            self.pending_message = None
            self.status.configure(text=text, text_color=colour)

    @staticmethod
    def _preview_image(path: Path | None, size: tuple[int, int]) -> ctk.CTkImage | None:
        from PIL import Image
        if path:
            try:
                with Image.open(path, formats=("PNG",)) as image:
                    if image.size == (640, 360):
                        return ctk.CTkImage(light_image=image.copy(), dark_image=image.copy(), size=size)
            except (OSError, ValueError):
                pass
        return None

    def _load_thumbnails(self) -> None:
        from dataclasses import replace
        from server_gui.workshop_previews import load_previews

        self.preview_cancel.set()
        self.preview_cancel = threading.Event()
        cancel, generation = self.preview_cancel, self.generation
        # A local Workshop preview may be any PNG/JPEG size. Having a path
        # does not mean it is a normalized, usable thumbnail already.
        items = [replace(item) for item in self.items
                 if self.preview_labels[item.published_id].cget("image") is None]

        def ready(loaded) -> None:
            def apply() -> None:
                if cancel.is_set() or generation != self.generation or not self.winfo_exists():
                    return
                item = next((item for item in self.items if item.published_id == loaded.published_id), None)
                if not item:
                    return
                item.preview_path = loaded.preview_path
                label = self.preview_labels[item.published_id]
                image = self._preview_image(item.preview_path, (120, 68))
                if image:
                    self.preview_images.append(image)
                label.configure(image=image, text="" if image else "NO PREVIEW")
                if item is self.selected and self.gallery_index == 0:
                    self._show_image()
            self.app.call_soon(apply)
        if items:
            self.app.background(lambda: load_previews(items, self.preview_cache, cancel, ready), lambda _result: None)

    def select_map(self, item) -> None:
        from copy import deepcopy
        from datetime import datetime, timezone
        import re

        self.gallery_cancel.set()
        self.gallery_cancel = threading.Event()
        cancel = self.gallery_cancel
        self.selected = item
        self.gallery_index = 0
        for key, card in self.cards.items():
            card.configure(border_color=theme.OLIVE_BRIGHT if item and item.published_id == key else theme.BORDER)
        self.detail_title.configure(text=item.display_title if item else "Select a map")
        text = "Choose a map to see screenshots, game modes and its description."
        stats = ""
        if item:
            def date(stamp: int) -> str:
                return datetime.fromtimestamp(stamp, timezone.utc).strftime("%d %b %Y") if 0 < stamp < 4102444800 else "Unknown"
            stats = f"{item.size / (1024 * 1024):.1f} MB  ·  " + (", ".join(item.modes).upper() or "Community map")
            if self.public:
                stats += f"\n{item.subscribers:,} subscribers  ·  {item.favorites:,} favorites"
            text = (f"Published: {date(item.created)}\nUpdated: {date(item.updated)}\n"
                    f"Creator: {item.author or 'Unknown'}\nWorkshop ID: {item.published_id}\n\n" +
                    re.sub(r"\[/?[a-zA-Z][^\]\r\n]{0,200}\]", "", item.description))
        self.detail_stats.configure(text=stats)
        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", text)
        self.detail_text.configure(state="disabled")
        self._show_image()
        self._set_busy(self.busy)
        if not item or not self.public or item.gallery_loaded and not item.gallery_error:
            return
        generation = self.generation
        snapshot = deepcopy(item)
        cache_key = (item.published_id, item.updated)
        cached_gallery = self.gallery_cache.get(cache_key)

        def deliver(urls=None, index=None, path=None, done=False, error="") -> None:
            def apply() -> None:
                if cancel.is_set() or generation != self.generation or not self.winfo_exists() or self.selected is not item:
                    return
                if urls is not None:
                    item.preview_urls = urls
                    item.preview_paths = [item.preview_path, *([None] * max(0, len(urls) - 1))] if urls else []
                    if not error:
                        if len(self.gallery_cache) >= 64:
                            self.gallery_cache.pop(next(iter(self.gallery_cache)))
                        self.gallery_cache[cache_key] = (time.monotonic() + 300, urls)
                if index is not None and index < len(item.preview_paths):
                    item.preview_paths[index] = path
                if done:
                    item.gallery_loaded = True
                    item.gallery_error = error
                self._show_image()
            self.app.call_soon(apply)

        def load() -> None:
            from dataclasses import replace
            from server_gui import workshop_public as wp, workshop_previews as previews
            from server_gui.workshop_import import WorkshopError
            urls = [snapshot.preview_url] if snapshot.preview_url else []
            error = ""
            try:
                gallery = cached_gallery[1] if cached_gallery and cached_gallery[0] > time.monotonic() else wp.gallery(snapshot.published_id, cancel)
                for url in gallery:
                    if url not in urls:
                        urls.append(url)
            except (OSError, WorkshopError):
                if cancel.is_set():
                    return
                error = "More screenshots unavailable"
            deliver(urls=urls, error=error)
            for index, url in enumerate(urls):
                if cancel.is_set():
                    return
                image = replace(snapshot, preview_url=url, preview_path=None)
                if index == 0 and snapshot.preview_path:
                    image.preview_path = snapshot.preview_path
                else:
                    previews.cache_preview(image, self.preview_cache, cancel)
                deliver(index=index, path=image.preview_path)
            deliver(done=True, error=error)
        self.app.background(load, lambda _result: None)

    def _show_image(self) -> None:
        item = self.selected
        count = max(1, len(item.preview_urls)) if item else 0
        path = None
        if item:
            self.gallery_index = min(self.gallery_index, count - 1)
            if self.gallery_index < len(item.preview_paths):
                path = item.preview_paths[self.gallery_index]
            if not path and self.gallery_index == 0:
                path = item.preview_path
        image = self._preview_image(path, (330, 186))
        if not hasattr(self, "_blank_gallery_image"):
            from PIL import Image
            self._blank_gallery_image = ctk.CTkImage(Image.new("RGB", (640, 360), "#191a14"), size=(330, 186))
        # CTk 5.2 leaves the old Tk image name behind when configured with None.
        # Always replace it with a live image so subsequent text updates are safe.
        self.gallery_image = image or self._blank_gallery_image
        self.detail_preview.configure(image=self.gallery_image, text="" if image else
                                      "Loading screenshots…" if item and not item.gallery_loaded and self.public else "No preview")
        self.gallery_count.configure(text=f"{self.gallery_index + 1} / {count}" if item else "")
        self.gallery_previous.configure(state="normal" if count > 1 else "disabled")
        self.gallery_next.configure(state="normal" if count > 1 else "disabled")
        if item and item.gallery_error:
            self.gallery_count.configure(text=f"{self.gallery_index + 1} / {count} · {item.gallery_error}")

    def turn_image(self, delta: int) -> None:
        if self.selected and self.selected.preview_urls:
            self.gallery_index = (self.gallery_index + delta) % len(self.selected.preview_urls)
            self._show_image()

    def do_import(self, only=None) -> None:
        from server_gui import workshop_import, workshop_public

        if self.busy:
            return
        chosen = [only] if only and only.ok else [item for item, variable in self.vars if variable.get() and item.ok]
        if not chosen:
            self.status.configure(text="Tick at least one map.", text_color=theme.WARN)
            return
        self.cancel = threading.Event()
        cancel = self.cancel
        self.installing = True
        self._set_busy(True)
        self.status.configure(text="Installing selected maps...", text_color=theme.MUTED)

        def progress(message: str) -> None:
            def show() -> None:
                if self.winfo_exists() and self.busy and not cancel.is_set():
                    self.status.configure(text=message, text_color=theme.MUTED)
            self.app.call_soon(show)

        def work() -> list[workshop_import.ImportResult]:
            results = []
            for item in chosen:
                if cancel.is_set():
                    break
                result = (workshop_public.download_item(item, self.maps_dir, cancel, progress)
                          if item.download_url else workshop_import.import_item(item, self.maps_dir))
                results.append(result)
            return results

        def done(results) -> None:
            if not self.winfo_exists():
                return
            self.installing = False
            self.catalog_cache.clear()
            self._set_busy(False)
            if isinstance(results, Exception):
                self.status.configure(text=f"Import failed: {results}", text_color=theme.DANGER)
                if self.close_after_work:
                    self.destroy()
                return
            good = [r for r in results if r.ok]
            bad = [r for r in results if not r.ok]
            self.imported.extend(r.stem for r in good if r.stem not in self.imported)
            for result in results:
                self.app.console_page.append_local(
                    f"Workshop {result.item.published_id} ({result.item.display_title}): {result.message}",
                    "INFO" if result.ok else "WARNING")
            message = f"Imported {len(good)} map(s)."
            if cancel.is_set():
                message += " Cancelled remaining downloads."
            if bad:
                message += " Failed: " + "; ".join(f"{r.item.display_title}: {r.message}" for r in bad)
            self.pending_message = (message, theme.OK if not bad else theme.WARN)
            if self.close_after_work:
                self.destroy()
                return
            # Keep the same results and selection page; no second network request.
            self._show(workshop_public.WorkshopPage(self.items, self.more) if self.public else self.items)

        self.app.background(work, done)


# --------------------------------------------------------------------------
# Network tab


class NetworkPage(ctk.CTkScrollableFrame):
    def __init__(self, master, app: App) -> None:
        super().__init__(master, fg_color="transparent", scrollbar_button_color=theme.BORDER)
        self.app = app
        self.grid_columnconfigure((0, 1), weight=1, uniform="net")
        self.port_var = tk.StringVar()
        self.browser_var = tk.BooleanVar()
        self.lan_var = tk.BooleanVar(value=True)
        self.forward_var = tk.BooleanVar(value=app.state_data.auto_forward)
        self._loading = False

        # Port
        card = Card(self, "Port", "Players connect to this UDP port.")
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 7), pady=(0, 12))
        body = card.body
        field_label(body, "Game port", 0)
        row = ctk.CTkFrame(body, fg_color="transparent")
        row.grid(row=0, column=1, sticky="w", pady=(8, 0))
        self.port_entry = entry(row, self.port_var, width=100)
        self.port_entry.grid(row=0, column=0)
        quiet_button(row, f"Use {host_settings.RETAIL_GAME_PORT}", lambda: self.port_var.set(str(host_settings.RETAIL_GAME_PORT)),
                     width=90).grid(row=0, column=1, padx=(8, 0))
        self.port_note = note(body, "", theme.FAINT, 11, 380)
        self.port_note.grid(row=1, column=1, sticky="w")
        field_label(body, "Query port", 2)
        self.query_label = note(body, "", theme.TEXT, 13, 380)
        self.query_label.grid(row=2, column=1, sticky="w", pady=(8, 0))
        field_label(body, "Addresses", 3)
        self.address_label = note(body, "Detecting...", theme.TEXT, 13, 380)
        self.address_label.grid(row=3, column=1, sticky="w", pady=(8, 0))
        self.port_var.trace_add("write", lambda *_: self.on_port_change())

        # Steam browser
        card = Card(self, "Server discovery", "Let players find the server on their network or on Steam.")
        card.grid(row=0, column=1, sticky="nsew", padx=(7, 0), pady=(0, 12))
        body = card.body
        body.grid_columnconfigure(0, weight=1)
        switch(body, "Steam LAN discovery (A2S)", self.lan_var, command=self.on_lan_change).grid(
            row=0, column=0, sticky="w")
        self.lan_note = note(body, "", theme.FAINT, 11, 420)
        self.lan_note.grid(row=1, column=0, sticky="w", pady=(4, 12))
        self.browser_switch = switch(body, "Public Steam server listing", self.browser_var, command=self.on_browser_change)
        self.browser_switch.grid(row=2, column=0, sticky="w")
        note(body, "Needs the ports forwarded and the original Steam runtime (steam_api.dll) beside the server. "
                   "Steam P2P on the Host tab works without either.", theme.FAINT, 11, 420).grid(row=3, column=0, sticky="w", pady=(4, 6))
        self.browser_notes = ctk.CTkFrame(body, fg_color="transparent")
        self.browser_notes.grid(row=4, column=0, sticky="ew")

        # Forwarding
        card = Card(self, "Automatic port forwarding", "Ask your router to open the ports while the server runs (UPnP or NAT-PMP).")
        card.grid(row=1, column=0, sticky="nsew", padx=(0, 7), pady=(0, 12))
        body = card.body
        body.grid_columnconfigure(0, weight=1)
        switch(body, "Forward ports automatically when the server starts", self.forward_var,
               command=self.on_forward_toggle).grid(row=0, column=0, sticky="w", columnspan=2)
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.grid(row=1, column=0, sticky="w", pady=(10, 4))
        accent_button(buttons, "Forward now", app.forward_ports, width=120).pack(side="left")
        quiet_button(buttons, "Remove", app.unforward_ports, width=90).pack(side="left", padx=8)
        self.forward_status = note(body, "Off. Mappings are removed again when the server stops.", theme.FAINT, 12, 420)
        self.forward_status.grid(row=2, column=0, sticky="w")

        # Firewall
        card = Card(self, "Firewall", "Let the operating system accept players on these ports.")
        card.grid(row=1, column=1, sticky="nsew", padx=(7, 0), pady=(0, 12))
        body = card.body
        body.grid_columnconfigure(0, weight=1)
        if sys.platform == "win32":
            buttons = ctk.CTkFrame(body, fg_color="transparent")
            buttons.grid(row=0, column=0, sticky="w")
            accent_button(buttons, "Add firewall rule", self.add_firewall_rule, width=150).pack(side="left")
            quiet_button(buttons, "Copy command", self.copy_firewall, width=120).pack(side="left", padx=8)
            note(body, "Windows asks for permission once, then adds an inbound UDP rule for the server program.",
                 theme.FAINT, 11, 420).grid(row=1, column=0, sticky="w", pady=(6, 0))
        else:
            quiet_button(body, "Copy commands", self.copy_firewall, width=130).grid(row=0, column=0, sticky="w")
        self.firewall_text = ctk.CTkTextbox(body, height=96, fg_color=theme.PANEL_ALT, text_color=theme.TEXT,
                                            font=mono_font(11), wrap="word", border_width=0)
        self.firewall_text.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.firewall_status = note(body, "", theme.FAINT, 12, 420)
        self.firewall_status.grid(row=3, column=0, sticky="w", pady=(4, 0))

        # Check
        card = Card(self, "Check connection", "What can be measured from here, honestly reported.")
        card.grid(row=2, column=0, sticky="nsew", padx=(0, 7), pady=(0, 12))
        body = card.body
        body.grid_columnconfigure(0, weight=1)
        self.check_button = accent_button(body, "Check now", self.run_check, width=120)
        self.check_button.grid(row=0, column=0, sticky="w")
        self.findings = ctk.CTkFrame(body, fg_color="transparent")
        self.findings.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        # Guide
        card = Card(self, "Port forwarding guide", "Only needed for direct internet play without Steam P2P.")
        card.grid(row=2, column=1, sticky="nsew", padx=(7, 0), pady=(0, 12))
        body = card.body
        body.grid_columnconfigure(0, weight=1)
        self.guide = note(body, "", theme.TEXT, 12, 440)
        self.guide.grid(row=0, column=0, sticky="w")
        accent_button(body, "Open router guides (portforward.com)", lambda: webbrowser.open(PORTFORWARD_GUIDE),
                      width=280).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.refresh_from_doc()

    def refresh_from_doc(self) -> None:
        doc = self.app.doc
        if doc is None:
            return
        self._loading = True
        try:
            port = str(doc.get("server", "port", host_settings.RETAIL_GAME_PORT))
            if self.port_var.get() != port:
                self.port_var.set(port)
            self.browser_var.set(bool(doc.get("steam", "enabled", False)) and bool(doc.get("steam", "public", True)))
            self.lan_var.set(bool(doc.get("server", "lan_discovery", True)))
        finally:
            self._loading = False
        self._refresh_texts()

    def on_show(self) -> None:
        self.refresh_from_doc()

    def _refresh_texts(self) -> None:
        doc = self.app.doc
        port = int(doc.get("server", "port", host_settings.RETAIL_GAME_PORT))
        query = host_settings.query_port(doc)
        explicit = int(doc.get("steam", "query_port", 0) or 0)
        query_text = f"A2S on game UDP {port}"
        if self.lan_var.get() and port not in host_settings.STEAM_LAN_PORTS:
            query_text += "\nLAN: first free UDP 27015-27020 (see server log)"
        if doc.get("steam", "enabled", False):
            query_text += f"\nSteam listing: UDP {query}" + (" (game port + 1)" if not explicit else "")
        self.query_label.configure(text=query_text)
        self.lan_note.configure(text=host_settings.lan_discovery_note(doc))
        if port == host_settings.RETAIL_GAME_PORT:
            self.port_note.configure(text="The original game's port: rows in the stock server browser connect here.", text_color=theme.FAINT)
        else:
            self.port_note.configure(text=f"The stock Steam browser always connects to {host_settings.RETAIL_GAME_PORT}; "
                                          "other ports work for direct and BattleSpades-client joins.", text_color=theme.WARN)
        for child in self.browser_notes.winfo_children():
            child.destroy()
        if self.browser_var.get():
            for index, (level, text) in enumerate(host_settings.steam_browser_readiness(self.app.paths.root, doc)):
                note(self.browser_notes, f"{theme.FINDING_MARKS[level]}  {text}", theme.FINDING_COLOURS[level], 12, 420).grid(
                    row=index, column=0, sticky="w", pady=1)
        ports = host_settings.server_ports(doc)
        joined = ", ".join(map(str, ports))
        self.firewall_text.configure(state="normal")
        self.firewall_text.delete("1.0", "end")
        self.firewall_text.insert("1.0", firewall.instructions(sys.platform, self.app.paths.firewall_program(), host_settings.firewall_ports(doc)))
        self.firewall_text.configure(state="disabled")
        lan = self.app.lan_ip or "this computer's LAN address"
        self.guide.configure(text=(
            "1. Open your router's admin page (usually http://192.168.0.1 or http://192.168.1.1).\n"
            "2. Find Port Forwarding (also called Virtual Server or NAT).\n"
            f"3. Forward UDP {joined} to {lan}.\n"
            "4. Save, start the server, then use Check connection.\n"
            "Give this computer a fixed (reserved) LAN address so the rule keeps working."))
        self.refresh_addresses()

    def refresh_addresses(self) -> None:
        port = self.port_var.get()
        public = self.app.public_ip or "unknown"
        lan = self.app.lan_ip or "unknown"
        self.address_label.configure(text=f"Public {public}:{port}\nLAN     {lan}:{port}")

    def on_port_change(self) -> None:
        if self._loading or self.app.doc is None:
            return
        text = self.port_var.get().strip()
        try:
            port = int(text)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self.port_entry.configure(border_color=theme.DANGER)
            self.port_note.configure(text="Enter a number between 1 and 65535.", text_color=theme.DANGER)
            return
        self.port_entry.configure(border_color=theme.BORDER)
        self.app.doc.set("server", "port", port)
        self.app.doc_changed("network")
        self._refresh_texts()

    def on_lan_change(self) -> None:
        """Persist LAN discovery independently of public Steam registration."""
        if self._loading or self.app.doc is None:
            return
        self.app.doc.set("server", "lan_discovery", bool(self.lan_var.get()))
        self.app.doc_changed("network")
        self._refresh_texts()

    def on_browser_change(self) -> None:
        if self._loading or self.app.doc is None:
            return
        enabled = bool(self.browser_var.get())
        self.app.doc.set("steam", "enabled", enabled)
        if enabled:
            self.app.doc.set("steam", "public", True)
        self.app.doc_changed("network")
        self._refresh_texts()

    def on_forward_toggle(self) -> None:
        self.app.state_data.auto_forward = bool(self.forward_var.get())
        self.app.state_data.save(self.app.paths.gui_state)
        if self.forward_var.get() and self.app.server is not None and not self.app.forwarder.mapped:
            self.app.forward_ports()
        elif not self.forward_var.get():
            self.set_forward_status("Off. Mappings are removed again when the server stops.", theme.FAINT)

    def set_forward_status(self, text: str, colour: str) -> None:
        self.forward_status.configure(text=text, text_color=colour)

    def copy_firewall(self) -> None:
        text = firewall.instructions(sys.platform, self.app.paths.firewall_program(), host_settings.firewall_ports(self.app.doc))
        self.app.clipboard_clear()
        self.app.clipboard_append(text)
        self.app.footer("Firewall instructions copied.", theme.OK)

    def add_firewall_rule(self) -> None:
        ports = host_settings.firewall_ports(self.app.doc)
        program = self.app.paths.firewall_program()
        self.firewall_status.configure(text="Waiting for the Windows permission prompt...", text_color=theme.MUTED)

        def done(result) -> None:
            if isinstance(result, Exception):
                result = firewall.FirewallResult(False, str(result))
            self.firewall_status.configure(text=result.message, text_color=theme.OK if result.ok else theme.WARN)

        self.app.background(lambda: firewall.add_windows_rule(program, ports), done)

    def run_check(self) -> None:
        app = self.app
        port = int(app.doc.get("server", "port", host_settings.RETAIL_GAME_PORT))
        running = app.server_state == "running"
        self.check_button.configure(state="disabled", text="Checking...")

        def work():
            lan = network.local_ip()
            public = None if getattr(app, "offline", False) else network.public_ip()
            router = None
            if app.forwarder.client is not None:
                try:
                    router = app.forwarder.client.external_ip()
                except network.UPnPError:
                    router = None
            else:
                try:
                    router = network.UPnPClient.discover(timeout=2.0).external_ip()
                except network.UPnPError:
                    router = None
            local_answer = network.a2s_probe("127.0.0.1", port) if running else None
            hairpin = network.a2s_probe(public, port) if (running and public and local_answer) else None
            return network.assess(port=port, server_running=running, local_answer=local_answer, lan_ip=lan,
                                  public=public, router_external=router, forwarding=app.forward_result,
                                  hairpin_answer=hairpin), public, lan

        def done(result) -> None:
            self.check_button.configure(state="normal", text="Check now")
            for child in self.findings.winfo_children():
                child.destroy()
            if isinstance(result, Exception):
                note(self.findings, f"Check failed: {result}", theme.WARN).grid(row=0, column=0, sticky="w")
                return
            findings, public, lan = result
            app.public_ip, app.lan_ip = public or app.public_ip, lan or app.lan_ip
            self.refresh_addresses()
            for index, finding in enumerate(findings):
                note(self.findings, f"{theme.FINDING_MARKS[finding.level]}  {finding.text}",
                     theme.FINDING_COLOURS[finding.level], 12, 440).grid(row=index, column=0, sticky="w", pady=2)

        app.background(work, done)


# --------------------------------------------------------------------------
# Console tab


class ConsolePage(ctk.CTkFrame):
    def __init__(self, master, app: App) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.lines: deque[logparse.LogLine] = deque(maxlen=MAX_LOG_LINES)
        self.last_level = "INFO"
        self.history: list[str] = []
        self.history_index = 0
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(self, fg_color=theme.PANEL, corner_radius=10)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.level_var = tk.StringVar(value=app.state_data.console_level if app.state_data.console_level in logparse.LEVELS else "INFO")
        self.autoscroll_var = tk.BooleanVar(value=app.state_data.console_autoscroll)
        ctk.CTkLabel(bar, text="Show", font=body_font(12), text_color=theme.MUTED).pack(side="left", padx=(12, 6), pady=8)
        option_menu(bar, logparse.LEVELS, self.level_var, command=lambda _v: self.rerender(), width=110).pack(side="left")
        self.search_entry = entry(bar, placeholder_text="Search the log", width=220)
        self.search_entry.pack(side="left", padx=10)
        self.search_entry.bind("<KeyRelease>", lambda _e: self.after_idle(self.rerender))
        switch(bar, "Auto-scroll", self.autoscroll_var, command=self._save_prefs).pack(side="left", padx=6)
        quiet_button(bar, "Clear", self.clear, width=70).pack(side="right", padx=(0, 12))
        quiet_button(bar, "Save...", self.save, width=70).pack(side="right", padx=6)
        quiet_button(bar, "Copy", self.copy, width=70).pack(side="right")

        self.text = ctk.CTkTextbox(self, fg_color="#0f110b", text_color=theme.TEXT, font=mono_font(12), wrap="none",
                                   corner_radius=10, border_width=1, border_color=theme.BORDER)
        self.text.grid(row=1, column=0, sticky="nsew")
        for level, colour in theme.LEVEL_COLOURS.items():
            self.text.tag_config(level, foreground=colour)
        self.text.configure(state="disabled")

        palette = ctk.CTkScrollableFrame(self, fg_color=theme.PANEL, width=230, corner_radius=10,
                                         scrollbar_button_color=theme.BORDER)
        palette.grid(row=1, column=1, sticky="ns", padx=(10, 0))
        ctk.CTkLabel(palette, text="QUICK COMMANDS", font=heading_font(17), text_color=theme.KHAKI).pack(anchor="w", padx=8, pady=(6, 2))
        category = None
        self.palette_buttons: list[ctk.CTkButton] = []
        for item in console.PALETTE:
            if item.category != category:
                category = item.category
                ctk.CTkLabel(palette, text=category.upper(), font=body_font(11, "bold"), text_color=theme.OLIVE_BRIGHT).pack(
                    anchor="w", padx=8, pady=(10, 2))
            button = quiet_button(palette, item.label, lambda i=item: self.use_palette(i), height=28, anchor="w")
            button.pack(fill="x", padx=6, pady=2)
            self.palette_buttons.append(button)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(row, text="/", font=heading_font(22), text_color=theme.OLIVE_BRIGHT).grid(row=0, column=0, padx=(4, 6))
        self.command_entry = entry(row, placeholder_text="Type a command, e.g.  say Welcome!   or   kick Name   (Up/Down: history)",
                                   font=mono_font(13), height=36)
        self.command_entry.grid(row=0, column=1, sticky="ew")
        self.command_entry.bind("<Return>", lambda _e: self.send())
        self.command_entry.bind("<Up>", lambda _e: self._history(-1))
        self.command_entry.bind("<Down>", lambda _e: self._history(1))
        self.send_button = accent_button(row, "Send", self.send, width=90, height=36)
        self.send_button.grid(row=0, column=2, padx=(8, 0))
        self.hint = note(row, "", theme.FAINT, 11, 900)
        self.hint.grid(row=1, column=1, sticky="w")
        self.refresh_state()

    def refresh_state(self) -> None:
        running = self.app.server_state == "running"
        state = "normal" if running else "disabled"
        self.send_button.configure(state=state)
        self.hint.configure(text="Commands run as the server operator (admin)." if running else
                            "Start the server to send commands. Replies appear in the log above.")

    # ---- log --------------------------------------------------------------
    def append_server(self, raw: str) -> logparse.LogLine:
        line = logparse.parse(raw, self.last_level)
        if line.level != "OUTPUT":
            self.last_level = line.level
        if line.logger == "BattleSpades.console":
            line = logparse.LogLine(line.text, "COMMAND" if line.message.startswith(">") else line.level,
                                    line.logger, line.message, line.time)
        self._add(line)
        return line

    def append_local(self, text: str, level: str = "INFO") -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._add(logparse.LogLine(f"{stamp} [window] {text}", level, "window", text, stamp))

    def _add(self, line: logparse.LogLine) -> None:
        dropped = len(self.lines) == self.lines.maxlen
        self.lines.append(line)
        if dropped:
            self.text.configure(state="normal")
            self.text.delete("1.0", "2.0")
            self.text.configure(state="disabled")
        if self._visible(line):
            self._insert(line)

    def _visible(self, line: logparse.LogLine) -> bool:
        level = line.level if line.level != "COMMAND" else "WARNING"
        return logparse.matches(logparse.LogLine(line.text, level), min_level=self.level_var.get(),
                                search=self.search_entry.get().strip())

    def _insert(self, line: logparse.LogLine) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", line.text + "\n", line.level)
        self.text.configure(state="disabled")
        if self.autoscroll_var.get():
            self.text.see("end")

    def rerender(self) -> None:
        self._save_prefs()
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for line in self.lines:
            if self._visible(line):
                self.text.insert("end", line.text + "\n", line.level)
        self.text.configure(state="disabled")
        self.text.see("end")

    def _save_prefs(self) -> None:
        self.app.state_data.console_level = self.level_var.get()
        self.app.state_data.console_autoscroll = bool(self.autoscroll_var.get())

    def visible_text(self) -> str:
        return "\n".join(line.text for line in self.lines if self._visible(line))

    def copy(self) -> None:
        self.app.clipboard_clear()
        self.app.clipboard_append(self.visible_text())
        self.app.footer("Log copied to the clipboard.", theme.OK)

    def save(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.app, defaultextension=".log", initialfile="battlespades-console.log",
                                            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        Path(path).write_text("\n".join(line.text for line in self.lines) + "\n", encoding="utf-8")
        self.app.footer(f"Log saved to {path}.", theme.OK)

    def clear(self) -> None:
        self.lines.clear()
        self.rerender()

    # ---- commands ---------------------------------------------------------
    def send(self) -> None:
        text = self.command_entry.get().strip().lstrip("/")
        problem = console.validate_command(text)
        if problem:
            self.hint.configure(text=problem, text_color=theme.WARN)
            return
        if self.app.server is None or not self.app.server.send_command(text):
            self.hint.configure(text="The server is not running.", text_color=theme.WARN)
            return
        self.history.append(text)
        self.history_index = len(self.history)
        self.set_command("")
        self.hint.configure(text="Sent. The reply appears in the log.", text_color=theme.FAINT)

    def _history(self, step: int) -> str:
        if not self.history:
            return "break"
        self.history_index = max(0, min(len(self.history), self.history_index + step))
        self.set_command(self.history[self.history_index] if self.history_index < len(self.history) else "")
        return "break"

    def set_command(self, text: str) -> None:
        self.command_entry.delete(0, "end")
        if text:
            self.command_entry.insert(0, text)
            self.command_entry.icursor("end")

    def use_palette(self, item: console.PaletteEntry) -> None:
        if not item.fields:
            self.set_command(item.template)
            if self.app.server_state == "running":
                self.send()
            return
        dialog = PaletteDialog(self.app, item, self.app.status or {}, self.app.maps_dir())
        self.app.wait_window(dialog)
        if dialog.result:
            self.set_command(dialog.result)
            if self.app.server_state == "running":
                self.send()


class PaletteDialog(ctk.CTkToplevel):
    def __init__(self, master, item: console.PaletteEntry, status: dict, maps_dir: Path) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.title(item.label)
        self.result: str | None = None
        self.item = item
        self.transient(master)
        self.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self, text=item.label.upper(), font=heading_font(22), text_color=theme.KHAKI).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(14, 0))
        note(self, item.help, theme.MUTED, 12, 380).grid(row=1, column=0, columnspan=2, sticky="w", padx=16, pady=(0, 8))
        players = [p.get("name", "") for p in status.get("player_list", []) if not p.get("bot")]
        bots = [p.get("name", "") for p in status.get("player_list", []) if p.get("bot")]
        suggestions = {
            "player": players + bots,
            "map": catalog.available_maps(maps_dir),
            "mode": list(catalog.REGISTERED_MODE_CODES),
            "level": [code for code, _ in catalog.DIFFICULTIES],
            "duration": ["30m", "2h", "1d", "perma"],
            "count": ["1", "2", "4", "8", "all"] if item.command == "bots" and "remove" in item.template else ["1", "2", "4", "8"],
        }
        self.vars: dict[str, tk.StringVar] = {}
        for index, name in enumerate(item.fields, start=2):
            ctk.CTkLabel(self, text=name.capitalize(), font=body_font(13, "bold"), text_color=theme.TEXT).grid(
                row=index, column=0, sticky="w", padx=(16, 10), pady=4)
            variable = tk.StringVar()
            options = suggestions.get(name)
            if options:
                widget = ctk.CTkComboBox(self, values=options, variable=variable, fg_color=theme.PANEL_ALT,
                                         border_color=theme.BORDER, button_color=theme.BORDER, text_color=theme.TEXT,
                                         dropdown_fg_color=theme.PANEL_ALT, font=body_font(13), width=260)
                variable.set("")
            else:
                widget = entry(self, variable, width=260)
            widget.grid(row=index, column=1, sticky="ew", padx=(0, 16), pady=4)
            self.vars[name] = variable
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=len(item.fields) + 2, column=0, columnspan=2, sticky="e", padx=16, pady=14)
        quiet_button(buttons, "Cancel", self.destroy, width=90).pack(side="left", padx=8)
        accent_button(buttons, "Send", self._ok, width=100).pack(side="left")
        self.bind("<Return>", lambda _e: self._ok())
        self.after(100, self.grab_set)

    def _ok(self) -> None:
        values = {name: variable.get() for name, variable in self.vars.items()}
        if not values.get(self.item.fields[0], "").strip():
            return
        self.result = console.fill(self.item, values)
        self.destroy()


# --------------------------------------------------------------------------
# Advanced tab


class AdvancedPage(ctk.CTkFrame):
    def __init__(self, master, app: App) -> None:
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.specs = app.doc.describe() if app.doc is not None else []
        self.current = self.specs[0].name if self.specs else ""
        self.rendered_version = -1
        self.rendered_section = ""
        self.errors: dict[tuple[str, str], str] = {}
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self, fg_color=theme.PANEL, corner_radius=10)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.widget_vars: list[tk.Variable] = []
        self.search_entry = entry(top, placeholder_text="Find a setting", width=240)
        self.search_entry.pack(side="left", padx=12, pady=8)
        self.search_entry.bind("<KeyRelease>", lambda _e: self.after_idle(self.render))
        note(top, RESTART_NOTE + " Values are checked with the server's own loader before saving.", theme.MUTED, 12, 520).pack(side="left")
        quiet_button(top, "Run full check", self.run_check, width=120).pack(side="right", padx=(0, 12))
        quiet_button(top, "Revert", app.revert_settings, width=80).pack(side="right", padx=6)
        self.restore_button = quiet_button(top, "Restore section defaults", self.restore_defaults, width=180)
        self.restore_button.pack(side="right")

        sections = ctk.CTkScrollableFrame(self, fg_color=theme.PANEL, width=190, corner_radius=10,
                                          scrollbar_button_color=theme.BORDER)
        sections.grid(row=1, column=0, sticky="ns", padx=(0, 10))
        self.section_buttons: dict[str, ctk.CTkButton] = {}
        for spec in self.specs:
            button = ctk.CTkButton(sections, text=spec.title, anchor="w", height=28, corner_radius=6, font=body_font(12),
                                   fg_color="transparent", hover_color=theme.PANEL_ALT, text_color=theme.TEXT,
                                   command=lambda n=spec.name: self.select(n))
            button.pack(fill="x", padx=4, pady=1)
            self.section_buttons[spec.name] = button
        self.body = ctk.CTkScrollableFrame(self, fg_color=theme.PANEL, corner_radius=10, scrollbar_button_color=theme.BORDER)
        self.body.grid(row=1, column=1, sticky="nsew")
        self.body.grid_columnconfigure(1, weight=1)

    def invalidate(self) -> None:
        # Stacked pages are all "mapped"; only re-render the visible one.
        self.rendered_version = -1
        if self.app.state_data.tab == "Advanced":
            self.render()

    def on_show(self) -> None:
        if self.rendered_version != self.app.doc_version or self.rendered_section != self.current:
            self.render()

    def select(self, name: str) -> None:
        self.current = name
        self.search_entry.delete(0, "end")
        self.render()
        self.body._parent_canvas.yview_moveto(0)

    def _keys_to_show(self) -> list[tuple[Any, KeySpec]]:
        query = self.search_entry.get().strip().casefold()
        if query:
            found = []
            for section in self.specs:
                for spec in section.keys:
                    haystack = f"{section.name}.{spec.key} {spec.help_text}".casefold()
                    if query in haystack:
                        found.append((section, spec))
            return found[:80]
        section = next((s for s in self.specs if s.name == self.current), None)
        return [(section, spec) for spec in section.keys] if section else []

    def render(self) -> None:
        if self.app.doc is None:
            return
        self.specs = self.app.doc.describe()
        for name, button in self.section_buttons.items():
            button.configure(fg_color=theme.OLIVE if name == self.current else "transparent")
        for child in self.body.winfo_children():
            child.destroy()
        self.widget_vars = []
        rows = self._keys_to_show()
        searching = bool(self.search_entry.get().strip())
        row = 0
        if not searching:
            section = next((s for s in self.specs if s.name == self.current), None)
            if section is not None:
                ctk.CTkLabel(self.body, text=section.title.upper(), font=heading_font(22), text_color=theme.KHAKI,
                             anchor="w").grid(row=row, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 0))
                row += 1
                if section.description:
                    note(self.body, section.description, theme.MUTED, 12, 760).grid(row=row, column=0, columnspan=3, sticky="w", padx=14)
                    row += 1
        elif not rows:
            note(self.body, "No setting matches.", theme.MUTED).grid(row=0, column=0, padx=14, pady=14)
        for section, spec in rows:
            row = self._row(row, spec, show_section=searching)
        self.rendered_version = self.app.doc_version
        self.rendered_section = self.current
        self.restore_button.configure(state="normal" if (self.app.defaults is not None and not searching) else "disabled")

    def _row(self, row: int, spec: KeySpec, *, show_section: bool) -> int:
        doc = self.app.doc
        if spec.description:
            note(self.body, spec.description, theme.FAINT, 11, 760).grid(row=row, column=0, columnspan=3, sticky="w", padx=14, pady=(10, 0))
            row += 1
        present = doc.has(spec.section, spec.key)
        name = f"{spec.section}.{spec.key}" if show_section else spec.key
        changed = self.app.defaults is not None and self.app.defaults.has(spec.section, spec.key) and \
            self.app.defaults.get(spec.section, spec.key) != doc.get(spec.section, spec.key)
        label = ctk.CTkLabel(self.body, text=("● " if changed else "") + name, font=mono_font(12),
                             text_color=theme.OLIVE_BRIGHT if changed else theme.TEXT, anchor="w")
        label.grid(row=row, column=0, sticky="nw", padx=(14, 10), pady=(6, 0))
        holder = ctk.CTkFrame(self.body, fg_color="transparent")
        holder.grid(row=row, column=1, sticky="ew", pady=(4, 0))
        holder.grid_columnconfigure(0, weight=1)
        value = doc.get(spec.section, spec.key, spec.example)
        widget_enabled = present or not spec.optional
        self._widget(holder, spec, value, widget_enabled)
        if spec.optional:
            variable = tk.BooleanVar(value=present)
            self.widget_vars.append(variable)
            ctk.CTkCheckBox(self.body, text="set", variable=variable, width=50, font=body_font(11), text_color=theme.MUTED,
                            fg_color=theme.OLIVE, hover_color=theme.OLIVE_HOVER, border_color=theme.BORDER,
                            command=lambda s=spec, v=variable: self._toggle_optional(s, v)).grid(row=row, column=2, padx=10)
        if spec.inline:
            row += 1
            note(self.body, spec.inline, theme.FAINT, 11, 560).grid(row=row, column=1, sticky="w")
        error = self.errors.get((spec.section, spec.key))
        if error:
            row += 1
            note(self.body, error, theme.DANGER, 11, 560).grid(row=row, column=1, sticky="w")
        return row + 1

    def _widget(self, holder, spec: KeySpec, value: Any, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        if spec.kind == "bool":
            variable = tk.BooleanVar(value=bool(value))
            self.widget_vars.append(variable)
            switch(holder, "", variable, command=lambda: self._commit(spec, bool(variable.get())), state=state).grid(row=0, column=0, sticky="w")
            return
        if spec.kind == "enum":
            labels = [label for label, _v in spec.choices]
            current = next((label for label, v in spec.choices if v == value and type(v) is type(value)), None)
            if current is None:
                current = next((label for label, v in spec.choices if v == value), labels[0] if labels else "")
            variable = tk.StringVar(value=current)
            self.widget_vars.append(variable)
            option_menu(holder, labels, variable, width=220, state=state,
                        command=lambda label: self._commit(spec, coerce(spec, label))).grid(row=0, column=0, sticky="w")
            return
        text = format_toml_value(value) if spec.kind in ("list", "table") else ("" if value is None else str(value))
        variable = tk.StringVar(value=text)
        self.widget_vars.append(variable)
        width = 190 if spec.kind in ("int", "float") else 420
        widget = entry(holder, variable, width=width, state=state,
                       font=mono_font(12) if spec.kind in ("list", "table") else body_font(13))
        widget.grid(row=0, column=0, sticky="w")

        def commit(_event=None) -> None:
            try:
                parsed = coerce(spec, variable.get())
            except ValueError as exc:
                widget.configure(border_color=theme.DANGER)
                self.errors[(spec.section, spec.key)] = str(exc)
                self.app.footer(str(exc), theme.DANGER)
                return
            widget.configure(border_color=theme.BORDER)
            had_error = self.errors.pop((spec.section, spec.key), None)
            if parsed != self.app.doc.get(spec.section, spec.key) or had_error:
                self._commit(spec, parsed)

        widget.bind("<Return>", commit)
        widget.bind("<FocusOut>", commit)

    def _commit(self, spec: KeySpec, value: Any) -> None:
        self.app.doc.set(spec.section, spec.key, value)
        self.app.host_page.load_from_doc()
        self.app.doc_changed("advanced")

    def _toggle_optional(self, spec: KeySpec, variable: tk.BooleanVar) -> None:
        if variable.get():
            self.app.doc.set(spec.section, spec.key, spec.example)
        else:
            self.app.doc.remove(spec.section, spec.key)
        self.app.doc_changed("advanced")
        self.render()

    def restore_defaults(self) -> None:
        if self.app.defaults is None or self.app.doc is None:
            return
        if not messagebox.askyesno(APP_TITLE, f"Restore every [{self.current}] setting to the shipped default?\n\n"
                                              "Nothing is written until you press Save settings."):
            return
        changed = self.app.doc.restore_section(self.current, self.app.defaults)
        self.app.host_page.load_from_doc()
        self.app.doc_changed("advanced")
        self.render()
        self.app.footer(f"[{self.current}]: {len(changed)} value(s) restored to defaults (not saved yet).")

    def run_check(self) -> None:
        """Run the console server's own ``--check`` (config, assets, natives)."""

        app = self.app
        if app.doc is not None and app.doc.dirty:
            messagebox.showinfo(APP_TITLE, "Save your settings first: the full check reads config.toml from disk.")
            return
        app.show_tab("Console")
        app.console_page.append_local("Running the full server check (--check)...", "COMMAND")

        def work():
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            completed = subprocess.run(app.paths.server_command() + ["--check"], cwd=str(app.paths.root),
                                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                                       timeout=300, creationflags=flags)
            return completed.returncode, (completed.stdout or "") + (completed.stderr or "")

        def done(result) -> None:
            if isinstance(result, Exception):
                app.console_page.append_local(f"Check could not run: {result}", "ERROR")
                return
            code, output = result
            for line in output.splitlines():
                app.console_page.append_local(line, "INFO" if code == 0 else "WARNING")
            app.console_page.append_local("Check passed." if code == 0 else f"Check failed (exit code {code}).",
                                          "COMMAND" if code == 0 else "ERROR")

        app.background(work, done)


# --------------------------------------------------------------------------
# Entry point


def _read_version(root: Path) -> str:
    try:
        return (Path(root) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"


def _warm_up_validator() -> None:
    """Import the server's config loader in the background (first save is quick)."""

    try:
        from server.config import load_config  # noqa: F401
        import modes  # noqa: F401
    except Exception:
        pass


def _configure_scaling(root: tk.Misc) -> None:
    """CustomTkinter scales on Windows/macOS; follow the X11 DPI on Linux."""

    if sys.platform.startswith("linux"):
        try:
            factor = float(root.winfo_fpixels("1i")) / 96.0
        except tk.TclError:
            factor = 1.0
        if factor > 1.15:
            ctk.set_widget_scaling(min(factor, 3.0))
            ctk.set_window_scaling(min(factor, 3.0))


def self_check(paths: host_paths.HostPaths) -> list[str]:
    """Problems a packaged window would hit, found without opening one."""

    problems = []
    import _tkinter  # noqa: F401 - proves Tcl/Tk was bundled

    for asset in ("assets/battlespades-server.png", "fonts/OFL.txt", "fonts/BarlowCondensed-Bold.ttf"):
        if not (Path(__file__).resolve().parent / asset).is_file():
            problems.append(f"missing bundled file server_gui/{asset}")
    if not Path(ctk.__file__).resolve().parent.joinpath("assets", "themes", "green.json").is_file():
        problems.append("missing CustomTkinter theme data")
    if host_paths.is_frozen() and not paths.server_executable.is_file():
        problems.append(f"missing server executable {paths.server_executable}")
    return problems


def main(argv=None) -> int:
    import argparse
    from server.network_options import add_network_arguments

    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="BattleSpadesServer", description="BattleSpades desktop server host")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--self-check", action="store_true")
    add_network_arguments(parser)
    try:
        options = parser.parse_args(arguments)
    except SystemExit as exc:
        return int(exc.code or 0)
    paths = host_paths.discover()
    if options.version:
        print(f"BattleSpadesServer {_read_version(paths.root)}")
        return 0
    if options.self_check:
        problems = self_check(paths)
        for problem in problems:
            print(problem, file=sys.stderr)
        return 1 if problems else 0
    lock = SingleInstance(paths.lock_file)
    if not lock.acquire():
        probe = tk.Tk()
        probe.withdraw()
        messagebox.showinfo(APP_TITLE, "BattleSpades Server is already open. Look for its window on the taskbar.")
        probe.destroy()
        return 1
    try:
        state = GuiState.load(paths.gui_state)
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("green")
        theme.register_fonts()
        _detect_heading_font()
        app = App(paths, state, lock, offline=options.offline, master_url=options.master_url)
        _configure_scaling(app)
        app.mainloop()
    finally:
        lock.release()
    return 0


def _detect_heading_font() -> bool:
    """Decide once whether the bundled heading family is usable."""

    global _HAS_HEADING
    probe = tk.Tk()
    probe.withdraw()
    try:
        from tkinter import font as tkfont

        _HAS_HEADING = theme.HEADING_FAMILY in set(tkfont.families(probe))
    finally:
        probe.destroy()
    return _HAS_HEADING
