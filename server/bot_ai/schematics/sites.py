"""Schematic site selection and the shared multi-builder site coordinator.

``find_schematic_site`` fits a schematic near a point (flat-enough ground,
away from protected points and other sites, no bodies inside) and plans it.
``SchematicSites`` holds the team's active sites: builders claim one plan
step at a time, stand where nobody else stands, and report results. Budgets
and cooldowns keep construction occasional; repeated rejections, no progress
or sustained incoming fire abandon a site.

Worker-local and bounded; nothing here touches the authoritative map. The
gateway/CombatSystem remain the final authority for every block.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
import math
from typing import Iterable, Sequence

from .library import get as get_schematic
from .model import Cell, Placement, Schematic, SolidFn, fit, rotation_for_facing
from .planner import (
    BuildPlan, BuildStep, Node, Region, body_cells, boundary_sources, find_stand,
    is_stand, node_for_position, node_position, plan_build, region_for,
    step_ready, step_remaining, walk_distances,
)

Identity = tuple[int, int, int]
Vector3 = tuple[float, float, float]

MAX_SITES = 8
MAX_ACTIVE_PER_TEAM = 3
OPTIONAL_TEAM_COOLDOWN = 45.0
SITE_SEPARATION = 10.0
SITE_IDLE_TIMEOUT = 45.0
STEP_CLAIM_SECONDS = 9.0
STEP_BACKOFF = 3.0
STEP_MAX_FAILURES = 5
SITE_MAX_FAILURES = 16
UNDER_FIRE_WINDOW = 15.0
UNDER_FIRE_LIMIT = 4
MAX_SITE_CANDIDATES = 12
MAX_PLANNED_CANDIDATES = 2
BUILDER_RADIUS = 28.0


def _spiral(radius: int) -> list[tuple[int, int]]:
    offsets = [(dx, dy) for dx in range(-radius, radius + 1) for dy in range(-radius, radius + 1)]
    offsets.sort(key=lambda o: (o[0] * o[0] + o[1] * o[1], o))
    return offsets


def ground_under(solid: SolidFn, x: int, y: int, near_z: float, span: int = 3) -> int | None:
    """Terrain top (support z) of a column near a body/point height."""

    expected = round(near_z + 2.25)
    for s in sorted(range(expected - span, expected + span + 1), key=lambda s: (abs(s - expected), s)):
        if is_stand(solid, (x, y, s)):
            return s
    return None


def interior_columns(placement: Placement) -> frozenset[tuple[int, int]]:
    """Columns an occupant may stand in while it builds around itself.

    Keep-clear floor columns within ``radius - 1`` of the occupant: the
    shelter interior, never the doorway approach outside the walls.
    """

    if placement.occupant is None:
        return frozenset()
    ox, oy = math.floor(placement.occupant[0]), math.floor(placement.occupant[1])
    walls = max(max(abs(u), abs(f)) for u, f, _h, _t in placement.schematic.cells)
    limit = max(1, walls - 1)
    floor = placement.ground_z - 1
    return frozenset((x, y) for x, y, z in placement.clear
                     if z == floor and max(abs(x - ox), abs(y - oy)) <= limit)


@dataclass(frozen=True, slots=True)
class SiteChoice:
    placement: Placement
    plan: BuildPlan


def find_schematic_site(solid: SolidFn, schematic: Schematic, centre: Vector3,
                        facing: tuple[float, float] = (1.0, 0.0), *,
                        exact: bool = False,
                        any_rotation: bool = True,
                        keep_away: Sequence[tuple[Vector3, float]] = (),
                        reserved: frozenset[Cell] | set[Cell] = frozenset(),
                        bodies: Sequence[Vector3] = (),
                        occupant_body: Vector3 | None = None,
                        max_candidates: int = MAX_SITE_CANDIDATES,
                        seed: int = 0) -> SiteChoice | None:
    """Fit and plan ``schematic`` near ``centre`` facing ``facing``.

    ``exact`` keeps the anchor on the centre column (a VIP boxing himself
    in). ``keep_away`` lists (point, radius) pairs no cell may approach
    (objectives, spawns). ``bodies`` are living players whose body cells must
    stay out of planned cells; ``occupant_body`` may stand in keep-clear cells.
    """

    cx, cy = math.floor(centre[0]), math.floor(centre[1])
    preferred = rotation_for_facing(*facing)
    rotations = [preferred] + ([r for r in ((preferred + 1) % 4, (preferred + 3) % 4,
                                            (preferred + 2) % 4)] if any_rotation else [])
    offsets = [(0, 0)] if exact else _spiral(3)[:max_candidates]
    fits: list[tuple[float, Placement]] = []
    body_set: set[Cell] = set()
    for body in bodies:
        if occupant_body is not None and math.dist(body, occupant_body) < .5:
            continue
        bx, by = body[0], body[1]
        top = math.floor(body[2])
        for x in range(math.floor(bx - .45), math.floor(bx + .45) + 1):
            for y in range(math.floor(by - .45), math.floor(by + .45) + 1):
                body_set.update((x, y, z) for z in range(top, top + 3))
    attempts = 0
    for dx, dy in offsets:
        x, y = cx + dx, cy + dy
        ground = ground_under(solid, x, y, centre[2])
        if ground is None:
            continue
        for rotation in rotations:
            attempts += 1
            if attempts > max_candidates * 2:
                break
            placement, _why = fit(schematic, solid, (x, y), ground, rotation)
            if placement is None:
                continue
            cells = set(placement.cells)
            if not cells.isdisjoint(reserved) or not placement.clear.isdisjoint(reserved):
                continue
            if not cells.isdisjoint(body_set):
                continue
            if any(math.hypot(cell[0] + .5 - point[0], cell[1] + .5 - point[1]) < radius
                   for point, radius in keep_away for cell in cells):
                continue
            score = math.hypot(dx, dy) + (0 if rotation == preferred else 1.5) + len(placement.foundation) * .2
            fits.append((score, placement))
    fits.sort(key=lambda item: item[0])
    for _score, placement in fits[:MAX_PLANNED_CANDIDATES]:
        plan = plan_build(solid, placement, seed=seed,
                          prefer_occupant=placement.occupant is not None and exact)
        if plan.feasible:
            return SiteChoice(placement, plan)
    return None


@dataclass(slots=True)
class StepClaim:
    builder: Identity
    until: float
    stand: Node


@dataclass(slots=True)
class SchematicSite:
    site_id: int
    team: int
    plan: BuildPlan
    purpose: str
    owner: Identity
    created_at: float
    expires_at: float
    priority: float = 0.8
    requester: str = ""
    builders_allowed: frozenset[int] = frozenset()
    occupant: Identity | None = None
    max_builders: int = 3
    region: Region | None = None
    claims: dict[int, StepClaim] = field(default_factory=dict)
    backoff: dict[int, float] = field(default_factory=dict)
    failures: Counter = field(default_factory=Counter)
    skipped: set[int] = field(default_factory=set)
    done: set[int] = field(default_factory=set)
    builders: dict[Identity, float] = field(default_factory=dict)
    lines_built: int = 0
    singles_built: int = 0
    line_cells: int = 0
    single_cells: int = 0
    cells_by_builder: Counter = field(default_factory=Counter)
    under_fire: deque = field(default_factory=lambda: deque(maxlen=16))
    progress_at: float = 0.0
    completed_at: float = 0.0
    abandoned: str = ""
    total_failures: int = 0

    @property
    def name(self) -> str:
        return self.plan.placement.name

    @property
    def active(self) -> bool:
        return not self.abandoned and not self.completed_at

    @property
    def centre(self) -> Vector3:
        x, y, z = self.plan.placement.anchor
        return (x + .5, y + .5, z - 2.25)

    @property
    def cells(self) -> frozenset[Cell]:
        return frozenset(self.plan.placement.cells) | self.plan.placement.clear

    def status(self, solid: SolidFn | None = None) -> dict[str, object]:
        remaining = (sum(1 for step in self.plan.steps for c in step.cells if not solid(*c))
                     if solid is not None else None)
        return {
            "site_id": self.site_id, "name": self.name, "team": self.team,
            "purpose": self.purpose, "anchor": self.plan.placement.anchor,
            "rotation": self.plan.placement.rotation, "steps": len(self.plan.steps),
            "done": len(self.done), "skipped": len(self.skipped), "remaining_cells": remaining,
            "lines_built": self.lines_built, "singles_built": self.singles_built,
            "line_cells": self.line_cells, "single_cells": self.single_cells,
            "builders": len(self.cells_by_builder), "completed_at": self.completed_at,
            "abandoned": self.abandoned, "created_at": self.created_at,
        }


class SchematicSites:
    """Worker-local registry of construction sites shared by a team's bots."""

    def __init__(self) -> None:
        self.sites: dict[int, SchematicSite] = {}
        self.next_id = 1
        self.team_ready: dict[int, float] = {}
        self.metrics: Counter[str] = Counter()
        self.history: deque[dict[str, object]] = deque(maxlen=64)
        self.optional_counts: Counter[int] = Counter()

    def optional_started(self, team: int) -> int:
        """Optional (self-initiated) sites this team started on this map."""

        return self.optional_counts[team]

    # --- creation / budget -----------------------------------------------------

    def active_sites(self, team: int) -> list[SchematicSite]:
        return [site for site in self.sites.values() if site.team == team and site.active]

    def can_start(self, team: int, now: float, *, optional: bool = True,
                  position: Vector3 | None = None) -> bool:
        active = self.active_sites(team)
        if len(active) >= MAX_ACTIVE_PER_TEAM or len(self.sites) >= MAX_SITES:
            return False
        if optional and now < self.team_ready.get(team, 0.0):
            return False
        if position is not None and any(math.dist(site.centre, position) < SITE_SEPARATION
                                        for site in active):
            return False
        return True

    def create(self, team: int, plan: BuildPlan, purpose: str, owner: Identity, now: float, *,
               ttl: float = 120.0, priority: float = 0.8, requester: str = "",
               builders_allowed: Iterable[int] = (), occupant: Identity | None = None,
               optional: bool = True) -> SchematicSite | None:
        if not plan.feasible or not self.can_start(team, now, optional=optional):
            return None
        self._evict()
        site = SchematicSite(self.next_id, team, plan, purpose, owner, now, now + ttl,
                             priority, requester, frozenset(int(v) for v in builders_allowed),
                             occupant, max(1, plan.placement.schematic.max_builders),
                             region_for(plan.placement), progress_at=now)
        self.next_id += 1
        self.sites[site.site_id] = site
        if optional:
            self.team_ready[team] = now + OPTIONAL_TEAM_COOLDOWN
            self.optional_counts[team] += 1
        self.metrics["sites_started"] += 1
        self.metrics[f"sites_started:{site.name}"] += 1
        return site

    def request(self, solid: SolidFn, team: int, schematic: Schematic | str | Placement,
                anchor: Vector3, now: float, *, facing: tuple[float, float] = (1.0, 0.0),
                rotation: int | None = None, exact: bool = True, purpose: str = "",
                requester: str = "strategy", builders: Iterable[int] = (),
                priority: float = 0.95, ttl: float = 90.0,
                owner: Identity = (-1, -1, -1)) -> SchematicSite | None:
        """External API: plan and register a site regardless of optional cooldowns."""

        if isinstance(schematic, Placement):
            plan = plan_build(solid, schematic)
        else:
            shape = get_schematic(schematic) if isinstance(schematic, str) else schematic
            if shape is None:
                return None
            if rotation is not None:
                ground = ground_under(solid, math.floor(anchor[0]), math.floor(anchor[1]), anchor[2])
                if ground is None:
                    return None
                placement, _why = fit(shape, solid, (math.floor(anchor[0]), math.floor(anchor[1])),
                                      ground, rotation)
                plan = plan_build(solid, placement) if placement is not None else None
            else:
                choice = find_schematic_site(solid, shape, anchor, facing, exact=exact,
                                             reserved=self.reserved_cells(team))
                plan = choice.plan if choice is not None else None
        if plan is None or not plan.feasible:
            self.metrics["requests_rejected"] += 1
            return None
        site = self.create(team, plan, purpose or plan.placement.schematic.purpose, owner, now,
                           ttl=ttl, priority=priority, requester=requester,
                           builders_allowed=builders, optional=False)
        if site is not None:
            self.metrics["requests_accepted"] += 1
        return site

    def cancel(self, site_id: int, reason: str = "cancelled") -> None:
        site = self.sites.get(site_id)
        if site is not None and site.active:
            self._abandon(site, reason)

    def reserved_cells(self, team: int, exclude: int = -1) -> frozenset[Cell]:
        cells: set[Cell] = set()
        for site in self.sites.values():
            if site.team == team and site.active and site.site_id != exclude:
                cells |= site.cells
        return frozenset(cells)

    # --- lifecycle --------------------------------------------------------------

    def refresh(self, solid: SolidFn, now: float,
                alive: dict[tuple[int, int], int] | None = None) -> None:
        for site in tuple(self.sites.values()):
            if not site.active:
                continue
            for index, step in enumerate(site.plan.steps):
                if index not in site.done and not step_remaining(solid, step):
                    site.done.add(index)
                    site.claims.pop(index, None)
                    site.progress_at = max(site.progress_at, now - 1e-6)
            site.claims = {i: c for i, c in site.claims.items() if c.until > now
                           and (alive is None or alive.get(c.builder[:2]) == c.builder[2])}
            site.builders = {b: t for b, t in site.builders.items() if now - t < 6.0}
            if len(site.done) + len(site.skipped) >= len(site.plan.steps):
                if site.skipped and len(site.done) < .85 * len(site.plan.steps):
                    self._abandon(site, "steps_rejected")
                else:
                    site.completed_at = now
                    self.metrics["sites_completed"] += 1
                    self.metrics[f"sites_completed:{site.name}"] += 1
                    self._record(site, "completed", now)
            elif now >= site.expires_at:
                self._abandon(site, "expired")
            elif now - site.progress_at > SITE_IDLE_TIMEOUT:
                self._abandon(site, "no_progress")

    def _abandon(self, site: SchematicSite, reason: str) -> None:
        site.abandoned = reason
        site.claims.clear()
        self.metrics["sites_abandoned"] += 1
        self.metrics[f"sites_abandoned:{reason}"] += 1
        self._record(site, "abandoned:" + reason, site.progress_at)

    def _record(self, site: SchematicSite, outcome: str, now: float) -> None:
        self.history.append({**site.status(), "outcome": outcome, "at": now})

    def _evict(self) -> None:
        finished = sorted((s for s in self.sites.values() if not s.active),
                          key=lambda s: s.completed_at or s.created_at)
        while len(self.sites) >= MAX_SITES and finished:
            self.sites.pop(finished.pop(0).site_id, None)

    def forget(self, builder: Identity | tuple[int, int]) -> None:
        key = tuple(builder[:2])
        for site in self.sites.values():
            site.claims = {i: c for i, c in site.claims.items() if c.builder[:2] != key}

    # --- builders ---------------------------------------------------------------

    def site_for(self, team: int, player_id: int, position: Vector3, *,
                 requested_only: bool = False) -> SchematicSite | None:
        """Nearest active site this builder may join (respecting builder caps)."""

        best = None
        best_distance = math.inf
        for site in self.sites.values():
            if site.team != team or not site.active:
                continue
            if site.builders_allowed and player_id not in site.builders_allowed:
                continue
            if requested_only and not site.requester:
                continue
            distance = math.dist(site.centre, position)
            if distance > BUILDER_RADIUS:
                continue
            busy = {b[:2] for b in site.builders}
            if len(busy) >= site.max_builders and not any(b[0] == player_id for b in busy):
                continue
            if distance < best_distance:
                best, best_distance = site, distance
        return best

    def claim(self, site: SchematicSite, builder: Identity, position: Vector3,
              solid: SolidFn, now: float, *, others: Sequence[Vector3] = (),
              prefer_inside: bool = False, seed: int = 0) -> tuple[BuildStep, Node] | None:
        """Claim the next ready step and a stand for ``builder``.

        Ready = drag start supported now, not done/claimed/backed off. Other
        builders' claimed cells and stand columns, and every other living
        body nearby, are excluded from this builder's stand and line.
        """

        site.builders[builder] = now
        for index, claim in tuple(site.claims.items()):
            if claim.builder == builder:
                site.claims.pop(index, None)
        region = site.region or region_for(site.plan.placement)
        avoid_cells: set[Cell] = set()
        avoid_columns: set[tuple[int, int]] = set()
        for index, claim in site.claims.items():
            avoid_cells.update(site.plan.steps[index].cells)
            avoid_columns.add(claim.stand[:2])
        other_bodies: set[Cell] = set()
        for body in others:
            node = node_for_position(solid, body)
            if node is not None:
                other_bodies.update(body_cells(node))
                avoid_columns.add(node[:2])
            else:
                top = math.floor(body[2])
                other_bodies.update((math.floor(body[0]), math.floor(body[1]), z)
                                    for z in range(top, top + 3))
        start = node_for_position(solid, position)
        if start is not None and region.contains(start[0], start[1]):
            sources: list[Node] = [start]
        else:
            sources = boundary_sources(solid, region)
        distances = walk_distances(solid, region, sources)
        if start is not None and region.contains(start[0], start[1]) and len(distances) <= 1:
            distances = walk_distances(solid, region, boundary_sources(solid, region))
        occupant = site.plan.placement.occupant
        prefer = occupant[:2] if prefer_inside and occupant else None
        allowed = interior_columns(site.plan.placement) if prefer is not None else None
        candidates: list[tuple[float, int, BuildStep]] = []
        for index, step in enumerate(site.plan.steps):
            if (index in site.done or index in site.skipped or index in site.claims
                    or site.backoff.get(index, 0.0) > now):
                continue
            if not step_ready(solid, step):
                continue
            if any(cell in other_bodies for cell in step.cells):
                continue
            mid = step.cells[len(step.cells) // 2]
            distance = math.dist(position, (mid[0] + .5, mid[1] + .5, mid[2] + .5))
            rank = index * 0.6 + distance * 0.25
            if prefer is not None:
                rank += math.hypot(mid[0] + .5 - prefer[0], mid[1] + .5 - prefer[1]) * 0.8
            candidates.append((rank, index, step))
        candidates.sort(key=lambda item: item[:2])
        # An occupant first tries to build from exactly where it stands: in a
        # half-built box a one-cell move can make navigation detour over the
        # low walls. Only then may it shift within the interior.
        passes: list[frozenset[tuple[int, int]] | None] = [allowed]
        if allowed is not None and start is not None and start[:2] in allowed:
            passes.insert(0, frozenset({start[:2]}))
        for columns in passes:
            for _rank, index, step in candidates[:6]:
                remaining = step_remaining(solid, step)
                found = find_stand(solid, step.cells, region, distances=distances,
                                   avoid_cells=frozenset(avoid_cells | other_bodies),
                                   avoid_columns=frozenset(avoid_columns), prefer=prefer,
                                   allowed_columns=columns,
                                   ground_z=site.plan.placement.ground_z, seed=seed + index)
                if found is None or not remaining:
                    continue
                node, _walk = found
                site.claims[index] = StepClaim(builder, now + STEP_CLAIM_SECONDS, node)
                return step, node
        return None

    def extend_claim(self, site: SchematicSite, index: int, builder: Identity, now: float) -> None:
        claim = site.claims.get(index)
        if claim is not None and claim.builder == builder:
            claim.until = now + STEP_CLAIM_SECONDS

    def release(self, site: SchematicSite, index: int, builder: Identity) -> None:
        claim = site.claims.get(index)
        if claim is not None and claim.builder == builder:
            site.claims.pop(index, None)

    def step_built(self, site: SchematicSite, index: int, builder: Identity,
                   placed: int, now: float) -> None:
        step = site.plan.steps[index]
        site.claims.pop(index, None)
        site.done.add(index)
        site.progress_at = now
        if step.is_line:
            site.lines_built += 1
            site.line_cells += placed
            self.metrics["lines_built"] += 1
            self.metrics["line_cells"] += placed
        else:
            site.singles_built += 1
            site.single_cells += placed
            self.metrics["singles_built"] += 1
            self.metrics["single_cells"] += placed
        site.cells_by_builder[builder[0]] += placed

    def step_failed(self, site: SchematicSite, index: int, builder: Identity,
                    reason: str, now: float) -> None:
        site.claims.pop(index, None)
        site.failures[index] += 1
        site.total_failures += 1
        site.backoff[index] = now + STEP_BACKOFF * site.failures[index]
        self.metrics["steps_failed"] += 1
        self.metrics[f"steps_failed:{reason}"] += 1
        if site.failures[index] >= STEP_MAX_FAILURES:
            site.skipped.add(index)
        if site.total_failures >= SITE_MAX_FAILURES:
            self._abandon(site, "too_many_rejections")

    def under_fire(self, site: SchematicSite, now: float) -> bool:
        """Record incoming fire; True when the site should be abandoned."""

        site.under_fire.append(now)
        recent = sum(1 for t in site.under_fire if now - t <= UNDER_FIRE_WINDOW)
        if recent >= UNDER_FIRE_LIMIT and site.active:
            self._abandon(site, "under_fire")
            return True
        return False
