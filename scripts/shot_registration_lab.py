"""End-to-end hit registration lab: does an on-target shot do damage?

Drives the REAL server (``scripts/anticheat_lab``: real tick, real input
buffer, real ``CombatSystem.handle_shot``, real lag compensation) with one
headless protocol-168 shooter and one moving server-owned target over an
impaired link, in virtual time.

The shooter is a perfect aimer *on its own screen*: every frame it aims at
the target body its client is displaying. The display follows the stock
client contract (``docs/LAG_COMPENSATION.md``): the newest WorldUpdate row
snaps the remote body, which is then simulated forward with the same native
mover (extrapolation, no interpolation buffer). The trigger is pulled
whenever the gun is ready, so every shot the client sends is one the player
saw land.

Per shot the lab records what the server did with it: dropped before
resolution (and why), resolved as a miss, or a hit with its damage. A shot
the client aimed perfectly that does no damage is a registration failure.

Usage::

    py -3.12 scripts/shot_registration_lab.py              # whole matrix
    py -3.12 scripts/shot_registration_lab.py --quick
    py -3.12 scripts/shot_registration_lab.py --weapon sniper --profile ping150

The module is also imported by ``tests/test_shot_registration.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "scripts"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import shared.constants as C  # noqa: E402
from server.game_constants import WEAPON_PROFILES  # noqa: E402

from anticheat_lab.lab import Lab  # noqa: E402
from anticheat_lab.link import NetProfile  # noqa: E402
from anticheat_lab.clientmodel import FRAME  # noqa: E402

# A long straight shooting lane carved into the lab map.
LANE_X0 = 40
LANE_X1 = 470
LANE_Y0 = 96
LANE_Y1 = 128
SURFACE = 220
EYE_ABOVE_GROUND = 2.25
EYE_Z = SURFACE - EYE_ABOVE_GROUND
SHOOTER_X = LANE_X0 + 5.5
LANE_Y = (LANE_Y0 + LANE_Y1) / 2.0 + 0.5

# Aim offsets below the eye (VXL z grows downward).
TORSO_AIM = 0.75
HEAD_AIM = -0.05

PROFILES = {
    "lan": NetProfile("lan"),
    "ping50": NetProfile("ping50", delay_ms=25.0, jitter_ms=5.0),
    "ping100": NetProfile("ping100", delay_ms=50.0, jitter_ms=10.0, loss=0.01),
    "ping200": NetProfile("ping200", delay_ms=100.0, jitter_ms=20.0, loss=0.02),
    "ping300": NetProfile("ping300", delay_ms=150.0, jitter_ms=25.0, loss=0.02),
    # Bad Wi-Fi: lost reliable packets arrive one retransmission late.
    "ping150_loss5": NetProfile("ping150_loss5", delay_ms=75.0, jitter_ms=20.0,
                                loss=0.05),
}


@dataclass(frozen=True)
class Weapon:
    name: str
    class_id: int
    loadout: tuple
    tool: int
    zoom: bool = False
    head: bool = False


WEAPONS = {
    "sniper": Weapon("sniper", 1, (18, 17, 20, 0), 18, zoom=True),
    "sniper_head": Weapon("sniper_head", 1, (18, 17, 20, 0), 18, zoom=True, head=True),
    "sniper_hip": Weapon("sniper_hip", 1, (18, 17, 20, 0), 18),
    "sniper2": Weapon("sniper2", 1, (19, 53, 56, 1), 19, zoom=True),
    "pistol": Weapon("pistol", 1, (17, 18, 20, 0), 17),
    "smg": Weapon("smg", 2, (7, 11, 67, 2), 7),
    "shotgun": Weapon("shotgun", 3, (9, 14, 21, 3), 9),
    "assault": Weapon("assault", 0, (60, 13, 11, 1), 60),
    "minigun": Weapon("minigun", 0, (8, 12, 72, 2), 8),
    "lmg": Weapon("lmg", 17, (61, 52, 51, 49), 61),
    "rifle": Weapon("rifle", 5, (6, 4), 6),
}

# Target motions: callables (tick, rng, state) -> input flags tuple.
MOTIONS = ("stand", "strafe", "jump_strafe", "run_away")


def _motion_flags(kind: str, tick: int, state: dict, rng: random.Random):
    up = down = left = right = jump = False
    if kind == "strafe" or kind == "jump_strafe":
        if tick >= state.get("switch_at", 0):
            state["dir"] = not state.get("dir", False)
            state["switch_at"] = tick + rng.randint(30, 80)
        left = state["dir"]
        right = not state["dir"]
        if kind == "jump_strafe" and tick % 70 == 0:
            jump = True
    elif kind == "run_away":
        up = True
    return (up, down, left, right, jump, False, False, False)


@dataclass
class ShotRecord:
    label: int
    sent_at: float
    resolved: bool = False
    reason: str = "pending"
    damage: float = 0.0
    headshot: bool = False
    view_error: float = 0.0


@dataclass
class Result:
    weapon: str
    profile: str
    motion: str
    distance: float
    sent: int = 0
    resolved: int = 0
    hits: int = 0
    damage: float = 0.0
    reasons: Counter = field(default_factory=Counter)
    rejections: Counter = field(default_factory=Counter)
    view_error_p95: float = 0.0
    rewind_error_hit_p95: float = 0.0
    rewind_error_miss: list = field(default_factory=list)

    @property
    def hit_rate(self) -> float:
        return self.hits / self.sent if self.sent else 0.0

    def row(self) -> dict:
        return {
            "weapon": self.weapon, "profile": self.profile,
            "motion": self.motion, "distance": self.distance,
            "sent": self.sent, "resolved": self.resolved, "hits": self.hits,
            "hit_rate": round(self.hit_rate, 3),
            "damage": round(self.damage, 1),
            "reasons": dict(self.reasons),
            "rejections": dict(self.rejections),
            "view_error_p95": round(self.view_error_p95, 3),
            "rewind_error_hit_p95": round(self.rewind_error_hit_p95, 3),
            "rewind_error_miss": [(round(v, 2), late) for v, late in self.rewind_error_miss],
        }


def _carve_lane(world) -> None:
    vxl = world.map
    for x in range(LANE_X0, LANE_X1):
        for y in range(LANE_Y0, LANE_Y1):
            for z in range(0, SURFACE):
                vxl.set_point(x, y, z, False, 0)
            for z in range(SURFACE, 240):
                vxl.set_point(x, y, z, True, 0x7F6E6E6E)
    refresh = getattr(world, "_refresh_world", None)
    if callable(refresh):
        refresh()


class RemoteView:
    """The stock client's picture of one remote body (snap + extrapolate)."""

    def __init__(self, model) -> None:
        from server.player import Player

        connection = model._facade()
        helper = Player(250, "view", 3, int(C.RIFLE_TOOL), connection)
        connection.player = helper
        helper._award_teabag_point = lambda: None
        helper.spawn(SHOOTER_X + 30.0, LANE_Y, EYE_Z)
        self.helper = helper
        self.fresh = False

    def snap(self, position, velocity, orientation, input_byte) -> None:
        helper = self.helper
        world_object = helper._ensure_world_object()
        world_object.set_position(*position)
        world_object.set_velocity(*velocity)
        helper._sync_cached_vectors()
        helper.set_orientation_vector(*orientation)
        flags = tuple(bool(input_byte & (1 << bit)) for bit in range(8))
        helper.update_input(*flags)
        self.fresh = True

    def step(self) -> None:
        helper = self.helper
        collisions = helper._build_player_collision_positions()
        helper._apply_input_state_to_world(trigger_jump=False, collisions=collisions)
        helper._world_object.update(FRAME, collisions)
        helper._sync_cached_vectors()

    @property
    def position(self):
        return (float(self.helper.x), float(self.helper.y), float(self.helper.z))


