"""A bot can only ever act on, and be announced as, the slot it really owns.

Regression coverage for two live reports:

* "a backfilled bot hijacks the player": the retail client binds its local
  player to the CreatePlayer carrying its name, so a bot joining later under
  a name a live human already uses steals that human's view and controls;
  a bot controller must also never steer, shoot or build under an id that
  now belongs to someone else;
* "bots are invisible after a reconnect": a peer that is still loading must
  get the bot roster changes it missed as PlayerLeft -> CreatePlayer, never
  as a silent respawn of the entry it already has, and PlayerLeft for a bot
  must reach exactly the peers that were told about it.
"""

from __future__ import annotations

import asyncio
import gc
import time
from types import SimpleNamespace

from modes.tdm import TDMMode
from server.bot_ai.director import BotDirector
from server.bot_ai.messages import (
    BotAction,
    BotActionKind,
    BotIntent,
    MovementAffordance,
    MovementIntent,
)
from server.config import ServerConfig
from server.main import BattleSpadesServer
from server.player import Player
from server.player_names import name_skeleton
from server.roster import catch_up_roster, player_life_token, remember_player_life
from shared.bytes import ByteReader
from shared.packet import CreatePlayer, PlayerLeft


class RecordingConnection:
    """Network peer double with the production roster ledgers."""

    def __init__(self, *, in_game: bool, player=None) -> None:
        self.in_game = in_game
        self.player = player
        self.sent: list[bytes] = []
        self.known_player_lives: dict[int, tuple[int, int]] = {}
        self.known_player_deaths: dict[int, tuple[int, int]] = {}
        self.known_corpse_cleanups: dict[int, tuple[int, int]] = {}

    def send(self, data, reliable=True, prefix=0x30, **_kwargs):
        self.sent.append(bytes(data))

    def roster_events(self) -> list[tuple[str, int, str]]:
        events = []
        for data in self.sent:
            if data[:1] == bytes((PlayerLeft.id,)):
                events.append(("left", data[1], ""))
            elif data[:1] == bytes((CreatePlayer.id,)):
                packet = CreatePlayer()
                packet.read(ByteReader(data[1:]))
                events.append(("create", int(packet.player_id), str(packet.name)))
        return events


def _supervisor(intents=()):
    queue = list(intents)

    def drain_intents(limit=12):
        taken = queue[:limit]
        del queue[:limit]
        return taken

    return SimpleNamespace(
        start=lambda _snapshot: None,
        close=lambda: None,
        discard_timeline=lambda: None,
        request_restart=lambda: None,
        drain_intents=drain_intents,
        queue=queue,
    )


def _server(*, name_prefix: str = "[BOT]", max_players: int = 8):
    config = ServerConfig()
    config.max_players = max_players
    config.bots.max_bots = 6
    config.bots.population_mode = "admin"
    config.bots.name_prefix = name_prefix
    server = BattleSpadesServer(config)
    server.world_manager.generate_flat_map()
    return server


async def _with_mode(server):
    server.mode = TDMMode(server)
    await server.mode.on_mode_start()


def _human(server, player_id: int, name: str, team: int = 2) -> Player:
    human = Player(player_id, name, team, 6)
    server.players[player_id] = human
    server.teams[team].add_player(human)
    return human


# --------------------------------------------------------------- bug 1


def test_backfilled_bot_never_takes_a_live_humans_name() -> None:
    """The retail client keys its own player on the CreatePlayer name."""

    async def scenario():
        for prefix in ("", "[BOT]"):
            server = _server(name_prefix=prefix)
            await _with_mode(server)
            human = _human(server, 0, "[BOT]Noob" if prefix else "Noob")
            lookalike = _human(server, 1, "N00b~2")
            watcher = RecordingConnection(in_game=True, player=human)
            server.connections = {"human": watcher}
            director = BotDirector(server, supervisor=_supervisor())
            for requested in ("Noob", "N00b", "noob", "Noob~2"):
                bot = await director.add_bot(team=3, name=requested)
                assert bot is not None
            humans = {name_skeleton(human.name), name_skeleton(lookalike.name)}
            created = [
                name for kind, player_id, name in watcher.roster_events()
                if kind == "create"
            ]
            assert len(created) == 4
            skeletons = [name_skeleton(name) for name in created]
            assert not humans.intersection(skeletons), created
            assert len(set(skeletons)) == len(skeletons), created
            assert all(len(name.encode("utf-8")) <= 15 for name in created)
            await director.close()

    asyncio.run(scenario())


def _intent(bot, generation: int, frame: int) -> BotIntent:
    now = time.monotonic()
    return BotIntent(
        bot_id=int(bot.id),
        bot_generation=int(generation),
        frame_id=frame,
        map_epoch=1,
        mode_epoch=1,
        topology_version=0,
        created_at=now,
        expires_at=now + 5.0,
        movement=MovementIntent(
            direction=(1.0, 0.0, 0.0),
            affordance=MovementAffordance.WALK,
        ),
    )


