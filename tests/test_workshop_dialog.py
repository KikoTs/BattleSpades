"""Exercise real Workshop widgets without networking or a server process."""

from pathlib import Path
from types import SimpleNamespace
import tkinter as tk

import pytest

from server_gui import workshop_public as wp
from tests.test_workshop_public import details, fake_network


def test_dialog_download_refresh_cancel_and_close(monkeypatch, tmp_path: Path) -> None:
    ctk = pytest.importorskip("customtkinter")
    from server_gui.app import WorkshopDialog

    try:
        root = ctk.CTk()
    except tk.TclError:
        pytest.skip("No Tk display available")
    root.withdraw()
    root.paths = SimpleNamespace(state_dir=tmp_path / "state")
    jobs, messages, callbacks = [], [], []
    root.background = lambda work, done: jobs.append((work, done))
    root.call_soon = callbacks.append
    root.console_page = SimpleNamespace(append_local=lambda *args: messages.append(args))
    monkeypatch.setattr(WorkshopDialog, "grab_set", lambda self: None)
    fake_network(monkeypatch)
    from PIL import Image
    preview = tmp_path / "preview.png"
    Image.new("RGB", (640, 360), "#718e45").save(preview)
    item = wp.parse_details(details())
    item.preview_path = preview
    monkeypatch.setattr(wp, "browse", lambda *_args, **_kw: wp.WorkshopPage([item], True))
    monkeypatch.setattr(wp, "gallery", lambda *_args: [])
    try:
        dialog = WorkshopDialog(root, tmp_path)
        dialog.withdraw()
        assert dialog.busy and dialog.search_button.cget("state") == "normal"
        work, done = jobs.pop(0)
        done(work())
        assert not dialog.busy and len(dialog.vars) == 1
        assert len(dialog.preview_images) == 1
        assert dialog.preview_images[0].cget("size") == (120, 68)
        # Gallery work is separate from the already-visible catalog.
        assert len(jobs) == 1
        work, done = jobs.pop(0)
        done(work())
        while callbacks:
            callbacks.pop(0)()
        assert dialog.next_button.cget("state") == "normal"
        assert dialog.previous_button.cget("state") == "disabled"
        assert not dialog.vars[0][1].get()  # never download a whole public page by default
        dialog.vars[0][1].set(True)
        dialog.do_import()
        assert dialog.busy
        dialog.scan()
        assert len(jobs) == 1  # no overlapping import/refresh workers
        work, done = jobs.pop(0)
        done(work())
        while callbacks:
            callbacks.pop(0)()
        assert dialog.imported == ["Test_Map"]
        assert "Imported 1 map(s)" in dialog.status.cget("text")
        assert messages and list(tmp_path.glob("*.vxl"))
        dialog.vars[0][1].set(True)
        dialog.do_import()
        dialog.close()
        assert dialog.cancel.is_set() and dialog.winfo_exists()
        work, done = jobs.pop(0)
        done(work())
        assert not dialog.winfo_exists()
    finally:
        root.destroy()


def test_importing_maps_does_not_change_host_settings(monkeypatch, tmp_path: Path) -> None:
    pytest.importorskip("customtkinter")
    from server_gui import app

    monkeypatch.setattr(app, "WorkshopDialog", lambda *_args: SimpleNamespace(imported=["Paintball"]))
    refreshed = []
    host = SimpleNamespace(
        app=SimpleNamespace(maps_dir=lambda: tmp_path, wait_window=lambda _dialog: None,
                            footer=lambda *_args: None),
        mode_codes={"Team Deathmatch": "tdm"}, mode_var=SimpleNamespace(get=lambda: "Team Deathmatch"),
        map_display={"London": "London"}, map_var=SimpleNamespace(get=lambda: "London"),
        custom_rotation=["London"], _fill_maps=lambda *args: refreshed.append(args),
        on_change=lambda: pytest.fail("Downloading a map changed the configuration"),
    )
    app.HostPage.import_workshop(host)
    assert refreshed == [("tdm", "London")]
    assert host.custom_rotation == ["London"]


def test_catalog_is_visible_before_images_and_stale_results_are_ignored(monkeypatch, tmp_path: Path) -> None:
    ctk = pytest.importorskip("customtkinter")
    from server_gui.app import WorkshopDialog
    try:
        root = ctk.CTk()
    except tk.TclError:
        pytest.skip("No Tk display available")
    root.withdraw()
    root.paths = SimpleNamespace(state_dir=tmp_path)
    jobs, callbacks = [], []
    root.background = lambda work, done: jobs.append((work, done))
    root.call_soon = callbacks.append
    monkeypatch.setattr(WorkshopDialog, "grab_set", lambda _self: None)
    calls = []
    def browse(query, _page, _cancel, **filters):
        calls.append(filters)
        return wp.WorkshopPage([wp.parse_details(details(title=query or "old", preview_url="https://cdn.steamusercontent.com/p.jpg"))])
    monkeypatch.setattr(wp, "browse", browse)
    try:
        dialog = WorkshopDialog(root, tmp_path)
        dialog.withdraw()
        old_work, old_done = jobs.pop(0)
        dialog.query.insert(0, "new")
        dialog.sort.set("Newest")
        dialog.mode.set("CTF")
        dialog.search()
        work, done = jobs.pop(0)
        done(work())
        assert not dialog.busy and dialog.items[0].title == "new"
        assert len(jobs) == 2  # thumbnails + selected gallery, neither has run
        assert dialog.preview_labels[dialog.items[0].published_id].cget("text") == "LOADING"
        assert calls[-1] == {"sort": "mostrecent", "days": 7, "tag": "CTF"}
        old_done(old_work())
        assert dialog.items[0].title == "new"
        # A second visit uses cached metadata instead of another catalog job.
        jobs.clear()
        dialog.scan()
        assert not dialog.busy and len(calls) == 2
        dialog.destroy()
    finally:
        root.destroy()


def test_local_preview_with_original_dimensions_is_normalized_off_thread(monkeypatch, tmp_path: Path) -> None:
    ctk = pytest.importorskip("customtkinter")
    from PIL import Image
    from server_gui.app import WorkshopDialog

    try:
        root = ctk.CTk()
    except tk.TclError:
        pytest.skip("No Tk display available")
    root.withdraw()
    root.paths = SimpleNamespace(state_dir=tmp_path / "state")
    jobs, callbacks = [], []
    root.background = lambda work, done: jobs.append((work, done))
    root.call_soon = callbacks.append
    monkeypatch.setattr(WorkshopDialog, "grab_set", lambda _self: None)
    original = tmp_path / "source-preview.jpg"
    Image.new("RGB", (1024, 768), "#718e45").save(original)
    item = wp.parse_details(details())
    item.preview_path = original
    monkeypatch.setattr(wp, "browse", lambda *_args, **_kw: wp.WorkshopPage([item]))
    monkeypatch.setattr(wp, "gallery", lambda *_args: [])
    try:
        dialog = WorkshopDialog(root, tmp_path)
        dialog.withdraw()
        work, done = jobs.pop(0)
        done(work())
        label = dialog.preview_labels[item.published_id]
        assert label.cget("image") is None
        assert len(jobs) == 2  # normalization plus the independent gallery
        work, done = jobs.pop(0)
        done(work())
        while callbacks:
            callbacks.pop(0)()
        assert label.cget("image") is not None
        assert item.preview_path.parent == dialog.preview_cache
        with Image.open(item.preview_path) as normalized:
            assert normalized.size == (640, 360)
        with Image.open(original) as source:
            assert source.size == (1024, 768)
        dialog.destroy()
    finally:
        root.destroy()
