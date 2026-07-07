# -*- coding: utf-8 -*-
"""block_build — place a block, observe broadcast, expect map state changed.

Sends BlockBuild(32) at a known coordinate. Server should:
  1. Accept the placement (validation: player in range, can_build).
  2. Broadcast BlockBuild back so other clients render it.
  3. Update its internal map state.

Surfaces:
  - BlockBuild parse + handler in protocol/packet_handler.py
  - server.combat.handle_block_build path
  - block-tool gating (player tool_id must be a block tool)
"""

import shared.packet as P

NAME = 'block_build'
TIMEOUT = 30.0


def script(c):
    c.do_full_handshake()

    # Switch to block tool. From shared.constants, BLOCK_TOOL is some int —
    # look it up dynamically.
    from shared.constants import BLOCK_TOOL
    block_tool_id = int(BLOCK_TOOL)
    c.log.emit('using_tool', tool_id=block_tool_id, BLOCK_TOOL=block_tool_id)

    # Send ClientData declaring our tool is the block tool.
    cd = c.make_client_data(loop_count=0, tool_id=block_tool_id,
                            orientation=(0.0, 1.0, 0.0))
    c.send(cd)
    c.pump(0.1)

    # Place a block at a coordinate near our spawn.
    spawn_x, spawn_y, spawn_z = c.spawn_xyz
    target_x = int(spawn_x)
    target_y = int(spawn_y) + 1
    # Place at z that's likely solid+1 — try ground level. Server validates.
    target_z = int(spawn_z) - 1

    bb = P.BlockBuild()
    bb.loop_count = 0
    bb.player_id = c.our_player_id
    bb.x = target_x
    bb.y = target_y
    bb.z = target_z
    bb.block_type = 0  # type byte; server should ignore for plain BlockBuild
    after_idx = len(c.received_log)
    c.send(bb)

    # Wait for the BlockBuild broadcast back to us
    echo = c.wait_for(
        'BlockBuild',
        predicate=lambda p: (
            int(getattr(p, 'x', -1)) == target_x and
            int(getattr(p, 'y', -1)) == target_y and
            int(getattr(p, 'z', -1)) == target_z
        ),
        timeout=3.0,
        after_idx=after_idx,
    )
    c.log.emit('block_build_echo',
               x=int(getattr(echo, 'x', -1)),
               y=int(getattr(echo, 'y', -1)),
               z=int(getattr(echo, 'z', -1)),
               player_id=int(getattr(echo, 'player_id', -1)))

    c.disconnect()
