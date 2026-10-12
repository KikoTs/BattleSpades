"""Objective play for community modes, executed in the existing AI worker.

Only local terrain probes and mode-sanctioned facts are used. Long routes use
the same resumable planner as stock modes; actions use the normal gateway.
"""
from __future__ import annotations

from dataclasses import replace
import math
from typing import TYPE_CHECKING, Callable

import shared.constants as C
from .messages import (BotAction, BotActionKind, BotIntent, BotIntentPriority, LookIntent,
                       MovementAffordance, MovementIntent, PerceptionFrame,
                       PlayerSnapshot, Vector3, VoxelCoordinate)
from .fun_messages import FunState
from .policies import ModeBotDecision, ModeBotPosture
from modes.unofficial.catalog import CATALOG
from server.game_constants import WEAPON_PROFILES

if TYPE_CHECKING:
    from .simple_worker import SimpleBotBrain, _BotState

SOLO = frozenset({"floor_tax", "hot_potato", "one_in_the_spade", "borrowed_arsenal"})
RACES = frozenset({"crumbling_run", "red_light_raid"})


def decision_for(frame: PerceptionFrame, observer: PlayerSnapshot) -> ModeBotDecision:
    """Choose a winning objective, without reading hidden enemy positions."""
    f = frame.fun
    assert f is not None
    code, team, pid = frame.mode_id, observer.team, observer.player_id
    home = dict(f.homes).get(team, f.center)
    enemy = next((p for t, p in f.homes if t != team), f.center)
    mine = next((p for p in f.players if p.player_id == pid), None)

    def goal(position, role, directive="", radius=2.0, combat=18.0):
        return ModeBotDecision(tuple(position), "fun_" + role, arrival_radius=radius,
                               directive=directive, objective_priority=1.0,
                               posture=ModeBotPosture.ASSAULT if combat >= 60 else ModeBotPosture.DEFEND,
                               engagement_radius=combat)

    if mine is None or not mine.active or mine.prisoner or f.paused:
        return goal(observer.position, "wait_round", "hold", combat=0)
    if frame.mode_phase != "live":
        if frame.mode_phase != "prepare":
            return goal(observer.position, "wait_round", "hold", combat=0)
        if code == "false_wall":
            if team not in f.stashed:
                return goal((home[0], home[1] + 8, home[2]), "stash_cache", "stash", 1)
            return goal(home, "build_false_wall", "fortify")
        if code in ("towerfall", "scrapyard_engineers"):
            return goal(home, "fortify_core", "fortify")
        if code != "block_hunt" or team == f.attackers:
            return goal(observer.position, "wait_start", "hold", combat=0)
    if code == "block_hunt":
        if team != f.attackers:
            if f.hunt_points:
                return goal(f.hunt_points[pid % len(f.hunt_points)], "hide_as_block", "hide", 1, 0)
            # Stable through prep/live: movement after activation breaks disguise.
            sign = -1 if pid % 2 else 1
            return goal((home[0] + (pid % 3 - 1) * 7, home[1] + sign * 22, home[2]),
                        "hide_as_block", "hide", 1, 0)
        if frame.created_at < f.clue_until and f.clues:
            return goal(min(f.clues, key=lambda p: math.dist(observer.position, p)), "investigate_clue", combat=90)
        if f.hunt_points:
            step = (int(frame.created_at / 30) + pid) % len(f.hunt_points)
            return goal(f.hunt_points[step], "search_props", combat=90)
        step = (int(frame.created_at / 8) + pid * 3) % 12
        return goal((f.center[0] + (step % 4 - 1.5) * 24,
                     f.center[1] + (step // 4 - 1) * 24, f.center[2]), "search_props", combat=90)
    if code in RACES:
        if code == "red_light_raid" and (f.red or f.light_remaining < .8):
            return goal(observer.position, "red_light_stop", "hold", combat=0)
        index = min(mine.checkpoint + 1, len(f.checkpoints) - 1)
        return goal(f.checkpoints[index], "ordered_checkpoints", "race", .8, 0)
    if code == "floor_tax":
        return goal(observer.position, "keep_safe_floor", "floor", .5, 0)
    if code == "hot_potato":
        return goal(observer.position, "pass_curse" if f.holder == pid else "evade_curse", "tag", .5, 0)
    if code in ("one_in_the_spade", "borrowed_arsenal"):
        step = (int(frame.created_at / 7) + pid) % 8
        return goal((f.center[0] + math.cos(step * math.pi / 4) * 27,
                     f.center[1] + math.sin(step * math.pi / 4) * 27, f.center[2]), "free_for_all", combat=100)
    if code == "lockdown":
        if any(p.prisoner and any(q.player_id == p.player_id and q.team == team for q in frame.players)
               for p in f.players):
            return goal(enemy, "rescue_prisoners", radius=3, combat=9)
        return goal(enemy if pid % 3 else home, "capture_or_guard", combat=70)
    if code == "kingmaker":
        angle = pid * 2.4
        return goal((f.center[0] + math.cos(angle) * 4, f.center[1] + math.sin(angle) * 4,
                     f.center[2]), "hold_hill", radius=2, combat=18)
    if code == "paint_raid":
        x0, x1, y0, y1, z = f.paint_bounds
        # Deterministic rows spread the roster without a team negotiation scan.
        row = (pid * 5 + int(frame.created_at / 12) * 3) % (y1 - y0 + 1)
        x = x0 + 2 if int(frame.created_at / 6) % 2 else x1 - 2
        return goal((x, y0 + row + .5, z - 2.25), "paint_territory", "paint", 1, 8)
    if code in ("towerfall", "scrapyard_engineers"):
        if code == "scrapyard_engineers" and observer.blocks < 20:
            return goal(f.center, "refill_scrap", radius=2, combat=8)
        core = next((p for t, p in f.cores if t != team), None)
        return goal(core or enemy, "destroy_core", "demolish", 2, 18)
    if code == "night_shift":
        if team != int(C.TEAM1):
            return goal(f.exit if f.stage == 3 else dict(f.homes)[int(C.TEAM1)], "pressure_outpost", combat=90)
        if f.stage == 1 and f.repair_cells:
            return goal(min(f.repair_cells, key=lambda p: math.dist(observer.position, p)), "repair_barricade", "repair", 2, 10)
        if f.stage == 3:
            return goal(f.exit, "hold_evacuation", radius=3, combat=10)
    carried = next((c for c in f.cargo if c.holder == pid), None)
    if carried is not None:
        if code == "last_exit":
            return goal(f.exit if f.exit_open else home, "extract_loot" if f.exit_open else "protect_loot", radius=2, combat=9)
        destination = enemy if code in ("bridge_chicken", "smugglers_run", "reverse_siege", "spadeball") else home
        return goal(destination, "deliver_ball" if code == "spadeball" else "deliver_cargo", radius=2, combat=5)
    if code in ("smugglers_run", "reverse_siege") and team != f.attackers:
        return goal(home, "intercept_smugglers", radius=3, combat=40)
    available = [c for c in f.cargo if c.holder < 0 and c.ready
                 and (code != "false_wall" or c.team != team)
                 and (code != "bridge_chicken" or c.team == team)]
    if available:
        token = min(available, key=lambda c: math.dist(observer.position, c.position) / max(1, c.value))
        return goal(token.position, "collect_ball" if code == "spadeball" else "collect_cargo", radius=1.2, combat=9)
    if code == "last_exit" and f.exit_open:
        return goal(f.exit, "extract", radius=2, combat=9)
    visible_cargo = [c for c in f.cargo if c.holder >= 0 and not c.hidden]
    if visible_cargo:
        token = min(visible_cargo, key=lambda c: math.dist(observer.position, c.position))
        return goal(token.position, "escort_or_intercept_carrier", radius=4, combat=20)
    return goal(f.center, "contest_objective", combat=30)


def combat_view(frame: PerceptionFrame, observer: PlayerSnapshot) -> tuple[PerceptionFrame, PlayerSnapshot]:
    """Keep collision bodies but hide inactive/undiscovered props from targeting."""
    f = frame.fun
    status = {p.player_id: p for p in f.players}
    rows = []
    for p in frame.players:
        info = status.get(p.player_id)
        visible = info is not None and info.active and not info.prisoner
        if info and info.disguised and p.team != observer.team:
            # Point-blank inspection or a recent sound nearby, still subject to LOS.
            visible &= math.dist(observer.position, p.position) < 3 or (
                frame.created_at < f.clue_until and math.dist(observer.position, p.position) < 10)
        armed = any(t in WEAPON_PROFILES for t in p.loadout)
        rows.append(replace(p, alive=p.alive and visible,
                            ammo_clip=p.ammo_clip if armed else 0,
                            ammo_reserve=p.ammo_reserve if armed else 0,
                            can_dig=CATALOG[frame.mode_id].digging and frame.mode_phase == "live",
                            blocks=p.blocks if CATALOG[frame.mode_id].building else 0,
                            team=1000 + p.player_id if frame.mode_id in SOLO else p.team))
    result = replace(frame, players=tuple(rows), stimuli=())
    return result, next(p for p in rows if p.player_id == observer.player_id)


def intent_for(brain: SimpleBotBrain, frame: PerceptionFrame, observer: PlayerSnapshot,
               state: _BotState, now: float) -> BotIntent:
    """One ordinary input/action, with urgent mode rules before combat/recovery."""
    from .simple_worker import _fallback_profile, _weapon_tool
    f = frame.fun
    decision = decision_for(frame, observer)
    tool = _weapon_tool(observer)
    spec = CATALOG[frame.mode_id]
    can_dig = spec.digging and frame.mode_phase == "live"
    if frame.mode_id == "block_hunt":
        can_dig = observer.team != f.attackers and frame.mode_phase in ("prepare", "live")
    observer = replace(observer, can_dig=can_dig,
                       blocks=observer.blocks if spec.building else 0)

    def emit(role=None, movement=None, action=BotAction(), look=None, selected=None):
        return brain._intent(frame, movement=movement or MovementIntent(), look=look,
                             tool_id=tool if selected is None else selected, action=action,
                             priority=BotIntentPriority.TRAVERSAL, debug_goal=decision.position,
                             debug_role=role or decision.role, crowd_adjust=False)

    if decision.directive == "hold":
        brain.skills.forget(observer.player_id, observer.generation)
        return emit()
    if decision.directive == "race":
        return race_intent(brain, frame, observer, state, decision, emit)
    if decision.directive == "floor":
        return floor_intent(brain, frame, observer, state, emit)
    if decision.directive == "hide":
        info = next(p for p in f.players if p.player_id == observer.player_id)
        if info.disguised:
            return emit()
        if math.dist(observer.position, decision.position) < 1.5:
            if math.hypot(*observer.velocity[:2]) > .1:
                return emit()
            return emit(action=BotAction(BotActionKind.DISGUISE), selected=int(C.DISGUISE_TOOL))
    if frame.mode_id == "bridge_chicken":
        # Objective bridges take precedence over distant pot-shots across a
        # chasm. The authored gap is 34 cells; ordinary recovery caps at 16.
        command = brain.skills.consider_gap(observer, frame.profile, now,
                                            decision.position, max_cells=48)
        intent = brain._skill_result(frame, observer, state, command, now)
        if intent is not None:
            return intent
    view, actor = combat_view(frame, observer)
    profile = frame.profile or _fallback_profile(observer.player_id)
    if decision.directive == "tag":
        visible = [p for p in view.players if p.player_id != actor.player_id and p.alive
                   and brain.world.has_line_of_sight(actor.eye, p.eye)]
        if f.holder == actor.player_id and visible:
            target = min(visible, key=lambda p: math.dist(actor.position, p.position))
            delta = direction(actor.position, target.position)
            return emit(movement=MovementIntent(direction=delta), look=LookIntent(target.eye, visible=True),
                        selected=int(C.PICKAXE_TOOL), action=BotAction(BotActionKind.MELEE, tool_id=int(C.PICKAXE_TOOL))
                        if math.dist(actor.position, target.position) < 3 else BotAction())
        holder = next((p for p in visible if p.player_id == f.holder), None)
        angle = actor.player_id * 2.4 + int(now / 5)
        away = direction(holder.position, actor.position) if holder else (math.cos(angle), math.sin(angle), 0)
        target = (max(f.center[0] - 50, min(f.center[0] + 50, actor.position[0] + away[0] * 12)),
                  max(f.center[1] - 30, min(f.center[1] + 30, actor.position[1] + away[1] * 12)), actor.position[2])
        decision = replace(decision, position=target, directive="")
    elif frame.mode_phase == "live" and decision.directive != "hide":
        target = brain._visible_target(view, actor, state, decision)
        if target is not None:
            return brain._combat_intent(view, actor, target, state, profile, now, decision)

    if brain.skills.active(observer):
        intent = brain._continue_skill(frame, observer, state, now)
        if intent is not None:
            return intent
    distance = math.dist(observer.position, decision.position)
    if now >= state.fun_action_at:
        if decision.directive == "stash" and distance < 1.5:
            state.fun_action_at = now + 1
            return emit(action=BotAction(BotActionKind.MODE_COMMAND, argument="stash"))
        if decision.directive == "paint":
            cells = [cell for cell, owner in f.paint if owner != observer.team
                     and math.dist(observer.eye, tuple(v + .5 for v in cell)) < 4]
            if cells:
                cell = min(cells, key=lambda c: math.dist(observer.position, c))
                state.fun_action_at = now + .2
                return emit(action=BotAction(BotActionKind.PAINT, position=cell), selected=int(C.BLOCK_TOOL))
        if decision.directive in ("repair", "fortify"):
            cells = f.repair_cells if decision.directive == "repair" else fortify_cells(f, observer)
            for cell in cells:
                if not brain.world.solid(*cell) and math.dist(observer.eye, cell) < 4:
                    state.fun_action_at = now + .25
                    return emit(action=BotAction(BotActionKind.BUILD, position=cell), selected=int(C.BLOCK_TOOL))
        if decision.directive == "demolish":
            dig_tool = next((t for t in observer.loadout if t in (int(C.SPADE_TOOL), int(C.PICKAXE_TOOL))), int(C.SPADE_TOOL))
            core = tuple(int(v) for v in decision.position)
            aim = tuple(v + .5 for v in core)
            if math.dist(observer.eye, aim) < 4:
                state.fun_action_at = now + .25
                return emit(look=LookIntent(aim), selected=dig_tool,
                            action=BotAction(BotActionKind.MELEE, tool_id=dig_tool))
            # Tall cores: target their supporting columns instead of staring up.
            if core[2] < observer.position[2] - 5:
                supports = [(core[0] + dx, core[1] + dy, math.floor(observer.position[2] + 1))
                            for dx in (-4, 4) for dy in (-4, 4)]
                intact = [c for c in supports if brain.world.solid(*c)]
                if intact:
                    cell = min(intact, key=lambda p: math.dist(observer.position, p))
                    aim = tuple(v + .5 for v in cell)
                    decision = replace(decision, position=(cell[0] + 1.8, cell[1] + 1.8, observer.position[2]), arrival_radius=1)
                    if math.dist(observer.eye, aim) < 4:
                        state.fun_action_at = now + .25
                        return emit(look=LookIntent(aim), selected=dig_tool,
                                    action=BotAction(BotActionKind.MELEE, tool_id=dig_tool))
        if decision.role == "fun_deliver_ball":
            ahead = [p for p in frame.players if p.team == observer.team and p.alive
                     and p.player_id != observer.player_id and 3 < math.dist(observer.position, p.position) < 7
                     and math.dist(p.position, decision.position) + 3 < distance
                     and brain.world.has_line_of_sight(observer.eye, p.eye)]
            if ahead:
                recipient = min(ahead, key=lambda p: math.dist(p.position, decision.position))
                state.fun_action_at = now + 2
                return emit(action=BotAction(BotActionKind.MODE_COMMAND, position=recipient.position, argument="pass"))
    goal = brain._goal_from_mode_decision(decision)
    return brain._navigation_intent(frame, observer, state, goal, now)


def direction(a: Vector3, b: Vector3) -> Vector3:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = max(.001, math.hypot(dx, dy))
    return dx / length, dy / length, 0.0


def fortify_cells(f: FunState, observer: PlayerSnapshot) -> tuple[VoxelCoordinate, ...]:
    home = dict(f.homes)[observer.team]
    x, y, z = math.floor(home[0]) + 3, math.floor(home[1]), math.floor(home[2] + 2.25) - 1
    return tuple((x, y + offset, z) for offset in (-1, 0, 1))


def floor_intent(brain: SimpleBotBrain, frame: PerceptionFrame, observer: PlayerSnapshot,
                 state: _BotState, emit: Callable[..., BotIntent]) -> BotIntent:
    active = {p.player_id for p in frame.fun.players if p.active}
    rivals = [p for p in frame.players if p.player_id != observer.player_id and p.alive
              and p.player_id in active and abs(p.position[2] - observer.position[2]) < 3
              and brain.world.has_line_of_sight(observer.eye, p.eye)]
    rival = min(rivals, key=lambda p: math.dist(p.position, observer.position)) if rivals else None
    best = None
    for i in range(8):
        angle = (i + observer.player_id) * math.pi / 4
        delta = (math.cos(angle), math.sin(angle), 0.0)
        cells = [(math.floor(observer.position[0] + delta[0] * d),
                  math.floor(observer.position[1] + delta[1] * d)) for d in (1, 2, 3)]
        z = round(observer.position[2] + 2.25)
        safe = sum(brain.world.solid(x, y, z) for x, y in cells)
        score = safe * 10 + sum(a * b for a, b in zip(delta, state.fun_direction))
        if rival is not None:
            toward = direction(observer.position, rival.position)
            score += sum(a * b for a, b in zip(delta, toward)) * 4
        if best is None or score > best[0]:
            best = score, delta
    state.fun_direction = best[1]
    movement = MovementIntent(direction=best[1], jump=best[0] < 20 and observer.grounded)
    if rival is not None:
        target = (rival.position[0], rival.position[1], round(rival.position[2] + 2.25) + .1)
        reach = math.dist(observer.eye, target)
        if reach < 5:
            return emit(movement=movement, look=LookIntent(target), selected=int(C.SPADE_TOOL),
                        action=BotAction(BotActionKind.MELEE, tool_id=int(C.SPADE_TOOL)))
        if 6 < reach < 18 and frame.created_at >= state.fun_action_at:
            state.fun_action_at = frame.created_at + 6
            aim = (target[0], target[1], target[2] - 4)
            return emit(movement=movement, look=LookIntent(aim), selected=int(C.GRENADE_TOOL),
                        action=BotAction(BotActionKind.FIRE, tool_id=int(C.GRENADE_TOOL)))
    return emit(movement=movement, selected=int(C.SPADE_TOOL))


def race_intent(brain: SimpleBotBrain, frame: PerceptionFrame, observer: PlayerSnapshot,
                state: _BotState, decision: ModeBotDecision,
                emit: Callable[..., BotIntent]) -> BotIntent:
    f = frame.fun
    route = f.route or f.checkpoints
    if not route:
        return emit("fun_missing_course")
    info = next(p for p in f.players if p.player_id == observer.player_id)
    checkpoint = f.checkpoints[min(info.checkpoint, len(f.checkpoints) - 1)]
    first = min(range(len(route)), key=lambda i: math.dist(route[i], checkpoint))
    next_checkpoint = decision.position
    last = min(range(len(route)), key=lambda i: math.dist(route[i], next_checkpoint))
    # A retry rewinds locally; the authoritative ordered checkpoint is the fence.
    if math.dist(observer.position, checkpoint) < 3.5:
        state.fun_waypoint = min(first + 1, last)
    index = max(first + 1, min(last, state.fun_waypoint))
    index = min(index, len(route) - 1)
    broad_course = len(route) > 32
    if math.dist(observer.position[:2], route[index][:2]) < (2.5 if broad_course else 1.4) and (observer.grounded or broad_course):
        index = min(index + 1, last)
    state.fun_waypoint = index
    target = route[index]
    delta = direction(observer.position, target)
    x, y, z = observer.position
    # Launch at a platform edge, with ordinary native jump and airborne steering.
    support = round(target[2] + 2.25)
    lookahead = 2.2 if broad_course else 1.7
    ahead = (math.floor(x + delta[0] * lookahead), math.floor(y + delta[1] * lookahead), support)
    jump = observer.grounded and not brain.world.solid(*ahead)
    movement = MovementIntent(direction=delta, jump=jump, affordance=MovementAffordance.JUMP if jump or not observer.grounded else MovementAffordance.WALK)
    return emit(movement=movement, look=LookIntent(target))
