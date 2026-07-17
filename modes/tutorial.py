"""Reconstructed retail tutorial controller for the authored Training map.

The stock client still contains the tutorial HUD, localized lesson strings,
music, completion sound, countdown, and the original twelve-lane VXL.  What it
does not contain is the server-side lesson state machine.  This module restores
that missing authority while deliberately remaining absent from ``modes``'
public registry; only :mod:`server.tutorial_launcher` may register it.

All callbacks run on the authoritative gameplay thread.  Terrain listeners do
constant-time enqueue work, and at most ``MUTATION_DRAIN_BUDGET`` changes are
interpreted in one tick.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import IntEnum
import math
import time
from typing import TYPE_CHECKING

import shared.constants as C
from shared.packet import (
    BlockBuildColored,
    DisplayCountdown,
    HelpMessage,
    SetClassLoadout,
)

from server.announcements import build_localised_overlay
from server.audio import (
    SND_TUTORIAL_COMPLETE,
    play_music_to,
    play_sound_to,
)
from server.class_selection import ClassSelection
from server.game_constants import TEAM1

from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player


class TutorialStage(IntEnum):
    """Ordered lesson phases recovered from the retail string table."""

    INTRO = 0
    BASIC_CONTROLS = 1
    JUMP = 2
    CROUCH = 3
    SHOOTING = 4
    CLIMB = 5
    COMPLETE = 6


@dataclass(slots=True)
class TutorialSession:
    """One concrete player life-cycle's progress through an authored lane."""

    player: object
    lane_index: int
    stage: TutorialStage = TutorialStage.INTRO
    stage_started: float = 0.0
    revealed: bool = False
    revealed_at: float = 0.0
    connection: object | None = None
    music_replayed: bool = False
    saw_jump: bool = False
    saw_crouch: bool = False
    minimum_local_x: float = 1_000.0
    destroyed_targets: set[int] = field(default_factory=set)
    built_cells: set[tuple[int, int, int]] = field(default_factory=set)
    completion_deadline: float | None = None
    last_countdown_value: int | None = None
    disconnect_sent: bool = False


