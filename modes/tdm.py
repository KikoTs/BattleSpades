"""
Team Deathmatch game mode.
Two teams fight for kills until score or time limit is reached.
"""

import logging
from typing import Optional, TYPE_CHECKING

import shared.constants as C

from server import mode_data
from server.game_constants import KILL_HEADSHOT, TEAM_NEUTRAL, TEAM1, TEAM2

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player

logger = logging.getLogger(__name__)


class TDMMode(BaseMode):
    """
    Team Deathmatch mode.

    Rules:
    - Each cross-team kill scores a point for the killer's team (+1 headshot).
    - First team to the score limit wins; otherwise the leader at the time
      limit wins.

    Scoring is driven by on_player_kill, which the server dispatches from
    Player.die() via the per-tick mode-event queue (queue_mode_event).
    """

    name = "Team Deathmatch"
    description = "Eliminate the enemy team to score points!"

    # Points per event.
    kill_points = 1
    headshot_bonus = 1

    def __init__(self, server):
        super().__init__(server)
        # Source the win threshold + clock from the single mode-data table so
        # the rules and the wire HUD limit can never disagree (the old
        # hardcoded 100 fought the wire default of 50). An explicit per-mode
        # config override (config.mode_score_limit, set by the [modes.tdm]
        # overlay) wins when present; the generic config.score_limit is a
        # CTF-era default and is NOT used for TDM.
        md = mode_data.get(server.config.game_mode)
        override = getattr(server.config, "mode_score_limit", None)
        self.score_limit = int(override) if override else int(md.default_score_limit)
        self.time_limit = float(md.default_time_limit)

    async def on_mode_start(self):
        """Start TDM mode."""
        await super().on_mode_start()

        # Reset team scores for a fresh match.
        for team in self.server.teams.values():
            team.reset()

        self._place_crates()

        logger.info(
            "TDM mode started (score_limit=%d, time_limit=%.0fs)",
            self.score_limit, self.time_limit,
        )

    def _place_crates(self):
        """Spawn the map's ammo/health crates: a pair near each team's base
        plus a couple at midfield, all on dry ground (reusing the spawn
        anchoring so they never float over water). Registered + broadcast as
        neutral entities the client renders and (later) players restock from."""
        wm = getattr(self.server, "world_manager", None)
        reg = getattr(self.server, "entity_registry", None)
        if wm is None or reg is None:
            return
        reg.clear()

        spots = []
        for team in (TEAM1, TEAM2):
            bx, by, _bz = wm.team_base_anchor(team)
            # Two crates a few blocks apart near the base.
            spots.append((bx + 3.0, by))
            spots.append((bx - 3.0, by))
        # Midfield neutral crates.
        spots.append((256.0, 256.0))
        spots.append((260.0, 260.0))

        # Alternate ammo / health.
        types = [C.AMMO_CRATE, C.HEALTH_CRATE]
        placed = 0
        for i, (sx, sy) in enumerate(spots):
            x, y, z = wm.dry_ground_anchor(sx, sy)
            etype = types[i % 2]
            kind = "ammo" if etype == C.AMMO_CRATE else "health"
            ent = reg.place(etype, x, y, z, state=TEAM_NEUTRAL, kind=kind)
            # Only put crates on the wire once the Entity format is verified
            # against the compiled client (a mismatch crashes it). Registered
            # server-side either way.
            if getattr(self.server.config, "entities_wire_ready", False):
                self.server.broadcast_create_entity(ent)
            placed += 1
        logger.info("TDM placed %d map crates%s", placed,
                    "" if getattr(self.server.config, "entities_wire_ready", False)
                    else " (not yet on wire — Entity format unverified)")

    async def on_player_kill(self, killer: 'Player', victim: 'Player', kill_type: int):
        """Award team points for a cross-team kill and check the win."""
        points = self.kill_points
        if kill_type == KILL_HEADSHOT:
            points += self.headshot_bonus

        team = self.server.teams.get(killer.team)
        if team is None:
            return
        team.add_score(points)

        # Push the new score with the lightweight SetScore(85) packet. NEVER
        # re-broadcast StateData here — the compiled client re-inits the scene
        # on a mid-game StateData (reloads prefabs / UGC palette) and crashes.
        self.server.broadcast_set_score(team)

        if team.score >= self.score_limit:
            await self._end_by_score(killer.team)

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
