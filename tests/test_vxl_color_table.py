"""The map's voxel colours live in a compact table that must behave like the dict it replaced."""

import random
import sys

import pytest

from aoslib.vxl import VXL, _ColorTable


def test_table_matches_a_dict_through_growth_deletion_and_slot_reuse():
    table, model = _ColorTable(), {}
    rng = random.Random(7)
    for step in range(200_000):
        key = rng.randrange(1 << 20) if step % 3 else rng.randrange(512)  # collide often
        action = rng.random()
        if action < 0.55:
            value = rng.randrange(1, 1 << 32)
            table[key] = value
            model[key] = value
        elif action < 0.85:
            if key in model:
                del table[key]
                del model[key]
            else:
                with pytest.raises(KeyError):
                    del table[key]
        else:
            assert table.get(key, 0) == model.get(key, 0)
            assert (key in table) == (key in model)
    assert len(table) == len(model)
    assert all(table.get(key) == value for key, value in model.items())
    assert table.get((1 << 26) - 1, 123) == 123


def test_reserved_slot_markers_are_never_accepted_as_keys():
    table = _ColorTable()
    for marker in (0xFFFFFFFF, 0xFFFFFFFE):
        with pytest.raises(KeyError):
            table[marker] = 1
        assert marker not in table and table.get(marker, 5) == 5


def test_churn_in_a_small_table_does_not_grow_it_without_bound():
    # Dig-and-rebuild play deletes as much as it stores; tombstones are purged in place.
    table = _ColorTable()
    for cycle in range(400):
        for key in range(1000):
            table[key + cycle] = key + 1
        for key in range(1000):
            del table[key + cycle]
    assert len(table) == 0
    assert sys.getsizeof(table) < 64 * 1024  # 1000 live entries never needed more
    table[5] = 9
    assert table.get(5) == 9


def test_map_colours_survive_edits_and_a_blank_reset():
    world = VXL(-1, b"", 0, 2)
    world.set_point(10, 10, 60, 0x7F112233)
    assert world.get_color(10, 10, 60) == 0x7F112233
    world.set_point(10, 10, 60, 0x7F445566)
    assert world.get_color(10, 10, 60) == 0x7F445566
    world.remove_point(10, 10, 60)
    assert world.get_color(10, 10, 60) == 0 and not world.get_solid(10, 10, 60)
    world.set_point(11, 10, 60, 0x7F010203)
    world.destroy()
    assert world.get_color(11, 10, 60) == 0
