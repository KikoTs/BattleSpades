"""Deterministic human-like profile and callsign generation."""

from __future__ import annotations

import random
from dataclasses import dataclass

import shared.constants as C

from .messages import BotProfile


# Nickname material. Bots should read like a lobby of ordinary players:
# handles people actually type, voxel/shovel jokes, and the odd tryhard tag,
# never a call-sign roster. Everything is ASCII and fits the retail 15-byte
# name field after decoration.
_HANDLES = (
    # Everyday gamer handles.
    "Pancake", "Noodles", "Biscuit", "Pickles", "Waffles", "Nugget",
    "Muffin", "Taco", "Pretzel", "Meatball", "Dumpling", "Toast",
    "Crumbs", "Beans", "Tater", "Sprout", "Pudding", "Churro",
    "Gumbo", "Nacho", "Cheddar", "Bagel", "Kiwi", "Mango",
    "Moose", "Goose", "Hamster", "Walrus", "Possum", "Gecko",
    "Ferret", "Llama", "Panda", "Koala", "Beaver", "Badger",
    "Otter", "Raccoon", "Pigeon", "Lobster", "Narwhal", "Capybara",
    "Frosty", "Sparky", "Rusty", "Dusty", "Smokey", "Ziggy",
    "Buster", "Scooter", "Rocco", "Boomer", "Tank", "Diesel",
    "Chunk", "Skipper", "Bubbles", "Wobble", "Doodle", "Zippy",
    "Gizmo", "Widget", "Sprocket", "Pixel", "Glitch", "Lagger",
    "Ping", "Noob", "Newbie", "Rookie", "Camper", "Spawny",
    # Ace of Spades flavour: blocks, shovels, voxels, bunkers.
    "SpadeLord", "BlockHead", "DirtDigger", "MoleMan", "Trencher",
    "Sandbag", "Bunker", "Voxelman", "Cubey", "Blocky", "Bricky",
    "Shovel", "Pickaxe", "Digger", "Tunneler", "Sapper", "Grenadier",
    "BlockBandit", "DirtNap", "MudMonkey", "SodBuster", "Gravel",
    "Scaffold", "Pillbox", "Foxhole", "Dugout", "Rampart", "Parapet",
    "Intel", "FlagRunner", "Tentmaker", "Jetboy", "RocketMan",
    "Sniperwolf", "Headhunter", "Spadewalker", "Cratebox", "Minesweep",
    # Sillier and more personal ones.
    "JustVibing", "SadPotato", "AngryToast", "LazyLlama",
    "SleepyBear", "HungryHippo", "CoffeeCup", "TeaTime", "Chonk",
    "Snacks", "SnackAttack", "BigSnacc", "Yeet", "Oof", "Bonk",
    "Bruh", "Kaboom", "Splat", "Thunk", "Kerplunk", "Boop",
    "Grumpy", "Wiggles", "Tofu", "Pesto", "Mochi", "Ramen",
)
_TITLES = ("Sgt", "Cpl", "Pvt", "Doc", "Mr", "Captain", "Major", "Lil", "Big", "The")
_ADJECTIVES = (
    "Sneaky", "Lucky", "Rusty", "Dusty", "Crispy", "Salty", "Spicy",
    "Soggy", "Grumpy", "Sleepy", "Fuzzy", "Tiny", "Mega", "Turbo",
    "Silent", "Brave", "Clumsy", "Wild", "Swift", "Frosty", "Cosmic",
    "Rogue", "Shady", "Chill", "Toxic", "Lazy", "Mighty", "Wobbly",
)
_NOUNS = (
    "Mole", "Badger", "Fox", "Hawk", "Otter", "Raven", "Wolf", "Yak",
    "Potato", "Toaster", "Brick", "Shovel", "Cactus", "Pickle", "Goblin",
    "Turtle", "Ninja", "Pirate", "Viking", "Wizard", "Gnome", "Duck",
    "Sniper", "Digger", "Camper", "Rocket", "Bandit", "Noodle",
)
# The original name tables. They are no longer shown to players; they only
# replay the original draw pattern on the trait stream so every seeded
# roster keeps its personalities (see _burn_legacy_name_draws).
_LEGACY_CALLSIGNS = (
    "Atlas", "Bishop", "Bolt", "Comet", "Echo", "Flint", "Ghost",
    "Harbor", "Ibis", "Juno", "Kestrel", "Mako", "Nova", "Orbit",
    "Pixel", "Quartz", "Rook", "Sable", "Tango", "Vex", "Warden",
)
_LEGACY_ADJECTIVES = (
    "Blue", "Brisk", "Calm", "Copper", "Dusty", "Lucky", "Quiet",
    "Red", "Silver", "Swift", "Wild",
)
_LEGACY_NOUNS = (
    "Badger", "Crow", "Fox", "Hawk", "Mole", "Otter", "Raven",
    "Wolf", "Yak",
)
_CLASS_PREFERENCES = tuple(
    int(value)
    for value in getattr(C, "DEFAULT_TEAM_CLASSES", ())
)


