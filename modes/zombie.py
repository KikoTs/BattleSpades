# BattleSpades Zombie mode: instant, grave-free conversion / timed zombie deaths.
# Local full-file build based on KikoTs/BattleSpades modes/zombie.py.
# Ordinary zombie deaths use [modes.zom] zombie_respawn_time (10 seconds).
# Infection uses KillAction/CreatePlayer to switch the native client model
# immediately, without creating a grave or applying the zombie death timer.

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

ZOMBIE_TEAM = TEAM1
SURVIVOR_TEAM = TEAM2
_ZOMBIE_CLASSES = frozenset((int(C.CLASS_ZOMBIE),))
_ZOMBIE_PREFABS = tuple(
    str(name)
    for name in C.PREFAB_LISTS.get(int(C.CLASS_PREFABS_ZOMBIE), ())
)
_SURVIVOR_CLASS_ORDER = BATTLESPADES_TEAM_CLASSES
_SURVIVOR_CLASSES = frozenset(_SURVIVOR_CLASS_ORDER)
_PLAYABLE_TEAMS = (ZOMBIE_TEAM, SURVIVOR_TEAM)
_TRANSITION_KILLS = frozenset((
    int(C.KILL.FORCED_TEAM_CHANGE_KILL),
    int(C.KILL.TEAM_CHANGE_KILL),
    int(C.KILL.CLASS_CHANGE_KILL),
))
_ZOMBIE_TEAM_NAME_ID = "ZOMBIE_TEAM"
_SURVIVOR_TEAM_NAME_ID = "SURVIVOR_TEAM"
_OUTBREAK_RETRY_SECONDS = 1.0

class ZombiePhase(Enum):

    WAITING = auto()
    COUNTDOWN = auto()
    ACTIVE = auto()
    INTERMISSION = auto()
    RESETTING = auto()

