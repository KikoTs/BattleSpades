"""Presses that lived in a ClientData the server did not simulate.

ClientData is ENet SEND_UNSEQUENCED: a datagram can be lost or arrive after
a newer one. The lost-frame refill (docs/RETAIL_INPUT_LOSS.md) keeps the step
count equal to the client's, but it guesses the missing frame's input. These
tests cover what a *late* original still tells the server, and that an older
packet never replaces newer input.
"""

import asyncio
from types import SimpleNamespace

from server.handlers.movement import handle_client_data
from server.player import INPUT_REORDER_WINDOW_TICKS
from tests.test_reversed_world_update import make_player


TICK_DT = 1.0 / 60.0
IDLE = (False,) * 8
FORWARD = (True, False, False, False, False, False, False, False)
FORWARD_JUMP = (True, False, False, False, True, False, False, False)
FORWARD_CROUCH = (True, False, False, False, False, True, False, False)
AIM = (1.0, 0.0, 0.0)
TURNED = (0.0, 1.0, 0.0)
NO_ACTIONS = (False,) * 9
HOVER = (False,) * 7 + (True, False)


def _recorder(player):
    """Replace physics with a recorder of the input each step used."""
    steps = []

    async def record_update(_dt):
        steps.append({
            "label": player.last_applied_input_loop,
            "up": bool(player.input.up),
            "jump": bool(player.input.jump),
            "crouch": bool(player.input.crouch),
            "hover": bool(player.input.hover),
            "aim": tuple(round(float(v), 3) for v in player.orientation),
        })

    player.update = record_update
    return steps


def _tick(player):
    asyncio.run(player.simulate_tick(TICK_DT))


def _prime(player, first=100, flags=FORWARD):
    """Consume two ordinary frames so the latch holds ``flags``."""
    for label in (first, first + 1):
        player.record_input_frame(label, flags, AIM, action_flags=NO_ACTIONS)
        _tick(player)


def test_late_original_restores_the_next_steps_buttons_and_aim():
    """Label 102 arrives after it was refilled but before 103 is simulated."""
    player, _ = make_player()
    steps = _recorder(player)
    _prime(player)

    # 102 is missing, 103 is here: the refill simulates 102 with held input.
    player.record_input_frame(103, FORWARD_JUMP, TURNED, action_flags=NO_ACTIONS)
    _tick(player)
    assert player.last_applied_input_loop == 102
    assert player.last_applied_input_synthesized is True

    # The original 102 shows up one tick late. It pressed jump and turned.
    player.record_input_frame(102, FORWARD_JUMP, TURNED, action_flags=NO_ACTIONS)
    assert player.input_frames_salvaged == 1
    assert player.input_frames_stale == 1

    _tick(player)
    # Step 103 latches packet 102: the client jumped and turned in it.
    assert steps[-1]["label"] == 103
    assert steps[-1]["jump"] is True
    assert steps[-1]["aim"] == (0.0, 1.0, 0.0)


def test_without_the_late_original_the_refill_guess_stands():
    player, _ = make_player()
    steps = _recorder(player)
    _prime(player)

    player.record_input_frame(103, FORWARD_JUMP, TURNED, action_flags=NO_ACTIONS)
    _tick(player)
    _tick(player)

    assert steps[-1]["label"] == 103
    assert steps[-1]["jump"] is False  # held buttons of packet 101
    assert player.input_frames_salvaged == 0


def test_jump_tap_that_lived_only_in_a_too_late_packet_fires_once():
    """The tap is over by the time its packet arrives: honour it, once."""
    player, _ = make_player()
    steps = _recorder(player)
    _prime(player)

    for label in (103, 104):
        player.record_input_frame(label, FORWARD, AIM, action_flags=NO_ACTIONS)
    _tick(player)  # refills 102
    _tick(player)  # consumes 103
    assert player.last_applied_input_loop == 103

    player.record_input_frame(102, FORWARD_JUMP, AIM, action_flags=NO_ACTIONS)
    assert player.input_presses_latched == 1

    _tick(player)  # consumes 104 with the latched tap
    assert steps[-1]["label"] == 104 and steps[-1]["jump"] is True

    player.record_input_frame(105, FORWARD, AIM, action_flags=NO_ACTIONS)
    _tick(player)
    assert steps[-1]["label"] == 105 and steps[-1]["jump"] is False
    assert sum(1 for step in steps if step["jump"]) == 1


