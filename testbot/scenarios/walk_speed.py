# -*- coding: utf-8 -*-
"""walk_speed — measure server's authoritative sprint speed.

Bot sprints +Y for a fixed window, then computes:
  - observed_speed = (final_y - first_y) / window_seconds (blocks/sec)
  - per-tick speed (median Δy per WorldUpdate)

Reports both to the event stream. The harness asserts that speed is
within reasonable bounds for the chosen class — but the real point is
to drive a measured value the developer can compare to the live client
to detect server-vs-client multiplier mismatch.

If the live client (with the multipliers we sent in InitialInfo)
predicts a different speed than what the server simulates and broadcasts,
the player feels rubber-banding. This scenario surfaces that.
"""

import time as _t

NAME = 'walk_speed'
TIMEOUT = 30.0

# Default: SOLDIER (class_id=0). With sprint_multiplier=1.4 the server
# should produce ~10-12 blocks/sec sprint speed. Tighten the bounds once
# we measure a few classes against the live client.
SPRINT_DURATION_S = 2.0
EXPECTED_SPEED_MIN = 6.0    # blocks/sec; below this, something's broken
EXPECTED_SPEED_MAX = 25.0   # above this, multiplier or scaling is wrong


def script(c):
    c.do_full_handshake()

    # Drive a steady 60Hz forward+sprint for SPRINT_DURATION_S.
    after_idx = len(c.received_log)
    start_t = _t.time()
    tick = 0
    next_send = start_t
    interval = 1.0 / 60.0
    while _t.time() - start_t < SPRINT_DURATION_S:
        cd = c.make_client_data(
            loop_count=tick,
            tool_id=2,
            orientation=(0.0, 1.0, 0.0),
            up=True, sprint=True,
        )
        c.send(cd)
        tick += 1
        next_send += interval
        # Keep network responsive
        c.pump(max(0.0, next_send - _t.time()))

    c.pump(0.2)  # let last WorldUpdate arrive

    # Walk through WorldUpdates collected during the window, extract our y
    # samples, and compute the speed.
    samples = []  # list of (t_recv, y)
    for ts, pid, parsed in c.received_log[after_idx:]:
        if parsed is None or type(parsed).__name__ != 'WorldUpdate':
            continue
        items = getattr(parsed, 'items', None) or {}
        snap = items.get(c.our_player_id)
        if snap is None:
            continue
        try:
            y = float(snap[0][1])
        except Exception:
            continue
        samples.append((ts, y))

    if len(samples) < 5:
        raise RuntimeError(
            'too few WorldUpdate samples to measure speed: {}'.format(len(samples)))

    # Average speed across the window
    t0, y0 = samples[0]
    t1, y1 = samples[-1]
    window = t1 - t0
    avg_speed = (y1 - y0) / window if window > 0 else 0.0

    # Per-tick deltas (median)
    diffs = []
    for i in range(1, len(samples)):
        dt = samples[i][0] - samples[i - 1][0]
        dy = samples[i][1] - samples[i - 1][1]
        if dt > 0:
            diffs.append(dy / dt)
    diffs.sort()
    median_speed = diffs[len(diffs) // 2] if diffs else 0.0

    c.log.emit(
        'walk_speed_measured',
        class_id=c.last_create_player and int(getattr(c.last_create_player, 'class_id', 0)) or 0,
        samples=len(samples),
        window_s=round(window, 3),
        first_y=round(y0, 3),
        last_y=round(y1, 3),
        avg_speed_blocks_per_s=round(avg_speed, 3),
        median_speed_blocks_per_s=round(median_speed, 3),
        sprint_multiplier=1.4,    # SOLDIER default per shared.constants
    )

    if not (EXPECTED_SPEED_MIN <= avg_speed <= EXPECTED_SPEED_MAX):
        raise RuntimeError(
            'walk speed out of expected range: {:.2f} (want {:.1f}..{:.1f})'.format(
                avg_speed, EXPECTED_SPEED_MIN, EXPECTED_SPEED_MAX))

    c.disconnect()
