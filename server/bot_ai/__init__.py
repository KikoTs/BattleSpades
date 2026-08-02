"""Deterministic server-owned bot runtime.

The package deliberately separates worker-safe data and decisions from the
authoritative gameplay objects in :mod:`server`.  Child processes never own a
``Player`` or mutate a VXL; they return short-lived intentions which the
gameplay thread validates and executes.
"""

from .director import BotDirector
from .gateway import BotActionGateway
from .messages import BotIntent, BotProfile, PerceptionFrame
from .supervisor import AIWorkerSupervisor
from .thread_supervisor import AIThreadSupervisor

__all__ = [
    "AIThreadSupervisor",
    "AIWorkerSupervisor",
    "BotActionGateway",
    "BotDirector",
    "BotIntent",
    "BotProfile",
    "PerceptionFrame",
]
