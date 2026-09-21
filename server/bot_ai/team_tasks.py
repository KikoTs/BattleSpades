"""Bounded worker-local task leases and reproducible tactical selection."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
import math

from .messages import BotAction, Vector3

Identity = tuple[int, int, int]


class TaskStage(str, Enum):
    APPROACH = "approach"
    EXECUTE = "execute"
    CONFIRM = "confirm"
    USE = "use"


@dataclass(frozen=True, slots=True)
class TacticalOrder:
    task_id: int
    role: str
    goal: Vector3
    look: Vector3 | None = None
    action: BotAction = BotAction()
    arrival_radius: float = 1.5
    hold: bool = False
    urgent: bool = False
    tool_id: int = -1


@dataclass(slots=True)
class TeamProject:
    project_id: int
    team: int
    kind: str
    owner: Identity
    position: Vector3
    lane: Vector3
    created_at: float
    expires_at: float
    stage: TaskStage = TaskStage.APPROACH
    participants: dict[Identity, float] = field(default_factory=dict)
    cells: tuple[tuple[int, int, int], ...] = ()
    last_progress_at: float = 0.0
    used_by: set[Identity] = field(default_factory=set)


class TeamTasks:
    """Reservations are suggestions to bots, never locks on human actions."""

    def __init__(self) -> None:
        self.projects: dict[int, TeamProject] = {}
        self.mischief_ready: dict[int, float] = {}
        self.mutation_times: dict[int, deque[tuple[float, int]]] = {}
        self.metrics: Counter[str] = Counter()
        self.events: deque[dict[str, object]] = deque(maxlen=256)

    def expire(self, now: float, observed: dict[tuple[int, int], tuple[bool, int]]) -> None:
        for project_id, project in tuple(self.projects.items()):
            owner = observed.get(project.owner[:2])
            if (now >= project.expires_at or owner is not None
                    and (not owner[0] or owner[1] != project.owner[2])):
                self.projects.pop(project_id, None)
                continue
            project.participants = {key: until for key, until in project.participants.items()
                                    if until > now and (key[:2] not in observed or
                                        observed[key[:2]] == (True, key[2]))}

    def reserve(self, project: TeamProject) -> bool:
        same_team = [p for p in self.projects.values() if p.team == project.team]
        if (len(same_team) >= 3 or len(self.projects) >= 12
                or any(p.owner == project.owner for p in same_team)
                or any(math.dist(p.position, project.position) < 7 for p in same_team)):
            return False
        self.projects[project.project_id] = project
        self.metrics["projects_started"] += 1
        return True

    def allow_mutation(self, team: int, now: float, cells: int) -> bool:
        history = self.mutation_times.setdefault(team, deque(maxlen=32))
        while history and now - history[0][0] >= 10:
            history.popleft()
        return 0 < cells <= 128 and sum(count for _, count in history) + cells <= 256

    def record_mutation(self, team: int, now: float, cells: int) -> None:
        history = self.mutation_times.setdefault(team, deque(maxlen=32))
        # Coalesce the newest bucket instead of evicting unexpired spending.
        # Extending its expiry is conservative and keeps memory bounded.
        if len(history) == history.maxlen:
            _, previous = history.pop()
            cells = min(257, previous + cells)
        history.append((now, cells))

    def event(self, kind: str, task_id: int, reason: str, now: float) -> None:
        self.metrics[kind] += 1
        self.events.append({"kind": kind, "task_id": task_id, "reason": reason, "at": now})
