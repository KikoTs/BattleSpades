"""Server-side evaluation of the retail Steam achievements.

Steam marks all 77 achievements game-server-set: the retail server evaluated
them and no client binary holds a rule. The rules here are rebuilt from the
descriptions in ``shared/achievement_table.py``; docs/ACHIEVEMENTS.md lists
the reading chosen for each one and the six that have no usable signal.

They run on every server. There is no official, ranked or write-token gate,
so a player's own Create Match server with bots unlocks them too.

Gameplay reports facts through the module-level hooks at the bottom of this
file; the engine owns the rules, thresholds and names. Every hook is a no-op
until the engine has started and never raises into gameplay.
"""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
import functools
import logging
import math
from pathlib import Path
import sqlite3
import time

import shared.constants as C
from server.game_constants import TEAM1, TEAM2
from shared.achievement_table import ACHIEVEMENTS, Achievement

logger = logging.getLogger(__name__)

BY_NAME: dict[str, Achievement] = {row.api_name: row for row in ACHIEVEMENTS}
#: Statistic -> its tiers as ``(threshold, api_name)``, lowest first.
TIERS: dict[str, tuple[tuple[int, str], ...]] = {}
for _row in ACHIEVEMENTS:
    if _row.stat:
        TIERS[_row.stat] = tuple(sorted(
            TIERS.get(_row.stat, ()) + ((int(_row.threshold), _row.api_name),)
        ))
del _row

ANNOUNCEMENT_STRING_ID = "ACHIEVEMENT_GAINED"
_PLAYABLE = (TEAM1, TEAM2)
_TRANSITION_KILLS = frozenset((
    int(C.FORCED_TEAM_CHANGE_KILL), int(C.TEAM_CHANGE_KILL), int(C.CLASS_CHANGE_KILL),
))
_WEAPON, _HEADSHOT, _MELEE = int(C.WEAPON_KILL), int(C.HEADSHOT_KILL), int(C.MELEE_KILL)

# A description names a weapon the way the retail profile statistics label
# it (server.profile_stats._TOOL_LABELS): "the spade" is SPADE on both the
# normal and the classic tool, "the shotgun" is not SHOTGUN2, and "the sniper
# rifle" is not the semi-auto SNIPER2, which has an achievement of its own.
SPADE_TOOLS = frozenset((int(C.SPADE_TOOL), int(C.CLASSIC_SPADE_TOOL)))
PICKAXE_TOOLS = frozenset((int(C.PICKAXE_TOOL),))
KNIFE_TOOLS = frozenset((int(C.KNIFE_TOOL),))
PISTOL_TOOLS = frozenset((int(C.PISTOL_TOOL), int(C.SNUB_PISTOL_TOOL)))
SNIPER_TOOLS = frozenset((int(C.SNIPER_TOOL),))
SNIPER2_TOOLS = frozenset((int(C.SNIPER2_TOOL),))
SHOTGUN_TOOLS = frozenset((int(C.SHOTGUN_TOOL),))
CLASSIC_RIFLE_TOOLS = frozenset((int(C.RIFLE_TOOL),))
SMG_TOOLS = frozenset((int(C.SMG_TOOL),))
MINIGUN_TOOLS = frozenset((int(C.MINIGUN_TOOL),))
GRENADE_KILLS = frozenset((int(C.GRENADE_KILL), int(C.CLASSIC_GRENADE_KILL)))
ROCKET_KILLS = frozenset((int(C.ROCKET_KILL), int(C.ROCKET2_KILL)))
ROCKET_PROJECTILES = frozenset(("rocket", "rocket2"))
DRILL_KILL = int(C.DRILL_KILL)

# Amounts the descriptions state for achievements without a Steam statistic.
STREAKS = {5: "misc_five_in_a_row", 10: "misc_ten_in_a_row", 15: "misc_fifteen_in_a_row"}
SNIPER_ACCURACY = {3: "sniper_accuracy", 6: "sniper_accuracy_hard"}
SNIPER2_RAPID_KILLS = int(C.SNIPER2_RAPID_KILL_ACHIEVE_COUNT)
SNIPER2_RAPID_SECONDS = float(C.SNIPER2_RAPID_KILL_ACHIEVE_TIME)
EXPLOSION_KILLS = 3
LOW_HEALTH_FRACTION = 0.10
ZOMBIE_ROUND_KILLS = 3
LAST_MAN_KILLS = {5: "lastman_kills_zombies_easy", 10: "lastman_kills_zombies_hard"}
LAST_MAN_SECONDS = 28.0
DIAMOND_HOTFOOT_SECONDS = 5.0
DIAMONDS_FOUND_PER_MATCH = 3
DEMOLITION_ROUND_DAMAGE = 100
MINIGUN_STRUCTURE_BLOCKS = 50
GRENADE_STRUCTURE_BLOCKS = 100
TURRET_KILLS = 10
TURRET_EVADED_BLOCKS = 100
HEALTH_FROM_DROPS = 150
# A charge counts as "below" its victim from half a block under the feet:
# the side or underside of the floor block, never its top face.
DYNAMITE_BELOW_BLOCKS = 0.5

FLUSH_INTERVAL_SECONDS = 10.0
# A failed save is not retried (or logged) on every tick.
SAVE_RETRY_SECONDS = 5.0
# A strike is judged once none of its shells is in flight (they start 48
# blocks up at 100 blocks/s); the cap bounds one whose shells never resolve.
STRIKE_MIN_SECONDS = 1.0
STRIKE_MAX_SECONDS = 15.0
REGION_RESYNC_SECONDS = 1.0
REGION_RESYNC_CELLS = 512

# Conditions from the descriptions that the map's ``ac_*`` rows do not carry.
_MELEE_KILL_REGIONS = frozenset(("map_maya_kill",))
_ZOMBIE_VICTIM_KILL_REGIONS = frozenset(("map_zombieisland_zombie_kill",))
_ZOMBIE_DESTROY_REGIONS = frozenset(("map_zombieisland_destroy",))
_ROCKET_DESTROY_REGIONS = frozenset(("map_moon_destroy",))
_DEMOLITION_DESTROY_REGIONS = frozenset(("map_concrete_destroy",))

