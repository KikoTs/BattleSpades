"""Physical pickups, deliveries, construction objectives and cooperative jobs."""
from __future__ import annotations

from dataclasses import dataclass
import math

import shared.constants as C
from server.pickups import broadcast_drop, broadcast_pickup, sanitize_drop
from .base import FunMode, TEAMS, TEAM1, TEAM2, TEAM_NEUTRAL, Vec, distance


@dataclass(eq=False)
class Cargo:
    home: Vec
    position: Vec
    team: int = TEAM_NEUTRAL
    value: int = 1
    holder: object = None
    entity_id: int | None = None
    available_at: float = 0.0
    hidden: bool = False
    marker_index: int | None = None


class CargoMode(FunMode):
    def __init__(self, server):
        super().__init__(server)
        self.cargo: list[Cargo] = []

    def create_cargo(self, position, team=TEAM_NEUTRAL, value=1, hidden=False, marker_index=None):
        token = Cargo(tuple(position), tuple(position), team, value, hidden=hidden, marker_index=marker_index)
        self.cargo.append(token)
        self.show_cargo(token)
        return token

    def show_cargo(self, token):
        entity = self.server.entity_registry.place(int(C.INTEL_PICKUP), *token.position,
                                                  state=token.team, kind="fun_cargo")
        token.entity_id = entity.entity_id
        self._entities.add(entity.entity_id)
        self.server.broadcast_create_entity(entity)
        if token.marker_index is not None:
            self.marker(token.marker_index, token.position, icon="minimap_intel")

    def hide_cargo(self, token):
        if token.marker_index is not None:
            self.remove_marker(token.marker_index)
        if token.entity_id is not None:
            if self.server.entity_registry.remove(token.entity_id) is not None:
                self.server.broadcast_destroy_entity(token.entity_id)
            self._entities.discard(token.entity_id)
            token.entity_id = None

    def carried(self, player):
        return next((c for c in self.cargo if c.holder is player), None)

    def can_take(self, player, token):
        return self.active(player) and self.carried(player) is None

    def visible(self, player, position):
        delta = tuple(b - a for a, b in zip(player.eye, position))
        length = math.sqrt(sum(v * v for v in delta))
        if length < 0.01:
            return True
        hit = self.server.world_manager.raycast(*player.eye, *(v / length for v in delta), max(0.0, length - 0.2))
        return hit is None

    async def take(self, player, token):
        if not token.hidden and not broadcast_pickup(self.server, player, int(C.INTEL_PICKUP), burdensome=False, state=token.team):
            return
        self.hide_cargo(token)
        token.holder = player
        await self.tell(player, f"Carrying {token.value} supply unit(s).")

    def release(self, token, *, position=None, velocity=(0.0, 0.0, 0.0), consume=False):
        player = token.holder
        if player is not None and not token.hidden:
            broadcast_drop(self.server, player, player.position, velocity)
        token.holder = None
        self.hide_cargo(token)
        if consume:
            if token in self.cargo:
                self.cargo.remove(token)
            return
        token.position = tuple(position or (player.position if player is not None else token.home))
        # Recover pickups dropped in water/solid/outside the authored arena.
        if token.position[2] >= float(self.data.get("fall_z", 235)):
            token.position = token.home
        token.available_at = self.clock() + 0.8
        self.show_cargo(token)

    async def handle_drop_pickup(self, player, position, velocity):
        token = self.carried(player)
        if token is None:
            return False
        pos, vel = sanitize_drop(player, int(C.INTEL_PICKUP), position, velocity)
        if pos is None:
            return True
        # Short, wall-clipped passes. Packet 71 supplies the native throw;
        # the authoritative persistent pickup is grounded at the landing spot.
        target = tuple(pos[i] + vel[i] * 0.18 for i in range(3))
        target = tuple(max(1.0, min(limit, value)) for value, limit in zip(target, (510, 510, 234)))
        if not self.visible(player, target):
            target = player.position
        grounded = self.server.world_manager.dry_surface_anchor(target[0], target[1])
        # A roof must not teleport an underground pass to the surface.
        if abs(grounded[2] - target[2]) > 5:
            grounded = player.position
        self.release(token, position=grounded, velocity=vel)
        return True

    async def on_player_death(self, player, killer, kill_type):
        token = self.carried(player)
        if token:
            self.release(token)
        await super().on_player_death(player, killer, kill_type)

    async def on_player_leave(self, player):
        token = self.carried(player)
        if token:
            self.release(token)
        await super().on_player_leave(player)

    async def tick_live(self, now, dt):
        for token in list(self.cargo):
            if token.holder is not None or now < token.available_at:
                continue
            for player in self.alive():
                if self.can_take(player, token) and distance(player.position, token.position) < 3 and self.visible(player, token.position):
                    await self.take(player, token)
                    break
        await self.objectives_tick(now, dt)

    async def objectives_tick(self, now, dt):
        pass

    def clear_round_objects(self):
        for token in list(getattr(self, "cargo", [])):
            self.release(token, consume=True)
        super().clear_round_objects()


