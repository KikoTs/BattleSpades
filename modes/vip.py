"""VIP mode: protect each team's boss, then eliminate the survivors.

The retail client already contains the gangster and boss character models. The
server owns the round state machine, promotes one player per team to the
team-specific boss class, and uses ChangePlayer action 8 for the native crown
marker visible through terrain.
"""

from __future__ import annotations

import asyncio
from collections import deque
from enum import Enum, auto
import logging
import random
import time
from typing import TYPE_CHECKING

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.class_selection import ClassSelection, normalize_class_selection
from server.game_constants import KILL_CLASS_CHANGE, TEAM1, TEAM2

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player


logger = logging.getLogger(__name__)

_PLAYABLE_TEAMS = (TEAM1, TEAM2)
_ORDINARY_GANGSTERS = tuple(int(value) for value in C.MAFIA_TEAM_CLASSES)


class VIPPhase(Enum):
    """Authoritative phase of one VIP sub-round."""

    WAITING = auto()
    RESETTING = auto()
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
    # Exact retail ``playlists/vip.txt`` rotation. An explicit operator map
    # list still wins in VoteManager, but an unconfigured VIP server should
    # not vote into maps that lack the shipped gangster-mode layout.
    stock_maps = ("Alcatraz", "CityOfChicago")

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
        self.round_respawns_per_tick = max(1, int(overlay.get(
            "round_respawns_per_tick", 4
        )))
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
        self._round_reset_queue: deque[Player] = deque()
        self._next_roster_audit = 0.0
        self._next_vip_survival_score = float("inf")
        self._next_escort_score = float("inf")
        # Promotion uses KillAction -> CreatePlayer so retail never receives a
        # second live Character for the same id. These synthetic class-change
        # deaths are ignored when their queued mode event drains later.
        self._promotion_deaths: set[int] = set()

    async def on_mode_start(self) -> None:
        """Reset the full match and wait until both teams can choose a VIP."""
        await self._cancel_round_task()
        self._round_reset_queue.clear()
        self._promotion_deaths.clear()
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
        self._round_reset_queue.clear()
        self._promotion_deaths.clear()
        self._clear_vip_markers()
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        """Advance selection and elimination checks on the gameplay tick."""
        await super().on_tick(tick)
        if self.ended or self.phase is VIPPhase.INTERMISSION:
            return
        now = time.monotonic()
        if self.phase is VIPPhase.RESETTING:
            await self._drain_round_reset(now)
            return

        # Joins, deaths, leaves, and team changes drive roster transitions.
        # This low-frequency audit is only a safety net for plugins that alter
        # player state without calling the public mode hooks.
        audit_due = now >= self._next_roster_audit
        if audit_due:
            self._next_roster_audit = now + 0.25
        if self.phase in (VIPPhase.WAITING, VIPPhase.SELECTING) and audit_due:
            await self._arm_selection_if_ready(now)
        if (
            self.phase is VIPPhase.SELECTING
            and self.selection_deadline is not None
            and now >= self.selection_deadline
        ):
            await self._select_vips()
        if self.phase is VIPPhase.ACTIVE:
            self._award_periodic_scores(now)
            if audit_due:
                await self._check_team_elimination()

    async def on_player_join(self, player: Player) -> None:
        """Start the selection countdown once both teams have a player."""
        if self.phase in (VIPPhase.WAITING, VIPPhase.SELECTING):
            await self._arm_selection_if_ready(time.monotonic())

    async def on_player_death(
        self,
        player: Player,
        killer: Player | None,
        kill_type: int,
    ) -> None:
        """Lock a VIP's team out of respawns and test for elimination."""
        player_id = int(getattr(player, "id", -1))
        if (
            int(kill_type) == KILL_CLASS_CHANGE
            and player_id in self._promotion_deaths
        ):
            self._promotion_deaths.discard(player_id)
            return
        team = int(getattr(player, "team", -1))
        killer_team = int(getattr(killer, "team", -1))
        if (
            self.phase is VIPPhase.ACTIVE
            and killer is not None
            and killer is not player
            and team in _PLAYABLE_TEAMS
            and killer_team in _PLAYABLE_TEAMS
            and killer_team != team
            and self.vips.get(killer_team) is killer
            and self.vip_alive.get(killer_team, False)
        ):
            # Retail awards this mode bonus for every enemy killed by a live
            # boss, independently of the ordinary combat kill score.
            self._award_player_score(
                killer,
                int(CG.VIP_SCORE_KILL_AS_VIP),
                int(C.SCORE_REASON.VIP_KILL_SCORE_REASON),
            )
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
        if self.phase in (VIPPhase.WAITING, VIPPhase.SELECTING):
            await self._arm_selection_if_ready(time.monotonic())

    async def on_player_team_change(
        self,
        player: Player,
        old_team: int,
        new_team: int,
    ) -> None:
        """Re-evaluate a sudden-death team after a regular member leaves it."""
        if old_team in _PLAYABLE_TEAMS:
            await self._check_team_elimination()
        if self.phase in (VIPPhase.WAITING, VIPPhase.SELECTING):
            await self._arm_selection_if_ready(time.monotonic())

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
            and self.phase not in (VIPPhase.INTERMISSION, VIPPhase.RESETTING)
            and self.respawn_enabled.get(team, False)
        )

    def respawn_time_for(self, player: Player) -> float:
        """Expose zero respawn time to a permanently dead retail client."""
        team = int(getattr(player, "team", -1))
        if (
            self.phase is VIPPhase.ACTIVE
            and team in _PLAYABLE_TEAMS
            and self.vips.get(team) is player
            and self.vip_alive.get(team, False)
        ):
            # The hook is queried while Player.die is constructing KillAction,
            # before the queued mode event flips vip_alive. Advertising the
            # ordinary delay here leaves retail counting toward a respawn the
            # server will correctly refuse.
            return 0.0
        if not self.can_player_respawn(player):
            return 0.0
        return float(self.server.config.respawn_time)

    def death_kill_type_for(
        self,
        player: Player,
        killer: Player | None,
        kill_type: int,
    ) -> int:
        """Select retail's dedicated boss-death transition for a live VIP."""

        team = int(getattr(player, "team", -1))
        if (
            self.phase is VIPPhase.ACTIVE
            and team in _PLAYABLE_TEAMS
            and self.vips.get(team) is player
            and self.vip_alive.get(team, False)
        ):
            return int(C.KILL.VIP_MODE_KILL)
        return int(kill_type)

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
        self._next_roster_audit = 0.0
        self._next_vip_survival_score = float("inf")
        self._next_escort_score = float("inf")
        self._round_reset_queue.clear()
        self._promotion_deaths.clear()

        if reset_players:
            for player in list(self.server.players.values()):
                if player.team not in _PLAYABLE_TEAMS or player.connection is None:
                    continue
                self._round_reset_queue.append(player)
            if self._round_reset_queue:
                self.phase = VIPPhase.RESETTING
                return

        self.phase = VIPPhase.WAITING
        await self._arm_selection_if_ready(time.monotonic())

    async def _drain_round_reset(self, now: float) -> None:
        """Respawn a bounded slice of the next VIP sub-round.

        This executes on the gameplay tick.  A respawn publishes reliable
        CreatePlayer, loadout, health, and restock packets; sending an entire
        24-player roster in one frame stalls both ENet and the native scene.
        Entries retain object identity so a disconnected player's reused
        compact id can never respawn the replacement accidentally.
        """

        budget = self.round_respawns_per_tick
        while budget > 0 and self._round_reset_queue:
            player = self._round_reset_queue.popleft()
            budget -= 1
            if (
                self.server.players.get(int(player.id)) is not player
                or player.team not in _PLAYABLE_TEAMS
                or player.connection is None
            ):
                continue
            try:
                if bool(getattr(player, "alive", False)):
                    player.die(kill_type=KILL_CLASS_CHANGE)
                player.apply_class_selection(self._ordinary_selection(player))
                self.server.respawn_player(player)
            except Exception:
                logger.exception(
                    "VIP sub-round respawn failed for player %s",
                    getattr(player, "id", "?"),
                )

        if not self._round_reset_queue:
            self.phase = VIPPhase.WAITING
            await self._arm_selection_if_ready(now)

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
        # Keep promotions outside ACTIVE until both old Characters have been
        # retired and recreated with their boss class.
        self.vip_alive = {TEAM1: False, TEAM2: False}
        self.respawn_enabled = {TEAM1: True, TEAM2: True}
        self.selection_deadline = None
        now = time.monotonic()
        self._next_vip_survival_score = (
            now + float(CG.VIP_SCORE_LIVEVIP_INTERVAL)
        )
        self._next_escort_score = now + float(CG.VIP_SCORE_ESCORT_INTERVAL)

        for team in _PLAYABLE_TEAMS:
            vip = selected[team]
            vip_class = int(C.MAFIA_VIPS[team])
            if bool(getattr(vip, "alive", False)):
                self._promotion_deaths.add(int(vip.id))
                vip.die(kill_type=KILL_CLASS_CHANGE)
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
        self.vip_alive = {TEAM1: True, TEAM2: True}
        self.phase = VIPPhase.ACTIVE
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
            if int(getattr(killer, "team", -1)) == team:
                bonus = int(CG.VIP_SCORE_OWN_VIP_KILL)
            else:
                bonus = int(CG.VIP_SCORE_VIP_KILL_CONSTANT)
                bonus += int(
                    int(getattr(vip, "score", 0))
                    * int(CG.VIP_SCORE_VIP_KILL_PERCENT)
                    / 100
                )
            if bonus:
                self._award_player_score(
                    killer,
                    bonus,
                    int(C.SCORE_REASON.VIP_KILLENEMYVIP_SCORE_REASON),
                )

        # This compatibility option is useful for servers that want the old
        # kill-the-boss rule without the elimination phase. The retail rule
        # keeps sudden death enabled and follows the elimination check below.
        if not self.sudden_death_enabled:
            await self._finish_round(_other_team(team))

    def _award_periodic_scores(self, now: float) -> None:
        """Award the two retail timed VIP score types without catch-up bursts.

        The native score menu specifies 50 points per ten seconds survived by
        a boss and 10 points per five seconds spent within 15 blocks of a live
        friendly boss.  A delayed gameplay tick awards at most one interval;
        replaying every missed interval after a stall would create a reliable
        packet burst and amplify exactly the client hitch this mode used to
        suffer from.
        """

        award_survival = now >= self._next_vip_survival_score
        award_escort = now >= self._next_escort_score
        if not award_survival and not award_escort:
            return

        if award_survival:
            self._next_vip_survival_score = (
                now + float(CG.VIP_SCORE_LIVEVIP_INTERVAL)
            )
        if award_escort:
            self._next_escort_score = now + float(CG.VIP_SCORE_ESCORT_INTERVAL)

        players = tuple(self.server.players.values())
        for team in _PLAYABLE_TEAMS:
            vip = self.vips.get(team)
            if (
                vip is None
                or not self.vip_alive.get(team, False)
                or not bool(getattr(vip, "alive", False))
                or not bool(getattr(vip, "spawned", False))
                or getattr(vip, "connection", None) is None
            ):
                continue

            if award_survival:
                self._award_player_score(
                    vip,
                    int(CG.VIP_SCORE_LIVEVIP_SCORE),
                    int(C.SCORE_REASON.VIP_SURVIVE_SCORE_REASON),
                )
            if not award_escort:
                continue

            vip_position = getattr(vip, "position", None)
            if vip_position is None:
                continue
            radius_sq = float(CG.VIP_ESCORT_RADIUS) ** 2
            for player in players:
                if (
                    player is vip
                    or int(getattr(player, "team", -1)) != team
                    or not bool(getattr(player, "alive", False))
                    or not bool(getattr(player, "spawned", False))
                    or getattr(player, "connection", None) is None
                ):
                    continue
                position = getattr(player, "position", None)
                if position is None:
                    continue
                try:
                    distance_sq = sum(
                        (float(position[index]) - float(vip_position[index])) ** 2
                        for index in range(3)
                    )
                except (IndexError, TypeError, ValueError):
                    continue
                if distance_sq <= radius_sq:
                    self._award_player_score(
                        player,
                        int(CG.VIP_SCORE_ESCORT_SCORE),
                        int(C.SCORE_REASON.VIP_ESCORT_SCORE_REASON),
                    )

    def _award_player_score(self, player: Player, amount: int, reason: int) -> None:
        """Commit one mode score and publish its native HUD reason."""

        if amount == 0:
            return
        from server.scoreboard import send_player_score

        player.score = int(getattr(player, "score", 0)) + int(amount)
        send_player_score(self.server, player, reason=int(reason))

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
