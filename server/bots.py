"""Server-side dev bots.

A bot is a real Player added to ``server.players`` (so it is simulated by the
shared tick loop and emitted in every real client's WorldUpdate rows) but NEVER
to ``server.connections`` (it has no ENet peer — the broadcast/WorldUpdate send
loops iterate connections and would crash on a peerless bot). A tiny
``_BotConnection`` stub gives the Player the ``.server`` back-reference that
``die()``/``damage()``/world lookups need, plus a no-op ``send``.

The AI is deliberately *human-like* rather than an aimbot. Instead of snapping
its orientation onto the target and dealing guaranteed damage through the raw
``damage()`` path, a bot:

  * keeps a stored heading and *slews* it toward the target at a capped turn
    rate, with a persistent gaussian aim-error term (re-rolled periodically) so
    shots miss sometimes;
  * fires through the REAL combat path
    (``CombatSystem._resolve_hitscan``) after gating on ``consume_shot`` — so
    line-of-sight, terrain, range, weapon-profile damage, headshots, ammo and
    reload all apply exactly as they do for a human;
  * has a reaction delay on newly acquiring a target;
  * bursts + pauses (for automatic weapons) and reloads when the clip empties;
  * decouples facing from movement: it faces the enemy for aim but strafes to
    hold a stand-off band, advancing/retreating and occasionally juking;
  * only acquires/keeps a target it actually has line-of-sight to (with a short
    commitment window so it doesn't twitch between equidistant enemies).

Not netcode-touching: bots ride the same aoslib physics engine and the same
combat/death/respawn paths as real players.
"""
from __future__ import annotations

import logging
import math
import random
import time
from typing import TYPE_CHECKING, List, Optional

from server.combat_runtime import get_combat_system
from server.game_constants import DEFAULT_WEAPON_TOOL, TEAM1, TEAM2

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player

logger = logging.getLogger(__name__)

# --- AI tuning (blocks / seconds / radians) --------------------------------
_ENGAGE_RANGE = 40.0      # planar distance at which a bot will open fire
_STOP_RANGE = 8.0         # inner edge of the stand-off band (retreat if closer)
_ADVANCE_RANGE = 22.0     # outer edge of the stand-off band (advance if farther)
_LOSE_RANGE = 64.0        # drop a committed target once this far away

_MAX_TURN_RATE = 3.5      # rad/s ceiling on how fast a bot can rotate (~200deg/s)
_AIM_ERROR_STDDEV = 0.06  # rad gaussian aim jitter (persistent, re-rolled)
_AIM_ERROR_INTERVAL = 0.45  # seconds a given aim-error sample persists
_FIRE_AIM_CONE = 0.20     # only pull the trigger once heading is within this of target

_REACTION_MIN = 0.18      # seconds before a freshly-acquired target can be shot at
_REACTION_MAX = 0.32

_BURST_MIN = 3            # automatic-weapon burst length (shots)
_BURST_MAX = 6
_BURST_PAUSE_MIN = 0.25   # pause between automatic bursts
_BURST_PAUSE_MAX = 0.6

_STRAFE_FLIP_MIN = 0.6    # seconds between strafe-direction flips
_STRAFE_FLIP_MAX = 1.4
_JUKE_CHANCE = 0.04       # per-decision chance to briefly juke/jump
_TARGET_RECHECK = 0.5     # seconds to keep re-validating LOS before dropping a target

_WANDER_INTERVAL = 2.0    # seconds between random headings when no target

# A weapon is treated as "automatic" (bursts) when it fires fast enough that a
# human would hold the trigger rather than tap it.
_AUTO_FIRE_THRESHOLD = 0.2  # fire_interval <= this => automatic


class _BotConnection:
    """Peerless stand-in so a bot Player resolves ``connection.server`` (used by
    die()/damage()/world lookups) without being a real ENet connection."""

    def __init__(self, server: "BattleSpadesServer"):
        self.server = server
        self.player: Optional["Player"] = None

    def send(self, data, reliable: bool = True, prefix: int = 0x30):
        # Bots have no peer; swallow anything aimed at them.
        return None


