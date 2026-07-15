"""Zombie infection mode for the retail MODE_ZOMBIE client scene.

The server owns role assignment and infection; the client already owns the
zombie models, mode HUD, sounds, and class-specific movement.  A round starts
with every connected player as a survivor, arms the retail outbreak timer,
selects patient zero, and permanently converts each later survivor death.
"""

from __future__ import annotations

from enum import Enum, auto
import logging
import random
import time
from typing import TYPE_CHECKING

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.class_selection import ClassSelection, normalize_class_selection
from server.game_constants import KILL_TEAM_CHANGE, TEAM1, TEAM2

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player


logger = logging.getLogger(__name__)

SURVIVOR_TEAM = TEAM1
ZOMBIE_TEAM = TEAM2
_ZOMBIE_CLASSES = frozenset((int(C.CLASS_ZOMBIE),))
_ZOMBIE_PREFABS = tuple(
    str(name)
    for name in C.PREFAB_LISTS.get(int(C.CLASS_PREFABS_ZOMBIE), ())
)
_SURVIVOR_CLASSES = frozenset(
    tuple(int(value) for value in C.DEFAULT_TEAM_CLASSES)
    + (int(C.CLASS_ROCKETEER),)
)


class ZombiePhase(Enum):
    """Authoritative phase of one infection round."""

    WAITING = auto()
    COUNTDOWN = auto()
    ACTIVE = auto()


