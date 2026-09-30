"""What the legitimate clients do.

Every segment is a generator; one ``yield`` is one client frame. A segment
only presses keys, moves the mouse and uses tools the way a person does: the
client model turns that into packets with the stock cadence. Server-side
events a player cannot cause (teleports, damage, enemy explosions) go
through ``ctx`` and are what a server legitimately does to a player.

``PLANS`` lists, per class, which segments a client of that class runs; the
false-positive run starts one client per plan.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, Optional

import shared.constants as C
from server.game_constants import WEAPON_PROFILES

from .clientmodel import GUN_TOOLS, MELEE_TOOLS, ClientModel

FPS = 60

SOLDIER, SCOUT, ROCKETEER, MINER = 0, 1, 2, 3
ENGINEER, SPECIALIST, MEDIC = 12, 16, 17


def seconds(value: float) -> int:
    return max(1, int(round(float(value) * FPS)))


def direction(yaw_deg: float, pitch_deg: float = 0.0) -> tuple:
    """Unit aim vector; pitch > 0 looks down (VXL z grows downward)."""

    yaw = math.radians(yaw_deg)
    pitch = math.radians(max(-89.0, min(89.0, pitch_deg)))
    return (
        math.cos(yaw) * math.cos(pitch),
        math.sin(yaw) * math.cos(pitch),
        math.sin(pitch),
    )


@dataclass
class Context:
    """One client's view of the lab."""

    model: ClientModel
    court: object
    rng: random.Random
    lane: int
    teleport: Callable            # (position) -> None, a server teleport
    server_event: Callable        # (name, **kwargs) -> None
    yaw: float = 0.0
    pitch: float = 0.0
    log: Optional[list] = None

    # -- mouse ---------------------------------------------------------
    def look(self, yaw: Optional[float] = None, pitch: Optional[float] = None):
        if yaw is not None:
            self.yaw = float(yaw)
        if pitch is not None:
            self.pitch = max(-89.0, min(89.0, float(pitch)))
        self.model.intent.aim = direction(self.yaw, self.pitch)

    def look_at(self, point) -> None:
        x, y, z = self.model.position
        dx, dy, dz = point[0] - x, point[1] - y, point[2] - z
        flat = math.hypot(dx, dy)
        if flat < 1e-6 and abs(dz) < 1e-6:
            return
        self.look(math.degrees(math.atan2(dy, dx)),
                  math.degrees(math.atan2(dz, flat)))

    def keys(self, **held) -> None:
        intent = self.model.intent
        for name in ("up", "down", "left", "right", "jump", "crouch",
                     "sneak", "sprint", "primary", "secondary", "zoom",
                     "hover"):
            setattr(intent, name, bool(held.get(name, False)))

    def release(self) -> None:
        self.keys()

    def tool(self, tool_id: int) -> bool:
        if int(tool_id) not in self.model.loadout:
            return False
        self.model.intent.tool = int(tool_id)
        return True

    def selectable_tools(self) -> list:
        """Tools of this life the wheel can actually select."""

        return [
            tool for tool in self.model.loadout
            if tool in GUN_TOOLS or tool in MELEE_TOOLS or tool in _THROWABLES
            or tool in (int(C.BLOCK_TOOL), int(C.PREFAB_TOOL))
        ]

    # -- what the block tool's ghost would accept ---------------------------
    def solid(self, cell) -> bool:
        return bool(self.model.env.world_manager.get_solid(*cell))

    def can_place(self, cell) -> bool:
        """The stock ghost test: air, touching a block, in reach, in sight."""

        from server.combat_runtime import cell_visible

        world = self.model.env.world_manager
        cell = tuple(int(v) for v in cell)
        if world.get_solid(*cell) or not world.can_build(*cell):
            return False
        x, y, z = cell
        if not any(
            world.get_solid(x + dx, y + dy, z + dz)
            for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0),
                               (0, 0, 1), (0, 0, -1))
        ):
            return False
        eye = self.model.position
        centre = (x + 0.5, y + 0.5, z + 0.5)
        if math.dist(eye, centre) > float(getattr(C, "MAX_BLOCK_DISTANCE", 10)):
            return False
        # Never build into the own body.
        if abs(centre[0] - eye[0]) < 1.0 and abs(centre[1] - eye[1]) < 1.0 \
                and -0.6 < centre[2] - eye[2] < 3.2:
            return False
        return bool(cell_visible(world, [eye], cell))


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def wait(ctx: Context, duration: float):
    for _ in range(seconds(duration)):
        yield


