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

# SetHP.damage_type values, decoded from the stock client's
# GameScene.process_packet_set_hp (aoslib/scenes/main/gameScene.pyd, headless
# IDA 2026-09-26): the handler compares damage_type against the anonymous
# constants A982..A985 (= 1..4 in shared/constants.py). 1 arms hit_time
# (HIT_INDICATOR_TIME), 2 is heal_hp_added, 3 arms burn_time
# (BURN_INDICATOR_TIME) and 4 arms sudden_death_damage_time
# (SUDDEN_DEATH_INDICATOR_TIME, the HUD's sudden_death_r/g/b flash).
SUDDEN_DEATH_DAMAGE_TYPE = 4

# The client tables carry VIP_SCORE_DEFEND / VIP_SCORE_DISTRACT but no amount
# for the two VIP assault reasons ("Close to VIP", "VIP Assault"). The paired
# Capture-the-Flag reasons use the same names ("Close to Flag", "Flag
# Assault") and 50 points each, so those amounts are reused. Server choice:
# the retail VIP amounts were not recovered.
VIP_SCORE_ASSAULT = int(CG.CTF_SCORE_ASSAULT)
VIP_SCORE_ASSAULT_ENEMY = int(CG.CTF_SCORE_ASSAULT_ENEMY)
# Damage on the enemy VIP older than this earns no "VIP Assault" credit; the
# same bounded window as the generic assist (server/combat_scores.py).
VIP_ASSAULT_ENEMY_WINDOW = 10.0


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
        self._last_man_announced: set[int] = set()
        # Sudden-death damage over time, per bereaved team (monotonic
        # clock): when VIP_SUDDEN_DEATH_ACTIVATED fires, then the next
        # 1 HP tick. Both are cleared by every sub-round boundary.
        self._sudden_death_activate_at: dict[int, float | None] = {
            TEAM1: None, TEAM2: None,
        }
        self._sudden_death_damage_at: dict[int, float | None] = {
            TEAM1: None, TEAM2: None,
        }
        # Recent enemy damage on each live VIP: team -> {attacker id:
        # (attacker, monotonic time)}; pays VIP_ASSAULT_ENEMY on the kill.
        self._vip_attackers: dict[int, dict[int, tuple[object, float]]] = {
            TEAM1: {}, TEAM2: {},
        }
        # Sub-rounds finished this match (RULE_VIP_NOOF_ROUNDS counts them).
        self.rounds_played = 0

    async def on_mode_start(self) -> None:
        """Reset the full match and wait until both teams can choose a VIP."""
        await self._cancel_round_task()
        self._round_reset_queue.clear()
        self._promotion_deaths.clear()
        self._clear_vip_markers()
        await super().on_mode_start()
        for team in self.server.teams.values():
            team.reset()
        self.rounds_played = 0
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
        self._clear_sudden_death()
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
            await self._advance_sudden_death(now)
            if self.ended or self.phase is not VIPPhase.ACTIVE:
                return
            if audit_due:
                await self._check_team_elimination()

    async def on_player_join(self, player: Player) -> None:
        """Start the selection countdown once both teams have a player."""
        if self.ended:
            return
        if (
            self.phase is VIPPhase.RESETTING
            and player.team in _PLAYABLE_TEAMS
            and getattr(player, "connection", None) is not None
            and all(queued is not player for queued in self._round_reset_queue)
        ):
            # can_player_respawn refuses while the sub-round reset drains, so
            # a joiner arriving mid-drain rides the same bounded respawn queue
            # instead of waiting dead for the ordinary timer.
            self._round_reset_queue.append(player)
        if self.phase in (VIPPhase.WAITING, VIPPhase.SELECTING):
            await self._arm_selection_if_ready(time.monotonic())

    async def on_player_death(
        self,
        player: Player,
        killer: Player | None,
        kill_type: int,
    ) -> None:
        """Lock a VIP's team out of respawns and test for elimination."""
        await super().on_player_death(player, killer, kill_type)
        player_id = int(getattr(player, "id", -1))
        if (
            int(kill_type) == KILL_CLASS_CHANGE
            and player_id in self._promotion_deaths
        ):
            self._promotion_deaths.discard(player_id)
            return
        if self.ended:
            # The match result is final: no boss bonus, no sudden-death
            # announcement and no sub-round may start during the end screen.
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

    async def on_player_kill(
        self,
        killer: Player,
        victim: Player,
        kill_type: int,
    ) -> None:
        """Generic kill score, then the VIP defend/distract/assault extras.

        Inferred from the retail reason names and the VIP_THREAT_RADIUS /
        VIP_SCORE_DEFEND / VIP_SCORE_DISTRACT constants (the retail server
        source is not recovered):

        * VIP Defend (150): kill an enemy within VIP_THREAT_RADIUS of your
          own live VIP. The VIP it threatened earns VIP Distraction (50).
        * Close to VIP (VIP_SCORE_ASSAULT): kill a bodyguard standing within
          VIP_THREAT_RADIUS of the enemy's live VIP.

        Killing the enemy VIP itself pays Kill Enemy VIP / VIP Assault in
        ``_kill_vip``; a live VIP's own kills already pay VIP Kill.
        """
        await super().on_player_kill(killer, victim, kill_type)
        if self.ended or self.phase is not VIPPhase.ACTIVE:
            return
        if killer is None or victim is None or killer is victim:
            return
        killer_team = int(getattr(killer, "team", -1))
        victim_team = int(getattr(victim, "team", -1))
        if (
            killer_team not in _PLAYABLE_TEAMS
            or victim_team not in _PLAYABLE_TEAMS
            or killer_team == victim_team
        ):
            return
        if any(vip is victim for vip in self.vips.values()):
            return
        radius = float(CG.VIP_THREAT_RADIUS)
        own_vip = self._live_vip(killer_team)
        if (
            own_vip is not None
            and own_vip is not killer
            and self._within(victim, own_vip, radius)
        ):
            self._award_player_score(
                killer,
                int(CG.VIP_SCORE_DEFEND),
                int(C.SCORE_REASON.VIP_DEFEND_SCORE_REASON),
            )
            self._award_player_score(
                own_vip,
                int(CG.VIP_SCORE_DISTRACT),
                int(C.SCORE_REASON.VIP_DISTRACT_SCORE_REASON),
            )
            return
        enemy_vip = self._live_vip(victim_team)
        if enemy_vip is not None and self._within(victim, enemy_vip, radius):
            self._award_player_score(
                killer,
                VIP_SCORE_ASSAULT,
                int(C.SCORE_REASON.VIP_ASSAULT_SCORE_REASON),
            )

    async def on_player_leave(self, player: Player) -> None:
        """Treat a VIP disconnect as a death so quitting cannot save a team.

        The hook may run while the departing player is still in the roster
        (before PlayerLeft) or after removal; every count below excludes it.
        """
        self._round_reset_queue = deque(
            queued for queued in self._round_reset_queue if queued is not player
        )
        if self.ended:
            return
        team = int(getattr(player, "team", -1))
        if (
            self.phase is VIPPhase.ACTIVE
            and team in _PLAYABLE_TEAMS
            and self.vips.get(team) is player
            and self.vip_alive[team]
        ):
            await self._kill_vip(team, player, killer=None)
        await self._check_team_elimination(exclude=player)
        if self.phase in (VIPPhase.WAITING, VIPPhase.SELECTING):
            await self._arm_selection_if_ready(time.monotonic(), exclude=player)

    async def on_player_team_change(
        self,
        player: Player,
        old_team: int,
        new_team: int,
    ) -> None:
        """Re-evaluate a sudden-death team after a regular member leaves it."""
        if self.ended:
            return
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
        """Keep bosses and sudden-death teams from switching out of the round.

        A live VIP may never switch. Once a team has lost its VIP (respawns
        off) nobody on it may leave during the active sub-round: a dead
        member would be revived at once by the other team's respawn timer
        (its death_time is already old), a live one would trade a permanent
        life for a fresh respawn, and the spectator hop only defers the same
        dodge. The next sub-round's reset restores free team choice.
        """
        team = int(getattr(player, "team", -1))
        if (
            team in _PLAYABLE_TEAMS
            and self.vips.get(team) is player
            and self.vip_alive.get(team, False)
        ):
            return False
        if (
            not self.ended
            and self.phase is VIPPhase.ACTIVE
            and team in _PLAYABLE_TEAMS
            and not self.respawn_enabled.get(team, True)
        ):
            return False
        return True

    def can_player_respawn(self, player: Player) -> bool:
        """Return the per-team respawn permission.

        RoundLifecycle consults this for every timed respawn and the join
        path consults it for a first spawn, so a joiner (or rejoiner) on a
        team whose VIP has died enters dead instead of adding a fresh life to
        a sudden-death team. Joiners during a sub-round reset or intermission
        are revived by the next reset queue.
        """
        team = int(getattr(player, "team", -1))
        return (
            not self.ended
            and self.phase not in (VIPPhase.INTERMISSION, VIPPhase.RESETTING)
            and self.respawn_enabled.get(team, False)
        )

    def respawn_time_for(self, player: Player) -> float:
        """KillAction countdown; NEVER_RESPAWN_TIME for a life that won't return.

        The stock HUD shows ``NEVER_RESPAWN`` ("No respawns!") when the
        KillAction carries ``NEVER_RESPAWN_TIME`` (255). A dying VIP and every
        member of a sudden-death team (including a joiner locked out of it)
        get that sentinel; a sub-round reset or intermission revives the team
        shortly, so those keep the zero "wait for the reset" timer.
        """
        never = float(getattr(C, "NEVER_RESPAWN_TIME", 255))
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
            return never
        if not self.can_player_respawn(player):
            if (
                not self.ended
                and self.phase is VIPPhase.ACTIVE
                and team in _PLAYABLE_TEAMS
            ):
                return never
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
            if (
                source is not None
                and source is not player
                and int(getattr(source, "team", -1)) in _PLAYABLE_TEAMS
                and int(getattr(source, "team", -1)) != team
                and amount > 0
            ):
                self._vip_attackers.setdefault(team, {})[
                    int(getattr(source, "id", -1))
                ] = (source, time.monotonic())
            reduced = int(round(float(amount) * float(C.VIP_DAMAGE_MULTIPLIER)))
            return max(1, reduced) if amount > 0 else 0
        return int(amount)

    def get_spawn_point(self, player: Player) -> tuple[float, float, float]:
        """Spawn gangster teams across their base, away from campers.

        Every life used to land on the single base anchor, which made the
        base trivially campable; the shared threat-aware chooser keeps teams
        at their base but spreads and protects the spawns.
        """
        return tuple(float(value) for value in super().get_spawn_point(player))

    def reveal_to(self, connection) -> None:
        """Replay live VIP crown markers to a joining GameScene."""
        super().reveal_to(connection)
        for team in _PLAYABLE_TEAMS:
            vip = self.vips.get(team)
            if (
                vip is not None
                and self.vip_alive.get(team, False)
                and self._is_connected(vip)
            ):
                self._set_vip_marker(vip, True, connection=connection)

    async def _begin_round(self, *, reset_players: bool) -> None:
        """Clear prior bosses, restore respawns, and arm the next selection."""
        self._clear_vip_markers()
        self.vips = {TEAM1: None, TEAM2: None}
        self.vip_alive = {TEAM1: False, TEAM2: False}
        self.respawn_enabled = {TEAM1: True, TEAM2: True}
        self.selection_deadline = None
        self._last_man_announced: set[int] = set()
        self._next_roster_audit = 0.0
        self._next_vip_survival_score = float("inf")
        self._next_escort_score = float("inf")
        self._round_reset_queue.clear()
        self._promotion_deaths.clear()
        self._clear_sudden_death()

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

    def _team_candidates(self, team: int, exclude=None) -> list[Player]:
        # A still-loading joiner cannot be promoted: its boss CreatePlayer,
        # SetHP and crown marker would reach no GameScene.
        return [
            player for player in self.server.players.values()
            if player.team == team
            and player.connection is not None
            and bool(getattr(player.connection, "in_game", True))
            and player is not exclude
        ]

    def _teams_ready(self, exclude=None) -> bool:
        return all(
            len(self._team_candidates(team, exclude)) >= self.minimum_team_size
            for team in _PLAYABLE_TEAMS
        )

    async def _arm_selection_if_ready(self, now: float, exclude=None) -> None:
        if self.ended or self.phase not in (VIPPhase.WAITING, VIPPhase.SELECTING):
            return
        if not self._teams_ready(exclude):
            self.phase = VIPPhase.WAITING
            self.selection_deadline = None
            return
        if self.phase is VIPPhase.WAITING:
            self.phase = VIPPhase.SELECTING
            self.selection_deadline = now + max(0.0, self.selection_delay)
            await self.broadcast_localised_message(
                "VIP_AWAITING_CHOICE", override_previous=True
            )

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

            # SetHP carries one unsigned byte. The boss's maximum is stored on
            # the body so health crates / medpacks heal to it (not to 100).
            vip.health = max(1, min(255, int(round(
                MAX_HEALTH * self.vip_health_multiplier
            ))))
            vip.max_health = int(vip.health)
            if vip.connection is not None:
                packet = SetHP()
                packet.hp = vip.health
                packet.damage_type = 0
                packet.source_x, packet.source_y, packet.source_z = getattr(
                    vip, "position", (0.0, 0.0, 0.0)
                )
                vip.connection.send(bytes(packet.generate()))
            self._set_vip_marker(vip, True)
            # Teammates get the retail name line; the boss's own HUD shows
            # VIP_YOU_ARE_VIP from the class change (hud.pyd), and retail
            # has no string naming the enemy VIP.
            self.announce_localised_to_team(
                team, "VIP_NAME_IS_VIP", (str(vip.name),), exclude=vip
            )
        self.vip_alive = {TEAM1: True, TEAM2: True}
        self.phase = VIPPhase.ACTIVE
        await self.broadcast_localised_message("VIP_START", override_previous=True)

    async def _kill_vip(
        self,
        team: int,
        vip: Player,
        killer: Player | None,
    ) -> None:
        """Enter sudden death for one team and play team-relative cues."""
        self.vip_alive[team] = False
        self.respawn_enabled[team] = not self.sudden_death_enabled
        attackers = self._vip_attackers.get(team, {})
        self._vip_attackers[team] = {}
        if self.sudden_death_enabled:
            # GAME_MODE_CALLBACK_VIP_SUDDEN_DEATH_DELAY: sudden death
            # activates VIP_SUDDEN_DEATH_DELAY_AFTER_VIP_KILL after the kill.
            self._sudden_death_activate_at[team] = time.monotonic() + float(
                CG.VIP_SUDDEN_DEATH_DELAY_AFTER_VIP_KILL
            )
            self._sudden_death_damage_at[team] = None
        if self._is_connected(vip):
            self._set_vip_marker(vip, False)
        # Retail team-relative pair ("VIP is dead! No more respawns!" for the
        # bereaved team, "Enemy VIP is dead! Mop up the rest!" for the other).
        self.announce_localised_to_team(team, "VIP_KILLED_VIP_YOURTEAM")
        self.announce_localised_to_team(_other_team(team), "VIP_KILLED_VIP_OPPOSITION")

        # Team-relative pair; spectators hear neither (they used to get the
        # "killed theirs" cheer for every VIP death).
        from server.audio import (
            SND_VIP_KILLED_THEIRS,
            SND_VIP_YOURS_IS_DEAD,
            play_team_relative,
        )

        play_team_relative(
            self.server,
            _other_team(team),
            good=SND_VIP_KILLED_THEIRS,
            bad=SND_VIP_YOURS_IS_DEAD,
        )

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
            if int(getattr(killer, "team", -1)) != team:
                self._award_vip_assault_enemy(team, killer, attackers)

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

    def _live_vip(self, team: int):
        """The team's boss while it is alive and still owns its id."""
        vip = self.vips.get(team)
        if (
            vip is None
            or not self.vip_alive.get(team, False)
            or not bool(getattr(vip, "alive", False))
            or not self._is_connected(vip)
        ):
            return None
        return vip

    @staticmethod
    def _within(first, second, radius: float) -> bool:
        a = getattr(first, "position", None)
        b = getattr(second, "position", None)
        if a is None or b is None:
            return False
        try:
            distance_sq = sum(
                (float(a[index]) - float(b[index])) ** 2 for index in range(3)
            )
        except (IndexError, TypeError, ValueError):
            return False
        return distance_sq <= float(radius) ** 2

    def _award_vip_assault_enemy(self, team: int, killer, attackers) -> None:
        """Pay "VIP Assault" to every other enemy who recently hurt the VIP."""
        now = time.monotonic()
        for player_id in sorted(attackers):
            attacker, touched = attackers[player_id]
            attacker_team = int(getattr(attacker, "team", -1))
            if (
                attacker is killer
                or now - float(touched) > VIP_ASSAULT_ENEMY_WINDOW
                or attacker_team == team
                or attacker_team not in _PLAYABLE_TEAMS
            ):
                continue
            self._award_player_score(
                attacker,
                VIP_SCORE_ASSAULT_ENEMY,
                int(C.SCORE_REASON.VIP_ASSAULT_ENEMY_SCORE_REASON),
            )

    def _clear_sudden_death(self) -> None:
        self._sudden_death_activate_at = {TEAM1: None, TEAM2: None}
        self._sudden_death_damage_at = {TEAM1: None, TEAM2: None}
        self._vip_attackers = {TEAM1: {}, TEAM2: {}}

    async def _advance_sudden_death(self, now: float) -> None:
        """Run the retail sudden-death damage over time.

        Values (shared/constants_gamemode.py): sudden death activates
        VIP_SUDDEN_DEATH_DELAY_AFTER_VIP_KILL (5 s) after a VIP dies and is
        announced with VIP_SUDDEN_DEATH_ACTIVATED; VIP_SUDDEN_DEATH_TIME
        (60 s) later every living member of that team starts losing
        VIP_SUDDEN_DEATH_DAMAGE (1 HP) each VIP_SUDDEN_DEATH_DAMAGE_FREQUENCY
        (1 s) until the sub-round ends. A stalled tick deals at most one
        step (no catch-up burst of reliable SetHP packets).
        """
        for team in _PLAYABLE_TEAMS:
            if self.ended or self.phase is not VIPPhase.ACTIVE:
                return
            activate_at = self._sudden_death_activate_at.get(team)
            if activate_at is not None and now >= activate_at:
                self._sudden_death_activate_at[team] = None
                self._sudden_death_damage_at[team] = now + float(
                    CG.VIP_SUDDEN_DEATH_TIME
                )
                self.announce_localised(
                    "VIP_SUDDEN_DEATH_ACTIVATED",
                    (self.server.teams[team].name,),
                    localise_parameters=True,
                )
            damage_at = self._sudden_death_damage_at.get(team)
            if damage_at is None or now < damage_at:
                continue
            self._sudden_death_damage_at[team] = now + max(
                0.05, float(CG.VIP_SUDDEN_DEATH_DAMAGE_FREQUENCY)
            )
            for player in list(self.server.players.values()):
                if (
                    int(getattr(player, "team", -1)) != team
                    or not bool(getattr(player, "alive", False))
                    or not bool(getattr(player, "spawned", False))
                ):
                    continue
                self._apply_sudden_death_damage(player)

    def _apply_sudden_death_damage(self, player) -> None:
        """Remove one sudden-death step and flash the native HUD indicator.

        ``Player.damage`` cannot carry SetHP damage_type 4, so the step is
        applied here. There is no attacker: a fatal step is an unattributed
        VIP_MODE_KILL (no suicide penalty, no kill credit).
        """
        if bool(getattr(player, "god_mode", False)):
            return
        amount = max(0, int(CG.VIP_SUDDEN_DEATH_DAMAGE))
        if amount <= 0:
            return
        health = max(0, int(getattr(player, "health", 0)) - amount)
        player.health = health
        if hasattr(player, "_last_damage_at"):
            player._last_damage_at = time.time()
        connection = getattr(player, "connection", None)
        if connection is not None and getattr(connection, "in_game", True):
            from shared.packet import SetHP

            packet = SetHP()
            packet.hp = max(0, min(255, health))
            packet.damage_type = SUDDEN_DEATH_DAMAGE_TYPE
            position = getattr(player, "position", None) or (0.0, 0.0, 0.0)
            packet.source_x, packet.source_y, packet.source_z = (
                float(position[0]), float(position[1]), float(position[2])
            )
            connection.send(bytes(packet.generate()))
        if health <= 0:
            player.die(killer=None, kill_type=int(C.KILL.VIP_MODE_KILL))

    def _award_player_score(self, player: Player, amount: int, reason: int) -> None:
        """Commit one mode score and publish its native HUD reason."""

        if amount == 0:
            return
        if not self._is_connected(player):
            # Departed (its compact id may belong to a newcomer).
            return
        from server.scoreboard import send_player_score

        player.score = int(getattr(player, "score", 0)) + int(amount)
        send_player_score(self.server, player, reason=int(reason))

    async def _check_team_elimination(self, exclude=None) -> None:
        if self.ended or self.phase is not VIPPhase.ACTIVE:
            return
        eliminated = []
        for team in _PLAYABLE_TEAMS:
            if self.respawn_enabled[team]:
                continue
            alive = any(
                player.team == team and player.alive and player.spawned
                and player is not exclude
                for player in self.server.players.values()
            )
            if not alive:
                eliminated.append(team)

        for team in _PLAYABLE_TEAMS:
            if self.respawn_enabled[team] or team in eliminated:
                continue
            living = sum(
                1 for player in self.server.players.values()
                if player.team == team and player.alive and player.spawned
                and player is not exclude
            )
            if living == 1 and team not in self._last_man_announced:
                self._last_man_announced.add(team)
                self.announce_localised(
                    "VIP_LAST_MAN_STANDING",
                    (self.server.teams[team].name,),
                    localise_parameters=True,
                )
        if len(eliminated) == 1:
            self.announce_localised(
                "VIP_TEAM_WIPED_OUT",
                (self.server.teams[eliminated[0]].name,),
                localise_parameters=True,
            )
            await self._finish_round(_other_team(eliminated[0]))
        elif len(eliminated) == 2:
            live_vip_teams = [team for team in _PLAYABLE_TEAMS if self.vip_alive[team]]
            await self._finish_round(live_vip_teams[0] if len(live_vip_teams) == 1 else None)

    def end_message_id(self, winner: int | None) -> int:
        """Retail scoreboard headline: VIP_TEAM1/2_WIN_MESSAGE or a draw."""
        if winner == TEAM1:
            return int(C.VIP_TEAM1_WIN_MESSAGE)
        if winner == TEAM2:
            return int(C.VIP_TEAM2_WIN_MESSAGE)
        return int(C.TEAM_SCORES_DRAW)

    async def _finish_round(self, winner: int | None) -> None:
        """Score one sub-round, then either finish the match or restart it."""
        if self.phase is VIPPhase.INTERMISSION or self.ended:
            return
        self.phase = VIPPhase.INTERMISSION
        self._clear_sudden_death()
        self._clear_vip_markers()
        self._last_man_announced.clear()

        # RULE_VIP_NOOF_ROUNDS / VIP_NOOF_ROUNDS_BEFORE_NEXT_MAP (3) is the
        # number of sub-rounds PLAYED on the map -- the same rule shape and
        # "_BEFORE_NEXT_MAP" name as Zombie's rounds -- not a win target. The
        # match winner is the team with more round wins (a tie is a draw).
        # Inferred without a retail capture (rules audit 2026-09-27 #15).
        self.rounds_played = int(getattr(self, "rounds_played", 0)) + 1
        last_round = self.rounds_played >= self.score_limit
        if winner is None:
            if last_round:
                await self._end_by_time()
                return
            await self.broadcast_localised_message("GAME_DRAWN")
        else:
            from server.scoreboard import send_team_score

            team = self.server.teams[winner]
            team.add_score(1)
            send_team_score(self.server, team)
            if last_round:
                # The match-end sequence announces TEAM_DEFEAT itself.
                await self._end_by_time()
                return
            await self.broadcast_localised_message(
                "TEAM_DEFEAT", (team.name,), localise_parameters=True
            )

        self._round_task = asyncio.create_task(self._round_intermission_task())

    async def _round_intermission_task(self) -> None:
        try:
            await asyncio.sleep(max(0.0, self.round_intermission))
            self._round_task = None
            if not self.ended:
                await self._begin_round(reset_players=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Without a fallback the match froze in INTERMISSION forever
            # (no respawns, no selection, no clock outcome).
            logger.exception("VIP sub-round restart failed")
            self._round_task = None
            await self._recover_failed_round_restart()

    def bot_retire_safe(self, bot) -> bool:
        """Never retire the last living member of a sudden-death team."""
        if self.ended or self.phase is not VIPPhase.ACTIVE:
            return True
        team = int(getattr(bot, "team", -1))
        if team not in _PLAYABLE_TEAMS or self.respawn_enabled.get(team, True):
            return True
        if not (bool(getattr(bot, "alive", False)) and bool(getattr(bot, "spawned", False))):
            return True
        others = any(
            player is not bot
            and int(getattr(player, "team", -1)) == team
            and bool(getattr(player, "alive", False))
            and bool(getattr(player, "spawned", False))
            for player in self.server.players.values()
        )
        return others

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
            # A departed boss's id may already belong to a new joiner; its
            # marker vanished with PlayerLeft, so send nothing for it.
            if self._is_connected(vip):
                self._set_vip_marker(vip, False)

    def _is_connected(self, player) -> bool:
        """True while ``player`` still owns its (reusable) id in the roster."""
        player_id = getattr(player, "id", None)
        if player is None or player_id is None:
            return False
        try:
            return self.server.players.get(int(player_id)) is player
        except (TypeError, ValueError):
            return False

    def mode_marks_player(self, player) -> bool:
        """VIPs carry the crown marker; the escape watch must not clear it."""
        return any(vip is player for vip in self.vips.values())

    def escape_watch_objective_player(self, player) -> bool:
        return self.mode_marks_player(player)

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
