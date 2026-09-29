"""Round 5 Map Creator parity: stock markers, capacity, save, tags, cues."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import shared.constants as C
from modes.ugc import UGCMode
from server.entities.registry import EntityRegistry, send_create_entity_to
from server.handlers.ugc import handle_place_ugc, handle_ugc_message
from server.ugc_capacity import (
    CHUNK_LIMIT,
    SOLID_LIMIT,
    UGCCapacity,
    chunk_index,
    ugc_capacity_full,
)
from server.ugc_project import (
    UGCProject,
    authored_mode_for_item,
    mode_id,
)
from shared.bytes import ByteReader
from shared.packet import CreateEntity, LocalisedMessage, PlaceUGC


RETAIL_UGC_MAPS = Path(
    os.environ.get("AOS_RETAIL_ROOT", r"G:\AoSRevival\aceofspades_nonsteam")
) / "ugc" / "maps"


class _Connection:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.in_game = True
        self.known_entity_ids: set[int] = set()

    def send(self, data, reliable=True) -> None:
        self.sent.append(bytes(data))


class _Server:
    """Editor facade with the real entity registry and per-peer symmetry."""

    def __init__(self, project: UGCProject, tmp_path: Path) -> None:
        self.config = SimpleNamespace(
            ugc_runtime=True,
            ugc_project=project,
            ugc_sidecar_path=str(tmp_path / "map.ugc"),
            ugc_vxl_path=str(tmp_path / "map.vxl"),
            ugc_preview_path=str(tmp_path / "map.png"),
            ugc_prefabs=(),
        )
        self.connections: dict[int, _Connection] = {}
        self.teams = {}
        self.entity_registry = EntityRegistry()
        self.world_manager = SimpleNamespace(
            get_spawn_point=lambda team: (1, 2, 3),
            map_metadata=SimpleNamespace(skybox_name=None, ground_colors=[]),
            map_raw_bytes=None,
            map=SimpleNamespace(generate_vxl=lambda _underwater: b"saved-vxl"),
        )
        self.loop_count = 5
        self.broadcasts: list[bytes] = []
        self.destroyed: list[int] = []

    def broadcast(self, data, **kwargs) -> None:
        self.broadcasts.append(bytes(data))

    def broadcast_create_entity(self, entity) -> None:
        for connection in self.connections.values():
            send_create_entity_to(connection, entity)

    def broadcast_destroy_entity(self, entity_id: int) -> None:
        self.destroyed.append(int(entity_id))
        for connection in self.connections.values():
            connection.known_entity_ids.discard(int(entity_id))


def _project(target: str = "ctf") -> UGCProject:
    return UGCProject(
        title="R5", description="R5", author="Builder",
        baseplate="GrasslandBaseplate", target_mode=target,
    )


def _editor(tmp_path: Path, project: UGCProject | None = None):
    project = project or _project()
    server = _Server(project, tmp_path)
    mode = UGCMode(server)
    host = _Connection()
    server.connections[1] = host
    mode.configure_initial_info_for(host, SimpleNamespace())
    player = SimpleNamespace(connection=host, id=1, name="Host")
    return server, mode, host, player


def _create_entities(connection: _Connection) -> list:
    return [
        CreateEntity(ByteReader(data[1:])).entity
        for data in connection.sent
        if data and data[0] == 21
    ]


def test_markers_are_type_29_entities_with_item_and_mode(tmp_path: Path) -> None:
    server, mode, host, player = _editor(tmp_path)
    assert mode.place_object(
        player, 100, 120, 220, int(C.UGC_ITEM_BLUE_SPAWN_ZONE_SMALL), True
    )
    entities = _create_entities(host)
    assert len(entities) == 1
    entity = entities[0]
    assert entity.type == int(C.UGC_ENTITY) == 29
    assert entity.int_properties == [int(C.UGC_ITEM_BLUE_SPAWN_ZONE_SMALL)]
    assert entity.ugc_mode == mode_id("ctf")
    assert (entity.pos_x, entity.pos_y, entity.pos_z) == (100.0, 120.0, 220.0)
    assert entity.face == 4
    assert entity.state == int(C.TEAM1)

    # Crate drop points are shared (nor) and neutral.
    assert mode.place_object(
        player, 101, 120, 220, int(C.UGC_ITEM_AMMO_DROP_POINT), True
    )
    crate = _create_entities(host)[-1]
    assert crate.ugc_mode == mode_id("nor")
    assert crate.state == int(C.TEAM_NEUTRAL)

    # A late joiner sees each marker exactly once, even after a refresh.
    guest = _Connection()
    server.connections[2] = guest
    mode.reveal_markers(guest)
    mode.reveal_markers(guest)
    assert len(_create_entities(guest)) == 2

    # Removal destroys the entity everywhere.
    entity_id = entity.entity_id
    assert mode.place_object(
        player, 100, 120, 220, int(C.UGC_ITEM_BLUE_SPAWN_ZONE_SMALL), False
    )
    assert server.destroyed == [entity_id]
    assert server.entity_registry.get(entity_id) is None


def test_other_entities_keep_zero_ugc_wire_tail() -> None:
    registry = EntityRegistry()
    crate = registry.place(int(C.AMMO_CRATE), 1.0, 2.0, 3.0)
    wire = crate.to_wire_entity()
    assert wire.ugc_mode == 0
    assert wire.int_properties == []


def test_authored_mode_follows_retail_get_ugc_mode() -> None:
    for item in (
        C.UGC_ITEM_HEALTH_DROP_POINT,
        C.UGC_ITEM_AMMO_DROP_POINT,
        C.UGC_ITEM_BLOCK_DROP_POINT,
    ):
        assert authored_mode_for_item(int(item), "ctf") == "nor"
    assert authored_mode_for_item(int(C.UGC_ITEM_OCC_BOMB_POINT), "tdm") == "oc"
    assert authored_mode_for_item(int(C.UGC_ITEM_GREEN_BASE_ZONE_LARGE), "tc") == "tc"


def test_tolerant_removal_echoes_stored_placement(tmp_path: Path) -> None:
    server, mode, host, player = _editor(tmp_path)
    item = int(C.UGC_ITEM_GREEN_SPAWN_ZONE_SMALL)
    assert mode.place_object(player, 50, 60, 220, item, True)
    # The stock ghost may sit one cell away from the entity.
    assert mode.place_object(player, 50, 61, 220, item, False)
    echo = PlaceUGC()
    echo.read(ByteReader(server.broadcasts[-2][1:]))
    assert (echo.x, echo.y, echo.z, echo.ugc_item_id, echo.placing) == (
        50, 60, 220, item, 0,
    )
    assert mode.project.placements == []
    # Beyond distance^2 1 nothing is removed.
    assert mode.place_object(player, 50, 60, 220, item, True)
    assert not mode.place_object(player, 51, 61, 220, item, False)


def test_removal_prefers_visible_mode_and_same_item() -> None:
    project = _project("tdm")
    zone = int(C.UGC_ITEM_BLUE_SPAWN_ZONE_SMALL)
    project.place(10, 10, 220, zone, mode="ctf")  # hidden in tdm
    project.place(10, 10, 220, zone, mode="tdm")
    removed = project.remove(10, 10, 220, zone)
    assert removed is not None and removed.mode == "tdm"
    project.place(20, 20, 220, int(C.UGC_ITEM_AMMO_DROP_POINT))
    project.place(20, 20, 220, int(C.UGC_ITEM_HEALTH_DROP_POINT))
    removed = project.remove(20, 20, 220, int(C.UGC_ITEM_AMMO_DROP_POINT))
    assert removed.item_id == int(C.UGC_ITEM_AMMO_DROP_POINT)


def test_placeholders_tags_and_target_mode_round_trip(tmp_path: Path) -> None:
    project = UGCProject(title="", description="", author="", baseplate="desert")
    assert project.title == "Untitled UGC"
    assert project.description == "Undescribed UGC"
    assert project.refresh_tags() == ["map"]

    # Complete the TDM requirements only.
    for index, item in enumerate((
        C.UGC_ITEM_AMMO_DROP_POINT,
        C.UGC_ITEM_HEALTH_DROP_POINT,
        C.UGC_ITEM_BLOCK_DROP_POINT,
    )):
        project.place(30 + index, 40, 220, int(item))
        project.place(30 + index, 41, 220, int(item))
    project.set_target_mode("tdm")
    project.place(100, 100, 220, int(C.UGC_ITEM_BLUE_SPAWN_ZONE_SMALL))
    project.place(101, 100, 220, int(C.UGC_ITEM_GREEN_SPAWN_ZONE_SMALL))
    publishable = project.publishable_modes()
    assert "tdm" in publishable
    project.set_target_mode("ctf")
    sidecar = project.to_sidecar()
    assert sidecar["tags"] == ["map", *publishable]
    assert sidecar["ugc_target_mode"] == "ctf"
    path = project.save(tmp_path / "p.ugc")
    assert UGCProject.load(path).target_mode == "ctf"


def test_save_request_flushes_and_acks_only_the_host(tmp_path: Path) -> None:
    server, mode, host, player = _editor(tmp_path)
    guest = _Connection()
    server.connections[2] = guest
    guest_player = SimpleNamespace(connection=guest, id=2, name="Guest")
    server.mode = mode

    async def run() -> None:
        await handle_ugc_message(
            server, guest_player,
            SimpleNamespace(message_id=int(C.UGC_CONVERT_TO_GAME)),
        )
        await handle_ugc_message(
            server, player, SimpleNamespace(message_id=int(C.UGC_CONVERT_TO_GAME))
        )
        await mode._save_task

    asyncio.run(run())
    assert (tmp_path / "map.vxl").read_bytes() == b"saved-vxl"
    sidecar = json.loads((tmp_path / "map.ugc").read_text(encoding="utf-8"))
    assert sidecar["title"] == "R5"
    acks = [
        LocalisedMessage(ByteReader(data[1:]))
        for data in host.sent
        if data and data[0] == 50
    ]
    assert [ack.string_id for ack in acks] == ["UGC_MAP_SAVE_SUCCESSFULLY"]
    assert acks[0].chat_type == 3
    assert acks[0].parameters == []
    assert not any(data and data[0] == 50 for data in guest.sent)


def test_save_failure_acks_error(tmp_path: Path) -> None:
    server, mode, host, player = _editor(tmp_path)

    def broken(_underwater):
        raise RuntimeError("disk full")

    server.world_manager.map = SimpleNamespace(generate_vxl=broken)

    async def run() -> None:
        assert mode.request_save(player)
        await mode._save_task

    asyncio.run(run())
    ack = LocalisedMessage(ByteReader(host.sent[-1][1:]))
    assert ack.string_id == "UGC_MAP_SAVE_ERROR"


def test_markers_require_top_face_support(tmp_path: Path) -> None:
    server, mode, host, player = _editor(tmp_path)
    server.mode = mode
    solids = {(10, 10, 221)}
    server.world_manager.get_solid = lambda x, y, z: (x, y, z) in solids
    player.alive = True
    player.x, player.y, player.z = 10.0, 10.0, 218.0
    player.tool = int(C.UGC_TOOL)
    player.tool_is_raw = True
    player.loadout = [int(C.UGC_TOOL)]
    item = int(C.UGC_ITEM_AMMO_DROP_POINT)

    def packet(x, y, z, placing=1):
        return SimpleNamespace(x=x, y=y, z=z, ugc_item_id=item, placing=placing)

    import server.handlers.ugc as handlers

    original = handlers.active_tool_authorized
    handlers.active_tool_authorized = lambda *_args: True
    try:
        asyncio.run(handle_place_ugc(server, player, packet(10, 10, 219)))
        assert mode.project.placements == []  # hanging under nothing
        asyncio.run(handle_place_ugc(server, player, packet(10, 10, 220)))
        assert len(mode.project.placements) == 1
    finally:
        handlers.active_tool_authorized = original


def test_paint_cue_is_positional_and_throttled(tmp_path: Path) -> None:
    server, mode, host, player = _editor(tmp_path)
    assert mode.on_single_paint(player, 4, 5, 6) is True
    assert mode.on_single_paint(player, 4, 5, 6) is False
    from shared.packet import PlaySound

    cue = PlaySound(ByteReader(server.broadcasts[-1][1:]))
    assert cue.sound_id == 47  # PAINT_PRIMARY_SOUND_ID
    assert cue.positioned
    assert (cue.x, cue.y, cue.z) == (4.5, 5.5, 6.5)


def test_capacity_counts_and_limits() -> None:
    masks = [0] * (512 * 512)
    capacity = UGCCapacity(masks)
    assert capacity.solid_count == 0 and capacity.chunk_count == 0
    capacity.apply(0, 0, 0, True)
    capacity.apply(1, 0, 0, True)
    capacity.apply(1, 0, 0, True)  # recolour republish is idempotent
    assert capacity.solid_count == 2 and capacity.chunk_count == 1
    capacity.apply(16, 0, 0, True)
    assert capacity.chunk_count == 2
    capacity.apply(0, 0, 0, False)
    capacity.apply(1, 0, 0, False)
    assert capacity.solid_count == 1 and capacity.chunk_count == 1
    assert chunk_index(511, 511, 239) == 15359
    capacity.solid_count = SOLID_LIMIT
    capacity.chunk_count = CHUNK_LIMIT - 1
    assert capacity.has_space()
    capacity.chunk_count = CHUNK_LIMIT
    assert not capacity.has_space()
    capacity.solid_count = SOLID_LIMIT - 1
    assert capacity.has_space()


def test_capacity_gate_only_in_map_creator() -> None:
    full = SimpleNamespace(has_block_space=lambda: False)
    assert ugc_capacity_full(SimpleNamespace(
        config=SimpleNamespace(ugc_runtime=True), mode=full
    ))
    assert not ugc_capacity_full(SimpleNamespace(
        config=SimpleNamespace(ugc_runtime=False), mode=full
    ))


@pytest.mark.parametrize(
    ("stem", "solids", "chunks"),
    (
        ("DesertBaseplate", 2_820_321, 1_137),
        ("MountainBaseplate", 1_180_703, 672),
        ("WaterBaseplate", 262_144, 1_024),
    ),
)
def test_capacity_matches_retail_baseplates(stem: str, solids: int, chunks: int) -> None:
    path = RETAIL_UGC_MAPS / f"{stem}.vxl"
    if not path.is_file():
        pytest.skip("retail UGC baseplates are not installed")
    capacity = UGCCapacity.from_vxl(path.read_bytes())
    assert (capacity.solid_count, capacity.chunk_count) == (solids, chunks)
    assert capacity.has_space()
