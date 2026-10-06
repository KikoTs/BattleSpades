"""Zombie infection mode for the retail MODE_ZOMBIE client scene.

The server owns role assignment and infection; the client already owns the
zombie models, mode HUD, sounds, and class-specific movement.  A round starts
with every connected player as a survivor, arms the retail outbreak timer,
selects patient zero, and permanently converts each later survivor death.
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
from server.class_data import BATTLESPADES_TEAM_CLASSES
from server.class_selection import ClassSelection, normalize_class_selection
from server.game_constants import (
    KILL_CLASS_CHANGE,
    KILL_TEAM_CHANGE,
    TEAM1,
    TEAM2,
    TEAM_SPECTATOR,
)

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player


logger = logging.getLogger(__name__)

# Retail Zombie mode assigns its infected models to Blue/team 1 and the
# survivors to Green/team 2. Reversing these roles makes zombie hands inherit
# the green palette, breaks native team-specific HUD assumptions, and has
# triggered the Tab-list crash reported by retail playtesters.
ZOMBIE_TEAM = TEAM1
SURVIVOR_TEAM = TEAM2
_ZOMBIE_CLASSES = frozenset((int(C.CLASS_ZOMBIE),))
_ZOMBIE_PREFABS = tuple(
    str(name)
    for name in C.PREFAB_LISTS.get(int(C.CLASS_PREFABS_ZOMBIE), ())
)
# Retail survivors pick from DEFAULT_TEAM_CLASSES (alias A93), which has no
# Rocketeer; BattleSpades survivors keep the Rocketeer (restored 2026-10-01).
_SURVIVOR_CLASS_ORDER = BATTLESPADES_TEAM_CLASSES
_SURVIVOR_CLASSES = frozenset(_SURVIVOR_CLASS_ORDER)
_PLAYABLE_TEAMS = (ZOMBIE_TEAM, SURVIVOR_TEAM)
# Deaths that only replace a Character (loadout edit, team/role change) are
# not combat deaths: they must never infect a survivor or score.
_TRANSITION_KILLS = frozenset((
    int(C.KILL.FORCED_TEAM_CHANGE_KILL),
    int(C.KILL.TEAM_CHANGE_KILL),
    int(C.KILL.CLASS_CHANGE_KILL),
))
# StateData team names are client string-table ids (aoslib/strings/*.py
# ZOMBIE_TEAM = u'Zombie', SURVIVOR_TEAM = u'Survivor', present in every
# shipped language).  A literal such as "Zombies" renders "Missing string".
_ZOMBIE_TEAM_NAME_ID = "ZOMBIE_TEAM"
_SURVIVOR_TEAM_NAME_ID = "SURVIVOR_TEAM"
# Retry cadence when the outbreak deadline passes while a survivor is between
# lives; avoids re-announcing ZOMBIE_VIRUS_RELEASED every tick.
_OUTBREAK_RETRY_SECONDS = 1.0


class ZombiePhase(Enum):
    """Authoritative phase of one infection round."""

    WAITING = auto()
    COUNTDOWN = auto()
    ACTIVE = auto()
    # Round result shown; the same map restarts after a short pause.
    INTERMISSION = auto()
    # Bounded per-tick respawn of every body back into the survivors.
    RESETTING = auto()


class ZombieMode(BaseMode):
    """Run survivor preparation, infection, last-man radar, and round wins.

    All methods execute on the 60 Hz gameplay event loop.  Role changes are
    committed before ``RoundLifecycle`` processes respawns in the same tick,
    so a dead survivor's next CreatePlayer can never expose a stale human body
    or weapon loadout to any client.
    """

    # Zombie scores only through its own ZOM_* economy.
    generic_scoring_enabled = False

    name = "Zombie"
    description = "Survive the outbreak, or infect every remaining human."

    def __init__(self, server) -> None:
        super().__init__(server)
        md = mode_data.get("zom")
        overlay = getattr(server.config, "mode_settings", {}).get("zom", {})
        # Retail ZOM_NOOF_ROUNDS_BEFORE_NEXT_MAP: rounds played on one map
        # before the scoreboard / map vote.  Team score counts rounds won.
        self.score_limit = max(1, int(overlay.get(
            "score_limit",
            server.config.game_rules.get("RULE_ZOMBIE_NOOF_ROUNDS"),
        )))
        self.round_intermission = max(0.0, float(overlay.get(
            "round_intermission",
            CG.ZOM_TIME_AFTER_ZOMBIE_WIN_BEFORE_SCORES,
        )))
        self.round_respawns_per_tick = max(1, int(overlay.get(
            "round_respawns_per_tick", 4
        )))
        self.time_limit = server.config.configured_time_limit(
            "zom", CG.ZOM_ROUND_TIME
        )
        self.infection_delay = float(overlay.get(
            "infection_delay", CG.ZOM_TIME_BEFORE_FIRST_INFECTION
        ))
        self.first_infected_count = max(1, int(overlay.get(
            "first_infected",
            server.config.game_rules.get("RULE_NOOF_FIRST_INFECTED_ZOMBIES"),
        )))
        self.minimum_players = max(2, int(overlay.get("minimum_players", 2)))
        self.zombie_respawn_time = max(0.0, float(overlay.get(
            "zombie_respawn_time", CG.ZOM_RESPAWN_AS_ZOMBIE_TIME
        )))

        self.phase = ZombiePhase.WAITING
        self.infection_deadline: float | None = None
        self.patient_zero_ids: set[int] = set()
        # Patient-zero bodies whose first zombie life has not spawned yet;
        # that life gets FIRST_ZOMBIE_SPAWN_PROTECTION_TIME (0.5 s) instead
        # of the ordinary RULE_SPAWN_PROTECTION_TIME window.
        self._patient_zero_protection_pending: dict[int, Player] = {}
        self.last_survivor_id: int | None = None
        self._next_survival_score_at: float | None = None
        self._next_last_man_score_at: float | None = None
        # A newly infected player respawns where the human died. Without this
        # one-use anchor, generic team spawning teleports every conversion to
        # the opposite side of large maps.
        self._infection_spawn_by_player: dict[
            int, tuple[float, float, float]
        ] = {}
        # The human loadout each infected player had, restored next round.
        self._survivor_selection_by_player: dict[int, ClassSelection] = {}
        self.rounds_played = 0
        # Audio: the pick countdown sounds once per armed countdown, and a
        # last-man / survivor-win track must be swapped back to the bed when
        # the next round starts.
        self._pick_timer_sounded = False
        self._round_music_changed = False
        self._round_task: asyncio.Task | None = None
        self._round_reset_queue: deque[Player] = deque()

    async def on_mode_start(self) -> None:
        """Reset the match and return every playing body to the survivors."""
        await self._cancel_round_task()
        self._round_reset_queue.clear()
        self._clear_last_survivor_marker()
        await super().on_mode_start()
        self.rounds_played = 0
        self._survivor_selection_by_player.clear()
        self._reset_round_state()
        self._publish_team_locks()
        for team in self.server.teams.values():
            team.reset()
        for player in list(self.server.players.values()):
            if not self._is_playing(player):
                continue
            self._assign_survivor(player)
        await self._arm_countdown_if_ready(time.time())
        logger.info(
            "Zombie mode started (round=%.0fs outbreak=%.0fs first=%d)",
            self.time_limit,
            self.infection_delay,
            self.first_infected_count,
        )

    async def on_mode_end(self, winner: int | None = None) -> None:
        """Clear transient radar state before the native score transition."""
        self._clear_last_survivor_marker()
        await super().on_mode_end(winner)

    async def deactivate(self) -> None:
        """Remove the last-man marker before a map or mode rollover."""
        await self._cancel_round_task()
        self._round_reset_queue.clear()
        self._clear_last_survivor_marker()
        self._infection_spawn_by_player.clear()
        self._survivor_selection_by_player.clear()
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        """Advance the outbreak timer and bounded periodic survivor scoring."""
        if self.ended:
            return
        now = time.time()
        if self.phase is ZombiePhase.INTERMISSION:
            return
        if self.phase is ZombiePhase.RESETTING:
            await self._drain_round_reset(now)
            return
        if self.phase is ZombiePhase.WAITING:
            await self._arm_countdown_if_ready(now)
        if (
            self.phase is ZombiePhase.COUNTDOWN
            and len(self._connected_players()) < self.minimum_players
        ):
            self.phase = ZombiePhase.WAITING
            self.infection_deadline = None
        self._sound_pick_timer_if_due(now)
        if (
            self.phase is ZombiePhase.COUNTDOWN
            and self.infection_deadline is not None
            and now >= self.infection_deadline
        ):
            await self._start_outbreak(now)
        if self.phase is ZombiePhase.ACTIVE:
            if await self._abort_if_underpopulated():
                return
            # ZOM_ROUND_TIME measures survival after Patient Zero is chosen,
            # not server uptime or time spent waiting for enough players.
            if self._is_final_round():
                await super().on_tick(tick)
            else:
                await self._advance_round_clock(now)
            if self.ended or self.phase is not ZombiePhase.ACTIVE:
                return
            self._award_periodic_survival_score(now)
            await self._check_population()

    async def _advance_round_clock(self, now: float) -> None:
        """BaseMode's round clock for a non-final round, minus the map vote.

        The retail map ballot belongs to the last round on a map; opening it
        in the final minute of round one of three would stage the next map
        two rounds early.
        """
        self.elapsed_time = now - self.start_time
        if self.time_limit <= 0:
            return
        remaining = self.time_limit - self.elapsed_time
        from server.audio import TIMEOUT_MUSIC_SECONDS, play_timeout_music

        if not self._timeout_music_played and remaining <= TIMEOUT_MUSIC_SECONDS:
            self._timeout_music_played = True
            play_timeout_music(self.server)
        self._announce_countdown(remaining)
        if self.elapsed_time >= self.time_limit:
            if self._timeout_events_remaining is None:
                self._timeout_events_remaining = len(
                    getattr(self.server, "_mode_events", ())
                )
            if self._timeout_events_remaining == 0:
                await self._end_by_time()
        else:
            self._timeout_events_remaining = None

    async def on_player_join(self, player: Player) -> None:
        """Apply the role chosen by ``prepare_join_team`` and arm a round."""
        if not self._is_playing(player):
            # Spectators keep their native spectator camera and never count
            # toward the infection roster until they pick a team.
            return
        target = ZOMBIE_TEAM if self.phase is ZombiePhase.ACTIVE else SURVIVOR_TEAM
        if self.phase is ZombiePhase.ACTIVE and int(player.team) == SURVIVOR_TEAM:
            # Joined as a human before the outbreak and the queued join event
            # drained after it: that body is a legitimate survivor.
            target = SURVIVOR_TEAM
        if int(player.team) != target and bool(getattr(player, "alive", False)):
            # The phase flipped between join and this event. KillAction is
            # the native-safe model swap; a silent team mutation would leave
            # the wrong Character alive on every client.
            player.die(kill_type=KILL_TEAM_CHANGE)
        if target == ZOMBIE_TEAM:
            self._assign_zombie(player)
        else:
            self._assign_survivor(player)
            await self._arm_countdown_if_ready(time.time())

    async def on_player_team_change(
        self,
        player: Player,
        old_team: int,
        new_team: int,
    ) -> None:
        """Commit the role loadout for a spectator who entered the round."""
        if self.server.players.get(int(player.id)) is not player:
            return
        if int(player.team) != int(new_team):
            return
        if int(new_team) == ZOMBIE_TEAM:
            self._assign_zombie(player)
        elif int(new_team) == SURVIVOR_TEAM:
            self._assign_survivor(player)
            await self._arm_countdown_if_ready(time.time())

    async def on_player_leave(self, player: Player) -> None:
        """Replace a departed sole zombie so the infection cannot soft-lock."""
        player_id = int(player.id)
        self.patient_zero_ids.discard(player_id)
        self._patient_zero_protection_pending.pop(player_id, None)
        self._infection_spawn_by_player.pop(player_id, None)
        self._survivor_selection_by_player.pop(player_id, None)
        if self.last_survivor_id == player_id:
            self.last_survivor_id = None
        if self.ended:
            # A map transition retires the old mode's roster; no gameplay.
            return
        if self.phase is ZombiePhase.COUNTDOWN:
            if len(self._connected_players(exclude=player)) < self.minimum_players:
                self.phase = ZombiePhase.WAITING
                self.infection_deadline = None
            return
        if self.phase is not ZombiePhase.ACTIVE:
            return
        if await self._abort_if_underpopulated(exclude=player):
            return
        zombies = self._zombies(exclude=player)
        survivors = self._survivors(exclude=player)
        if not zombies and survivors:
            replacement = random.choice(survivors)
            await self._infect(replacement, patient_zero=True)
            self.announce_localised_to_team(
                SURVIVOR_TEAM, "ZOMBIE_INFECTION_DETECTED", override_previous=True
            )
        await self._check_population(exclude=player)

    async def on_player_spawn(self, player: Player) -> None:
        """Give patient zero's first zombie life the retail 0.5 s window."""
        await super().on_player_spawn(player)
        pending = self._patient_zero_protection_pending.pop(
            int(getattr(player, "id", -1)), None
        )
        if pending is not player or int(getattr(player, "team", -1)) != ZOMBIE_TEAM:
            return
        self.limit_spawn_protection(player, float(C.FIRST_ZOMBIE_SPAWN_PROTECTION_TIME))

    @staticmethod
    def limit_spawn_protection(player, window: float) -> None:
        """Cap this life's spawn protection at ``window`` seconds.

        Player.spawn_protection_remaining() derives the window from
        RULE_SPAWN_PROTECTION_TIME and ``spawned_at`` (it also feeds the
        WorldUpdate spawn-protection timer every client draws). Moving the
        life's start back by the surplus leaves exactly ``window`` seconds;
        the protection still ends early the moment the player attacks. When
        the server rule is shorter than ``window`` (or off) nothing changes.
        """
        try:
            player.spawn_protection_cap = max(0.0, float(window))
        except (AttributeError, TypeError, ValueError):
            return

    async def on_player_death(
        self,
        player: Player,
        killer: Player | None,
        kill_type: int,
    ) -> None:
        """Permanently convert every survivor combat death after the outbreak.

        Character replacements (loadout edit, team change, round reset) are
        not deaths in the infection sense and never convert or score.
        """
        if self.ended or int(kill_type) in _TRANSITION_KILLS:
            return
        if self.phase is not ZombiePhase.ACTIVE or int(player.team) != SURVIVOR_TEAM:
            return
        if self.server.players.get(int(player.id)) is not player:
            # Departed before the queued death drained; the leave hook owns it.
            return
        if killer is not None and killer is not player and int(killer.team) == ZOMBIE_TEAM:
            self._award_player(
                killer,
                int(CG.ZOM_SCORE_KILL_SURVIVOR),
                reason=int(C.ZOM_KILLSURVIVOR_SCORE_REASON),
            )
        await self._infect(player, patient_zero=False)
        await self._check_population()

    async def on_player_kill(
        self,
        killer: Player,
        victim: Player,
        kill_type: int,
    ) -> None:
        """Pay survivors for zombie kills, plus the last-survivor bonus.

        A survivor killing a zombie earns the generic kill/headshot/melee
        score the loading screen lists for every mode; it used to earn
        nothing at all. Zombies keep only their own ZOM_SCORE_SURVIVORKILL
        (awarded in ``on_player_death``), so nothing stacks for them.
        """
        if (
            not self.ended
            and self.phase is ZombiePhase.ACTIVE
            and int(killer.team) == SURVIVOR_TEAM
            and int(victim.team) == ZOMBIE_TEAM
        ):
            self.award_generic_kill_score(killer, victim, kill_type, mode_opted_in=True)
        if (
            not self.ended
            and self.phase is ZombiePhase.ACTIVE
            and int(killer.team) == SURVIVOR_TEAM
            and int(victim.team) == ZOMBIE_TEAM
            and self.last_survivor_id == int(killer.id)
        ):
            self._award_player(
                killer,
                int(CG.ZOM_SCORE_LASTMAN_ZOMBIEKILL),
                reason=int(C.ZOM_LASTMAN_ZOMBIEKILL_SCORE_REASON),
            )

    def prepare_join_team(self, requested_team: int) -> int:
        """Force pre-outbreak joins to survivors and late joins to zombies."""
        if self.phase is ZombiePhase.ACTIVE:
            return ZOMBIE_TEAM
        return SURVIVOR_TEAM

    def prepare_join_selection(
        self,
        team: int,
        selection: ClassSelection,
    ) -> ClassSelection:
        """Normalize the untrusted join loadout against the assigned role."""
        if int(team) == ZOMBIE_TEAM:
            return self._zombie_selection()
        class_id = int(selection.class_id)
        if class_id not in _SURVIVOR_CLASSES:
            class_id = int(C.CLASS_SOLDIER)
        return normalize_class_selection(
            class_id,
            selection.loadout,
            selection.prefabs,
            selection.ugc_tools,
        )

    def prepare_bot_selection(
        self,
        team: int,
        selection: ClassSelection,
        *,
        player_id: int,
    ) -> ClassSelection:
        """Commit the validated base Zombie before bot CreatePlayer.

        Fast/Jump remain available to reverse-engineering fixtures, but are
        not rotated into production until their retail movement and balance
        have separate acceptance evidence.
        """

        if int(team) != ZOMBIE_TEAM:
            if int(selection.class_id) in _SURVIVOR_CLASSES:
                return selection
            # Never let a bot spawn a human with a zombie/mode-only kit.
            return normalize_class_selection(int(C.CLASS_SOLDIER))
        return self._zombie_selection(int(C.CLASS_ZOMBIE))

    def allows_class_selection(self, player: Player, selection: ClassSelection) -> bool:
        """Reject cross-role class packets while allowing legal loadout edits.

        During the outbreak survivors keep the class they have (retail:
        "You cannot change class during a zombie outbreak!"); same-class
        loadout edits stay legal.
        """
        team = int(getattr(player, "team", -1))
        allowed = _ZOMBIE_CLASSES if team == ZOMBIE_TEAM else _SURVIVOR_CLASSES
        if int(selection.class_id) not in allowed:
            return False
        if (
            team == SURVIVOR_TEAM
            and self.phase is ZombiePhase.ACTIVE
            and int(selection.class_id) != int(getattr(player, "class_id", selection.class_id))
        ):
            return False
        return True

    def end_message_id(self, winner: int | None) -> int:
        """Retail scoreboard headline: ZOMBIE_WIN / SURVIVOR_WIN."""
        return int(
            C.SURVIVOR_WIN_MESSAGE if winner == SURVIVOR_TEAM else C.ZOMBIE_WIN_MESSAGE
        )

    def _publish_team_locks(self) -> None:
        """Mirror the StateData lock bits to clients already in the scene."""
        from server.scoreboard import send_lock_team, send_team_lock_class

        active = self.phase is ZombiePhase.ACTIVE
        send_lock_team(self.server, ZOMBIE_TEAM, not active)
        send_lock_team(self.server, SURVIVOR_TEAM, active)
        send_team_lock_class(self.server, SURVIVOR_TEAM, active)

    def allows_team_change(self, player: Player, new_team: int) -> bool:
        """Roles are infection state and cannot be escaped through team UI.

        The one legal move is a spectator entering the round, and only onto
        the role a fresh join would get (survivors before the outbreak,
        zombies once it is running).
        """
        if int(getattr(player, "team", -1)) != TEAM_SPECTATOR:
            return False
        return int(new_team) == self.prepare_join_team(int(new_team))

    def can_player_respawn(self, player: Player) -> bool:
        """All connected players keep respawning until the round ends."""
        return not self.ended

    def respawn_time_for(self, player: Player) -> float:
        """Use the retail zero-second infected respawn after any death."""
        if self.phase is ZombiePhase.ACTIVE:
            return self.zombie_respawn_time
        return float(self.server.config.respawn_time)

    def modify_incoming_damage(
        self,
        player: Player,
        amount: int,
        source: Player | None,
        kill_type: int,
    ) -> int:
        """Disable same-role damage even when the global server enables FF."""
        if source is not None and source is not player and int(source.team) == int(player.team):
            return 0
        return int(amount)

    def get_spawn_point(self, player: Player) -> tuple[float, float, float]:
        """Reuse the infection site once, then use normal team spawn regions."""
        infection_spawn = self._infection_spawn_by_player.pop(
            int(player.id), None
        )
        if infection_spawn is not None:
            return infection_spawn
        return tuple(float(value) for value in super().get_spawn_point(player))

    def configure_state_data(self, packet) -> None:
        """Publish asymmetric native class menus and phase-aware team locks."""
        packet.team1_name = _ZOMBIE_TEAM_NAME_ID
        packet.team2_name = _SURVIVOR_TEAM_NAME_ID
        packet.team1_classes = [int(C.CLASS_ZOMBIE)]
        packet.team2_classes = list(_SURVIVOR_CLASS_ORDER)
        packet.team1_locked = self.phase is not ZombiePhase.ACTIVE
        packet.team2_locked = self.phase is ZombiePhase.ACTIVE
        # The Zombie team sees every survivor on the minimap and big map.
        # Retail Player.get_map_icon (player.pyd 0x10013140, player.pyx:486-489)
        # returns no marker for an enemy unless viewer.team.can_see_other_team,
        # and only then reaches the survivor-heart branch (:500-503). The only
        # sources of that flag are this StateData bit (GameScene
        # .process_packet_state_data, gameScene.pyd 0x1023E340) and
        # TeamMapVisibility(83). ``exposed_teams_always_on_minimap`` alone
        # only edge-pins markers that are already visible (:532-535), so
        # without this bit a stock client drew no survivors for zombies.
        packet.team1_can_see_team2 = True
        packet.team2_can_see_team1 = False
        packet.lock_team_swap = True
        # One stable class bypasses the ordinary class picker.  Fast/Jump
        # Zombie lack picker icons in this client and are intentionally hidden.
        packet.team1_locked_class = True
        # Retail locks survivors' class once the outbreak starts (the HUD
        # shows ZOMBIE_OUTBREAK_CLASS_SELECT); TeamLockClass(80) updates
        # players already in the scene at the same boundary.
        packet.team2_locked_class = self.phase is ZombiePhase.ACTIVE
        packet.team1_show_score = False
        packet.team2_show_score = False
        packet.team1_show_max_score = False
        packet.team2_show_max_score = False
        # HeadCount: TEAM_PLAYERS_COUNT_VALUE (0) draws each team's player
        # count ('%i' % count, IDA HeadCount.draw 0x100328E0) -- zombies vs
        # survivors. Zombie keeps no team score, so the generic score type
        # showed nothing useful. Which type retail sent is server-side and
        # unrecovered: inferred from the widget, VERIFY with a capture.
        packet.team_headcount_type = int(C.TEAM_PLAYERS_COUNT_VALUE)

    def configure_initial_info(self, packet) -> None:
        """Force role-safe combat and expose the opposing infection roster.

        Retail ``Player.display_map_icon_out_of_bounds`` (player.pyx:532-535)
        edge-pins an enemy marker when ``exposed_teams_always_on_minimap`` is
        on and the viewing team can see the other team.  It reveals nobody by
        itself: visibility is the StateData ``team1_can_see_team2`` bit set
        in ``configure_state_data``.  This is distinct from
        ``high_minimap_visibility``, whose VIP icon is reserved for the final
        survivor.
        """
        packet.friendly_fire = 0
        packet.exposed_teams_always_on_minimap = 1
        # RULE_CLASS_SPEED is already in movement_speed_multipliers: the
        # builder and Player authority share class_data.rule_speed_scale.
        # Scaling the list again here made clients predict rule squared.

    def start_cue_for(self, player):
        """ZOMBIE_START_SURVIVOR / ZOMBIE_START_ZOMBIE by team.

        "You have been infected! Kill the survivors!" is the zombie team's
        start line (the same text as YOU_HAVE_BEEN_INFECTED, which an
        infected survivor gets instead). Spectators get none.
        """
        team = int(getattr(player, "team", -1))
        if team == SURVIVOR_TEAM:
            return "ZOMBIE_START_SURVIVOR"
        if team == ZOMBIE_TEAM:
            return "ZOMBIE_START_ZOMBIE"
        return None

    def reveal_to(self, connection) -> None:
        """Replay the current last-survivor heart/marker to a late client."""
        super().reveal_to(connection)
        self.send_start_cue_to(connection)
        if self.last_survivor_id is None:
            return
        player = self.server.players.get(self.last_survivor_id)
        if player is not None:
            self._set_high_minimap_visibility(player, True, connection=connection)

    def countdown_seconds_remaining(self, now: float) -> float:
        """Return the phase-appropriate native HUD countdown.

        During preparation the HUD must count toward Patient Zero, not show the
        ten-minute survival clock. Once active it returns the normal remaining
        round time consumed by ``SimulationRuntime``.
        """

        if (
            self.phase is ZombiePhase.COUNTDOWN
            and self.infection_deadline is not None
        ):
            return max(0.0, float(self.infection_deadline) - float(now))
        if self.phase is not ZombiePhase.ACTIVE:
            return 0.0
        return max(0.0, float(self.time_limit) - float(self.elapsed_time))

    async def _arm_countdown_if_ready(self, now: float) -> None:
        if self.phase is not ZombiePhase.WAITING:
            return
        if len(self._connected_players()) < self.minimum_players:
            return
        self.phase = ZombiePhase.COUNTDOWN
        self.infection_deadline = now + max(0.0, self.infection_delay)
        from server.scoreboard import send_round_timer

        self._pick_timer_sounded = False
        # A short outbreak delay is already inside the sound's lead window.
        self._sound_pick_timer_if_due(now)
        send_round_timer(
            self.server,
            self.countdown_seconds_remaining(now),
        )
        # Retail round-start cue (ZOMBIE_START_SURVIVOR "Escape the zombies!",
        # server-sent: no client binary references it) to the survivors, then
        # the pre-outbreak line queued behind it; the HUD shows the timer.
        # Sending the start cue when the outbreak clock arms is our timing.
        self.broadcast_start_cue()
        await self.broadcast_localised_message(
            "ZOMBIE_VIRUS_RELEASED", override_previous=False
        )

    def _sound_pick_timer_if_due(self, now: float) -> None:
        """GAME_MODE_CALLBACK_ZOMBIE_PICK_SOUND: start the 8 s
        zombie_timer_countdown so it ends on the pick. It used to play when
        the 60 s clock armed, finishing 52 s before anything happened. The
        outbreak retry (a candidate between lives) keeps the same countdown
        and must not replay it."""
        if (
            self.phase is not ZombiePhase.COUNTDOWN
            or self.infection_deadline is None
            or getattr(self, "_pick_timer_sounded", False)
        ):
            return
        from server.audio import ZOMBIE_TIMER_SOUND_LEAD, SND_ZOMBIE_TIMER, play_sound

        if self.infection_deadline - now > ZOMBIE_TIMER_SOUND_LEAD:
            return
        self._pick_timer_sounded = True
        play_sound(self.server, SND_ZOMBIE_TIMER, volume=1.0)

    async def _start_outbreak(self, now: float) -> None:
        candidates = self._living_survivors()
        if len(candidates) < 2:
            # Never consume the final human: wait for a viable infection round.
            if len(self._connected_players()) < self.minimum_players:
                self.phase = ZombiePhase.WAITING
                self.infection_deadline = None
            else:
                # Enough players, one is merely between lives. Retry shortly
                # without re-arming (which would replay the timer sound and
                # ZOMBIE_VIRUS_RELEASED every tick at infection_delay=0).
                self.infection_deadline = now + _OUTBREAK_RETRY_SECONDS
            return
        count = min(self.first_infected_count, len(candidates) - 1)
        # Prefer settled clients for Patient Zero: a still-loading joiner
        # would miss YOU_HAVE_BEEN_INFECTED and its own model swap.
        settled = [
            player for player in candidates
            if bool(getattr(getattr(player, "connection", None), "in_game", True))
        ]
        pool = settled if len(settled) >= count else candidates
        selected = random.sample(pool, count)
        self.phase = ZombiePhase.ACTIVE
        self.infection_deadline = None
        # BaseMode's timer starts when the mode object is activated. Infection
        # can wait indefinitely for players, so restart it at the native
        # outbreak boundary or a quiet server eventually ends on first join.
        self.start_time = now
        self.elapsed_time = 0.0
        self._timeout_music_played = False
        self._timeout_events_remaining = None
        self._countdown_fired.clear()
        self._countdown_last_remaining = float("inf")
        self._next_survival_score_at = now + float(CG.ZOM_SCORE_SURVIVE_INTERVAL)
        self._next_last_man_score_at = now + float(CG.ZOM_SCORE_LASTMAN_INTERVAL)
        self._publish_team_locks()
        for player in selected:
            await self._infect(player, patient_zero=True)
        from server import achievements

        # "Start the round as a zombie" is this draw, not a later replacement.
        achievements.zombie_round_started(
            self.server, selected, ZOMBIE_TEAM, SURVIVOR_TEAM
        )
        # Survivors learn of the outbreak; each infected player already got
        # YOU_HAVE_BEEN_INFECTED from _infect. Retail never names Patient Zero.
        self.announce_localised_to_team(
            SURVIVOR_TEAM, "ZOMBIE_INFECTION_DETECTED", override_previous=True
        )
        # This is an outbreak cue, not a per-kill sound. Replaying it for every
        # later infection made ordinary Zombie kills sound like round starts.
        from server.audio import SND_ZOMBIE_BECOME, play_sound

        play_sound(self.server, SND_ZOMBIE_BECOME, volume=1.0)
        from server.scoreboard import send_round_timer

        send_round_timer(self.server, self.time_limit)
        await self._refresh_last_survivor_marker()

    async def _infect(self, player: Player, *, patient_zero: bool) -> None:
        if int(getattr(player, "team", -1)) == ZOMBIE_TEAM:
            return
        class_id = int(getattr(player, "class_id", C.CLASS_SOLDIER))
        if class_id in _SURVIVOR_CLASSES:
            # Restored when the next round returns this body to the humans.
            self._survivor_selection_by_player[int(player.id)] = (
                normalize_class_selection(
                    class_id,
                    getattr(player, "loadout", ()) or (),
                    getattr(player, "prefabs", ()) or (),
                    getattr(player, "ugc_tools", ()) or (),
                )
            )
        position = getattr(player, "position", None)
        if position is not None and len(position) >= 3:
            try:
                self._infection_spawn_by_player[int(player.id)] = tuple(
                    float(value) for value in position[:3]
                )
            except (TypeError, ValueError):
                self._infection_spawn_by_player.pop(int(player.id), None)
        if getattr(player, "alive", False):
            # KillAction is the native-safe model replacement boundary.  A
            # server-only team mutation leaves the old human Character alive.
            player.die(kill_type=KILL_TEAM_CHANGE)
        self._move_to_team(player, ZOMBIE_TEAM)
        selection = self._zombie_selection(self._zombie_class_for(player))
        player.apply_class_selection(selection)
        player.pending_selection = None
        player.pending_class_id = None
        player.pending_loadout = None
        if patient_zero:
            self.patient_zero_ids.add(int(player.id))
            self._patient_zero_protection_pending[int(player.id)] = player
        self.announce_localised_to_player(
            player, "YOU_HAVE_BEEN_INFECTED", override_previous=True
        )

    def _assign_survivor(self, player: Player) -> None:
        self._move_to_team(player, SURVIVOR_TEAM)
        selection = self._survivor_selection_by_player.pop(int(player.id), None)
        if selection is None:
            class_id = int(getattr(player, "class_id", C.CLASS_SOLDIER))
            if class_id in _SURVIVOR_CLASSES:
                selection = normalize_class_selection(
                    class_id,
                    getattr(player, "loadout", ()) or (),
                    getattr(player, "prefabs", ()) or (),
                    getattr(player, "ugc_tools", ()) or (),
                )
            else:
                # A zombie's hand/prefab kit is not a human loadout.
                selection = normalize_class_selection(int(C.CLASS_SOLDIER))
        player.apply_class_selection(selection)
        pending = getattr(player, "pending_selection", None)
        if (
            pending is not None
            and int(getattr(pending, "class_id", -1)) not in _SURVIVOR_CLASSES
        ):
            # A staged zombie kit must not be applied at the next respawn.
            self._clear_pending_selection(player)

    def _assign_zombie(self, player: Player) -> None:
        self._move_to_team(player, ZOMBIE_TEAM)
        player.apply_class_selection(
            self._zombie_selection(self._zombie_class_for(player))
        )
        # A staged human loadout must not be applied at the next respawn.
        self._clear_pending_selection(player)

    @staticmethod
    def _clear_pending_selection(player: Player) -> None:
        player.pending_selection = None
        player.pending_class_id = None
        player.pending_loadout = None

    @staticmethod
    def _zombie_selection(
        class_id: int = int(C.CLASS_ZOMBIE),
    ) -> ClassSelection:
        """Return the complete native Zombie hand/prefab selection."""

        return normalize_class_selection(
            int(class_id),
            prefabs=_ZOMBIE_PREFABS,
        )

    @staticmethod
    def _zombie_class_for(player: Player) -> int:
        """Return the production-validated base Zombie for every player."""

        return int(C.CLASS_ZOMBIE)

    def _move_to_team(self, player: Player, team: int) -> None:
        old_team = int(getattr(player, "team", -1))
        if old_team in self.server.teams:
            self.server.teams[old_team].remove_player(player)
        player.team = int(team)
        self.server.teams[int(team)].add_player(player)

    @staticmethod
    def _is_playing(player: Player) -> bool:
        """Connected and on a playable team (spectators are not in a round)."""
        return (
            getattr(player, "connection", None) is not None
            and int(getattr(player, "team", TEAM_SPECTATOR)) != TEAM_SPECTATOR
        )

    def _connected_players(self, *, exclude: Player | None = None) -> list[Player]:
        return [
            player for player in self.server.players.values()
            if player is not exclude and self._is_playing(player)
        ]

    def _survivors(self, *, exclude: Player | None = None) -> list[Player]:
        return [
            player for player in self._connected_players(exclude=exclude)
            if int(player.team) == SURVIVOR_TEAM
        ]

    def _living_survivors(self) -> list[Player]:
        return [
            player for player in self._survivors()
            if bool(getattr(player, "alive", False))
            and bool(getattr(player, "spawned", False))
        ]

    def _zombies(self, *, exclude: Player | None = None) -> list[Player]:
        return [
            player for player in self._connected_players(exclude=exclude)
            if int(player.team) == ZOMBIE_TEAM
        ]

    async def _check_population(self, *, exclude: Player | None = None) -> None:
        survivors = self._survivors(exclude=exclude)
        if not survivors and self._connected_players(exclude=exclude):
            await self._finish_round(ZOMBIE_TEAM, "ZOMBIE_WIN")
            return
        await self._refresh_last_survivor_marker(exclude=exclude)

    async def _abort_if_underpopulated(
        self, *, exclude: Player | None = None
    ) -> bool:
        """Return an ACTIVE round to WAITING when too few players remain.

        A departure is not a zombie victory: with fewer than
        ``minimum_players`` left there is no viable infection round, so the
        remaining bodies go back to the survivors and the lobby re-arms the
        outbreak once enough players are present again.
        """
        if self.phase is not ZombiePhase.ACTIVE:
            return False
        remaining = self._connected_players(exclude=exclude)
        if len(remaining) >= self.minimum_players:
            return False
        logger.info(
            "Zombie round aborted: %d player(s) left (minimum %d)",
            len(remaining),
            self.minimum_players,
        )
        self._clear_last_survivor_marker()
        self._reset_round_state()
        self._restore_gameplay_music()
        self._publish_team_locks()
        for player in remaining:
            if int(player.team) == SURVIVOR_TEAM:
                continue
            # KillAction is the native-safe model swap; the ordinary respawn
            # then recreates the body as a human.
            if bool(getattr(player, "alive", False)):
                player.die(kill_type=KILL_TEAM_CHANGE)
            self._assign_survivor(player)
        return True

    async def _refresh_last_survivor_marker(
        self, *, exclude: Player | None = None
    ) -> None:
        # Team membership, not liveness: a survivor between lives after a
        # loadout edit is still human, and counting only living bodies made
        # the LAST_MAN_STANDING cue flap on every such respawn.
        survivors = self._survivors(exclude=exclude)
        new_id = int(survivors[0].id) if len(survivors) == 1 else None
        if new_id == self.last_survivor_id:
            return
        self._clear_last_survivor_marker()
        if new_id is None:
            return
        player = self.server.players.get(new_id)
        if player is None:
            return
        self.last_survivor_id = new_id
        self._set_high_minimap_visibility(player, True)
        self._next_last_man_score_at = time.time() + float(
            CG.ZOM_SCORE_LASTMAN_INTERVAL
        )
        # Retail pair: everyone sees LAST_MAN_STANDING, the human gets the
        # personal ZOMBIE_LAST_MAN line.
        await self.broadcast_localised_message("LAST_MAN_STANDING")
        self.announce_localised_to_player(
            player, "ZOMBIE_LAST_MAN", override_previous=True
        )
        # INGAME_MUSIC_LAST_MAN (server-only music id). Skipped while the bed
        # is already a last-man track, so the music does not restart.
        from server.audio import play_last_man_music

        if play_last_man_music(self.server):
            self._round_music_changed = True

    def _clear_last_survivor_marker(self) -> None:
        player_id = self.last_survivor_id
        self.last_survivor_id = None
        if player_id is None:
            return
        player = self.server.players.get(player_id)
        if player is not None:
            self._set_high_minimap_visibility(player, False)

    def mode_marks_player(self, player) -> bool:
        """The last survivor owns the heart marker (never cleared by the
        escape watch)."""
        return (
            self.last_survivor_id is not None
            and int(getattr(player, "id", -1)) == self.last_survivor_id
        )

    def escape_watch_objective_player(self, player) -> bool:
        """Zombies must be able to reach survivors once the outbreak runs:
        a survivor who seals himself inside solid blocks is revealed."""
        return (
            self.phase is ZombiePhase.ACTIVE
            and int(getattr(player, "team", -1)) == SURVIVOR_TEAM
        )

    def _set_high_minimap_visibility(
        self,
        player: Player,
        visible: bool,
        *,
        connection=None,
    ) -> None:
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

    def _award_periodic_survival_score(self, now: float) -> None:
        from server import escape_watch

        # A survivor revealed by the escape watch (sealed in solid blocks,
        # out of the map) is not "surviving" in reach of the zombies: no
        # survive/last-man points while flagged. The clock still advances.
        all_living = self._living_survivors()
        if not all_living:
            return
        living = [
            player for player in all_living
            if not escape_watch.is_flagged(self.server, player)
        ]
        if not living:
            if self._next_survival_score_at is not None and now >= self._next_survival_score_at:
                intervals = 1 + int(
                    (now - self._next_survival_score_at)
                    // float(CG.ZOM_SCORE_SURVIVE_INTERVAL)
                )
                self._next_survival_score_at += (
                    intervals * float(CG.ZOM_SCORE_SURVIVE_INTERVAL)
                )
            if self._next_last_man_score_at is not None and now >= self._next_last_man_score_at:
                intervals = 1 + int(
                    (now - self._next_last_man_score_at)
                    // float(CG.ZOM_SCORE_LASTMAN_INTERVAL)
                )
                self._next_last_man_score_at += (
                    intervals * float(CG.ZOM_SCORE_LASTMAN_INTERVAL)
                )
            return
        if (
            self._next_survival_score_at is not None
            and now >= self._next_survival_score_at
        ):
            intervals = 1 + int(
                (now - self._next_survival_score_at)
                // float(CG.ZOM_SCORE_SURVIVE_INTERVAL)
            )
            points = intervals * int(CG.ZOM_SCORE_SURVIVE)
            for player in living:
                self._award_player(
                    player,
                    points,
                    reason=int(C.ZOM_SURVIVE_SCORE_REASON),
                )
            self._next_survival_score_at += (
                intervals * float(CG.ZOM_SCORE_SURVIVE_INTERVAL)
            )
        last_man = next(
            (
                player for player in living
                if self.last_survivor_id is not None
                and int(player.id) == self.last_survivor_id
            ),
            None,
        )
        if (
            last_man is not None
            and self._next_last_man_score_at is not None
            and now >= self._next_last_man_score_at
        ):
            intervals = 1 + int(
                (now - self._next_last_man_score_at)
                // float(CG.ZOM_SCORE_LASTMAN_INTERVAL)
            )
            self._award_player(
                last_man,
                intervals * int(CG.ZOM_SCORE_LASTMAN),
                reason=int(C.ZOM_LASTMAN_SCORE_REASON),
            )
            self._next_last_man_score_at += (
                intervals * float(CG.ZOM_SCORE_LASTMAN_INTERVAL)
            )

    def _award_player(
        self,
        player: Player,
        points: int,
        *,
        reason: int,
    ) -> None:
        if points <= 0:
            return
        if self.server.players.get(int(player.id)) is not player:
            # Departed (or its compact id was reused): no SetScore for a ghost.
            return
        player.score = int(getattr(player, "score", 0)) + int(points)
        from server.scoreboard import send_player_score

        send_player_score(self.server, player, reason=reason)

    async def _end_by_time(self) -> None:
        """Survivors win the native round if any living human lasts 600s."""
        if self.ended:
            return
        # Any human still on the survivor team (even one between lives after
        # a loadout edit) means the outbreak was contained.
        winner = SURVIVOR_TEAM if self._survivors() else ZOMBIE_TEAM
        await self._finish_round(
            winner, "SURVIVOR_WIN" if winner == SURVIVOR_TEAM else "ZOMBIE_WIN"
        )

    def _is_final_round(self) -> bool:
        """True while the round in progress is the last one on this map."""
        return self.rounds_played + 1 >= self.score_limit

    async def _finish_round(self, winner: int, string_id: str) -> None:
        """Score one round, then restart it in place or end the match.

        Retail plays ZOM_NOOF_ROUNDS_BEFORE_NEXT_MAP rounds per map.  Every
        round shows ZOMBIE_WIN / SURVIVOR_WIN; only the last one hands over
        to the native scoreboard and map vote via ``on_mode_end``.  The team
        score is the number of rounds each role has won.
        """
        if self.ended or self.phase in (
            ZombiePhase.INTERMISSION,
            ZombiePhase.RESETTING,
        ):
            return
        from server.scoreboard import send_team_score

        bonus_players = self._survivors() if winner == SURVIVOR_TEAM else []
        from server import achievements

        achievements.zombie_round_finished(self.server, bonus_players)
        self.phase = ZombiePhase.INTERMISSION
        self.infection_deadline = None
        # In-scene clients still hold the outbreak locks (Zombie open,
        # Survivor + class locked) while the server now only admits
        # survivors: republish the intermission locks at the boundary.
        self._publish_team_locks()
        self._clear_last_survivor_marker()
        self.rounds_played += 1
        team = self.server.teams[winner]
        team.add_score(1)
        send_team_score(self.server, team)
        await self.broadcast_localised_message(string_id)
        if winner == SURVIVOR_TEAM and self.rounds_played < self.score_limit:
            # GAME_MODE_CALLBACK_SURVIVOR_WIN_MUSIC. The final round's win
            # gets the ending track from the base end sequence instead.
            from server.audio import play_ending_music

            play_ending_music(self.server)
            self._round_music_changed = True
        # SURVIVOR_WIN promises "Survivors receive a score bonus!".
        for player in bonus_players:
            self._award_player(
                player,
                int(CG.ZOM_EXTRA_INDIVIDUAL_SCORE_FOR_SURVIVAL),
                reason=int(C.ZOM_SURVIVE_SCORE_REASON),
            )
        if self.rounds_played >= self.score_limit:
            await self.on_mode_end(self._match_winner())
            return
        logger.info(
            "Zombie round %d/%d won by team %s; next round in %.1fs",
            self.rounds_played,
            self.score_limit,
            winner,
            self.round_intermission,
        )
        self._round_task = asyncio.ensure_future(self._round_intermission_task())

    def _match_winner(self) -> int | None:
        """The team that won more rounds; None on a tie.

        The match used to go to whoever won the last round, so zombies
        winning 2-1 could still end on "Survivors win".
        """

        zombies = self.server.teams[ZOMBIE_TEAM].score
        survivors = self.server.teams[SURVIVOR_TEAM].score
        if zombies == survivors:
            return None
        return ZOMBIE_TEAM if zombies > survivors else SURVIVOR_TEAM

    async def _round_intermission_task(self) -> None:
        try:
            await asyncio.sleep(self.round_intermission)
            self._round_task = None
            if not self.ended and self.phase is ZombiePhase.INTERMISSION:
                await self._begin_next_round()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Staying in INTERMISSION froze the match: on_tick returns early
            # there, so no clock, outbreak or win check ever ran again.
            logger.exception("Zombie round restart failed")
            self._round_task = None
            await self._recover_failed_round_restart()

    def bot_retire_safe(self, bot) -> bool:
        """Retiring the last survivor (or the only zombie) decides a round."""
        if self.ended or self.phase is not ZombiePhase.ACTIVE:
            return True
        team = int(getattr(bot, "team", -1))
        if team == SURVIVOR_TEAM:
            return len(self._survivors(exclude=bot)) > 0
        if team == ZOMBIE_TEAM:
            return len(self._zombies(exclude=bot)) > 0
        return True

    def bot_retire_rank(self, bot) -> int:
        """Prefer retiring zombies: a departing survivor shifts the round."""
        return 0 if int(getattr(bot, "team", -1)) == ZOMBIE_TEAM else 1

    async def _cancel_round_task(self) -> None:
        task = self._round_task
        self._round_task = None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _begin_next_round(self) -> None:
        """Return every playing body to the survivors on the same map."""
        self._clear_last_survivor_marker()
        self._reset_round_state()
        self._restore_gameplay_music()
        self._round_reset_queue.clear()
        for player in list(self.server.players.values()):
            if self._is_playing(player):
                self._round_reset_queue.append(player)
        if self._round_reset_queue:
            self.phase = ZombiePhase.RESETTING
            self._publish_team_locks()
            return
        self._publish_team_locks()
        await self._arm_countdown_if_ready(time.time())

    async def _drain_round_reset(self, now: float) -> None:
        """Respawn a bounded slice of the next round's survivors.

        A respawn publishes reliable KillAction/CreatePlayer/restock packets;
        a whole roster in one frame stalls ENet and the native scene, so this
        mirrors VIP's per-tick budget.  Entries keep object identity so a
        departed player's reused compact id is never respawned by mistake.
        """
        budget = self.round_respawns_per_tick
        respawn = getattr(self.server, "respawn_player", None)
        while budget > 0 and self._round_reset_queue:
            player = self._round_reset_queue.popleft()
            budget -= 1
            if (
                self.server.players.get(int(player.id)) is not player
                or not self._is_playing(player)
            ):
                continue
            try:
                if bool(getattr(player, "alive", False)):
                    player.die(
                        kill_type=KILL_TEAM_CHANGE
                        if int(player.team) == ZOMBIE_TEAM
                        else KILL_CLASS_CHANGE
                    )
                self._assign_survivor(player)
                if callable(respawn):
                    respawn(player)
            except Exception:
                logger.exception(
                    "Zombie round respawn failed for player %s",
                    getattr(player, "id", "?"),
                )
        if not self._round_reset_queue:
            self.phase = ZombiePhase.WAITING
            await self._arm_countdown_if_ready(now)

    def _reset_round_state(self) -> None:
        """Clear every per-round field; the phase returns to WAITING."""
        self.phase = ZombiePhase.WAITING
        self.infection_deadline = None
        self.patient_zero_ids.clear()
        self._patient_zero_protection_pending.clear()
        self._next_survival_score_at = None
        self._next_last_man_score_at = None
        self._infection_spawn_by_player.clear()
        self.elapsed_time = 0.0
        self._timeout_events_remaining = None
        self._countdown_fired.clear()
        self._countdown_last_remaining = float("inf")

    def _restore_gameplay_music(self) -> None:
        """Swap a finished round's last-minute, last-man or survivor-win
        track back to the bed."""
        changed = getattr(self, "_round_music_changed", False)
        if not self._timeout_music_played and not changed:
            return
        self._timeout_music_played = False
        self._round_music_changed = False
        from server.audio import play_gameplay_music

        play_gameplay_music(self.server)
