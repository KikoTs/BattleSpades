"""Retail Map Creator project, mode, conversion, and prefab regressions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import zlib

import shared.constants as C
from modes import get_mode_class
from modes.ugc import UGCMode
from server.class_selection import ClassSelection
from server.config import ServerConfig
from server.game_constants import TEAM1, TEAM2
from server.handlers.blocks import handle_paint_block
from server.map_metadata import load_map_metadata
from server.player import Player
from server.prefab_actions import PrefabActionService
from server.projectiles import PROJECTILE_SPECS
from server.runtime_paths import RuntimePaths
from server.ugc_launcher import (
    apply_map_creator_config,
    build_parser,
    configure_ugc_runtime,
)
from server.ugc_project import (
    COMMON_MODE,
    TERRAINS,
    UGCAssetLayout,
    UGCProject,
    create_project_files,
    discover_ugc_assets,
    mode_id,
    read_baseplate_presentation,
)
from shared.bytes import ByteReader
from shared.packet import (
    BuildPrefabAction,
    ErasePrefabAction,
    ForceTeamJoin,
    InitialUGCBatch,
    MapDataChunk,
    PaintBlockPacket,
    PlaySound,
    SetGroundColors,
    SkyboxData,
    UGCObjectives,
)


ROOT = Path(__file__).resolve().parents[1]


def _fake_retail_assets(tmp_path: Path) -> UGCAssetLayout:
    root = tmp_path / "retail"
    maps = root / "ugc" / "maps"
    prefabs = root / "ugc" / "kv6"
    maps.mkdir(parents=True)
    prefabs.mkdir(parents=True)
    for terrain in TERRAINS:
        (maps / f"{terrain.stem}.vxl").write_bytes(
            b"retail-vxl-" + terrain.stem.encode("ascii")
        )
        (maps / f"{terrain.stem}.txt").write_text(
            "skybox_texture = 'User_Grassland.txt'\n"
            "ground_colors = ((1, 2, 3, 4), (5, 6, 7, 8))\n",
            encoding="utf-8",
        )
    # This model belongs to every recovered terrain palette.
    (prefabs / "ugc_prefab_primitive_block.kv6").write_bytes(b"kv6")
    return UGCAssetLayout(root, maps, prefabs)


def _complete_ctf_project() -> UGCProject:
    project = UGCProject(
        title="Project Alpha",
        description="A test project",
        author="Builder",
        baseplate="GrasslandBaseplate",
        target_mode="ctf",
    )
    for index, item in enumerate(
        (
            C.UGC_ITEM_AMMO_DROP_POINT,
            C.UGC_ITEM_HEALTH_DROP_POINT,
            C.UGC_ITEM_BLOCK_DROP_POINT,
        )
    ):
        project.place(30 + index, 40, 220, int(item))
        project.place(30 + index, 41, 220, int(item))
    for index, item in enumerate(
        (
            C.UGC_ITEM_BLUE_SPAWN_ZONE_SMALL,
            C.UGC_ITEM_GREEN_SPAWN_ZONE_SMALL,
            C.UGC_ITEM_BLUE_BASE_ZONE_SMALL,
            C.UGC_ITEM_GREEN_BASE_ZONE_SMALL,
        )
    ):
        project.place(100 + index, 100, 220, int(item))
    return project


def test_editor_is_not_selectable_through_normal_server_registry() -> None:
    assert get_mode_class("ugc") is None


def test_project_round_trip_preserves_retail_sidecar_and_requirements(
    tmp_path: Path,
) -> None:
    project = _complete_ctf_project()

    validation = project.validation()
    assert validation.complete is True
    assert all(row.minimum <= row.value <= row.maximum for row in validation.objectives)
    assert {
        placement.mode
        for placement in project.placements
        if placement.item_id in {
            int(C.UGC_ITEM_AMMO_DROP_POINT),
            int(C.UGC_ITEM_HEALTH_DROP_POINT),
            int(C.UGC_ITEM_BLOCK_DROP_POINT),
        }
    } == {COMMON_MODE}

    sidecar = project.save(tmp_path / "ProjectAlpha.ugc")
    reloaded = UGCProject.load(sidecar)
    assert reloaded.to_sidecar() == project.to_sidecar()

    # Objective zones remain associated with their authored target mode.
    reloaded.set_target_mode("tdm")
    assert reloaded.validation().complete is False
    reloaded.set_target_mode("ctf")
    assert reloaded.validation().complete is True


def test_retail_assets_create_byte_exact_project_triplet(tmp_path: Path) -> None:
    layout = _fake_retail_assets(tmp_path)
    discovered = discover_ugc_assets(layout.root)
    project = UGCProject(
        title="My Map",
        description="My Map",
        author="Builder",
        baseplate="grassland",
        target_mode="tdm",
    )

    vxl, metadata, sidecar = create_project_files(
        project, tmp_path / "projects", "My Map", discovered
    )

    source_vxl, source_metadata = discovered.terrain_files(project.terrain)
    assert vxl.read_bytes() == source_vxl.read_bytes()
    assert metadata.read_bytes() == source_metadata.read_bytes()
    assert UGCProject.load(sidecar).baseplate == "GrasslandBaseplate"
    skybox, colors = read_baseplate_presentation(metadata)
    assert skybox == "User_Grassland.txt"
    assert colors == [(1, 2, 3, 4), (5, 6, 7, 8)]


def test_txt_atmosphere_and_ugc_placements_layer_for_published_game(
    tmp_path: Path,
) -> None:
    vxl = tmp_path / "AuthoredMap.vxl"
    vxl.write_bytes(b"vxl")
    vxl.with_suffix(".txt").write_text(
        "skybox_texture = 'User_Grassland.txt'\n"
        "fog_color = (12, 34, 56)\n",
        encoding="utf-8",
    )
    vxl.with_suffix(".ugc").write_text(
        json.dumps(
            {
                "skybox_name": "User_Desert.txt",
                "ugc_entities": [
                    {
                        "position": [100, 110, 220],
                        "mode": "ctf",
                        "item": "ugc_spawnblue_small",
                    },
                    {
                        "position": [300, 310, 220],
                        "mode": "ctf",
                        "item": "ugc_spawngreen_small",
                    },
                    {
                        "position": [200, 210, 220],
                        "mode": "nor",
                        "item": "ugc_ammo_drop",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    metadata = load_map_metadata(vxl, "ctf")

    assert metadata.source == vxl.with_suffix(".ugc")
    assert metadata.skybox_name == "User_Desert.txt"
    assert metadata.fog_color == (12, 34, 56)
    assert len(metadata.spawn_zones[TEAM1]) == 1
    assert len(metadata.spawn_zones[TEAM2]) == 1
    assert [entity.kind for entity in metadata.entities] == ["ammo"]


def test_dedicated_runtime_is_locked_and_uses_retail_prefab_catalog(
    tmp_path: Path,
) -> None:
    layout = _fake_retail_assets(tmp_path)
    project = _complete_ctf_project()
    project_dir = tmp_path / "projects"
    vxl, metadata, sidecar = create_project_files(
        project, project_dir, "ProjectAlpha", layout
    )
    config = ServerConfig()

    configured = configure_ugc_runtime(
        config,
        paths=RuntimePaths.from_root(ROOT),
        assets=layout,
        project=project,
        vxl_path=vxl,
        metadata_path=metadata,
        sidecar_path=sidecar,
        port=32902,
    )

    assert configured is config
    assert configured.ugc_runtime is True
    assert configured.default_mode == "ugc"
    assert configured.default_map == "ProjectAlpha"
    assert configured.port == 32902
    assert configured.map_rotation == []
    assert configured.bots.enabled is False
    assert configured.plugins_enabled is False
    assert configured.steam.enabled is False
    assert configured.ugc_prefabs == ("ugc_prefab_primitive_block",)
    assert configured.prefab_search_dirs == (
        str(RuntimePaths.from_root(ROOT).prefabs),
        str(layout.prefabs),
    )
    assert configured.game_rules.enabled("RULE_ENABLE_PREFABS") is True


def test_builder_join_enforces_one_five_item_editor_backpack() -> None:
    project = UGCProject(
        title="Loadout",
        description="Loadout",
        author="Builder",
        baseplate="grassland",
        target_mode="tdm",
    )
    config = ServerConfig()
    config.ugc_runtime = True
    config.ugc_project = project
    config.ugc_prefabs = tuple(f"construct_{index}" for index in range(7))
    config.game_rules.apply({
        "RULE_ENABLE_FLARE_BLOCKS": True,
        "RULE_ENABLE_PREFABS": True,
    })
    mode = UGCMode(SimpleNamespace(config=config))

    selection = mode.prepare_join_selection(
        TEAM2,
        ClassSelection(
            class_id=int(C.CLASS_MINER),
            loadout=(int(C.DRILLGUN_TOOL),),
            prefabs=tuple(reversed(config.ugc_prefabs)),
        ),
    )

    assert selection.class_id == int(C.CLASS_UGCBUILDER)
    assert selection.prefabs == tuple(reversed(config.ugc_prefabs))[:5]
    assert selection.ugc_tools == ()
    assert {
        int(C.UGC_DRILLGUN_TOOL),
        int(C.UGC_SNOWBLOWER_TOOL),
        int(C.UGC_SUPERSPADE_TOOL),
        int(C.JETPACK_UGCBUILDER),
        int(C.PAINTBRUSH_TOOL),
        int(C.UGC_PREFAB_TOOL),
        int(C.UGC_TOOL),
    }.issubset(selection.loadout)
    # InitialInfo disables the ordinary combat prefab tool in editor mode.
    assert int(C.PREFAB_TOOL) not in selection.loadout
    assert mode.allows_class_selection(SimpleNamespace(), selection) is True

    mixed = mode.prepare_join_selection(
        TEAM2,
        ClassSelection(
            class_id=int(C.CLASS_UGCBUILDER),
            loadout=(),
            prefabs=config.ugc_prefabs[:3],
            ugc_tools=(0, 1, 2, 3),
        ),
    )
    assert mixed.prefabs == config.ugc_prefabs[:3]
    assert mixed.ugc_tools == (0, 1)
    assert mode.allows_class_selection(SimpleNamespace(), mixed) is True

    overflow = ClassSelection(
        class_id=int(C.CLASS_UGCBUILDER),
        loadout=mixed.loadout,
        prefabs=config.ugc_prefabs[:3],
        ugc_tools=(0, 1, 2),
    )
    assert mode.allows_class_selection(SimpleNamespace(), overflow) is False


def test_editor_snapshot_exposes_complete_construct_catalog() -> None:
    """The native library receives every terrain-compatible construct."""

    project = UGCProject(
        title="Catalog",
        description="Catalog",
        author="Builder",
        baseplate="grassland",
        target_mode="tdm",
    )
    catalog = tuple(f"UGC_Prefab_Grassland_{index}" for index in range(373))
    config = SimpleNamespace(
        ugc_runtime=True,
        ugc_project=project,
        ugc_prefabs=catalog,
    )
    mode = UGCMode(SimpleNamespace(config=config))
    packet = SimpleNamespace()

    mode.configure_state_data(packet)

    assert packet.prefabs == list(catalog)
    assert len(packet.prefabs) == 373
    assert packet.team1_locked_class is False


def test_editor_forces_builder_team_without_skipping_prefab_menu() -> None:
    project = UGCProject(
        title="Menu",
        description="Menu",
        author="Builder",
        baseplate="grassland",
        target_mode="tdm",
    )
    config = SimpleNamespace(
        ugc_runtime=True,
        ugc_project=project,
        ugc_prefabs=(),
    )
    mode = UGCMode(SimpleNamespace(config=config))
    connection = _Connection()

    mode.send_post_state_data(connection)

    packet = ForceTeamJoin(ByteReader(connection.sent[0][1:]))
    assert packet.team_id == TEAM1
    assert packet.instant == 0


class _Connection:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.in_game = True

    def send(self, data, reliable=True) -> None:
        self.sent.append(bytes(data))


class _EditorServer:
    def __init__(self, project: UGCProject, tmp_path: Path) -> None:
        self.config = SimpleNamespace(
            ugc_runtime=True,
            ugc_project=project,
            ugc_sidecar_path=str(tmp_path / "map.ugc"),
            ugc_vxl_path=str(tmp_path / "map.vxl"),
            ugc_preview_path=str(tmp_path / "map.png"),
            ugc_prefabs=("ugc_prefab_primitive_block",),
        )
        self.connections: dict[int, _Connection] = {}
        self.teams = {
            TEAM1: SimpleNamespace(infinite_blocks=False),
            TEAM2: SimpleNamespace(infinite_blocks=False),
        }
        self.world_manager = SimpleNamespace(
            get_spawn_point=lambda team: (1, 2, 3),
            map_metadata=SimpleNamespace(skybox_name=None, ground_colors=[]),
            map_raw_bytes=(bytes(range(256)) * 32) + b"ugc-vxl-tail",
        )
        self.loop_count = 77
        self.broadcasts: list[bytes] = []

    def broadcast(self, data, **kwargs) -> None:
        self.broadcasts.append(bytes(data))


def test_mode_assigns_one_host_and_replays_large_object_batches(
    tmp_path: Path,
) -> None:
    project = UGCProject(
        title="Batch",
        description="Batch",
        author="Builder",
        baseplate="grassland",
        target_mode="tdm",
    )
    for index in range(513):
        project.place(
            index % 512,
            index // 512,
            220,
            int(C.UGC_ITEM_HEALTH_DROP_POINT),
        )
    server = _EditorServer(project, tmp_path)
    mode = UGCMode(server)
    shared_info = SimpleNamespace(
        map_name="Batch",
        filename="Batch",
    )
    mode.configure_initial_info(shared_info)
    host = _Connection()
    guest = _Connection()
    server.connections[1] = host
    host_info = SimpleNamespace()
    mode.configure_initial_info_for(host, host_info)
    server.connections[2] = guest
    guest_info = SimpleNamespace()
    mode.configure_initial_info_for(guest, guest_info)

    # Dedicated Map Creator owns the source files server-side. Advertising
    # HOST would make a direct-connect retail client dereference absent local
    # lobby UGC data instead of accepting the server's full MapSync.
    assert shared_info.map_name == "GrasslandBaseplate"
    assert shared_info.filename == "GrasslandBaseplate"
    assert host_info.map_is_ugc == int(C.MAP_IS_UGC_CLIENT)
    assert guest_info.map_is_ugc == int(C.MAP_IS_UGC_CLIENT)
    assert host.ugc_editor_owner is True
    assert guest.ugc_editor_owner is False

    mode.send_initial_batch(guest)
    batches = []
    for data in guest.sent:
        packet = InitialUGCBatch()
        packet.read(ByteReader(data[1:]))
        batches.append(packet)
    assert [len(batch.items) for batch in batches] == [512, 1]
    assert sum(len(batch.items) for batch in batches) == 513


def test_ugc_guest_receives_host_vxl_before_normal_map_sync(tmp_path: Path) -> None:
    project = UGCProject(
        title="Transfer",
        description="Transfer",
        author="Builder",
        baseplate="grassland",
        target_mode="tdm",
    )
    server = _EditorServer(project, tmp_path)
    mode = UGCMode(server)
    connection = _Connection()

    mode.send_pre_validation_map_data(connection)

    assert connection.sent[0] == b"\x36"  # MapDataStart(54)
    assert connection.sent[-1] == b"\x3a"  # MapDataEnd(58)
    chunks = []
    percentages = []
    for data in connection.sent[1:-1]:
        assert data[0] == 56
        chunk = MapDataChunk(ByteReader(data[1:]))
        chunks.append(bytes(chunk.data))
        percentages.append(int(chunk.percent_complete))
    assert percentages == sorted(percentages)
    assert percentages[-1] == 100
    assert zlib.decompress(b"".join(chunks)) == server.world_manager.map_raw_bytes


def test_host_object_mutation_echoes_and_updates_native_validation(
    tmp_path: Path,
) -> None:
    project = _complete_ctf_project()
    server = _EditorServer(project, tmp_path)
    mode = UGCMode(server)
    host = _Connection()
    server.connections[1] = host
    mode.configure_initial_info_for(host, SimpleNamespace())
    player = SimpleNamespace(connection=host, id=1, name="Host")

    assert mode.place_object(
        player,
        200,
        200,
        220,
        int(C.UGC_ITEM_HEALTH_DROP_POINT),
        True,
    ) is True
    assert server.broadcasts[-2][0] == 97
    objectives = UGCObjectives()
    objectives.read(ByteReader(server.broadcasts[-1][1:]))
    assert objectives.mode == mode_id("ctf")
    values = dict(zip(objectives.objective_ids, objectives.objective_values))
    assert values["UGC_OBJECTIVE_HEALTHCRATE_SPAWNS"] == 3

    assert mode.place_object(
        player,
        200,
        200,
        220,
        int(C.UGC_ITEM_HEALTH_DROP_POINT),
        False,
    ) is True
    assert project.validation().complete is True


def test_host_skybox_and_water_settings_replicate_and_persist(
    tmp_path: Path,
) -> None:
    project = _complete_ctf_project()
    server = _EditorServer(project, tmp_path)
    mode = UGCMode(server)
    host = _Connection()
    server.connections[1] = host
    mode.configure_initial_info_for(host, SimpleNamespace())
    player = SimpleNamespace(connection=host, id=1, name="Host")

    assert mode.set_skybox(player, "User_Desert.txt") is True
    assert project.skybox_name == "User_Desert.txt"
    assert server.world_manager.map_metadata.skybox_name == "User_Desert.txt"
    skybox = SkyboxData()
    skybox.read(ByteReader(server.broadcasts[-1][1:]))
    assert skybox.value == "User_Desert.txt"
    assert mode.set_skybox(player, "../../bad.dll") is False

    colors = [(1, 2, 3, 32), (4, 5, 6, 239)]
    assert mode.set_ground_colors(player, colors) is True
    assert project.ground_colors == colors
    palette = SetGroundColors()
    palette.read(ByteReader(server.broadcasts[-1][1:]))
    assert palette.ground_colors == colors
    assert mode.set_ground_colors(player, [(999, 2, 3, 4)]) is False


class _PrefabModel:
    def get_points(self):
        return (
            (0, 0, 0, 10, 20, 30),
            (1, 0, 0, 40, 50, 60),
        )


class _PrefabWorld:
    def __init__(self) -> None:
        self.solids = {(100, 100, 101)}
        self.colors: dict[tuple[int, int, int], tuple[int, int, int]] = {}

    def get_solid(self, x, y, z):
        return (int(x), int(y), int(z)) in self.solids

    def set_block(self, x, y, z, solid, color):
        coordinate = (int(x), int(y), int(z))
        if solid:
            self.solids.add(coordinate)
            self.colors[coordinate] = tuple(color)
        else:
            self.solids.discard(coordinate)
            self.colors.pop(coordinate, None)
        return True

    def get_color(self, x, y, z):
        return self.colors.get((int(x), int(y), int(z)), (0, 0, 0))

    def destroy_blocks(self, coordinates):
        removed = []
        for coordinate in coordinates:
            coordinate = tuple(coordinate)
            if coordinate in self.solids:
                self.solids.remove(coordinate)
                self.colors.pop(coordinate, None)
                removed.append(coordinate)
        return removed


def test_ugc_prefab_uses_raw_kv6_colors_and_native_build_erase_echoes(
    monkeypatch,
) -> None:
    from server import prefab_actions as prefab_actions_module

    registry = SimpleNamespace(get=lambda name: _PrefabModel())
    monkeypatch.setattr(prefab_actions_module.prefabs, "get_registry", lambda: registry)
    monkeypatch.setattr(prefab_actions_module.prefabs, "prefab_allowed", lambda p, n: True)

    world = _PrefabWorld()
    player = SimpleNamespace(
        id=3,
        name="Builder",
        team=TEAM1,
        alive=True,
        spawned=True,
        class_id=int(C.CLASS_UGCBUILDER),
        tool=int(C.UGC_PREFAB_TOOL),
        tool_is_raw=True,
        loadout=[int(C.UGC_PREFAB_TOOL)],
        prefabs=["ugc_prefab_primitive_block"],
        blocks=0,
        x=100.0,
        y=100.0,
        z=100.0,
        sent=[],
        relocated=[],
    )
    player.send = lambda data, reliable=True: player.sent.append(bytes(data))
    player.set_position = lambda x, y, z: (
        player.relocated.append((x, y, z)),
        setattr(player, "x", x),
        setattr(player, "y", y),
        setattr(player, "z", z),
    )
    server = SimpleNamespace(
        config=SimpleNamespace(ugc_runtime=True),
        world_manager=world,
        teams={TEAM1: SimpleNamespace(infinite_blocks=True)},
        players={player.id: player},
        loop_count=90,
        broadcasts=[],
    )
    server.broadcast = lambda data, **kwargs: server.broadcasts.append(bytes(data))
    service = PrefabActionService(server)

    build = BuildPrefabAction()
    build.loop_count = 80
    build.prefab_name = "ugc_prefab_primitive_block"
    build.player_id = player.id
    build.prefab_yaw = build.prefab_pitch = build.prefab_roll = 0
    build.from_block_index = build.to_block_index = 0
    build.position = (100, 100, 100)
    build.color = (200, 210, 220)
    build.add_to_user_blocks = False

    assert service.place_packet(player, build) is True
    assert world.colors[(100, 100, 100)] == (10, 20, 30)
    assert world.colors[(101, 100, 100)] == (40, 50, 60)
    assert player.relocated == [(100.5, 100.5, 96.75)]
    assert [packet[0] for packet in server.broadcasts if packet[0] == 30] == [30]
    assert sum(packet[0] == PlaySound.id for packet in server.broadcasts) == 1
    build_echo = next(packet for packet in server.broadcasts if packet[0] == 30)
    echoed_build = BuildPrefabAction()
    echoed_build.read(ByteReader(build_echo[1:]))
    assert echoed_build.position == (100, 100, 100)
    assert echoed_build.add_to_user_blocks is False
    assert (
        echoed_build.from_block_index,
        echoed_build.to_block_index,
    ) == (0, 2)
    assert player.sent[-1][0] == 29

    erase = ErasePrefabAction()
    erase.loop_count = 81
    erase.prefab_name = build.prefab_name
    erase.player_id = player.id
    erase.prefab_yaw = erase.prefab_pitch = erase.prefab_roll = 0
    erase.from_block_index = erase.to_block_index = 0
    erase.position = build.position

    assert service.erase_packet(player, erase) is True
    assert (100, 100, 100) not in world.solids
    assert (101, 100, 100) not in world.solids
    assert [
        packet[0] for packet in server.broadcasts
        if packet[0] in (30, 31)
    ] == [30, 31]
    erase_echo = next(packet for packet in server.broadcasts if packet[0] == 31)
    echoed_erase = ErasePrefabAction()
    echoed_erase.read(ByteReader(erase_echo[1:]))
    assert echoed_erase.position == (100, 100, 100)
    assert (
        echoed_erase.from_block_index,
        echoed_erase.to_block_index,
    ) == (0, 2)
    assert [packet[0] for packet in player.sent] == [29, 29]


def test_production_ugc_prefab_prepares_before_bounded_world_commit(
    monkeypatch,
) -> None:
    """Native KV6 decode/rotation must not execute in packet-drain context."""

    from concurrent.futures import Future
    from server import prefab_actions as prefab_actions_module

    registry = SimpleNamespace(get=lambda name: _PrefabModel())
    monkeypatch.setattr(prefab_actions_module.prefabs, "get_registry", lambda: registry)
    monkeypatch.setattr(prefab_actions_module.prefabs, "prefab_allowed", lambda p, n: True)

    class _ControlledExecutor:
        def __init__(self) -> None:
            self.future = Future()
            self.job = None

        def submit(self, function, *arguments):
            self.job = (function, arguments)
            return self.future

        def run(self):
            function, arguments = self.job
            try:
                self.future.set_result(function(*arguments))
            except Exception as exc:  # pragma: no cover - assertion aid
                self.future.set_exception(exc)

    world = _PrefabWorld()
    player = SimpleNamespace(
        id=4,
        name="AsyncBuilder",
        team=TEAM1,
        alive=True,
        spawned=True,
        class_id=int(C.CLASS_UGCBUILDER),
        tool=int(C.UGC_PREFAB_TOOL),
        tool_is_raw=True,
        loadout=[int(C.UGC_PREFAB_TOOL)],
        prefabs=["ugc_prefab_primitive_block"],
        blocks=0,
        x=90.0,
        y=90.0,
        z=90.0,
        sent=[],
        send=lambda data, reliable=True: player.sent.append(bytes(data)),
        set_position=lambda *_args: None,
    )
    server = SimpleNamespace(
        config=SimpleNamespace(
            ugc_runtime=True,
            prefab_queue_limit=8,
            prefab_cell_batch_limit=8,
            prefab_validation_batch_limit=8,
        ),
        simulation_runtime=object(),
        world_manager=world,
        teams={TEAM1: SimpleNamespace(infinite_blocks=True)},
        players={player.id: player},
        loop_count=91,
        broadcasts=[],
    )
    server.broadcast = lambda data, **kwargs: server.broadcasts.append(bytes(data))
    service = PrefabActionService(server)
    executor = _ControlledExecutor()
    service._executor = executor

    build = BuildPrefabAction()
    build.loop_count = 90
    build.prefab_name = "ugc_prefab_primitive_block"
    build.player_id = player.id
    build.prefab_yaw = build.prefab_pitch = build.prefab_roll = 0
    build.from_block_index = build.to_block_index = 0
    build.position = (100, 100, 100)
    build.color = (200, 210, 220)
    build.add_to_user_blocks = False

    assert service.place_packet(player, build) is True
    assert service.pending_count == 1
    assert world.colors == {}
    assert server.broadcasts == []

    # The packet handler only submitted immutable work.  A real executor runs
    # this job on its private worker; the controlled test advances it here.
    assert executor.job is not None
    assert executor.future.done() is False
    executor.run()
    assert service.tick() == 2
    assert service.pending_count == 0
    assert world.colors[(100, 100, 100)] == (10, 20, 30)
    assert world.colors[(101, 100, 100)] == (40, 50, 60)
    assert [payload[0] for payload in server.broadcasts if payload[0] == 30] == [30]
    assert sum(payload[0] == PlaySound.id for payload in server.broadcasts) == 1
    echoed_build = BuildPrefabAction()
    echoed_build.read(ByteReader(server.broadcasts[0][1:]))
    assert (
        echoed_build.from_block_index,
        echoed_build.to_block_index,
    ) == (0, 2)
    assert [payload[0] for payload in player.sent] == [29]


def test_ugc_paintbrush_and_projectile_aliases_are_authoritative() -> None:
    config = ServerConfig()
    config.ugc_runtime = True
    world = _PrefabWorld()
    world.solids.add((101, 100, 100))
    server = SimpleNamespace(config=config, world_manager=world, loop_count=12)
    server.broadcasts = []
    server.broadcast = lambda data, **kwargs: server.broadcasts.append(bytes(data))
    player = SimpleNamespace(
        id=5,
        alive=True,
        spawned=True,
        class_id=int(C.CLASS_UGCBUILDER),
        tool=int(C.PAINTBRUSH_TOOL),
        tool_is_raw=True,
        loadout=[int(C.PAINTBRUSH_TOOL)],
        x=100.0,
        y=100.0,
        z=100.0,
        is_block_tool=lambda: False,
    )
    paint = SimpleNamespace(
        loop_count=11,
        x=101,
        y=100,
        z=100,
        color=(7, 8, 9),
    )

    asyncio.run(handle_paint_block(server, player, paint))

    assert world.colors[(101, 100, 100)] == (7, 8, 9)
    assert server.broadcasts[-1][0] == 7
    echoed = PaintBlockPacket(ByteReader(server.broadcasts[-1][1:]))
    assert echoed.loop_count == 11
    drill = PROJECTILE_SPECS[int(C.UGC_DRILLGUN_TOOL)]
    cannon = PROJECTILE_SPECS[int(C.UGC_SNOWBLOWER_TOOL)]
    assert (drill.entity_type, drill.damage_type, drill.kill_type) == (
        int(C.DRILL_ENTITY),
        int(C.UGC_DRILL_DAMAGE),
        int(C.UGC_DRILL_KILL),
    )
    assert (cannon.entity_type, cannon.damage_type, cannon.kill_type) == (
        int(C.SNOWBALL_ENTITY),
        int(C.UGC_SNOWBALL_DAMAGE),
        int(C.UGC_SNOWBALL_KILL),
    )

    # The retail UGCSnowBlowerWeapon.use_an_ammo method is a no-op.  It is
    # capacity-limited by BlockManager, not by the class's one-block wallet.
    stock = Player.__new__(Player)
    stock.blocks = 0
    stock._oriented_next_use = {}
    stock.oriented_stock = {}
    assert Player.consume_oriented_item(stock, C.UGC_SNOWBLOWER_TOOL, now=1.0)
    assert stock.blocks == 0


def test_dedicated_ugc_clientdata_drives_single_block_paint() -> None:
    """Direct UGC clients retain held input even when packet 7 is host-local."""

    from server.combat_runtime import get_combat_system

    config = ServerConfig()
    config.ugc_runtime = True
    world = _PrefabWorld()
    target = (101, 100, 100)
    world.solids.add(target)
    world.colors[target] = (1, 2, 3)
    world.raycast = lambda *_args: target
    server = SimpleNamespace(
        config=config,
        world_manager=world,
        loop_count=20,
        broadcasts=[],
    )
    server.broadcast = lambda data, **kwargs: server.broadcasts.append(bytes(data))
    player = SimpleNamespace(
        id=7,
        alive=True,
        spawned=True,
        class_id=int(C.CLASS_UGCBUILDER),
        tool=int(C.PAINTBRUSH_TOOL),
        tool_is_raw=True,
        loadout=[int(C.PAINTBRUSH_TOOL)],
        block_color=0x0A141E,
        eye=(100.0, 100.0, 100.0),
        orientation=(1.0, 0.0, 0.0),
    )
    packet = SimpleNamespace(
        loop_count=19,
        primary=True,
        secondary=False,
        # PaintbrushTool.on_set keeps the editor palette active, so retail
        # ClientData legitimately carries the high-bit palette flag while a
        # stroke is held.  The HUD consumes palette clicks before they become
        # primary/secondary action bits; this flag must not suppress painting.
        palette_enabled=True,
    )

    assert get_combat_system(server).handle_paintbrush_input(player, packet)
    assert world.colors[target] == (10, 20, 30)
    echoed = PaintBlockPacket(ByteReader(server.broadcasts[-1][1:]))
    assert echoed.loop_count == 19
    assert (echoed.x, echoed.y, echoed.z) == target
    assert echoed.color == (10, 20, 30)


def test_map_creator_config_selects_project_and_portable_save_root(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.from_root(tmp_path)
    paths.config.write_text(
        "[map_creator]\n"
        'project = "CommunityBuild"\n'
        'output_dir = "editor-saves"\n'
        'terrain = "urban"\n'
        'target_mode = "ctf"\n'
        'retail_root = "retail-client"\n',
        encoding="utf-8",
    )
    arguments = apply_map_creator_config(build_parser().parse_args([]), paths)

    assert arguments.project == "CommunityBuild"
    assert arguments.output_dir == "editor-saves"
    assert arguments.terrain == "urban"
    assert arguments.target_mode == "ctf"
    assert Path(arguments.retail_root) == (tmp_path / "retail-client").resolve()
