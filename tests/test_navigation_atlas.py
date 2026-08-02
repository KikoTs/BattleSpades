"""Deterministic coverage for cached map-wide bot navigation semantics."""

from __future__ import annotations

import zlib
from pathlib import Path

import pytest

from server.bot_ai.navigation_atlas import (
    _CACHE_HEADER,
    ColumnFlag,
    NavigationAtlas,
    source_digest,
)


def _mask(*support_z: int) -> int:
    return sum(1 << int(z) for z in support_z)


def _follow_water(
    atlas: NavigationAtlas,
    start: tuple[int, int],
) -> tuple[tuple[int, int], int]:
    """Follow one cached flow while proving it cannot revisit a water cell."""

    position = start
    seen: set[tuple[int, int]] = set()
    steps = 0
    while True:
        route = atlas.water_route(*position)
        if route is None:
            return position, steps
        assert position not in seen
        seen.add(position)
        position = route.next_x, route.next_y
        steps += 1
        assert steps <= atlas.area


def test_atlas_labels_primary_ground_layers_and_narrow_passages() -> None:
    masks = [_mask(239)] * 24
    # The longest connected surface is the primary ground component.
    for x in range(5):
        masks[x] = _mask(10)
    # One vertically layered column represents a tunnel under upper ground.
    masks[3 * 6 + 2] = _mask(5, 12)

    atlas = NavigationAtlas.from_masks(masks, width=6, height=4)

    upper = atlas.context(2, 3, 5)
    tunnel = atlas.context(2, 3, 12)
    passage = atlas.context(2, 0, 10)
    assert upper is not None and tunnel is not None and passage is not None
    assert upper.layer_count == 2
    assert upper.underground is False
    assert tunnel.underground is True
    assert passage.main_ground is True
    assert passage.chokepoint is True
    atlas.validate()


def test_water_flow_merges_without_cycles_and_reaches_direct_shore() -> None:
    width, height = 9, 5
    masks = [_mask(20)] * (width * height)
    for x in range(7):
        masks[2 * width + x] = _mask(239)
    masks[2 * width + 7] = _mask(238)

    atlas = NavigationAtlas.from_masks(masks, width=width, height=height)

    end, steps = _follow_water(atlas, (0, 2))
    assert end == (7, 2)
    assert steps == 7
    route = atlas.water_route(0, 2)
    assert route is not None and route.climbable is True
    assert atlas.stats().unreachable_water_columns == 0


def test_water_flow_avoids_near_two_block_bank_for_swimmable_shore() -> None:
    width, height = 9, 5
    masks = [_mask(20)] * (width * height)
    for x in range(1, 7):
        masks[2 * width + x] = _mask(239)
    masks[2 * width] = _mask(237)
    masks[2 * width + 7] = _mask(238)

    atlas = NavigationAtlas.from_masks(masks, width=width, height=height)

    route = atlas.water_route(1, 2)
    assert route is not None
    assert (route.goal_x, route.goal_y) == (7, 2)
    assert route.climbable is True
    end, steps = _follow_water(atlas, (1, 2))
    assert end == (7, 2)
    assert steps == 6


def test_enclosed_water_routes_to_high_bank_for_build_recovery() -> None:
    width, height = 8, 3
    masks = [_mask(20)] * (width * height)
    for x in range(6):
        masks[width + x] = _mask(239)
    masks[width + 6] = _mask(226)

    atlas = NavigationAtlas.from_masks(masks, width=width, height=height)

    route = atlas.water_route(0, 1)
    assert route is not None
    assert route.climbable is False
    end, steps = _follow_water(atlas, (0, 1))
    assert end == (6, 1)
    assert steps == 6
    stats = atlas.stats()
    assert stats.assisted_water_columns == 6
    assert stats.unreachable_water_columns == 0


def test_cache_round_trip_rejects_stale_and_oversized_payloads() -> None:
    atlas = NavigationAtlas.from_masks(
        [_mask(10), _mask(239), _mask(238), _mask(10)],
        width=2,
        height=2,
    )
    digest = source_digest(b"fixture-vxl")
    encoded = atlas.to_cache_bytes(digest)

    restored = NavigationAtlas.from_cache_bytes(
        encoded,
        expected_digest=digest,
    )
    assert restored.stats() == atlas.stats()
    assert restored.water_route(1, 0) == atlas.water_route(1, 0)

    with pytest.raises(ValueError, match="does not match"):
        NavigationAtlas.from_cache_bytes(
            encoded,
            expected_digest=source_digest(b"changed-vxl"),
        )

    # The payload length remains bounded even if a forged compressed stream
    # expands well beyond the header's declared fixed-width arrays.
    oversized = zlib.compress(b"x" * 100_000)
    header = list(_CACHE_HEADER.unpack_from(encoded))
    header[-1] = len(oversized)
    corrupt = _CACHE_HEADER.pack(*header) + oversized
    with pytest.raises(ValueError):
        NavigationAtlas.from_cache_bytes(
            corrupt,
            expected_digest=digest,
        )


def test_route_validation_rejects_non_decreasing_water_edge() -> None:
    masks = [_mask(10)] * 15
    masks[6] = _mask(239)
    masks[7] = _mask(239)
    masks[8] = _mask(238)
    atlas = NavigationAtlas.from_masks(
        masks,
        width=5,
        height=3,
    )
    first = 6
    next_index = int(atlas.water_next[first])
    atlas.water_distance[next_index] = atlas.water_distance[first]

    with pytest.raises(ValueError, match="strictly decreasing"):
        atlas.validate()


def test_flags_remain_fixed_width_cache_data() -> None:
    atlas = NavigationAtlas.from_masks(
        [_mask(10), _mask(239)],
        width=2,
        height=1,
    )

    assert atlas.flags[0] & int(ColumnFlag.DRY)
    assert atlas.flags[1] & int(ColumnFlag.WATER)
    assert len(atlas.flags) == atlas.area


def test_every_shipped_vxl_has_a_matching_complete_cache() -> None:
    maps_path = Path(__file__).resolve().parents[1] / "maps"
    maps = sorted(maps_path.glob("*.vxl"))
    assert maps

    for map_path in maps:
        raw_vxl = map_path.read_bytes()
        cache = map_path.with_suffix(".botnav")
        assert cache.is_file(), f"missing bot navigation cache for {map_path.name}"
        atlas = NavigationAtlas.from_cache_bytes(
            cache.read_bytes(),
            expected_digest=source_digest(raw_vxl),
        )
        atlas.validate()
        assert atlas.stats().unreachable_water_columns == 0, map_path.name
