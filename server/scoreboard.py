"""Scoreboard + HUD timer.

Three wire pieces the client uses to show scores/time:

- SetScore(85): the incremental score updater. type=TEAM(0) sets a team's
  score bar; type=PLAYER(1) sets ONE player's personal scoreboard number.
  The client keeps a running per-player table from these — so a player's
  kill count only shows up if we send a PLAYER SetScore when they score.
- DisplayCountdown(84): the on-screen round-timer countdown (a float of
  seconds REMAINING). Broadcast each frame so it ticks smoothly.
- GameStats(67): final leaderboard data. Carries a list of
  (player_id, stat_type) rows; a separate terminal UI packet renders the
  full-screen leaderboard, but same-map restarts must not use that transition.

Constants (from shared.constants, verified live):
  SCORE.TEAM=0, SCORE.PLAYER=1
  SCORE_REASON.KILL=1, SUICIDE=2, DEATH=220
"""
from __future__ import annotations

import shared.constants as C
from server.game_constants import TEAM1, TEAM2
from shared.packet import (GameStats, DisplayCountdown, SetScore,
                           ShowGameStats, MapEnded, ShowTextMessage,
                           ForceShowScores, LockTeam, TeamLockClass)
from server.connection import internal_team_to_wire

SCORE_TEAM = int(C.SCORE.TEAM)
SCORE_PLAYER = int(C.SCORE.PLAYER)
REASON_KILL = int(getattr(C.SCORE_REASON, "KILL_SCORE_REASON", 1))
REASON_SUICIDE = int(getattr(C.SCORE_REASON, "SUICIDE_SCORE_REASON", 2))
# Retail HUD.draw_timer (hud.pyd, hud.pyx:777-786) prints
# ``'%02d:%02d' % gmtime(countdown)[tm_min, tm_sec]``: there is no hour field.
# A clock above one hour -- Classic CTF's retail 5400 s, the Match Lobby's
# 90-minute option -- starts at 30:00, reaches 00:00 a full hour before the
# round ends, then wraps to 59:59 ("the timer hits zero and resets to 59
# minutes"). The round length stays retail; only the displayed value is held
# at the largest MM:SS the HUD can draw until the last hour, so 00:00 on the
# HUD always means the round is over. Just under 3600 so gmtime never rolls
# the minute over to 00 between the 1 Hz refreshes.
HUD_CLOCK_MAX_SECONDS = 3599.999


def player_score_packet(player, *, reason: int = 0) -> bytes:
    """Encode an absolute snapshot without awarding profile credit."""
    pkt = SetScore()
    pkt.type, pkt.reason = SCORE_PLAYER, int(reason)
    pkt.specifier, pkt.value = int(player.id), int(getattr(player, "score", 0))
    return bytes(pkt.generate())


def broadcast_player_packet(server, data: bytes, player_id: int, *,
                            reliable: bool = True) -> None:
    """Send a packet naming ``player_id`` only to peers that know that id.

    Retail SetScore(PLAYER) indexes the scene's player table; a dead joiner
    (never CreatePlayer'd on peers) or a departed id raises there.
    ``BattleSpadesServer.broadcast(known_player_id=...)`` filters per
    connection by ``known_player_lives``.
    """
    if reliable:
        server.broadcast(data, known_player_id=int(player_id))
    else:
        server.broadcast(data, reliable=False, known_player_id=int(player_id))


def _is_departed(server, player) -> bool:
    """True when ``player``'s id now belongs to a different roster object.

    A leaver's scoring event can drain after its numeric id was reused by a
    new joiner; the old score must not overwrite the new player's row.
    """
    players = getattr(server, "players", None)
    if not isinstance(players, dict):
        return False
    current = players.get(int(getattr(player, "id", -1)))
    return current is not None and current is not player


def send_player_score(server, player, *, reason: int | None = None) -> None:
    """Push ONE player's personal score to every client (SetScore type=PLAYER).
    Without this the per-player scoreboard column stays 0 no matter how many
    kills they get. Only peers that know the id receive it; nothing is sent
    for a departed player whose id has been reused."""
    from server.profile_stats import score_changed
    reason = REASON_KILL if reason is None else int(reason)
    score_changed(player, reason)
    if _is_departed(server, player):
        return
    broadcast_player_packet(
        server, player_score_packet(player, reason=reason), int(player.id)
    )