def _install_view(model, target_id_ref) -> RemoteView:
    view = RemoteView(model)
    original = model._on_world_update

    def on_world_update(self, packet):
        original(packet)
        target_id = target_id_ref.get("id")
        row = packet.player_updates.get(target_id) if target_id is not None else None
        if row is not None:
            view.snap(row[0], row[2], row[1], int(row[6]))

    model._on_world_update = MethodType(on_world_update, model)
    return view


async def run_case(
    weapon: Weapon,
    profile: NetProfile,
    motion: str,
    distance: float,
    *,
    seconds: float = 20.0,
    seed: int = 1,
    clientdata_after_shot: bool = False,
    lag_overrides: Optional[dict] = None,
    baseline: bool = False,
) -> Result:
    undo = _apply_baseline() if baseline else []
    lab = Lab(profile=profile, seed=seed, court=False,
              config_overrides=dict(lag_overrides or {}))
    await lab.start()
    rng = random.Random(seed * 7 + 3)
    result = Result(weapon.name, profile.name, motion, distance)
    try:
        server = lab.server
        _carve_lane(server.world_manager)
        target = lab.add_dummy("Target", (SHOOTER_X + distance, LANE_Y, EYE_Z), team=3)
        target.set_orientation_vector(-1.0, 0.0, 0.0)
        hits: list = []

        def damage(amount, source=None, kill_type=0, **_kw):
            hits.append((float(amount), int(kill_type), lab.clock.now))
            return False

        target.damage = damage

        client = lab.add_client("Shooter", team=2, class_id=weapon.class_id,
                                loadout=weapon.loadout, shot_phase=0)
        model = client.model
        target_ref = {"id": int(target.id)}
        view = _install_view(model, target_ref)
        joined = await lab.join_all(60.0)
        if not joined or client.player is None:
            raise RuntimeError("shooter did not join")
        shooter = client.player
        shooter.set_position(SHOOTER_X, LANE_Y, EYE_Z)
        # Spawn protection must not hide registration problems.
        shooter.spawned_at = -1e9
        await lab.run(0.5)

        # -- per-shot bookkeeping on the server --------------------------
        combat = server.combat
        records: dict = {}
        original_handle = combat.handle_shot
        original_reject = combat._reject
        current = {"record": None}

        def reject(player, kind, **detail):
            if current["record"] is not None:
                current["record"].reason = f"rejected:{kind}"
            result.rejections[kind] += 1
            return original_reject(player, kind, **detail)

        def handle_shot(player, packet):
            if player is not shooter:
                return original_handle(player, packet)
            record = records.get(int(packet.loop_count))
            if record is None:
                record = ShotRecord(int(packet.loop_count), lab.clock.now)
                records[record.label] = record
            current["record"] = record
            # Lag-compensation accuracy: the rewound body vs the body this
            # client displayed when it fired this label.
            seen_at = seen_by_label.get(int(packet.loop_count))
            if seen_at is not None:
                from server import lag_compensation as _lc

                context = _lc.rewind_targets(server, player, packet)
                body = context.body(target) if context is not None else target
                record.rewind_error = math.dist(
                    seen_at, (float(body.x), float(body.y), float(body.z)))
                arrival = getattr(player, "label_arrival_tick", lambda _l: None)(
                    packet.loop_count)
                record.late_ticks = (
                    None if arrival is None else int(server.loop_count) - int(arrival)
                )
            stats_before = int(getattr(player, "anticheat_stats", {}).get("shots", 0)
                               if isinstance(getattr(player, "anticheat_stats", None), dict)
                               else 0)
            hits_before = len(hits)
            zoom_for = getattr(player, "zoom_for_action", None)
            if callable(zoom_for):
                server_zoom = bool(zoom_for(packet.loop_count))
            else:
                server_zoom = bool(getattr(getattr(player, "input", None), "zoom", False))
            record.reason = "dropped:cadence_or_ammo"
            try:
                outcome = original_handle(player, packet)
            finally:
                current["record"] = None
            stats = getattr(player, "anticheat_stats", None)
            stats_after = int(stats.get("shots", 0)) if isinstance(stats, dict) else 0
            if stats_after > stats_before:
                record.resolved = True
                record.reason = (
                    "miss" if (server_zoom or not weapon.zoom)
                    else "miss:server_unzoomed"
                )
            elif record.reason == "dropped:cadence_or_ammo":
                state = (
                    f"clip={player.ammo_clip} reload={player.reloading}"
                )
                record.reason = "dropped:" + (
                    "reloading" if player.reloading else
                    "empty" if player.ammo_clip <= 0 else "cadence"
                )
                record.detail = state
            if len(hits) > hits_before:
                record.damage = sum(h[0] for h in hits[hits_before:])
                record.headshot = any(h[1] == int(getattr(C, "KILL_HEADSHOT", 2))
                                      or h[1] == 2 for h in hits[hits_before:])
                record.reason = "hit"
            return outcome

        combat.handle_shot = handle_shot
        combat._reject = reject

        # -- the target moves on the server --------------------------------
        motion_state: dict = {}
        tick_counter = {"n": 0}
        original_tick = lab._tick

        async def tick():
            tick_counter["n"] += 1
            # An ammo crate whenever the reserve runs low (sniper: 8 rounds).
            if (tick_counter["n"] % 30 == 0 and shooter.alive
                    and int(getattr(shooter, "ammo_reserve", 99)) <= 2):
                shooter.restock_ammo(int(getattr(C, "AMMO_CRATE", 3)))
            if target.alive:
                # Turn back before the lane edge (no teleports mid-run).
                last_y = motion_state.get("last_y", target.y)
                if ((target.y < LANE_Y - 9 and target.y < last_y)
                        or (target.y > LANE_Y + 9 and target.y > last_y)):
                    motion_state["dir"] = not motion_state.get("dir", False)
                    motion_state["switch_at"] = tick_counter["n"] + 40
                motion_state["last_y"] = target.y
                flags = _motion_flags(motion, tick_counter["n"], motion_state, rng)
                target.update_input(*flags)
                # Facing the shooter, left/right strafes ACROSS the line of
                # fire; run_away runs down the lane.
                if motion == "run_away":
                    target.set_orientation_vector(1.0, 0.0, 0.0)
                else:
                    target.set_orientation_vector(-1.0, 0.0, 0.0)
                # Keep the target on the lane.
                if not (LANE_Y0 + 2 < target.y < LANE_Y1 - 2) or target.x > LANE_X1 - 4:
                    target.set_position(SHOOTER_X + distance, LANE_Y, target.z)
            await original_tick()

        lab._tick = tick

        # -- the shooter: perfect aim on its own screen ----------------------
        errors: list = []
        seen_by_label: dict = {}
        sent_labels: set = set()
        zoom_state = {"zoomed_frames": 0, "rezoom_at": 0}

        def script():
            frame = 0
            while True:
                frame += 1
                view.step() if view.fresh else None
                intent = model.intent
                intent.tool = weapon.tool
                eye = model.position
                seen = view.position
                seen_by_label[model.loop] = seen
                offset = HEAD_AIM if weapon.head else TORSO_AIM
                aim_point = (seen[0], seen[1], seen[2] + offset)
                delta = tuple(aim_point[i] - eye[i] for i in range(3))
                length = math.sqrt(sum(c * c for c in delta)) or 1.0
                intent.aim = tuple(c / length for c in delta)
                gun = model.guns.get(weapon.tool)
                reloading = bool(gun is not None and (gun.reload_left > 0.0 or gun.reload_pending))
                if weapon.zoom:
                    # Character.reload cancels zoom; the player re-zooms after.
                    if reloading:
                        intent.zoom = False
                        intent.secondary = False
                        zoom_state["zoomed_frames"] = 0
                        zoom_state["rezoom_at"] = frame + rng.randint(4, 15)
                    elif frame >= zoom_state["rezoom_at"]:
                        intent.zoom = True
                        intent.secondary = True
                        zoom_state["zoomed_frames"] += 1
                    ready = intent.zoom and zoom_state["zoomed_frames"] >= rng.randint(2, 10)
                else:
                    ready = True
                intent.primary = bool(ready and view.fresh)
                # True position of the body at the moment of the shot,
                # for the view-error statistic.
                truth = (float(target.x), float(target.y), float(target.z))
                errors.append(math.dist(seen, truth))
                yield

        model.script = script()

        # Clientdata-after-shot variant: the frame's ClientData carries the
        # post-shot state (zoom dropped by the last round).
        if clientdata_after_shot:
            original_weapons = model._weapons
            original_send = model._send_client_data
            pending = {"skip": False}

            def send_client_data(self, neutral=False):
                if pending["skip"]:
                    return
                original_send(neutral)

            def weapons(self, aim):
                shots_before = self.stats.shots_sent
                original_weapons(aim)
                if weapon.zoom and self.stats.shots_sent > shots_before:
                    gun = self.guns.get(weapon.tool)
                    if gun is not None and gun.clip <= 0:
                        self.intent.zoom = False
                        self.intent.secondary = False
                pending["skip"] = False
                original_send(False)
                pending["skip"] = True

            model._weapons = MethodType(weapons, model)
            model._send_client_data = MethodType(send_client_data, model)
            pending["skip"] = True

        shots_before = model.stats.shots_by_tool.get(weapon.tool, 0)
        await lab.run(seconds)
        await lab.run(1.0 + profile.rtt_ms / 1000.0 * 3)  # drain in-flight shots
        result.sent = model.stats.shots_by_tool.get(weapon.tool, 0) - shots_before
        for record in records.values():
            if record.resolved:
                result.resolved += 1
            if record.damage > 0:
                result.hits += 1
                result.damage += record.damage
            result.reasons[record.reason] += 1
        missing = result.sent - len(records)
        if missing > 0:
            result.reasons["never_arrived"] += missing
        hit_errors = sorted(getattr(r, "rewind_error", 0.0) for r in records.values()
                            if r.damage > 0 and hasattr(r, "rewind_error"))
        if hit_errors:
            result.rewind_error_hit_p95 = hit_errors[int(0.95 * (len(hit_errors) - 1))]
        result.rewind_error_miss = [(r.rewind_error, getattr(r, "late_ticks", None))
                                    for r in records.values()
                                    if r.resolved and r.damage <= 0
                                    and hasattr(r, "rewind_error")][:12]
        if errors:
            ordered = sorted(errors)
            result.view_error_p95 = ordered[int(0.95 * (len(ordered) - 1))]
        return result
    finally:
        await lab.stop()
        for restore in reversed(undo):
            restore()


