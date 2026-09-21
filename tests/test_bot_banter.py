"""Bot chat stays sparse, varied and strictly rate limited."""

from server.bot_ai.banter import BotBanter


def _crowd(seed: int = 3) -> BotBanter:
    banter = BotBanter(seed)
    for index, name in enumerate(("Atlas", "Bishop", "Bolt", "Comet", "Echo", "Flint", "Ghost",
                                  "Harbor", "Ibis", "Juno", "Kestrel", "Mako")):
        banter.register(index, name)
    return banter


def _fight(banter: BotBanter, now: float, killer: int, victim: int, **changes) -> None:
    details = dict(killer_id=killer, victim_id=victim, killer_name=f"K{killer}",
                   victim_name=f"V{victim}", streak=1, distance=20., melee=False, explosive=False)
    details.update(changes)
    banter.on_kill(now, **details)


def test_a_busy_match_never_exceeds_the_global_chat_rate():
    banter, spoken = _crowd(), []
    for tick in range(600):  # ten minutes, a kill every second
        now = float(tick)
        _fight(banter, now, tick % 12, (tick * 7 + 1) % 12, streak=tick % 5)
        spoken += [(now, *line) for line in banter.due(now)]
    assert 5 <= len(spoken) <= 100
    assert all(later[0] - earlier[0] >= 6.0 for earlier, later in zip(spoken, spoken[1:]))
    assert len({text for _now, _player, text in spoken}) >= 5
    assert all(len(text) <= 60 for _now, _player, text in spoken)


def test_some_bots_never_type_and_individuals_pause_between_lines():
    banter, speakers, last = _crowd(), set(), {}
    for tick in range(3000):
        now = float(tick)
        _fight(banter, now, tick % 12, (tick + 5) % 12)
        for player_id, _text in banter.due(now):
            assert now - last.get(player_id, -100.) >= 25.
            last[player_id] = now
            speakers.add(player_id)
    assert 3 <= len(speakers) < 12


def test_fighting_bots_hold_their_line_and_stale_lines_are_dropped():
    banter = BotBanter(1)
    banter.register(1, "Juno")
    banter._voices[1].chance = 1.0
    _fight(banter, 100., 1, 2)
    assert banter.due(101., busy=frozenset({1})) == []
    assert banter.due(140., busy=frozenset()) == []  # too old to be a reaction


def test_revenge_and_repeat_deaths_are_recognised():
    banter = BotBanter(5)
    for player_id in (1, 2):
        banter.register(player_id, f"Bot{player_id}")
        banter._voices[player_id].chance = 0.0
    _fight(banter, 10., 2, 1)
    _fight(banter, 20., 2, 1)
    assert banter._voices[1].nemesis == 2 and banter._voices[1].nemesis_count == 2
    _fight(banter, 30., 1, 2)
    assert banter._voices[1].nemesis == -1


def test_forgetting_a_bot_cancels_its_queued_line():
    banter = BotBanter(2)
    banter.register(4, "Nova")
    banter._voices[4].chance = 1.0
    _fight(banter, 50., 4, 9)
    banter.forget(4)
    assert banter.due(60.) == []


def _director():
    from types import SimpleNamespace
    import shared.constants as C
    from server.bot_ai.director import BotDirector
    from tests.test_equipment_handlers import _server_player

    server, killer, _ = _server_player(C.MINIGUN_TOOL, [C.MINIGUN_TOOL])
    sent = []
    server.broadcast = lambda data, **_kwargs: sent.append(bytes(data))
    director = BotDirector(server, supervisor=SimpleNamespace())
    director._started = True
    victim = SimpleNamespace(id=9, name="Victim", team=3 if killer.team != 3 else 2,
                             position=(30., 30., 30.))
    runtime = SimpleNamespace(player=SimpleNamespace(alive=False), lock_confirmed_at=0.)
    director._runtime[int(killer.id)] = runtime
    director.banter.register(int(killer.id), "Juno")
    director.banter._voices[int(killer.id)].chance = 1.0
    return director, server, killer, victim, sent


def test_director_turns_a_kill_into_one_ordinary_chat_packet_from_the_bot():
    import time
    from shared.packet import ChatMessage
    import shared.constants as C

    director, _server, killer, victim, sent = _director()
    director.on_player_killed(victim, killer, int(C.WEAPON_KILL))
    director._release_banter(time.monotonic() + 6.)
    assert len(sent) == 1
    packet = ChatMessage()
    from shared.bytes import ByteReader
    packet.read(ByteReader(sent[0][1:]))
    assert packet.player_id == int(killer.id) and packet.chat_type == int(C.CHAT_ALL)
    assert 0 < len(packet.value) <= 60
    director._release_banter(time.monotonic() + 7.)  # global gap: nothing more
    assert len(sent) == 1


def test_chatter_switch_and_team_kills_stay_silent():
    import time
    import shared.constants as C

    director, server, killer, victim, sent = _director()
    victim.team = killer.team
    director.on_player_killed(victim, killer, int(C.WEAPON_KILL))
    director._release_banter(time.monotonic() + 6.)
    assert sent == []
    victim.team = 3 if killer.team != 3 else 2
    server.config.bots.chatter = False
    director.on_player_killed(victim, killer, int(C.WEAPON_KILL))
    director._release_banter(time.monotonic() + 6.)
    assert sent == []