def test_press_still_held_in_a_newer_frame_is_not_latched_again():
    """A jump the following packets still hold is seen there, not twice."""
    player, _ = make_player()
    steps = _recorder(player)
    _prime(player)

    for label in (103, 104):
        player.record_input_frame(
            label, FORWARD_JUMP, AIM, action_flags=NO_ACTIONS
        )
    _tick(player)  # refills 102
    _tick(player)  # consumes 103, latch now holds jump from packet 103
    player.record_input_frame(102, FORWARD_JUMP, AIM, action_flags=NO_ACTIONS)

    assert player.input_frames_salvaged == 1
    assert player.input_presses_latched == 0
    assert player._press_latch_flags == ()


def test_gadget_tap_in_a_late_packet_reaches_the_parachute_once():
    player, _ = make_player()
    steps = _recorder(player)
    _prime(player)

    player.record_input_frame(103, FORWARD, AIM, action_flags=NO_ACTIONS)
    _tick(player)  # refills 102
    player.record_input_frame(102, FORWARD, AIM, action_flags=HOVER)
    assert player.input_presses_latched == 1

    _tick(player)
    assert steps[-1]["label"] == 103 and steps[-1]["hover"] is True
    player.record_input_frame(104, FORWARD, AIM, action_flags=NO_ACTIONS)
    _tick(player)
    assert steps[-1]["hover"] is False
    assert sum(1 for step in steps if step["hover"]) == 1


def test_late_duplicate_of_a_real_frame_is_never_salvaged():
    player, _ = make_player()
    steps = _recorder(player)
    _prime(player)

    # 101 was consumed for real; a delayed copy claims a jump.
    player.record_input_frame(101, FORWARD_JUMP, AIM, action_flags=NO_ACTIONS)
    player.record_input_frame(102, FORWARD, AIM, action_flags=NO_ACTIONS)
    _tick(player)

    assert player.input_frames_salvaged == 0
    assert player.input_presses_latched == 0
    assert steps[-1]["jump"] is False


def test_each_refilled_label_is_salvaged_at_most_once():
    player, _ = make_player()
    _recorder(player)
    _prime(player)

    player.record_input_frame(103, FORWARD, AIM, action_flags=NO_ACTIONS)
    _tick(player)
    _tick(player)
    for _ in range(3):
        player.record_input_frame(
            102, FORWARD_JUMP, AIM, action_flags=NO_ACTIONS
        )

    assert player.input_frames_salvaged == 1
    assert player.input_presses_latched == 1


def test_a_new_life_drops_the_previous_bodys_latch():
    player, _ = make_player()
    _recorder(player)
    _prime(player)
    player.record_input_frame(103, FORWARD, AIM, action_flags=NO_ACTIONS)
    _tick(player)
    _tick(player)
    player.record_input_frame(102, FORWARD_JUMP, AIM, action_flags=NO_ACTIONS)
    assert player._press_latch_flags != ()

    player.spawn(*player.position)

    assert player._press_latch_flags == ()
    assert player._synthesized_labels == {}


# ---------------------------------------------------------------------------
# Arrival order
# ---------------------------------------------------------------------------


def test_arrival_order_measures_how_far_the_link_reorders():
    player, _ = make_player()
    for label in (200, 201, 203, 204):
        player.record_input_frame(label, FORWARD, AIM, received_server_tick=50)
    assert player.input_reorder_spread_frames(50) == 0

    player.record_input_frame(202, FORWARD, AIM, received_server_tick=51)

    assert player.input_frames_reordered == 1
    assert player.input_reorder_spread_frames(51) == 2
    # The evidence expires.
    assert player.input_reorder_spread_frames(
        51 + INPUT_REORDER_WINDOW_TICKS + 1
    ) == 0


