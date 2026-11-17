"""Chat-issued orders for server-owned bots (/follow /hold /attack ...).

A human on a team gives an order; the director publishes it to every bot's
perception frame as a ``player_order`` objective (the same channel the zombie
horde coordinator uses), and ``ModePolicyMemory.decide`` consults it before
the mode's own policy. A bot with no order behaves exactly as before.

Everything here is bounded and fail-safe: a malformed order yields ``None``
(the bot falls back to its normal mode behaviour), never an exception.

Orders and their meaning (CTF vocabulary; other modes fall back to the team
anchor or the spot where the bot was ordered):

``follow``   escort the issuer; when the issuer dies or changes team the order
             becomes ``hold`` (and ends if the issuer disconnects)
``hold``     stay where ordered; strafe within a leash while fighting
``attack``   push enemy intel, else enemy base, else hunt the nearest enemy;
             while a teammate carries the enemy intel, a share of the attackers
             (``order_attack_escort_ratio``) escorts him and the rest push the
             enemy base
``defend``   hold our base; chase a thief only while he is near the base
``protect``  intel-centric: escort a friendly carrier and/or hunt the thief of
             our intel at any distance (split by bot id when both), else guard
             our intel
``escort``   escort the friendly carrier (else the issuer); never splits

A bot carrying intel always takes it home first, whatever it was ordered.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

from .messages import ObjectiveSnapshot, PerceptionFrame, PlayerSnapshot, Vector3

KIND = "player_order"

# ``player_order.state`` codes.
FOLLOW, HOLD, ATTACK, DEFEND, PROTECT, ESCORT = 1, 2, 3, 4, 5, 6
CODES = {
    "follow": FOLLOW, "hold": HOLD, "attack": ATTACK,
    "defend": DEFEND, "protect": PROTECT, "escort": ESCORT,
}
NAMES = {value: key for key, value in CODES.items()}

# Operator tunables (``[bots] order_*``). The director stamps the first two on
# every published order as ``ObjectiveSnapshot.params`` and the bot's job as the
# third, so the worker needs no config of its own; an empty ``params`` means
# "use these defaults, no job".
DEFAULT_HOLD_LEASH = 6.0            # blocks a holding bot may stray in a fight
DEFAULT_DEFEND_CHASE_RADIUS = 40.0  # blocks from our base a thief is chased to
DEFAULT_PROTECT_HUNT_RATIO = 0.5    # share of /protect bots that hunt the thief
DEFAULT_ATTACK_ESCORT_RATIO = 0.5   # share of /attack bots that escort a carrier
P_HOLD_LEASH, P_DEFEND_CHASE, P_JOB = 0, 1, 2

# A bot's job while a friendly carrier exists. ``JOB_NONE`` means the plain
# version of its order: /attack pushes the objective, /protect escorts.
JOB_NONE, JOB_ESCORT, JOB_PUSH = 0, 1, 2

# A bot already this far from our base counts as "out chasing": it keeps the
# chase until the thief passes 25 % beyond the radius, so a thief walking the
# edge of the radius does not flip the whole team between hunting and guarding.
_CHASE_HYSTERESIS = 1.25
_AT_BASE_RADIUS = 12.0


def clamp_params(hold_leash: float, defend_chase: float) -> tuple[float, float]:
    """Return the worker tunables in published order, each bounded to a sane range."""

    return (
        min(40.0, max(0.0, float(hold_leash))),
        min(400.0, max(0.0, float(defend_chase))),
    )


def clamp_ratio(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def attack_escort_count(members: int, ratio: float) -> int:
    """How many of ``members`` /attack bots escort a friendly carrier.

    ``ratio`` of them, rounded down, but always leaving one attacker: a single
    attacking bot attacks.
    """

    return min(max(0, members - 1), int(members * clamp_ratio(ratio)))


def protect_escort_count(members: int, hunt_ratio: float) -> int:
    """How many of ``members`` /protect bots escort when our intel is also stolen.

    ``hunt_ratio`` of them, rounded down, hunt the thief; the rest escort, and
    at least one always does: a single bot escorts.
    """

    return max(1, members - int(members * clamp_ratio(hunt_ratio)))


@dataclass(frozen=True, slots=True)
class PlayerOrder:
    code: int
    team: int
    issuer_id: int
    anchor: Vector3  # where the bot stood when ordered (hold point / fallback)


class PlayerOrderBook:
    """Orders keyed by bot player id.

    An order belongs to the bot's player id, not to a life: it survives the
    bot's own death and respawn, and ends only on ``/free`` (by any teammate),
    a map change or round restart, the bot leaving, the bot changing team, or
    the player who issued it disconnecting. Every order records its issuer; an
    order replaced by someone else's belongs to the new issuer.
    """

    __slots__ = ("_orders", "_jobs")

    def __init__(self) -> None:
        self._orders: dict[int, PlayerOrder] = {}
        # (team, order code) -> {bot id: job}, fixed while a friendly carrier
        # exists so a bot keeps its job through death and respawn.
        self._jobs: dict[tuple[int, int], dict[int, int]] = {}

    def issue(self, bot_id: int, order: PlayerOrder) -> None:
        self._orders[int(bot_id)] = order

    def clear(self, bot_id: int) -> bool:
        return self._orders.pop(int(bot_id), None) is not None

    def clear_all(self) -> None:
        self._orders.clear()
        self._jobs.clear()

    def get(self, bot_id: int) -> PlayerOrder | None:
        return self._orders.get(int(bot_id))

    def __len__(self) -> int:
        return len(self._orders)

    def issuers(self) -> set[int]:
        """Player ids that issued at least one live order."""

        return {order.issuer_id for order in self._orders.values()}

    def clear_issued_by(self, issuer_id: int) -> list[int]:
        """Free every order ``issuer_id`` issued, as ``/free`` would. Returns the bot ids."""

        freed = [bot_id for bot_id, order in self._orders.items() if order.issuer_id == int(issuer_id)]
        for bot_id in freed:
            del self._orders[bot_id]
        return freed

    def drop_team_changes(self, bot_teams: dict[int, int]) -> int:
        """Forget orders whose bot now plays for another team. Returns the count."""

        stale = [
            bot_id for bot_id, order in self._orders.items()
            if bot_id in bot_teams and int(bot_teams[bot_id]) != order.team
        ]
        for bot_id in stale:
            del self._orders[bot_id]
        return len(stale)

    def release_orphaned_follows(self, issuer_alive, positions: dict[int, Vector3]) -> list[int]:
        """Turn ``follow`` orders whose issuer is gone into ``hold`` where the bot stands.

        ``issuer_alive(issuer_id, team)`` says whether that player is still alive
        on the order's team. A bot never chases an issuer's respawn: it keeps
        guarding the place it was at when the issuer fell. Returns the bot ids
        converted. A bot with no known position keeps its order unchanged.
        """

        converted: list[int] = []
        for bot_id, order in list(self._orders.items()):
            if order.code != FOLLOW or bot_id not in positions:
                continue
            if issuer_alive(order.issuer_id, order.team):
                continue
            self._orders[bot_id] = PlayerOrder(
                HOLD, order.team, order.issuer_id,
                tuple(float(value) for value in positions[bot_id]),
            )
            converted.append(bot_id)
        return converted

    def sync_jobs(self, team: int, code: int, active: bool, escort_count, exclude=()) -> None:
        """Keep the escort/push job table of one team's ``code`` orders current.

        While ``active`` (a friendly carrier exists) every ordered bot gets one
        job and keeps it, whatever happens to the others: a bot that dies is
        still in the book, so it returns to the same job after respawn, and
        bots freed or newly ordered mid-carry never reshuffle the rest. The
        escorts are the lowest bot ids. ``escort_count(members)`` says how many
        escort; bots in ``exclude`` (the carrier itself) take no job. When no
        longer ``active`` the table is dropped and every bot runs the plain
        version of its order.
        """

        key = (int(team), int(code))
        if not active:
            self._jobs.pop(key, None)
            return
        skip = {int(item) for item in exclude}
        members = sorted(
            bot_id for bot_id, order in self._orders.items()
            if order.team == team and order.code == code and bot_id not in skip
        )
        table = self._jobs.setdefault(key, {})
        for gone in [bot_id for bot_id in table if bot_id not in members]:
            del table[gone]
        wanted = int(escort_count(len(members)))
        escorts = sum(1 for job in table.values() if job == JOB_ESCORT)
        for bot_id in members:
            if bot_id in table:
                continue
            if escorts < wanted:
                table[bot_id] = JOB_ESCORT
                escorts += 1
            else:
                table[bot_id] = JOB_PUSH

    def job_of(self, bot_id: int) -> int:
        order = self._orders.get(int(bot_id))
        if order is None:
            return JOB_NONE
        return self._jobs.get((order.team, order.code), {}).get(int(bot_id), JOB_NONE)

    def objectives(self, live_bot_ids, params: tuple[float, ...] = ()) -> list[ObjectiveSnapshot]:
        live = {int(item) for item in live_bot_ids}
        for stale in [bot_id for bot_id in self._orders if bot_id not in live]:
            del self._orders[stale]
        return [
            ObjectiveSnapshot(
                KIND,
                order.team,
                order.anchor,
                carrier_id=bot_id,
                state=order.code,
                attacker=order.issuer_id,
                params=(*params, float(self.job_of(bot_id))),
            )
            for bot_id, order in self._orders.items()
        ]


def _alive(player: PlayerSnapshot) -> bool:
    return bool(player.alive and player.spawned)


def _player(frame: PerceptionFrame, player_id: int) -> PlayerSnapshot | None:
    return next(
        (p for p in frame.players if p.player_id == int(player_id) and _alive(p)),
        None,
    )


def _objective(frame: PerceptionFrame, kind: str, own_team: int, *, enemy: bool):
    return next(
        (
            item for item in frame.objectives
            if item.kind == kind and ((item.team != own_team) if enemy else (item.team == own_team))
        ),
        None,
    )


def _param(order: ObjectiveSnapshot, index: int, default: float) -> float:
    params = order.params
    if index < len(params):
        try:
            value = float(params[index])
        except (TypeError, ValueError):
            return default
        if math.isfinite(value):
            return value
    return default


def _flat(a: Vector3, b: Vector3) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def describe_role(role: str) -> str:
    """Plain-language reading of a worker intent role, for ``/status``."""

    if not role:
        return "idle"
    base, _, suffix = role.partition(":")
    if base == "combat_objective":
        return "fighting, " + describe_role(suffix)
    if base.startswith("combat"):
        return "fighting"
    text = _ROLE_TEXT.get(base, base)
    if suffix == "arrived":
        text += " (in position)"
    return text


_ROLE_TEXT = {
    "order_follow_escort": "following you",
    "order_hold": "holding position",
    "order_attack": "pushing the enemy objective",
    "order_attack_hunt": "hunting the nearest enemy",
    "order_attack_escort": "escorting the carrier home",
    "order_defend": "guarding the base",
    "order_defend_hunt": "chasing the thief near our base",
    "order_protect_guard": "guarding our intel",
    "order_protect_escort": "escorting the carrier",
    "order_protect_hunt": "hunting the thief",
    "order_escort_carrier": "escorting the carrier",
    "order_escort_issuer": "escorting you",
    "order_escort_hold": "nobody to escort, holding",
    "order_capture": "bringing the intel home",
}


def player_order_decision(frame: PerceptionFrame, observer: PlayerSnapshot):
    """Return the ``ModeBotDecision`` for this bot's chat order, or ``None``."""

    from .policies import ModeBotDecision, ModeBotPosture

    order = next(
        (
            item for item in frame.objectives
            if item.kind == KIND and item.carrier_id == observer.player_id
        ),
        None,
    )
    if order is None:
        return None
    code = int(order.state)
    anchor = tuple(order.position)
    team = observer.team
    hold_leash = _param(order, P_HOLD_LEASH, DEFAULT_HOLD_LEASH)
    chase_radius = _param(order, P_DEFEND_CHASE, DEFAULT_DEFEND_CHASE_RADIUS)
    job = int(_param(order, P_JOB, JOB_NONE))

    own_base = _objective(frame, "ctf_base", team, enemy=False)
    # A carried flag always goes home first: holding it helps nobody.
    if observer.carried_entity_id >= 0 and own_base is not None:
        return ModeBotDecision(
            own_base.position, "order_capture", sprint=True, arrival_radius=4.0,
            posture=ModeBotPosture.EVASIVE, objective_priority=0.98, engagement_radius=8.0,
        )

    def escort(position: Vector3, role: str):
        far = math.dist(observer.position, position) > 12.0
        return ModeBotDecision(
            tuple(position), role, sprint=far, arrival_radius=4.5,
            posture=ModeBotPosture.ESCORT, objective_priority=0.95,
            engagement_radius=40.0, watch_position=None,
        )

    def guard(point: Vector3, role: str, radius: float = 6.0, leash: float = 0.0):
        return ModeBotDecision(
            tuple(point), role, sprint=False, arrival_radius=radius,
            posture=ModeBotPosture.DEFEND, objective_priority=0.95,
            engagement_radius=32.0, leash_radius=leash,
        )

    def hunt(position: Vector3, role: str, engagement: float = 160.0):
        return ModeBotDecision(
            tuple(position), role, sprint=True, arrival_radius=3.0,
            posture=ModeBotPosture.ASSAULT, objective_priority=0.95,
            engagement_radius=engagement,
        )

    def hold_here(role: str = "order_hold"):
        return guard(anchor, role, 2.0, hold_leash)

    # Who carries what. Intel positions follow their holder, so a carrier is
    # locatable at any distance without being in the perception cohort.
    enemy_intel = _objective(frame, "ctf_intel", team, enemy=True)
    own_intel = _objective(frame, "ctf_intel", team, enemy=False)

    def friendly_carrier():
        """Position of a teammate (not this bot) carrying the enemy intel."""
        if enemy_intel is None or enemy_intel.carrier_id < 0:
            return None
        if enemy_intel.carrier_id == observer.player_id:
            return None
        live = _player(frame, enemy_intel.carrier_id)
        if live is not None:
            return tuple(live.position)
        return tuple(enemy_intel.position)

    def thief_position():
        """Position of the enemy carrying OUR intel, if anyone is."""
        if own_intel is None or own_intel.carrier_id < 0:
            return None
        live = _player(frame, own_intel.carrier_id)
        return tuple(live.position) if live is not None else tuple(own_intel.position)

    def base_point() -> Vector3:
        return (
            own_base.position if own_base is not None else
            (_objective(frame, "team_anchor", team, enemy=False) or order).position
        )

    if code == FOLLOW:
        issuer = _player(frame, order.attacker)
        if issuer is not None and issuer.team == team:
            return escort(issuer.position, "order_follow_escort")
        return hold_here()

    if code == HOLD:
        return hold_here()

    if code == ESCORT:
        carrier = friendly_carrier()
        if carrier is not None:
            return escort(carrier, "order_escort_carrier")
        issuer = _player(frame, order.attacker)
        if issuer is not None and issuer.team == team and issuer.player_id != observer.player_id:
            return escort(issuer.position, "order_escort_issuer")
        return hold_here("order_escort_hold")

    if code == DEFEND:
        base = base_point()
        thief = thief_position()
        if thief is not None and chase_radius > 0.0:
            out = _flat(observer.position, base) > _AT_BASE_RADIUS
            limit = chase_radius * (_CHASE_HYSTERESIS if out else 1.0)
            if _flat(thief, base) <= limit:
                return hunt(thief, "order_defend_hunt", engagement=60.0)
        return guard(base, "order_defend")

    if code == PROTECT:
        carrier = friendly_carrier()
        thief = thief_position()
        if carrier is not None and thief is not None:
            # Both jobs at once. The director split the team's protect-ordered
            # bots by id and fixed the jobs for as long as the carrier lives
            # (``order_protect_hunt_ratio``): the hunters chase the thief, and
            # a bot with no job (a plain order inside a /split) escorts.
            if job == JOB_PUSH:
                return hunt(thief, "order_protect_hunt")
            return escort(carrier, "order_protect_escort")
        if carrier is not None:
            return escort(carrier, "order_protect_escort")
        if thief is not None:
            return hunt(thief, "order_protect_hunt")
        point = (
            own_intel.position if own_intel is not None else
            own_base.position if own_base is not None else anchor
        )
        return guard(point, "order_protect_guard")

    if code == ATTACK:
        # While a teammate carries the enemy intel, the director has split the
        # team's attack-ordered bots by id (``order_attack_escort_ratio``) and
        # fixed the jobs until the carrier drops or dies: escorts follow him,
        # the rest keep attacking. The intel moves with its carrier, so the
        # attackers aim at the enemy base instead of converging on him. When
        # the intel resets there is no carrier and every bot attacks it again.
        carrier = friendly_carrier()
        if carrier is not None and job == JOB_ESCORT:
            return escort(carrier, "order_attack_escort")
        target = (
            (None if carrier is not None else _objective(frame, "ctf_intel", team, enemy=True))
            or _objective(frame, "ctf_base", team, enemy=True)
            or _objective(frame, "team_anchor", team, enemy=True)
        )
        if target is not None:
            return ModeBotDecision(
                tuple(target.position), "order_attack", sprint=True, arrival_radius=3.0,
                posture=ModeBotPosture.ASSAULT, objective_priority=0.95,
                engagement_radius=160.0,
            )
        enemies = [p for p in frame.players if _alive(p) and p.team != team]
        if enemies:
            nearest = min(enemies, key=lambda p: math.dist(observer.position, p.position))
            return hunt(nearest.position, "order_attack_hunt")
        return None

    return None
