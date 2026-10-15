"""Disguise, elimination, tagging and gun-economy community modes."""
from __future__ import annotations

import math
import shared.constants as C
from .base import FunMode, TEAMS, TEAM1, TEAM2, distance


class BlockHunt(FunMode):
    mode_code = "block_hunt"

    def _load_arena(self):
        super()._load_arena()
        wm = self.server.world_manager
        if wm.map_name == "SpookyMansion" and not self.data.get("anchors"):
            # Dry paths around the mansion, clear of its roofs and water ring.
            self.anchors.update(blue=(235.5, 173.5, 227.25),
                                green=(271.5, 342.5, 227.25),
                                center=(280.5, 257.5, 217.25),
                                waiting=(245.5, 418.5, 234.25))
            self.data["hunt_points"] = [
                (235.5, 173.5, 227.25), (359.5, 202.5, 230.25),
                (189.5, 263.5, 230.25), (374.5, 252.5, 228.25),
                (271.5, 342.5, 227.25), (280.5, 160.5, 227.25),
                (280.5, 257.5, 217.25), (255.5, 258.5, 221.25),
                (281.5, 315.5, 227.25), (320.5, 257.5, 227.25),
            ]
        self.hunt_points = tuple(self.valid_position(p) for p in self.data.get("hunt_points", ())[:32]
                                 if wm.spawn_position_is_safe(p))

    def loadout(self, player):
        if player.team == self.defenders:
            return (int(C.BLOCK_TOOL), int(C.SPADE_TOOL), int(C.DISGUISE_TOOL))
        return (int(C.PICKAXE_TOOL), int(C.SMG_TOOL))

    def can_join_round(self, player):
        # Casual hide-and-seek admits a new human immediately. Existing
        # eliminated lives still wait; bot backfill cannot extend the round.
        return (super().can_join_round(player) or
                (self.phase == "live" and not self.ended and not self.retiring
                 and not player.is_bot and player.team in TEAMS
                 and player not in self.participants and player not in self.eliminated))

    def on_disguise_activated(self, player):
        # Retail Restock(AMMO_CRATE) replenishes the client counter as well as
        # the server wallet, preserving health, disguise and use cadence.
        player.restock_ammo(int(C.AMMO_CRATE))

    def modify_incoming_damage(self, player, amount, source, kill_type):
        if source is not None and source.team == self.defenders:
            return 0
        return super().modify_incoming_damage(player, amount, source, kill_type)

    async def prepare_round(self):
        self.next_clue = self.clock() + self.preparation + 30
        self.bot_clue_until = 0.0
        self.bot_clues = ()

    def allow_action(self, player, action, position=None):
        if action in ("dig", "disguise"):
            return (not self.ended and not self.retiring and player in self.participants
                    and player not in self.eliminated and player.team == self.defenders
                    and self.phase in ("prepare", "live"))
        if action == "paint":
            return False
        if action == "shoot" and player.team == self.defenders:
            return False
        return super().allow_action(player, action, position)

    async def tick_live(self, now, dt):
        if not self.alive(self.defenders):
            await self.finish((self.attackers,), "All hiders found!")
        elif not self.alive(self.attackers):
            await self.finish((self.defenders,), "Hunters eliminated!")
        elif now >= self.next_clue:
            from server.audio import play_sound, SND_DIG_HIT_BLOCK
            for player in self.alive(self.defenders):
                play_sound(self.server, SND_DIG_HIT_BLOCK, position=player.position, reliable=True)
            self.next_clue = now + 30
            self.bot_clue_until = now + 4
            # The sound gives a rough search area, never a hidden hitbox lock.
            self.bot_clues = tuple((round(p.x / 8) * 8.0, round(p.y / 8) * 8.0, p.z)
                                  for p in self.alive(self.defenders))
            await self.broadcast_message("Listen: hiders made a sound!")

    async def on_tick(self, tick):
        await super().on_tick(tick)
        if self.phase == "prepare":
            for player in self.players(alive=True):
                if (player in self.participants and player.team == self.attackers
                        and distance(player.position, self.home(player.team)) > 8):
                    point = self.server.world_manager.sanitize_spawn_point(self.get_spawn_point(player), player.team)
                    self.teleport(player, point)

    async def timeout(self):
        await self.finish((self.defenders,) if self.alive(self.defenders) else (self.attackers,))