def settle(ctx: Context, duration: float = 0.5):
    ctx.release()
    yield from wait(ctx, duration)


def turn(ctx: Context, yaw: float, pitch: Optional[float] = None,
         duration: float = 0.25):
    """Mouse sweep to ``yaw``/``pitch`` with an ease-in/ease-out profile."""

    frames = seconds(duration)
    start_yaw, start_pitch = ctx.yaw, ctx.pitch
    delta = (float(yaw) - start_yaw + 180.0) % 360.0 - 180.0
    end_pitch = start_pitch if pitch is None else float(pitch)
    for index in range(1, frames + 1):
        t = index / frames
        eased = t * t * (3.0 - 2.0 * t)
        ctx.look(start_yaw + delta * eased,
                 start_pitch + (end_pitch - start_pitch) * eased)
        yield


def teleport(ctx: Context, position, yaw: float = 0.0):
    """The server moves the player (admin teleport, mode teleport)."""

    ctx.release()
    ctx.teleport(position)
    ctx.look(yaw, 0.0)
    # The client learns it from its next self row.
    yield from wait(ctx, 0.75)


def move(ctx: Context, duration: float, **held):
    ctx.keys(**held)
    yield from wait(ctx, duration)


# ---------------------------------------------------------------------------
# movement segments
# ---------------------------------------------------------------------------


def run_and_strafe(ctx: Context, duration: float = 8.0):
    yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    rng = ctx.rng
    frames = seconds(duration)
    heading = 0.0
    index = 0
    while index < frames:
        span = rng.randint(12, 70)
        choice = rng.random()
        held = {"up": True}
        if choice < 0.2:
            held = {"up": True, "sprint": True}
        elif choice < 0.35:
            held = {"up": True, "left": True}
        elif choice < 0.5:
            held = {"up": True, "right": True}
        elif choice < 0.6:
            held = {"down": True}
        elif choice < 0.7:
            held = {"left": True}
        elif choice < 0.8:
            held = {"right": True, "sneak": True}
        elif choice < 0.9:
            held = {"up": True, "crouch": True}
        if rng.random() < 0.3:
            held["jump"] = True
        # Stay inside the lane area: turn around near its ends.
        x = ctx.model.position[0] - ctx.court.x
        if x > 84:
            heading = 180.0
        elif x < 36:
            heading = 0.0
        target = heading + rng.uniform(-25.0, 25.0)
        sweep = turn(ctx, target, rng.uniform(-10.0, 10.0),
                     duration=rng.uniform(0.08, 0.4))
        for frame in range(span):
            ctx.keys(**held)
            if held.get("jump") and frame > rng.randint(2, 6):
                ctx.model.intent.jump = False
            next(sweep, None)
            yield
            index += 1
    yield from settle(ctx)


def sprint_jumps(ctx: Context, jumps: int = 8):
    yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    for index in range(jumps):
        yield from move(ctx, ctx.rng.uniform(0.3, 0.7), up=True, sprint=True)
        tap = ctx.rng.randint(1, 8)
        yield from move(ctx, tap / FPS, up=True, sprint=True, jump=True)
        yield from move(ctx, 0.6, up=True, sprint=True)
        if ctx.model.position[0] - ctx.court.x > 80:
            yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    yield from settle(ctx)


def crouch_dance(ctx: Context, duration: float = 4.0):
    yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=90.0)
    frames = seconds(duration)
    index = 0
    while index < frames:
        hold = ctx.rng.randint(1, 20)
        crouched = ctx.rng.random() < 0.5
        moving = ctx.rng.random() < 0.6
        for _ in range(hold):
            ctx.keys(crouch=crouched, up=moving and index % 120 < 60,
                     down=moving and index % 120 >= 60)
            yield
            index += 1
    yield from settle(ctx)


