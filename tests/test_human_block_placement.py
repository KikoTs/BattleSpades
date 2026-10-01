"""A human player can place blocks: one click and a dragged line.

Regression guard for the "I can't place blocks" report. It drives the REAL
server (``scripts/anticheat_lab``: real tick, real packet handlers, real
WorldMutationService commit after the owner's movement frame) with a lab
client that speaks the retail wire protocol: ClientData with the block tool
selected, then BlockLine(40) packets labelled with the client's own frame.
A placement only counts when the voxel is committed to the authoritative map,
the wallet is charged, and the builder receives its BlockLine echo (the stock
client keeps its ghost blocks until that echo arrives).
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

import shared.constants as C
from shared.bytes import ByteReader
from shared.packet import BlockLine

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from anticheat_lab import scenarios  # noqa: E402
from anticheat_lab.lab import Lab  # noqa: E402


def _centre(cell):
    return (cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5)


async def _build_session():
    lab = Lab(profile="lan", mode="tdm", seed=5)
    await lab.start()
    try:
        client = lab.add_client(
            "Builder", team=2, class_id=scenarios.SOLDIER, loadout=(8, 12, 72, 2),
        )
        model = client.model
        echoes = []
        original_on_packet = model.on_packet

        def on_packet(packet):
            if packet and packet[0] == BlockLine.id:
                echo = BlockLine()
                echo.read(ByteReader(packet[1:]))
                echoes.append((
                    (int(echo.x1), int(echo.y1), int(echo.z1)),
                    (int(echo.x2), int(echo.y2), int(echo.z2)),
                ))
            return original_on_packet(packet)

        model.on_packet = on_packet

        def teleport(position):
            if client.player is not None and client.player.alive:
                client.player.set_position(*position)

        ctx = scenarios.Context(
            model=model, court=lab.court, rng=random.Random(5), lane=0,
            teleport=teleport, server_event=lambda *_a, **_k: None, log=[],
        )
        assert await lab.join_all(60.0), "builder never joined"
        assert int(C.BLOCK_TOOL) in model.loadout

        court = lab.court
        world = lab.server.world_manager
        single = court.cell(13, 16, up=1)
        line = [court.cell(13 + step, 19, up=1) for step in range(4)]
        assert not world.get_solid(*single)
        assert not any(world.get_solid(*cell) for cell in line)
        blocks = {}

        def script():
            yield from scenarios.teleport(ctx, court.stand(10, 17), yaw=0.0)
            assert ctx.tool(int(C.BLOCK_TOOL))
            yield from scenarios.wait(ctx, 0.6)
            blocks["start"] = int(client.player.blocks)
            ctx.look_at(_centre(single))
            yield from scenarios.wait(ctx, 0.2)
            assert ctx.can_place(single)
            ctx.model.queue("block_line", start=single, end=single)
            yield from scenarios.wait(ctx, 1.0)
            blocks["after_single"] = int(client.player.blocks)
            ctx.look_at(_centre(line[-1]))
            yield from scenarios.wait(ctx, 0.2)
            assert ctx.can_place(line[0]) and ctx.can_place(line[-1])
            ctx.model.queue(
                "block_line", start=line[0], end=line[-1], cost=len(line),
            )
            yield from scenarios.wait(ctx, 1.0)
            blocks["after_line"] = int(client.player.blocks)
            while True:
                yield

        model.script = script()
        await lab.run(5.0)
        return {
            "single": single,
            "line": line,
            "single_solid": bool(world.get_solid(*single)),
            "line_solid": [bool(world.get_solid(*cell)) for cell in line],
            "echoes": list(echoes),
            "blocks": dict(blocks),
            "pending": lab.server.world_mutations.pending_count,
            "sent": int(model.stats.blocks_sent),
        }
    finally:
        await lab.stop()


def test_human_places_single_block_and_dragged_line_in_tdm():
    result = asyncio.run(_build_session())

    assert result["sent"] == 2
    single, line = result["single"], result["line"]
    # Committed to the authoritative map (not just accepted and queued).
    assert result["single_solid"], "single click did not place a block"
    assert all(result["line_solid"]), f"drag line placed {result['line_solid']}"
    assert result["pending"] == 0
    # The builder's own echo finalises the stock client's ghost blocks.
    assert (single, single) in result["echoes"]
    assert (line[0], line[-1]) in result["echoes"]
    # Charged once per committed voxel; a cancelled reservation refunds.
    blocks = result["blocks"]
    assert blocks["start"] - blocks["after_single"] == 1
    assert blocks["after_single"] - blocks["after_line"] == len(line)
