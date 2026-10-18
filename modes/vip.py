"""VIP mode: win by killing three opposing bosses.

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
# New server rules for the six-second post-VIP-death final minute.
VIP_SUDDEN_DEATH_GRACE_SECONDS = 6.0
VIP_FINAL_MINUTE_SECONDS = 60.0

# Damage on the enemy VIP older than this earns no "VIP Assault" credit; the
# same bounded window as the generic assist (server/combat_scores.py).
VIP_ASSAULT_ENEMY_WINDOW = 10.0

# CityOfChicago: two surveyed opposite corners of each team's spawn area.
# X/Y identify an entire rectangle, not just the two corners as spawn points.
# The sample Z values are at player height and are used to bound the authored
# volume; actual spawn height is resolved from the live VXL floor.
# BLUE=TEAM1, GREEN=TEAM2. Unlike the previous generic west/east fallback,
# CityOfChicago's blue base is southeast and its green base northwest.
VIP_CITY_OF_CHICAGO_SPAWN_CORNERS = {
    TEAM1: ((326.469, 485.533, 223.745), (352.547, 458.450, 233.741)),
    TEAM2: ((53.549, 106.548, 224.742), (30.452, 66.451, 227.750)),
}


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

    Both teams respawn until their own VIP dies. Killing an opposing VIP
    immediately earns a team point and reveals the survivors to the attackers.
    Six seconds later those survivors lose HP and at most a minute remains in
    the sub-round. Reaching the VIP kill target does not interrupt the
    current sub-round: the opposing team has time to counterattack.
    A 3:3 tie immediately ends the match in a draw, without overtime.
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
        # What a live VIP's disconnect does. Retail's server rules are not
        # recovered: the client strings only prove that a VIP *death* ends
        # that team's respawns ("VIP is dead! No more respawns!"). "reassign"
        # (default) crowns another member of the team so a team with anyone
        # left always has a VIP; "death" resolves the quit as a VIP kill.
        # Either way a team with nobody left loses its VIP.
        self.leave_policy = str(overlay.get("leave_policy", "reassign")).lower()
        if self.leave_policy not in {"reassign", "death"}:
            logger.warning(
                "Unknown [modes.vip] leave_policy %r; using 'reassign'",
                self.leave_policy,
            )
            self.leave_policy = "reassign"

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
        # Retained for compatibility with old queued transition callbacks.
        # New VIP promotions replace the body with CreatePlayer directly and
        # do not generate class-change death events or affect death counts.
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
        # RULE_VIP_NOOF_ROUNDS now means VIP kills required to win the match.
        # A sub-round ends independently when survivors are cleared or its
        # capped timer runs out. Never overwrite BaseMode.time_limit.
        self.rounds_played = 0
        self._subround_deadline: float | None = None
        self._vip_points_awarded: set[int] = set()
        # The set contains teams whose players can see opposing enemy icons.
        self._revealed_enemy_to: set[int] = set()
        # (map identity, team) -> (authored zone, positions that passed terrain
        # checks at load time). Only used for the manually surveyed Chicago map.
        self._vip_city_spawn_cache: dict[tuple[int, int], tuple[object, list[tuple[int, ...]]]] = {}

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
            "VIP UPDATE 2026-10-13: first to %d VIP kills, enemy radar on kill, "
            "6-second grace, final 60-second sub-round, instant draw on 3:3 (selection=%.1fs)",
            self.score_limit,
            self.selection_delay,
        )

    async def deactivate(self) -> None:
        """Cancel a pending sub-round before a map or mode rollover."""
        await self._cancel_round_task()
        self._round_reset_queue.clear()
        self._promotion_deaths.clear()
        self._clear_sudden_death()
        self._clear_vip_markers()
        await super().deactivate()

    def countdown_seconds_remaining(self, now: float | None = None) -> float:
        """Return the active clock, or a frozen zero during the intermission.

        SimulationRuntime publishes this result each second.  Explicitly
        returning zero prevents the prior, longer clock from reappearing
        after an early sub-round finish.
        """
        if self.phase is VIPPhase.INTERMISSION:
            return 0.0
        if self._subround_deadline is not None:
            return max(0.0, self._subround_deadline - time.monotonic())
        if self.time_limit <= 0:
            return 0.0
        return max(0.0, self.time_limit - (
            (time.time() if now is None else now) - self.start_time))

    def _reset_subround_clock(self) -> None:
        """Re-arm the clock and restore the gameplay bed for a new VIP round.

        The previous sub-round may have forced game_ending at 1:00, before
        BaseMode's regular whole-round timer reached its music threshold.
        Reset the music family *before* clearing that per-round flag so the
        next VIP draw doesn't keep playing the previous round's finale.
        """
        if self._timeout_music_played:
            from server.audio import play_gameplay_music

            play_gameplay_music(self.server)
        self.start_time = time.time()
        self.elapsed_time = 0.0
        self._timeout_events_remaining = None
        self._timeout_music_played = False
        self._countdown_fired.clear()
        self._countdown_last_remaining = float("inf")

    async def _end_by_time(self) -> None:
        """Time ran out in one VIP sub-round; do not end the whole match."""
        if self.ended or self.phase is VIPPhase.INTERMISSION:
            return
        if self.phase is not VIPPhase.ACTIVE:
            self._reset_subround_clock()
            return
        live = [t for t in _PLAYABLE_TEAMS if self.vip_alive.get(t, False)]
        await self._finish_round(live[0] if len(live) == 1 else None)

    async def _tick_vip_clock(self) -> None:
        """Run the normal shared maintenance but NOT BaseMode's map vote.

        BaseMode opens the F1/F2/F3 map ballot ten seconds before its
        ``time_limit`` expires, even when a VIP match has no winner yet.
        VIP matches have independent sub-round timers and must present the
        map ballot only through ``on_mode_end`` after the match is decided.
        """
        self.elapsed_time = time.time() - self.start_time
        if self.escape_watch_enabled:
            try:
                from server import escape_watch

                escape_watch.tick(self.server, self)
            except Exception:
                logger.exception("VIP escape watch failed")
        if self.phase is not VIPPhase.ACTIVE:
            self._timeout_events_remaining = None
            return

        has_clock = self.time_limit > 0 or self._subround_deadline is not None
        if not has_clock:
            self._timeout_events_remaining = None
            return
        remaining = self.countdown_seconds_remaining()
        from server.audio import TIMEOUT_MUSIC_SECONDS, play_timeout_music

        if not self._timeout_music_played and remaining <= TIMEOUT_MUSIC_SECONDS:
            self._timeout_music_played = True
            play_timeout_music(self.server)
        self._announce_countdown(remaining)
        if remaining > 0:
            self._timeout_events_remaining = None
            return

        # Keep the shared event-drain guard: a death already accepted at
        # the deadline must be processed before declaring the sub-round over.
        if self._timeout_events_remaining is None:
            self._timeout_events_remaining = len(
                getattr(self.server, "_mode_events", ())
            )
        if self._timeout_events_remaining == 0:
            await self._end_by_time()

    async def on_tick(self, tick: int) -> None:
        """Advance the VIP sub-round clock, roster and elimination checks."""
        if self.ended:
            return
        await self._tick_vip_clock()
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
                await self._audit_vips()

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
        """Hand a departing VIP's crown to a teammate (``leave_policy``).

        With nobody left on the team (or ``leave_policy = "death"``) the quit
        counts as the VIP's death, so the round still resolves. The hook may
        run while the departing player is still in the roster (before
        PlayerLeft) or after removal; every count below excludes it.
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
            await self._resolve_vip_departure(team, player)
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
        connection = getattr(player, "connection", None)
        if new_team != old_team and getattr(connection, "in_game", False):
            # A spectator (or the other team) stepping onto the VIP-less team.
            self._tell_locked_out(connection, player)

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

    @staticmethod
    def _city_spawn_zone(team: int):
        """Make a retail-style authored volume from surveyed Chicago corners.

        A little vertical padding admits nearby floors; a 2D bounding box
        alone cannot distinguish an upper roof from a street-level floor.
        The actual Z is always validated against the live map at spawn time.
        """
        from server.map_metadata import MapZone

        corners = VIP_CITY_OF_CHICAGO_SPAWN_CORNERS[team]
        x0, x1 = sorted((corners[0][0], corners[1][0]))
        y0, y1 = sorted((corners[0][1], corners[1][1]))
        # A player position is approximately 2.75 above its supporting block.
        floor0, floor1 = sorted((corners[0][2] + 2.75, corners[1][2] + 2.75))
        cx, cy, cz = (x0 + x1) / 2, (y0 + y1) / 2, (floor0 + floor1) / 2
        return MapZone(
            "spawn", team, cx, cy, cz,
            (x0 - cx, x1 - cx, y0 - cy, y1 - cy,
             floor0 - cz - 5.0, floor1 - cz + 5.0),
            "vip_cityofchicago_surveyed_spawn",
        )

    def _get_city_spawn_candidates(self, world, team: int):
        key = (id(world), team)
        entry = self._vip_city_spawn_cache.get(key)
        if entry is not None:
            return entry
        zone = self._city_spawn_zone(team)
        candidates = list(world._zone_candidates(zone))
        self._vip_city_spawn_cache[key] = (zone, candidates)
        logger.info(
            "VIP CityOfChicago surveyed spawn: team %s has %d valid candidate floors",
            team, len(candidates),
        )
        return zone, candidates

    def get_spawn_point(self, player: Player) -> tuple[float, float, float]:
        """Use surveyed CityOfChicago rectangles; standard spawns elsewhere.

        Use WorldManager's authored-zone safety logic to support both outdoor
        terrain and permitted indoor floors. A cached floor can become unsafe
        after block edits, so revalidate it on every spawn. If the entire box
        is blocked, fall back to the standard rescue-capable spawning path.
        """
        world = getattr(self.server, "world_manager", None)
        team = int(getattr(player, "team", -1))
        name = str(getattr(world, "map_name", "") or "").lower()
        if (name.removesuffix(".vxl") == "cityofchicago"
                and team in VIP_CITY_OF_CHICAGO_SPAWN_CORNERS
                and getattr(world, "map", None) is not None):
            try:
                zone, candidates = self._get_city_spawn_candidates(world, team)
                # Randomise 32 choices for a distributed team spawn without
                # repeatedly scanning the complete rectangle every tick.
                sample = random.sample(candidates, min(32, len(candidates)))
                for candidate in sample:
                    position = world.spawn_candidate_position(candidate, (zone,))
                    if position is not None:
                        return tuple(float(value) for value in position)
                # Handles a damaged or heavily rebuilt spawn zone; re-enumerate
                # once before falling back to the normal safety resolver.
                key = (id(world), team)
                self._vip_city_spawn_cache.pop(key, None)
                zone, candidates = self._get_city_spawn_candidates(world, team)
                for candidate in random.sample(candidates, min(64, len(candidates))):
                    position = world.spawn_candidate_position(candidate, (zone,))
                    if position is not None:
                        return tuple(float(value) for value in position)
                logger.warning(
                    "VIP CityOfChicago spawn zone team %s is blocked; "
                    "using the normal safe-spawn fallback", team,
                )
            except Exception:
                logger.exception(
                    "VIP CityOfChicago custom spawn error for team %s; "
                    "using normal spawns", team,
                )
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
        for team in sorted(self._revealed_enemy_to):
            self._set_enemy_map_visibility(team, True, connection=connection)
        self._tell_locked_out(connection)

    def _locked_out(self, player) -> bool:
        """Whether ``player`` waits out this sub-round: its team's VIP died."""
        team = int(getattr(player, "team", -1))
        return bool(
            player is not None
            and not self.ended
            and self.phase is VIPPhase.ACTIVE
            and team in _PLAYABLE_TEAMS
            and not self.vip_alive.get(team, True)
            and not self.respawn_enabled.get(team, True)
            and not bool(getattr(player, "alive", False))
        )

    def _tell_locked_out(self, connection, player=None) -> bool:
        """Tell a player who arrives on a VIP-less team why it stays dead.

        Retail VIP_ALREADY_DEAD, "your team's V.I.P. is already dead". No
        client binary references the id, so the retail server sent it; when
        is not recorded. This is the one moment the sentence describes: the
        player joins a team, and that team's VIP is already dead.
        """
        if player is None:
            player = getattr(connection, "player", None)
        if connection is None or not self._locked_out(player):
            return False
        try:
            self.send_localised_message_to(connection, "VIP_ALREADY_DEAD")
        except Exception:  # noqa: BLE001 - the line is informational
            logger.debug("VIP_ALREADY_DEAD skipped", exc_info=True)
            return False
        return True

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
        self._vip_points_awarded.clear()
        # BaseMode treats the timer as a whole-match timer. VIP treats it
        # as an independent sub-round timer. This also prevents a stale clock
        # from expiring while clients respawn / the next VIP is selected.
        self._reset_subround_clock()

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
                # Round reset is NOT a combat death: do not send KillAction.
                # RoundLifecycle.respawn_player safely replaces even a live
                # body (the same transition is used by unofficial round modes)
                # and puts it at its team's spawn with replenished equipment.
                # A player already killed during combat keeps that real death.
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
        # Keep promotions outside ACTIVE while both live Characters receive
        # the in-place boss class/loadout change.
        self.vip_alive = {TEAM1: False, TEAM2: False}
        self.respawn_enabled = {TEAM1: True, TEAM2: True}
        self.selection_deadline = None
        now = time.monotonic()
        self._next_vip_survival_score = (
            now + float(CG.VIP_SCORE_LIVEVIP_INTERVAL)
        )
        self._next_escort_score = now + float(CG.VIP_SCORE_ESCORT_INTERVAL)

        for team in _PLAYABLE_TEAMS:
            self._promote_vip(team, selected[team])
        self.vip_alive = {TEAM1: True, TEAM2: True}
        self._reset_subround_clock()
        self.phase = VIPPhase.ACTIVE
        from server import achievements

        # "Start the round as VIP" is this draw, not a later successor.
        achievements.vip_round_started(self.server, selected)
        await self.broadcast_localised_message("VIP_START", override_previous=True)

    def _promote_vip(self, team: int, vip: Player) -> None:
        """Promote a living gangster to VIP without moving or killing them.

        The native GameScene accepts SetClassLoadout(13) with instant=1 as a
        live class/equipment change, just as the built-in Tutorial does.  A
        normal respawn here incorrectly sends the chosen VIP back to base.
        Retain the exact position, direction and current live Character.
        Only a teammate who was already dead needs an ordinary respawn.
        """
        from server.game_constants import MAX_HEALTH
        from shared.packet import SetClassLoadout, SetHP

        vip_class = int(C.MAFIA_VIPS[team])
        vip.apply_class_selection(normalize_class_selection(vip_class))
        vip.pending_selection = None
        vip.pending_class_id = None
        vip.pending_loadout = None

        if bool(getattr(vip, "alive", False)) and bool(getattr(vip, "spawned", False)):
            # A class_id assignment refreshes the authoritative movement
            # profile in Player.class_id's setter; notify every client of the
            # active boss model and inventory without CreatePlayer/KillAction.
            payload = SetClassLoadout()
            payload.player_id = int(vip.id)
            payload.class_id = vip_class
            payload.instant = 1
            payload.loadout = list(vip.loadout)
            payload.prefabs = list(vip.prefabs)
            payload.ugc_tools = list(getattr(vip, "ugc_tools", ()) or ())
            self.server.broadcast(bytes(payload.generate()), reliable=True)
            if vip.loadout:
                vip.set_tool(int(vip.loadout[-1]), raw=True)
            # Server ammo/tool counters must agree with the freshly granted
            # client inventory, without resetting location or death history.
            vip._reset_ammo()
            vip._reset_equipment_state()
        else:
            # Rare case: the selected player was killed before the VIP draw
            # completed.  There is no live Character to update in place.
            self.server.respawn_player(vip)

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

    def _reassign_vip(self, team: int, departed=None) -> bool:
        """Hand a departed live VIP's crown to another member of its team.

        Returns False (nothing changed) when the team has nobody left who can
        hold it; the caller then resolves the departure as a VIP death.
        Living members are preferred so the hand-over does not double as a
        free respawn for a dead teammate.
        """
        candidates = self._team_candidates(team, exclude=departed)
        if not candidates:
            return False
        living = [
            player for player in candidates
            if bool(getattr(player, "alive", False))
            and bool(getattr(player, "spawned", False))
        ]
        successor = random.choice(living or candidates)
        self.vip_alive[team] = False
        self._vip_attackers[team] = {}
        self.vips[team] = successor
        try:
            self._promote_vip(team, successor)
        except Exception:
            logger.exception(
                "VIP hand-over failed for team %s (player %s)",
                team, getattr(successor, "id", "?"),
            )
            self.vips[team] = departed
            return False
        self.vip_alive[team] = True
        logger.info(
            "VIP of team %s left; %s is the new VIP",
            team, getattr(successor, "name", "?"),
        )
        return True

    async def _resolve_vip_departure(self, team: int, vip) -> None:
        """A live VIP left the round: hand the crown over, or lose the VIP."""
        if self.leave_policy == "reassign" and self._reassign_vip(team, vip):
            return
        await self._kill_vip(team, vip, killer=None)

    async def _audit_vips(self) -> None:
        """Safety net: a live VIP that vanished without a leave hook.

        Plugins and roster paths that drop a player without calling
        ``on_player_leave`` would otherwise leave a ghost crown: the team
        keeps respawning, nobody can kill its VIP and the sub-round never
        ends.
        """
        for team in _PLAYABLE_TEAMS:
            vip = self.vips.get(team)
            if (
                self.ended
                or self.phase is not VIPPhase.ACTIVE
                or vip is None
                or not self.vip_alive.get(team, False)
            ):
                continue
            connection = getattr(vip, "connection", None)
            if (
                self._is_connected(vip)
                and int(getattr(vip, "team", -1)) == team
                and connection is not None
                and bool(getattr(connection, "in_game", True))
            ):
                continue
            await self._resolve_vip_departure(team, vip)
        await self._check_team_elimination()

    async def _kill_vip(
        self,
        team: int,
        vip: Player,
        killer: Player | None,
    ) -> None:
        """Award one team point and let the attackers locate survivors."""
        # Redundant queued death/leave callbacks must never grant extra points.
        # Use the scored-team guard rather than vip_alive: a failed VIP
        # reassign temporarily sets vip_alive=False before death resolution.
        if team in self._vip_points_awarded:
            return
        self.vip_alive[team] = False
        self.respawn_enabled[team] = not self.sudden_death_enabled
        attackers = self._vip_attackers.get(team, {})
        self._vip_attackers[team] = {}
        scoring_team = _other_team(team)
        if team not in self._vip_points_awarded:
            from server.scoreboard import send_team_score

            self._vip_points_awarded.add(team)
            team_record = self.server.teams[scoring_team]
            team_record.add_score(1)
            send_team_score(self.server, team_record)
            logger.info("VIP killed: team %s scored, score=%s/%s",
                        scoring_team,
                        self.server.teams[TEAM1].score,
                        self.server.teams[TEAM2].score)
        # Packet 83's team id is the team that GAINS radar visibility.
        self._reveal_enemies_on_map(scoring_team)
        if self.sudden_death_enabled:
            self._sudden_death_activate_at[team] = (
                time.monotonic() + VIP_SUDDEN_DEATH_GRACE_SECONDS
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

        # A lead such as 3:2 must leave the opponents a chance to answer
        # during the final minute. When that answer ties the score at 3:3,
        # the entire match ends as a draw immediately, with no extra round.
        score1 = int(self.server.teams[TEAM1].score)
        score2 = int(self.server.teams[TEAM2].score)
        if (self.score_limit > 0
                and score1 == score2
                and score1 >= self.score_limit):
            await self._finish_round(None)
        elif not self.sudden_death_enabled:
            # Keep the opt-out's original kill-the-boss instant-end policy.
            await self._finish_round(scoring_team)

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

    def _set_enemy_map_visibility(
        self, team: int, visible: bool, connection=None
    ) -> None:
        """Toggle enemy icons on the minimap for the specified friendly team."""
        from shared.packet import TeamMapVisibility

        packet = TeamMapVisibility()
        packet.team_id = int(team)
        packet.visible = int(bool(visible))
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            connection.send(data, reliable=True)

    def _reveal_enemies_on_map(self, team: int) -> None:
        if team not in self._revealed_enemy_to:
            self._revealed_enemy_to.add(team)
            self._set_enemy_map_visibility(team, True)

    def _cap_round_time_to_one_minute(self) -> None:
        """Cap this sub-round and immediately switch to the ending music.

        BaseMode looks at time_limit - elapsed_time; VIP's one-minute cap
        uses _subround_deadline instead, so its generic music threshold can
        still be many minutes away when the HUD switches to 1:00.
        """
        remaining = self.countdown_seconds_remaining()
        if self.time_limit <= 0 and self._subround_deadline is None:
            remaining = float("inf")
        if remaining > VIP_FINAL_MINUTE_SECONDS:
            self._subround_deadline = time.monotonic() + VIP_FINAL_MINUTE_SECONDS
            from server.scoreboard import send_round_timer

            send_round_timer(self.server, VIP_FINAL_MINUTE_SECONDS, reliable=True)
        # A one-minute clock (whether freshly capped or already shorter)
        # always gets exactly one game_ending track.  The same flag feeds
        # BaseMode.join_music_track for late joiners and prevents duplicate
        # playback from the normal BaseMode timeout check.
        if not self._timeout_music_played:
            from server.audio import play_timeout_music

            self._timeout_music_played = True
            play_timeout_music(self.server)

    def _clear_sudden_death(self, *, clear_enemy_visibility: bool = True) -> None:
        """Stop VIP bleed/timing; optionally retain enemy radar for the pause.

        The losing VIP's team must remain visible to its opponents until the
        next sub-round starts, including when the 60-second deadline expires.
        Only _begin_round or a mode retirement revokes the visibility flags.
        """
        self._subround_deadline = None
        if clear_enemy_visibility:
            for team in sorted(self._revealed_enemy_to):
                self._set_enemy_map_visibility(team, False)
            self._revealed_enemy_to.clear()
        self._sudden_death_activate_at = {TEAM1: None, TEAM2: None}
        self._sudden_death_damage_at = {TEAM1: None, TEAM2: None}
        self._vip_attackers = {TEAM1: {}, TEAM2: {}}

    async def _advance_sudden_death(self, now: float) -> None:
        """After 6 s, cap the clock and bleed the VIP-less team once per s."""
        if self._subround_deadline is not None and now >= self._subround_deadline:
            # _tick_vip_clock owns expiration, including the accepted-death
            # drain.  Do not inflict another damage tick after the buzzer.
            return
        for team in _PLAYABLE_TEAMS:
            if self.ended or self.phase is not VIPPhase.ACTIVE:
                return
            activate_at = self._sudden_death_activate_at.get(team)
            if activate_at is not None and now >= activate_at:
                self._sudden_death_activate_at[team] = None
                self._cap_round_time_to_one_minute()
                self._sudden_death_damage_at[team] = now  # first HP tick at t+6s
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
        """End a sub-round, then decide whether either team won the match."""
        if self.phase is VIPPhase.INTERMISSION or self.ended:
            return
        from server import achievements

        achievements.vip_round_finished(self.server, self)
        self.phase = VIPPhase.INTERMISSION
        # Zero the displayed countdown immediately. The once-per-second
        # SimulationRuntime refresh now reads zero too; it cannot resurrect
        # the old 15-minute timer when the 60-second VIP round ends early.
        from server.scoreboard import send_round_timer

        send_round_timer(self.server, 0.0, reliable=True)
        # Do not clear TeamMapVisibility here: revealing opponents is part of
        # the defeated VIP's end-of-round state. Keep it for intermission,
        # including after a timed expiration, then clear in _begin_round.
        self._clear_sudden_death(clear_enemy_visibility=False)
        self._clear_vip_markers()
        self._last_man_announced.clear()
        self.rounds_played += 1

        # VIP deaths award points immediately; _finish_round never awards
        # another point. The match ends once a team has the required score
        # after the final sub-round, or immediately if the last VIP kill
        # equalised the score at 3:3 (no tiebreak round).
        score1 = int(self.server.teams[TEAM1].score)
        score2 = int(self.server.teams[TEAM2].score)
        if self.score_limit > 0 and max(score1, score2) >= self.score_limit:
            if score1 != score2:
                winning_team = TEAM1 if score1 > score2 else TEAM2
                await self._end_by_score(winning_team)
                return
            logger.info(
                "VIP draw: %d:%d after %d sub-rounds; ending match",
                score1, score2, self.rounds_played,
            )
            # self._end_by_time is overridden by VIP to finish only one
            # sub-round, so explicitly call BaseMode's end-of-match flow.
            await super()._end_by_time()
            return

        if winner is None:
            await self.broadcast_localised_message("GAME_DRAWN")
        else:
            await self.broadcast_localised_message(
                "TEAM_DEFEAT", (self.server.teams[winner].name,),
                localise_parameters=True,
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
