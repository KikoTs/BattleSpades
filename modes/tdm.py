"""
Team Deathmatch game mode.
Two teams fight for kills until score or time limit is reached.
"""

import logging
from typing import Optional, TYPE_CHECKING

from server import mode_data
import shared.constants as C
import shared.constants_gamemode as CG
from server.game_constants import KILL_HEADSHOT, KILL_MELEE, TEAM1, TEAM2

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player

logger = logging.getLogger(__name__)


class TDMMode(BaseMode):
    """
    Team Deathmatch mode.

    Rules:
    - Each cross-team kill scores one point for the killer's team.
    - First team to the score limit wins; otherwise the leader at the time
      limit wins.

    Scoring is driven by on_player_kill, which the server dispatches from
    Player.die() via the per-tick mode-event queue (queue_mode_event).
    """

    name = "Team Deathmatch"
    description = "Eliminate the enemy team to score points!"

    # Points per event.
    kill_points = int(CG.TDM_TEAM_SCORE_FOR_KILL)
    headshot_bonus = 0  # Optional custom-server override, not a retail default.

    def __init__(self, server):
        super().__init__(server)
        # Source the win threshold + clock from the single mode-data table so
        # the rules and the wire HUD limit can never disagree (the old
        # hardcoded 100 fought the wire default of 50). An explicit per-mode
        # config override (config.mode_score_limit, set by the [modes.tdm]
        # overlay) wins when present; the generic config.score_limit is a
        # CTF-era default and is NOT used for TDM.
        md = mode_data.get(server.config.game_mode)
        # [modes.tdm] overlay from config.toml wins over the mode-data default.
        overlay = getattr(server.config, "mode_settings", {}).get("tdm", {})
        self.score_limit = int(server.config.mode_rule(
            "tdm", "score_limit", "RULE_TDM_SCORE_TARGET"
        ))
        self.time_limit = server.config.configured_time_limit(
            "tdm", md.default_time_limit
        )
        self.kill_points = int(overlay.get("kill_points", self.kill_points))
        self.headshot_bonus = int(overlay.get("headshot_bonus", self.headshot_bonus))

    async def on_mode_start(self):
        """Start TDM mode."""
        await super().on_mode_start()

        # Reset team scores for a fresh match.
        for team in self.server.teams.values():
            team.reset()

        logger.info(
            "TDM mode started (score_limit=%d, time_limit=%.0fs)",
            self.score_limit, self.time_limit,
        )

    async def on_player_kill(self, killer: 'Player', victim: 'Player', kill_type: int):
        """Award team + personal points for a cross-team kill and check win."""
        # No scoring once the round has ended (during the stats screen / restart).
        if (self.ended or killer is victim or killer.team == victim.team
                or killer.team not in (TEAM1, TEAM2) or victim.team not in (TEAM1, TEAM2)):
            return
        from server.scoreboard import send_player_score, send_team_score

        points = self.kill_points
        if kill_type == KILL_HEADSHOT:
            points += self.headshot_bonus

        team = self.server.teams.get(killer.team)
        if team is None:
            return
        team.add_score(points)

        # Personal scoreboard: the client's per-player column. Award the
        # generic per-kill score (100, +50 headshot) so the leaderboard fills.
        # (killer.kills is already incremented in Player.die.)
        if kill_type == KILL_HEADSHOT:
            amount, reason = CG.GENERIC_SCORE_HEADSHOT, C.KILL_SCORE_HEADSHOT_REASON
        elif kill_type == KILL_MELEE:
            amount, reason = CG.GENERIC_SCORE_MELEE, C.KILL_SCORE_MELEE_REASON
        else:
            amount, reason = CG.GENERIC_SCORE_KILL, C.KILL_SCORE_REASON
        killer.score += int(amount)
        send_player_score(self.server, killer, reason=int(reason))

        # Audio stingers: a "good" cue to the killer, a "bad" cue to the victim.
        from server.audio import play_sound_to, SND_EVENT_POSITIVE, SND_EVENT_NEGATIVE
        play_sound_to(killer, SND_EVENT_POSITIVE, volume=0.6)
        if victim.connection is not None:
            play_sound_to(victim, SND_EVENT_NEGATIVE, volume=0.6)

        # Team score bar (SetScore type=TEAM). NEVER re-broadcast StateData —
        # the compiled client re-inits the scene on a mid-game StateData
        # (reloads prefabs / UGC palette) and crashes.
        send_team_score(self.server, team)

        if team.score >= self.score_limit:
            await self._end_by_score(killer.team)

    async def on_player_death(self, player, killer, kill_type: int) -> None:
        """Apply retail personal penalties without changing the team kill count."""
        if self.ended or kill_type in {
            C.FORCED_TEAM_CHANGE_KILL, C.TEAM_CHANGE_KILL, C.CLASS_CHANGE_KILL
        }:
            return
        from server.scoreboard import send_player_score
        if killer is player or killer is None:
            player.score += int(CG.GENERIC_SCORE_SUICIDE)
            send_player_score(self.server, player, reason=int(C.SUICIDE_SCORE_REASON))
        elif killer.team == player.team:
            killer.score += int(CG.GENERIC_SCORE_TEAMKILL)
            send_player_score(self.server, killer, reason=int(C.KILL_SCORE_TEAMKILL_REASON))

    async def on_tick(self, tick: int):
        """Periodic lead announcements."""
        await super().on_tick(tick)
        if self.ended:
            return

        # Every 60s, announce the lead.
        if tick % (60 * self.server.tick_rate) == 0 and tick > 0:
            blue = self.server.teams[TEAM1].score
            green = self.server.teams[TEAM2].score
            if blue != green:
                leader = TEAM1 if blue > green else TEAM2
                name = self.server.teams[leader].name
                await self.broadcast_message(f"{name} leads by {abs(blue - green)} points!")
            else:
                await self.broadcast_message("Teams are tied!")
