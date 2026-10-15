"""Retail-physics races: ordered checkpoints, fall recovery and timed hazards."""
from __future__ import annotations

import math
import shared.constants as C
from .base import FunMode, distance


class CourseMode(FunMode):
    def __init__(self, server):
        super().__init__(server)
        self.checkpoint = {}
        self.penalty = {}
        self.finished = {}
        self.best = {}
        self.course = []
        self.retry_after = {}

    def _load_arena(self):
        super()._load_arena()
        self.course = [self.valid_position(v) for v in self.data.get("checkpoints", [])]
        if not 2 <= len(self.course) <= 32:
            raise ValueError("Race arena requires 2..32 ordered checkpoints")

    def loadout(self, player):
        return (int(C.PICKAXE_TOOL),)

    def get_spawn_point(self, player):
        if not self.course:
            return super().get_spawn_point(player)
        point = self.course[self.checkpoint.get(player, 0)]
        roster = sorted(self.players(), key=lambda p: p.id)
        rank = roster.index(player) if player in roster else 0
        # Avoid native body collisions ejecting an overlapping race grid.
        offsets = sorted(((x * 1.5, y * 1.5) for x in range(-2, 3) for y in range(-2, 3)),
                         key=lambda v: (v[0] ** 2 + v[1] ** 2, v))
        dx, dy = offsets[rank % len(offsets)]
        return point[0] + dx, point[1] + dy, point[2]

    async def prepare_round(self):
        self.checkpoint = {p: 0 for p in self.participants}
        self.penalty = {p: 0.0 for p in self.participants}
        self.finished.clear()
        self.retry_after.clear()
        self.crumble = {}
        self.rebuild = {}
        self.next_floor = 0.0
        self._checkpoint_zones = {}

    async def begin_round(self):
        self.race_start = self.clock()
        for player in self.players(alive=True):
            self.teleport(player, self.get_spawn_point(player))
            self.show_checkpoint(player)

    async def on_player_join(self, player):
        await super().on_player_join(player)
        if player in self.participants:
            self.checkpoint.setdefault(player, 0)
            self.penalty.setdefault(player, 0.0)
            if self.phase == "live":
                self.show_checkpoint(player)

    def show_checkpoint(self, player):
        from modes.objective_zones import around, minimap_zone_packet, minimap_zone_clear_packet
        import shared.constants_gamemode as CG
        old = self._checkpoint_zones.pop(player.id, None)
        if old:
            player.send(bytes(minimap_zone_clear_packet(old).generate()))
        index = self.checkpoint.get(player, 0) + 1
        if index < len(self.course):
            zone = around(index, self.course[index], radius_xy=3, height_above=2, depth_below=2)
            self._checkpoint_zones[player.id] = zone
            player.send(bytes(minimap_zone_packet(zone, color=(245, 190, 55), icon_id=CG.ZONE_ICON_SPAWN, icon_scale=.35).generate()))

    def reveal_to(self, connection):
        super().reveal_to(connection)
        if getattr(connection, "player", None) is not None and self.course and hasattr(self, "_checkpoint_zones"):
            self.show_checkpoint(connection.player)

    def clear_markers(self):
        from modes.objective_zones import minimap_zone_clear_packet
        for pid, zone in getattr(self, "_checkpoint_zones", {}).items():
            player = self.server.players.get(pid)
            if player is not None:
                player.send(bytes(minimap_zone_clear_packet(zone).generate()))
        self._checkpoint_zones = {}
        super().clear_markers()

    def active(self, player):
        return super().active(player) and player not in self.finished

    def modify_incoming_damage(self, player, amount, source, kill_type):
        return 0

    def allow_action(self, player, action, position=None):
        return False

    async def retry(self, player):
        if not self.active(player) or self.clock() < self.retry_after.get(player, 0):
            return
        self.retry_after[player] = self.clock() + 0.5
        self.penalty[player] = self.penalty.get(player, 0.0) + 2.0
        self.teleport(player, self.get_spawn_point(player))
        await self.tell(player, "Checkpoint reset: +2 seconds.")

    async def tick_live(self, now, dt):
        for player in self.alive():
            if player.z > float(self.data.get("fall_z", 105)):
                await self.retry(player)
                continue
            next_index = self.checkpoint.get(player, 0) + 1
            if next_index < len(self.course) and distance(player.position, self.course[next_index]) < 3:
                self.checkpoint[player] = next_index
                self.show_checkpoint(player)
                if next_index == len(self.course) - 1:
                    result = now - self.race_start + self.penalty[player]
                    self.finished[player] = result
                    self.best[player] = min(result, self.best.get(player, float("inf")))
                    await self.broadcast_message(f"{player.name} finished in {result:.2f}s (penalties {self.penalty[player]:.0f}s).")
                    self.deadline = min(self.deadline, now + 20)
                else:
                    await self.tell(player, f"Checkpoint {next_index}/{len(self.course)-1}")
        if self.finished and not self.alive():
            await self.timeout()

    async def timeout(self):
        scores = {p: -round(t * 1000) for p, t in self.finished.items()}
        if not scores:
            await self.finish((), "Nobody finished.")
            return
        best = max(scores.values())
        await self.finish(tuple(p for p, score in scores.items() if score == best))

    async def on_player_leave(self, player):
        for mapping in (self.checkpoint, self.penalty, self.finished, self.best, self.retry_after):
            mapping.pop(player, None)
        await super().on_player_leave(player)