class TutorialMode(BaseMode):
    """Drive the original Training.vxl obstacle, target, and building lessons.

    Ownership:
        One mode instance owns lane allocation, per-player progress, and target
        reset/progress state for the lifetime of the dedicated tutorial server.
    Failure behavior:
        Construction without the private launcher marker is rejected.  A map
        missing any of the authored target groups also fails startup instead
        of silently running an impossible lesson.
    """

    name = "Tutorial"
    description = "Learn the basics of playing Ace of Spades"
    mode_code = "tut"
    score_limit = 0
    time_limit = 0

    LANE_X_ORIGINS = (0, 148, 298)
    LANE_Y_ORIGINS = (0, 126, 246, 372)
    # Spell this out because comprehensions inside a class body execute in a
    # nested scope and cannot portably resolve sibling class attributes.
    LANE_ORIGINS = (
        (0, 0), (148, 0), (298, 0),
        (0, 126), (148, 126), (298, 126),
        (0, 246), (148, 246), (298, 246),
        (0, 372), (148, 372), (298, 372),
    )
    LANE_WIDTH = 146
    LANE_HEIGHT = 120

    # Interior position recovered from the authored corridor.  Generic
    # get_height() sees its roof and would incorrectly place players outside.
    SPAWN_LOCAL = (140.5, 76.5, 230.75)
    SPAWN_ORIENTATION = (-1.0, 0.0, 0.0)

    # Five red bullseyes are repeated byte-for-byte in every lane.
    TARGET_RGB = 0xE43334
    TARGET_CENTERS = (
        (39, 62, 221),
        (39, 91, 221),
        (43, 60, 229),
        (43, 93, 229),
        (53, 78, 229),
    )
    TARGET_SCAN_RADIUS = 4

    HELP_BY_STAGE = {
        TutorialStage.INTRO: ("TUTORIAL_INTRO",),
        TutorialStage.BASIC_CONTROLS: (
            "TUTORIAL_BASIC_CONTROLS_1",
            "TUTORIAL_BASIC_CONTROLS_2",
            "TUTORIAL_BASIC_CONTROLS_3",
        ),
        TutorialStage.JUMP: ("TUTORIAL_JUMP_1", "TUTORIAL_JUMP_2"),
        TutorialStage.CROUCH: ("TUTORIAL_CROUCH_1", "TUTORIAL_CROUCH_2"),
        TutorialStage.SHOOTING: (
            "TUTORIAL_SHOOTING_1",
            "TUTORIAL_SHOOTING_2",
            "TUTORIAL_SHOOTING_3",
        ),
        TutorialStage.CLIMB: (
            "TUTORIAL_CLIMB1",
            "TUTORIAL_CLIMB2",
            "TUTORIAL_CLIMB3",
        ),
        TutorialStage.COMPLETE: (
            "TUTORIAL_COMPLETE_1",
            "TUTORIAL_COMPLETE_2",
            "TUTORIAL_COMPLETE_3",
        ),
    }

    INTRO_SECONDS = 3.0
    COMPLETION_SECONDS = 10.0
    HELP_TRANSITION_DELAY = 0.35
    MUTATION_QUEUE_LIMIT = 4096
    MUTATION_DRAIN_BUDGET = 512

    # The retail help copy describes two inventory unlocks, not one static
    # Soldier loadout: the pistol appears at the gallery, then the block tool
    # and spade appear at the climb.  SetClassLoadout(13) is bidirectional and
    # its native GameScene handler immediately selects the final list item.
    MOVEMENT_LOADOUT: tuple[int, ...] = ()
    SHOOTING_LOADOUT = (int(C.PISTOL_TOOL),)
    CLIMB_LOADOUT = (
        int(C.PISTOL_TOOL),
        int(C.BLOCK_TOOL),
        int(C.SPADE_TOOL),
    )
    TUTORIAL_LOADOUT = CLIMB_LOADOUT
    MOVEMENT_SELECTION = ClassSelection(
        class_id=int(C.CLASS_SOLDIER),
        loadout=MOVEMENT_LOADOUT,
    )
    TUTORIAL_SELECTION = ClassSelection(
        class_id=int(C.CLASS_SOLDIER),
        loadout=CLIMB_LOADOUT,
    )

    def __init__(self, server) -> None:
        if not bool(getattr(server.config, "tutorial_runtime", False)):
            raise RuntimeError(
                "TutorialMode is isolated; launch it with run_tutorial.py"
            )
        super().__init__(server)
        self._sessions: dict[int, TutorialSession] = {}
        self._lane_occupants: dict[int, int] = {}
        self._mutation_queue: deque[tuple[int, int, int, bool]] = deque()
        self._mutation_listener_token: int | None = None
        self._target_voxels: list[
            list[dict[tuple[int, int, int], int]]
        ] = []
        self._target_lookup: dict[tuple[int, int, int], tuple[int, int]] = {}
        self._pending_restore_packets: dict[
            int, list[tuple[tuple[int, int, int], int]]
        ] = {}

    async def on_mode_start(self) -> None:
        """Initialize target geometry without normal pickups or match music."""

        self.started = True
        self.ended = False
        self.winner = None
        self.start_time = time.time()
        self.elapsed_time = 0.0
        self._capture_authored_targets()
        subscribe = getattr(self.server.world_manager, "subscribe_mutations", None)
        if callable(subscribe):
            self._mutation_listener_token = subscribe(self._on_world_mutation)

    async def on_mode_end(self, winner=None) -> None:
        """Stop the isolated lesson without invoking a normal round rollover."""

        await self.deactivate()

    async def deactivate(self) -> None:
        """Detach the canonical terrain listener and release all lane state."""

        token = self._mutation_listener_token
        if token is not None:
            unsubscribe = getattr(
                self.server.world_manager, "unsubscribe_mutations", None
            )
            if callable(unsubscribe):
                unsubscribe(token)
        self._mutation_listener_token = None
        self._sessions.clear()
        self._lane_occupants.clear()
        self._mutation_queue.clear()
        self._pending_restore_packets.clear()
        self.started = False
        self.ended = True

    def prepare_join_team(self, requested_team: int) -> int:
        """Put every tutorial participant in the single playable team."""

        return TEAM1

    def prepare_join_selection(
        self,
        team: int,
        selection: ClassSelection,
    ) -> ClassSelection:
        """Start without tools; lesson transitions grant the authored kit."""

        return self.MOVEMENT_SELECTION

    def allows_class_selection(
        self,
        player: "Player",
        selection: ClassSelection,
    ) -> bool:
        """Reject menu-driven inventory changes during the scripted lesson."""

        return False

    def allows_equipped_tool(self, player: "Player", tool_id: int) -> bool:
        """Accept ClientData tool changes only after that lesson unlocks them."""

        session = self._sessions.get(id(player))
        if session is None or session.stage < TutorialStage.SHOOTING:
            return False
        allowed = (
            self.SHOOTING_LOADOUT
            if session.stage is TutorialStage.SHOOTING
            else self.CLIMB_LOADOUT
        )
        return int(tool_id) in allowed

    def allows_team_change(self, player: "Player", new_team: int) -> bool:
        """Tutorial lanes are not team-swappable."""

        return False

    def prepare_player_spawn(self, player: "Player") -> None:
        """Restore the stage-appropriate native tool across accidental deaths."""

        session = self._sessions.get(id(player))
        tool = (
            int(C.SPADE_TOOL)
            if session is not None and session.stage >= TutorialStage.CLIMB
            else int(C.PISTOL_TOOL)
        )
        player.weapon = tool
        player.tool = tool
        player.tool_is_raw = True

    def get_spawn_point(self, player: "Player") -> tuple[float, float, float]:
        """Allocate one authored interior lane and reset its five targets."""

        token = id(player)
        session = self._sessions.get(token)
        if session is None:
            lane_index = next(
                (
                    index
                    for index in range(len(self.LANE_ORIGINS))
                    if index not in self._lane_occupants
                ),
                None,
            )
            if lane_index is None:
                raise RuntimeError("all twelve tutorial lanes are occupied")
            session = TutorialSession(player=player, lane_index=lane_index)
            self._sessions[token] = session
            self._lane_occupants[lane_index] = token
            self._restore_lane_targets(lane_index)

        origin_x, origin_y = self.LANE_ORIGINS[session.lane_index]
        local_x, local_y, z = self.SPAWN_LOCAL
        return (origin_x + local_x, origin_y + local_y, z)

    def get_spawn_orientation(self, player: "Player") -> tuple[float, float, float]:
        """Face players from the corridor toward the obstacle course."""

        return self.SPAWN_ORIENTATION

    def can_player_respawn(self, player: "Player") -> bool:
        """Allow an accidental death to restart inside the same lane."""

        return not self.ended

    def respawn_time_for(self, player: "Player") -> float:
        """Return immediately; the tutorial is not a competitive death loop."""

        return 0.0

    def modify_incoming_damage(
        self,
        player: "Player",
        amount: int,
        source: "Player | None",
        kill_type: int,
    ) -> int:
        """Make training participants invulnerable."""

        return 0

    def configure_state_data(self, packet) -> None:
        """Expose one locked Soldier lane and suppress competitive HUD state."""

        packet.team1_classes = [int(C.CLASS_SOLDIER)]
        packet.team2_classes = []
        packet.team1_locked = False
        packet.team2_locked = True
        packet.team1_locked_class = True
        packet.team2_locked_class = True
        packet.lock_team_swap = True
        packet.lock_spectator_swap = True
        packet.team1_show_score = False
        packet.team1_show_max_score = False
        packet.team2_show_score = False
        packet.team2_show_max_score = False
        packet.team1_infinite_blocks = False
        packet.team2_infinite_blocks = False
        packet.score_limit = 0

    def configure_initial_info(self, packet) -> None:
        """Publish the stock tutorial scene switches and its tiny tool set."""

        allowed_tools = set(self.TUTORIAL_LOADOUT)
        packet.disabled_tools = [
            tool
            for tool in range(int(C.NOOF_SELECTABLE_TOOLS))
            if tool not in allowed_tools
        ]
        packet.disabled_classes = [
            class_id
            for class_id in range(int(C.CLASS_NOOF))
            if class_id != int(C.CLASS_SOLDIER)
        ]
        packet.enable_minimap = 0
        packet.enable_colour_picker = 0
        packet.enable_colour_palette = 0
        packet.enable_deathcam = 0
        packet.enable_spectator = 0
        packet.enable_player_score = 0
        packet.enable_fall_on_water_damage = 0
        packet.friendly_fire = 0
        packet.enable_corpse_explosion = 0

    async def on_player_join(self, player: "Player") -> None:
        """Publish any target reset after CreatePlayer made its actor id safe."""

        session = self._sessions.get(id(player))
        if session is None:
            return
        pending = self._pending_restore_packets.pop(session.lane_index, ())
        for (x, y, z), color in pending:
            packet = BlockBuildColored()
            packet.loop_count = int(getattr(self.server, "loop_count", 0))
            packet.player_id = int(player.id)
            packet.x, packet.y, packet.z = x, y, z
            packet.color = int(color) & 0xFFFFFF
            # The joining GameScene is still gated and catches this canonical
            # edit from its map watermark; settled observers already know this
            # player because CreatePlayer was broadcast before this hook.
            self.server.broadcast(
                bytes(packet.generate()),
                reliable=True,
                record_mutation=False,
            )

    async def on_player_leave(self, player: "Player") -> None:
        """Release exactly the departing object token, safe across id reuse."""

        token = id(player)
        session = self._sessions.pop(token, None)
        if session is None:
            return
        if self._lane_occupants.get(session.lane_index) == token:
            self._lane_occupants.pop(session.lane_index, None)

    def reveal_to(self, connection) -> None:
        """Start tutorial music and the first localized help panel post-load."""

        player = getattr(connection, "player", None)
        if player is None:
            return
        session = self._sessions.get(id(player))
        if session is None:
            return
        play_music_to(connection, "tutorial_music_001")
        session.revealed = True
        session.connection = connection
        session.revealed_at = time.monotonic()
        session.stage_started = session.revealed_at
        self._send_help(player, session.stage)

    async def on_tick(self, tick: int) -> None:
        """Advance bounded terrain events and position/input lesson gates."""

        if self.ended:
            return
        now = time.monotonic()
        self.elapsed_time = max(0.0, time.time() - self.start_time)
        self._drain_world_mutations(now)

        for token, session in tuple(self._sessions.items()):
            player = session.player
            if self._sessions.get(token) is not session or not session.revealed:
                continue
            if not bool(getattr(player, "spawned", False)):
                continue

            # The first ClientData can arrive while the native frontend is
            # still handing its main-menu music player to GameScene. A single
            # post-scene replay closes that race; repeating every tick would
            # constantly restart/fade the authored track.
            if (
                not session.music_replayed
                and session.connection is not None
                and now - session.revealed_at >= 1.0
            ):
                play_music_to(session.connection, "tutorial_music_001")
                session.music_replayed = True

            origin_x, _origin_y = self.LANE_ORIGINS[session.lane_index]
            local_x = float(getattr(player, "x", 0.0)) - origin_x
            session.minimum_local_x = min(session.minimum_local_x, local_x)
            input_state = getattr(player, "input", None)
            session.saw_jump = session.saw_jump or bool(
                getattr(input_state, "jump", False)
                or getattr(player, "last_trigger_jump", False)
            )
            session.saw_crouch = session.saw_crouch or bool(
                getattr(input_state, "crouch", False)
            )

            if session.stage is TutorialStage.INTRO:
                if now - session.stage_started >= self.INTRO_SECONDS:
                    self._enter_stage(session, TutorialStage.BASIC_CONTROLS, now)
            elif session.stage is TutorialStage.BASIC_CONTROLS:
                # The retail capsule collides at x=134.45 against the first
                # authored jump obstacle. Gate on the reachable approach side
                # rather than the voxel plane itself, or ordinary W movement
                # can never advance this lesson.
                if session.minimum_local_x <= 135.0:
                    self._enter_stage(session, TutorialStage.JUMP, now)
            elif session.stage is TutorialStage.JUMP:
                # Crossing x=119 proves the authored ledge was traversed even
                # if a very short input pulse fell between server samples.
                if (
                    session.saw_jump and session.minimum_local_x <= 128.0
                ) or session.minimum_local_x <= 119.0:
                    self._enter_stage(session, TutorialStage.CROUCH, now)
            elif session.stage is TutorialStage.CROUCH:
                # The corridor cannot be crossed standing; x=99 is therefore
                # a geometry-backed fallback for a missed crouch sample.
                if (
                    session.saw_crouch and session.minimum_local_x <= 108.0
                ) or session.minimum_local_x <= 99.0:
                    self._enter_stage(session, TutorialStage.SHOOTING, now)
            elif session.stage is TutorialStage.SHOOTING:
                if len(session.destroyed_targets) >= len(self.TARGET_CENTERS):
                    self._enter_stage(session, TutorialStage.CLIMB, now)
            elif session.stage is TutorialStage.CLIMB:
                # The recovered final prompt explicitly teaches click-drag
                # BlockLine. Two committed cells distinguish a line from an
                # accidental single click while still allowing two taps.
                if len(session.built_cells) >= 2:
                    self._enter_stage(session, TutorialStage.COMPLETE, now)
            elif session.stage is TutorialStage.COMPLETE:
                self._tick_completion(session, now)

    def session_for(self, player: object) -> TutorialSession | None:
        """Return read-only-by-convention progress for tests/administration."""

        return self._sessions.get(id(player))

    def _capture_authored_targets(self) -> None:
        """Index the repeated red bullseye voxels from the loaded Training VXL."""

        world = self.server.world_manager
        self._target_voxels = []
        self._target_lookup = {}
        for lane_index, (origin_x, origin_y) in enumerate(self.LANE_ORIGINS):
            lane_targets: list[dict[tuple[int, int, int], int]] = []
            for target_index, (local_x, local_y, center_z) in enumerate(
                self.TARGET_CENTERS
            ):
                x = origin_x + local_x
                center_y = origin_y + local_y
                voxels: dict[tuple[int, int, int], int] = {}
                for y in range(
                    center_y - self.TARGET_SCAN_RADIUS,
                    center_y + self.TARGET_SCAN_RADIUS + 1,
                ):
                    for z in range(
                        center_z - self.TARGET_SCAN_RADIUS,
                        center_z + self.TARGET_SCAN_RADIUS + 1,
                    ):
                        coordinate = (x, y, z)
                        if not world.get_solid(*coordinate):
                            continue
                        color = int(world.get_color(*coordinate)) & 0xFFFFFF
                        if color != self.TARGET_RGB:
                            continue
                        voxels[coordinate] = color
                        self._target_lookup[coordinate] = (
                            lane_index,
                            target_index,
                        )
                if not voxels:
                    raise RuntimeError(
                        "Training.vxl target geometry is missing at "
                        f"lane={lane_index} center={(x, center_y, center_z)}"
                    )
                lane_targets.append(voxels)
            self._target_voxels.append(lane_targets)

    def _restore_lane_targets(self, lane_index: int) -> None:
        """Restore destroyed/repainted bullseyes before assigning a reused lane."""

        if not self._target_voxels:
            return
        world = self.server.world_manager
        restored: list[tuple[tuple[int, int, int], int]] = []
        for target in self._target_voxels[lane_index]:
            for coordinate, color in target.items():
                current = (
                    int(world.get_color(*coordinate)) & 0xFFFFFF
                    if world.get_solid(*coordinate)
                    else None
                )
                if current != color:
                    if world.set_block(*coordinate, True, color):
                        restored.append((coordinate, color))
                clear_damage = getattr(world, "clear_block_damage", None)
                if callable(clear_damage):
                    clear_damage(*coordinate)
        if restored:
            self._pending_restore_packets[lane_index] = restored

    def _on_world_mutation(
        self,
        x: int,
        y: int,
        z: int,
        solid: bool,
        color: int,
        topology_version: int,
    ) -> None:
        """Enqueue one canonical edit without doing lesson work in the publisher."""

        if len(self._mutation_queue) >= self.MUTATION_QUEUE_LIMIT:
            return
        self._mutation_queue.append((int(x), int(y), int(z), bool(solid)))

    def _drain_world_mutations(self, now: float) -> None:
        """Interpret a bounded edit batch as target hits or construction."""

        for _ in range(min(len(self._mutation_queue), self.MUTATION_DRAIN_BUDGET)):
            x, y, z, solid = self._mutation_queue.popleft()
            coordinate = (x, y, z)
            if not solid:
                target = self._target_lookup.get(coordinate)
                if target is None:
                    continue
                lane_index, target_index = target
                token = self._lane_occupants.get(lane_index)
                session = self._sessions.get(token) if token is not None else None
                if session is None or target_index in session.destroyed_targets:
                    continue
                session.destroyed_targets.add(target_index)
                if session.revealed:
                    remaining = max(
                        0,
                        len(self.TARGET_CENTERS)
                        - len(session.destroyed_targets),
                    )
                    self._send_target_remaining(session.player, remaining)
                    if (
                        remaining == 0
                        and session.stage is TutorialStage.SHOOTING
                    ):
                        self._enter_stage(session, TutorialStage.CLIMB, now)
                continue

            lane_index = self._lane_for_coordinate(x, y)
            if lane_index is None or coordinate in self._target_lookup:
                continue
            token = self._lane_occupants.get(lane_index)
            session = self._sessions.get(token) if token is not None else None
            if session is not None and session.stage is TutorialStage.CLIMB:
                session.built_cells.add(coordinate)

    def _lane_for_coordinate(self, x: int, y: int) -> int | None:
        """Return the authored lane rectangle containing one map coordinate."""

        for lane_index, (origin_x, origin_y) in enumerate(self.LANE_ORIGINS):
            if (
                origin_x <= x < origin_x + self.LANE_WIDTH
                and origin_y <= y < origin_y + self.LANE_HEIGHT
            ):
                return lane_index
        return None

    def _enter_stage(
        self,
        session: TutorialSession,
        stage: TutorialStage,
        now: float,
    ) -> None:
        """Commit one monotonic stage transition and publish its native HUD."""

        if stage <= session.stage:
            return
        session.stage = stage
        session.stage_started = now
        # Send the inventory transaction before its matching help text so the
        # HUD and held model already agree when the localized panel appears.
        if stage is TutorialStage.SHOOTING:
            self._grant_loadout(session, self.SHOOTING_LOADOUT)
        elif stage is TutorialStage.CLIMB:
            self._grant_loadout(session, self.CLIMB_LOADOUT)
        self._send_help(session.player, stage)
        if stage is TutorialStage.SHOOTING:
            if len(session.destroyed_targets) >= len(self.TARGET_CENTERS):
                self._enter_stage(session, TutorialStage.CLIMB, now)
        elif stage is TutorialStage.COMPLETE:
            session.completion_deadline = now + self.COMPLETION_SECONDS
            session.last_countdown_value = None
            play_sound_to(session.player, SND_TUTORIAL_COMPLETE)
            self._send_countdown(session.player, self.COMPLETION_SECONDS)

    def _grant_loadout(
        self,
        session: TutorialSession,
        loadout: tuple[int, ...],
    ) -> None:
        """Atomically update authority and every native observer's loadout.

        Packet 13 is ordinarily client-to-server during class selection, but
        the retail GameScene also has a server receive handler for it.  With
        ``instant=1`` that handler applies the active loadout and equips the
        final item, which is why the climb grant deliberately ends in spade.
        """

        player = session.player
        selection = ClassSelection(
            class_id=int(C.CLASS_SOLDIER),
            loadout=tuple(int(tool) for tool in loadout),
        )
        player.apply_class_selection(selection)
        equipped = int(loadout[-1])
        player.set_tool(equipped, raw=True)

        packet = SetClassLoadout()
        packet.player_id = int(player.id)
        packet.class_id = int(C.CLASS_SOLDIER)
        packet.instant = 1
        packet.loadout = list(loadout)
        packet.prefabs = []
        packet.ugc_tools = []
        payload = bytes(packet.generate())
        player.send(payload, reliable=True)
        self.server.broadcast(payload, exclude=player, reliable=True)

    def _send_help(self, player: object, stage: TutorialStage) -> None:
        """Replace one player's localized retail HelpPanel contents."""

        packet = HelpMessage()
        packet.delay = float(self.HELP_TRANSITION_DELAY)
        packet.message_ids = list(self.HELP_BY_STAGE[stage])
        player.send(bytes(packet.generate()), reliable=True)

    @staticmethod
    def _send_target_remaining(player: object, remaining: int) -> None:
        """Show the stock localized target-destroyed counter to one player."""

        player.send(
            build_localised_overlay(
                "TUTORIAL_DESTROY_TARGET",
                (int(remaining),),
                override_previous=True,
            ),
            reliable=True,
        )

    @staticmethod
    def _send_countdown(player: object, seconds: float) -> None:
        """Set the native tutorial exit countdown for one GameScene."""

        packet = DisplayCountdown()
        packet.timer = float(max(0.0, seconds))
        player.send(bytes(packet.generate()), reliable=True)

    def _tick_completion(self, session: TutorialSession, now: float) -> None:
        """Refresh the visible integer timer, then close the completed session."""

        deadline = session.completion_deadline
        if deadline is None:
            return
        remaining = max(0.0, deadline - now)
        display_value = int(math.ceil(remaining))
        if display_value != session.last_countdown_value:
            session.last_countdown_value = display_value
            self._send_countdown(session.player, remaining)
        if remaining > 0.0 or session.disconnect_sent:
            return
        session.disconnect_sent = True
        session.player.disconnect(reason=int(C.DISCONNECT.ERROR_MATCH_ENDED))


__all__ = ["TutorialMode", "TutorialSession", "TutorialStage"]