class ZombieMode(BaseMode):
    """Run survivor preparation, infection, last-man radar, and round wins.

    All methods execute on the 60 Hz gameplay event loop.  Role changes are
    committed before ``RoundLifecycle`` processes respawns in the same tick,
    so a dead survivor's next CreatePlayer can never expose a stale human body
    or weapon loadout to any client.
    """

    name = "Zombie"
    description = "Survive the outbreak, or infect every remaining human."

    def __init__(self, server) -> None:
        super().__init__(server)
        md = mode_data.get("zom")
        overlay = getattr(server.config, "mode_settings", {}).get("zom", {})
        self.score_limit = int(overlay.get(
            "score_limit",
            server.config.game_rules.get("RULE_ZOMBIE_NOOF_ROUNDS"),
        ))
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
        self.last_survivor_id: int | None = None
        self._next_survival_score_at: float | None = None
        self._next_last_man_score_at: float | None = None

    async def on_mode_start(self) -> None:
        """Reset one round and return every connected body to the survivors."""
        self._clear_last_survivor_marker()
        await super().on_mode_start()
        self.phase = ZombiePhase.WAITING
        self.infection_deadline = None
        self.patient_zero_ids.clear()
        self._next_survival_score_at = None
        self._next_last_man_score_at = None
        for team in self.server.teams.values():
            team.reset()
        for player in list(self.server.players.values()):
            if getattr(player, "connection", None) is None:
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
        self._clear_last_survivor_marker()
        await super().deactivate()

    async def on_tick(self, tick: int) -> None:
        """Advance the outbreak timer and bounded periodic survivor scoring."""
        if self.ended:
            return
        now = time.time()
        if self.phase is ZombiePhase.WAITING:
            await self._arm_countdown_if_ready(now)
        if (
            self.phase is ZombiePhase.COUNTDOWN
            and self.infection_deadline is not None
            and now >= self.infection_deadline
        ):
            await self._start_outbreak(now)
        if self.phase is ZombiePhase.ACTIVE:
            # ZOM_ROUND_TIME measures survival after Patient Zero is chosen,
            # not server uptime or time spent waiting for enough players.
            await super().on_tick(tick)
            if self.ended:
                return
            self._award_periodic_survival_score(now)
            await self._check_population()

    async def on_player_join(self, player: Player) -> None:
        """Apply the role chosen by ``prepare_join_team`` and arm a round."""
        if self.phase is ZombiePhase.ACTIVE:
            self._assign_zombie(player)
        else:
            self._assign_survivor(player)
            await self._arm_countdown_if_ready(time.time())

    async def on_player_leave(self, player: Player) -> None:
        """Replace a departed sole zombie so the infection cannot soft-lock."""
        self.patient_zero_ids.discard(int(player.id))
        if self.phase is ZombiePhase.COUNTDOWN:
            if len(self._connected_players()) < self.minimum_players:
                self.phase = ZombiePhase.WAITING
                self.infection_deadline = None
            return
        if self.phase is not ZombiePhase.ACTIVE:
            return
        zombies = self._zombies()
        survivors = self._survivors()
        if not zombies and survivors:
            replacement = random.choice(survivors)
            await self._infect(replacement, patient_zero=True)
            await self.broadcast_message(
                f"{replacement.name} replaces the departed Patient Zero!"
            )
        await self._check_population()

    async def on_player_death(
        self,
        player: Player,
        killer: Player | None,
        kill_type: int,
    ) -> None:
        """Permanently convert every survivor death after the outbreak."""
        if self.phase is not ZombiePhase.ACTIVE or int(player.team) != SURVIVOR_TEAM:
            return
        if killer is not None and killer is not player and int(killer.team) == ZOMBIE_TEAM:
            self._award_player(killer, int(CG.ZOM_SCORE_KILL_SURVIVOR))
        await self._infect(player, patient_zero=False)
        await self._check_population()

    async def on_player_kill(
        self,
        killer: Player,
        victim: Player,
        kill_type: int,
    ) -> None:
        """Award the recovered bonus for a last survivor killing a zombie."""
        if (
            self.phase is ZombiePhase.ACTIVE
            and int(killer.team) == SURVIVOR_TEAM
            and int(victim.team) == ZOMBIE_TEAM
            and self.last_survivor_id == int(killer.id)
        ):
            self._award_player(killer, int(CG.ZOM_SCORE_LASTMAN_ZOMBIEKILL))

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
            return selection
        return self._zombie_selection(int(C.CLASS_ZOMBIE))

    def allows_class_selection(self, player: Player, selection: ClassSelection) -> bool:
        """Reject cross-role class packets while allowing legal loadout edits."""
        allowed = (
            _ZOMBIE_CLASSES
            if int(getattr(player, "team", -1)) == ZOMBIE_TEAM
            else _SURVIVOR_CLASSES
        )
        return int(selection.class_id) in allowed

    def allows_team_change(self, player: Player, new_team: int) -> bool:
        """Roles are infection state and cannot be escaped through team UI."""
        return False

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
        """Use the map's validated team regions for survivor/zombie separation."""
        return tuple(
            float(value)
            for value in self.server.world_manager.get_spawn_point(player.team)
        )

    def configure_state_data(self, packet) -> None:
        """Publish asymmetric native class menus and phase-aware team locks."""
        packet.team1_name = "Survivors"
        packet.team2_name = "Zombies"
        packet.team1_classes = sorted(_SURVIVOR_CLASSES)
        packet.team2_classes = [int(C.CLASS_ZOMBIE)]
        packet.team1_locked = self.phase is ZombiePhase.ACTIVE
        packet.team2_locked = self.phase is not ZombiePhase.ACTIVE
        packet.lock_team_swap = True
        packet.team1_locked_class = False
        # One stable class bypasses the ordinary class picker.  Fast/Jump
        # Zombie lack picker icons in this client and are intentionally hidden.
        packet.team2_locked_class = True
        packet.team1_show_score = False
        packet.team2_show_score = False
        packet.team1_show_max_score = False
        packet.team2_show_max_score = False

    def configure_initial_info(self, packet) -> None:
        """Force role-safe combat and leave minimap exposure event-driven."""
        packet.friendly_fire = 0
        packet.exposed_teams_always_on_minimap = 0
        class_speed = float(
            self.server.config.game_rules.get("RULE_CLASS_SPEED")
        )
        packet.movement_speed_multipliers = [
            float(value) * class_speed
            for value in packet.movement_speed_multipliers
        ]

    def reveal_to(self, connection) -> None:
        """Replay the current last-survivor heart/marker to a late client."""
        if self.last_survivor_id is None:
            return
        player = self.server.players.get(self.last_survivor_id)
        if player is not None:
            self._set_high_minimap_visibility(player, True, connection=connection)

    async def _arm_countdown_if_ready(self, now: float) -> None:
        if self.phase is not ZombiePhase.WAITING:
            return
        if len(self._connected_players()) < self.minimum_players:
            return
        self.phase = ZombiePhase.COUNTDOWN
        self.infection_deadline = now + max(0.0, self.infection_delay)
        from server.audio import SND_ZOMBIE_TIMER, play_sound

        play_sound(self.server, SND_ZOMBIE_TIMER, volume=1.0)
        await self.broadcast_message(
            f"Zombie outbreak in {int(round(max(0.0, self.infection_delay)))} seconds!"
        )

    async def _start_outbreak(self, now: float) -> None:
        candidates = self._living_survivors()
        if len(candidates) < 2:
            # Never consume the final human: wait for a viable infection round.
            self.phase = ZombiePhase.WAITING
            self.infection_deadline = None
            return
        count = min(self.first_infected_count, len(candidates) - 1)
        selected = random.sample(candidates, count)
        self.phase = ZombiePhase.ACTIVE
        self.infection_deadline = None
        # BaseMode's timer starts when the mode object is activated. Infection
        # can wait indefinitely for players, so restart it at the native
        # outbreak boundary or a quiet server eventually ends on first join.
        self.start_time = now
        self.elapsed_time = 0.0
        self._timeout_music_played = False
        self._next_survival_score_at = now + float(CG.ZOM_SCORE_SURVIVE_INTERVAL)
        self._next_last_man_score_at = now + float(CG.ZOM_SCORE_LASTMAN_INTERVAL)
        for player in selected:
            await self._infect(player, patient_zero=True)
            await self.broadcast_message(f"{player.name} is Patient Zero!")
        await self._refresh_last_survivor_marker()

    async def _infect(self, player: Player, *, patient_zero: bool) -> None:
        if int(getattr(player, "team", -1)) == ZOMBIE_TEAM:
            return
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
        from server.audio import SND_ZOMBIE_BECOME, play_sound

        play_sound(self.server, SND_ZOMBIE_BECOME, volume=1.0)

    def _assign_survivor(self, player: Player) -> None:
        self._move_to_team(player, SURVIVOR_TEAM)
        class_id = int(getattr(player, "class_id", C.CLASS_SOLDIER))
        if class_id not in _SURVIVOR_CLASSES:
            class_id = int(C.CLASS_SOLDIER)
        selection = normalize_class_selection(
            class_id,
            getattr(player, "loadout", ()) or (),
            getattr(player, "prefabs", ()) or (),
            getattr(player, "ugc_tools", ()) or (),
        )
        player.apply_class_selection(selection)

    def _assign_zombie(self, player: Player) -> None:
        self._move_to_team(player, ZOMBIE_TEAM)
        player.apply_class_selection(
            self._zombie_selection(self._zombie_class_for(player))
        )

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

    def _connected_players(self) -> list[Player]:
        return [
            player for player in self.server.players.values()
            if getattr(player, "connection", None) is not None
        ]

    def _survivors(self) -> list[Player]:
        return [
            player for player in self._connected_players()
            if int(player.team) == SURVIVOR_TEAM
        ]

    def _living_survivors(self) -> list[Player]:
        return [
            player for player in self._survivors()
            if bool(getattr(player, "alive", False))
            and bool(getattr(player, "spawned", False))
        ]

    def _zombies(self) -> list[Player]:
        return [
            player for player in self._connected_players()
            if int(player.team) == ZOMBIE_TEAM
        ]

    async def _check_population(self) -> None:
        survivors = self._survivors()
        if not survivors and self._connected_players():
            await self._finish_round(ZOMBIE_TEAM, "The zombie horde wins!")
            return
        await self._refresh_last_survivor_marker()

    async def _refresh_last_survivor_marker(self) -> None:
        living = self._living_survivors()
        new_id = int(living[0].id) if len(living) == 1 else None
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
        await self.broadcast_message(f"{player.name} is the last survivor!")

    def _clear_last_survivor_marker(self) -> None:
        player_id = self.last_survivor_id
        self.last_survivor_id = None
        if player_id is None:
            return
        player = self.server.players.get(player_id)
        if player is not None:
            self._set_high_minimap_visibility(player, False)

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
        living = self._living_survivors()
        if not living:
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
                self._award_player(player, points)
            self._next_survival_score_at += (
                intervals * float(CG.ZOM_SCORE_SURVIVE_INTERVAL)
            )
        if (
            len(living) == 1
            and self._next_last_man_score_at is not None
            and now >= self._next_last_man_score_at
        ):
            intervals = 1 + int(
                (now - self._next_last_man_score_at)
                // float(CG.ZOM_SCORE_LASTMAN_INTERVAL)
            )
            self._award_player(
                living[0], intervals * int(CG.ZOM_SCORE_LASTMAN)
            )
            self._next_last_man_score_at += (
                intervals * float(CG.ZOM_SCORE_LASTMAN_INTERVAL)
            )

    def _award_player(self, player: Player, points: int) -> None:
        if points <= 0:
            return
        player.score = int(getattr(player, "score", 0)) + int(points)
        from server.scoreboard import send_player_score

        send_player_score(self.server, player)

    async def _end_by_time(self) -> None:
        """Survivors win the native round if any living human lasts 600s."""
        if self.ended:
            return
        winner = SURVIVOR_TEAM if self._living_survivors() else ZOMBIE_TEAM
        message = (
            "The survivors endured the outbreak!"
            if winner == SURVIVOR_TEAM
            else "The zombie horde wins!"
        )
        await self._finish_round(winner, message)

    async def _finish_round(self, winner: int, message: str) -> None:
        if self.ended:
            return
        from server.scoreboard import send_team_score

        team = self.server.teams[winner]
        team.score = max(1, int(team.score))
        send_team_score(self.server, team)
        await self.broadcast_message(message)
        await self.on_mode_end(winner)
