"""Native AncientEgypt digging distinguishes cooldown rejection from a stuck ray."""

import asyncio
import math
import time
from types import SimpleNamespace
from unittest.mock import patch

import shared.constants as C

from server.bot_ai.director import BotDirector
from server.bot_ai.messages import BotAction, BotActionKind
from server.config import ServerConfig
from server.main import BattleSpadesServer


def test_egypt_recorded_machete_face_accumulates_real_damage_despite_early_retry():
    async def scenario():
        server = BattleSpadesServer(ServerConfig())
        world = server.world_manager
        assert world.load_map("AncientEgypt")
        director = BotDirector(server, supervisor=SimpleNamespace())
        bot = await director.add_bot(team=2, class_id=16)
        assert bot is not None
        tool = int(C.MACHETE_TOOL)
        bot.loadout = [tool]
        bot.set_tool(tool, raw=True)
        bot.set_position(229.49290466308594, 267.4670715332031, 221.7423553466797)
        # The full match had already excavated this occupied column. Make the
        # starting pocket explicit; the two tested target cells stay authored.
        for z in (220, 221, 222, 223):
            world.set_block(229, 267, z, False)
        target = (229.5, 268.5, 221.5)
        cells = ((229, 268, 221), (229, 268, 222))
        assert all(world.get_solid(*cell) for cell in cells)
        direction = tuple(target[index] - bot.eye[index] for index in range(3))
        length = math.sqrt(sum(value * value for value in direction))
        bot.set_orientation_vector(*(value / length for value in direction))
        assert world.raycast(*bot.eye, *bot.orientation, 4.0) == cells[0]
        action = BotAction(BotActionKind.MELEE, tool, position=target)
        topology = world.topology_version
        mutations = []
        world.subscribe_mutations(lambda *change: mutations.append(change))
        now = time.monotonic()

        for offset, accepted, damage in ((0., True, 2.), (.1, False, 2.),
                                          (.75, True, 4.)):
            with patch("server.combat_runtime.time.monotonic", return_value=now + offset):
                assert director.gateway.execute(bot, action) is accepted
            assert all(world.get_solid(*cell) for cell in cells)
            assert all(world.block_damage[cell] == damage for cell in cells)
            assert world.topology_version == topology
            assert not mutations

        with patch("server.combat_runtime.time.monotonic", return_value=now + 1.5):
            assert director.gateway.execute(bot, action)
        assert all(not world.get_solid(*cell) for cell in cells)
        assert all(cell not in world.block_damage for cell in cells)
        assert world.topology_version >= topology + len(cells)
        assert set(cells) <= {tuple(change[:3]) for change in mutations if not change[3]}

    asyncio.run(scenario())
