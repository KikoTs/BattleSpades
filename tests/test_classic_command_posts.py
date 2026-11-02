"""Classic medical tents: safe presentation and authoritative full resupply."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import shared.constants as C
from modes.classic_ctf import ClassicCTFMode
from server.class_selection import normalize_class_selection
from server.game_constants import PLAYER_STANDING_POS_ABOVE_GROUND, TEAM1, TEAM2, WEAPON_PROFILES
from tests.test_pickups import _server_player


def test_classic_starts_with_two_supported_tents_and_no_map_crates():
    async def scenario():
        server, _, _ = _server_player()
        server.config.default_mode = "cctf"
        server.mode = mode = ClassicCTFMode(server)
        try:
            await mode.on_mode_start()
            posts = [entity for entity in server.entity_registry.all() if entity.kind == "base"]
            assert len(posts) == 2
            assert {entity.state for entity in posts} == {TEAM1, TEAM2}
            assert all(entity.type == int(C.CAPTURE_POINT_ENTITY) and entity.wire_visible for entity in posts)
            assert all(entity.to_wire_entity().type == int(C.CAPTURE_POINT_ENTITY) for entity in posts)
            assert not any(entity.kind in {"map_health", "map_ammo", "map_block"}
                           for entity in server.entity_registry.all())
        finally:
            await mode.deactivate()
    asyncio.run(scenario())


@pytest.mark.parametrize("weapon", (int(C.RIFLE_TOOL), int(C.CLASSIC_SHOTGUN_TOOL)))
def test_own_tent_fills_all_resources_without_resetting_clip_or_reload(weapon):
    async def scenario():
        server, player, _ = _server_player()
        server.config.default_mode = "cctf"
        server.mode = mode = ClassicCTFMode(server)
        try:
            await mode.on_mode_start()
            post = server.entity_registry.get(mode._base_entities[player.team])
            player.apply_class_selection(normalize_class_selection(C.CLASS_CLASSIC_SOLDIER, [weapon]))
            player.spawn(post.x, post.y, post.z - PLAYER_STANDING_POS_ABOVE_GROUND)
            player.set_tool(weapon, raw=True)
            player.health, player.blocks = 37, 7
            player.ammo_clip, player.ammo_reserve = 1, 0
            player.grenades = 0
            player.reloading, player.reload_end_time = True, 9999.0
            with patch("modes.classic_ctf.time.time", return_value=1000.0):
                await mode.on_tick(0)
            assert player.health == player.max_health
            assert player.blocks == player._block_wallet_max()
            assert player.ammo_clip == 1
            assert player.ammo_reserve == WEAPON_PROFILES[weapon].reserve_ammo
            assert player.grenades == int(C.CLASSIC_GRENADE_STOCK)
            assert player.reloading and player.reload_end_time == 9999.0
            player.health = 37
            with patch("modes.classic_ctf.time.time", return_value=1001.0):
                await mode.on_tick(1)
            assert player.health == 37  # Bounded continuous contact, no tick spam.
            with patch("modes.classic_ctf.time.time", return_value=1003.0):
                await mode.on_tick(2)
            assert player.health == player.max_health
        finally:
            await mode.deactivate()
    asyncio.run(scenario())


@pytest.mark.parametrize("where", ("enemy", "above", "dead", "ended", "wall"))
def test_tent_does_not_supply_enemies_other_floors_or_inactive_players(where):
    async def scenario():
        server, player, _ = _server_player()
        server.config.default_mode = "cctf"
        server.mode = mode = ClassicCTFMode(server)
        try:
            await mode.on_mode_start()
            team = TEAM2 if where == "enemy" else TEAM1
            post = server.entity_registry.get(mode._base_entities[team])
            z = post.z - PLAYER_STANDING_POS_ABOVE_GROUND - (10 if where == "above" else 0)
            player.spawn(post.x + (2.0 if where == "wall" else 0.0), post.y, z)
            if where == "wall":
                for block_z in range(int(post.z) - 4, int(post.z)):
                    server.world_manager.set_block(int(post.x) + 1, int(post.y), block_z, True, 0x707070)
            player.health = 37
            if where == "dead":
                player.alive = False
            if where == "ended":
                mode.ended = True
            await mode.on_tick(0)
            assert player.health == 37
        finally:
            await mode.deactivate()
    asyncio.run(scenario())
