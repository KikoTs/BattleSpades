"""The Network page keeps local discovery independent of public listing."""

from pathlib import Path
from types import SimpleNamespace
from collections.abc import Iterable
import sys
import tkinter as tk

import pytest

from server_gui.config_doc import ConfigDocument
from server_gui.gui_state import GuiState


def test_network_page_loads_old_config_and_persists_lan_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctk = pytest.importorskip("customtkinter")
    from server_gui.app import NetworkPage
    from server_gui import firewall

    requested_ports: list[tuple[int, ...]] = []
    instructions = firewall.instructions

    def record_instructions(platform: str, program: Path, ports: Iterable[int]) -> str:
        requested_ports.append(tuple(ports))
        return instructions(platform, program, requested_ports[-1])

    monkeypatch.setattr(firewall, "instructions", record_instructions)

    try:
        root = ctk.CTk()
    except tk.TclError:
        pytest.skip("No Tk display available")
    root.withdraw()
    root.doc = ConfigDocument("[server]\nport=32887\n")
    root.state_data = GuiState()
    root.paths = SimpleNamespace(root=tmp_path, firewall_program=lambda: tmp_path / "server.exe")
    root.lan_ip = root.public_ip = None
    root.forward_ports = root.unforward_ports = lambda: None
    changes = []
    root.doc_changed = changes.append
    try:
        page = NetworkPage(root, root)
        assert page.lan_var.get() is True
        assert page.browser_var.get() is False
        assert "27015-27020" in page.query_label.cget("text")
        assert 27015 in requested_ports[-1]
        if sys.platform == "darwin":
            # macOS allows the application, rather than individual UDP ports.
            assert "--unblockapp" in page.firewall_text.get("1.0", "end")
        else:
            assert "27015" in page.firewall_text.get("1.0", "end")
        assert "27015" not in page.guide.cget("text")  # no WAN forwarding
        page.lan_var.set(False)
        page.on_lan_change()
        assert changes == ["network"]
        root.doc = ConfigDocument(root.doc.text())
        page.refresh_from_doc()
        assert page.lan_var.get() is False
        assert not root.doc.get("steam", "enabled", False)
        assert 27015 not in requested_ports[-1]
        assert "27015" not in page.firewall_text.get("1.0", "end")
    finally:
        root.destroy()