class _BotState:
    """Per-bot mutable AI state (keyed by bot.id in BotManager)."""

    __slots__ = (
        "heading",          # current facing yaw (radians), slewed each tick
        "aim_error",        # persistent gaussian aim offset (radians)
        "aim_error_until",  # when to re-roll aim_error
        "target_id",        # currently committed target id (or None)
        "target_last_seen", # monotonic time we last had LOS to the target
        "next_shot",        # earliest monotonic time we may fire again
        "burst_left",       # shots remaining in the current automatic burst
        "strafe_dir",       # -1 / 0 / +1 current strafe direction
        "next_strafe_flip", # when to reconsider strafe direction
        "wander_heading",   # heading used while no target
        "next_wander",      # when to re-roll wander_heading
    )

    def __init__(self, heading: float):
        self.heading = heading
        self.aim_error = 0.0
        self.aim_error_until = 0.0
        self.target_id: Optional[int] = None
        self.target_last_seen = 0.0
        self.next_shot = 0.0
        self.burst_left = 0
        self.strafe_dir = 0
        self.next_strafe_flip = 0.0
        self.wander_heading = heading
        self.next_wander = 0.0


class BotManager:
    def __init__(self, server: "BattleSpadesServer"):
        self.server = server
        self.bots: List["Player"] = []
        self._state: dict[int, _BotState] = {}

    # ------------------------------------------------------------------ spawn
    def add_bot(self, team: int, name: Optional[str] = None, class_id: int = 0) -> Optional["Player"]:
        from server.player import Player

        pid = self.server.get_next_player_id()
        if pid < 0:
            logger.warning("BotManager: no free player id for a bot")
            return None
        name = name or f"Bot{pid}"
        conn = _BotConnection(self.server)
        bot = Player(pid, name, team, DEFAULT_WEAPON_TOOL, conn)
        conn.player = bot
        bot.is_bot = True
        bot.class_id = class_id

        self.server.players[pid] = bot
        self.server.teams[team].add_player(bot)

        spawn = self.server.world_manager.get_spawn_point(team)
        bot.spawn(spawn[0], spawn[1], spawn[2])
        self.server._broadcast_create_player(bot, spawn)

        self._state[pid] = _BotState(random.uniform(-math.pi, math.pi))

        self.bots.append(bot)
        logger.info("Bot spawned: %s (id=%d, team=%d)", name, pid, team)
        return bot

    def spawn_initial(self, count: int) -> None:
        """Spawn `count` bots split across the two teams."""
        for i in range(count):
            team = TEAM1 if i % 2 == 0 else TEAM2
            self.add_bot(team)

    # ------------------------------------------------------------------- tick
    def update(self, dt: float) -> None:
        """Per-tick AI. Sets each bot's inputs/orientation (the shared
        simulate_tick loop then steps its physics) and fires through the real
        combat path (LOS/terrain/range/ammo all apply)."""
        if not self.bots:
            return
        now = time.monotonic()
        for bot in self.bots:
            state = self._state.get(bot.id)
            if state is None:
                state = self._state[bot.id] = _BotState(random.uniform(-math.pi, math.pi))

            if not bot.alive or not bot.spawned:
                # idle: no input; drop any committed target so re-acquisition
                # (and the reaction delay) fires cleanly on respawn.
                state.target_id = None
                continue

            target = self._select_target(bot, state, now)
            if target is None:
                self._wander(bot, state, now, dt)
                continue

            self._engage(bot, state, target, now, dt)

    def _engage(self, bot: "Player", state: "_BotState", target: "Player",
                now: float, dt: float) -> None:
        dx = target.x - bot.x
        dy = target.y - bot.y
        dist = math.hypot(dx, dy)

        # --- AIM: slew heading toward the target with a persistent error term.
        target_yaw = math.atan2(dy, dx)
        if now >= state.aim_error_until:
            state.aim_error = random.gauss(0.0, _AIM_ERROR_STDDEV)
            state.aim_error_until = now + _AIM_ERROR_INTERVAL
        aim_yaw = target_yaw + state.aim_error
        self._slew_heading(state, aim_yaw, dt)
        # Aim slightly downward at the target's chest for a plausible shot line.
        self._apply_heading(bot, state, target)

        # --- MOVEMENT: hold a stand-off band, strafing, decoupled from facing.
        self._drive_movement(bot, state, target_yaw, dist, now)

        # --- SHOOTING: only once reaction has elapsed and the heading is on
        # target; routed through the real combat path so LOS/terrain/range/
        # ammo/damage all apply.
        aim_off = abs(self._wrap(aim_yaw - state.heading))
        if (dist <= _ENGAGE_RANGE
                and now >= state.next_shot
                and aim_off <= _FIRE_AIM_CONE):
            self._fire(bot, state, now)

    # --------------------------------------------------------------- aiming
    @staticmethod
    def _wrap(angle: float) -> float:
        """Wrap an angle to (-pi, pi]."""
        while angle > math.pi:
            angle -= 2.0 * math.pi
        while angle <= -math.pi:
            angle += 2.0 * math.pi
        return angle

    def _slew_heading(self, state: "_BotState", target_yaw: float, dt: float) -> None:
        delta = self._wrap(target_yaw - state.heading)
        max_step = _MAX_TURN_RATE * dt
        if delta > max_step:
            delta = max_step
        elif delta < -max_step:
            delta = -max_step
        state.heading = self._wrap(state.heading + delta)

    def _apply_heading(self, bot: "Player", state: "_BotState",
                       target: Optional["Player"] = None) -> None:
        ox = math.cos(state.heading)
        oy = math.sin(state.heading)
        oz = 0.0
        if target is not None:
            # Pitch the shot line toward the target's eye so hitscan can
            # actually connect (heading only tracks the planar yaw).
            dx = target.x - bot.x
            dy = target.y - bot.y
            planar = math.hypot(dx, dy)
            dz = target.eye_z - bot.eye_z
            if planar > 1e-6:
                oz = dz / planar
        bot.set_orientation_vector(ox, oy, oz)

    # --------------------------------------------------------------- movement
    def _drive_movement(self, bot: "Player", state: "_BotState",
                        target_yaw: float, dist: float, now: float) -> None:
        # Advance/retreat to hold the stand-off band.
        up = down = False
        if dist > _ADVANCE_RANGE:
            up = True
        elif dist < _STOP_RANGE:
            down = True

        # Strafe: flip direction on a jittered timer to look like circle-
        # strafing rather than a straight beeline.
        if state.strafe_dir == 0 or now >= state.next_strafe_flip:
            state.strafe_dir = random.choice((-1, 1))
            state.next_strafe_flip = now + random.uniform(_STRAFE_FLIP_MIN, _STRAFE_FLIP_MAX)
        left = state.strafe_dir < 0
        right = state.strafe_dir > 0

        # Occasional juke: a quick hop to break aim.
        jump = random.random() < _JUKE_CHANCE

        # Face the ENEMY for aiming (already applied), but the movement keys are
        # interpreted relative to that facing by the physics engine — so up =
        # toward the enemy, left/right = strafe around them. This gives the
        # decoupled "keep facing, circle-strafe" behavior.
        bot.update_input(up, down, left, right, jump, False, False, False)

    def _wander(self, bot: "Player", state: "_BotState", now: float, dt: float) -> None:
        if now >= state.next_wander:
            state.wander_heading = random.uniform(-math.pi, math.pi)
            state.next_wander = now + _WANDER_INTERVAL
        # Ease the facing toward the wander heading (no snap).
        self._slew_heading(state, state.wander_heading, dt)
        bot.set_orientation_vector(math.cos(state.heading), math.sin(state.heading), 0.0)
        bot.update_input(True, False, False, False, False, False, False, False)

    # --------------------------------------------------------------- shooting
    def _fire(self, bot: "Player", state: "_BotState", now: float) -> None:
        profile = bot.get_weapon_profile()

        # Clip empty -> reload (the shared Player.update tick finishes it after
        # reload_time); pause fire until then.
        if bot.is_weapon_tool() and bot.ammo_clip <= 0:
            if bot.start_reload(now):
                state.burst_left = 0
                state.next_shot = now + profile.reload_time
            else:
                state.next_shot = now + 0.3
            return

        # Gate through the REAL fire path: ammo + fire_interval + reload state.
        if not bot.consume_shot(now):
            state.next_shot = now + profile.fire_interval
            return

        # Resolve the shot through the authoritative hitscan (LOS, terrain,
        # range, player trace, weapon-profile damage, headshots, block damage).
        try:
            combat = get_combat_system(self.server)
            combat._resolve_hitscan(bot, bot.orientation)
        except Exception:
            logger.debug("bot hitscan failed", exc_info=True)

        # Cadence: bursts for automatics, single well-spaced shots otherwise.
        if profile.fire_interval <= _AUTO_FIRE_THRESHOLD:
            if state.burst_left <= 0:
                state.burst_left = random.randint(_BURST_MIN, _BURST_MAX)
            state.burst_left -= 1
            if state.burst_left <= 0:
                state.next_shot = now + random.uniform(_BURST_PAUSE_MIN, _BURST_PAUSE_MAX)
            else:
                state.next_shot = now + profile.fire_interval
        else:
            # Semi-auto: fire_interval plus a little human hesitation.
            state.next_shot = now + profile.fire_interval + random.uniform(0.0, 0.15)

    # --------------------------------------------------------------- targeting
    def _select_target(self, bot: "Player", state: "_BotState",
                       now: float) -> Optional["Player"]:
        """Return the bot's target with commitment: keep the current target
        while it stays alive, in range, and (recently) visible; otherwise
        re-acquire the nearest enemy we actually have line-of-sight to. On a
        None->target or target-change transition, stamp a reaction delay."""
        current = None
        if state.target_id is not None:
            current = self.server.players.get(state.target_id)

        # Validate the committed target.
        if current is not None and current.alive and current.spawned and current.team != bot.team:
            dx = current.x - bot.x
            dy = current.y - bot.y
            dist = math.hypot(dx, dy)
            if dist <= _LOSE_RANGE:
                if self._has_los(bot, current):
                    state.target_last_seen = now
                    return current
                # Lost LOS: keep committed briefly (last-seen memory) so a bot
                # doesn't instantly forget an enemy that ducked behind cover.
                if now - state.target_last_seen <= _TARGET_RECHECK:
                    return current

        # Re-acquire: nearest visible enemy.
        new_target = self._nearest_visible_enemy(bot)
        if new_target is None:
            state.target_id = None
            return None

        if state.target_id != new_target.id:
            # Fresh acquisition (or target switch): reaction delay before firing.
            state.target_id = new_target.id
            state.target_last_seen = now
            state.next_shot = now + random.uniform(_REACTION_MIN, _REACTION_MAX)
            state.burst_left = 0
        return new_target

    def _nearest_visible_enemy(self, bot: "Player") -> Optional["Player"]:
        best = None
        best_d = 1e18
        for p in self.server.players.values():
            if p is bot or p.team == bot.team:
                continue
            if not p.alive or not p.spawned:
                continue
            d = (p.x - bot.x) ** 2 + (p.y - bot.y) ** 2
            if d >= best_d:
                continue
            if not self._has_los(bot, p):
                continue
            best_d = d
            best = p
        return best

    def _has_los(self, bot: "Player", target: "Player") -> bool:
        """True if no solid block sits between the bot's eye and the target's
        eye (cheap terrain gate; the hitscan does the precise version)."""
        ex, ey, ez = bot.eye
        tx, ty, tz = target.eye
        dx = tx - ex
        dy = ty - ey
        dz = tz - ez
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist <= 1e-6:
            return True
        inv = 1.0 / dist
        try:
            hit = self.server.world_manager.raycast(
                ex, ey, ez, dx * inv, dy * inv, dz * inv, dist,
            )
        except Exception:
            return True  # fail-open: don't let a raycast error freeze the bot
        if hit is None:
            return True
        # A block was hit before reaching the target -> blocked.
        bcx, bcy, bcz = hit[0] + 0.5, hit[1] + 0.5, hit[2] + 0.5
        block_dist = math.sqrt((bcx - ex) ** 2 + (bcy - ey) ** 2 + (bcz - ez) ** 2)
        # Small slack so a block hugging the target still counts as visible.
        return block_dist >= dist - 1.0

    def remove_all(self) -> None:
        for bot in list(self.bots):
            self.server.players.pop(bot.id, None)
            self.server.teams[bot.team].remove_player(bot)
            self._state.pop(bot.id, None)
        self.bots.clear()