class ZombieMode(BaseMode):
    survivor_class_order = _SURVIVOR_CLASS_ORDER
    survivor_classes = _SURVIVOR_CLASSES
    normalize_survivor_selection = staticmethod(normalize_class_selection)

    generic_scoring_enabled = False

    name = "Zombie"
    description = "Survive the outbreak, or infect every remaining human."

    def __init__(self, server) -> None:
        super().__init__(server)
        md = mode_data.get("zom")
        overlay = getattr(server.config, "mode_settings", {}).get("zom", {})
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
        # Keep [modes.zom].zombie_respawn_time authoritative. The old
        # retail constant was 0, but a killed zombie must wait 10 seconds by
        # default; initial infection uses a separate immediate path.
        self.zombie_respawn_time = max(0.0, float(overlay.get(
            "zombie_respawn_time", 10.0
        )))
        # The normal Zombie death creates a grave. Only the instantaneous
        # infection transition uses the existing classic-corpse cleanup path,
        # avoiding any grave entity without changing global corpse rules.
        self.death_representation = "grave"

        self.phase = ZombiePhase.WAITING
        self.infection_deadline: float | None = None
        self.patient_zero_ids: set[int] = set()
        self._patient_zero_protection_pending: dict[int, Player] = {}
        self.last_survivor_id: int | None = None
        self._next_survival_score_at: float | None = None
        self._next_last_man_score_at: float | None = None
        self._infection_spawn_by_player: dict[
            int, tuple[float, float, float]
        ] = {}
        self._survivor_selection_by_player: dict[int, ClassSelection] = {}
        self.rounds_played = 0
        self._pick_timer_sounded = False
        self._round_music_changed = False
        self._round_task: asyncio.Task | None = None
        self._round_reset_queue: deque[Player] = deque()
        # Client team-visibility flags can persist across same-map rounds.
        # Keep the asymmetric Zombie map permissions current for all peers.
        self._next_map_visibility_refresh_at = 0.0

    async def on_mode_start(self) -> None:
        await self._cancel_round_task()
        self._round_reset_queue.clear()
        self._clear_last_survivor_marker()
        await super().on_mode_start()
        self.rounds_played = 0
        self._survivor_selection_by_player.clear()
        self._reset_round_state()
        self._publish_team_locks()
        self._set_team_map_visibility()
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
        self._clear_last_survivor_marker()
        await super().on_mode_end(winner)

    async def deactivate(self) -> None:
        await self._cancel_round_task()
        self._round_reset_queue.clear()
        self._clear_last_survivor_marker()
        self._infection_spawn_by_player.clear()
        self._survivor_selection_by_player.clear()
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        if self.ended:
            return
        now = time.time()
        if now >= self._next_map_visibility_refresh_at:
            # Packet 83 explicitly resets the survivor team's enemy reveal
            # bit. StateData alone may be stale on an in-place round restart.
            self._set_team_map_visibility()
            self._next_map_visibility_refresh_at = now + 3.0
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
            if self._is_final_round():
                await super().on_tick(tick)
            else:
                await self._advance_round_clock(now)
            if self.ended or self.phase is not ZombiePhase.ACTIVE:
                return
            self._award_periodic_survival_score(now)
            await self._check_population()

    async def _advance_round_clock(self, now: float) -> None:
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
        if not self._is_playing(player):
            return
        target = ZOMBIE_TEAM if self.phase is ZombiePhase.ACTIVE else SURVIVOR_TEAM
        if self.phase is ZombiePhase.ACTIVE and int(player.team) == SURVIVOR_TEAM:
            target = SURVIVOR_TEAM
        if int(player.team) != target and bool(getattr(player, "alive", False)):
            player.die(kill_type=KILL_TEAM_CHANGE)
        if target == ZOMBIE_TEAM:
            self._assign_zombie(player)
        else:
            self._assign_survivor(player)
            await self._arm_countdown_if_ready(time.time())
        # Player join / team changes can race the initial StateData snapshot.
        conn = getattr(player, "connection", None)
        if conn is not None and bool(getattr(conn, "in_game", False)):
            self._set_team_map_visibility(connection=conn)

    async def on_player_team_change(
        self,
        player: Player,
        old_team: int,
        new_team: int,
    ) -> None:
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
        player_id = int(player.id)
        self.patient_zero_ids.discard(player_id)
        self._patient_zero_protection_pending.pop(player_id, None)
        self._infection_spawn_by_player.pop(player_id, None)
        self._survivor_selection_by_player.pop(player_id, None)
        if self.last_survivor_id == player_id:
            self.last_survivor_id = None
        if self.ended:
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
        await super().on_player_spawn(player)
        if int(getattr(player, "team", -1)) == ZOMBIE_TEAM:
            # Clear any marker inherited from the player's previous life.
            self._set_high_minimap_visibility(player, False)
        pending = self._patient_zero_protection_pending.pop(
            int(getattr(player, "id", -1)), None
        )
        if pending is not player or int(getattr(player, "team", -1)) != ZOMBIE_TEAM:
            return
        self.limit_spawn_protection(player, float(C.FIRST_ZOMBIE_SPAWN_PROTECTION_TIME))

    @staticmethod
    def limit_spawn_protection(player, window: float) -> None:
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
        if self.ended or int(kill_type) in _TRANSITION_KILLS:
            return
        if self.phase is not ZombiePhase.ACTIVE or int(player.team) != SURVIVOR_TEAM:
            return
        if self.server.players.get(int(player.id)) is not player:
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
        if self.phase is ZombiePhase.ACTIVE:
            return ZOMBIE_TEAM
        return SURVIVOR_TEAM

    def prepare_join_selection(
        self,
        team: int,
        selection: ClassSelection,
    ) -> ClassSelection:
        if int(team) == ZOMBIE_TEAM:
            return self._zombie_selection()
        class_id = int(selection.class_id)
        if class_id not in self.survivor_classes:
            class_id = int(C.CLASS_SOLDIER)
        return self.normalize_survivor_selection(
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

        if int(team) != ZOMBIE_TEAM:
            if int(selection.class_id) in self.survivor_classes:
                return selection
            return self.normalize_survivor_selection(int(C.CLASS_SOLDIER))
        return self._zombie_selection(int(C.CLASS_ZOMBIE))

    def allows_class_selection(self, player: Player, selection: ClassSelection) -> bool:
        team = int(getattr(player, "team", -1))
        allowed = _ZOMBIE_CLASSES if team == ZOMBIE_TEAM else self.survivor_classes
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
        return int(
            C.SURVIVOR_WIN_MESSAGE if winner == SURVIVOR_TEAM else C.ZOMBIE_WIN_MESSAGE
        )

    def _publish_team_locks(self) -> None:
        from server.scoreboard import send_lock_team, send_team_lock_class

        active = self.phase is ZombiePhase.ACTIVE
        send_lock_team(self.server, ZOMBIE_TEAM, not active)
        send_lock_team(self.server, SURVIVOR_TEAM, active)
        send_team_lock_class(self.server, SURVIVOR_TEAM, active)

    def allows_team_change(self, player: Player, new_team: int) -> bool:
        if int(getattr(player, "team", -1)) != TEAM_SPECTATOR:
            return False
        return int(new_team) == self.prepare_join_team(int(new_team))

    def can_player_respawn(self, player: Player) -> bool:
        return not self.ended

    def respawn_time_for(self, player: Player) -> float:
        """Show 0 s for infection and 10 s for an already infected zombie."""
        if self.phase is ZombiePhase.ACTIVE:
            if int(getattr(player, "team", -1)) == ZOMBIE_TEAM:
                return self.zombie_respawn_time
            # A killed survivor is converted immediately by on_player_death.
            return 0.0
        return float(self.server.config.respawn_time)

    def death_kill_type_for(self, player: Player, killer, kill_type: int) -> int:
        """Avoid graves for humans converting to zombies only.

        Player.die calls this synchronous hook before corpse_lifecycle. A
        conversion must use KillAction so the stock client replaces its
        character model, but classic-corpse handling skips entity-11 graves.
        The instant follow-up CreatePlayer clears the temporary corpse.
        """
        self.death_representation = (
            "classic_corpse"
            if self.phase is ZombiePhase.ACTIVE
            and int(getattr(player, "team", -1)) == SURVIVOR_TEAM
            else "grave"
        )
        return int(kill_type)

    def modify_incoming_damage(
        self,
        player: Player,
        amount: int,
        source: Player | None,
        kill_type: int,
    ) -> int:
        if source is not None and source is not player and int(source.team) == int(player.team):
            return 0
        return int(amount)

    def get_spawn_point(self, player: Player) -> tuple[float, float, float]:
        infection_spawn = self._infection_spawn_by_player.pop(
            int(player.id), None
        )
        if infection_spawn is not None:
            return infection_spawn
        return tuple(float(value) for value in super().get_spawn_point(player))

    def configure_state_data(self, packet) -> None:
        packet.team1_name = _ZOMBIE_TEAM_NAME_ID
        packet.team2_name = _SURVIVOR_TEAM_NAME_ID
        packet.team1_classes = [int(C.CLASS_ZOMBIE)]
        packet.team2_classes = list(self.survivor_class_order)
        packet.team1_locked = self.phase is not ZombiePhase.ACTIVE
        packet.team2_locked = self.phase is ZombiePhase.ACTIVE
        packet.team1_can_see_team2 = True
        packet.team2_can_see_team1 = False
        packet.lock_team_swap = True
        packet.team1_locked_class = True
        packet.team2_locked_class = self.phase is ZombiePhase.ACTIVE
        packet.team1_show_score = False
        packet.team2_show_score = False
        packet.team1_show_max_score = False
        packet.team2_show_max_score = False
        packet.team_headcount_type = int(C.TEAM_PLAYERS_COUNT_VALUE)

    def configure_initial_info(self, packet) -> None:
        packet.friendly_fire = 0
        # Never pin enemy map icons at the borders (even on the full map).
        packet.exposed_teams_always_on_minimap = 0

    def _set_team_map_visibility(self, *, connection=None) -> None:
        """Zombie viewers see humans; survivor viewers never reveal zombies.

        StateData initializes these flags, but the stock client also accepts
        TeamMapVisibility (83) at runtime. Explicitly applying both flags is
        important after transitions and in-place Zombie round restarts.
        This does not control the client-side Scout radar station mechanic.
        """
        from shared.packet import TeamMapVisibility

        for team, visible in ((ZOMBIE_TEAM, True), (SURVIVOR_TEAM, False)):
            packet = TeamMapVisibility()
            packet.team_id = int(team)
            packet.visible = int(visible)
            data = bytes(packet.generate())
            if connection is None:
                self.server.broadcast(data, reliable=True)
            else:
                connection.send(data, reliable=True)

    def start_cue_for(self, player):
        team = int(getattr(player, "team", -1))
        if team == SURVIVOR_TEAM:
            return "ZOMBIE_START_SURVIVOR"
        if team == ZOMBIE_TEAM:
            return "ZOMBIE_START_ZOMBIE"
        return None

    def reveal_to(self, connection) -> None:
        super().reveal_to(connection)
        self._set_team_map_visibility(connection=connection)
        # Late joiners must not inherit an old marked-zombie state either.
        for zombie in self._zombies():
            self._set_high_minimap_visibility(zombie, False, connection=connection)
        self.send_start_cue_to(connection)
        if self.last_survivor_id is None:
            return
        player = self.server.players.get(self.last_survivor_id)
        if player is not None:
            self._set_high_minimap_visibility(player, True, connection=connection)

    def countdown_seconds_remaining(self, now: float) -> float:

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
        self._sound_pick_timer_if_due(now)
        send_round_timer(
            self.server,
            self.countdown_seconds_remaining(now),
        )
        self.broadcast_start_cue()
        await self.broadcast_localised_message(
            "ZOMBIE_VIRUS_RELEASED", override_previous=False
        )

    def _sound_pick_timer_if_due(self, now: float) -> None:
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
            if len(self._connected_players()) < self.minimum_players:
                self.phase = ZombiePhase.WAITING
                self.infection_deadline = None
            else:
                self.infection_deadline = now + _OUTBREAK_RETRY_SECONDS
            return
        count = min(self.first_infected_count, len(candidates) - 1)
        settled = [
            player for player in candidates
            if bool(getattr(getattr(player, "connection", None), "in_game", True))
        ]
        pool = settled if len(settled) >= count else candidates
        selected = random.sample(pool, count)
        self.phase = ZombiePhase.ACTIVE
        self.infection_deadline = None
        self.start_time = now
        self.elapsed_time = 0.0
        self._timeout_music_played = False
        self._timeout_events_remaining = None
        self._countdown_fired.clear()
        self._countdown_last_remaining = float("inf")
        self._next_survival_score_at = now + float(CG.ZOM_SCORE_SURVIVE_INTERVAL)
        self._next_last_man_score_at = now + float(CG.ZOM_SCORE_LASTMAN_INTERVAL)
        self._publish_team_locks()
        self._set_team_map_visibility()
        for player in selected:
            await self._infect(player, patient_zero=True)
        from server import achievements

        achievements.zombie_round_started(
            self.server, selected, ZOMBIE_TEAM, SURVIVOR_TEAM
        )
        self.announce_localised_to_team(
            SURVIVOR_TEAM, "ZOMBIE_INFECTION_DETECTED", override_previous=True
        )
        from server.audio import SND_ZOMBIE_BECOME, play_sound

        play_sound(self.server, SND_ZOMBIE_BECOME, volume=1.0)
        from server.scoreboard import send_round_timer

        send_round_timer(self.server, self.time_limit)
        await self._refresh_last_survivor_marker()

    async def _infect(self, player: Player, *, patient_zero: bool) -> None:
        if int(getattr(player, "team", -1)) == ZOMBIE_TEAM:
            return
        class_id = int(getattr(player, "class_id", C.CLASS_SOLDIER))
        if class_id in self.survivor_classes:
            self._survivor_selection_by_player[int(player.id)] = (
                self.normalize_survivor_selection(
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
        # Stock clients cannot change a live Character's class/team purely
        # from the server's Player fields. Retire the human Character with the
        # transition-only kill (no grave) and create the zombie straight away.
        # The same method also handles a human already killed in combat: that
        # earlier death was marked classic_corpse by death_kill_type_for.
        if getattr(player, "alive", False):
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
        # Restore normal grave presentation before any other player can die.
        self.death_representation = "grave"
        # An infected player's previous visibility flag is not a zombie trait.
        # Ensure a stale VIP/escape marker cannot reveal the new zombie.
        self._set_high_minimap_visibility(player, False)
        self._set_team_map_visibility()
        # Do not wait for the generic respawn scheduler (whose zombie timer
        # correctly remains 10s). A newly infected player must transform now.
        if not bool(getattr(player, "alive", False)):
            self.server.respawn_player(player)
        self.announce_localised_to_player(
            player, "YOU_HAVE_BEEN_INFECTED", override_previous=True
        )

    def _assign_survivor(self, player: Player) -> None:
        self._move_to_team(player, SURVIVOR_TEAM)
        selection = self._survivor_selection_by_player.pop(int(player.id), None)
        if selection is None:
            class_id = int(getattr(player, "class_id", C.CLASS_SOLDIER))
            if class_id in self.survivor_classes:
                selection = self.normalize_survivor_selection(
                    class_id,
                    getattr(player, "loadout", ()) or (),
                    getattr(player, "prefabs", ()) or (),
                    getattr(player, "ugc_tools", ()) or (),
                )
            else:
                selection = self.normalize_survivor_selection(int(C.CLASS_SOLDIER))
        player.apply_class_selection(selection)
        pending = getattr(player, "pending_selection", None)
        if (
            pending is not None
            and int(getattr(pending, "class_id", -1)) not in self.survivor_classes
        ):
            self._clear_pending_selection(player)

    def _assign_zombie(self, player: Player) -> None:
        self._move_to_team(player, ZOMBIE_TEAM)
        player.apply_class_selection(
            self._zombie_selection(self._zombie_class_for(player))
        )
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

        return normalize_class_selection(
            int(class_id),
            prefabs=_ZOMBIE_PREFABS,
        )

    @staticmethod
    def _zombie_class_for(player: Player) -> int:

        return int(C.CLASS_ZOMBIE)

    def _move_to_team(self, player: Player, team: int) -> None:
        old_team = int(getattr(player, "team", -1))
        if old_team in self.server.teams:
            self.server.teams[old_team].remove_player(player)
        player.team = int(team)
        self.server.teams[int(team)].add_player(player)

    @staticmethod
    def _is_playing(player: Player) -> bool:
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
            if bool(getattr(player, "alive", False)):
                player.die(kill_type=KILL_TEAM_CHANGE)
            self._assign_survivor(player)
        return True

    async def _refresh_last_survivor_marker(
        self, *, exclude: Player | None = None
    ) -> None:
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
        await self.broadcast_localised_message("LAST_MAN_STANDING")
        self.announce_localised_to_player(
            player, "ZOMBIE_LAST_MAN", override_previous=True
        )
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
        return (
            self.last_survivor_id is not None
            and int(getattr(player, "id", -1)) == self.last_survivor_id
        )

    def escape_watch_objective_player(self, player) -> bool:
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
            return
        player.score = int(getattr(player, "score", 0)) + int(points)
        from server.scoreboard import send_player_score

        send_player_score(self.server, player, reason=reason)

    async def _end_by_time(self) -> None:
        if self.ended:
            return
        winner = SURVIVOR_TEAM if self._survivors() else ZOMBIE_TEAM
        await self._finish_round(
            winner, "SURVIVOR_WIN" if winner == SURVIVOR_TEAM else "ZOMBIE_WIN"
        )

    def _is_final_round(self) -> bool:
        return self.rounds_played + 1 >= self.score_limit

    async def _finish_round(self, winner: int, string_id: str) -> None:
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
        self._publish_team_locks()
        self._clear_last_survivor_marker()
        self.rounds_played += 1
        team = self.server.teams[winner]
        team.add_score(1)
        send_team_score(self.server, team)
        await self.broadcast_localised_message(string_id)
        if winner == SURVIVOR_TEAM and self.rounds_played < self.score_limit:
            from server.audio import play_ending_music

            play_ending_music(self.server)
            self._round_music_changed = True
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
            logger.exception("Zombie round restart failed")
            self._round_task = None
            await self._recover_failed_round_restart()

    def bot_retire_safe(self, bot) -> bool:
        if self.ended or self.phase is not ZombiePhase.ACTIVE:
            return True
        team = int(getattr(bot, "team", -1))
        if team == SURVIVOR_TEAM:
            return len(self._survivors(exclude=bot)) > 0
        if team == ZOMBIE_TEAM:
            return len(self._zombies(exclude=bot)) > 0
        return True

    def bot_retire_rank(self, bot) -> int:
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
        self._clear_last_survivor_marker()
        self._reset_round_state()
        self._set_team_map_visibility()
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
        changed = getattr(self, "_round_music_changed", False)
        if not self._timeout_music_played and not changed:
            return
        self._timeout_music_played = False
        self._round_music_changed = False
        from server.audio import play_gameplay_music

        play_gameplay_music(self.server)