def stairs_and_drop(ctx: Context):
    """Climb the staircase, cross the platform, walk off its far edge."""

    x, y, z = ctx.court.stairs_foot
    yield from teleport(ctx, (x, y + (ctx.lane % 5) - 2, z), yaw=0.0)
    ctx.look(0.0, 0.0)
    yield from move(ctx, 5.5, up=True)
    yield from move(ctx, 2.5, up=True, sprint=True)   # off the edge, 10 blocks
    yield from settle(ctx, 1.5)


def high_fall(ctx: Context, height: float = 28.0, chute: bool = False):
    """Dropped from the sky by the server; optionally under a parachute."""

    yield from teleport(
        ctx, ctx.court.sky(40 + ctx.lane * 4, 30, height), yaw=0.0
    )
    if chute and ctx.model.parachute_id:
        yield from wait(ctx, 0.25)
        # The deploy key is a short tap (a few frames).
        for _ in range(ctx.rng.randint(2, 6)):
            ctx.keys(hover=True, up=True)
            yield
        yield from move(ctx, 4.0, up=True)
    else:
        yield from move(ctx, 3.0, up=True)
    yield from settle(ctx, 1.0)


def water(ctx: Context):
    yield from teleport(ctx, ctx.court.pool_edge, yaw=180.0)
    ctx.look(180.0, 0.0)
    yield from move(ctx, 2.0, up=True)             # drop into the pool
    yield from move(ctx, 1.5, up=True, left=True)  # wade
    yield from move(ctx, 0.5, jump=True, up=True)
    yield from move(ctx, 1.5, down=True, right=True)
    yield from move(ctx, 0.6, crouch=True, up=True)
    # Walk back out over the steps on the +x side of the pool.
    sx, sy, _sz = ctx.court.stand(27, 46)
    ctx.look_at((sx, sy, ctx.model.position[2]))
    yield from move(ctx, 2.0, up=True)
    ctx.look(0.0, 0.0)
    yield from move(ctx, 6.0, up=True)
    yield from settle(ctx)


def jetpack(ctx: Context):
    if not ctx.model.jetpack_id:
        return
    yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    # Full burn to exhaustion, then fall.
    yield from move(ctx, 4.0, jump=True, up=True)
    yield from move(ctx, 2.5, up=True)
    yield from settle(ctx, 2.5)
    # Short hops.
    for _ in range(5):
        yield from move(ctx, ctx.rng.uniform(0.35, 0.9), jump=True, up=True)
        yield from move(ctx, ctx.rng.uniform(0.2, 0.8), up=True)
    yield from settle(ctx, 3.0)
    # Feathering: tap, tap, tap while strafing.
    for _ in range(14):
        yield from move(ctx, ctx.rng.uniform(0.05, 0.45), jump=True,
                        left=ctx.rng.random() < 0.5)
        yield from move(ctx, ctx.rng.uniform(0.05, 0.3),
                        right=ctx.rng.random() < 0.5)
    yield from settle(ctx, 3.0)
    if ctx.model.jetpack_id == int(C.JETPACK_UGCBUILDER):
        yield from move(ctx, 2.0, hover=True, up=True)
        yield from move(ctx, 1.0, hover=True, crouch=True)
        yield from settle(ctx, 1.0)


def server_teleports(ctx: Context, count: int = 6):
    """Teleports in quick succession while the player keeps moving/firing."""

    court = ctx.court
    gun = next((t for t in ctx.model.loadout if t in GUN_TOOLS), None)
    if gun is not None:
        ctx.tool(gun)
    for index in range(count):
        spot = court.stand(
            36 + ctx.rng.randint(0, 40), 22 + ctx.rng.randint(0, 30)
        )
        ctx.teleport(spot)
        for frame in range(ctx.rng.randint(20, 70)):
            ctx.keys(up=True, sprint=frame % 40 < 20, primary=frame % 9 == 0)
            yield
    yield from settle(ctx)