_EXCLUDED_MODES = frozenset(("ugc", "tut", "tutorial"))


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS counters (
    identity TEXT NOT NULL,
    stat TEXT NOT NULL,
    value INTEGER NOT NULL,
    PRIMARY KEY (identity, stat)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS unlocks (
    identity TEXT NOT NULL,
    api_name TEXT NOT NULL,
    unlocked_at REAL NOT NULL,
    player_name TEXT NOT NULL DEFAULT '',
    reported_at REAL,
    PRIMARY KEY (identity, api_name)
) WITHOUT ROWID;
"""


class AchievementStore:
    """SQLite store of lifetime counters and unlocked achievements.

    Counters are written as increments and unlocks with ``INSERT OR IGNORE``,
    so server processes sharing one file (a fleet) add to each other's
    progress instead of overwriting it. Writes run on the gameplay thread:
    they are small, batched, and use WAL so a commit does not wait for a
    disk sync. A locked file fails after ``BUSY_SECONDS`` and is retried.
    """

    BUSY_SECONDS = 0.05

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._connection: sqlite3.Connection | None = None

    def open(self) -> None:
        if self._connection is not None:
            return
        memory = self.path == ":memory:"
        if not memory:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path, timeout=self.BUSY_SECONDS, isolation_level=None,
            check_same_thread=False,
        )
        try:
            if not memory:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(_SCHEMA)
        except sqlite3.Error:
            connection.close()
            raise
        self._connection = connection

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            connection.close()

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            self.open()
        return self._connection

    def counters(self, identity: str) -> dict[str, int]:
        rows = self._db().execute(
            "SELECT stat, value FROM counters WHERE identity = ?", (identity,)
        ).fetchall()
        return {str(stat): int(value) for stat, value in rows}

    def unlocks(self, identity: str) -> dict[str, float]:
        rows = self._db().execute(
            "SELECT api_name, unlocked_at FROM unlocks WHERE identity = ?", (identity,)
        ).fetchall()
        return {str(name): float(at) for name, at in rows}

    def add_counters(self, rows) -> None:
        """Apply ``(identity, stat, delta)`` increments in one transaction."""
        rows = [(str(i), str(s), int(d)) for i, s, d in rows if int(d)]
        if not rows:
            return
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            db.executemany(
                "INSERT INTO counters (identity, stat, value) VALUES (?, ?, ?) "
                "ON CONFLICT (identity, stat) DO UPDATE SET value = value + excluded.value",
                rows,
            )
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")

    def unlock(self, identity: str, api_name: str, player_name: str, now: float) -> bool:
        """Record one unlock; False when it was already recorded."""
        cursor = self._db().execute(
            "INSERT OR IGNORE INTO unlocks (identity, api_name, unlocked_at, player_name) "
            "VALUES (?, ?, ?, ?)",
            (identity, api_name, float(now), str(player_name)),
        )
        return cursor.rowcount == 1

    def take_unreported(self, identity: str, now: float) -> list[str]:
        """Unlocks not yet handed to the master, marked as handed over."""
        db = self._db()
        db.execute("BEGIN IMMEDIATE")
        try:
            names = [str(row[0]) for row in db.execute(
                "SELECT api_name FROM unlocks WHERE identity = ? AND reported_at IS NULL "
                "ORDER BY unlocked_at, api_name",
                (identity,),
            )]
            if names:
                db.execute(
                    "UPDATE unlocks SET reported_at = ? "
                    "WHERE identity = ? AND reported_at IS NULL",
                    (float(now), identity),
                )
        except BaseException:
            db.execute("ROLLBACK")
            raise
        db.execute("COMMIT")
        return names


# ---------------------------------------------------------------------------
# Transient state
# ---------------------------------------------------------------------------

@dataclass
class Tracker:
    """One connection's progress that is not a lifetime counter."""

    identity: str | None = None
    resolved: bool = False
    # Reset at match start; ``round`` also at each Zombie/VIP sub-round.
    match: dict = field(default_factory=dict)
    round: dict = field(default_factory=dict)
    # Fractions behind the integer lifetime counters (blocks run, seconds).
    remainder: dict = field(default_factory=dict)
    last_position: tuple | None = None
    sniper_streak: int = 0
    sniper2_kills: deque = field(default_factory=deque)
    # (source, kill type, charge was below the feet) of this life's last hit.
    last_hit: tuple | None = None


@dataclass
class BlastScope:
    """One explosion: its kills and destroyed blocks belong together."""

    thrower: object
    kill_type: int
    origin: tuple | None
    buried_mine: bool = False
    turret: object | None = None
    turret_target: object | None = None
    kills: int = 0
    blocks: int = 0
    victims: list = field(default_factory=list)


@dataclass
class _RegionState:
    region: object
    remaining: set = field(default_factory=set)
    contributors: dict = field(default_factory=dict)
    done: bool = False


@dataclass
class _Strike:
    zone: object
    survivors: dict
    stayed: dict
    elapsed: float = 0.0


@dataclass
class _ObjectiveTrack:
    hotfoot: object | None = None
    thief: object | None = None
    interceptor: object | None = None


def _team(player) -> int:
    try:
        return int(getattr(player, "team", -1))
    except (TypeError, ValueError):
        return -1


def _is_bot(player) -> bool:
    return bool(getattr(player, "is_bot", False))


def _position(player) -> tuple[float, float, float] | None:
    from server.combat_scores import position_of

    position = getattr(player, "position", None)
    return position_of(position if position is not None else player)


def _feet_z(player) -> float | None:
    position = _position(player)
    if position is None:
        return None
    crouched = bool(getattr(
        player, "hitbox_crouched",
        getattr(getattr(player, "input", None), "crouch", False),
    ))
    return position[2] + float(
        C.PLAYER_CROUCHING_POS_ABOVE_GROUND if crouched
        else C.PLAYER_STANDING_POS_ABOVE_GROUND
    )


def _contains(bounds, position) -> bool:
    x0, x1, y0, y1, z0, z1 = bounds
    x, y, z = position
    return x0 <= x <= x1 and y0 <= y <= y1 and z0 <= z <= z1


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class AchievementEngine:
    """Counters, unlocks and the per-match state behind the rules."""

    def __init__(self, server, *, store: AchievementStore | None = None,
                 clock=time.time, monotonic=time.monotonic) -> None:
        self.server = server
        self.config = getattr(getattr(server, "config", None), "achievements", None)
        self.store = store
        self.active = False
        self.faults: dict[str, int] = {}
        self._clock = clock
        self._monotonic = monotonic
        self._progress: dict[str, dict[str, int]] = {}
        self._unlocked: dict[str, dict[str, float]] = {}
        self._dirty: dict[tuple[str, str], int] = {}
        self._save_retry_at: dict[tuple[str, str], float] = {}
        self._flush_in = FLUSH_INTERVAL_SECONDS
        self._reset_match_state()

    def _reset_match_state(self) -> None:
        self._round_open = True
        self._blasts: list[BlastScope] = []
        self._projectile: tuple | None = None
        self._regions: list[_RegionState] = []
        self._region_resync_in = REGION_RESYNC_SECONDS
        self._strikes: list[_Strike] = []
        self._hill_scorers: dict[int, dict[int, object]] = {}
        self._zombie_teams: tuple[int, int] | None = None
        self._initial_zombies: list = []
        self._vips: dict | None = None
        self._diamonds: dict[int, _ObjectiveTrack] = {}
        self._bombs: dict[int, _ObjectiveTrack] = {}
        self._last_bomb_planter: object | None = None
        self._intel_interceptors: dict[int, object] = {}
        self._demolition_last_damager: dict[int, object] = {}

    # -- lifecycle ---------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.config, "enabled", True))

    @property
    def count_bot_kills(self) -> bool:
        return bool(getattr(self.config, "count_bot_kills", True))

    def start(self) -> bool:
        """Open the store; a file that cannot be opened falls back to memory
        so the round still announces unlocks (they are lost on restart).
        Never raises: achievements must not stop the server from starting."""
        if self.active or not self.enabled:
            return self.active
        if self.store is None:
            self.store = AchievementStore(
                getattr(self.config, "path", "") or ":memory:"
            )
        try:
            self.store.open()
        except Exception as error:  # noqa: BLE001 - see docstring
            logger.warning(
                "Achievement store %s is unavailable (%s); unlocks will not "
                "survive a restart", self.store.path, error,
            )
            self.store = AchievementStore(":memory:")
            try:
                self.store.open()
            except Exception:  # noqa: BLE001 - see docstring
                logger.exception("Achievements disabled: no store could be opened")
                return False
        self.active = True
        logger.info("Achievements enabled (store %s)", self.store.path)
        return True

    def close(self) -> None:
        if not self.active:
            return
        try:
            self.flush()
        finally:
            self.active = False
            if self.store is not None:
                self.store.close()

    def fault(self, name: str) -> None:
        """Count a failed hook; log the first failure of each kind in full."""
        count = self.faults[name] = self.faults.get(name, 0) + 1
        if count == 1:
            logger.exception("Achievement hook %s failed", name)

    def _round_counts(self) -> bool:
        """The open round is a real one: not an intermission, the tutorial
        or the map creator."""
        if not self.active or not self._round_open:
            return False
        config = getattr(self.server, "config", None)
        return not (bool(getattr(config, "ugc_runtime", False))
                    or str(getattr(config, "default_mode", "")).lower() in _EXCLUDED_MODES)

    def _live(self) -> bool:
        """That round is still being played (not its end screen)."""
        return self._round_counts() and not bool(
            getattr(getattr(self.server, "mode", None), "ended", False)
        )

    # -- identity and persistence -----------------------------------------

    def tracker(self, player) -> Tracker:
        state = getattr(player, "achievement_tracker", None)
        if state is None:
            state = player.achievement_tracker = Tracker()
        return state

    def identity_for(self, player) -> str | None:
        """Stable key of a human player; bots have none.

        A verified AoSPlay account wins, then the Steam relay's SteamID, then
        the name. The name is the only identity a direct legacy client has.
        """
        if player is None or _is_bot(player):
            return None
        state = self.tracker(player)
        if state.resolved:
            return state.identity
        identity = None
        legacy_id = str(getattr(player, "account_legacy_id", None) or "").strip()
        if legacy_id:
            identity = "aosplay:" + legacy_id
        if identity is None:
            relay = getattr(self.server, "steam_p2p", None)
            peer = getattr(getattr(player, "connection", None), "peer", None)
            lookup = getattr(relay, "identity_for", None)
            if peer is not None and callable(lookup):
                found = lookup(peer)
                if found:
                    identity = str(found)
        if identity is None:
            name = str(getattr(player, "name", "") or "").strip()
            if name:
                identity = "name:" + name.casefold()
        state.identity, state.resolved = identity, True
        return identity

    def _load(self, identity: str) -> None:
        if identity not in self._progress:
            self._progress[identity] = self.store.counters(identity)
            self._unlocked[identity] = self.store.unlocks(identity)

    def progress(self, identity: str) -> dict[str, int]:
        """Lifetime counters of ``identity`` including unflushed increments."""
        self._load(identity)
        return dict(self._progress[identity])

    def unlocked(self, identity: str) -> dict[str, float]:
        """``api_name -> unlock time`` for ``identity``."""
        self._load(identity)
        return dict(self._unlocked[identity])

    def flush(self) -> None:
        """Write pending counter increments; keep them when the store is busy."""
        if not self._dirty:
            return
        rows = [(identity, stat, delta) for (identity, stat), delta in self._dirty.items()]
        try:
            self.store.add_counters(rows)
        except sqlite3.Error as error:
            logger.warning("Achievement counters not saved yet: %s", error)
            return
        self._dirty.clear()

    def add(self, player, stat: str, amount: int = 1) -> list[str]:
        """Add to a lifetime counter and unlock every tier it now reaches."""
        if stat not in TIERS:
            raise KeyError(f"unknown achievement statistic {stat!r}")
        amount = int(amount)
        identity = self.identity_for(player)
        if identity is None or amount <= 0:
            return []
        self._load(identity)
        counters = self._progress[identity]
        value = counters[stat] = counters.get(stat, 0) + amount
        key = (identity, stat)
        self._dirty[key] = self._dirty.get(key, 0) + amount
        return [
            api_name for threshold, api_name in TIERS[stat]
            if value >= threshold and self.unlock(player, api_name)
        ]

    def add_fraction(self, player, stat: str, amount: float) -> None:
        """Accumulate a measure (seconds, blocks run) into a whole counter."""
        remainder = self.tracker(player).remainder
        value = remainder.get(stat, 0.0) + float(amount)
        whole = int(value + 1e-9)
        remainder[stat] = max(0.0, value - whole)
        if whole:
            self.add(player, stat, whole)

    def unlock(self, player, api_name: str) -> bool:
        """Unlock once: save it, then tell every client. True when new."""
        row = BY_NAME[api_name]
        identity = self.identity_for(player)
        if identity is None:
            return False
        self._load(identity)
        if api_name in self._unlocked[identity]:
            return False
        name = str(getattr(player, "name", "") or "").strip() or "Player"
        now = float(self._clock())
        retry_at = self._save_retry_at.get((identity, api_name))
        if retry_at is not None and self._monotonic() < retry_at:
            return False
        try:
            # Counters first, so the saved progress never trails its unlock.
            self.flush()
            created = self.store.unlock(identity, api_name, name, now)
        except sqlite3.Error as error:
            self._save_retry_at[(identity, api_name)] = self._monotonic() + SAVE_RETRY_SECONDS
            logger.warning("Achievement %s for %s not saved: %s", api_name, identity, error)
            return False
        self._save_retry_at.pop((identity, api_name), None)
        self._unlocked[identity][api_name] = now
        if not created:
            # Another server process sharing the store recorded it first.
            return False
        logger.info("Achievement unlocked: %s (%s) by %s [%s]",
                    row.display_name, api_name, name, identity)
        self._announce(name, row.display_name)
        return True

    def _announce(self, player_name: str, display_name: str) -> None:
        from server.announcements import broadcast_localised_overlay

        try:
            broadcast_localised_overlay(
                self.server, ANNOUNCEMENT_STRING_ID, (player_name, display_name)
            )
        except Exception:  # noqa: BLE001 - the unlock is already saved
            self.fault("announce")

    def take_unreported(self, identity: str) -> list[str]:
        """Newly unlocked api names to attach to a master round result."""
        if not self.active:
            return []
        return self.store.take_unreported(identity, float(self._clock()))

    def player_left(self, player) -> None:
        """Save a departing player's counters and forget the cached copy, so
        a later session reads progress another server may have added."""
        state = getattr(player, "achievement_tracker", None)
        identity = getattr(state, "identity", None)
        if not identity:
            return
        self.flush()
        if not any(key[0] == identity for key in self._dirty):
            self._progress.pop(identity, None)
            self._unlocked.pop(identity, None)

    # -- scopes ------------------------------------------------------------

    def _count(self, player, scope: str, key: str, amount: float = 1) -> float:
        counters = getattr(self.tracker(player), scope)
        value = counters[key] = counters.get(key, 0) + amount
        return value

    def _connected(self, player) -> bool:
        players = getattr(self.server, "players", None)
        if player is None or not isinstance(players, dict):
            return False
        try:
            return players.get(int(getattr(player, "id", -1))) is player
        except (TypeError, ValueError):
            return False

    def match_started(self) -> None:
        self._reset_match_state()
        for player in tuple(getattr(self.server, "players", {}).values()):
            state = self.tracker(player)
            state.match.clear()
            state.round.clear()
            state.sniper_streak = 0
            state.sniper2_kills.clear()
            state.last_hit = None
        self._build_regions()

    def round_started(self) -> None:
        """A Zombie or VIP sub-round begins inside the match."""
        self._round_open = True
        for player in tuple(getattr(self.server, "players", {}).values()):
            self.tracker(player).round.clear()

    def match_ended(self, winner) -> None:
        if self._vips is not None:
            # The match clock ran out inside a VIP round.
            self.vip_round_finished(getattr(self.server, "mode", None))
        if self._round_counts() and winner in _PLAYABLE:
            loser = TEAM2 if int(winner) == TEAM1 else TEAM1
            damager = self._demolition_last_damager.get(loser)
            if self._connected(damager) and _team(damager) == int(winner):
                self.unlock(damager, "demolition_final_damage")
            planter = self._last_bomb_planter
            if self._connected(planter) and _team(planter) == int(winner):
                self.unlock(planter, "bomb_final_bomber")
        self._round_open = False
        self._strikes.clear()
        self.flush()

    # -- combat ------------------------------------------------------------

    def _is_zombie(self, player) -> bool:
        return self._zombie_teams is not None and _team(player) == self._zombie_teams[0]

    def damaged(self, victim, source, amount, kill_type: int) -> None:
        if source is None or source is victim or float(amount) <= 0:
            return
        below = False
        scope = self._blasts[-1] if self._blasts else None
        if scope is not None:
            scope.victims.append(victim)
            if scope.origin is not None and int(kill_type) == int(C.DYNAMITE_KILL):
                feet = _feet_z(victim)
                below = feet is not None and scope.origin[2] - feet >= DYNAMITE_BELOW_BLOCKS
        self.tracker(victim).last_hit = (source, int(kill_type), below)

    def shot_resolved(self, player, tool: int, hit: bool) -> None:
        """A sniper-rifle trigger pull that damaged nobody ends the run."""
        if not hit and int(tool) in SNIPER_TOOLS:
            self.tracker(player).sniper_streak = 0

    def died(self, victim, killer, kill_type: int, victim_jetpacking: bool = False) -> None:
        victim_state = self.tracker(victim)
        last_hit, victim_state.last_hit = victim_state.last_hit, None
        kill_type = int(kill_type)
        if kill_type in _TRANSITION_KILLS or not self._live():
            return
        if killer is None or killer is victim:
            return
        killer_team, victim_team = _team(killer), _team(victim)
        if (killer_team == victim_team or killer_team not in _PLAYABLE
                or victim_team not in _PLAYABLE):
            return
        # Every player's tally, bots included: "the highest number of kills"
        # is judged against the whole server.
        self._count(killer, "round", "kills")
        if _is_bot(killer) or (_is_bot(victim) and not self.count_bot_kills):
            return
        state = self.tracker(killer)
        self._count(killer, "round", "counted_kills")

        tool = int(getattr(killer, "tool", -1))
        gun = kill_type in (_WEAPON, _HEADSHOT)
        headshot = kill_type == _HEADSHOT
        melee = kill_type == _MELEE
        killer_zombie = self._is_zombie(killer)
        victim_zombie = self._is_zombie(victim)
        scope = self._blasts[-1] if self._blasts else None

        streak = STREAKS.get(int(getattr(killer, "kill_streak", 0)))
        if streak is not None:
            self.unlock(killer, streak)

        if melee:
            if tool in SPADE_TOOLS:
                self.add(killer, "spade_kill_count")
            elif tool in PICKAXE_TOOLS:
                self.add(killer, "pickaxe_kill_count")
            elif tool in KNIFE_TOOLS and victim_zombie:
                self.add(killer, "knife_zombie_count")
        if headshot:
            if tool in SNIPER_TOOLS:
                self.add(killer, "sniper_kill_count")
            elif tool in SHOTGUN_TOOLS:
                self.add(killer, "shotgun_headshots_count")
            elif tool in CLASSIC_RIFLE_TOOLS:
                self.add(killer, "classic_rifle_headshot_count")
            elif tool in PISTOL_TOOLS and victim_zombie:
                self.add(killer, "pistol_zombie_kill_count")
        if gun and tool in SNIPER_TOOLS:
            state.sniper_streak += 1
            accuracy = SNIPER_ACCURACY.get(state.sniper_streak)
            if accuracy is not None:
                self.unlock(killer, accuracy)
        if gun and tool in SNIPER2_TOOLS:
            now = self._monotonic()
            recent = state.sniper2_kills
            recent.append(now)
            while recent and now - recent[0] > SNIPER2_RAPID_SECONDS:
                recent.popleft()
            if len(recent) >= SNIPER2_RAPID_KILLS:
                self.unlock(killer, "sniper2_rapid_kill")

        if bool(getattr(killer, "jetpack_active", False)):
            self.add(killer, "jetpack_kill_count")
            if gun and tool in SMG_TOOLS:
                self.add(killer, "jetpack_smg_kill_count")
        if victim_jetpacking:
            self.unlock(killer, "jetpack_killed_using")

        try:
            health = float(getattr(killer, "health", 0))
            full = float(getattr(killer, "max_health", 0) or C.INITIAL_HEALTH)
        except (TypeError, ValueError):
            health = full = 0.0
        if bool(getattr(killer, "alive", False)) and 0 < health < full * LOW_HEALTH_FRACTION:
            self.add(killer, "low_health_kills")

        # A fall is credited to the last enemy who hurt this life; what hurt
        # it decides which push the fall belongs to.
        hit_source, hit_kill, hit_below = last_hit or (None, -1, False)
        if kill_type == int(C.FALL_KILL):
            if killer_zombie:
                self.unlock(killer, "zombie_fall")
            if hit_source is killer and hit_kill in ROCKET_KILLS:
                self.unlock(killer, "rocket_fall")
        if (hit_source is killer and hit_below and hit_kill == int(C.DYNAMITE_KILL)
                and kill_type in (int(C.DYNAMITE_KILL), int(C.FALL_KILL))):
            self.add(killer, "dynamite_below_count")

        if scope is not None and scope.thrower is killer and kill_type == scope.kill_type:
            scope.kills += 1
            if scope.kills == EXPLOSION_KILLS:
                self.unlock(killer, "misc_triple_explosion")
            if kill_type == int(C.LANDMINE_KILL) and scope.buried_mine:
                self.add(killer, "landmine_hidden_count")
            turret = scope.turret
            if turret is not None and kill_type == int(C.ROCKET_TURRET_KILL):
                kills = turret.achievement_kills = int(getattr(turret, "achievement_kills", 0)) + 1
                if kills == TURRET_KILLS:
                    self.unlock(killer, "turret_accuracy")

        if killer_zombie:
            if self._count(killer, "round", "zombie_kills") == ZOMBIE_ROUND_KILLS:
                self.unlock(killer, "zombie_kills_humans")
            if bool(getattr(killer, "wade", False)):
                self.add(killer, "zombie_kills_in_water")
        mode = getattr(self.server, "mode", None)
        if victim_zombie and getattr(mode, "last_survivor_id", None) == getattr(killer, "id", -1):
            last_man = LAST_MAN_KILLS.get(int(self._count(killer, "round", "last_man_kills")))
            if last_man is not None:
                self.unlock(killer, last_man)

        if str(getattr(mode, "mode_code", "") or "").lower() == "cctf":
            holders = getattr(mode, "intel_holder", None)
            if isinstance(holders, dict) and any(h is killer for h in holders.values()):
                self.add(killer, "classic_kills_with_intel_count")

        position = _position(killer)
        if position is not None:
            for entry in self._regions:
                region = entry.region
                if (region.kind != int(C.ACH_KILL_REGION)
                        or region.team not in (None, killer_team)
                        or not _contains(region.bounds, position)):
                    continue
                if region.api_name in _MELEE_KILL_REGIONS and not melee:
                    continue
                if region.api_name in _ZOMBIE_VICTIM_KILL_REGIONS and not victim_zombie:
                    continue
                kills = self._count(killer, "round", "region:" + region.api_name)
                if kills == max(1, int(region.kills)):
                    self.unlock(killer, region.api_name)

    # -- explosions --------------------------------------------------------

    def projectile_exploding(self, explosion, thrower) -> None:
        """Called right before a projectile's blast: direct rocket hits, and
        which turret (aimed at whom) fired a turret rocket."""
        spec = getattr(explosion, "spec", None)
        self._projectile = (
            int(getattr(spec, "kill_type", -1)),
            getattr(explosion, "turret", None),
            getattr(explosion, "turret_target_id", None),
        )
        target_id = getattr(explosion, "contact_player_id", None)
        if (target_id is None or thrower is None or _is_bot(thrower) or not self._live()
                or str(getattr(spec, "name", "")) not in ROCKET_PROJECTILES):
            return
        target = getattr(self.server, "players", {}).get(int(target_id))
        if (target is None or target is thrower or _team(target) == _team(thrower)
                or _team(target) not in _PLAYABLE or _team(thrower) not in _PLAYABLE):
            return
        if _is_bot(target) and not self.count_bot_kills:
            return
        if bool(getattr(thrower, "alive", False)) and bool(getattr(thrower, "airborne", False)):
            self.add(thrower, "airborne_rocket_count")

    def blast_begin(self, thrower, kill_type, origin=None) -> BlastScope:
        scope = BlastScope(thrower=thrower, kill_type=int(kill_type), origin=origin)
        pending, self._projectile = self._projectile, None
        if pending is not None and pending[0] == scope.kill_type:
            scope.turret = pending[1]
            if pending[2] is not None:
                scope.turret_target = getattr(self.server, "players", {}).get(int(pending[2]))
        if scope.kill_type == int(C.LANDMINE_KILL) and origin is not None:
            # The blast starts at the mine: a solid cell there is the block
            # someone put back over it.
            world = getattr(self.server, "world_manager", None)
            solid = getattr(world, "get_solid", None)
            if callable(solid):
                scope.buried_mine = bool(solid(*(math.floor(value) for value in origin)))
        self._blasts.append(scope)
        return scope

    def blast_end(self, scope: BlastScope) -> None:
        if scope in self._blasts:
            self._blasts.remove(scope)
        target = scope.turret_target
        if (scope.blocks and target is not None and not _is_bot(target) and self._live()
                and bool(getattr(target, "alive", False))
                and _team(target) != _team(scope.thrower)
                and all(victim is not target for victim in scope.victims)):
            # The rocket was aimed at this player, missed, and broke terrain.
            if self._count(target, "match", "turret_evaded_blocks", scope.blocks) >= TURRET_EVADED_BLOCKS:
                self.unlock(target, "turret_evasion")

    # -- terrain -----------------------------------------------------------

    def _build_regions(self) -> None:
        world = getattr(self.server, "world_manager", None)
        metadata = getattr(world, "map_metadata", None)
        self._regions = []
        # Only stock maps: a custom map must not hand out retail volumes.
        if metadata is None or not bool(getattr(metadata, "official_map", False)):
            return
        solid = getattr(world, "get_solid", None)
        already_razed = set()
        for region in getattr(metadata, "achievement_regions", ()) or ():
            if region.api_name not in BY_NAME:
                continue
            entry = _RegionState(region)
            if region.kind == int(C.ACH_BLOCK_DESTROY_REGION):
                if not callable(solid):
                    continue
                x0, x1, y0, y1, z0, z1 = (math.floor(value) for value in region.bounds)
                entry.remaining = {
                    (x, y, z)
                    for x in range(x0, x1 + 1)
                    for y in range(y0, y1 + 1)
                    for z in range(z0, z1 + 1)
                    if solid(x, y, z)
                }
                if not entry.remaining:
                    already_razed.add(region.api_name)
            elif region.kind != int(C.ACH_KILL_REGION):
                continue
            self._regions.append(entry)
        # A same-map restart keeps the terrain: a structure with a volume
        # already gone cannot be destroyed again this match.
        self._regions = [
            entry for entry in self._regions
            if entry.region.kind != int(C.ACH_BLOCK_DESTROY_REGION)
            or entry.region.api_name not in already_razed
        ]

    def _finish_regions(self, api_name: str) -> None:
        """Every volume of one achievement is rubble: credit whoever took
        part in each of them."""
        entries = [
            entry for entry in self._regions
            if entry.region.api_name == api_name
            and entry.region.kind == int(C.ACH_BLOCK_DESTROY_REGION)
        ]
        if not entries or any(entry.remaining or entry.done for entry in entries):
            return
        for entry in entries:
            entry.done = True
        shared = set(entries[0].contributors)
        for entry in entries[1:]:
            shared &= set(entry.contributors)
        for key in shared:
            player = entries[0].contributors[key]
            if self._connected(player):
                self.unlock(player, api_name)

    def _note_region_cells(self, player, cells, cause) -> None:
        team = _team(player) if player is not None else -1
        mode = getattr(self.server, "mode", None)
        for entry in self._regions:
            if entry.done or not entry.remaining:
                continue
            hit = entry.remaining.intersection(cells)
            if not hit:
                continue
            entry.remaining.difference_update(hit)
            region = entry.region
            credited = (
                player is not None and not _is_bot(player) and self._live()
                and region.team in (None, team)
            )
            if credited and region.api_name in _ZOMBIE_DESTROY_REGIONS:
                credited = self._is_zombie(player)
            if credited and region.api_name in _ROCKET_DESTROY_REGIONS:
                credited = cause in ROCKET_KILLS
            if credited and region.api_name in _DEMOLITION_DESTROY_REGIONS:
                credited = str(getattr(mode, "mode_code", "") or "").lower() == "dem"
            if credited:
                entry.contributors[id(player)] = player
            if not entry.remaining:
                self._finish_regions(region.api_name)

    def _resync_regions(self) -> None:
        """Cells removed outside the combat path (fire, goo) still count as
        gone; without this a nearly razed volume could never complete."""
        solid = getattr(getattr(self.server, "world_manager", None), "get_solid", None)
        if not callable(solid):
            return
        for entry in self._regions:
            if entry.done or not 0 < len(entry.remaining) <= REGION_RESYNC_CELLS:
                continue
            entry.remaining = {cell for cell in entry.remaining if solid(*cell)}
            if not entry.remaining:
                self._finish_regions(entry.region.api_name)

    def blocks_destroyed(self, player, removed, collapsed, chunks, shot_tool=None) -> None:
        cells = [tuple(int(value) for value in cell) for cell in removed]
        cells.extend(tuple(int(value) for value in cell) for cell in collapsed)
        if not cells:
            return
        scope = self._blasts[-1] if self._blasts else None
        cause = scope.kill_type if scope is not None else None
        if self._regions:
            self._note_region_cells(player, cells, cause)
        if scope is not None:
            scope.blocks += len(cells)
        if player is None or not self._live():
            return
        mode = getattr(self.server, "mode", None)
        base_damage = getattr(mode, "enemy_objective_damage", None)
        damage = base_damage(player, cells) if callable(base_damage) else None
        if damage:
            # Bots too: the final blow to a base may be a bot's.
            self._demolition_last_damager[int(damage[0])] = player
        if _is_bot(player):
            return
        if self._is_zombie(player):
            self.add(player, "block_as_zombie_count", len(cells))
        if cause in GRENADE_KILLS:
            structures = sum(1 for chunk in chunks if len(chunk) >= GRENADE_STRUCTURE_BLOCKS)
            if structures:
                self.add(player, "grenade_demolish_count", structures)
        elif (scope is None and shot_tool is not None and int(shot_tool) in MINIGUN_TOOLS
              and any(len(chunk) >= MINIGUN_STRUCTURE_BLOCKS for chunk in chunks)):
            self.unlock(player, "minigun_demolish")

        if damage:
            count = int(damage[1])
            self.add(player, "demolition_damage_many_rounds_count", count)
            if self._count(player, "match", "demolition_damage", count) >= DEMOLITION_ROUND_DAMAGE:
                self.unlock(player, "demolition_damage_one_round")
            if cause == DRILL_KILL:
                self.add(player, "drillgun_demolition_count", count)

    # -- pickups -----------------------------------------------------------

    @staticmethod
    def crate_baseline(player) -> tuple:
        return (
            getattr(player, "health", 0),
            getattr(player, "weapon", None),
            getattr(player, "ammo_reserve", 0),
        )

    def crate_collected(self, player, award: int, baseline) -> None:
        if _is_bot(player) or not self._live():
            return
        health, weapon, reserve = baseline
        if int(award) == int(C.MOST_HEALTH_CRATES_COLLECTED):
            healed = int(getattr(player, "health", 0)) - int(health)
            if healed > 0 and self._count(player, "match", "health_from_drops", healed) >= HEALTH_FROM_DROPS:
                self.unlock(player, "health_drop_greedy")
        elif int(award) == int(C.MOST_AMMO_CRATES_COLLECTED):
            from server.game_constants import WEAPON_PROFILES

            profile = WEAPON_PROFILES.get(weapon)
            if profile is None or getattr(player, "weapon", None) != weapon:
                return
            gained = int(getattr(player, "ammo_reserve", 0)) - int(reserve)
            capacity = int(profile.reserve_ammo)
            if gained > 0 and capacity > 0 and self._count(
                    player, "match", f"ammo_from_drops:{int(weapon)}", gained) >= capacity:
                self.unlock(player, "ammo_drop_greedy")

    # -- Zombie ------------------------------------------------------------

    def zombie_round_started(self, initial_zombies, zombie_team: int, survivor_team: int) -> None:
        self._zombie_teams = (int(zombie_team), int(survivor_team))
        self._initial_zombies = list(initial_zombies)
        self.round_started()

    def zombie_round_finished(self, survivors) -> None:
        if self._live():
            for player in survivors:
                # Killed in the round's last instant: its infection is only
                # still queued.
                if bool(getattr(player, "alive", False)):
                    self.unlock(player, "zombie_survivor")
            self._award_most_kills(self._initial_zombies, "zombie_mvp")
        self._initial_zombies = []
        self._round_open = False

    def _award_most_kills(self, candidates, api_name: str) -> None:
        players = tuple(getattr(self.server, "players", {}).values())
        for candidate in candidates:
            if not self._connected(candidate):
                continue
            own = self.tracker(candidate).round.get("counted_kills", 0)
            if own >= 1 and all(
                self.tracker(other).round.get("kills", 0) <= own
                for other in players if other is not candidate
            ):
                self.unlock(candidate, api_name)

    # -- VIP ---------------------------------------------------------------

    def vip_round_started(self, vips) -> None:
        self._vips = dict(vips)
        self.round_started()

    def vip_round_finished(self, mode) -> None:
        vips, self._vips = self._vips, None
        if not vips or not self._round_counts():
            self._round_open = False
            return
        self._award_most_kills(vips.values(), "vip_mvp")
        current = getattr(mode, "vips", {}) or {}
        alive = getattr(mode, "vip_alive", {}) or {}
        for team, vip in vips.items():
            if (current.get(team) is vip and alive.get(team)
                    and bool(getattr(vip, "alive", False))
                    and not self.tracker(vip).round.get("kills", 0)):
                self.unlock(vip, "vip_pacifist")
        self._round_open = False

    # -- Diamond Mine ------------------------------------------------------

    def diamond_picked_up(self, player, serial: int, *, fresh_seconds, found_by, last_team) -> None:
        """``fresh_seconds`` is the age of a diamond nobody carried yet,
        ``None`` once it has been carried and dropped."""
        track = self._diamonds.setdefault(int(serial), _ObjectiveTrack())
        track.hotfoot = track.interceptor = None
        if fresh_seconds is not None:
            if float(fresh_seconds) <= DIAMOND_HOTFOOT_SECONDS:
                track.hotfoot = player
            if (found_by is not None and found_by is not player
                    and _team(found_by) in _PLAYABLE and _team(found_by) != _team(player)):
                track.thief = player
        elif last_team is not None and int(last_team) in _PLAYABLE and int(last_team) != _team(player):
            track.interceptor = player

    def diamond_cashed_in(self, player, serial, found_by_player: bool) -> None:
        track = self._diamonds.pop(int(serial), None) if serial is not None else None
        if not self._live():
            return
        if track is not None:
            if track.hotfoot is player:
                self.unlock(player, "diamond_hotfoot")
            if track.thief is player:
                self.add(player, "diamond_thief_count")
            if track.interceptor is player:
                self.add(player, "diamond_interceptor_count")
        if found_by_player and self._count(player, "match", "diamonds_found") == DIAMONDS_FOUND_PER_MATCH:
            self.unlock(player, "diamond_collector")

    # -- Multi-Hill --------------------------------------------------------

    def hill_scored(self, zone_index: int, player) -> None:
        self._hill_scorers.setdefault(int(zone_index), {})[id(player)] = player

    def hill_contest_won(self, players, retained: bool) -> None:
        if not self._live():
            return
        stat = "hill_defender_count" if retained else "hill_interceptor_count"
        for player in players:
            self.add(player, stat)

    def hill_kill(self, killer, victim, *, killer_in_hill: bool, victim_in_hill: bool) -> None:
        if not self._live() or (_is_bot(victim) and not self.count_bot_kills):
            return
        if killer_in_hill:
            self.add(killer, "hill_kill_shooting_out_count")
        if victim_in_hill:
            self.add(killer, "hill_kill_shooting_in_count")

    def hill_expired(self, zone, occupant_ids) -> None:
        """The hill is depleted and the airstrike is on its way: its
        occupants are the players who brought the strike down."""
        scorers = self._hill_scorers.pop(int(zone.index), {})
        if not self._live():
            return
        if len(scorers) == 1:
            only = next(iter(scorers.values()))
            if self._connected(only):
                self.unlock(only, "hill_greedy")
        players = getattr(self.server, "players", {})
        inside = {}
        for team, ids in (occupant_ids or {}).items():
            for player_id in ids:
                player = players.get(player_id)
                if (player is not None and not _is_bot(player) and _team(player) == int(team)
                        and bool(getattr(player, "alive", False))):
                    inside[id(player)] = player
        if inside:
            self._strikes.append(_Strike(zone, dict(inside), dict(inside)))

    def _strike_in_flight(self) -> bool:
        engine = getattr(self.server, "projectile_engine", None)
        try:
            from modes.airstrike import AIRSTRIKE_SPEC
        except Exception:  # noqa: BLE001 - no airstrike module, no shells
            return False
        return any(
            getattr(projectile, "spec", None) is AIRSTRIKE_SPEC
            for projectile in getattr(engine, "projectiles", ()) or ()
        )

    def _tick_strikes(self, dt: float) -> None:
        in_flight = self._strike_in_flight()
        for strike in tuple(self._strikes):
            strike.elapsed += dt
            for key, player in tuple(strike.survivors.items()):
                position = _position(player)
                if not self._connected(player) or not bool(getattr(player, "alive", False)):
                    strike.survivors.pop(key, None)
                    strike.stayed.pop(key, None)
                elif position is None or not strike.zone.contains(position):
                    strike.stayed.pop(key, None)
            if strike.elapsed < STRIKE_MIN_SECONDS or (
                    in_flight and strike.elapsed < STRIKE_MAX_SECONDS):
                continue
            self._strikes.remove(strike)
            for key, player in strike.survivors.items():
                self.add(player, "hill_strike_survivor_count")
                if key in strike.stayed:
                    self.unlock(player, "hill_strike_stay_put")

    # -- Occupation --------------------------------------------------------

    def bomb_picked_up(self, player, serial: int, *, from_spawn: bool, attacker: bool) -> None:
        track = self._bombs.setdefault(int(serial), _ObjectiveTrack())
        track.hotfoot = player if from_spawn and attacker else None
        if attacker:
            # The attackers have their bomb back: the interception failed.
            track.interceptor = None

    def bomb_dropped(self, player, serial: int, *, in_base: bool) -> None:
        track = self._bombs.get(int(serial))
        if track is None:
            return
        # Only the carry that began at the spawn point is still marked.
        carried_in = track.hotfoot is player and in_base
        track.hotfoot = None
        if carried_in and self._live():
            self.unlock(player, "bomb_hotfoot")

    def bomb_carrier_killed(self, killer, victim, serial: int) -> None:
        if not self._live() or (_is_bot(victim) and not self.count_bot_kills):
            return
        self._bombs.setdefault(int(serial), _ObjectiveTrack()).interceptor = killer

    def bomb_detonated(self, serial: int, *, inside: bool, planter) -> None:
        track = self._bombs.pop(int(serial), None)
        if not self._live():
            return
        if inside:
            if planter is not None:
                self._last_bomb_planter = planter
                if bool(getattr(planter, "alive", False)):
                    self.unlock(planter, "bomb_survivor")
        elif track is not None and self._connected(track.interceptor):
            self.add(track.interceptor, "bomb_interceptor_count")

    # -- CTF ---------------------------------------------------------------

    def intel_carrier_killed(self, killer, victim, intel_team: int, kill_type: int) -> None:
        self._intel_interceptors.pop(int(intel_team), None)
        if (not self._live() or killer is None or killer is victim
                or int(kill_type) in _TRANSITION_KILLS
                or _team(killer) != int(intel_team) or _team(victim) == int(intel_team)
                or (_is_bot(victim) and not self.count_bot_kills)):
            return
        self._intel_interceptors[int(intel_team)] = killer

    def intel_picked_up(self, intel_team: int) -> None:
        self._intel_interceptors.pop(int(intel_team), None)

    def intel_returned(self, intel_team: int) -> None:
        interceptor = self._intel_interceptors.pop(int(intel_team), None)
        if (self._live() and self._connected(interceptor)
                and _team(interceptor) == int(intel_team)):
            self.add(interceptor, "intel_defence_count")

    # -- tick --------------------------------------------------------------

    def tick(self, dt: float) -> None:
        if not 0.0 < dt <= 1.0:
            return
        if self._live():
            mode = getattr(self.server, "mode", None)
            last_man = getattr(mode, "last_survivor_id", None) if self._zombie_teams else None
            for player in tuple(getattr(self.server, "players", {}).values()):
                if _is_bot(player):
                    continue
                state = self.tracker(player)
                position = _position(player)
                last, state.last_position = state.last_position, position
                if not bool(getattr(player, "alive", False)) or _team(player) not in _PLAYABLE:
                    continue
                if bool(getattr(player, "airborne", False)):
                    self.add_fraction(player, "airborne_seconds_count", dt)
                elif position is not None and last is not None:
                    step = sum((a - b) ** 2 for a, b in zip(position, last)) ** 0.5
                    if 0.0 < step <= 2.0:  # teleports/respawns are not running
                        self.add_fraction(player, "distance_run", step)
                if last_man is not None and last_man == getattr(player, "id", -1):
                    self.add_fraction(player, "zombie_seconds_as_lms_count", dt)
                    if self._count(player, "round", "last_man_seconds", dt) >= LAST_MAN_SECONDS:
                        self.unlock(player, "zombie_lms_one_round")
            if self._strikes:
                self._tick_strikes(dt)
        self._region_resync_in -= dt
        if self._region_resync_in <= 0.0:
            self._region_resync_in = REGION_RESYNC_SECONDS
            if self._regions:
                self._resync_regions()
        self._flush_in -= dt
        if self._flush_in <= 0.0:
            self._flush_in = FLUSH_INTERVAL_SECONDS
            self.flush()


# ---------------------------------------------------------------------------
# Gameplay hooks
# ---------------------------------------------------------------------------

def engine_of(server) -> AchievementEngine | None:
    """The started engine of ``server``, or None."""
    engine = getattr(server, "achievements", None)
    return engine if engine is not None and getattr(engine, "active", False) else None


def _hook(method: str, default=None):
    """``hook(server, ...)`` -> ``engine.method(...)``, guarded.

    A fault is counted and logged once; it never reaches the caller, so an
    achievement bug cannot break a kill, a blast or a round.
    """

    def call(server, *args, **kwargs):
        engine = engine_of(server)
        if engine is None:
            return default
        try:
            return getattr(engine, method)(*args, **kwargs)
        except Exception:  # noqa: BLE001 - see docstring
            engine.fault(method)
            return default

    call.__name__ = call.__qualname__ = method
    return call


add = _hook("add", default=())
unlock = _hook("unlock", default=False)
match_started = _hook("match_started")
match_ended = _hook("match_ended")
player_left = _hook("player_left")
tick = _hook("tick")
damaged = _hook("damaged")
died = _hook("died")
shot_resolved = _hook("shot_resolved")
blocks_destroyed = _hook("blocks_destroyed")
projectile_exploding = _hook("projectile_exploding")
crate_collected = _hook("crate_collected")
take_unreported = _hook("take_unreported", default=())
zombie_round_started = _hook("zombie_round_started")
zombie_round_finished = _hook("zombie_round_finished")
vip_round_started = _hook("vip_round_started")
vip_round_finished = _hook("vip_round_finished")
diamond_picked_up = _hook("diamond_picked_up")
diamond_cashed_in = _hook("diamond_cashed_in")
hill_scored = _hook("hill_scored")
hill_contest_won = _hook("hill_contest_won")
hill_kill = _hook("hill_kill")
hill_expired = _hook("hill_expired")
bomb_picked_up = _hook("bomb_picked_up")
bomb_dropped = _hook("bomb_dropped")
bomb_carrier_killed = _hook("bomb_carrier_killed")
bomb_detonated = _hook("bomb_detonated")
intel_carrier_killed = _hook("intel_carrier_killed")
intel_picked_up = _hook("intel_picked_up")
intel_returned = _hook("intel_returned")


def crate_baseline(player) -> tuple:
    """What a crate is about to change, read before its refill runs."""
    return AchievementEngine.crate_baseline(player)


def _begin_scope(server, thrower, kill_type, origin):
    engine = engine_of(server)
    if engine is None:
        return None, None
    try:
        return engine, engine.blast_begin(thrower, kill_type, origin)
    except Exception:  # noqa: BLE001 - never break the explosion
        engine.fault("blast_begin")
        return None, None


def _end_scope(engine, scope) -> None:
    if engine is None or scope is None:
        return
    try:
        engine.blast_end(scope)
    except Exception:  # noqa: BLE001 - never break the explosion
        engine.fault("blast_end")


def blast_scope(function):
    """Decorate ``BattleSpadesServer._apply_blast``: the kills and block
    removals inside one call are one explosion's."""

    names = ("gx", "gy", "gz", "damage", "block_damage", "kill_type", "thrower")

    @functools.wraps(function)
    def wrapper(self, *args, **kwargs):
        engine = scope = None
        if engine_of(self) is not None:
            values = dict(zip(names, args))
            values.update(kwargs)
            try:
                origin = (float(values["gx"]), float(values["gy"]), float(values["gz"]))
                kill_type = int(values["kill_type"])
            except (KeyError, TypeError, ValueError):
                origin = kill_type = None
            if kill_type is not None:
                engine, scope = _begin_scope(self, values.get("thrower"), kill_type, origin)
        try:
            return function(self, *args, **kwargs)
        finally:
            _end_scope(engine, scope)

    return wrapper


@contextmanager
def block_cause(server, player, kill_type: int):
    """Attribute block removals inside the ``with`` body to one weapon (the
    drill's bore, which is not an explosion)."""
    engine, scope = _begin_scope(server, player, kill_type, None)
    try:
        yield
    finally:
        _end_scope(engine, scope)
