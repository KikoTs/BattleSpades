"""Full-roster adversarial setups for the production gameplay audit.

Only initial positions, terrain and mode roles are controlled. Afterwards all
bots use the real director, perception, actions, physics and mode lifecycle.
"""
from __future__ import annotations

from collections import Counter
import math
import random
from types import SimpleNamespace

import shared.constants as C
from server.class_selection import normalize_class_selection
from server.game_constants import DEFAULT_WEAPON_TOOL, TEAM1, TEAM2
from server.player import Player

TARGET_SCENARIOS = ("rooftop", "hidden", "water", "buried", "highroof",
                    "skybridge", "stacked", "moving", "repairing")


class ScenarioConnection:
    """Local connected participant; no public network traffic is emitted."""

    def __init__(self, server) -> None:
        self.server = server
        self.player: Player | None = None
        self.in_game = True
        self.scenario_participant = True
        self._transition_identity: tuple[int, str] | None = None

    def send(self, *args, **kwargs) -> None:
        pass

    def send_packet(self, *args, **kwargs) -> None:
        pass

    def disconnect(self, reason: int = 0) -> None:
        self.in_game = False

    def arm_scene_transition(self) -> None:
        """Retain identity before the real transition retires the old body."""
        if self.player is not None:
            self._transition_identity = int(self.player.team), str(self.player.name)
        self.in_game = False

    async def reload_scene(self) -> bool:
        """Rejoin through mode hooks after an in-process scene replacement.

        This participant has no renderer or map-transfer socket. The production
        transition still owns retirement, world replacement and fresh spawning.
        """
        if self._transition_identity is None:
            return False
        team, name = self._transition_identity
        self.scenario_flags = (False,) * 8
        self.in_game = True
        await _join_human(self, team, name)
        self._transition_identity = None
        return True


async def add_human(server, team: int, name: str) -> Player:
    """Join through the ordinary mode hooks with a real native player body."""
    connection = ScenarioConnection(server)
    return await _join_human(connection, team, name)


async def _join_human(connection: ScenarioConnection, team: int, name: str) -> Player:
    server = connection.server
    assign_team = getattr(server.mode, "prepare_join_team", None)
    if callable(assign_team):
        team = assign_team(team)
    player = Player(server.get_next_player_id(), name, team, DEFAULT_WEAPON_TOOL, connection)
    connection.player = player
    player.apply_class_selection(normalize_class_selection(int(C.CLASS_SOLDIER)))
    server.players[player.id] = player
    server.teams[team].add_player(player)
    server.connections[connection] = connection
    await server.mode.on_player_join(player)
    server.respawn_player(player)
    return player