def send_team_score(server, team, *, reason: int | None = None) -> None:
    """Push one team's score bar to every client (SetScore type=TEAM)."""
    pkt = SetScore()
    pkt.type = SCORE_TEAM
    pkt.reason = REASON_KILL if reason is None else int(reason)
    pkt.specifier = internal_team_to_wire(team.id)
    pkt.value = int(team.score)
    server.broadcast(bytes(pkt.generate()))


def send_round_timer(server, seconds_remaining: float, *, reliable: bool = True) -> None:
    """Broadcast the HUD countdown (DisplayCountdown 84) — seconds REMAINING.

    The once-per-second refresh is the only steady reliable traffic in a
    quiet round, and one lost reliable packet holds every later packet on
    the channel (unreliable WorldUpdate rows included) until ENet
    retransmits it: remote players freeze for an RTT. The refresh carries
    an absolute value the HUD counts down from locally, so callers send it
    unreliably (sequenced: a stale refresh behind a newer one is dropped)
    and keep ``reliable=True`` for round starts and mode transitions.
    """
    pkt = DisplayCountdown()
    pkt.timer = hud_clock_seconds(seconds_remaining)
    server.broadcast(bytes(pkt.generate()), reliable=reliable)


def hud_clock_seconds(seconds_remaining: float) -> float:
    """The DisplayCountdown value: non-negative and never past the HUD's hour.

    See ``HUD_CLOCK_MAX_SECONDS``. Non-finite input shows 0 rather than NaN.
    """
    try:
        value = float(seconds_remaining)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return min(HUD_CLOCK_MAX_SECONDS, max(0.0, value))


def reveal_to(server, connection) -> None:
    """Replay absolute scores after the loading peer has its full roster."""
    known = getattr(connection, "known_player_lives", None)
    for player in server.players.values():
        if known is not None and int(player.id) not in known:
            continue
        connection.send(player_score_packet(player), reliable=True)
    for team in getattr(server, "teams", {}).values():
        pkt = SetScore()
        pkt.type, pkt.reason = SCORE_TEAM, int(C.NO_SCORE_REASON)
        pkt.specifier, pkt.value = internal_team_to_wire(team.id), int(team.score)
        connection.send(bytes(pkt.generate()), reliable=True)
    if bool(getattr(getattr(server, "mode", None), "ended", False)):
        from shared.bytes import ByteReader
        for data in getattr(server, "_game_stats_packets", ()):
            packet = GameStats(ByteReader(data[1:]))
            rows = [(player_id, award) for player_id, award in zip(packet.player_ids, packet.types)
                    if known is None or player_id in known]
            packet.noOfStats = len(rows)
            packet.player_ids = [player_id for player_id, _award in rows]
            packet.types = [award for _player_id, award in rows]
            connection.send(bytes(packet.generate()), reliable=True)


def reset_round_scores(server) -> None:
    """Reset only match counters; lifetime profile evidence remains cumulative."""
    from server.combat_scores import RoundCombatStats
    for player in server.players.values():
        player.score = player.kills = player.deaths = player.captures = 0
        player.kill_streak = 0
        player.round_combat_stats = RoundCombatStats()
        player.damage_contributions = {}
        state = getattr(player, "profile_stats", None)
        if state is not None:
            state.last_score = 0
        broadcast_player_packet(server, player_score_packet(player), int(player.id))
    bridge = getattr(server, "revival_master", None)
    reset_baselines = getattr(bridge, "reset_scoreboard_baselines", None)
    if callable(reset_baselines):
        reset_baselines()
    server._game_stats_sent = False
    server._game_stats_packets = ()


# Every retail GAME_STAT_TYPES id (0..29); the stock client renders all 30.
_AWARD_ORDER = tuple(sorted(int(stat) for stat in C.GAME_STAT_TYPES))
# A "streak" of one kill is not an achievement; it would only fill a row.
_AWARD_MINIMUM = {int(C.BIGGEST_KILL_STREAK): 2}
# The one "fewest" award: lowest shots fired among players with a real
# round (FEWEST_SHOTS_MIN_KILLS kills); combat_scores.record_shot counts.
_FEWEST_AWARDS = frozenset((int(C.FEWEST_SHOTS_FIRED),))