# ---------------------------------------------------------------------------
# combat segments
# ---------------------------------------------------------------------------


def _targets(ctx: Context) -> list:
    own = ctx.model.team
    points = []
    for player_id, entry in ctx.model.roster.items():
        if player_id == ctx.model.player_id or not entry.get("alive"):
            continue
        if entry.get("team") == own or entry.get("position") is None:
            continue
        points.append(entry["position"])
    return points


def empty_the_gun(ctx: Context, tool: int, *, cycles: int = 2,
                  moving: bool = True):
    """Hold the trigger through whole magazines, reloads included."""

    if not ctx.tool(tool):
        return
    profile = WEAPON_PROFILES[tool]
    yield from wait(ctx, 0.3)
    clip_time = float(profile.fire_interval) * int(profile.clip_size)
    reload_time = float(profile.reload_time) * (
        int(profile.clip_size) if getattr(profile, "clip_reload", False) else 1
    )
    budget = seconds(min(45.0, cycles * (clip_time + reload_time) + 2.0))
    targets = _targets(ctx)
    sweep = None
    for frame in range(budget):
        if frame % 45 == 0:
            if targets and ctx.rng.random() < 0.7:
                point = ctx.rng.choice(targets)
                x, y, z = ctx.model.position
                yaw = math.degrees(math.atan2(point[1] - y, point[0] - x))
                pitch = math.degrees(math.atan2(
                    point[2] - z, math.hypot(point[0] - x, point[1] - y)
                ))
                sweep = turn(ctx, yaw + ctx.rng.uniform(-3, 3),
                             pitch + ctx.rng.uniform(-2, 2),
                             duration=ctx.rng.uniform(0.06, 0.3))
            else:
                sweep = turn(ctx, ctx.rng.uniform(-180, 180),
                             ctx.rng.uniform(-25, 25),
                             duration=ctx.rng.uniform(0.05, 0.5))
        held = {"primary": True}
        if moving:
            phase = (frame // 50) % 4
            held.update({0: {"left": True}, 1: {"right": True},
                         2: {"up": True}, 3: {"down": True}}[phase])
            if frame % 140 < 3:
                held["jump"] = True
            if 60 < frame % 200 < 90:
                held["crouch"] = True
        if tool in (int(C.SNIPER_TOOL), int(C.SNIPER2_TOOL)) or frame % 300 > 200:
            held["zoom"] = True
        ctx.keys(**held)
        if sweep is not None:
            next(sweep, None)
        yield
    yield from settle(ctx, 0.4)


def tap_fire(ctx: Context, tool: int, shots: int = 12):
    """Single clicks, as fast as a finger goes (8 to 12 clicks a second)."""

    if not ctx.tool(tool):
        return
    yield from wait(ctx, 0.3)
    for _ in range(shots):
        for _ in range(ctx.rng.randint(1, 3)):
            ctx.keys(primary=True)
            yield
        for _ in range(ctx.rng.randint(3, 6)):
            ctx.keys()
            yield
    yield from settle(ctx, 0.3)


def flick_shots(ctx: Context, tool: int, shots: int = 10):
    """Fast mouse flicks with the click at the end of the flick."""

    if not ctx.tool(tool):
        return
    yield from wait(ctx, 0.3)
    profile = WEAPON_PROFILES[tool]
    for _ in range(shots):
        target_yaw = ctx.yaw + ctx.rng.choice((-1, 1)) * ctx.rng.uniform(30, 170)
        frames = ctx.rng.randint(3, 9)
        sweep = turn(ctx, target_yaw, ctx.rng.uniform(-20, 20),
                     duration=frames / FPS)
        for index in range(frames):
            next(sweep, None)
            # The click lands on the last frames of the flick, or just after.
            ctx.keys(primary=index >= frames - ctx.rng.randint(1, 2))
            yield
        ctx.keys(primary=True)
        yield
        ctx.keys()
        yield from wait(ctx, float(profile.fire_interval) + ctx.rng.uniform(0.0, 0.3))
    yield from settle(ctx, 0.3)


def reload_cancel(ctx: Context, tool: int):
    """Manual reloads at every point of the magazine and tool swaps mid-reload."""

    if not ctx.tool(tool):
        return
    yield from wait(ctx, 0.3)
    profile = WEAPON_PROFILES[tool]
    for _ in range(4):
        yield from move(ctx, float(profile.fire_interval) * ctx.rng.randint(1, 4),
                        primary=True)
        ctx.release()
        ctx.model.queue("reload")
        yield from wait(ctx, float(profile.reload_time) * ctx.rng.uniform(0.3, 1.3))
        yield from move(ctx, 0.4, primary=True)
    yield from settle(ctx, 0.3)


def weapons(ctx: Context):
    yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    for tool in [t for t in ctx.model.loadout if t in GUN_TOOLS]:
        if tool == int(C.MG_TOOL):
            continue
        yield from empty_the_gun(ctx, tool)
        yield from tap_fire(ctx, tool)
        yield from flick_shots(ctx, tool)
        yield from reload_cancel(ctx, tool)
        if ctx.model.position[0] - ctx.court.x > 86 or not ctx.model.alive:
            yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)


