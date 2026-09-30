"""Whole runs on the in-process lab and their summaries."""

from __future__ import annotations

import random
from collections import Counter
from typing import Iterable, Optional

import shared.constants as C

from . import scenarios
from .lab import Lab
from .link import PROFILES, NetProfile

# Reports that state a fact about legitimate play rather than a violation.
INFORMATIONAL = frozenset({"transition_death_credited"})

# The client flavours: (suffix, shot phase, update rate, hitches).
FLAVOURS = (
    ("retail", 0, 60.0, None),
    ("native", 1, 60.0, None),
    ("fast", 0, 60.7, None),          # game loop a little faster than 60 Hz
    ("slow", 1, 59.4, None),
    ("stutter", 0, 60.0, (7.0, 0.35)),  # freezes 0.35 s every 7 s
)


class Director:
    """Server-side events of a run: enemy bodies, damage, enemy grenades."""

    def __init__(self, lab: Lab) -> None:
        self.lab = lab
        self.rng = random.Random(lab.seed * 31 + 5)
        self.dummies = []

    def place_dummies(self, count: int = 4, team: int = 3) -> None:
        court = self.lab.court
        for index in range(count):
            position = court.stand(88, 24 + index * 7)
            dummy = self.lab.add_dummy(f"Target{index}", position, team=team)
            dummy.god_mode = True
            self.dummies.append(dummy)

    def event(self, client, name: str, **arguments) -> None:
        player = client.player
        if player is None or not player.alive:
            return
        source = self.dummies[0] if self.dummies else None
        if name == "hurt":
            player.damage(
                int(arguments.get("amount", 20)), source=source,
                kill_type=int(C.KILL.WEAPON_KILL) if hasattr(C.KILL, "WEAPON_KILL") else 0,
            )
        elif name == "kill":
            player.damage(
                1000, source=source,
                kill_type=int(C.KILL.WEAPON_KILL) if hasattr(C.KILL, "WEAPON_KILL") else 0,
            )
        elif name == "grenade_near":
            self._grenade_near(player, float(arguments.get("distance", 3.0)), source)

    def _grenade_near(self, player, distance: float, thrower) -> None:
        from shared.packet import UseOrientedItem

        server = self.lab.server
        angle = self.rng.uniform(0.0, 6.283)
        import math

        packet = UseOrientedItem()
        packet.loop_count = int(server.loop_count)
        packet.player_id = int(getattr(thrower, "id", 0))
        packet.tool = int(C.GRENADE_TOOL)
        packet.value = 0.35
        packet.position = (
            float(player.x) + math.cos(angle) * distance,
            float(player.y) + math.sin(angle) * distance,
            float(player.z) + 1.0,
        )
        packet.velocity = (0.0, 0.0, 0.0)
        if thrower is not None:
            server.spawn_grenade(thrower, packet)


def _context(lab: Lab, director: Director, client, lane: int, seed: int):
    def teleport(position):
        if client.player is not None and client.player.alive:
            client.player.set_position(*position)

    def server_event(name, **arguments):
        director.event(client, name, **arguments)

    return scenarios.Context(
        model=client.model, court=lab.court, rng=random.Random(seed),
        lane=lane, teleport=teleport, server_event=server_event, log=[],
    )


async def run_plans(
    *,
    profile: NetProfile | str,
    plans: Iterable[scenarios.Plan] = scenarios.PLANS,
    seconds: float = 120.0,
    seed: int = 1,
    anticheat_settings: Optional[dict] = None,
    config_overrides: Optional[dict] = None,
    flavours=FLAVOURS,
    segments=None,
    dummies: int = 4,
    map_name: Optional[str] = None,
    prepare=None,
) -> dict:
    """Run one client per plan on one network profile; return the results."""

    lab = Lab(
        profile=profile, seed=seed, anticheat_settings=anticheat_settings,
        config_overrides=config_overrides, map_name=map_name,
    )
    await lab.start()
    try:
        director = Director(lab)
        if dummies:
            director.place_dummies(dummies)
        contexts = []
        for index, plan in enumerate(plans):
            suffix, phase, hz, hitches = flavours[index % len(flavours)]
            client = lab.add_client(
                f"{plan.name}-{suffix}", team=2, class_id=plan.class_id,
                loadout=plan.loadout, prefabs=plan.prefabs, shot_phase=phase,
                hz=hz, hitches=hitches,
            )
            context = _context(lab, director, client, index, seed * 101 + index)
            contexts.append((client, plan, context))
        joined = await lab.join_all(60.0)
        for client, plan, context in contexts:
            client.model.script = scenarios.script(context, plan, segments)
        if prepare is not None:
            prepare(lab, director, contexts)
        await lab.run(seconds)
        results = lab.results()
        for client, plan, context in contexts:
            results[client.name]["segments"] = [name for _f, name in context.log]
            results[client.name]["flavour"] = client.name.rsplit("-", 1)[-1]
        return {
            "profile": lab.profile.name,
            "network": lab.profile.describe(),
            "seconds": seconds,
            "seed": seed,
            "joined": joined,
            "clients": results,
            "kicks": list(lab.kicks),
            "lines": list(lab.anticheat_lines),
        }
    finally:
        await lab.stop()


def summarize(run: dict) -> dict:
    """Totals per detector and per action class for one run."""

    detections: Counter = Counter()
    offenders: dict = {}
    sent: Counter = Counter()
    accepted: Counter = Counter()
    client_totals: Counter = Counter()
    for name, entry in run["clients"].items():
        for kind, count in entry["counts"].items():
            if kind.split(":")[0] in INFORMATIONAL:
                continue
            detections[kind] += int(count)
            offenders.setdefault(kind, []).append((name, int(count)))
        shots_by_tool = entry["client"]["shots_by_tool"]
        for tool, count in shots_by_tool.items():
            sent[f"shot:{tool}"] += int(count)
            accepted[f"shot:{tool}"] += int(
                entry["weapons"].get(
                    int(tool), entry["weapons"].get(str(tool), {})
                ).get("shots", 0)
            )
        for kind, client_key in (("block_line", "blocks_sent"),
                                 ("throw", "throws_sent"),
                                 ("reload", "reloads_sent")):
            sent[kind] += int(entry["client"].get(client_key, 0))
            accepted[kind] += int(entry["accepted"].get(kind, 0))
        for key in ("adjusts", "snaps", "no_history", "deaths", "spawns",
                    "blasts", "frames"):
            client_totals[key] += int(entry["client"].get(key, 0))
    shots_sent = sum(v for k, v in sent.items() if k.startswith("shot:"))
    shots_accepted = sum(v for k, v in accepted.items() if k.startswith("shot:"))
    return {
        "profile": run["profile"],
        "network": run["network"],
        "seconds": run["seconds"],
        "clients": len(run["clients"]),
        "detections": dict(detections),
        "offenders": offenders,
        "kicks": run["kicks"],
        "sent": dict(sent),
        "accepted": dict(accepted),
        "shots_sent": shots_sent,
        "shots_accepted": shots_accepted,
        "client": dict(client_totals),
    }
