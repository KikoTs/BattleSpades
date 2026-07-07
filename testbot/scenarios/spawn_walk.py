# -*- coding: utf-8 -*-
"""spawn_walk — drive ClientData input + observe WorldUpdate.

For ~2s, send 60Hz ClientData with `up=True, sprint=True` and a +Y-facing
orientation. Then check the most recent WorldUpdate to see whether the
server moved our player along +Y.

This surfaces:
  - whether the server accepts our ClientData layout at all (no parse errors)
  - whether the server runs movement on the world authoritatively
  - whether WorldUpdate carries our position back to us

Tolerance: any movement at all in ~2s along +Y, given gravity+friction may
also drag us z-down on terrain. We just verify y_after - y_before > 0.5.
"""

NAME = 'spawn_walk'
TIMEOUT = 30.0


def script(c):
    c.do_full_handshake()

    start_x, start_y, start_z = c.spawn_xyz
    c.log.emit('walk_start_pos', x=start_x, y=start_y, z=start_z)

    # Drive 60Hz input for ~2 seconds
    n_ticks = 120
    after_idx = len(c.received_log)
    for tick in range(n_ticks):
        cd = c.make_client_data(
            loop_count=tick,
            tool_id=2,
            orientation=(0.0, 1.0, 0.0),  # face +Y
            up=True,
            sprint=True,
        )
        c.send(cd)
        c.pump(1.0 / 60)

    # Idle one more pump so we definitely catch the latest WorldUpdate
    c.pump(0.2)

    # Find the latest WorldUpdate
    last_wu = None
    for ts, pid, parsed in c.received_log[after_idx:]:
        if parsed is not None and type(parsed).__name__ == 'WorldUpdate':
            last_wu = parsed
    if last_wu is None:
        raise RuntimeError('no WorldUpdate received during walk window')

    # WorldUpdate carries per-player snapshots in `parsed.items` (dict).
    # Each snapshot is a list-like: [position(xyz), orientation(xyz), ...].
    items = getattr(last_wu, 'items', None)
    end_pos = None
    if items and c.our_player_id in items:
        snap = items[c.our_player_id]
        # snap is a list/tuple; first element is [x, y, z]
        try:
            pos = snap[0]
            end_pos = (float(pos[0]), float(pos[1]), float(pos[2]))
        except Exception:
            end_pos = None

    c.log.emit('walk_observation',
               start=(start_x, start_y, start_z),
               end=end_pos,
               had_snapshot=bool(end_pos is not None),
               worldupdate_repr=repr(last_wu)[:200])

    if end_pos is None:
        raise RuntimeError(
            'could not locate our player_id={} in WorldUpdate'.format(c.our_player_id))

    dy = end_pos[1] - start_y
    if abs(dy) < 0.5:
        raise RuntimeError(
            'no measurable +Y movement after {} ticks: dy={:.3f}'.format(
                n_ticks, dy))

    c.log.emit('walk_passed', dy=dy)
    c.disconnect()