def target_scenario(kind: str, *, seed: int):
    """Put one real last survivor/VIP in a difficult place against a full roster."""
    if kind not in TARGET_SCENARIOS:
        raise ValueError(kind)

    async def setup(server, director, clock, state: dict):
        from modes.vip import VIPMode
        from modes.zombie import ZombieMode, ZombiePhase, SURVIVOR_TEAM, ZOMBIE_TEAM
        from scripts.bot_zombie_horde_scenario import (
            _flat_site, _wade_site, build_structure, land_components,
        )

        mode = server.mode
        if not isinstance(mode, (VIPMode, ZombieMode)):
            raise ValueError("target scenarios require VIP or Zombie mode")
        world = server.world_manager
        target = await add_human(server, TEAM1, f"{kind} target")
        if isinstance(mode, ZombieMode):
            mode._assign_survivor(target)
            target.spawn(*world.get_spawn_point(SURVIVOR_TEAM))
            for bot in tuple(director.bots):
                await mode._infect(bot, patient_zero=False)
                server.respawn_player(bot)
            mode.phase = ZombiePhase.ACTIVE
            mode.infection_deadline = None
            mode.start_time = clock.time()
            await mode._refresh_last_survivor_marker()
            if target.team != SURVIVOR_TEAM or any(b.team != ZOMBIE_TEAM for b in director.bots):
                raise RuntimeError("last-man-standing roster was not established")
        else:
            server.config.auto_balance = False
            # This is a last-player challenge, not an ordinary 8v8 with an
            # idle VIP. Establish the initial asymmetric roster before the
            # real selection path; nobody can end the challenge by killing
            # the opposing crown while the test target is still hidden.
            for bot in tuple(director.bots):
                server.teams[bot.team].remove_player(bot)
                bot.team = TEAM2
                server.teams[TEAM2].add_player(bot)
                server.respawn_player(bot)
            mode.minimum_team_size = 1
            await mode._select_vips()
            if mode.vips.get(TEAM1) is not target:
                raise RuntimeError("test VIP was not selected")

        cells: list[tuple[int, int, int]] = []
        rng = random.Random(seed)
        if kind == "water":
            labels, sizes = land_components(world)
            if not sizes:
                raise RuntimeError("map has no dry shore")
            wx, wy = _wade_site(world, labels, max(range(len(sizes)), key=sizes.__getitem__), rng)[:2]
            position = (wx + .5, wy + .5, 237.75)
        else:
            x, y, floor = _flat_site(world, rng, level=True, span=8)
            if kind == "buried":
                support = min(235, floor + 6)
                for cx in range(x - 1, x + 2):
                    for cy in range(y - 1, y + 2):
                        for z in range(support - 4, support):
                            world.map.remove_point(cx, cy, z)
                cells = [(cx, cy, floor) for cx in range(x - 2, x + 3)
                         for cy in range(y - 2, y + 3) if world.map.get_solid(cx, cy, floor)]
                position = (x + .5, y + .5, support - 2.25)
            else:
                shape = (kind if kind in {"highroof","skybridge","stacked"} else
                         "fortress" if kind in {"rooftop","moving"} else "room")
                if kind == "repairing":
                    # A low load-bearing hut the survivor can legally repair
                    # from its roof. Tall foundations cannot be reached from
                    # the top, so rebuilding those would be a fake challenge.
                    built=[]
                    top=floor-3
                    for cx in range(x-3,x+4):
                        for cy in range(y-3,y+4):
                            for z in range(top,floor):
                                if z==top or cx in (x-3,x+3) or cy in (y-3,y+3):
                                    world.set_block(cx,cy,z,True,0x777777)
                                    built.append((cx,cy,z))
                    tops=[(x,y,top)]
                else:
                    tops, built = build_structure(world,shape,x,y,floor,
                        height=128 if kind=="highroof" else 64,
                        bridge_span=64,bridge_width=1+seed%3)
                cells = [tuple(cell) for cell in built]
                tx, ty, support = tops[0]
                position = (tx + .5, ty + .5, support - 2.25)
        health = int(target.health)
        target.spawn(*position)
        target.health = health
        target._world_object.set_velocity(0.0, 0.0, 0.0)
        if not target.alive or not target.spawned:
            raise RuntimeError("scenario target did not enter the match alive")
        # Structures were placed before the first simulated tick. Give the
        # worker their actual VXL rather than the original authored map.
        world.map_raw_bytes = world.map.generate_vxl()
        director.supervisor.start(director._make_map_snapshot(current=True))
        initial_health = int(target.health)
        initial_team = int(target.team)
        from server.bot_ai.director import _player_life_id
        initial_life = _player_life_id(target)
        if kind == "repairing":
            target.set_tool(int(C.BLOCK_TOOL),raw=True)
            target.blocks = min(200,target.blocks)
        initial_blocks = target.blocks
        corners = [(position[0]+dx,position[1]+dy) for dx,dy in ((-.6,-.6),(.6,-.6),(.6,.6),(-.6,.6))]
        control = {"corner":0,"next_repair":0.,"pending":None}
        state.update(kind=kind, target_id=int(target.id), target_position=position,
                     target_started_alive=True,
                     bots=len(director.bots), starting_bot_positions={str(b.id): b.position for b in director.bots},
                     structure_cells=len(cells), first_damage_s=None, target_defeated_s=None,
                     structure_cells_removed=0, first_collapse_s=None, target_fell_s=None)
        state["target_team_size"] = sum(p.team == initial_team for p in server.players.values())

        def on_tick(tick: int, elapsed: float) -> None:
            # Preserve the original challenge's outcome. Subsequent natural
            # rounds may respawn this actor and demolish the old test building.
            if state["target_defeated_s"] is not None:
                target.connection.scenario_flags = (False,) * 8
                return
            if kind == "moving" and target.alive and target.spawned and target.grounded:
                goal=corners[control["corner"]]
                dx,dy=goal[0]-target.x,goal[1]-target.y
                distance=math.hypot(dx,dy)
                if distance<.3:
                    control["corner"]=(control["corner"]+1)%len(corners)
                    state["human_turns"]=state.get("human_turns",0)+1
                else:
                    target.connection.scenario_orientation=(dx/distance,dy/distance,0.)
                target.connection.scenario_flags=(distance>.3,False,False,False,False,False,False,False)
            if kind == "repairing" and target.alive and target.spawned and elapsed>=control["next_repair"]:
                control["next_repair"]=elapsed+.3
                pending=control["pending"]
                if pending is not None and world.get_solid(*pending):
                    state["human_repairs_confirmed"]=state.get("human_repairs_confirmed",0)+1
                    control["pending"]=None
                # Real build reach, visibility, attachment, body overlap,
                # tool cadence and inventory are checked by the authority.
                missing=sorted((cell for cell in cells if not world.get_solid(*cell)
                    and math.dist(target.eye,tuple(v+.5 for v in cell))<5.),
                    key=lambda c:math.dist(target.eye,tuple(v+.5 for v in c)))
                if missing and control["pending"] is None:
                    cell=missing[0]
                    direction=tuple(cell[a]+.5-target.eye[a] for a in range(3))
                    length=math.sqrt(sum(v*v for v in direction))
                    target.connection.scenario_orientation=tuple(v/length for v in direction)
                    packet=SimpleNamespace(x=cell[0],y=cell[1],z=cell[2],loop_count=server.loop_count)
                    state["human_repair_attempts"]=state.get("human_repair_attempts",0)+1
                    if server.combat.handle_block_build(target,packet):
                        control["pending"]=cell
                        state["human_blocks_spent"]=initial_blocks-target.blocks
                if control["pending"] is not None and tick%180==0:
                    control["pending"]=None
            state["target_final_position"] = tuple(target.position)
            if state["target_fell_s"] is None and target.position[2] > position[2] + 4:
                state["target_fell_s"] = round(elapsed, 3)
            horde = getattr(director, "_zombie_siege", None)
            if horde is not None:
                roles = Counter(order.role for order in horde.last_orders.values())
                peaks = state.setdefault("horde_role_peaks", {})
                for role, count in roles.items():
                    peaks[role] = max(peaks.get(role, 0), count)
                state["horde_max_pile"] = horde.coordinator.metrics.max_pile
            if state["first_damage_s"] is None and target.health < initial_health:
                state["first_damage_s"] = round(elapsed, 3)
            if state["target_defeated_s"] is None and (not target.alive or target.team != initial_team):
                state["target_defeated_s"] = round(elapsed, 3)
            if _player_life_id(target) != initial_life and not state.get("target_reset_s"):
                state["target_reset_s"] = round(elapsed, 3)
            if (tick % 60 == 0 or state["target_defeated_s"] is not None) and cells:
                remaining = sum(bool(world.map.get_solid(*cell)) for cell in cells)
                state["structure_cells_removed"] = len(cells) - remaining
                if state["first_collapse_s"] is None and remaining <= len(cells) // 2:
                    state["first_collapse_s"] = round(elapsed, 3)

        return on_tick

    return setup