def shooting_in_flight(ctx: Context):
    """Fire while falling, jetpacking and landing."""

    gun = next((t for t in ctx.model.loadout if t in GUN_TOOLS), None)
    if gun is None or not ctx.tool(gun):
        return
    yield from teleport(ctx, ctx.court.sky(40 + ctx.lane * 4, 34, 18.0), yaw=0.0)
    yield from move(ctx, 2.5, primary=True, up=True)
    if ctx.model.jetpack_id:
        yield from move(ctx, 2.5, primary=True, jump=True, up=True)
        yield from move(ctx, 2.0, primary=True, left=True)
    yield from settle(ctx, 1.0)


def rapid_tool_switching(ctx: Context, duration: float = 5.0):
    tools = ctx.selectable_tools()
    if len(tools) < 2:
        return
    yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    for frame in range(seconds(duration)):
        if frame % ctx.rng.randint(2, 9) == 0:
            ctx.tool(ctx.rng.choice(tools))
        ctx.keys(primary=frame % 3 != 0, up=frame % 80 < 40)
        yield
    yield from settle(ctx)


def melee_and_dig(ctx: Context):
    tools = [t for t in ctx.model.loadout if t in MELEE_TOOLS]
    if not tools:
        return
    yield from teleport(ctx, ctx.court.wall_front, yaw=0.0)
    for tool in tools:
        if not ctx.tool(tool):
            continue
        yield from wait(ctx, 0.3)
        # The wall four blocks ahead, then the ground at the feet.
        x, y, z = ctx.court.stand(26, 12)
        ctx.look(0.0, 5.0)
        yield from move(ctx, 1.0, up=True)
        yield from move(ctx, 4.0, primary=True)
        ctx.look(0.0, 70.0)
        yield from move(ctx, 3.0, primary=True)
        yield from move(ctx, 3.0, secondary=True)
        # Both buttons at once: still one swing per cooldown.
        yield from move(ctx, 2.0, primary=True, secondary=True)
        ctx.look(180.0, 60.0)
        yield from move(ctx, 2.0, primary=True, down=True)
        yield from teleport(ctx, ctx.court.wall_front, yaw=0.0)
    yield from settle(ctx)


