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
from shared.packet import (GameStats, DisplayCountdown, SetScore,
                           ShowGameStats, MapEnded)
from server.connection import internal_team_to_wire

SCORE_TEAM = int(C.SCORE.TEAM)
SCORE_PLAYER = int(C.SCORE.PLAYER)
REASON_KILL = int(getattr(C.SCORE_REASON, "KILL_SCORE_REASON", 1))
REASON_SUICIDE = int(getattr(C.SCORE_REASON, "SUICIDE_SCORE_REASON", 2))


def send_player_score(server, player, *, reason: int | None = None) -> None:
    """Push ONE player's personal score to every client (SetScore type=PLAYER).
    Without this the per-player scoreboard column stays 0 no matter how many
    kills they get."""
    pkt = SetScore()
    pkt.type = SCORE_PLAYER
    pkt.reason = REASON_KILL if reason is None else int(reason)
    pkt.specifier = int(player.id)
    pkt.value = int(getattr(player, "score", 0))
    server.broadcast(bytes(pkt.generate()))


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


def broadcast_game_stats(server, winner: int | None = None) -> None:
    """Broadcast the end-of-round GameStats(67) leaderboard data to all
    in-game clients. The client already knows each player's score (from the
    per-player SetScore stream). This packet alone is safe in GameScene; do not
    pair it with a terminal screen packet during a same-map restart."""
    players = [p for p in server.players.values() if getattr(p, "spawned", False) or True]

    pkt = GameStats()
    pkt.noOfStats = len(players)
    # team_id selects which team's column heads the widget; the client shows
    # both teams regardless. Use the winner (or TEAM1) as the header team.
    pkt.team_id = int(internal_team_to_wire(winner)) if winner is not None else 0
    pkt.player_ids = [int(p.id) for p in players]
    # stat_type 0 = "kills" column; the client labels the row from this.
    pkt.types = [0 for _ in players]
    server.broadcast(bytes(pkt.generate()))

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
