"""One occupied VIP guard post, with a bounded relief approach.

The outgoing guard stays until relief actually arrives at the VIP's level.
This coordinator reads friendly state only; it cannot order or move humans.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .human_behavior import variation


@dataclass(slots=True)
class _Post:
    carrier: tuple[int, int, int]
    owner: tuple[int, int, int]
    rotate_at: float
    incoming: tuple[int, int, int] | None = None
    deadline: float = 0.


def _key(player):
    return player.player_id, player.generation, player.life_id


class GuardHandoffs:
    def __init__(self) -> None:
        self.posts: dict[int, _Post] = {}
        self.events: list[tuple[float, int, str]] = []

    def reset(self) -> None:
        self.posts.clear()
        self.events.clear()

    def guards(self, frame, team: int, vip) -> frozenset[int]:
        now = frame.created_at
        carrier = next((p for p in frame.players if p.player_id == vip.carrier_id
                        and p.alive and p.spawned and p.team == team), None)
        candidates = {_key(p): p for p in frame.players if p.team == team and p.is_bot
                      and p.alive and p.spawned and p.player_id != vip.carrier_id}
        if carrier is None or len(candidates) < 2:
            self.posts.pop(team, None)
            return frozenset()
        post = self.posts.get(team)
        if post is None or post.carrier != _key(carrier) or post.owner not in candidates:
            nearest = min(candidates, key=lambda k: (math.dist(candidates[k].position, vip.position), k))
            post = self.posts[team] = _Post(_key(carrier), nearest,
                now + 40 + 15 * variation(*nearest, frame.strategy_seed))
        if post.incoming is not None:
            replacement = candidates.get(post.incoming)
            if replacement is None or now >= post.deadline:
                self.events.append((now, team, "relief_unavailable"))
                post.incoming = None
                post.rotate_at = now + 20
            elif (replacement.grounded and not replacement.wade
                    and math.dist(replacement.position, vip.position) <= 6
                    and abs(replacement.position[2]-vip.position[2]) < 2):
                post.owner = post.incoming
                post.incoming = None
                post.rotate_at = now + 40 + 15 * variation(*post.owner, int(now))
                self.events.append((now, team, "relief_arrived"))
        if post.incoming is None and now >= post.rotate_at:
            alternatives = [key for key in candidates if key != post.owner
                            and candidates[key].carried_entity_id < 0]
            if alternatives:
                post.incoming = min(alternatives, key=lambda k: (
                    math.dist(candidates[k].position, vip.position),
                    variation(*k, int(now // 40), frame.strategy_seed)))
                post.deadline = now + 8
        del self.events[:-32]
        return frozenset(key[0] for key in (post.owner, post.incoming) if key is not None)