def test_smaller_recent_reordering_outlives_an_older_larger_one():
    """The guard must not drop out when the largest event ages out."""
    player, _ = make_player()
    label = 1000

    def reorder(late, tick):
        nonlocal label
        label += 20
        player.record_input_frame(label, FORWARD, AIM, received_server_tick=tick)
        player.record_input_frame(
            label - late, FORWARD, AIM, received_server_tick=tick
        )

    reorder(3, 100)
    for tick in (700, 1000, 1250):
        reorder(2, tick)

    assert player.input_reorder_spread_frames(1290) == 3
    # The three-frame event is now older than the window; the two-frame
    # ones are not.
    assert player.input_reorder_spread_frames(1301) == 2
    assert player.input_reorder_spread_frames(1250 + 1201) == 0


def test_clock_sync_relabel_is_not_counted_as_reordering():
    """ClockSync rewrites the client loop by more than ten frames at once."""
    player, _ = make_player()
    player.record_input_frame(5000, FORWARD, AIM, received_server_tick=10)
    player.record_input_frame(4000, FORWARD, AIM, received_server_tick=11)
    player.record_input_frame(4001, FORWARD, AIM, received_server_tick=12)

    assert player.input_frames_reordered == 0
    assert player.input_reorder_spread_frames(12) == 0
    assert player.last_input_arrival_fresh is True


def _packet(label, flags, aim=AIM, tool=None, hover=False):
    names = ("up", "down", "left", "right", "jump", "crouch", "sneak", "sprint")
    fields = dict(zip(names, flags))
    return SimpleNamespace(
        loop_count=label,
        o_x=aim[0], o_y=aim[1], o_z=aim[2],
        primary=False, secondary=False, zoom=False, can_pickup=False,
        can_display_weapon=True, is_on_fire=False, is_weapon_deployed=False,
        hover=hover, palette_enabled=False, ooo=0,
        tool_id=tool,
        **fields,
    )


def test_older_client_data_does_not_replace_newer_immediate_state():
    """An out-of-order packet must not turn the aim or the tool back."""
    player, connection = make_player()
    server = connection.server
    server.loop_count = 900
    server.config = SimpleNamespace(movement_debug_capture=False)
    rifle, blocks = player.loadout[0], player.loadout[1]

    asyncio.run(handle_client_data(
        server, player, _packet(300, FORWARD, AIM, tool=rifle)
    ))
    asyncio.run(handle_client_data(
        server, player, _packet(302, FORWARD, TURNED, tool=blocks)
    ))
    assert int(player.tool) == int(blocks)
    newest_aim = tuple(round(float(v), 3) for v in player.orientation)

    asyncio.run(handle_client_data(
        server, player, _packet(301, FORWARD_JUMP, AIM, tool=rifle)
    ))

    assert int(player.tool) == int(blocks)
    assert tuple(round(float(v), 3) for v in player.orientation) == newest_aim
    assert player.input.jump is False
    # It is still buffered for the simulation, in label order.
    assert sorted(player.input_history) == [300, 301, 302]


def test_crouch_edge_is_counted_once_despite_the_buffered_replay(monkeypatch):
    """The replay trails the arrival state; both call update_input."""
    from server import combat_scores

    presses = []
    monkeypatch.setattr(
        combat_scores, "record_teabag_crouch",
        lambda server, player, **_kw: presses.append(1),
    )
    player, connection = make_player()
    server = connection.server
    server.loop_count = 900
    server.config = SimpleNamespace(movement_debug_capture=False)
    _recorder(player)
    rifle = player.loadout[0]

    # Two frames queue up before the first tick: a standing queue depth of 2.
    label = 400
    for flags in (FORWARD, FORWARD):
        asyncio.run(handle_client_data(
            server, player, _packet(label, flags, tool=rifle)
        ))
        label += 1
    # One crouch press held for six frames, one frame consumed per tick.
    for _ in range(6):
        asyncio.run(handle_client_data(
            server, player, _packet(label, FORWARD_CROUCH, tool=rifle)
        ))
        label += 1
        _tick(player)

    assert len(presses) == 1
