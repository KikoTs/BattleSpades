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
                           ShowGameStats, MapEnded)
from server.connection import internal_team_to_wire

SCORE_TEAM = int(C.SCORE.TEAM)
SCORE_PLAYER = int(C.SCORE.PLAYER)
REASON_KILL = int(getattr(C.SCORE_REASON, "KILL_SCORE_REASON", 1))
REASON_SUICIDE = int(getattr(C.SCORE_REASON, "SUICIDE_SCORE_REASON", 2))


def player_score_packet(player, *, reason: int = 0) -> bytes:
    """Encode an absolute snapshot without awarding profile credit."""
    pkt = SetScore()
    pkt.type, pkt.reason = SCORE_PLAYER, int(reason)
    pkt.specifier, pkt.value = int(player.id), int(getattr(player, "score", 0))
    return bytes(pkt.generate())


def send_player_score(server, player, *, reason: int | None = None) -> None:
    """Push ONE player's personal score to every client (SetScore type=PLAYER).
    Without this the per-player scoreboard column stays 0 no matter how many
    kills they get."""
    from server.profile_stats import score_changed
    score_changed(player, REASON_KILL if reason is None else int(reason))
    server.broadcast(player_score_packet(player, reason=REASON_KILL if reason is None else int(reason)))


def send_team_score(server, team, *, reason: int | None = None) -> None:
    """Push one team's score bar to every client (SetScore type=TEAM)."""
    pkt = SetScore()
    pkt.type = SCORE_TEAM
    pkt.reason = REASON_KILL if reason is None else int(reason)
    pkt.specifier = internal_team_to_wire(team.id)
    pkt.value = int(team.score)
    server.broadcast(bytes(pkt.generate()))


def send_round_timer(server, seconds_remaining: float) -> None:
    """Broadcast the HUD countdown (DisplayCountdown 84) — seconds REMAINING."""
    pkt = DisplayCountdown()
    pkt.timer = float(max(0.0, seconds_remaining))
    server.broadcast(bytes(pkt.generate()))


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
        server.broadcast(player_score_packet(player))
    bridge = getattr(server, "revival_master", None)
    reset_baselines = getattr(bridge, "reset_scoreboard_baselines", None)
    if callable(reset_baselines):
        reset_baselines()
    server._game_stats_sent = False
    server._game_stats_packets = ()


_AWARD_ORDER = (
    int(C.MOST_KILLS), int(C.MOST_ASSISTS), int(C.MOST_HEADSHOTS),
    int(C.MOST_MELEE_KILLS), int(C.BIGGEST_KILL_STREAK), int(C.MOST_SUICIDES),
)


def _team_awards(server, team_id: int) -> list[tuple[int, int]]:
    """Choose at most three supported awards, with stable player-id ties.

    Retail exposes award ids and the three-row limit, not the server's award
    sampling policy. Never fabricate unknown movement/objective statistics.
    """
    players = sorted(
        (player for player in server.players.values()
         if int(getattr(player, "team", -1)) == team_id),
        key=lambda player: int(player.id),
    )
    result = []
    for award in _AWARD_ORDER:
        scored = [(int(getattr(getattr(player, "round_combat_stats", None),
                               "awards", {}).get(award, 0)), int(player.id))
                  for player in players]
        if not scored:
            continue
        amount, player_id = max(scored, key=lambda row: (row[0], -row[1]))
        if amount > 0:
            result.append((player_id, award))
        if len(result) >= int(C.NOOF_GAME_STATS_TO_SHOW):
            break
    return result


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
        server.broadcast(data)
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


def send_map_ended(server) -> None:
    """Signal the map has ended (MapEnded 52). Sent alongside the stats
    screen so the client's has_map_ended state matches the StateData flag.
    This is terminal for the active GameScene."""
    server.broadcast(bytes(MapEnded().generate()))
