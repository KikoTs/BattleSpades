"""Shared round, stock presentation, inventory and terrain contracts.

Living players are repositioned/restocked using CreatePlayer without KillAction.
Teams are stable: asymmetric modes rotate roles, never kill players to recolour
them. Timers use a monotonic clock and no tick hook sleeps.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from collections import deque
from typing import TYPE_CHECKING

import shared.constants as C
from modes.base_mode import BaseMode
from server.game_constants import TEAM1, TEAM2, TEAM_NEUTRAL
from server import hud_packets
from .catalog import CATALOG

if TYPE_CHECKING:
    from server.player import Player

TEAMS = (TEAM1, TEAM2)
Vec = tuple[float, float, float]
Cell = tuple[int, int, int]


@dataclass(eq=False)
class KitPreview:
    class_id: int
    team: int


def distance(a: Vec, b: Vec) -> float:
    return math.dist(a, b)


class FunMode(BaseMode):
    """Finite community matches with bounded resets and explicit role rules."""
    unofficial = True
    generic_scoring_enabled = False
    escape_watch_enabled = False
    shoot_with_intel = True
    mode_code = ""
    class_id = int(C.CLASS_ENGINEER)
    supports_bots = True
    map_pickups_enabled = False
    clock = staticmethod(time.monotonic)

    def __init__(self, server):
        super().__init__(server)
        self.spec = CATALOG[self.mode_code]
        self.name = f"[Unofficial] {self.spec.title}"
        self.description = self.spec.description
        self.stock_maps = (self.spec.map_name,)
        self.settings = dict(getattr(server.config, "mode_settings", {}).get(self.mode_code, {}))
        self.duration = self.number("round_time", self.spec.duration, 20, 1800)
        self.preparation = self.number("prepare_time", self.spec.preparation, 0, 600)
        self.rounds = int(self.number("rounds", 4, 1, 20))
        if self.spec.asymmetric and self.rounds % 2:
            self.rounds += 1
        self.score_limit = self.rounds
        self.time_limit = 0
        self.phase = "waiting"
        self.deadline = 0.0
        self.round_number = 0
        self.participants: set[Player] = set()
        self.eliminated: set[Player] = set()
        self.points: dict[object, int] = {}
        self.match_points: dict[object, int] = {}
        self.last_result: tuple[object, ...] = ()
        self.data: dict = {}
        self.anchors: dict[str, Vec] = {}
        self.markers: set[int] = set()
        self._zones: dict[int, tuple] = {}
        self._entities: set[int] = set()
        self._dirty: set[Cell] = set()
        self._restore: deque[Cell] = deque()
        self._subscription: int | None = None
        self._baseline = None
        self._last_tick = 0.0
        self._notified: set[Player] = set()
        self._arena_map = None
        self._timer_at = 0.0

    def number(self, key: str, default: float, low: float, high: float) -> float:
        value = float(self.settings.get(key, default))
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"modes.{self.mode_code}.{key} must be in [{low}, {high}]")
        return value

    @property
    def attackers(self) -> int:
        return TEAMS[(max(1, self.round_number) - 1) % 2]

    @property
    def defenders(self) -> int:
        return TEAM2 if self.attackers == TEAM1 else TEAM1

    def players(self, *, alive: bool = False) -> list[Player]:
        return [p for p in self.server.players.values() if p.team in TEAMS
                and (not alive or (p.alive and p.spawned))]

    def active(self, player: Player) -> bool:
        return (self.phase == "live" and not self.ended and not self.retiring
                and player in self.participants and player not in self.eliminated
                and self.server.players.get(player.id) is player
                and player.alive and player.spawned)

    def alive(self, team: int | None = None) -> list[Player]:
        return [p for p in self.players(alive=True) if self.active(p)
                and (team is None or p.team == team)]

    def ready(self) -> bool:
        players = self.players()
        if not any(not player.is_bot for player in players):
            return False
        if self.spec.solo:
            return len(players) >= (1 if self.mode_code in ("crumbling_run", "red_light_raid") else 2)
        return all(any(p.team == team for p in players) for team in TEAMS)

    async def on_mode_start(self) -> None:
        await self.restore_all()
        self.cleanup()
        await super().on_mode_start()
        self.phase = "waiting"
        self.round_number = 0
        self.participants.clear()
        self.eliminated.clear()
        self.points.clear()
        self.match_points.clear()
        self.last_result = ()
        self.deadline = 0.0
        self._last_tick = self.clock()
        self._timer_at = 0.0
        self._notified.clear()
        self._load_arena()
        from aoslib.vxl import VXL
        self._baseline = VXL(-1, self.server.world_manager.map.generate_vxl(False), 0, 2)
        self._arena_map = self.server.world_manager.map
        self._dirty.clear()
        self._subscription = self.server.world_manager.subscribe_mutations(self._changed)
        await self.broadcast_message(f"{self.name}: {self.description}")
        await self.broadcast_message("/funrules explains the mode; /funscore shows standings.")

    def _load_arena(self) -> None:
        wm = self.server.world_manager
        self.data = dict(getattr(wm.map_metadata, "unofficial", {}) or {})
        self.data.update(self.settings.get("arena", {}))
        self.anchors.clear()
        authored = self.data.get("anchors", {})
        for key, team in (("blue", TEAM1), ("green", TEAM2)):
            self.anchors[key] = self.valid_position(authored[key]) if key in authored else wm.get_spawn_point(team)
        blue, green = self.anchors["blue"], self.anchors["green"]
        mid = tuple((a + b) / 2 for a, b in zip(blue, green))
        self.anchors["center"] = self.valid_position(authored["center"]) if "center" in authored else wm.dry_ground_anchor(mid[0], mid[1])
        for key, value in authored.items():
            self.anchors[str(key)] = self.valid_position(value)
        required = {"crumbling_run": "course", "red_light_raid": "course",
                    "floor_tax": "floors", "tunnel_rats": "tunnels",
                    "bridge_chicken": "bridge", "towerfall": "towers"}.get(self.mode_code)
        if required and self.data.get("layout") != required:
            raise ValueError(f"{self.spec.title} requires a {required} arena; generate/select {self.spec.map_name}. See docs/UNOFFICIAL_MODES.md")

    @staticmethod
    def valid_position(value) -> Vec:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError("arena positions require [x, y, z]")
        result = tuple(float(v) for v in value)
        if not all(math.isfinite(v) and 0 <= v < limit for v, limit in zip(result, (512, 512, 238))):
            raise ValueError("arena position outside playable VXL bounds")
        return result

    def anchor(self, name: str) -> Vec:
        return self.anchors.get(name, self.anchors["center"])

    def home(self, team: int) -> Vec:
        return self.anchor("blue" if team == TEAM1 else "green")

    def get_spawn_point(self, player: Player) -> Vec:
        if not self.anchors:
            return self.server.world_manager.get_spawn_point(player.team)
        point = self.home(player.team)
        roster = sorted((p for p in self.players() if p.team == player.team), key=lambda p: p.id)
        rank = roster.index(player) if player in roster else 0
        offsets = sorted(((x * 1.5, y * 1.5) for x in range(-2, 3) for y in range(-2, 3)),
                         key=lambda v: (v[0] ** 2 + v[1] ** 2, v))
        dx, dy = offsets[rank % len(offsets)]
        return point[0] + dx, point[1] + dy, point[2]

    def prepare_player_spawn(self, player: Player) -> None:
        from server.class_selection import ClassSelection
        player.pending_selection = None
        player.pending_class_id = player.pending_loadout = None
        player.apply_class_selection(ClassSelection(class_id=self.player_class(player), loadout=self.loadout(player)))
        # Player.spawn equips its primary weapon; keep that field aligned
        # with the selected gun. Melee-only lives select their tool below.
        from server.game_constants import WEAPON_PROFILES
        for tool in reversed(player.loadout):
            if tool in WEAPON_PROFILES:
                player.set_tool(tool, raw=True)
                break

    def loadout(self, player: Player) -> tuple[int, ...]:
        from .catalog import RETIRED
        if self.mode_code in RETIRED:
            return (int(C.BLOCK_TOOL), int(C.PICKAXE_TOOL), int(C.SMG_TOOL))
        kits = {
            C.CLASS_SOLDIER: (C.ASSAULT_RIFLE_TOOL, C.GRENADE_TOOL),
            C.CLASS_SCOUT: (C.PISTOL_TOOL, C.SNIPER_TOOL, C.GRENADE_TOOL),
            C.CLASS_MINER: (C.SHOTGUN_TOOL, C.GRENADE_TOOL),
            C.CLASS_MEDIC: (C.SMG_TOOL, C.MEDPACK_TOOL, C.GRENADE_TOOL),
        }
        return (int(C.BLOCK_TOOL), int(C.SPADE_TOOL), *map(int, kits.get(self.player_class(player), (C.SMG_TOOL, C.GRENADE_TOOL))))

    def roster(self) -> tuple[int, ...]:
        if self.mode_code in {"night_shift", "last_exit", "towerfall", "kingmaker"}:
            return (C.CLASS_SOLDIER, C.CLASS_SCOUT, C.CLASS_MINER, C.CLASS_ENGINEER, C.CLASS_MEDIC)
        return (self.class_id,)

    def player_class(self, player: Player) -> int:
        return player.class_id if player.class_id in self.roster() else self.roster()[0]

    def prepare_join_selection(self, team, selection):
        from server.class_selection import ClassSelection
        player = KitPreview(class_id=selection.class_id, team=team)
        return ClassSelection(self.player_class(player), self.loadout(player))

    def allows_class_selection(self, player, selection) -> bool:
        return player.team in TEAMS and selection.class_id in self.roster()

    def equip(self, player: Player, loadout: tuple[int, ...]) -> None:
        """Retail instant packet 13, already used by the tutorial grants."""
        from server.class_selection import ClassSelection
        from shared.packet import SetClassLoadout
        player.apply_class_selection(ClassSelection(class_id=self.player_class(player), loadout=loadout))
        player.set_tool(self.starting_tool(loadout), raw=True)
        packet = SetClassLoadout()
        packet.player_id, packet.class_id, packet.instant = player.id, player.class_id, 1
        packet.loadout, packet.prefabs, packet.ugc_tools = list(loadout), [], []
        self.server.broadcast(bytes(packet.generate()), reliable=True)

    def reset_player(self, player: Player, position: Vec | None = None) -> None:
        """Create a fresh body without manufacturing a death or a kill."""
        self.server.round_lifecycle.remove_owned_deployables(player)
        self.server.respawn_player(player)
        player.set_tool(self.starting_tool(self.loadout(player)), raw=True)
        if position is not None:
            self.teleport(player, position)

    @staticmethod
    def starting_tool(loadout: tuple[int, ...]) -> int:
        from server.game_constants import WEAPON_PROFILES
        return next((tool for tool in reversed(loadout) if tool in WEAPON_PROFILES), loadout[0])

    def teleport(self, player: Player, position: Vec) -> None:
        player.set_position(*position)
        player.velocity = (0.0, 0.0, 0.0)
        # Drop old hitbox history across a discontinuous movement boundary.
        player.input_history.clear()
        from server import lag_compensation
        lag_compensation.forget_player(player)

    async def on_player_spawn(self, player: Player) -> None:
        player.ensure_legal_tool()
        if self.phase in ("prepare", "live") and player not in self.participants:
            self.teleport(player, self.anchor("waiting"))
        if getattr(player.connection, "in_game", False):
            await self.on_player_ready(player)

    async def on_player_ready(self, player: Player) -> None:
        if player.is_bot or player in self._notified:
            return
        if self.server.players.get(player.id) is not player:
            return
        self._notified.add(player)
        await self.show_rules(player)

    async def show_rules(self, player: Player) -> None:
        from .guides import instructions
        from server.announcements import build_overlay_message
        import textwrap
        self._notified.add(player)
        player.send(build_overlay_message(f"{self.spec.title}: /funrules for your objective"))
        for line in instructions(self, player):
            for part in textwrap.wrap(line, width=88):
                await self.tell(player, part)

    async def on_player_join(self, player: Player) -> None:
        if self.can_join_round(player):
            self.participants.add(player)
        await self.on_player_spawn(player)

    def can_join_round(self, player: Player) -> bool:
        """Admit fresh lives during prep, or humans in respawning live modes."""
        return (not self.ended and not self.retiring and player.team in TEAMS
                and player not in self.participants and player not in self.eliminated
                and (self.phase == "prepare" or
                     (self.phase == "live" and not player.is_bot and not self.spec.elimination)))

    async def on_player_leave(self, player: Player) -> None:
        self.participants.discard(player)
        self.eliminated.discard(player)
        self._notified.discard(player)
        self.points.pop(player, None)
        self.match_points.pop(player, None)

    async def on_player_death(self, player: Player, killer: Player | None, kill_type: int) -> None:
        if self.phase == "live" and player in self.participants and self.spec.elimination:
            self.eliminated.add(player)

    def allows_team_change(self, player: Player, new_team: int) -> bool:
        return self.phase == "waiting"

    def allows_bot_join(self, team: int | None = None) -> bool:
        return self.supports_bots

    def prepare_bot_selection(self, team, selection, **kwargs):
        return self.prepare_join_selection(team, selection)

    def bot_team_anchor(self, team: int) -> Vec:
        return self.home(team)

    def bot_retire_safe(self, player: Player) -> bool:
        # Ordinary backfill changes wait for intermission. Human slot requests
        # can still force retirement through the director's existing path.
        return self.phase not in ("prepare", "live") or player not in self.participants

    def bot_snapshot(self):
        from .bot_support import snapshot
        return snapshot(self) if self.anchors else None

    async def on_bot_command(self, player: Player, command: str, position=None) -> None:
        """Use the same rules as human commands, rechecking queued work."""
        if not player.is_bot or self.server.players.get(player.id) is not player:
            return
        if command == "stash" and self.mode_code == "false_wall":
            await self.stash(player)
        elif command == "pass" and self.mode_code == "spadeball" and self.active(player):
            if position is not None:
                delta = tuple(b - a for a, b in zip(player.position, position))
                length = math.sqrt(sum(v * v for v in delta))
                if 0 < length <= 12:
                    await self.handle_drop_pickup(player, player.position,
                                                  tuple(v / length * min(30, length / .18) for v in delta))

    def can_player_respawn(self, player: Player) -> bool:
        if self.ended or self.retiring or self.phase in ("over", "restoring"):
            return False
        if self.phase != "live":
            return True
        if self.can_join_round(player):
            return True
        return (player in self.participants and player not in self.eliminated
                and not self.spec.elimination)

    def respawn_time_for(self, player: Player) -> float:
        return 3.0 if self.can_player_respawn(player) else min(255.0, max(1.0, self.deadline - self.clock() + 8))

    def players_are_enemies(self, a: Player, b: Player) -> bool:
        return a is not b and (self.spec.solo or a.team != b.team)

    def modify_incoming_damage(self, player: Player, amount: float, source: Player | None, kill_type: int) -> float:
        if not self.active(player) or (source is not None and not self.active(source)):
            return 0
        if source is not None and source is not player and not self.players_are_enemies(source, player):
            return 0
        return amount

    def allows_equipped_tool(self, player: Player, tool: int) -> bool:
        return tool in self.loadout(player)

    def allow_action(self, player: Player, action: str, position=None) -> bool:
        if self.ended or self.retiring or player not in self.participants or player in self.eliminated:
            return False
        if action == "build":
            return self.spec.building and self.phase in ("prepare", "live")
        if action == "paint":
            return self.phase in ("prepare", "live") and (self.spec.building or self.mode_code == "paint_raid")
        if action == "dig":
            return self.spec.digging and self.phase == "live"
        if action == "shoot":
            return self.active(player)
        if action in ("grenade", "medpack"):
            return self.active(player)
        return False  # No unadvertised grenades, gadgets or prefab bypasses.

    def configure_initial_info(self, packet) -> None:
        packet.disabled_classes = [i for i in range(int(C.CLASS_NOOF)) if i not in self.roster()]
        packet.custom_game_rules.append(("BATTLESPADES_FUN", self.mode_code))
        packet.enable_minimap = 1
        # Fixed kits are advertised through the real class menu, not granted
        # only after its stock loadout has already been displayed.
        if len(self.roster()) > 1:
            packet.loadout_overrides = {}
            for cid in self.roster():
                kit = self.loadout(KitPreview(class_id=cid, team=TEAM1))
                for slot in range(7):
                    packet.loadout_overrides[(cid, slot)] = list(kit) if slot == C.CLASS_COMMON else []
        packet.allow_shooting_holding_intel = 1
        if self.spec.solo:
            packet.friendly_fire = 1

    def configure_state_data(self, packet) -> None:
        packet.team1_classes = packet.team2_classes = list(self.roster())
        packet.team1_locked_class = packet.team2_locked_class = len(self.roster()) == 1
        packet.team1_show_max_score = packet.team2_show_max_score = False
        if self.spec.solo:
            packet.team1_show_score = packet.team2_show_score = False

    async def tell(self, player: Player, message: str) -> None:
        from commands.command_handler import send_message
        await send_message(self.server, player, message)

    def marker(self, index: int, position: Vec, *, team: int | None = None, icon: str = "minimap_base", radius: float = 2) -> None:
        from modes.objective_zones import around, minimap_zone_packet, minimap_zone_clear_packet
        import shared.constants_gamemode as CG
        color = self.server.teams[team].color if team in TEAMS else (245, 190, 55)
        zone = around(index, position, radius_xy=radius, height_above=2, depth_below=2)
        old = self._zones.get(index)
        if old and old[0].bounds != zone.bounds:
            self.server.broadcast(bytes(minimap_zone_clear_packet(old[0]).generate()), reliable=True)
        icon_id = CG.ZONE_ICON_SPAWN if icon == "minimap_spawn" else CG.ZONE_ICON_TERRITORY_A + index % 7
        packet = minimap_zone_packet(zone, color=color, icon_id=icon_id, icon_scale=.35)
        self._zones[index] = (zone, bytes(packet.generate()))
        if not old or old[1] != self._zones[index][1]:
            self.server.broadcast(self._zones[index][1], reliable=True)

    def reveal_to(self, connection) -> None:
        super().reveal_to(connection)
        for _, packet in self._zones.values():
            connection.send(packet, reliable=True)

    def moving_marker(self, index: int, position: Vec, icon: str = "map_bomb_16") -> None:
        ident = hud_packets.BILLBOARD_ID_BASE + index
        self.markers.add(ident)
        hud_packets.add_billboard(self.server, ident, index, icon, position)

    def clear_markers(self) -> None:
        from modes.objective_zones import minimap_zone_clear_packet
        for zone, _ in self._zones.values():
            self.server.broadcast(bytes(minimap_zone_clear_packet(zone).generate()), reliable=True)
        self._zones.clear()
        for ident in self.markers:
            hud_packets.remove_billboard(self.server, ident)
        self.markers.clear()

    def remove_marker(self, index: int) -> None:
        from modes.objective_zones import minimap_zone_clear_packet
        old = self._zones.pop(index, None)
        if old:
            self.server.broadcast(bytes(minimap_zone_clear_packet(old[0]).generate()), reliable=True)

    def award(self, owner, amount: int = 1) -> None:
        self.points[owner] = self.points.get(owner, 0) + int(amount)
        from server.scoreboard import send_player_score, send_team_score
        if owner in TEAMS:
            self.server.teams[owner].score = self.points[owner]
            send_team_score(self.server, self.server.teams[owner])
        elif self.server.players.get(owner.id) is owner:
            owner.score = self.points[owner]
            send_player_score(self.server, owner)

    async def finish(self, winners: tuple = (), message: str = "") -> None:
        if self.phase != "live" or self.ended or self.retiring:
            return
        self.phase = "over"
        self.server.world_mutations.cancel_all()
        self.deadline = self.clock() + 6.0
        self.last_result = tuple(winners)
        if getattr(self, "compare_deliveries", False):
            self.match_points[self.attackers] = self.match_points.get(self.attackers, 0) + self.points.get(self.attackers, 0)
        else:
            for winner in winners:
                self.match_points[winner] = self.match_points.get(winner, 0) + 1
        names = ", ".join(self.label(w) for w in winners) if winners else "Draw"
        await self.broadcast_message(f"{message + ' ' if message else ''}Round {self.round_number}: {names}.")

    def label(self, owner) -> str:
        return ("Blue" if owner == TEAM1 else "Green") if owner in TEAMS else owner.name

    def leaders(self, scores: dict | None = None) -> tuple:
        scores = self.points if scores is None else scores
        owners = list(self.participants) if self.spec.solo else list(TEAMS)
        if not owners:
            return ()
        best = max(scores.get(owner, 0) for owner in owners)
        leaders = tuple(owner for owner in owners if scores.get(owner, 0) == best)
        return leaders if len(leaders) == 1 else ()

    async def timeout(self) -> None:
        await self.finish(self.leaders(), "Time!")

    async def prepare_round(self) -> None:
        """Called once before players are refreshed and preparation starts."""

    async def begin_round(self) -> None:
        """Called once when the protected preparation phase ends."""

    async def tick_live(self, now: float, dt: float) -> None:
        """Mode-specific objective simulation."""

    async def _new_round(self) -> None:
        self.clear_round_objects()
        self.round_number += 1
        self.participants = set(self.players())
        self.eliminated.clear()
        self.points.clear()
        from server.scoreboard import send_player_score, send_team_score
        for team in self.server.teams.values():
            team.score = 0
            send_team_score(self.server, team)
        for player in self.players():
            player.score = 0
            send_player_score(self.server, player)
        self.phase = "prepare"
        await self.prepare_round()
        for player in self.players():
            self.reset_player(player)
        self.deadline = self.clock() + self.preparation
        self._timer_at = 0
        await self.broadcast_message(f"Round {self.round_number}/{self.rounds}: {int(self.preparation)}s preparation.")
        if self.spec.asymmetric:
            await self.broadcast_message(f"{self.label(self.attackers)}: raiders/hunters. {self.label(self.defenders)}: hiding/defending.")
        for player in self.players():
            if not player.is_bot and getattr(player.connection, "in_game", False):
                await self.show_rules(player)

    async def on_tick(self, tick: int) -> None:
        if self.ended or self.retiring:
            return
        now = self.clock()
        dt = min(0.25, max(0.0, now - self._last_tick))
        self._last_tick = now
        if now >= self._timer_at:
            from server.scoreboard import send_round_timer
            remaining = self.deadline - now if self.phase in ("prepare", "live", "over") else 0
            send_round_timer(self.server, remaining, reliable=False)
            self._timer_at = now + 1
        for player in self.players(alive=True):
            if (player not in self.participants or player in self.eliminated) and self.phase != "waiting":
                if distance(player.position, self.anchor("waiting")) > 5:
                    self.teleport(player, self.anchor("waiting"))
        if self.phase == "waiting":
            if self.ready():
                await self._new_round()
            return
        if self.phase == "restoring":
            self.restore_slice()
            if not self._restore:
                if self.round_number >= self.rounds:
                    winners = self.leaders(self.match_points)
                    from server.scoreboard import send_team_score, send_player_score
                    for team in self.server.teams.values():
                        team.score = self.match_points.get(team.id, 0)
                        send_team_score(self.server, team)
                    if self.spec.solo:
                        for player in self.players():
                            player.score = self.match_points.get(player, 0)
                            send_player_score(self.server, player)
                    if winners:
                        await self.broadcast_message(f"Match winner: {self.label(winners[0])}")
                    await self.on_mode_end(winners[0] if winners and not self.spec.solo else None)
                elif self.ready():
                    await self._new_round()
                else:
                    self.phase = "waiting"
            return
        if self.phase == "over":
            if now >= self.deadline:
                self.clear_round_objects()
                self.server.world_mutations.cancel_all()
                self._queue_restoration()
                self.phase = "restoring"
            return
        if self.phase == "prepare":
            if now >= self.deadline:
                self.phase = "live"
                self.deadline = now + self.duration
                self._timer_at = 0
                for player in self.players(alive=True):
                    if player in self.participants:
                        player.restock_ammo()
                await self.begin_round()
                await self.broadcast_message("GO! " + self.description)
            return
        if not any(p in self.participants for p in self.players()):
            await self.finish((), "No participants remain.")
            return
        await self.tick_live(now, dt)
        if self.phase == "live" and now >= self.deadline:
            await self.timeout()

    def _changed(self, x: int, y: int, z: int, solid: bool, color: int, version: int) -> None:
        if self.phase != "restoring":
            self._dirty.add((x, y, z))

    def set_cell(self, cell: Cell, solid: bool, color: int = 0x909090) -> None:
        """Use canonical terrain state and its proven packet repair encoder."""
        wm = self.server.world_manager
        if wm.set_block(*cell, solid, color):
            # Packet 33 indexes a Character. An arbitrary actor (including
            # id zero) may be absent during roster skew or a disconnect.
            # Match TerrainRepairService's per-recipient local-id contract.
            # WorldManager's mutation journal already covers loading peers.
            for connection in tuple(self.server.connections.values()):
                player = getattr(connection, "player", None)
                if not getattr(connection, "in_game", False) or player is None:
                    continue
                for packet in self.server.terrain_repair.canonical_packets(cell, player.id):
                    connection.send(packet, reliable=True)

    def restore_slice(self) -> None:
        for _ in range(min(128, len(self._restore))):
            cell = self._restore.popleft()
            solid = self._baseline.get_solid(*cell)
            color = self._baseline.get_color(*cell) if solid else 0
            self.server.world_manager.clear_block_damage(*cell)
            self.server.world_manager.block_health.pop(cell, None)
            self.set_cell(cell, bool(solid), color or 0)
            self._dirty.discard(cell)

    def clear_round_objects(self) -> None:
        self.clear_markers()
        for ident in tuple(self._entities):
            if self.server.entity_registry.remove(ident) is not None:
                self.server.broadcast_destroy_entity(ident)
        self._entities.clear()

    def cleanup(self) -> None:
        if self._subscription is not None:
            self.server.world_manager.unsubscribe_mutations(self._subscription)
            self._subscription = None
        self.clear_round_objects()

    async def restore_all(self) -> None:
        # Admin restarts and same-map mode changes must not adopt a damaged
        # arena as their new baseline. Never write an old baseline to a new map.
        if self._baseline is not None and self.server.world_manager.map is self._arena_map:
            self.phase = "restoring"
            self.server.world_mutations.cancel_all()
            self._queue_restoration()
            while self._restore:
                self.restore_slice()
                await asyncio.sleep(0)

    def _queue_restoration(self) -> None:
        # Partial hits have no topology mutation, but their health and shade
        # must also reset before the next round's cores can be attacked.
        cells = self._dirty.union(self.server.world_manager.block_damage)
        self._restore = deque(sorted(cells, key=lambda c: -c[2]))

    async def deactivate(self) -> None:
        await self.restore_all()
        self.cleanup()
        await super().deactivate()
