"""Shared, costed demolition of publicly marked elevated objectives.

VIP attackers can use their ordinary spades against real foundations instead
of trying to pillar a hundred blocks independently. The proof, work slots and
live tool admission are shared; perception and native actions stay unchanged.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
import math
import time

from server.dig_profiles import best_navigation_dig_profile, melee_dig_positions
from .horde_strategy import dig_stand_spot
from .recovery_skills import ClimbAbilities, dig_aims, node_of, typical_level_cost
from .simple_navigation import visible_cell_aim
from .structure_collapse import (UNREACHABLE_COST, dig_cost, horde_floor,
                                  iter_approach, iter_plan_collapse, support_cells)


@dataclass(frozen=True, slots=True)
class SiegeOrder:
    stand: tuple[float,float,float]
    role: str
    aim: tuple[int,int,int] | None = None
    profile: object | None = None
    look: tuple[float,float,float] | None = None


@dataclass(slots=True)
class _Job:
    position: tuple[float,float,float]
    floor: int
    version: int
    search: object | None
    plan: object | None = None
    next_at: float = 0.
    retry_at: float = 0.
    updated_at: float = 0.
    remaining: int = -1
    progress_at: float = 0.
    diagnostics: dict = field(default_factory=dict)


class ObjectiveSiege:
    def __init__(self, world) -> None:
        self.world=world
        self.jobs: dict[tuple, _Job] = {}
        self.epoch=None
        self.metrics=Counter()
        self.events=deque(maxlen=64)

    def reset(self) -> None:
        self.jobs.clear()
        self.epoch=None

    def _added_near(self, job: _Job) -> bool:
        version=self.world.topology_version
        if job.version==version:
            return False
        history=getattr(self.world,"mutation_history",())
        if not history or history[0][0]>job.version+1:
            return True
        points=(job.position, *(job.plan.cut if job.plan is not None else ()))
        padding=3 if job.plan is not None else 48
        x0,x1=min(c[0] for c in points)-padding,max(c[0] for c in points)+padding
        y0,y1=min(c[1] for c in points)-padding,max(c[1] for c in points)+padding
        return any(v>job.version and added and x0<=x<=x1 and y0<=y<=y1
                   for v,x,y,added in history)

    def _begin(self, actor, target, friends, now: float) -> _Job:
        position=tuple(target.position)
        floor=horde_floor(self.world.solid,position,[p.position for p in friends])
        profile=best_navigation_dig_profile(actor.loadout)
        def cost(cell):
            if dig_cost(self.world.solid,cell,floor)>=UNREACHABLE_COST:
                return 1000
            return max(1,profile.swings_for_health(self.world.block_health(*cell)))
        diagnostics={}
        def search():
            approach=yield from iter_approach(self.world.solid,position,limit=384)
            plan=yield from iter_plan_collapse(self.world.solid,support_cells(self.world.solid,position),
                ground_floor_z=floor,node_limit=8192,cost_fn=cost,diagnostics=diagnostics)
            if plan is not None:
                moves=approach.moves_from(actor.position) if approach is not None else None
                walk_seconds=moves*.25 if moves is not None else math.inf
                abilities=ClimbAbilities.from_observer(actor)
                rise=max(0.,actor.position[2]-position[2])
                build_seconds=(rise*typical_level_cost(abilities)
                               if abilities.can_build and abilities.blocks>=math.ceil(rise) else math.inf)
                cut_seconds=plan.cost*profile.fire_interval+min(
                    math.dist(actor.position,tuple(v+.5 for v in cell)) for cell in plan.cut)/4.
                diagnostics.update(walk_seconds=walk_seconds if math.isfinite(walk_seconds) else None,
                    build_seconds=build_seconds if math.isfinite(build_seconds) else None,cut_seconds=cut_seconds)
                if min(walk_seconds,build_seconds)+2<cut_seconds:
                    diagnostics["reason"]="faster_existing_route" if walk_seconds<build_seconds else "faster_owned_construction"
                    self.metrics[diagnostics["reason"]]+=1
                    return None
            return plan
        self.metrics["analyses_started"]+=1
        return _Job(position,floor,self.world.topology_version,search(),
                    updated_at=now,progress_at=now,diagnostics=diagnostics)

    def order(self, frame, actor, decision) -> SiegeOrder | None:
        epoch=frame.map_epoch,frame.mode_epoch
        if self.epoch!=epoch:
            self.reset()
            self.epoch=epoch
        if (frame.mode_id not in {"vip","hc_vip"} or decision is None
                or decision.role not in {"vip_flank_attack","vip_sudden_death_assault","vip_mop_up"}
                or actor.carried_entity_id>=0 or not actor.can_shoot or not actor.can_dig
                or not actor.grounded or actor.wade):
            return None
        profile=best_navigation_dig_profile(actor.loadout)
        if profile is None:
            return None
        target=next((o for o in frame.objectives if o.kind=="vip" and o.team!=actor.team
            and actor.position[2]-o.position[2]>=6
            and math.dist(actor.position[:2],o.position[:2])<=48),None)
        if target is None:
            return None
        now=frame.created_at
        friendly_vip=next((o.carrier_id for o in frame.objectives if o.kind=="vip" and o.team==actor.team),-1)
        friends=[p for p in frame.players if p.team==actor.team and p.is_bot and p.alive and p.spawned
                 and p.player_id!=friendly_vip and p.carried_entity_id<0 and p.can_dig and p.can_shoot
                 and best_navigation_dig_profile(p.loadout) is not None
                 and math.dist(p.position[:2],target.position[:2])<=48]
        key=actor.team,target.carrier_id
        self.jobs={k:j for k,j in self.jobs.items() if now-j.updated_at<15}
        job=self.jobs.get(key)
        if job is None or math.dist(job.position,target.position)>2 or self._added_near(job):
            if len(self.jobs)>=4:
                self.jobs.pop(min(self.jobs,key=lambda k:self.jobs[k].updated_at))
            job=self.jobs[key]=self._begin(actor,target,friends,now)
        job.updated_at=now
        if now<job.retry_at:
            return None
        if job.search is not None and now>=job.next_at:
            job.next_at=now+.1
            deadline=time.perf_counter()+.0015
            while time.perf_counter()<deadline:
                try:
                    next(job.search)
                except StopIteration as result:
                    job.search=None
                    job.plan=result.value
                    if job.plan is None:
                        job.retry_at=now+8
                        self.metrics["no_legal_cut"]+=1
                        return None
                    job.progress_at=now
                    self.metrics["cuts_found"]+=1
                    break
        # Cover a distinct supported approach while the proof is pending or
        # other crews own the faces. This also prevents self-defeating pillars.
        angle=actor.player_id*2.399963
        point=(target.position[0]+8*math.cos(angle),target.position[1]+8*math.sin(angle),actor.position[2])
        node=self.world.surface(math.floor(point[0]),math.floor(point[1]),point[2],vertical_span=3,clearance=3)
        screen=SiegeOrder(node.position,"objective_siege_cover") if node is not None else None
        if job.plan is None:
            return screen
        cut=tuple(c for c in job.plan.cut if self.world.solid(*c))
        if not cut:
            self.events.append((now,target.carrier_id,"cut_removed_await_fall"))
            job.plan=None
            job.retry_at=now+.4
            return screen
        if len(cut)!=job.remaining:
            job.progress_at=now
            job.remaining=len(cut)
        if now-job.progress_at>12:
            self.events.append((now,target.carrier_id,"work_stalled"))
            self.metrics["stalled_cuts"]+=1
            job.plan=None
            job.retry_at=now+6
            return None
        job.version=self.world.topology_version
        free=list(friends)
        reserved=[]
        for cell in cut[:8]:
            stand=dig_stand_spot(self.world.solid,cell,job.floor,job.position,reserved=reserved)
            if stand is None or not free:
                continue
            owner=min(free,key=lambda p:(math.dist(p.position,stand),p.player_id))
            free.remove(owner)
            reserved.append(stand)
            if owner.player_id!=actor.player_id:
                continue
            gap=math.dist(actor.position[:2],stand[:2])
            if gap>1.5 or abs(actor.position[2]-stand[2])>1.2:
                return SiegeOrder(stand,"objective_foundation_approach")
            if gap>.25:
                return SiegeOrder(stand,"objective_foundation_align",profile=profile)
            protected={node_of(p.position) for p in friends if math.dist(p.position,actor.position)<8}
            aims=dig_aims(self.world,(cell,),protected,profile)
            for aim in aims or ():
                # Spawn/base construction exclusions forbid placing new
                # obstacles; they are not terrain-damage permissions. Dig
                # legality is enforced by the shared native melee authority.
                point=visible_cell_aim(self.world,actor.eye,aim)
                if point is not None:
                    return SiegeOrder(stand,"objective_foundation_cut",aim,profile,point)
            return SiegeOrder(stand,"objective_foundation_align",profile=profile)
        return screen