@dataclass(frozen=True, slots=True)
class _DifficultyBand:
    skill: tuple[float, float]
    reaction: tuple[float, float]
    tracking: tuple[float, float]
    turn_speed: tuple[float, float]
    turn_acceleration: tuple[float, float]
    aim_noise: tuple[float, float]


_BANDS = {
    # aim_noise bands calibrated 2026-07-15 against scripts/bot_aim_benchmark
    # (25 blocks, stationary): casual lands roughly a third of its shots,
    # normal most, hard nearly all.  Live target tracking settles noise by a
    # skill-weighted factor (director._apply_motor), so raw values here are
    # deliberately higher than the pre-tracking era.
    "casual": _DifficultyBand(
        (0.20, 0.45), (0.38, 0.65), (0.18, 0.32),
        (2.0, 3.2), (7.0, 11.0), (0.20, 0.28),
    ),
    "normal": _DifficultyBand(
        (0.45, 0.72), (0.22, 0.45), (0.09, 0.20),
        (3.0, 4.8), (10.0, 17.0), (0.055, 0.105),
    ),
    "hard": _DifficultyBand(
        (0.70, 0.90), (0.18, 0.30), (0.05, 0.13),
        (4.0, 5.8), (14.0, 22.0), (0.022, 0.055),
    ),
}