class FalseWall(CargoMode):
    mode_code = "false_wall"

    async def prepare_round(self):
        self.stashed = set()
        self.stash_owners = {}
        for team in TEAMS:
            self.create_cargo(self.home(team), team)
            self.marker(team, self.home(team), team=team)

    async def stash(self, player):
        if self.phase != "prepare" or player not in self.participants or not player.alive:
            return
        owner = self.stash_owners.get(player.team)
        replace_bot = owner is not None and owner.is_bot and not player.is_bot
        if (player.team in self.stashed and not replace_bot) or distance(player.position, self.home(player.team)) > 14:
            await self.tell(player, "One stash per team, within 14 blocks of your base.")
            return
        token = next(c for c in self.cargo if c.team == player.team)
        self.hide_cargo(token)
        token.home = token.position = player.position
        self.show_cargo(token)
        self.stashed.add(player.team)
        self.stash_owners[player.team] = player
        await self.tell(player, "Cache hidden here. Build your false walls!")

    async def begin_round(self):
        from server.announcements import build_overlay_message
        self.server.broadcast(build_overlay_message("RAID: steal the ENEMY cache and bring it to YOUR base!"), reliable=True)
        await self.broadcast_message("First cache delivered to your own base wins. Defend your team's cache too!")

    def can_take(self, player, token):
        return token.team != player.team and super().can_take(player, token)

    async def objectives_tick(self, now, dt):
        for token in list(self.cargo):
            if token.holder and distance(token.holder.position, self.home(token.holder.team)) < 6:
                winner = token.holder.team
                self.release(token, consume=True)
                await self.finish((winner,), "Cache recovered!")
                return