DEFAULT_MATRIX = (
    ("sniper", "stand", 90.0),
    ("sniper", "strafe", 60.0),
    ("sniper", "strafe", 120.0),
    ("sniper", "jump_strafe", 90.0),
    ("sniper", "run_away", 60.0),
    ("sniper_head", "stand", 90.0),
    ("sniper_head", "strafe", 60.0),
    ("sniper2", "strafe", 60.0),
    ("rifle", "strafe", 40.0),
    ("pistol", "strafe", 20.0),
    ("smg", "strafe", 15.0),
    ("smg", "jump_strafe", 15.0),
    ("shotgun", "strafe", 10.0),
    ("assault", "strafe", 20.0),
    ("minigun", "strafe", 15.0),
    ("lmg", "strafe", 15.0),
)


def _apply_baseline() -> list:
    """Re-create the pre-fix server behaviour for before/after comparisons."""

    from server.player import Player

    undo = []
    if hasattr(Player, "zoom_for_action"):
        saved = Player.zoom_for_action
        Player.zoom_for_action = (
            lambda self, loop_count=None: bool(getattr(self.input, "zoom", False))
        )
        undo.append(lambda: setattr(Player, "zoom_for_action", saved))
    if hasattr(Player, "_reload_done_by_label"):
        saved_reload = Player._reload_done_by_label
        Player._reload_done_by_label = lambda self, profile, loop: False
        undo.append(lambda: setattr(Player, "_reload_done_by_label", saved_reload))
    from server import lag_compensation

    if hasattr(lag_compensation, "late_shot_ms"):
        saved_late = lag_compensation.late_shot_ms
        lag_compensation.late_shot_ms = lambda *args, **kwargs: 0.0
        undo.append(lambda: setattr(lag_compensation, "late_shot_ms", saved_late))
    for hook in BASELINE_HOOKS:
        undo.extend(hook())
    return undo


