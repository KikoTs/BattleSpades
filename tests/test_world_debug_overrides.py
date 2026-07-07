from aoslib import world


def test_world_debug_override_round_trip():
    original = world.get_debug_movement_overrides()
    try:
        updated = world.set_debug_movement_override('standing_pos_above_ground', 2.4)
        assert updated == 2.4
        overrides = world.get_debug_movement_overrides()
        assert overrides['standing_pos_above_ground'] == 2.4
        assert overrides['standing_height'] == 2.85

        reset_value = world.reset_debug_movement_override('standing_pos_above_ground')
        assert reset_value == 2.25
        overrides = world.get_debug_movement_overrides()
        assert overrides['standing_height'] == 2.7
    finally:
        for name in world.get_debug_movement_override_names():
            value = original[name]
            world.set_debug_movement_override(name, value)


def test_world_debug_override_names_include_live_tuning_surface():
    names = set(world.get_debug_movement_override_names())
    expected = {
        'standing_pos_above_ground',
        'crouching_pos_above_ground',
        'crouch_shift',
        'jump_impulse',
        'ground_friction',
        'air_friction',
        'water_friction_scale',
        'accel_multiplier_scale',
        'sprint_multiplier_scale',
        'crouch_sneak_multiplier_scale',
        'climb_step_height',
        'climb_shift',
        'fall_slow_down',
    }
    assert expected.issubset(names)
    assert 'standing_height' not in names
    assert 'crouch_height' not in names