class FloorTax(FunMode):
    mode_code = "floor_tax"

    def loadout(self, player):
        return (int(C.SPADE_TOOL), int(C.GRENADE_TOOL))

    async def prepare_round(self):
        self.next_cut = 0.0
        self.next_grenades = self.clock() + self.preparation + 12
        self.next_warning = self.clock() + self.preparation + 60
        self.sudden_death = self.clock() + self.preparation + 90

    def floor_cell(self, position):
        if position is None:
            return False
        x, y, z = position
        bounds = self.data.get("play_bounds", (210, 303, 226, 287))
        return (bounds[0] <= x < bounds[1] and bounds[2] <= y < bounds[3]
                and z in self.data.get("floor_levels", (82, 90, 98)))

    def allow_action(self, player, action, position=None):
        if action == "dig":
            return self.active(player) and self.floor_cell(position)
        return super().allow_action(player, action, position)

    def cut_floor(self, player):
        # Held input is sampled at 10 Hz, independent of stock shovel animation.
        # Aim and reach come from the authoritative body, never a supplied cell.
        if not player.is_spade_tool() or not (player.input.primary_fire or player.input.secondary_fire):
            return
        hit = self.server.world_manager.raycast(*player.eye, *player.orientation, 5.0)
        if hit is None or not self.floor_cell(hit):
            return
        x, y, z = hit
        for cell in ((x, y, z), (x + 1, y, z), (x, y + 1, z), (x + 1, y + 1, z)):
            if self.floor_cell(cell):
                self.set_cell(cell, False)

    async def tick_live(self, now, dt):
        fall_z = float(self.data.get("fall_z", 108))
        for player in self.alive():
            bounds = self.data.get("play_bounds")
            outside = bool(bounds and not (bounds[0] <= player.x < bounds[1]
                                           and bounds[2] <= player.y < bounds[3]))
            if player.z > fall_z or outside:
                self.eliminated.add(player)
                self.teleport(player, self.anchor("waiting"))
                await self.tell(player, "You fell out. Next round shortly.")
        if now >= self.next_cut:
            self.next_cut = now + 0.1
            for player in self.alive():
                self.cut_floor(player)
                if now >= self.sudden_death:
                    x, y, z = math.floor(player.x), math.floor(player.y), round(player.z + 2.25)
                    if self.floor_cell((x, y, z)):
                        self.set_cell((x, y, z), False)
        if now >= self.next_grenades:
            self.next_grenades = now + 12
            for player in self.alive():
                player.restock_ammo(int(C.AMMO_CRATE))
        if now >= self.next_warning:
            self.next_warning = now + 30
            await self.broadcast_message("SUDDEN DEATH: standing tiles crumble!" if now >= self.sudden_death else "Hold shovel fire to cut 2x2 holes. Grenades refill every 12 seconds; explosions push without health damage.")
        remaining = self.alive()
        if len(remaining) <= 1:
            await self.finish(tuple(remaining))

    def modify_incoming_damage(self, player, amount, source, kill_type):
        return 0