# Extra (callable -> list of undo callables) baseline patches.
BASELINE_HOOKS: list = []


async def run_matrix(cases, profiles, *, seconds, seed, clientdata_after_shot=False,
                     lag_overrides=None, baseline=False):
    rows = []
    for weapon_name, motion, distance in cases:
        for profile_name in profiles:
            result = await run_case(
                WEAPONS[weapon_name], PROFILES[profile_name], motion, distance,
                seconds=seconds, seed=seed,
                clientdata_after_shot=clientdata_after_shot,
                lag_overrides=lag_overrides, baseline=baseline,
            )
            row = result.row()
            rows.append(row)
            print(json.dumps(row), flush=True)
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--weapon", action="append")
    parser.add_argument("--motion", default=None)
    parser.add_argument("--distance", type=float, default=None)
    parser.add_argument("--profile", action="append")
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--clientdata-after-shot", action="store_true")
    parser.add_argument("--baseline", action="store_true",
                        help="patch the server back to its pre-fix behaviour")
    args = parser.parse_args(argv)
    import logging

    logging.basicConfig(level=logging.ERROR)
    profiles = args.profile or (["lan", "ping100", "ping200"] if args.quick
                                else list(PROFILES))
    if args.weapon:
        cases = [(w, args.motion or "strafe", args.distance or 40.0) for w in args.weapon]
    else:
        cases = DEFAULT_MATRIX
    asyncio.run(run_matrix(cases, profiles, seconds=args.seconds, seed=args.seed,
                           clientdata_after_shot=args.clientdata_after_shot,
                           baseline=args.baseline))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
