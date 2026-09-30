"""Isolated reconstruction of the retail Map Creator host ruleset.

This module is intentionally absent from :mod:`modes`' normal registry.  The
dedicated ``run_map_creator.py`` entrypoint validates a genuine baseplate and
then registers it process-locally, just like the reconstructed tutorial.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import time
from typing import TYPE_CHECKING
import zlib

import shared.constants as C
import shared.constants_gamemode as MG
from shared.packet import (
    FogColor,
    ForceTeamJoin,
    InitialUGCBatch,
    MapDataChunk,
    MapDataEnd,
    MapDataStart,
    PlaceUGC,
    SetGroundColors,
    SetUGCEditMode,
    SkyboxData,
    UGCBatchEntity,
    UGCMapInfo,
    UGCObjectives,
)

from modes.base_mode import BaseMode
from server.announcements import build_localised_overlay
from server.audio import SND_PAINT, play_sound
from server.class_selection import ClassSelection, normalize_server_selection
from server.game_constants import TEAM1, TEAM_NEUTRAL
from server.ugc_capacity import UGCCapacity
from server.ugc_project import (
    UGCPlacement,
    UGCProject,
    mode_id,
    normalize_target_mode,
)

if TYPE_CHECKING:
    from server.connection import Connection
    from server.player import Player


logger = logging.getLogger(__name__)


class UGCMode(BaseMode):
    """Own editor roles, object state, validation packets, and checkpoints.

    Tick/thread contract: all project mutations occur on the gameplay thread.
    Project snapshots are serialized there and disk writes are serialized by
    one async lock. VXL serialization occurs only for an explicit save or
    shutdown, never in the periodic 60 Hz path.
    """

    # No combat score economy: no generic kill/suicide SetScore.
    generic_scoring_enabled = False
    # No combat stakes: never reveal builders as map escapers.
    escape_watch_enabled = False

    name = "Map Creator"
    description = "Retail-compatible hosted UGC editor"
    score_limit = 0
    time_limit = 0
    CHECKPOINT_SECONDS = 1.0
    BATCH_SIZE = 512
    # Recovered from aoslib.ugc_data.ugc_data.DATA_CHUNK_SIZE.  The stock
    # lobby host feeds this many raw VXL bytes into one persistent zlib stream
    # before deciding whether packet 56 has output ready.
    VXL_SOURCE_CHUNK_SIZE = 1048
    # One positional PAINT_PRIMARY_SOUND per editor per interval; the retail
    # brush fires every 30 ms, which would otherwise flood the audio lane.
    PAINT_SOUND_INTERVAL = 0.1
    SAVE_SUCCESS_STRING = "UGC_MAP_SAVE_SUCCESSFULLY"
    SAVE_ERROR_STRING = "UGC_MAP_SAVE_ERROR"

    def __init__(self, server) -> None:
        if not bool(getattr(server.config, "ugc_runtime", False)):
            raise RuntimeError(
                "UGCMode is isolated; launch it with run_map_creator.py"
            )
        super().__init__(server)
        project = getattr(server.config, "ugc_project", None)
        if not isinstance(project, UGCProject):
            raise RuntimeError("Map Creator runtime is missing its UGCProject")
        self.project = project
        self._host_connection: object | None = None
        self._mutation_listener_token: int | None = None
        self._metadata_dirty = False
        self._world_dirty = False
        self._last_checkpoint = 0.0
        self._checkpoint_task: asyncio.Task | None = None
        self._preview_task: asyncio.Task | None = None
        self._save_task: asyncio.Task | None = None
        self._save_waiters: list[object] = []
        self._project_write_lock = asyncio.Lock()
        # Stock clients only see markers as CreateEntity(21) type
        # UGC_ENTITY(29); packets 97/98 have no gameScene receive handler.
        self._marker_entities: dict[UGCPlacement, int] = {}
        self.capacity: UGCCapacity | None = None
        self._paint_sound_next: dict[int, float] = {}

    async def on_mode_start(self) -> None:
        """Start the non-competitive editor without match music or pickups."""

        self.started = True
        self.ended = False
        self.winner = None
        self.start_time = time.time()
        self.elapsed_time = 0.0
        for team in self.server.teams.values():
            team.infinite_blocks = True
        self._build_capacity()
        subscribe = getattr(self.server.world_manager, "subscribe_mutations", None)
        if callable(subscribe):
            self._mutation_listener_token = subscribe(self._on_world_mutation)
        for placement in tuple(self.project.placements):
            self._create_marker(placement, announce=False)
        logger.info(
            "UGC editor ready: project=%s terrain=%s target=%s entities=%d",
            self.project.title,
            self.project.baseplate,
            self.project.target_mode,
            len(self.project.placements),
        )

    async def on_mode_end(self, winner=None) -> None:
        """Checkpoint VXL and sidecar without invoking the victory sequence."""

        await self.deactivate()

    async def deactivate(self) -> None:
        """Detach listeners and atomically persist the complete authored map."""

        token = self._mutation_listener_token
        if token is not None:
            unsubscribe = getattr(self.server.world_manager, "unsubscribe_mutations", None)
            if callable(unsubscribe):
                unsubscribe(token)
        self._mutation_listener_token = None
        task = self._checkpoint_task
        if task is not None:
            try:
                await task
            except (OSError, asyncio.CancelledError):
                logger.warning("UGC metadata checkpoint did not finish", exc_info=True)
        save_task = self._save_task
        if save_task is not None:
            try:
                await save_task
            except (OSError, asyncio.CancelledError):
                logger.warning("UGC save request did not finish", exc_info=True)
        preview_task = self._preview_task
        if preview_task is not None:
            try:
                await preview_task
            except (OSError, asyncio.CancelledError):
                logger.warning("UGC preview checkpoint did not finish", exc_info=True)
        try:
            await asyncio.to_thread(self._save_complete_project)
        except Exception:
            logger.exception("UGC final project checkpoint failed")
        self._host_connection = None
        self.started = False
        self.ended = True

    async def on_tick(self, tick: int) -> None:
        """Schedule at most one non-blocking metadata checkpoint per second."""

        if self.ended or not self._metadata_dirty:
            return
        now = time.monotonic()
        if now - self._last_checkpoint < self.CHECKPOINT_SECONDS:
            return
        if self._checkpoint_task is not None and not self._checkpoint_task.done():
            return
        self._last_checkpoint = now
        self._checkpoint_task = asyncio.create_task(
            self._checkpoint_metadata()
        )

    async def _checkpoint_metadata(self) -> None:
        """Snapshot after older writes finish, so metadata cannot regress."""

        async with self._project_write_lock:
            payload = json.dumps(
                self.project.to_sidecar(), indent=4, ensure_ascii=False
            ) + "\n"
            self._metadata_dirty = False
            destination = Path(self.server.config.ugc_sidecar_path)
            try:
                await asyncio.to_thread(_atomic_write_text, destination, payload)
            except Exception:
                self._metadata_dirty = True
                logger.exception("UGC metadata checkpoint failed")

    def prepare_join_team(self, requested_team: int) -> int:
        """All editors inhabit the one non-competitive builder team."""

        return TEAM1

    def prepare_join_selection(
        self,
        team: int,
        selection: ClassSelection,
    ) -> ClassSelection:
        """Normalize every join to UGC Builder and its five-slot backpack.

        The retail ``SelectUGC`` menu owns one ``inventory_items`` list for
        both constructs and Game Data objects.  ``UGC_MAX_LOADOUT_PREFABS``
        is therefore a combined limit, despite its historical name.  Packet
        13 serializes the two item kinds into separate arrays, so the server
        preserves the client's prefab-first wire order while enforcing the
        shared budget.  An empty selection stays empty; the native editor
        menu is responsible for choosing authored items.
        """

        capacity = int(C.UGC_MAX_LOADOUT_PREFABS)
        requested_prefabs = selection.prefabs[:capacity]
        remaining = max(0, capacity - len(requested_prefabs))
        requested_tools = selection.ugc_tools[:remaining]
        return normalize_server_selection(
            self.server.config,
            int(C.CLASS_UGCBUILDER),
            selection.loadout,
            requested_prefabs,
            requested_tools,
        )

    def allows_class_selection(
        self,
        player: "Player",
        selection: ClassSelection,
    ) -> bool:
        """Accept only normalized UGC Builder loadouts and palette prefabs."""

        if int(selection.class_id) != int(C.CLASS_UGCBUILDER):
            return False
        allowed = {
            str(name).casefold()
            for name in getattr(self.server.config, "ugc_prefabs", ())
        }
        return (
            len(selection.prefabs) + len(selection.ugc_tools)
            <= int(C.UGC_MAX_LOADOUT_PREFABS)
            and all(str(name).casefold() in allowed for name in selection.prefabs)
            and all(0 <= int(item_id) < 19 for item_id in selection.ugc_tools)
        )

    def allows_equipped_tool(self, player: "Player", tool_id: int) -> bool:
        """Keep held-tool replication within the committed builder loadout."""

        return int(tool_id) in {
            int(value) for value in (getattr(player, "loadout", ()) or ())
        }

    def allows_team_change(self, player: "Player", new_team: int) -> bool:
        """The editor does not expose competitive team switching."""

        return int(new_team) == TEAM1

    def modify_incoming_damage(
        self,
        player: "Player",
        amount: int,
        source: "Player | None",
        kill_type: int,
    ) -> int:
        """Keep authors alive while carving or flying around their terrain."""

        return 0

    smart_spawns = False

    def get_spawn_point(self, player: "Player") -> tuple[float, float, float]:
        """Use the map's prewarmed safe terrain resolver near its first side."""

        return self.server.world_manager.get_spawn_point(TEAM1)

    def configure_state_data(self, packet) -> None:
        """Expose one infinite-stock UGC Builder team and no match HUD."""

        packet.team1_classes = [int(C.CLASS_UGCBUILDER)]
        packet.team2_classes = []
        packet.team1_locked = False
        packet.team2_locked = True
        # Keep the sole class selectable: LoadingMenu detects UGC after the
        # forced-team packet and opens SelectPrefabs instead of bypassing the
        # native five-item editor backpack screen.
        packet.team1_locked_class = False
        packet.team2_locked_class = True
        packet.lock_team_swap = True
        packet.lock_spectator_swap = True
        packet.team1_show_score = False
        packet.team1_show_max_score = False
        packet.team2_show_score = False
        packet.team2_show_max_score = False
        packet.team1_infinite_blocks = True
        packet.team2_infinite_blocks = True
        packet.score_limit = 0
        # InitialInfo.ugc_prefab_sets tells the retail client which local
        # PNG/KV6 models to load; StateData.prefabs separately populates the
        # Construct Library's prefab_manager.map_prefabs list.  The count is
        # a 16-bit value on the native wire (Grassland has 373 entries), not
        # the byte-plus-padding layout once assumed by our packet codec.
        packet.prefabs = list(self.server.config.ugc_prefabs)

    def send_post_state_data(self, connection: "Connection") -> None:
        """Route a joining editor straight into the native UGC loadout menu.

        Packet 115 does not immediately spawn when ``instant`` is false.  It
        records ``GameScene.force_team_join`` so LoadingMenu's Start action
        selects the builder team and opens SelectPrefabs.  Without it a
        direct-connect editor falls through the ordinary competitive team and
        class menus, unlike the original Steam-lobby Map Creator flow.
        """

        packet = ForceTeamJoin()
        packet.team_id = TEAM1
        packet.instant = 0
        connection.send(bytes(packet.generate()), reliable=True)

    def configure_initial_info(self, packet) -> None:
        """Publish the global UGC scene switches and chosen terrain palette."""

        # Retail UGC maps are looked up in ``ugc/maps`` by their baseplate
        # stem.  The project title is Steam-lobby metadata, not a VXL
        # filename.  Advertising our server-side project slug here makes
        # GameClient.start_processing_map call ``len(None)`` before it can
        # answer MapDataValidation because no matching local UGC/baseplate
        # exists.  Keep both native lookup fields on the authored baseplate;
        # the checksum still describes the authoritative project VXL, so a
        # modified project naturally falls through to the map-sync path.
        packet.map_name = self.project.baseplate
        packet.filename = self.project.baseplate

        class_items = C.CLASS_ITEMS[int(C.CLASS_UGCBUILDER)]
        allowed_tools = {
            int(tool)
            for slot, values in class_items.items()
            if int(slot) not in (int(C.CLASS_PREFABS), int(C.CLASS_UGC_TOOLS))
            for tool in values
        }
        packet.disabled_tools = [
            tool for tool in range(int(C.NOOF_SELECTABLE_TOOLS))
            if tool not in allowed_tools
        ]
        packet.disabled_classes = [
            class_id for class_id in range(int(C.CLASS_NOOF))
            if class_id != int(C.CLASS_UGCBUILDER)
        ]
        packet.map_is_ugc = int(C.MAP_IS_UGC_CLIENT)
        packet.ugc_mode = mode_id(self.project.target_mode)
        packet.ugc_prefab_sets = [int(self.project.prefab_terrain.prefab_tag)]
        if self.project.ground_colors:
            packet.ground_colors = list(self.project.ground_colors)
        packet.enable_minimap = 1
        packet.enable_colour_picker = 1
        packet.enable_colour_palette = 1
        packet.enable_deathcam = 0
        packet.enable_spectator = 0
        packet.enable_player_score = 0
        packet.enable_fall_on_water_damage = 0
        packet.friendly_fire = 0
        packet.enable_corpse_explosion = 0

    def configure_initial_info_for(self, connection: "Connection", packet) -> None:
        """Assign one logical editor owner and a safe dedicated-host wire role.

        Retail ``MAP_IS_UGC_HOST`` is not merely a permission bit: it tells
        ``GameClient.start_processing_map`` to load VXL/UGC from the host's
        local Steam-lobby project.  A player direct-connecting to this
        dedicated launcher has no such object, so advertising HOST produces a
        native ``len(None)`` crash before MapSync.  The server therefore owns
        persistence and sends every socket CLIENT, while retaining the first
        connection as the sole authority for mutating editor metadata.
        """

        active_connections = tuple(self.server.connections.values())
        if not getattr(self.server.config, "ugc_owner_legacy_id", "") and not any(
            active is self._host_connection for active in active_connections
        ):
            self._host_connection = connection
        connection.ugc_editor_owner = connection is self._host_connection
        connection.ugc_role = int(C.MAP_IS_UGC_CLIENT)
        packet.map_is_ugc = int(C.MAP_IS_UGC_CLIENT)

    def send_pre_validation_map_data(self, connection: "Connection") -> None:
        """Act as the retail lobby host and stream its source VXL to a guest.

        Packet order is a native crash invariant.  ``MAP_IS_UGC_CLIENT`` does
        not load a baseplate from disk: it waits for the lobby host's
        ``MapDataStart(54) -> MapDataChunk(56)* -> MapDataEnd(58)`` stream.
        The chunks together form one zlib stream of the raw VXL bytes.  Only
        after packet 58 does ``GameClient`` populate ``map_data`` and send
        ``MapDataValidation(60)``.  Sending packet 60 or MapSync first makes
        the compiled client evaluate ``len(None)`` in start_processing_map.

        This method runs during the bounded join handshake on the gameplay
        event-loop thread.  It performs no file I/O and reads the immutable
        map-load snapshot.  Normal MapSync follows and applies every authored
        mutation made since that snapshot.
        """

        raw_vxl = getattr(self.server.world_manager, "map_raw_bytes", None)
        if not isinstance(raw_vxl, bytes) or not raw_vxl:
            raise RuntimeError("UGC editor has no source VXL snapshot to transfer")

        connection.send(bytes(MapDataStart().generate()), reliable=True)

        compressor = zlib.compressobj()
        total = len(raw_vxl)
        offset = 0
        while offset < total:
            next_offset = min(offset + self.VXL_SOURCE_CHUNK_SIZE, total)
            compressed = compressor.compress(raw_vxl[offset:next_offset])
            offset = next_offset
            if compressed:
                chunk = MapDataChunk()
                chunk.percent_complete = min(100, int(offset * 100 / total))
                chunk.data = compressed
                # Packet 56 already contains zlib output.  The connection's
                # normal framing remains unchanged; no second bulk codec is
                # introduced here.
                connection.send(bytes(chunk.generate()), reliable=True)

        final_data = compressor.flush()
        if final_data:
            chunk = MapDataChunk()
            chunk.percent_complete = 100
            chunk.data = final_data
            connection.send(bytes(chunk.generate()), reliable=True)

        connection.send(bytes(MapDataEnd().generate()), reliable=True)
        logger.info(
            "Sent UGC host VXL to %s: raw=%d bytes",
            getattr(getattr(connection, "peer", None), "address", "test-peer"),
            total,
        )

    async def on_player_leave(self, player: "Player") -> None:
        """Release host ownership so the next connection can recover editing."""

        if getattr(player, "connection", None) is self._host_connection:
            self._host_connection = None

    def is_host(self, player_or_connection: object) -> bool:
        """Return whether an object resolves to the current UGC host socket."""

        connection = getattr(player_or_connection, "connection", player_or_connection)
        owner_id = getattr(self.server.config, "ugc_owner_legacy_id", "")
        if owner_id:
            player = getattr(connection, "player", None)
            return player is not None and str(getattr(player, "account_legacy_id", "")) == owner_id
        return connection is self._host_connection

    def reveal_to(self, connection: "Connection") -> None:
        """Replay editor objects and validation only after GameScene exists."""

        self.send_initial_batch(connection)
        self.send_objectives(connection)
        self.reveal_markers(connection)

    # ------------------------------------------------------------------
    # Stock-client marker entities (UGC-1)
    # ------------------------------------------------------------------

    @staticmethod
    def _marker_team(item_id: int) -> int:
        """Wire team for one marker; UGC_ENTITY_TEAMS values are wire ids."""

        team = int(C.UGC_ENTITY_TEAMS.get(int(item_id), C.TEAM_NEUTRAL))
        return team if team in (int(C.TEAM1), int(C.TEAM2)) else TEAM_NEUTRAL

    def _create_marker(self, placement: UGCPlacement, *, announce: bool = True) -> None:
        """Register one type-29 entity mirroring a placement.

        The native client materializes markers from 97/98 and ignores type
        29; the stock client needs this entity to draw, point at and remove
        a marker (gameScene 0x10178b80 / UGCEntity, weapons/ugcTool.py).
        """

        registry = getattr(self.server, "entity_registry", None)
        if registry is None or placement in self._marker_entities:
            return
        try:
            entity = registry.place(
                int(C.UGC_ENTITY),
                float(placement.x),
                float(placement.y),
                float(placement.z),
                state=self._marker_team(placement.item_id),
                kind="ugc_marker",
            )
        except RuntimeError:
            logger.warning("UGC marker entity id space exhausted")
            return
        entity.ugc_mode = mode_id(placement.mode)
        entity.int_properties = (int(placement.item_id),)
        self._marker_entities[placement] = int(entity.entity_id)
        if announce:
            broadcast = getattr(self.server, "broadcast_create_entity", None)
            if callable(broadcast):
                broadcast(entity)

    def _destroy_marker(self, placement: UGCPlacement) -> None:
        """Remove the type-29 entity of one removed placement."""

        entity_id = self._marker_entities.pop(placement, None)
        registry = getattr(self.server, "entity_registry", None)
        if entity_id is None or registry is None:
            return
        registry.remove(entity_id)
        destroy = getattr(self.server, "broadcast_destroy_entity", None)
        if callable(destroy):
            destroy(entity_id)

    def reveal_markers(self, connection: "Connection") -> None:
        """Create every live marker once in a GameScene (join/refresh)."""

        registry = getattr(self.server, "entity_registry", None)
        if registry is None:
            return
        from server.entities.registry import send_create_entity_to

        for entity_id in tuple(self._marker_entities.values()):
            entity = registry.get(entity_id)
            if entity is not None:
                send_create_entity_to(connection, entity)

    # ------------------------------------------------------------------
    # Retail map capacity (UGC-8)
    # ------------------------------------------------------------------

    def _build_capacity(self) -> None:
        raw = getattr(self.server.world_manager, "map_raw_bytes", None)
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            self.capacity = None
            return
        try:
            self.capacity = UGCCapacity.from_vxl(bytes(raw))
        except ValueError:
            logger.warning("UGC capacity model could not parse the project VXL")
            self.capacity = None
            return
        logger.info(
            "UGC capacity: %d solids, %d chunks (limits 2800000/3200)",
            self.capacity.solid_count,
            self.capacity.chunk_count,
        )

    def has_block_space(self) -> bool:
        """Retail ``is_space_to_add_blocks`` for UGC voxel additions."""

        capacity = self.capacity
        return capacity is None or capacity.has_space()

    # ------------------------------------------------------------------
    # Paintbrush single-block cue (UGC-9)
    # ------------------------------------------------------------------

    def on_single_paint(self, player: "Player", x: int, y: int, z: int) -> bool:
        """Send PAINT_PRIMARY_SOUND (id 47) at one accepted single-cell paint.

        ``constants_audio.PAINT_PRIMARY_SOUND`` has no client-side caller in
        any retail .py/.pyd, so it is a server PlaySound.  Throttled per
        editor; the RMB spray loop is client-local.
        """

        now = time.monotonic()
        key = int(getattr(player, "id", -1))
        if now < self._paint_sound_next.get(key, 0.0):
            return False
        self._paint_sound_next[key] = now + self.PAINT_SOUND_INTERVAL
        play_sound(
            self.server,
            SND_PAINT,
            position=(float(x) + 0.5, float(y) + 0.5, float(z) + 0.5),
            reliable=False,
        )
        return True

    def send_initial_batch(self, connection: "Connection") -> None:
        """Send bounded packet-98 chunks in recovered record order."""

        placements = tuple(self.project.placements)
        chunks = [
            placements[index:index + self.BATCH_SIZE]
            for index in range(0, len(placements), self.BATCH_SIZE)
        ] or [()]
        for placements_chunk in chunks:
            packet = InitialUGCBatch()
            packet.items = []
            for placement in placements_chunk:
                item = UGCBatchEntity()
                item.mode = mode_id(placement.mode)
                item.x, item.y, item.z = placement.position
                item.ugc_item_id = int(placement.item_id)
                packet.items.append(item)
            connection.send(bytes(packet.generate()), reliable=True)

    def send_objectives(self, connection: "Connection | None" = None) -> None:
        """Publish packet 68 using the native objective string/count arrays."""

        validation = self.project.validation()
        packet = UGCObjectives()
        packet.mode = mode_id(validation.mode)
        packet.noOfObjectives = len(validation.objectives)
        packet.objective_ids = [row.objective_id for row in validation.objectives]
        packet.objective_values = [row.value for row in validation.objectives]
        data = bytes(packet.generate())
        if connection is not None:
            connection.send(data, reliable=True)
        else:
            self.server.broadcast(data, reliable=True, record_mutation=False)

    def place_object(
        self,
        player: "Player",
        x: int,
        y: int,
        z: int,
        item_id: int,
        placing: bool,
    ) -> bool:
        """Commit one host-authorized object and echo packet 97 to observers.

        A removal echoes the REMOVED placement's stored position and item
        (the native client deletes by exact position+item) and destroys its
        type-29 entity for stock clients.
        """

        if not self.is_host(player):
            return False
        if placing:
            placement = self.project.place_placement(x, y, z, item_id)
        else:
            placement = self.project.remove(x, y, z, item_id)
        if placement is None:
            return False
        self._metadata_dirty = True
        packet = PlaceUGC()
        packet.loop_count = int(self.server.loop_count)
        packet.x, packet.y, packet.z = placement.position
        packet.ugc_item_id = int(placement.item_id)
        packet.placing = int(bool(placing))
        self.server.broadcast(
            bytes(packet.generate()), reliable=True, record_mutation=False
        )
        if placing:
            self._create_marker(placement)
        else:
            self._destroy_marker(placement)
        self.send_objectives()
        return True

    def set_target_mode(self, player: "Player", value: str | int) -> bool:
        """Apply one host settings change and update every editor client."""

        if not self.is_host(player):
            return False
        try:
            target = (
                normalize_target_mode(value)
                if isinstance(value, str)
                else normalize_target_mode(
                    MG.MODE_IDS_MODE.get(int(value), "")
                )
            )
        except ValueError:
            return False
        self.project.set_target_mode(target)
        self._metadata_dirty = True
        packet = SetUGCEditMode()
        packet.mode = mode_id(target)
        self.server.broadcast(
            bytes(packet.generate()), reliable=True, record_mutation=False
        )
        self.send_objectives()
        return True

    def set_title(self, player: "Player", value: str) -> bool:
        """Persist one bounded title from the maintained local UGC settings UI."""

        if not self.is_host(player):
            return False
        title = str(value)
        if (
            not title.strip()
            or len(title) > 80
            or "\x00" in title
            or "\r" in title
            or "\n" in title
        ):
            return False
        title = title.strip()
        if title == self.project.title:
            return True
        self.project.title = title
        self.project.modified_since_publish = True
        self._metadata_dirty = True
        return True

    def set_skybox(self, player: "Player", value: str) -> bool:
        """Commit one host-selected retail skydome and relay packet 51."""

        if not self.is_host(player):
            return False
        from server.map_metadata import normalize_skybox_name

        skybox = normalize_skybox_name(value)
        if skybox is None:
            return False
        self.project.skybox_name = skybox
        self.project.modified_since_publish = True
        metadata = getattr(self.server.world_manager, "map_metadata", None)
        if metadata is not None:
            metadata.skybox_name = skybox
        self._metadata_dirty = True
        packet = SkyboxData()
        packet.value = skybox
        self.server.broadcast(
            bytes(packet.generate()),
            exclude=player,
            reliable=True,
            record_mutation=False,
        )
        # Retail GameScene.set_skybox_name (0x1012d4c0): when the skydome is
        # a FOG_COLORS key, send FogColor(74) with make_color(FOG_COLORS[n])
        # and apply it locally.  The dedicated server is the host, so every
        # editor INCLUDING the requester receives it.
        fog = C.FOG_COLORS.get(skybox)
        if fog is not None:
            rgb = tuple(int(component) & 0xFF for component in fog[:3])
            if metadata is not None:
                metadata.fog_color = rgb
            fog_packet = FogColor()
            fog_packet.color = (rgb[0] << 16) | (rgb[1] << 8) | rgb[2]
            self.server.broadcast(
                bytes(fog_packet.generate()),
                reliable=True,
                record_mutation=False,
            )
        return True

    def set_ground_colors(self, player: "Player", values) -> bool:
        """Commit and relay the full host-selected terrain/water RGBA palette."""

        if not self.is_host(player):
            return False
        from server.map_metadata import normalize_ground_colors

        colors = normalize_ground_colors(values)
        if not colors or len(colors) != len(values):
            return False
        self.project.ground_colors = colors
        self.project.modified_since_publish = True
        metadata = getattr(self.server.world_manager, "map_metadata", None)
        if metadata is not None:
            metadata.ground_colors = list(colors)
        self._metadata_dirty = True
        packet = SetGroundColors()
        packet.ground_colors = list(colors)
        self.server.broadcast(
            bytes(packet.generate()),
            exclude=player,
            reliable=True,
            record_mutation=False,
        )
        return True

    async def receive_preview(self, player: "Player", data: bytes) -> bool:
        """Persist a host PNG off-thread and relay it to editor guests."""

        if (
            not self.is_host(player)
            or len(data) > 8 * 1024 * 1024
            or not data.startswith(b"\x89PNG\r\n\x1a\n")
        ):
            return False
        if self._preview_task is not None and not self._preview_task.done():
            return False
        destination = Path(self.server.config.ugc_preview_path)
        payload = bytes(data)
        self._preview_task = asyncio.create_task(
            self._persist_preview(destination, payload)
        )
        packet = UGCMapInfo()
        packet.png_data = payload
        self.server.broadcast(
            bytes(packet.generate()),
            exclude=player,
            reliable=True,
            record_mutation=False,
        )
        return True

    async def _persist_preview(self, destination: Path, payload: bytes) -> None:
        """Write one PNG, then commit its sidecar flag on the event loop."""

        await asyncio.to_thread(_atomic_write_bytes, destination, payload)
        self.project.use_overhead_image = True
        self.project.modified_since_publish = True
        self._metadata_dirty = True

    def send_preview(self, connection: "Connection") -> bool:
        """Send the optional overhead PNG through packet 102 with a size cap."""

        preview = Path(getattr(self.server.config, "ugc_preview_path", ""))
        if not preview.is_file():
            return False
        data = preview.read_bytes()
        if len(data) > 8 * 1024 * 1024 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        packet = UGCMapInfo()
        packet.png_data = data
        connection.send(bytes(packet.generate()), reliable=True)
        return True

    def request_checkpoint(self) -> None:
        """Mark metadata for the next bounded asynchronous checkpoint."""

        self._metadata_dirty = True

    def request_save(self, player: "Player") -> bool:
        """Flush VXL + sidecar now and acknowledge the requesting host.

        Sent by the native F10 quick save / Esc SAVE as UGCMessage(100)
        UGC_CONVERT_TO_GAME (no shipped client sends that id).  Requests
        coalesce: one arriving while a save runs is served by one more save
        after it, so an ack never predates edits made before the request.
        Non-host requests are ignored (only the retail lobby owner saves).
        """

        if not self.is_host(player) or self.ended:
            return False
        connection = getattr(player, "connection", None)
        if connection is None:
            return False
        self._save_waiters.append(connection)
        if self._save_task is None or self._save_task.done():
            self._save_task = asyncio.create_task(self._run_saves())
        return True

    async def _run_saves(self) -> None:
        while self._save_waiters:
            waiters, self._save_waiters = self._save_waiters, []
            ok = True
            try:
                # Periodic metadata checkpoints share the same destination
                # and temporary filename. Snapshot and write in lock order:
                # an older checkpoint must not replace an acknowledged save.
                async with self._project_write_lock:
                    raw = bytes(self.server.world_manager.map.generate_vxl(False))
                    sidecar_text = json.dumps(
                        self.project.to_sidecar(), indent=4, ensure_ascii=False
                    ) + "\n"
                    self._metadata_dirty = False
                    self._world_dirty = False
                    await asyncio.to_thread(
                        self._write_project_files, raw, sidecar_text
                    )
            except Exception:
                logger.exception("UGC save request failed")
                self._metadata_dirty = True
                self._world_dirty = True
                ok = False
            data = build_localised_overlay(
                self.SAVE_SUCCESS_STRING if ok else self.SAVE_ERROR_STRING,
                override_previous=True,
            )
            for connection in waiters:
                try:
                    connection.send(data, reliable=True)
                except Exception:
                    logger.debug("UGC save ack send failed", exc_info=True)

    def _write_project_files(self, raw: bytes, sidecar_text: str) -> None:
        _atomic_write_bytes(Path(self.server.config.ugc_vxl_path), raw)
        _atomic_write_text(Path(self.server.config.ugc_sidecar_path), sidecar_text)

    def _on_world_mutation(
        self,
        x: int,
        y: int,
        z: int,
        solid: bool,
        color: int,
        topology_version: int,
    ) -> None:
        """Record a dirty bit and keep the O(1) capacity counters current."""

        self._world_dirty = True
        capacity = self.capacity
        if capacity is not None:
            capacity.apply(int(x), int(y), int(z), bool(solid))

    def _save_complete_project(self) -> None:
        """Serialize canonical VXL and sidecar during the stopped lifecycle."""

        sidecar = Path(self.server.config.ugc_sidecar_path)
        vxl = Path(self.server.config.ugc_vxl_path)
        raw = bytes(self.server.world_manager.map.generate_vxl(False))
        _atomic_write_bytes(vxl, raw)
        self.project.save(sidecar)
        self._metadata_dirty = False
        self._world_dirty = False


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(payload, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


__all__ = ["UGCMode"]