_THROWABLES = {
    int(C.GRENADE_TOOL): (50.0, 2.5, 1.0),
    int(getattr(C, "ANTIPERSONNEL_GRENADE_TOOL", 32)): (50.0, 2.5, 1.0),
    int(getattr(C, "MOLOTOV_TOOL", 33)): (40.0, 10.0, 1.0),
    int(getattr(C, "CLASSIC_GRENADE_TOOL", 31)): (35.0, 3.0, 1.0),
    int(getattr(C, "CHEMICALBOMB_TOOL", 54)): (50.0, 10.0, 1.0),
    int(getattr(C, "STICKY_GRENADE_TOOL", 57)): (50.0, 5.0, 1.0),
    int(C.RPG_TOOL): (75.0, 10.0, 2.0),
    int(C.RPG2_TOOL): (150.0, 10.0, 2.0),
    int(C.DRILLGUN_TOOL): (40.0, 10.0, 2.0),
    int(getattr(C, "SNOWBLOWER_TOOL", 29)): (50.0, 10.0, 0.5),
    int(getattr(C, "GRENADE_LAUNCHER_WEAPON_TOOL", 55)): (75.0, 2.5, 1.0),
    int(getattr(C, "MINE_LAUNCHER_TOOL", 58)): (75.0, 10.0, 1.0),
}


def throwables(ctx: Context):
    tools = [t for t in ctx.model.loadout if t in _THROWABLES]
    if not tools:
        return
    yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    for tool in tools:
        if not ctx.tool(tool):
            continue
        speed, fuse, interval = _THROWABLES[tool]
        yield from wait(ctx, 0.5)
        for index in range(8):
            ctx.look(ctx.rng.uniform(-40, 40), ctx.rng.uniform(-35, 10))
            held = {"up": index % 2 == 0, "sprint": False,
                    "jump": index % 4 == 3}
            ctx.keys(**held)
            # Hold the trigger until the tool lets the round go.
            for _ in range(seconds(6.0)):
                if ctx.model.can_throw(tool):
                    break
                yield
            else:
                break
            # A cooked grenade leaves with whatever fuse is left.
            ctx.model.queue(
                "throw", tool=tool, speed=speed,
                fuse=fuse * ctx.rng.uniform(0.15, 1.0), interval=interval,
            )
            yield from wait(ctx, ctx.rng.uniform(0.05, 0.4))
        if ctx.model.position[0] - ctx.court.x > 80 or not ctx.model.alive:
            yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    yield from settle(ctx, 3.0)


def blast_jump(ctx: Context):
    """Explosives at the own feet: the blast throws the player."""

    tools = [t for t in ctx.model.loadout if t in _THROWABLES]
    rockets = [t for t in tools if t in (int(C.RPG_TOOL), int(C.RPG2_TOOL))]
    tool = (rockets or tools or [None])[0]
    if tool is None or not ctx.tool(tool):
        return
    yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    speed, fuse, interval = _THROWABLES[tool]
    gun = next((t for t in ctx.model.loadout if t in GUN_TOOLS), None)
    for _ in range(3):
        if not ctx.tool(tool):
            return
        for _ in range(seconds(6.0)):
            if ctx.model.can_throw(tool):
                break
            yield
        else:
            return
        ctx.look(0.0, 80.0)
        ctx.keys(jump=True, up=True)
        ctx.model.queue("throw", tool=tool, speed=speed,
                        fuse=min(fuse, 0.6), interval=interval)
        yield
        if gun is not None:
            ctx.tool(gun)
        # Keep moving and shooting while the blast carries the body.
        ctx.look(0.0, 0.0)
        yield from move(ctx, 2.5, up=True, primary=gun is not None)
        yield from settle(ctx, 1.5)
        if not ctx.model.alive:
            yield from wait(ctx, 1.0)
        yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)


def enemy_explosions(ctx: Context, count: int = 5):
    """Enemy grenades landing near the player while it moves and fires."""

    yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    gun = next((t for t in ctx.model.loadout if t in GUN_TOOLS), None)
    if gun is not None:
        ctx.tool(gun)
    for _ in range(count):
        yield from move(ctx, ctx.rng.uniform(0.5, 1.5), up=True)
        ctx.server_event("grenade_near", distance=ctx.rng.uniform(2.5, 5.0))
        yield from move(ctx, 3.0, up=True, primary=gun is not None,
                        left=ctx.rng.random() < 0.5)
        if ctx.model.position[0] - ctx.court.x > 80 or not ctx.model.alive:
            yield from wait(ctx, 1.0)
            yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    yield from settle(ctx)


