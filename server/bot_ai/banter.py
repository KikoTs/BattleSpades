"""Sparse, rate-limited bot chat that reacts to what just happened.

Some players never type and some cannot stop. Each bot gets a fixed
talkativeness from its name; most events produce nothing. Lines are queued
with a typing delay and hard global limits: at most one bot line every few
seconds and a small backlog, so chat can never flood the reliable channel.
"""

from __future__ import annotations

from dataclasses import dataclass
import random
import zlib

_GLOBAL_GAP = 6.0
_MAX_QUEUE = 3
_STALE_AFTER = 7.0

_LINES: dict[str, tuple[str, ...]] = {
    "kill": ("gotcha", "boom", "got one", "too slow", "sit down", "nice try {victim}",
             "there we go", "one down", "lol", "ez", "saw you coming {victim}"),
    "kill_long": ("what a shot", "did you see that", "long range special",
                  "{victim} never saw it", "sniped"),
    "kill_melee": ("spaded", "get dug", "shovel time", "up close and personal"),
    "kill_explosive": ("kaboom", "fire in the hole", "special delivery {victim}",
                       "watch your feet"),
    "streak": ("on fire", "{count} in a row", "cant stop wont stop",
               "anyone going to stop me?", "{count} streak lets go"),
    "revenge": ("payback {victim}", "got you back {victim}", "we're even {victim}",
                "remember me {victim}?"),
    "death": ("ouch", "nice shot", "ok that was clean", "how", "wow", "ns {killer}",
              "lag", "i blinked", "where was he", "aaand dead", "oof"),
    "death_streak_ended": ("finally got me", "fair enough {killer}", "good run while it lasted"),
    "death_repeat": ("{killer} again?!", "ok {killer} i see you", "{killer} chill",
                     "im coming for you {killer}"),
    "death_explosive": ("who threw that", "didnt even hear it", "nice nade {killer}"),
    "suicide": ("oops", "that was on purpose", "dont look at me", "gravity wins again"),
    "start": ("glhf", "gl all", "lets go", "hf", "good luck have fun"),
    "end": ("gg", "gg wp", "ggs", "good game all", "close one"),
}


@dataclass(slots=True)
class _Voice:
    chance: float
    next_at: float = 0.0
    nemesis: int = -1
    nemesis_count: int = 0
    last_line: str = ""


@dataclass(frozen=True, slots=True)
class _Queued:
    due_at: float
    player_id: int
    text: str
    while_alive: bool


class BotBanter:
    """Decide which bot says what, and when it is allowed out."""

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(int(seed) ^ 0x5EED)
        self._voices: dict[int, _Voice] = {}
        self._queue: list[_Queued] = []
        self._next_global_at = 0.0

    def register(self, player_id: int, name: str) -> None:
        # Talkativeness belongs to the identity, not to one match: roughly a
        # third of the roster never types at all.
        roll = (zlib.crc32(str(name).encode("utf-8")) & 0xFFFF) / 65535.0
        chance = 0.0 if roll < 0.35 else 0.10 + 0.30 * (roll - 0.35) / 0.65
        self._voices[int(player_id)] = _Voice(chance)

    def forget(self, player_id: int) -> None:
        self._voices.pop(int(player_id), None)
        self._queue = [item for item in self._queue if item.player_id != int(player_id)]

    def reset(self) -> None:
        self._queue.clear()
        for voice in self._voices.values():
            voice.nemesis, voice.nemesis_count = -1, 0

    def on_kill(self, now: float, *, killer_id: int, victim_id: int, killer_name: str,
                victim_name: str, streak: int, distance: float, melee: bool,
                explosive: bool) -> None:
        victim_voice = self._voices.get(int(victim_id))
        killer_voice = self._voices.get(int(killer_id))
        if killer_id == victim_id:
            self._say(now, victim_id, "suicide", {}, boost=1.5, alive=False)
            return
        if killer_voice is not None:
            revenge = killer_voice.nemesis == int(victim_id)
            if revenge:
                killer_voice.nemesis, killer_voice.nemesis_count = -1, 0
            kind = ("revenge" if revenge else "streak" if streak >= 3 and streak % 2 == 1
                    else "kill_melee" if melee else "kill_explosive" if explosive
                    else "kill_long" if distance >= 70.0 else "kill")
            self._say(now, killer_id, kind, {"victim": victim_name, "count": streak},
                      boost=2.0 if kind in {"revenge", "streak", "kill_long"} else 1.0,
                      alive=True)
        if victim_voice is not None:
            repeat = victim_voice.nemesis == int(killer_id)
            victim_voice.nemesis_count = victim_voice.nemesis_count + 1 if repeat else 1
            victim_voice.nemesis = int(killer_id)
            kind = ("death_repeat" if victim_voice.nemesis_count >= 2
                    else "death_explosive" if explosive else "death")
            self._say(now, victim_id, kind, {"killer": killer_name},
                      boost=2.0 if kind == "death_repeat" else 1.2, alive=False)

    def on_phase(self, now: float, kind: str) -> None:
        """``start`` or ``end``: one or two talkative bots greet or sign off."""

        speakers = [player_id for player_id, voice in self._voices.items() if voice.chance > 0.0]
        self._rng.shuffle(speakers)
        for player_id in speakers[:2]:
            self._say(now, player_id, kind, {}, boost=3.0, alive=True)

    def _say(self, now: float, player_id: int, kind: str, names: dict[str, object],
             *, boost: float, alive: bool) -> None:
        voice = self._voices.get(int(player_id))
        if (voice is None or voice.chance <= 0.0 or now < voice.next_at
                or len(self._queue) >= _MAX_QUEUE
                or self._rng.random() > voice.chance * boost):
            return
        options = [line for line in _LINES[kind] if line != voice.last_line]
        text = self._rng.choice(options).format(**names)[:60]
        voice.last_line = text
        voice.next_at = now + self._rng.uniform(25.0, 55.0)
        typing = 1.2 + 0.11 * len(text) + self._rng.uniform(0.0, 1.5)
        self._queue.append(_Queued(now + typing, int(player_id), text, alive))

    def due(self, now: float, *, busy: frozenset[int] = frozenset()) -> list[tuple[int, str]]:
        """Release at most one line; fighting bots keep their hands on the gun."""

        self._queue = [item for item in self._queue if now - item.due_at <= _STALE_AFTER]
        if now < self._next_global_at:
            return []
        for index, item in enumerate(self._queue):
            if item.due_at > now or (item.while_alive and item.player_id in busy):
                continue
            del self._queue[index]
            self._next_global_at = now + _GLOBAL_GAP
            return [(item.player_id, item.text)]
        return []
