"""VIP mode: protect each team's boss, then eliminate the survivors.

The retail client already contains the gangster and boss character models. The
server owns the round state machine, promotes one player per team to the
team-specific boss class, and uses ChangePlayer action 8 for the native crown
marker visible through terrain.
"""

from __future__ import annotations

import asyncio
from enum import Enum, auto
import logging
import random
import time
from typing import TYPE_CHECKING

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.class_selection import ClassSelection, normalize_class_selection
from server.game_constants import TEAM1, TEAM2

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player


logger = logging.getLogger(__name__)

_PLAYABLE_TEAMS = (TEAM1, TEAM2)
_ORDINARY_GANGSTERS = tuple(int(value) for value in C.MAFIA_TEAM_CLASSES)


class VIPPhase(Enum):
    """Authoritative phase of one VIP sub-round."""

    WAITING = auto()
    SELECTING = auto()
    ACTIVE = auto()
    INTERMISSION = auto()


def _other_team(team: int) -> int:
    return TEAM2 if team == TEAM1 else TEAM1


class VIPMode(BaseMode):
    """Run the retail gangster VIP rules as a bounded state machine.

    Both teams respawn until their own VIP dies. That team then enters sudden
    death and its remaining lives become permanent. The opposing team keeps
    respawning while its VIP remains alive. Eliminating every survivor on a
    VIP-less team awards one round; the first team to the configured number of
    rounds wins the match.
    """

    name = "VIP"
    description = "Protect your VIP, kill theirs, then mop up the survivors!"

    def __init__(self, server) -> None:
        super().__init__(server)
        md = mode_data.get("vip")
        overlay = getattr(server.config, "mode_settings", {}).get("vip", {})
        self.score_limit = int(server.config.mode_rule(
            "vip", "score_limit", "RULE_VIP_NOOF_ROUNDS"
        ))
        self.time_limit = server.config.configured_time_limit(
            "vip", md.default_time_limit
        )
        self.selection_delay = float(overlay.get(
            "selection_delay", CG.VIP_SELECTION_DELAY
        ))
        self.round_intermission = float(overlay.get("round_intermission", 7.0))
        self.minimum_team_size = int(overlay.get(
            "minimum_team_size", CG.VIP_MINIMUM_TEAM_SIZE_TO_START
        ))
        self.sudden_death_enabled = bool(server.config.mode_rule(
            "vip", "sudden_death", "RULE_ENABLE_SUDDEN_DEATH"
        ))
        self.vip_health_multiplier = float(server.config.mode_rule(
            "vip", "vip_health_multiplier", "RULE_VIP_HEALTH"
        ))

        self.phase = VIPPhase.WAITING
        self.vips: dict[int, Player | None] = {TEAM1: None, TEAM2: None}
        self.vip_alive: dict[int, bool] = {TEAM1: False, TEAM2: False}
        self.respawn_enabled: dict[int, bool] = {TEAM1: True, TEAM2: True}
        self.selection_deadline: float | None = None
        self._round_task: asyncio.Task | None = None

    async def on_mode_start(self) -> None:
        """Reset the full match and wait until both teams can choose a VIP."""
        await self._cancel_round_task()
        self._clear_vip_markers()
        await super().on_mode_start()
        for team in self.server.teams.values():
            team.reset()
        # BaseMode's same-map restart respawns everybody immediately after
        # this hook returns. Demote last match's bosses first so that respawn
        # cannot publish a stale VIP class before the new selection phase.
        for player in list(self.server.players.values()):
            if player.team in _PLAYABLE_TEAMS and player.connection is not None:
                player.apply_class_selection(self._ordinary_selection(player))
        await self._begin_round(reset_players=False)
        logger.info(
            "VIP mode started (rounds=%d selection=%.1fs intermission=%.1fs)",
            self.score_limit,
            self.selection_delay,
            self.round_intermission,
        )

    async def deactivate(self) -> None:
        """Cancel a pending sub-round before a map or mode rollover."""
        await self._cancel_round_task()
        self._clear_vip_markers()
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        """Advance selection and elimination checks on the gameplay tick."""
        await super().on_tick(tick)
        if self.ended or self.phase is VIPPhase.INTERMISSION:
            return
        now = time.time()
        if self.phase is VIPPhase.WAITING:
            await self._arm_selection_if_ready(now)
        if (
            self.phase is VIPPhase.SELECTING
            and self.selection_deadline is not None
            and now >= self.selection_deadline
        ):
            await self._select_vips()
        if self.phase is VIPPhase.ACTIVE:
            await self._check_team_elimination()

    async def on_player_join(self, player: Player) -> None:
        """Start the selection countdown once both teams have a player."""
        if self.phase in (VIPPhase.WAITING, VIPPhase.SELECTING):
            await self._arm_selection_if_ready(time.time())

    async def on_player_death(
        self,
        player: Player,
        killer: Player | None,
        kill_type: int,
    ) -> None:
        """Lock a VIP's team out of respawns and test for elimination."""
        team = int(getattr(player, "team", -1))
        if (
            self.phase is VIPPhase.ACTIVE
            and team in _PLAYABLE_TEAMS
            and self.vips.get(team) is player
            and self.vip_alive[team]
        ):
            await self._kill_vip(team, player, killer)
        await self._check_team_elimination()

    async def on_player_leave(self, player: Player) -> None:
        """Treat a VIP disconnect as a death so quitting cannot save a team."""
        team = int(getattr(player, "team", -1))
        if (
            self.phase is VIPPhase.ACTIVE
            and team in _PLAYABLE_TEAMS
            and self.vips.get(team) is player
            and self.vip_alive[team]
        ):
            await self._kill_vip(team, player, killer=None)
        await self._check_team_elimination()

    async def on_player_team_change(
        self,
        player: Player,
        old_team: int,
        new_team: int,
    ) -> None:
        """Re-evaluate a sudden-death team after a regular member leaves it."""
        if old_team in _PLAYABLE_TEAMS:
            await self._check_team_elimination()
        if self.phase is VIPPhase.WAITING:
            await self._arm_selection_if_ready(time.time())

    def prepare_join_selection(
        self,
        team: int,
        selection: ClassSelection,
    ) -> ClassSelection:
        """Coerce an untrusted join selection to an ordinary gangster body."""
        if team not in _PLAYABLE_TEAMS:
            return selection
        class_id = int(selection.class_id)
        if class_id not in _ORDINARY_GANGSTERS:
            class_id = random.choice(_ORDINARY_GANGSTERS)
        return normalize_class_selection(
            class_id,
            selection.loadout,
            selection.prefabs,
            selection.ugc_tools,
        )

    def allows_class_selection(self, player: Player, selection: ClassSelection) -> bool:
        """Reject mid-life class packets; VIP owns gangster/boss assignment."""
        return False

    def allows_team_change(self, player: Player, new_team: int) -> bool:
        """Prevent a selected VIP from escaping sudden death by switching."""
        team = int(getattr(player, "team", -1))
        return not (
            team in _PLAYABLE_TEAMS
            and self.vips.get(team) is player
            and self.vip_alive.get(team, False)
        )

    def can_player_respawn(self, player: Player) -> bool:
        """Return the per-team respawn permission consumed by RoundLifecycle."""
        team = int(getattr(player, "team", -1))
        return (
            not self.ended
            and self.phase is not VIPPhase.INTERMISSION
            and self.respawn_enabled.get(team, False)
        )

    def respawn_time_for(self, player: Player) -> float:
        """Expose zero respawn time to a permanently dead retail client."""
        if not self.can_player_respawn(player):
            return 0.0
        return float(self.server.config.respawn_time)

    def modify_incoming_damage(
        self,
        player: Player,
        amount: int,
        source: Player | None,
        kill_type: int,
    ) -> int:
        """Apply the recovered 0.5 incoming-damage multiplier to live VIPs."""
        team = int(getattr(player, "team", -1))
        if (
            team in _PLAYABLE_TEAMS
            and self.vips.get(team) is player
            and self.vip_alive.get(team, False)
        ):
            reduced = int(round(float(amount) * float(C.VIP_DAMAGE_MULTIPLIER)))
            return max(1, reduced) if amount > 0 else 0
        return int(amount)

    def get_spawn_point(self, player: Player) -> tuple[float, float, float]:
        """Spawn gangster teams at their authored/dry base anchors."""
        world = self.server.world_manager
        base_anchor = getattr(world, "team_base_anchor", None)
        if callable(base_anchor) and player.team in _PLAYABLE_TEAMS:
            return tuple(float(value) for value in base_anchor(player.team))
        return tuple(float(value) for value in world.get_spawn_point(player.team))

    def reveal_to(self, connection) -> None:
        """Replay live VIP crown markers to a joining GameScene."""
        for team in _PLAYABLE_TEAMS:
            vip = self.vips.get(team)
            if vip is not None and self.vip_alive.get(team, False):
                self._set_vip_marker(vip, True, connection=connection)

    async def _begin_round(self, *, reset_players: bool) -> None:
        """Clear prior bosses, restore respawns, and arm the next selection."""
        self._clear_vip_markers()
        self.vips = {TEAM1: None, TEAM2: None}
        self.vip_alive = {TEAM1: False, TEAM2: False}
        self.respawn_enabled = {TEAM1: True, TEAM2: True}
        self.selection_deadline = None
        self.phase = VIPPhase.WAITING

        if reset_players:
            for player in list(self.server.players.values()):
                if player.team not in _PLAYABLE_TEAMS or player.connection is None:
                    continue
                player.apply_class_selection(self._ordinary_selection(player))
                self.server.respawn_player(player)

        await self._arm_selection_if_ready(time.time())

    def _ordinary_selection(self, player: Player) -> ClassSelection:
        """Return one legal ordinary gangster selection for a new sub-round."""
        class_id = int(getattr(player, "class_id", -1))
        if class_id not in _ORDINARY_GANGSTERS:
            class_id = random.choice(_ORDINARY_GANGSTERS)
        return normalize_class_selection(
            class_id,
            getattr(player, "loadout", ()) or (),
            getattr(player, "prefabs", ()) or (),
            getattr(player, "ugc_tools", ()) or (),
        )

    def _team_candidates(self, team: int) -> list[Player]:
        return [
            player for player in self.server.players.values()
            if player.team == team and player.connection is not None
        ]

    def _teams_ready(self) -> bool:
        return all(
            len(self._team_candidates(team)) >= self.minimum_team_size
            for team in _PLAYABLE_TEAMS
        )

    async def _arm_selection_if_ready(self, now: float) -> None:
        if self.phase not in (VIPPhase.WAITING, VIPPhase.SELECTING):
            return
        if not self._teams_ready():
            self.phase = VIPPhase.WAITING
            self.selection_deadline = None
            return
        if self.phase is VIPPhase.WAITING:
            self.phase = VIPPhase.SELECTING
            self.selection_deadline = now + max(0.0, self.selection_delay)
            await self.broadcast_message("Choosing VIPs...")

    async def _select_vips(self) -> None:
        """Promote exactly one player per team and publish native markers."""
        candidates = {team: self._team_candidates(team) for team in _PLAYABLE_TEAMS}
        if any(len(values) < self.minimum_team_size for values in candidates.values()):
            self.phase = VIPPhase.WAITING
            self.selection_deadline = None
            return

        selected = {team: random.choice(candidates[team]) for team in _PLAYABLE_TEAMS}
        self.vips = selected
        self.vip_alive = {TEAM1: True, TEAM2: True}
        self.respawn_enabled = {TEAM1: True, TEAM2: True}
        self.phase = VIPPhase.ACTIVE
        self.selection_deadline = None

        for team in _PLAYABLE_TEAMS:
            vip = selected[team]
            vip_class = int(C.MAFIA_VIPS[team])
            vip.apply_class_selection(normalize_class_selection(vip_class))
            self.server.respawn_player(vip)
            from server.game_constants import MAX_HEALTH
            from shared.packet import SetHP

            vip.health = max(1, int(round(
                MAX_HEALTH * self.vip_health_multiplier
            )))
            if vip.connection is not None:
                packet = SetHP()
                packet.hp = vip.health
                packet.damage_type = 0
                packet.source_x, packet.source_y, packet.source_z = getattr(
                    vip, "position", (0.0, 0.0, 0.0)
                )
                vip.connection.send(bytes(packet.generate()))
            self._set_vip_marker(vip, True)
            await self.broadcast_message(
                f"{vip.name} is the {self.server.teams[team].name} VIP!"
            )
        await self.broadcast_message("Protect the VIP!")

    async def _kill_vip(
        self,
        team: int,
        vip: Player,
        killer: Player | None,
    ) -> None:
        """Enter sudden death for one team and play team-relative cues."""
        self.vip_alive[team] = False
        self.respawn_enabled[team] = not self.sudden_death_enabled
        self._set_vip_marker(vip, False)
        suffix = " No more respawns!" if self.sudden_death_enabled else ""
        await self.broadcast_message(
            f"{self.server.teams[team].name} VIP has been killed!{suffix}"
        )

        from server.audio import (
            SND_VIP_KILLED_THEIRS,
            SND_VIP_YOURS_IS_DEAD,
            play_sound_to,
        )
        for player in list(self.server.players.values()):
            if getattr(player, "connection", None) is None:
                continue
            sound = (
                SND_VIP_YOURS_IS_DEAD
                if int(player.team) == team
                else SND_VIP_KILLED_THEIRS
            )
            play_sound_to(player, sound, volume=1.0)

        if killer is not None and killer is not vip:
            from server.scoreboard import send_player_score

            if int(getattr(killer, "team", -1)) == team:
                bonus = int(CG.VIP_SCORE_OWN_VIP_KILL)
            else:
                bonus = int(CG.VIP_SCORE_VIP_KILL_CONSTANT)
                bonus += int(
                    int(getattr(vip, "score", 0))
                    * int(CG.VIP_SCORE_VIP_KILL_PERCENT)
                    / 100
                )
                killer_team = int(getattr(killer, "team", -1))
                if (
                    killer_team in _PLAYABLE_TEAMS
                    and self.vips.get(killer_team) is killer
                    and self.vip_alive.get(killer_team, False)
                ):
                    bonus += int(CG.VIP_SCORE_KILL_AS_VIP)
            if bonus:
                killer.score = int(getattr(killer, "score", 0)) + bonus
                send_player_score(self.server, killer)

        # This compatibility option is useful for servers that want the old
        # kill-the-boss rule without the elimination phase. The retail rule
        # keeps sudden death enabled and follows the elimination check below.
        if not self.sudden_death_enabled:
            await self._finish_round(_other_team(team))

    async def _check_team_elimination(self) -> None:
        if self.phase is not VIPPhase.ACTIVE:
            return
        eliminated = []
        for team in _PLAYABLE_TEAMS:
            if self.respawn_enabled[team]:
                continue
            alive = any(
                player.team == team and player.alive and player.spawned
                for player in self.server.players.values()
            )
            if not alive:
                eliminated.append(team)

        if len(eliminated) == 1:
            await self._finish_round(_other_team(eliminated[0]))
        elif len(eliminated) == 2:
            live_vip_teams = [team for team in _PLAYABLE_TEAMS if self.vip_alive[team]]
            await self._finish_round(live_vip_teams[0] if len(live_vip_teams) == 1 else None)

    async def _finish_round(self, winner: int | None) -> None:
        """Score one sub-round, then either finish the match or restart it."""
        if self.phase is VIPPhase.INTERMISSION or self.ended:
            return
        self.phase = VIPPhase.INTERMISSION
        self._clear_vip_markers()

        if winner is None:
            await self.broadcast_message("VIP round ended in a draw.")
        else:
            from server.scoreboard import send_team_score

            team = self.server.teams[winner]
            team.add_score(1)
            send_team_score(self.server, team)
            await self.broadcast_message(f"{team.name} wins the VIP round!")
            if team.score >= self.score_limit:
                await self._end_by_score(winner)
                return

        self._round_task = asyncio.create_task(self._round_intermission_task())

    async def _round_intermission_task(self) -> None:
        try:
            await asyncio.sleep(max(0.0, self.round_intermission))
            self._round_task = None
            if not self.ended:
                await self._begin_round(reset_players=True)
        except asyncio.CancelledError:
            raise

    async def _cancel_round_task(self) -> None:
        task = self._round_task
        if task is None or task.done():
            self._round_task = None
            return
        if task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._round_task = None

    def _clear_vip_markers(self) -> None:
        seen: set[int] = set()
        for vip in self.vips.values():
            player_id = getattr(vip, "id", None)
            if vip is None or player_id is None or int(player_id) in seen:
                continue
            seen.add(int(player_id))
            self._set_vip_marker(vip, False)

    def _set_vip_marker(self, player: Player, visible: bool, connection=None) -> None:
        """Send ChangePlayer action 8, the retail through-wall crown marker."""
        from shared.packet import ChangePlayer

        packet = ChangePlayer()
        packet.player_id = int(player.id)
        packet.type = int(C.SET_HIGH_MINIMAP_VISIBILITY)
        packet.high_minimap_visibility = int(bool(visible))
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            connection.send(data, reliable=True)
