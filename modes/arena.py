"""
Arena game mode.
Round-based elimination mode - last team standing wins the round.
"""

import time
import logging
from typing import Optional, List, TYPE_CHECKING

from server.game_constants import TEAM1, TEAM2

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player

logger = logging.getLogger(__name__)

_PLAYABLE_TEAMS = (TEAM1, TEAM2)


class ArenaMode(BaseMode):
    """
    Arena mode.

    Rules:
    - Round-based elimination
    - No respawning during rounds
    - Last team with players alive wins the round
    - Win majority of rounds to win the match
    """

    name = "Arena"
    description = "Eliminate all enemies to win the round!"
    mode_code = "arena"

    rounds_to_win = 5
    round_time_limit = 180  # 3 minutes per round
    round_start_delay = 5  # Seconds before round starts
    round_end_delay = 5    # Seconds after round ends

    def __init__(self, server):
        super().__init__(server)

        # [modes.arena] overlay (non-retail extension mode). The match clock
        # defaults to none: rounds_to_win ends the match.
        config = getattr(server, "config", None)
        settings = getattr(config, "mode_settings", None)
        overlay = dict(settings.get(self.mode_code, {}) or {}) if isinstance(settings, dict) else {}
        self.rounds_to_win = max(1, int(overlay.get(
            "rounds_to_win", overlay.get("score_limit", type(self).rounds_to_win)
        )))
        self.round_time_limit = max(1.0, float(overlay.get(
            "round_time_limit", type(self).round_time_limit
        )))
        self.time_limit = max(0.0, float(overlay.get("time_limit", 0.0)))
        # StateData/HUD score target: team.score mirrors round wins.
        self.score_limit = self.rounds_to_win

        # Round state
        self.current_round = 0
        self.round_wins = {TEAM1: 0, TEAM2: 0}

        self.round_started = False
        self.round_ended = False
        self.round_start_time = 0.0

        # Pre-round countdown
        self.countdown_started = False
        self.countdown_end_time = 0.0
        self.next_round_at: float | None = None
        # True while the countdown is held because a team has nobody alive
        # to fight; normal respawns stay on during this warm-up.
        self.waiting_for_players = False

        # Players alive this round
        self.alive_players: List['Player'] = []

    async def on_mode_start(self):
        """Start arena mode (also runs on every in-place match restart)."""
        await super().on_mode_start()
        self.current_round = 0
        self.round_wins = {TEAM1: 0, TEAM2: 0}
        for team_id in _PLAYABLE_TEAMS:
            team = self.server.teams.get(team_id)
            if team is not None:
                team.score = 0
        # Callers own the bodies here: a same-map restart respawns everyone
        # right after on_mode_start, and a map/mode rollover spawns through
        # the join path. Spawning here too sent a second CreatePlayer.
        await self._start_new_round(respawn=False)
        logger.info("Arena mode started")

    def can_player_respawn(self, player: 'Player') -> bool:
        """No timed respawns once FIGHT! has been called.

        Dead players wait for the next round, whose start respawns everyone
        through the ordinary CreatePlayer/restock path. The post-round
        intermission is included so nobody gets a second body 5 s later.
        """
        return not self.round_started

    def respawn_time_for(self, player: 'Player') -> float:
        """KillAction's respawn countdown must not promise a 5 s respawn
        during a live round: the dead fighter returns when the next round
        starts. Advertise the longest remaining wait (round clock plus the
        post-round delay); an early elimination only shortens it."""
        if not self.round_started:
            return float(getattr(self.server.config, "respawn_time", 0.0))
        now = time.time()
        if self.next_round_at is not None:
            remaining = self.next_round_at - now
        elif self.round_ended:
            remaining = float(self.round_end_delay)
        else:
            remaining = (
                self.round_time_limit - (now - self.round_start_time)
                + float(self.round_end_delay)
            )
        return max(0.0, min(255.0, float(remaining)))

    async def _start_new_round(self, respawn: bool = True):
        """Start a new round."""
        self.current_round += 1
        self.round_started = False
        self.round_ended = False
        self.next_round_at = None
        self.alive_players = []
        self.waiting_for_players = False

        await self.broadcast_message(f"Round {self.current_round} - Get ready!")

        if respawn:
            # Server respawn path: pending class/loadout, CreatePlayer to
            # every client, restock and on_player_spawn.
            for player in list(self.server.players.values()):
                if player.team not in _PLAYABLE_TEAMS:
                    continue
                try:
                    self.server.respawn_player(player)
                except Exception:
                    logger.debug("arena respawn failed for %s",
                                 getattr(player, "id", "?"), exc_info=True)

        # Start countdown
        self.countdown_started = True
        self.countdown_end_time = time.time() + self.round_start_delay

    async def on_tick(self, tick: int):
        """Check round state."""
        # Base clock: music, match time limit, countdown cues, map vote.
        await super().on_tick(tick)
        if self.ended:
            return
        current_time = time.time()
        if self.next_round_at is not None:
            if current_time >= self.next_round_at:
                await self._start_new_round()
            return

        # Handle countdown
        if self.countdown_started and not self.round_started:
            if not self._both_teams_ready():
                # FIGHT with an empty side would hand the round to the
                # other team (or leave a first joiner dead for the whole
                # round). Hold the countdown with respawns on until both
                # teams have somebody alive.
                if not self.waiting_for_players:
                    self.waiting_for_players = True
                    await self.broadcast_message("Waiting for players on both teams...")
                self.countdown_end_time = current_time + self.round_start_delay
                return
            if self.waiting_for_players:
                self.waiting_for_players = False
                self.countdown_end_time = current_time + self.round_start_delay
            remaining = self.countdown_end_time - current_time

            if remaining <= 0:
                await self._begin_round()
            elif tick % self.server.tick_rate == 0:
                seconds = int(remaining)
                if seconds > 0:
                    await self.broadcast_message(f"{seconds}...")

        # Check round time limit
        if self.round_started and not self.round_ended:
            round_time = current_time - self.round_start_time

            if round_time >= self.round_time_limit:
                # Round timed out - draw or team with more alive wins
                await self._end_round_timeout()

    async def _begin_round(self):
        """Actually start the round (after countdown)."""
        self.round_started = True
        self.countdown_started = False
        self.round_start_time = time.time()
        # The mode starts before bot/human joins. Freeze the actual roster at
        # FIGHT, otherwise the first death can eliminate an apparently empty
        # side even while its newly joined teammates are alive.
        self.alive_players = [player for player in self.server.players.values()
                              if player.team in _PLAYABLE_TEAMS
                              and player.alive and player.spawned]

        await self.broadcast_message("FIGHT!")
        logger.info(f"Round {self.current_round} started")

    def _both_teams_ready(self) -> bool:
        """True when each playable team has a live, spawned body."""
        ready = {TEAM1: False, TEAM2: False}
        for player in list(getattr(self.server, "players", {}).values()):
            team = getattr(player, "team", None)
            if (
                team in ready
                and bool(getattr(player, "alive", False))
                and bool(getattr(player, "spawned", False))
            ):
                ready[team] = True
        return all(ready.values())

    def bot_retire_safe(self, bot) -> bool:
        """A live fighter in a live round decides it when retired."""
        if not self.round_started or self.round_ended or self.ended:
            return True
        return not (
            bool(getattr(bot, "alive", False))
            and any(player is bot for player in self.alive_players)
        )

    def _team_alive_counts(self) -> dict:
        players = getattr(self.server, "players", {})
        team_alive = {TEAM1: 0, TEAM2: 0}
        for p in self.alive_players:
            # A departed player's id may already be reused by a newcomer.
            if players.get(getattr(p, "id", None)) is not p:
                continue
            if p.alive and p.team in team_alive:
                team_alive[p.team] += 1
        return team_alive

    async def _check_elimination(self) -> None:
        if not self.round_started or self.round_ended or self.ended:
            return
        team_alive = self._team_alive_counts()
        if team_alive[TEAM1] == 0 and team_alive[TEAM2] > 0:
            await self._end_round(winner=TEAM2)
        elif team_alive[TEAM2] == 0 and team_alive[TEAM1] > 0:
            await self._end_round(winner=TEAM1)
        elif team_alive[TEAM1] == 0 and team_alive[TEAM2] == 0:
            await self._end_round(winner=None)  # Draw

    async def on_player_death(self, player: 'Player', killer: Optional['Player'], kill_type: int):
        """Handle player death - check for round end."""
        # Generic retail suicide/team-kill penalty (BaseMode contract).
        await super().on_player_death(player, killer, kill_type)
        if not self.round_started or self.round_ended:
            return

        # Remove from alive list
        if player in self.alive_players:
            self.alive_players.remove(player)
        await self._check_elimination()

    async def on_player_leave(self, player: 'Player'):
        """A departing fighter can decide the round (last one on a side)."""
        self.alive_players = [p for p in self.alive_players if p is not player]
        await self._check_elimination()

    async def _end_round(self, winner: Optional[int]):
        """End the current round."""
        if self.round_ended or self.ended:
            return
        self.round_ended = True

        if winner is not None:
            self.round_wins[winner] += 1
            team_name = self.server.teams[winner].name
            await self.broadcast_message(f"{team_name} wins the round!")
            logger.info(f"Round {self.current_round} won by team {winner}")
        else:
            await self.broadcast_message("Round draw!")
            logger.info(f"Round {self.current_round} was a draw")

        # The HUD team bars show round wins.
        from server.scoreboard import send_team_score
        for team_id in _PLAYABLE_TEAMS:
            team = self.server.teams[team_id]
            team.score = int(self.round_wins[team_id])
            send_team_score(self.server, team)

        # Show round score
        team1_name = self.server.teams[TEAM1].name
        team2_name = self.server.teams[TEAM2].name
        await self.broadcast_message(
            f"Score - {team1_name}: {self.round_wins[TEAM1]} | {team2_name}: {self.round_wins[TEAM2]}"
        )

        # Check for match win (TEAM1 first: deterministic order)
        for team_id in _PLAYABLE_TEAMS:
            if self.round_wins[team_id] >= self.rounds_to_win:
                await self._end_match(team_id)
                return

        # Start next round after delay
        await self._schedule_next_round()

    async def _end_round_timeout(self):
        """Handle round ending due to time limit."""
        # Team with more alive players wins
        team_alive = self._team_alive_counts()

        if team_alive[TEAM1] > team_alive[TEAM2]:
            winner = TEAM1
        elif team_alive[TEAM2] > team_alive[TEAM1]:
            winner = TEAM2
        else:
            winner = None

        await self.broadcast_message("Time's up!")
        await self._end_round(winner)

    async def _schedule_next_round(self):
        """Schedule the next round."""
        if not self.ended:
            # on_tick is awaited by the authoritative simulation. Sleeping
            # here suspends physics/network progress for the whole interval.
            self.next_round_at = time.time() + self.round_end_delay

    async def _end_match(self, winner: int):
        """End the entire match through the shared once-only score end."""
        self.next_round_at = None
        await self._end_by_score(winner)

    def modify_incoming_damage(self, player: 'Player', amount: int,
                               source: Optional['Player'], kill_type: int) -> int:
        """No damage during the countdown/warm-up or after the round ends.

        Player.damage consults this hook (``on_player_damage`` is never
        called by the server).
        """
        if not self.round_started or self.round_ended:
            return 0
        return int(amount)

    async def on_player_damage(self, player: 'Player', attacker: Optional['Player'],
                               damage: int, kill_type: int) -> int:
        """Compatibility wrapper around ``modify_incoming_damage``."""
        return self.modify_incoming_damage(player, damage, attacker, kill_type)