class CrumblingRun(CourseMode):
    mode_code = "crumbling_run"

    def configure_initial_info_for(self, connection, packet):
        if getattr(connection, "race_momentum_capable", False):
            packet.custom_game_rules.append(("BATTLESPADES_RACE_MOMENTUM", "1"))

    async def tick_live(self, now, dt):
        await super().tick_live(now, dt)
        if self.phase != "live":
            return
        if now >= self.next_floor:
            self.next_floor = now + 0.05
            for player in self.alive():
                if player.airborne:
                    continue
                # Checkpoint pads remain intact so retries always have support.
                if any(distance(player.position, point) < 6 for point in self.course):
                    continue
                x, y = math.floor(player.x - .5), math.floor(player.y - .5)
                for z in range(max(0, math.floor(player.z)), min(238, math.floor(player.z) + 5)):
                    if self.server.world_manager.get_solid(math.floor(player.x), math.floor(player.y), z):
                        if z != self.data.get("crumble_floor", z):
                            break
                        for dx in (0, 1):
                            for dy in (0, 1):
                                cell = x + dx, y + dy, z
                                if self.server.world_manager.get_solid(*cell):
                                    self.crumble.setdefault(cell, now + .3)
                        break
        for cell, due in list(self.crumble.items())[:256]:
            if now >= due:
                self.set_cell(cell, False)
                self.rebuild[cell] = now + 8
                del self.crumble[cell]
        for cell, due in list(self.rebuild.items())[:256]:
            if now >= due:
                if any(abs(p.x - cell[0] - .5) < 1 and abs(p.y - cell[1] - .5) < 1
                       and cell[2] - 2.5 < p.z < cell[2] + 1 for p in self.alive()):
                    continue
                self.set_cell(cell, True, self._baseline.get_color(*cell) or 0x909090)
                del self.rebuild[cell]


class RedLightRaid(CourseMode):
    mode_code = "red_light_raid"

    async def begin_round(self):
        await super().begin_round()
        self.red = False
        self.change_at = self.clock() + 6
        self.grace_until = 0
        self.still = {}

    async def tick_live(self, now, dt):
        if now >= self.change_at:
            self.red = not self.red
            self.change_at = now + (3 if self.red else 6)
            self.grace_until = now + 0.65
            self.still.clear()
            await self.broadcast_message("RED: stop moving!" if self.red else "GREEN: run!")
            from server.audio import play_sound, SND_DIG_HIT_BLOCK
            play_sound(self.server, SND_DIG_HIT_BLOCK, reliable=True)
        if self.red:
            for player in self.alive():
                if now < self.grace_until:
                    self.still[player] = player.position
                elif distance(player.position, self.still.setdefault(player, player.position)) > 0.4:
                    await self.retry(player)
                    self.still[player] = player.position
        await super().tick_live(now, dt)