class ProfileFactory:
    """Create reproducible profiles while guaranteeing unique wire names."""

    def __init__(self, seed: int = 0) -> None:
        self._rng = random.Random(int(seed))
        # Names come from their own stream so the nickname catalog can grow
        # without moving any seeded personality trait (replay fixtures).
        self._name_rng = random.Random(int(seed) * 7919 + 0x6E616D65)
        self._used_names: set[str] = set()
        self._legacy_used: set[str] = set()
        self._legacy_for: dict[str, str] = {}

    def release_name(self, name: str) -> None:
        """Allow a retired profile's name to be reused later."""

        key = str(name).casefold()
        self._used_names.discard(key)
        self._legacy_used.discard(self._legacy_for.pop(key, ""))

    def create(self, difficulty: str = "mixed") -> BotProfile:
        """Return one profile constrained to the configured difficulty mix."""

        selected = self._choose_difficulty(difficulty)
        band = _BANDS[selected]
        skill = self._uniform(band.skill)
        return BotProfile(
            name=self._unique_name(),
            difficulty=selected,
            skill=skill,
            aggression=self._rng.uniform(0.25, 0.90),
            caution=self._rng.uniform(0.20, 0.90),
            teamwork=self._rng.uniform(0.30, 0.95),
            creativity=self._rng.uniform(0.15, 0.85),
            reaction_time=self._uniform(band.reaction),
            tracking_delay=self._uniform(band.tracking),
            turn_speed=self._uniform(band.turn_speed),
            turn_acceleration=self._uniform(band.turn_acceleration),
            recoil_control=0.30 + skill * 0.65,
            burst_discipline=self._rng.uniform(0.35, 0.90),
            preferred_range=self._rng.uniform(14.0, 36.0),
            aim_noise=self._uniform(band.aim_noise),
            class_preferences=tuple(
                self._rng.sample(
                    _CLASS_PREFERENCES,
                    k=min(2, len(_CLASS_PREFERENCES)),
                )
            ),
        )

    def _choose_difficulty(self, requested: str) -> str:
        normalized = str(requested).lower()
        if normalized in _BANDS:
            return normalized
        # Approved mixed roster: 20% casual, 60% normal, 20% hard.
        roll = self._rng.random()
        if roll < 0.20:
            return "casual"
        if roll < 0.80:
            return "normal"
        return "hard"

    def _uniform(self, bounds: tuple[float, float]) -> float:
        return self._rng.uniform(bounds[0], bounds[1])

    def _unique_name(self) -> str:
        """Generate an ASCII name fitting the retail 3..15 byte field."""

        legacy = self._burn_legacy_name_draws()
        for _ in range(512):
            candidate = self._styled_name()[:15]
            key = candidate.casefold()
            if 3 <= len(candidate) <= 15 and candidate.isascii() and key not in self._used_names:
                self._used_names.add(key)
                self._legacy_for[key] = legacy
                return candidate
        # Deterministic bounded fallback if the catalog is somehow exhausted.
        index = len(self._used_names)
        candidate = f"Player{index:04d}"[:15]
        self._used_names.add(candidate.casefold())
        return candidate

    def _burn_legacy_name_draws(self) -> str:
        """Consume the trait stream exactly as the original name picker did.

        Returns the original pick (or "" for its fallback) so a retired bot
        frees it again, keeping later draws identical too.
        """

        rng = self._rng
        for _ in range(512):
            style = rng.randrange(4)
            if style == 0:
                candidate = rng.choice(_LEGACY_CALLSIGNS)
            elif style == 1:
                candidate = f"{rng.choice(_LEGACY_CALLSIGNS)}{rng.randrange(10, 100)}"
            elif style == 2:
                candidate = f"{rng.choice(_LEGACY_ADJECTIVES)}{rng.choice(_LEGACY_NOUNS)}"
            else:
                candidate = f"{rng.choice(_LEGACY_NOUNS)}{rng.randrange(2, 90)}"
            candidate = candidate[:15]
            if 3 <= len(candidate) <= 15 and candidate not in self._legacy_used:
                self._legacy_used.add(candidate)
                return candidate
        return ""

    def _styled_name(self) -> str:
        """One nickname in a style real lobbies are full of."""

        rng = self._name_rng
        roll = rng.random()
        if roll < 0.30:
            name = rng.choice(_HANDLES)
        elif roll < 0.50:
            name = f"{rng.choice(_HANDLES)}{rng.choice(_number_suffixes())}"
        elif roll < 0.68:
            name = f"{rng.choice(_ADJECTIVES)}{rng.choice(_NOUNS)}"
        elif roll < 0.78:
            name = f"{rng.choice(_TITLES)}{rng.choice(_HANDLES)}"
        elif roll < 0.88:
            name = f"{rng.choice(_ADJECTIVES)}_{rng.choice(_NOUNS)}".lower()
        elif roll < 0.95:
            name = f"{rng.choice(_HANDLES).lower()}{rng.randrange(1, 100)}"
        else:
            core = rng.choice(_NOUNS)
            name = f"xX{core}Xx" if rng.random() < 0.6 else f"x{core}x"
        if len(name) > 15:
            # Too long once decorated: fall back to the bare handle/noun.
            name = rng.choice(_HANDLES)
        return name
        # Deterministic bounded fallback if a tiny catalog is exhausted.
        index = len(self._used_names)
        candidate = f"Bot{index:04d}"[:15]
        self._used_names.add(candidate)
        return candidate


def _number_suffixes() -> tuple[str, ...]:
    """Suffixes people really append: short numbers, years and tags."""

    return ("7", "9", "11", "12", "13", "21", "23", "33", "42", "77", "88",
            "99", "123", "01", "02", "04", "07", "94", "97", "98", "2k",
            "_", "_x", "YT", "HD")