def _award_amount(player, award: int) -> int | None:
    """Round value of ``award`` for ``player``; None = not eligible."""
    state = getattr(player, "round_combat_stats", None)
    awards = getattr(state, "awards", None)
    if award in _FEWEST_AWARDS:
        from server.combat_scores import FEWEST_SHOTS_MIN_KILLS, SHOTS_MEASURE

        measures = getattr(state, "measures", None)
        kills = int(awards.get(C.MOST_KILLS, 0)) if isinstance(awards, dict) else 0
        shots = measures.get(SHOTS_MEASURE, 0.0) if isinstance(measures, dict) else 0.0
        if kills < FEWEST_SHOTS_MIN_KILLS or shots <= 0:
            return None
        return -int(shots)
    amount = int(awards.get(award, 0)) if isinstance(awards, dict) else 0
    return amount if amount >= _AWARD_MINIMUM.get(award, 1) else None


def _int_attr(player, name: str) -> int:
    try:
        return int(getattr(player, name, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _award_rng(server):
    rng = getattr(server, "_game_stats_rng", None)
    if rng is None:
        import random

        rng = random.Random()
    return rng


def _team_awards(server, team_id: int) -> list[tuple[int, int]]:
    """Choose at most three awards from all 30 retail stat types.

    Retail ships the 30 GAME_STAT_TYPES and the server-only
    NOOF_GAME_STATS_TO_SHOW (3), not the dedicated server's sampling
    policy.  A fixed priority would show Kills/Assists/Headshots every round,
    so the three rows are a random sample of the stat types someone on the
    team actually earned this round (inferred), listed in stat-id order.
    Nothing is fabricated: every value comes from counted round evidence.
    Ties go to the higher round score, then fewer deaths, then the lower
    player id. Only players still on the roster are eligible (a departed id
    cannot be resolved by the client).
    """
    try:
        team_id = int(team_id)
    except (TypeError, ValueError):
        return []
    players = []
    for player in server.players.values():
        try:
            if int(getattr(player, "team", -1)) == team_id:
                players.append(player)
        except (TypeError, ValueError):
            continue
    players.sort(key=lambda player: int(player.id))
    winners = []
    for award in _AWARD_ORDER:
        scored = []
        for player in players:
            amount = _award_amount(player, award)
            if amount is None:
                continue
            scored.append((
                amount,
                _int_attr(player, "score"),
                -_int_attr(player, "deaths"),
                -int(player.id),
            ))
        if scored:
            winners.append((-max(scored)[3], award))
    limit = int(C.NOOF_GAME_STATS_TO_SHOW)
    if len(winners) > limit:
        winners = _award_rng(server).sample(winners, limit)
    return sorted(winners, key=lambda row: row[1])


def _broadcast_game_stats_rows(server, pkt, data: bytes) -> None:
    """Send one GameStats column, omitting award rows a peer cannot resolve.

    Same per-connection filter as ``reveal_to``: a row naming an id the peer
    never received a CreatePlayer for would index a missing roster entry.
    """
    connections = getattr(server, "connections", None)
    if not isinstance(connections, dict):
        server.broadcast(data)
        return
    if getattr(server, "_stopping", False):
        return
    rows = list(zip(pkt.player_ids, pkt.types))
    for connection in tuple(connections.values()):
        if not bool(getattr(connection, "in_game", False)):
            continue
        known = getattr(connection, "known_player_lives", None)
        if known is None or all(player_id in known for player_id, _ in rows):
            connection.send(data, reliable=True)
            continue
        filtered = [(player_id, award) for player_id, award in rows
                    if player_id in known]
        copy = GameStats()
        copy.team_id = pkt.team_id
        copy.noOfStats = len(filtered)
        copy.player_ids = [player_id for player_id, _award in filtered]
        copy.types = [award for _player_id, award in filtered]
        connection.send(bytes(copy.generate()), reliable=True)


def broadcast_game_stats(server, winner: int | None = None) -> None:
    """Broadcast the end-of-round GameStats(67) leaderboard data to all
    in-game clients. The client already knows each player's score (from the
    per-player SetScore stream). This packet alone is safe in GameScene; do not
    pair it with a terminal screen packet during a same-map restart."""
    # The retail receiver appends each packet into its team column. A repeated
    # end callback must not duplicate award rows or reserve results twice.
    if bool(getattr(server, "_game_stats_sent", False)):
        return
    server._game_stats_sent = True
    packets = []
    for team_id in (TEAM1, TEAM2):
        awards = _team_awards(server, team_id)
        pkt = GameStats()
        pkt.team_id = int(team_id)
        pkt.noOfStats = len(awards)
        pkt.player_ids = [player_id for player_id, _award in awards]
        pkt.types = [award for _player_id, award in awards]
        data = bytes(pkt.generate())
        packets.append(data)
        _broadcast_game_stats_rows(server, pkt, data)
    server._game_stats_packets = tuple(packets)

    # Submit the same finished-round snapshot outside the simulation thread.
    # The bridge tracks per-player baselines, so same-map restarts add deltas
    # rather than re-uploading cumulative scoreboard values.
    revival_master = getattr(server, "revival_master", None)
    schedule_results = getattr(revival_master, "schedule_round_results", None)
    if callable(schedule_results):
        schedule_results(winner)


def show_game_stats(server) -> None:
    """Trigger the client's full-screen end-of-round stats screen
    (ShowGameStats 53). LIVE-VERIFIED: this is the packet that pops the
    scores/credits screen (the client renders it from the accumulated
    per-player SetScore stream + the level screenshot). This destroys the
    active GameScene and is only suitable when play will not resume in it."""
    server.broadcast(bytes(ShowGameStats().generate()))


def send_show_text_message(server, message_id: int, duration: float) -> None:
    """Set the retail scoreboard headline (ShowTextMessage 73).

    IDA (stock gameScene.pyd, body 0x101A0490): the client runs
    ``self.show_text_message(packet.message_id, packet.duration)``; the nine
    ids (shared.constants NEXT_MAP_MESSAGE..TEAM_SCORES_DRAW) pick the line
    ``hud.ViewScores.set_message``/``ViewGameStats.set_message`` draw from
    the HUD-local strings (TEAM_DEFEAT, GAME_DRAWN, ZOMBIE_WIN, ...). It is
    not a free-text overlay and does not change scenes."""
    pkt = ShowTextMessage()
    pkt.message_id = max(0, min(8, int(message_id)))
    pkt.duration = max(0.0, min(120.0, float(duration)))
    server.broadcast(bytes(pkt.generate()))


def force_show_scores(server, forced: bool) -> None:
    """Hold the scoreboard open or release it (ForceShowScores 72).

    IDA (stock gameScene.pyd, body 0x101A0300): the client runs
    ``self.force_show_scores(packet.forced)``, the same HUD toggle as the
    scores key. Retail holds it open for the end-of-round dwell; the
    GameScene stays alive, so it is safe for same-map restarts."""
    pkt = ForceShowScores()
    pkt.forced = 1 if forced else 0
    server.broadcast(bytes(pkt.generate()))


def send_lock_team(server, team_id: int, locked: bool) -> None:
    """Update one team's join lock on every settled client (LockTeam 79).

    IDA (stock gameScene.pyd, body 0x1019EB70): sets ``teams[id].locked`` and
    refreshes an open SelectTeam/SelectClass/ChangeTeam menu. StateData
    carries the same bit for joiners; this keeps players already in the
    scene in step when a mode phase changes the lock."""
    pkt = LockTeam()
    pkt.team_id = internal_team_to_wire(int(team_id))
    pkt.locked = 1 if locked else 0
    server.broadcast(bytes(pkt.generate()))


def send_team_lock_class(server, team_id: int, locked: bool) -> None:
    """Update one team's class lock on every settled client (TeamLockClass 80).

    IDA (stock gameScene.pyd, body 0x1019F880): sets ``teams[id].locked_class``;
    the HUD then refuses the class menu with ZOMBIE_OUTBREAK_CLASS_SELECT-
    style messages."""
    pkt = TeamLockClass()
    pkt.team_id = internal_team_to_wire(int(team_id))
    pkt.locked = 1 if locked else 0
    server.broadcast(bytes(pkt.generate()))


def send_map_ended(server) -> None:
    """Signal the map has ended (MapEnded 52). Sent alongside the stats
    screen so the client's has_map_ended state matches the StateData flag.
    This is terminal for the active GameScene."""
    server.broadcast(bytes(MapEnded().generate()))
