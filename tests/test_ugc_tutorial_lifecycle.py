"""Persistence and lane reuse must not replay an older editor/player life."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from modes import ugc as ugc_module
from modes.tutorial import TutorialStage
from shared.bytes import ByteReader
from shared.packet import LocalisedMessage
from tests.test_tutorial import _new_mode, _Player
from tests.test_ugc_round5 import _editor


def test_explicit_save_cannot_be_overwritten_by_older_checkpoint(tmp_path, monkeypatch):
    server, mode, host, player = _editor(tmp_path)

    async def exercise():
        checkpoint_started = asyncio.Event()
        release_checkpoint = asyncio.Event()

        async def controlled_io(function, *args):
            # Model an older disk job suspended before its replace. The
            # newer job runs to completion if the writer allows overlap.
            if function is ugc_module._atomic_write_text:
                if json.loads(args[1])["title"] == "R5":
                    checkpoint_started.set()
                    await release_checkpoint.wait()
            return function(*args)

        monkeypatch.setattr(ugc_module.asyncio, "to_thread", controlled_io)
        mode.request_checkpoint()
        await mode.on_tick(1)
        await checkpoint_started.wait()
        mode.project.title = "New edited title"
        assert mode.request_save(player)
        await asyncio.sleep(0)  # let the save reach its first I/O/lock wait
        release_checkpoint.set()
        await asyncio.gather(mode._checkpoint_task, mode._save_task)

    asyncio.run(exercise())
    saved = json.loads(Path(server.config.ugc_sidecar_path).read_text())
    assert saved["title"] == "New edited title"
    ack = LocalisedMessage(ByteReader(host.sent[-1][1:]))
    assert ack.string_id == "UGC_MAP_SAVE_SUCCESSFULLY"


def test_checkpoint_waiting_for_save_snapshots_latest_metadata(tmp_path, monkeypatch):
    server, mode, _host, player = _editor(tmp_path)

    async def exercise():
        save_started = asyncio.Event()
        release_save = asyncio.Event()

        async def controlled_io(function, *args):
            if function == mode._write_project_files:
                save_started.set()
                await release_save.wait()
            return function(*args)

        monkeypatch.setattr(ugc_module.asyncio, "to_thread", controlled_io)
        assert mode.request_save(player)
        await save_started.wait()
        mode.project.title = "Edit while saving"
        mode.request_checkpoint()
        await mode.on_tick(1)
        await asyncio.sleep(0)
        mode.project.title = "Edit while checkpoint waits"
        release_save.set()
        await asyncio.gather(mode._save_task, mode._checkpoint_task)

    asyncio.run(exercise())
    saved = json.loads(Path(server.config.ugc_sidecar_path).read_text())
    assert saved["title"] == "Edit while checkpoint waits"


def test_failed_checkpoint_retries_and_does_not_poison_explicit_save(tmp_path, monkeypatch):
    server, mode, host, player = _editor(tmp_path)
    original_write = ugc_module._atomic_write_text
    calls = 0

    def fail_once(path, text):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("test write failure")
        return original_write(path, text)

    monkeypatch.setattr(ugc_module, "_atomic_write_text", fail_once)

    async def exercise():
        mode.request_checkpoint()
        await mode.on_tick(1)
        await mode._checkpoint_task
        assert mode._metadata_dirty
        mode.project.title = "Recovered save"
        assert mode.request_save(player)
        await mode._save_task

    asyncio.run(exercise())
    saved = json.loads(Path(server.config.ugc_sidecar_path).read_text())
    assert saved["title"] == "Recovered save"
    ack = LocalisedMessage(ByteReader(host.sent[-1][1:]))
    assert ack.string_id == "UGC_MAP_SAVE_SUCCESSFULLY"


@pytest.mark.parametrize("reuse_numeric_id", [False, True])
def test_reused_lane_ignores_old_occupant_queued_target_removal(reuse_numeric_id):
    server, mode = _new_mode()
    first = _Player(1)
    mode.get_spawn_point(first)
    red = next(iter(mode._target_voxels[0][0]))
    try:
        assert server.world_manager.destroy_blocks([red])
        assert mode._mutation_queue
        asyncio.run(mode.on_player_leave(first))
        replacement = _Player(1 if reuse_numeric_id else 2)
        mode.get_spawn_point(replacement)
        assert server.world_manager.get_solid(*red)
        mode._drain_world_mutations(100.0)
        session = mode.session_for(replacement)
        assert not session.destroyed_targets
        assert server.world_manager.get_solid(*red)
        # A removal belonging to this learner still counts normally.
        assert server.world_manager.destroy_blocks([red])
        mode._drain_world_mutations(101.0)
        assert session.destroyed_targets == {0}
    finally:
        asyncio.run(mode.deactivate())


def test_lane_reuse_does_not_credit_restored_build_to_next_learner():
    server, mode = _new_mode()
    first = _Player(1)
    mode.get_spawn_point(first)
    built = (130, 70, 220)
    try:
        assert server.world_manager.set_block(*built, True, 0x123456)
        asyncio.run(mode.on_player_leave(first))
        replacement = _Player(2)
        mode.get_spawn_point(replacement)
        session = mode.session_for(replacement)
        session.stage = TutorialStage.CLIMB
        mode._drain_world_mutations(100.0)
        assert built not in session.built_cells
        assert not server.world_manager.get_solid(*built)
    finally:
        asyncio.run(mode.deactivate())