def damage_and_death(ctx: Context):
    """Hit by an enemy a few times, then killed; the respawn follows."""

    yield from teleport(ctx, ctx.court.lane(ctx.lane), yaw=0.0)
    for _ in range(3):
        yield from move(ctx, 0.7, up=True, primary=True)
        ctx.server_event("hurt", amount=20)
    yield from move(ctx, 0.5, up=True, primary=True)
    ctx.server_event("kill")
    yield from wait(ctx, 1.0)


# ---------------------------------------------------------------------------
# building segments
# ---------------------------------------------------------------------------


def building(ctx: Context, blocks: int = 14):
    if not ctx.tool(int(C.BLOCK_TOOL)):
        return
    court = ctx.court
    yield from teleport(ctx, court.stand(10 + ctx.lane * 2, 16), yaw=0.0)
    yield from wait(ctx, 0.4)
    ctx.model.queue("color", value=ctx.rng.randint(0, 0xFFFFFF))
    base_u = 13 + ctx.lane * 2
    for index in range(blocks):
        # A column in front of the feet, one block per click.
        cell = court.cell(base_u, 16, up=1 + index % 3)
        ctx.look_at((cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.5))
        ctx.keys(primary=True)
        if ctx.can_place(cell):
            ctx.model.queue("block_line", start=cell, end=cell)
        yield from wait(ctx, 0.5 + ctx.rng.uniform(0.0, 0.2))
        if index % 3 == 2:
            base_u += 0
            # Knock the column down again with the spade so it can be rebuilt.
            spade = next((t for t in ctx.model.loadout if t in MELEE_TOOLS), None)
            if spade is not None and ctx.tool(spade):
                yield from wait(ctx, 0.3)
                for up in (3, 2, 1):
                    target = court.cell(base_u, 16, up=up)
                    ctx.look_at((target[0] + 0.5, target[1] + 0.5, target[2] + 0.5))
                    yield from move(ctx, 2.6, primary=True)
                ctx.release()
                ctx.tool(int(C.BLOCK_TOOL))
                yield from wait(ctx, 0.4)
    # A dragged line along the ground row next to the player.
    start = court.cell(base_u, 18, up=1)
    end = court.cell(base_u + 5, 18, up=1)
    ctx.look_at((end[0] + 0.5, end[1] + 0.5, end[2] + 0.5))
    line = [court.cell(base_u + step, 18, up=1) for step in range(6)]
    free = [cell for cell in line if not ctx.solid(cell)]
    if free and ctx.can_place(end) and ctx.can_place(start):
        ctx.model.queue("block_line", start=start, end=end, cost=len(free))
    yield from wait(ctx, 0.8)
    yield from settle(ctx)


def bridge(ctx: Context, length: int = 10):
    """Build out over the pool while walking on what was just built."""

    if not ctx.tool(int(C.BLOCK_TOOL)):
        return
    court = ctx.court
    v = 50 + (ctx.lane % 6)
    yield from teleport(ctx, court.stand(30, v), yaw=180.0)
    yield from wait(ctx, 0.4)
    for index in range(length):
        cell = court.cell(27 - index, v, up=0)
        ctx.look_at((cell[0] + 0.5, cell[1] + 0.5, cell[2] + 0.2))
        if ctx.can_place(cell):
            ctx.model.queue("block_line", start=cell, end=cell)
        yield from wait(ctx, 0.55)
        ctx.look(180.0, 35.0)
        yield from move(ctx, 0.22, up=True, sneak=True)
        ctx.release()
    yield from settle(ctx)


SEGMENTS = {
    "run_and_strafe": run_and_strafe,
    "sprint_jumps": sprint_jumps,
    "crouch_dance": crouch_dance,
    "stairs_and_drop": stairs_and_drop,
    "high_fall": high_fall,
    "parachute": lambda ctx: high_fall(ctx, height=34.0, chute=True),
    "water": water,
    "jetpack": jetpack,
    "server_teleports": server_teleports,
    "weapons": weapons,
    "shooting_in_flight": shooting_in_flight,
    "rapid_tool_switching": rapid_tool_switching,
    "melee_and_dig": melee_and_dig,
    "throwables": throwables,
    "blast_jump": blast_jump,
    "enemy_explosions": enemy_explosions,
    "damage_and_death": damage_and_death,
    "building": building,
    "bridge": bridge,
}