class HotPotato(FunMode):
    mode_code = "hot_potato"

    def __init__(self, server):
        super().__init__(server)
        self.lives = {}
        self.holder = None

    def loadout(self, player):
        return (int(C.PICKAXE_TOOL),)

    def get_spawn_point(self, player):
        points = self.data.get("spawn_points", ())
        if points:
            roster = sorted(self.players(), key=lambda p: p.id)
            index = roster.index(player) if player in roster else player.id
            return tuple(points[index % len(points)])
        return super().get_spawn_point(player)

    async def prepare_round(self):
        self.lives = {p: 3 for p in self.participants}
        self.holder = None
        self.fuse = 0.0
        self.pass_after = 0.0
        self.marker_at = self.status_at = 0.0

    async def begin_round(self):
        self.choose_holder()

    async def on_player_join(self, player):
        await super().on_player_join(player)
        if player in self.participants:
            self.lives.setdefault(player, 3)

    async def on_player_leave(self, player):
        if player is self.holder:
            self.pass_to(None)
        self.lives.pop(player, None)
        await super().on_player_leave(player)

    def choose_holder(self, exclude=None):
        candidates = sorted((p for p in self.alive() if p is not exclude), key=lambda p: p.id)
        turn = getattr(self, "holder_turn", 0)
        self.pass_to(candidates[turn % len(candidates)] if candidates else None)
        self.holder_turn = turn + 1
        self.fuse = self.clock() + 12

    def pass_to(self, player):
        from server.pickups import broadcast_drop, broadcast_pickup
        if self.holder is not None:
            broadcast_drop(self.server, self.holder, self.holder.position, (0, 0, 0))
        self.holder = player
        if player is not None:
            broadcast_pickup(self.server, player, int(C.INTEL_PICKUP), burdensome=False, state=player.team)
            self.moving_marker(1, player.position)
        else:
            self.clear_markers()
        self.pass_after = self.clock() + 0.65

    def modify_incoming_damage(self, player, amount, source, kill_type):
        if (self.active(player) and self.active(source) if source is not None else False):
            if source is self.holder and source.is_spade_tool() and self.clock() >= self.pass_after:
                self.pass_to(player)
        return 0

    async def handle_drop_pickup(self, player, position, velocity):
        # The curse cannot be discarded using the native drop key.
        return True

    def can_player_respawn(self, player):
        return super().can_player_respawn(player) and self.lives.get(player, 3) > 0

    async def on_player_death(self, player, killer, kill_type):
        if self.phase == "live" and player in self.participants:
            self.lose_life(player)

    def lose_life(self, player):
        self.lives[player] = max(0, self.lives.get(player, 3) - 1)
        if self.lives[player] == 0:
            self.eliminated.add(player)
            if player.alive:
                self.teleport(player, self.anchor("waiting"))
        if player is self.holder:
            self.pass_to(None)

    async def tick_live(self, now, dt):
        if self.holder not in self.alive():
            self.choose_holder()
        if self.holder and now >= self.fuse:
            loser = self.holder
            self.lose_life(loser)
            await self.broadcast_message(f"{loser.name}: {self.lives[loser]} lives left!")
            self.choose_holder(exclude=loser)
        if self.holder and now >= self.marker_at:
            self.marker_at = now + 0.1
            self.moving_marker(1, self.holder.position)
        if self.holder and now >= self.status_at:
            self.status_at = now + 1
            from server.announcements import build_overlay_message
            message = build_overlay_message(f"{self.holder.name} has the bomb | {max(0, math.ceil(self.fuse - now))}s | Hit someone with your shovel to pass")
            self.server.broadcast(message, reliable=False)
        remaining = [p for p in self.participants if self.lives.get(p, 0) > 0]
        if len(remaining) <= 1:
            await self.finish(tuple(remaining))

    async def timeout(self):
        await self.finish(self.leaders(self.lives))

    def clear_round_objects(self):
        if getattr(self, "holder", None) is not None:
            self.pass_to(None)
        super().clear_round_objects()


class OneInTheSpade(FunMode):
    mode_code = "one_in_the_spade"

    def __init__(self, server):
        super().__init__(server)
        self.bullets = {}

    def loadout(self, player):
        return ((int(C.PICKAXE_TOOL), int(C.PISTOL_TOOL)) if self.bullets.get(player, 1) > 0
                else (int(C.PICKAXE_TOOL),))

    def prepare_player_spawn(self, player):
        self.bullets[player] = 1
        super().prepare_player_spawn(player)

    async def prepare_round(self):
        self.bullets.clear()

    async def on_player_leave(self, player):
        self.bullets.pop(player, None)
        await super().on_player_leave(player)

    def allow_action(self, player, action, position=None):
        if action == "shoot" and not player.is_spade_tool() and self.bullets.get(player, 0) <= 0:
            return False
        return super().allow_action(player, action, position)

    def weapon_fired(self, player, tool):
        if tool == int(C.PISTOL_TOOL):
            self.bullets[player] = max(0, self.bullets.get(player, 0) - 1)
            self.server.queue_mode_event("on_weapon_fired", player)

    async def on_weapon_fired(self, player):
        if self.active(player):
            self.equip(player, self.loadout(player))

    def modify_incoming_damage(self, player, amount, source, kill_type):
        allowed = super().modify_incoming_damage(player, amount, source, kill_type)
        return max(player.health, allowed) if allowed and source is not None and source is not player else allowed

    async def on_player_death(self, player, killer, kill_type):
        if self.phase == "live" and killer is not None and killer is not player and killer in self.participants:
            self.award(killer)
            self.bullets[killer] = self.bullets.get(killer, 0) + 1
            self.equip(killer, self.loadout(killer))
            killer.restock_ammo(int(C.AMMO_CRATE))
            if self.points[killer] >= 15:
                await self.finish((killer,))