def test_bot_controller_cannot_drive_an_id_it_no_longer_owns() -> None:
    """Intents and actions need the id, the generation AND the live slot."""

    server = _server()
    director = BotDirector(server, supervisor=_supervisor())
    bot = asyncio.run(director.add_bot(team=2, name="Driver"))
    assert bot is not None
    runtime = director._runtime[bot.id]
    director._map_epoch = director._mode_epoch = 1

    # The number is handed to a human by any path that skipped remove_bot.
    human = Player(bot.id, "Kiko", 2, 6)
    human.spawn(*bot.position)
    server.players[bot.id] = human
    before = (
        human.input.up, human.input.down, human.input.left,
        human.input.right, human.input.primary_fire, int(human.tool),
    )

    fire = BotAction(kind=BotActionKind.FIRE, tool_id=int(bot.tool))
    assert director.gateway.execute(bot, fire) is False
    director.supervisor.queue.append(_intent(bot, runtime.generation, 1))
    runtime.pending_action = fire
    director._pending_gateway_actions[int(bot.id)] = (
        runtime.generation, fire, time.monotonic()
    )
    director._started = True
    try:
        assert director.drain_actions(limit=4) == 0
        director._drain_intents(time.monotonic())
        assert runtime.intent is None
        director._retire_orphaned_runtimes()
    finally:
        director._started = False

    assert bot.id not in director._runtime
    assert bot not in director.bots
    assert server.players[bot.id] is human
    assert before == (
        human.input.up, human.input.down, human.input.left,
        human.input.right, human.input.primary_fire, int(human.tool),
    )


def test_live_bot_still_executes_through_the_gateway_guard() -> None:
    server = _server()
    director = BotDirector(server, supervisor=_supervisor())
    bot = asyncio.run(director.add_bot(team=2, name="Owner"))
    assert director.gateway.owns_live_slot(bot)
    assert director._runtime_is_live(director._runtime[bot.id])
    # Same id, next generation: the old controller is not this occupant.
    runtime = director._runtime[bot.id]
    runtime.generation += 1
    assert not director._runtime_is_live(runtime)


def test_departing_bot_id_is_not_reused_before_its_player_left() -> None:
    """A yielding leave hook must not let the slot be re-created and wiped."""

    class YieldingMode:
        def __init__(self):
            self.release = asyncio.Event()
            self.entered = asyncio.Event()

        async def on_player_join(self, player):
            return None

        async def on_player_leave(self, player):
            self.entered.set()
            await self.release.wait()

    async def scenario():
        server = _server()
        watcher = RecordingConnection(in_game=True)
        server.connections = {"watcher": watcher}
        mode = YieldingMode()
        server.mode = mode
        director = BotDirector(server, supervisor=_supervisor())
        leaving = await director.add_bot(team=2, name="Leaving")
        departing_id = int(leaving.id)

        removal = asyncio.create_task(director.remove_bot(leaving, force=True))
        await mode.entered.wait()
        # The tick's backfill runs while the leave hook is suspended.
        assert server.get_next_player_id() != departing_id
        backfill = await director.add_bot(team=3, name="Backfill")
        assert backfill is not None and int(backfill.id) != departing_id
        mode.release.set()
        await removal
        assert departing_id not in server.reserved_player_ids
        assert server.get_next_player_id() == departing_id

        events = watcher.roster_events()
        assert ("left", departing_id, "") in events
        assert ("left", int(backfill.id), "") not in events
        server.mode = None
        await director.close()

    asyncio.run(scenario())


# --------------------------------------------------------------- bug 2


def test_bot_player_left_reaches_only_peers_that_know_it() -> None:
    async def scenario():
        server = _server()
        await _with_mode(server)
        director = BotDirector(server, supervisor=_supervisor())
        bot = await director.add_bot(team=2, name="Shy")
        knows = RecordingConnection(in_game=True)
        remember_player_life(knows, bot)
        # Joined while the bot was dead: never created on this GameScene.
        stranger = RecordingConnection(in_game=True)
        server.connections = {"knows": knows, "stranger": stranger}

        assert await director.remove_bot(bot, force=True)

        assert ("left", int(bot.id), "") in knows.roster_events()
        assert all(kind != "left" for kind, *_ in stranger.roster_events())
        assert int(bot.id) not in knows.known_player_lives
        await director.close()

    asyncio.run(scenario())


def test_reconnecting_peer_gets_replaced_bot_as_leave_then_create() -> None:
    """A bot swapped on the same id while a peer loads is re-announced."""

    async def scenario():
        server = _server()
        await _with_mode(server)
        director = BotDirector(server, supervisor=_supervisor())
        first = await director.add_bot(team=2, name="First")
        other = await director.add_bot(team=3, name="Steady")
        slot = int(first.id)

        rejoiner = Player(5, "Kiko", 2, 6)
        server.players[5] = rejoiner
        loading = RecordingConnection(in_game=False, player=rejoiner)
        server.connections = {"loading": loading}
        # Handshake roster (Connection.send_existing_players records these).
        remember_player_life(loading, first)
        remember_player_life(loading, other)
        old_token = player_life_token(first)

        # Population churn while the map loads: retire, then backfill the
        # same lowest free number. Gameplay broadcasts are gated meanwhile.
        assert await director.remove_bot(first, force=True)
        del first
        gc.collect()
        second = await director.add_bot(team=3, name="Second")
        assert int(second.id) == slot
        assert player_life_token(second)[0] != old_token[0]
        assert loading.sent == []

        catch_up_roster(server, loading)

        events = loading.roster_events()
        assert events[:2] == [
            ("left", slot, ""),
            ("create", slot, second.name),
        ], events
        # The unchanged bot is not re-created, and the ledger now matches.
        assert all(event[1] != int(other.id) for event in events)
        assert loading.known_player_lives[slot] == player_life_token(second)
        await director.close()

    asyncio.run(scenario())