MOVEMENT = ("run_and_strafe", "sprint_jumps", "crouch_dance",
            "stairs_and_drop", "water", "server_teleports")


@dataclass(frozen=True)
class Plan:
    name: str
    class_id: int
    loadout: tuple
    segments: tuple
    prefabs: tuple = ()


PLANS = (
    Plan("soldier", SOLDIER, (8, 12, 72, 2),
         MOVEMENT + ("high_fall", "parachute", "weapons", "shooting_in_flight",
                     "rapid_tool_switching", "melee_and_dig", "throwables",
                     "blast_jump", "enemy_explosions", "damage_and_death",
                     "building", "bridge")),
    Plan("soldier2", SOLDIER, (60, 13, 11, 1),
         ("weapons", "throwables", "blast_jump", "melee_and_dig",
          "run_and_strafe", "enemy_explosions", "building")),
    Plan("scout", SCOUT, (18, 17, 20, 0),
         MOVEMENT + ("weapons", "rapid_tool_switching", "melee_and_dig",
                     "damage_and_death", "high_fall")),
    Plan("scout2", SCOUT, (19, 53, 56, 1),
         ("weapons", "run_and_strafe", "melee_and_dig", "enemy_explosions")),
    Plan("rocketeer", ROCKETEER, (7, 11, 67, 2),
         MOVEMENT + ("jetpack", "shooting_in_flight", "weapons", "throwables",
                     "enemy_explosions", "high_fall", "building")),
    Plan("rocketeer2", ROCKETEER, (7, 16, 66, 0),
         ("jetpack", "shooting_in_flight", "weapons", "server_teleports",
          "rapid_tool_switching", "bridge")),
    Plan("miner", MINER, (9, 14, 21, 3),
         MOVEMENT + ("weapons", "melee_and_dig", "throwables", "building",
                     "bridge", "damage_and_death")),
    Plan("miner2", MINER, (10, 63, 59, 3),
         ("weapons", "melee_and_dig", "run_and_strafe", "building")),
    Plan("engineer", ENGINEER, (7, 29, 68, 0),
         MOVEMENT + ("jetpack", "shooting_in_flight", "weapons", "throwables",
                     "building", "bridge")),
    Plan("engineer2", ENGINEER, (7, 58, 64, 0),
         ("weapons", "throwables", "run_and_strafe", "melee_and_dig")),
    Plan("specialist", SPECIALIST, (62, 55, 54, 50),
         MOVEMENT + ("weapons", "throwables", "melee_and_dig",
                     "rapid_tool_switching", "enemy_explosions")),
    Plan("specialist2", SPECIALIST, (7, 53, 57, 2),
         ("weapons", "throwables", "run_and_strafe", "damage_and_death")),
    Plan("medic", MEDIC, (61, 52, 51, 49),
         MOVEMENT + ("weapons", "melee_and_dig", "rapid_tool_switching",
                     "enemy_explosions", "building")),
    Plan("medic2", MEDIC, (10, 52, 51, 0),
         ("weapons", "melee_and_dig", "run_and_strafe", "high_fall")),
)

PLAN_BY_NAME = {plan.name: plan for plan in PLANS}


def script(ctx: Context, plan: Plan, segments=None, *, repeat: bool = True):
    """The client's whole session; survives deaths and class changes."""

    names = tuple(segments or plan.segments)
    while True:
        for name in names:
            segment = SEGMENTS[name]
            while not ctx.model.alive:
                ctx.release()
                yield
            if ctx.log is not None:
                ctx.log.append((ctx.model.stats.frames, name))
            runner = segment(ctx)
            if runner is None:
                continue
            for _ in runner:
                yield
                if not ctx.model.alive:
                    break
        if not repeat:
            break
    ctx.release()
    while True:
        yield