class BorrowedArsenal(FunMode):
    mode_code = "borrowed_arsenal"
    guns = (int(C.RIFLE_TOOL), int(C.SMG_TOOL), int(C.SHOTGUN_TOOL), int(C.SNIPER_TOOL), int(C.PISTOL_TOOL))

    def __init__(self, server):
        super().__init__(server)
        self.gun = {}
        self.spawn_count = {}

    def loadout(self, player):
        return (int(C.BLOCK_TOOL), int(C.PICKAXE_TOOL), self.gun.get(player, self.guns[0]))

    async def prepare_round(self):
        self.gun.clear()
        self.spawn_count.clear()

    def prepare_player_spawn(self, player):
        self.gun[player] = self.guns[min(len(self.guns) - 1, self.points.get(player, 0) // 2)]
        super().prepare_player_spawn(player)

    async def on_player_leave(self, player):
        self.gun.pop(player, None)
        self.spawn_count.pop(player, None)
        await super().on_player_leave(player)

    async def on_player_death(self, player, killer, kill_type):
        if self.phase == "live" and killer is not None and killer is not player and self.active(killer):
            self.award(killer)
            score = self.points[killer]
            if score >= len(self.guns) * 2:
                await self.finish((killer,), "Weapon ladder complete!")
                return
            self.gun[killer] = self.guns[score // 2]
            self.equip(killer, self.loadout(killer))
            killer.restock_ammo(int(C.AMMO_CRATE))
            await self.tell(killer, f"Weapon {score // 2 + 1}/{len(self.guns)}: {score % 2}/2 kills toward the next weapon.")


class Lockdown(FunMode):
    mode_code = "lockdown"

    def __init__(self, server):
        super().__init__(server)
        self.prisoners = set()
        self.rescue = {team: 0.0 for team in TEAMS}

    async def prepare_round(self):
        self.prisoners.clear()
        self.rescue = {team: 0.0 for team in TEAMS}
        for i, team in enumerate(TEAMS):
            self.marker(i, self.jail(team), team=team)

    def jail(self, team):
        return self.home(TEAM2 if team == TEAM1 else TEAM1)

    def get_spawn_point(self, player):
        return self.jail(player.team) if player in self.prisoners else super().get_spawn_point(player)

    def active(self, player):
        return super().active(player) and player not in self.prisoners

    def allow_action(self, player, action, position=None):
        return player not in self.prisoners and super().allow_action(player, action, position)

    async def on_player_death(self, player, killer, kill_type):
        if self.phase == "live" and player in self.participants:
            self.prisoners.add(player)

    async def tick_live(self, now, dt):
        for player in self.prisoners.intersection(self.players(alive=True)):
            if distance(player.position, self.jail(player.team)) > 4:
                self.teleport(player, self.jail(player.team))
        for team in TEAMS:
            roster = [p for p in self.participants if p.team == team]
            if roster and all(p in self.prisoners for p in roster):
                await self.finish((TEAM2 if team == TEAM1 else TEAM1,))
                return
            rescuers = [p for p in self.alive(team) if distance(p.position, self.jail(team)) < 6]
            enemies = [p for p in self.alive(TEAM2 if team == TEAM1 else TEAM1) if distance(p.position, self.jail(team)) < 6]
            self.rescue[team] = self.rescue[team] + dt if rescuers and not enemies else 0
            if self.rescue[team] >= 3:
                freed = [p for p in self.prisoners if p.team == team]
                self.prisoners.difference_update(freed)
                for player in freed:
                    self.reset_player(player)
                self.rescue[team] = 0
                if freed:
                    await self.broadcast_message(f"{self.label(team)} rescued {len(freed)} prisoners!")

    async def timeout(self):
        await self.finish(self.leaders({team: sum(p.team != team for p in self.prisoners) for team in TEAMS}))

    async def on_player_leave(self, player):
        self.prisoners.discard(player)
        await super().on_player_leave(player)


class Kingmaker(FunMode):
    mode_code = "kingmaker"

    async def prepare_round(self):
        self.score_at = 0.0
        self.owner = None
        self.marker(0, self.anchor("center"), radius=8)

    async def tick_live(self, now, dt):
        teams = {p.team for p in self.alive() if distance(p.position, self.anchor("center")) <= 8}
        owner = next(iter(teams)) if len(teams) == 1 else None
        if owner != self.owner:
            self.owner = owner
            self.marker(0, self.anchor("center"), team=owner, radius=8)
            await self.broadcast_message(f"{self.label(owner)} controls the hill!" if owner is not None else "Hill contested or empty: scoring paused.")
        if now >= self.score_at:
            self.score_at = now + 1
            if owner is not None:
                self.award(owner)
                if self.points[owner] >= 90:
                    await self.finish((owner,), "Hill secured!")