class LastExit(CargoMode):
    mode_code = "last_exit"

    async def prepare_round(self):
        self.extract_progress = {}
        self.exit_open = False
        self.extracted = set()
        for i in range(6):
            pos = self.anchor(f"loot{i + 1}") if f"loot{i + 1}" in self.anchors else self.offset_center(i)
            self.create_cargo(pos, value=1 + i % 3, marker_index=10 + i)
        self.exit_position = self.anchor("exit")
        self.marker(30, self.exit_position, icon="minimap_spawn", radius=5)

    def offset_center(self, i):
        x, y, _ = self.anchor("center")
        return self.server.world_manager.dry_surface_anchor(x + (i % 3 - 1) * 12, y + (i // 3 * 2 - 1) * 10)

    async def objectives_tick(self, now, dt):
        if not self.exit_open and self.deadline - now <= self.duration - min(60, self.duration / 4):
            self.exit_open = True
            self.exit_position = self.anchor("exit")
            self.marker(30, self.exit_position, icon="minimap_spawn")
            await self.broadcast_message("EXIT OPEN: hold the marked exit for 4 seconds to extract.")
        if self.exit_open:
            for player in self.alive():
                token = self.carried(player)
                at_exit = distance(player.position, self.exit_position) < 5
                contested = any(p.team != player.team and distance(p.position, self.exit_position) < 5 for p in self.alive())
                self.extract_progress[player] = self.extract_progress.get(player, 0) + dt if at_exit and not contested else 0
                if self.extract_progress[player] >= 4:
                    value = token.value if token else 0
                    if token:
                        self.release(token, consume=True)
                    self.award(player.team, value)
                    self.extracted.add(player)
                    self.eliminated.add(player)
                    self.teleport(player, self.anchor("waiting"))
                    await self.broadcast_message(f"{player.name} extracted {value} points safely.")
        if not self.alive():
            await self.finish(self.leaders())


class TunnelRats(CargoMode):
    mode_code = "tunnel_rats"

    async def prepare_round(self):
        for i in range(3):
            point = self.anchor(f"cache{i + 1}")
            self.create_cargo(point)
            self.marker(i, point, icon="minimap_intel")
        for team in TEAMS:
            self.marker(10 + team, self.home(team), team=team)

    async def objectives_tick(self, now, dt):
        for token in list(self.cargo):
            if token.holder and distance(token.holder.position, self.home(token.holder.team)) < 6:
                player = token.holder
                self.release(token, consume=True)
                self.award(player.team)
                if self.points[player.team] >= 2:
                    await self.finish((player.team,))
                    return


class BridgeChicken(CargoMode):
    mode_code = "bridge_chicken"

    async def prepare_round(self):
        for team in TEAMS:
            self.create_cargo(self.home(team), team)
            self.marker(team, self.home(TEAM2 if team == TEAM1 else TEAM1), team=team)

    def can_take(self, player, token):
        return player.team == token.team and super().can_take(player, token)

    def modify_incoming_damage(self, player, amount, source, kill_type):
        return 0 if source is None else super().modify_incoming_damage(player, amount, source, kill_type)

    async def objectives_tick(self, now, dt):
        for player in self.alive():
            token = self.carried(player)
            if player.z > float(self.data.get("fall_z", 110)):
                if token:
                    self.release(token, position=token.home)
                self.reset_player(player)
            elif token and distance(player.position, self.home(TEAM2 if player.team == TEAM1 else TEAM1)) < 5:
                await self.finish((player.team,), "Relay delivered!")
                return


class Towerfall(FunMode):
    mode_code = "towerfall"

    async def prepare_round(self):
        self.cores = {}
        for team, key in ((TEAM1, "core_blue"), (TEAM2, "core_green")):
            point = self.anchor(key)
            self.cores[team] = tuple(int(v) for v in point)
            self.marker(team, point, team=team, icon="minimap_occupation_target")

    async def tick_live(self, now, dt):
        lost = [team for team, cell in self.cores.items() if not self.server.world_manager.get_solid(*cell)]
        if lost:
            await self.finish(tuple(team for team in TEAMS if team not in lost), "Core collapsed!")

    def allow_action(self, player, action, position=None):
        # Cores cannot be painted into an indistinguishable ordinary block.
        if action == "paint" and position in getattr(self, "cores", {}).values():
            return False
        return super().allow_action(player, action, position)


class PaintRaid(FunMode):
    mode_code = "paint_raid"

    async def prepare_round(self):
        self.owners = {}
        self.paint_bounds = tuple(self.data.get("paint_bounds", (230, 282, 235, 277, 96)))
        if len(self.paint_bounds) != 5:
            raise ValueError("paint_bounds requires [x0, x1, y0, y1, z]")
        self.marker(0, self.anchor("center"))

    def paint_color_for(self, player):
        return self.server.teams[player.team].color

    def allow_action(self, player, action, position=None):
        if action == "paint":
            if not self.active(player) or position is None:
                return False
            x0, x1, y0, y1, z = self.paint_bounds
            return x0 <= position[0] <= x1 and y0 <= position[1] <= y1 and position[2] == z
        return super().allow_action(player, action, position)

    async def on_block_paint(self, player, x, y, z):
        cell = (x, y, z)
        if not self.allow_action(player, "paint", cell):
            return
        previous = self.owners.get(cell)
        if previous == player.team:
            return
        if previous in TEAMS:
            self.award(previous, -1)
        self.owners[cell] = player.team
        self.award(player.team)


class SmugglersRun(CargoMode):
    mode_code = "smugglers_run"
    compare_deliveries = True

    async def prepare_round(self):
        self.delivered = 0
        self.delivery_progress = {}
        for i in range(5):
            home = self.home(self.attackers)
            self.create_cargo((home[0] + (i % 3 - 1) * 2, home[1] + (i // 3) * 2, home[2]), self.attackers, hidden=True)
        self.marker(0, self.home(self.defenders), icon="minimap_diamond_dropoff")

    def can_take(self, player, token):
        return player.team == self.attackers and super().can_take(player, token)

    async def objectives_tick(self, now, dt):
        for player in self.alive(self.attackers):
            token = self.carried(player)
            at_goal = token and distance(player.position, self.home(self.defenders)) < 6
            self.delivery_progress[player] = self.delivery_progress.get(player, 0) + dt if at_goal else 0
            if self.delivery_progress[player] >= 2:
                self.release(token, consume=True)
                self.delivered += 1
                self.award(self.attackers)
                self.delivery_progress[player] = 0
                await self.broadcast_message(f"Package delivered: {self.delivered}/5")
        if self.delivered == 5:
            await self.finish((self.attackers,))

    async def timeout(self):
        await self.finish((self.attackers,) if self.delivered >= 3 else (self.defenders,), f"Delivered {self.delivered}/5.")


class ReverseSiege(SmugglersRun):
    mode_code = "reverse_siege"

    def home(self, team):
        if self.anchors:
            return self.anchor("fort") if team == self.attackers else self.anchor("exit")
        return super().home(team)

    async def prepare_round(self):
        await super().prepare_round()
        self.close_at = self.clock() + self.preparation + 45
        self.gates = deque_gates(self.data.get("closing_gates", []))

    async def objectives_tick(self, now, dt):
        await super().objectives_tick(now, dt)
        if self.gates and now >= self.close_at:
            cells = self.gates.pop(0)
            for cell in cells:
                # Never construct a closing gate through a living player.
                if not any(distance(p.position, cell) < 2.5 for p in self.players(alive=True)):
                    self.set_cell(cell, True, 0xB07030)
            self.close_at = now + 35
            await self.broadcast_message("An escape route is closing. Dig through or find another!")


def deque_gates(value):
    gates = []
    for group in value[:8]:
        cells = [tuple(int(v) for v in FunMode.valid_position(c)) for c in group[:64]]
        gates.append(cells)
    return gates


class ScrapyardEngineers(Towerfall):
    mode_code = "scrapyard_engineers"
    # Exact in the retail 1/128 fixed-point field, and integral for both
    # Engineer wallets (2000/3000). A server-only arbitrary +25 grant has no
    # matching native packet and eventually leaves the local tool unusable.
    block_wallet_multiplier = 0.125

    def configure_initial_info(self, packet):
        super().configure_initial_info(packet)
        packet.block_wallet_multiplier = self.block_wallet_multiplier

    async def prepare_round(self):
        await super().prepare_round()
        self.scrap_at = {}
        self.marker(10, self.anchor("center"), icon="minimap_blockcrate")

    async def tick_live(self, now, dt):
        await super().tick_live(now, dt)
        for player in self.alive():
            if distance(player.position, self.anchor("center")) < 5 and now >= self.scrap_at.get(player, 0):
                player.restock_blocks()
                self.scrap_at[player] = now + 8
                await self.tell(player, "Salvage refilled your building blocks.")


class Spadeball(CargoMode):
    mode_code = "spadeball"

    def __init__(self, server):
        super().__init__(server)
        self.goal_pause = 0

    def loadout(self, player):
        return (int(C.BLOCK_TOOL), int(C.PICKAXE_TOOL))

    async def prepare_round(self):
        self.ball = self.create_cargo(self.anchor("center"))
        self.last_team = None
        self.goal_pause = 0
        for team in TEAMS:
            self.marker(team, self.home(team), team=team)

    async def take(self, player, token):
        await super().take(player, token)
        if token.holder is player:
            self.last_team = player.team

    async def objectives_tick(self, now, dt):
        if now < self.goal_pause or self.last_team is None:
            return
        position = self.ball.holder.position if self.ball.holder else self.ball.position
        goal_team = TEAM2 if self.last_team == TEAM1 else TEAM1
        if distance(position, self.home(goal_team)) < 5:
            self.award(self.last_team)
            await self.broadcast_message(f"GOAL for {self.label(self.last_team)}!")
            self.release(self.ball, position=self.anchor("center"))
            if self.points[self.last_team] >= 5:
                await self.finish((self.last_team,))
                return
            self.goal_pause = now + 3
            self.ball.available_at = self.goal_pause
            self.last_team = None
            for player in self.players():
                self.reset_player(player)

    def modify_incoming_damage(self, player, amount, source, kill_type):
        if self.clock() < self.goal_pause:
            return 0
        return super().modify_incoming_damage(player, amount, source, kill_type)


class NightShift(CargoMode):
    """Finite waves; optional repairs never gate enemy or round progression."""
    mode_code = "night_shift"
    supports_bots = True
    owns_bot_population = True
    wave_count = 6

    def __init__(self, server):
        super().__init__(server)
        self.wave_bots = set()
        self.stage = self.wave = self.spawn_remaining = 0
        self.intermission_until = 0.0

    def ready(self):
        return any(not p.is_bot for p in self.players()) and self.server.bots is not None

    def prepare_join_team(self, requested):
        return TEAM1

    def prepare_bot_team(self, requested):
        return TEAM1 if requested == TEAM1 else TEAM2

    async def on_player_join(self, player):
        if player.is_bot:
            self.participants.add(player)
            if player.team == TEAM2:
                self.wave_bots.add(player)
        await super().on_player_join(player)

    def bot_team_anchor(self, team):
        return self.home(team)

    def get_spawn_point(self, player):
        if player.is_bot and player.team == TEAM2 and "enemy_north" in self.anchors:
            return self.anchor(("green", "enemy_north", "enemy_south")[(player.id + self.wave) % 3])
        return super().get_spawn_point(player)

    async def on_player_leave(self, player):
        self.wave_bots.discard(player)
        await super().on_player_leave(player)

    def configure_state_data(self, packet):
        super().configure_state_data(packet)
        packet.team2_locked = True

    def allows_bot_join(self, team=None):
        return self.phase in ("prepare", "live") and team in TEAMS

    async def prepare_round(self):
        await self.retire_wave()
        self.stage = self.wave = self.spawn_remaining = self.deliveries = 0
        self.evacuation = self.intermission_until = self.next_bot = 0.0
        self.spawn_failed_since = None
        self.status_at = 0.0
        self.repair_rewarded = False
        self.repair_cells = [tuple(int(v) for v in self.valid_position(c))
                             for c in self.data.get("repair_cells", [])[:64]]
        for cell in self.repair_cells:
            self.set_cell(cell, False)
        self.show_jobs()

    def show_jobs(self):
        self.clear_markers()
        self.marker(0, self.home(TEAM1), team=TEAM1)
        if self.repair_cells and not self.repair_rewarded:
            self.marker(1, self.repair_cells[len(self.repair_cells) // 2])

    async def begin_round(self):
        await self.start_wave()

    async def start_wave(self):
        self.wave += 1
        self.stage = 1
        self.intermission_until = 0.0
        humans = max(1, sum(not p.is_bot for p in self.players()))
        self.spawn_remaining = min(24, 3 + self.wave * 2 + (humans - 1) * 2)
        self.next_bot = self.clock()
        self.spawn_failed_since = None
        for player in tuple(self.participants):
            if player.team == TEAM1:
                if not player.alive:
                    self.reset_player(player)
                else:
                    player.restock_ammo(int(C.AMMO_CRATE))
        await self.broadcast_message(f"WAVE {self.wave}/{self.wave_count}: {self.spawn_remaining} enemies. Clear them all. Yellow B = optional barricade repair; use BLOCKS to fill the bright gaps.")

    def can_player_respawn(self, player):
        if self.phase == "live":
            return player.team == TEAM1 and (self.intermission_until > 0 or self.can_join_round(player))
        return player.team == TEAM1 and super().can_player_respawn(player)

    def can_take(self, player, token):
        return player.team == TEAM1 and super().can_take(player, token)

    async def repair_tick(self):
        if (not self.repair_rewarded and self.repair_cells
                and all(self.server.world_manager.get_solid(*cell) for cell in self.repair_cells)):
            self.repair_rewarded = True
            for player in self.alive(TEAM1):
                player.restock_ammo(int(C.AMMO_CRATE))
            self.show_jobs()
            await self.broadcast_message("Barricade repaired: team ammunition refilled! Repairs are optional; clear enemies to advance.")

    async def objectives_tick(self, now, dt):
        for bot in tuple(self.wave_bots):
            if not bot.alive or self.server.players.get(bot.id) is not bot:
                self.wave_bots.discard(bot)
                if self.server.players.get(bot.id) is bot:
                    await self.server.bots.remove_bot(bot, force=True)
        humans = [p for p in self.alive(TEAM1) if not p.is_bot]
        if not humans:
            await self.finish((TEAM2,), "Squad eliminated.")
            return
        if self.stage == 3:
            at_exit = any(distance(p.position, self.anchor("exit")) < 6 for p in self.alive(TEAM1))
            self.evacuation = self.evacuation + dt if at_exit else 0
            if self.evacuation >= 6:
                await self.finish((TEAM1,), "All six waves cleared. Squad extracted!")
            elif now >= self.status_at:
                self.status_at = now + 2
                await self.broadcast_message(f"EVAC: follow the spawn marker and hold for {max(1, math.ceil(6 - self.evacuation))} seconds.")
            return
        await self.repair_tick()
        if self.intermission_until:
            if now >= self.intermission_until:
                await self.start_wave()
            return
        if self.spawn_remaining and len(self.wave_bots) < 8 and now >= self.next_bot:
            self.next_bot = now + 1.0
            bot = await self.server.bots.add_bot(team=TEAM2)
            if bot is not None:
                self.wave_bots.add(bot)
                self.participants.add(bot)
                self.spawn_remaining -= 1
                self.spawn_failed_since = None
            else:
                self.spawn_failed_since = now if self.spawn_failed_since is None else self.spawn_failed_since
                if now - self.spawn_failed_since >= 20:
                    await self.finish((), "Cannot spawn the enemy wave: check bot capacity. Round reset; no winner awarded.")
                    return
        if not self.spawn_remaining and not self.wave_bots:
            if self.wave >= self.wave_count:
                self.stage = 3
                self.clear_markers()
                self.marker(0, self.anchor("exit"), icon="minimap_spawn")
                await self.broadcast_message("All waves cleared! Follow EVAC on the map. Stand inside for 6 seconds.")
            else:
                self.intermission_until = now + 8
                self.show_jobs()
                for player in tuple(self.participants):
                    if player.team == TEAM1 and not player.alive:
                        self.reset_player(player)
                await self.broadcast_message(f"Wave {self.wave} cleared! Next wave in 8 seconds. Fallen squadmates return; reload and repair.")
        elif now >= self.status_at:
            self.status_at = now + 10
            await self.broadcast_message(f"Wave {self.wave}/{self.wave_count}: {len(self.wave_bots)} alive, {self.spawn_remaining} incoming.")
        if not self.spawn_remaining and 0 < len(self.wave_bots) <= 2:
            if now >= getattr(self, "enemy_marker_at", 0):
                self.enemy_marker_at = now + .5
                for bot in self.wave_bots:
                    self.moving_marker(100 + bot.id, bot.position, "map_player_16")

    async def timeout(self):
        await self.finish((TEAM2,), "Survival time expired.")

    async def retire_wave(self):
        if self.server.bots is not None:
            for bot in tuple(self.wave_bots):
                await self.server.bots.remove_bot(bot, force=True, notify_mode=False)
                self.participants.discard(bot)
        self.wave_bots.clear()
        self.spawn_remaining = 0

    async def deactivate(self):
        await self.retire_wave()
        await super().deactivate()
