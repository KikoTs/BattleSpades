"""Retail HUD / rule-mutation packets the server previously never sent.

One module owns the wire side of:

* MinimapBillboard(41) / MinimapBillboardClear(42) -- minimap icons keyed by
  an id the server chooses (``hud.minimap.add_billboard`` /
  ``remove_billboard`` in the stock client).
* POIFocus(18) -- presentation-only camera pan to a world point.
* ProgressBar(65) -- **disabled**: drawing it crashes the stock client.
* TeamLockScore(81) / TeamInfiniteBlocks(82) -- runtime team rule flags.

Contract used by other modules (all safe no-ops on bad input or send errors,
all deliver only to in-game connections)::

    add_billboard(server, entity_id, key, icon_name, position, *,
                  tracking=False, color=None, recipients=None)
    remove_billboard(server, entity_id, *, recipients=None)
    reveal_billboards(server, connection)
    set_progress(server, recipients, progress, rate, *, color=None)
    clear_progress(server, recipients)
    poi_focus(server, position, *, recipients=None)
    set_team_lock_score(server, team, locked)
    set_team_infinite_blocks(server, team, enabled)

``recipients`` may be ``None`` (every in-game connection), one connection or
player, or an iterable of connections / players.

Stock-client behaviour (live probes 2026-09-26 on the tracer dev client, whose
``hud.pyd`` / ``gameScene.pyd`` / ``draw.pyd`` / ``packet.pyd`` are
byte-identical to the Steam install; details in docs/PROTOCOL.md rows 18, 41,
42, 65, 81, 82):

* 41 reads only ``entity_id, color, x, y, z, icon_name, tracking`` -- the
  ``key`` byte is never read. It calls
  ``minimap.add_billboard(id, r/255, g/255, b/255, x, y, z, icon_name,
  tracking)``; re-sending an id updates that billboard in place.
  ``icon_name`` is loaded as ``png/ui/<icon_name>.png``; an unknown name
  raises IOError inside the packet handler, hence :data:`BILLBOARD_ICONS`.
  With ``tracking`` set, ``entity_id`` must be a live CreateEntity id in
  ``scene.entities``: the billboard follows that entity, otherwise the client
  logs "invalid billboard tracking target id (KeyError)" and drops it.
* 42 removes by id; an unknown id is a silent no-op.
* 18 activates the GameScene ``LookAtController`` on the local player,
  aimed at the target. It has no timeout: it holds until the local player
  dies (ChaseController) or is (re)spawned (controller cleared) -- verified
  for both a death and a server ``respawn_player`` of a living player.
* 65: ``ProgressBar.draw`` (hud.pyd, progressBar.py:21) calls
  ``aoslib.draw.draw_progress_bar`` with 7 arguments; draw.pyd accepts 5.
  The first frame after any visible bar raises TypeError out of
  ``GameManager.draw`` and the pyglet reactor exits (client crash). No wire
  value hides the bar either (fixed16 cannot carry the NaN the handler
  tests). The packet is therefore never sent.
* 81 sets ``teams[team_id].locked_score``; while set the client ignores
  team SetScore(85) rows for that team (player rows still apply).
* 82 sets ``teams[team_id].infinite_blocks``; the client block, prefab,
  flare and snow tools then skip their block-count checks.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "BILLBOARD_ICONS",
    "BILLBOARD_ID_BASE",
    "PROGRESS_BAR_SUPPORTED",
    "add_billboard",
    "clear_all_billboards",
    "clear_progress",
    "poi_focus",
    "remove_billboard",
    "reset_state",
    "reveal_billboards",
    "reveal_hud_state",
    "reveal_team_rules",
    "set_progress",
    "set_team_infinite_blocks",
    "set_team_lock_score",
    "team_infinite_blocks",
    "team_score_locked",
]

# Wire limits (fixed16 = sign-magnitude, 1/64 units, 15-bit magnitude).
_FIXED_MAX = 0x7FFF / 64.0
_SHORT_MIN, _SHORT_MAX = -0x8000, 0x7FFF
_ICON_NAME_MAX = 64

# Non-tracking billboards share the client's billboard list with tracking
# ones, and a tracking billboard's id IS its entity id. Callers minting ids
# for free-standing markers should start here so they never collide with a
# CreateEntity id (the registry allocates small ids).
BILLBOARD_ID_BASE = 0x7000

# ``png/ui/<name>.png`` files present in the stock Steam install that are
# minimap / marker art. Anything else is refused: an unknown name raises
# IOError inside the client's packet handler.
BILLBOARD_ICONS: frozenset[str] = frozenset(
    {
        "Minimap_Zombie",
        "MultiHill",
        "OccupationTarget",
        "base_icon",
        "bomb_icon_256x256",
        "ctf_capture_point",
        "ctf_secure_base",
        "diamond_dropoff",
        "diamond_icon_256x256",
        "grenade_icon",
        "health_icon",
        "heart_icon_256x256",
        "intel_icon_256x256",
        "map_base_16",
        "map_bomb_16",
        "map_diamond_16",
        "map_grave_16",
        "map_intel_16",
        "map_pickup_16",
        "map_player_16",
        "map_vip_player_16",
        "marker_attack_16",
        "marker_bg_16",
        "marker_build_16",
        "marker_c4_16",
        "marker_defend_16",
        "marker_dynamite_16",
        "marker_land_mine_16",
        "marker_machinegun_16",
        "marker_medpack_16",
        "marker_radar_station_16",
        "marker_rally_point_16",
        "marker_satchel_charge_16",
        "marker_tunnel_entrance_16",
        "marker_turret_16",
        "minimap_ammocrate",
        "minimap_base",
        "minimap_blockcrate",
        "minimap_bomb",
        "minimap_ctf_capture_point",
        "minimap_ctf_secure_base",
        "minimap_diamond",
        "minimap_diamond_dropoff",
        "minimap_healthcrate",
        "minimap_intel",
        "minimap_multihill",
        "minimap_occupation_target",
        "minimap_spawn",
        "pointer_icon",
        "spawn_icon",
        "tc_base_icon",
        "tc_billboard_a",
        "tc_billboard_b",
        "tc_billboard_c",
        "tc_billboard_d",
        "tc_billboard_e",
        "tc_billboard_f",
        "tc_billboard_g",
        "tc_minimap_a",
        "tc_minimap_b",
        "tc_minimap_base",
        "tc_minimap_c",
        "tc_minimap_d",
        "tc_minimap_e",
        "tc_minimap_f",
        "tc_minimap_g",
        "vip_icon",
        "vip_icon_256x256",
    }
)

# ProgressBar(65) crashes the stock client on its first draw (see module
# docstring). Kept as a named switch so tests and callers can see why the
# progress API is inert.
PROGRESS_BAR_SUPPORTED = False


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Billboard:
    entity_id: int
    key: int
    icon_name: str
    position: tuple[float, float, float]
    tracking: bool
    color: tuple[int, int, int]
    # None = shown to every in-game connection (and replayed to joiners);
    # otherwise the set of player ids it was addressed to.
    player_ids: Optional[frozenset[int]]


@dataclass(slots=True)
class _HudState:
    billboards: dict[int, _Billboard]
    lock_score: dict[int, bool]
    infinite_blocks: dict[int, bool]


def _state(server) -> _HudState:
    state = getattr(server, "_hud_packets_state", None)
    if not isinstance(state, _HudState):
        state = _HudState(billboards={}, lock_score={}, infinite_blocks={})
        try:
            setattr(server, "_hud_packets_state", state)
        except Exception:  # pragma: no cover - exotic server stubs
            pass
    return state


def reset_state(server) -> None:
    """Forget every tracked billboard / team rule (fresh GameScene: new map)."""

    try:
        setattr(server, "_hud_packets_state", None)
    except Exception:  # pragma: no cover
        pass


# --------------------------------------------------------------------------
# recipients / sending
# --------------------------------------------------------------------------


def _as_connection(item: Any):
    if item is None:
        return None
    if hasattr(item, "send") and hasattr(item, "in_game"):
        return item
    conn = getattr(item, "connection", None)
    if conn is not None and hasattr(conn, "send"):
        return conn
    return None


def _in_game_connections(server) -> list:
    connections = getattr(server, "connections", None) or {}
    try:
        values = tuple(connections.values())
    except Exception:
        return []
    return [c for c in values if bool(getattr(c, "in_game", False))]


def _resolve(server, recipients) -> list:
    if recipients is None:
        return _in_game_connections(server)
    single = _as_connection(recipients)
    if single is not None:
        items: Iterable[Any] = (single,)
    else:
        try:
            items = tuple(recipients)
        except TypeError:
            return []
    seen: set[int] = set()
    out = []
    for item in items:
        conn = _as_connection(item)
        if conn is None or id(conn) in seen:
            continue
        if not bool(getattr(conn, "in_game", False)):
            continue
        seen.add(id(conn))
        out.append(conn)
    return out


def _player_id_of(conn) -> Optional[int]:
    player = getattr(conn, "player", None)
    pid = getattr(player, "id", None)
    return int(pid) if isinstance(pid, int) else None


def _knows_entity(conn, entity_id: int) -> bool:
    known = getattr(conn, "known_entity_ids", None)
    if known is None:
        return True
    try:
        return int(entity_id) in known
    except Exception:
        return False


def _send(connections: Iterable[Any], data: bytes) -> int:
    sent = 0
    for conn in connections:
        try:
            conn.send(data, reliable=True)
            sent += 1
        except Exception:
            logger.debug("hud packet %s send failed", data[:1].hex(), exc_info=True)
    return sent


def _generate(packet) -> bytes:
    return bytes(packet.generate())


# --------------------------------------------------------------------------
# value coercion
# --------------------------------------------------------------------------


def _finite(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _position(position: Any) -> Optional[tuple[float, float, float]]:
    try:
        if hasattr(position, "x") and hasattr(position, "y") and hasattr(position, "z"):
            # glm vectors: attribute access only (never iterate them).
            raw = (position.x, position.y, position.z)
        else:
            raw = (position[0], position[1], position[2])
    except Exception:
        return None
    out = []
    for v in raw:
        f = _finite(v)
        if f is None:
            return None
        out.append(max(-_FIXED_MAX, min(_FIXED_MAX, f)))
    return (out[0], out[1], out[2])


def _color(color: Any, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if color is None:
        return default
    try:
        if isinstance(color, int):
            return ((color >> 16) & 0xFF, (color >> 8) & 0xFF, color & 0xFF)
        r, g, b = (int(color[0]), int(color[1]), int(color[2]))
        return (r & 0xFF, g & 0xFF, b & 0xFF)
    except Exception:
        return default


def _team_wire(team: Any) -> Optional[int]:
    team_id = getattr(team, "id", team)
    try:
        team_id = int(team_id)
    except (TypeError, ValueError):
        return None
    try:
        from server.connection import internal_team_to_wire

        return int(internal_team_to_wire(team_id))
    except Exception:
        return team_id


# --------------------------------------------------------------------------
# MinimapBillboard(41) / MinimapBillboardClear(42)
# --------------------------------------------------------------------------


def _billboard_packet(record: _Billboard) -> bytes:
    from shared.packet import MinimapBillboard

    pkt = MinimapBillboard()
    pkt.entity_id = record.entity_id
    pkt.key = record.key
    pkt.icon_name = record.icon_name
    pkt.x, pkt.y, pkt.z = record.position
    pkt.tracking = 1 if record.tracking else 0
    pkt.color = record.color
    return _generate(pkt)


def _clear_packet(entity_id: int) -> bytes:
    from shared.packet import MinimapBillboardClear

    pkt = MinimapBillboardClear()
    pkt.entity_id = int(entity_id)
    return _generate(pkt)


def _billboard_targets(record: _Billboard, connections: Iterable[Any]) -> list:
    if not record.tracking:
        return list(connections)
    # A tracking billboard resolves ``scene.entities[entity_id]`` client-side;
    # a peer that never saw that CreateEntity would just drop it.
    return [c for c in connections if _knows_entity(c, record.entity_id)]


def add_billboard(
    server,
    entity_id,
    key,
    icon_name,
    position,
    *,
    tracking=False,
    color=None,
    recipients=None,
) -> bool:
    """Show (or move / restyle) one minimap billboard.

    ``entity_id`` is the billboard id (short). Free-standing markers should
    use ids >= :data:`BILLBOARD_ID_BASE`; with ``tracking=True`` it must be
    the CreateEntity id the marker follows (``position`` is then only the
    initial value). ``key`` is carried on the wire but the stock client never
    reads it. ``icon_name`` must be in :data:`BILLBOARD_ICONS`. ``color`` is
    an (r, g, b) tuple or 0xRRGGBB int tinting the icon (default white).
    Returns True when the billboard was accepted and recorded.
    """

    try:
        entity_id = int(entity_id)
        key = int(key)
        if not (_SHORT_MIN <= entity_id <= _SHORT_MAX) or not (0 <= key <= 0xFF):
            return False
        icon_name = str(icon_name or "")
        if not icon_name or len(icon_name) > _ICON_NAME_MAX:
            return False
        if icon_name not in BILLBOARD_ICONS:
            logger.warning("refusing unknown minimap billboard icon %r", icon_name)
            return False
        pos = _position(position)
        if pos is None:
            return False
        connections = _resolve(server, recipients)
        player_ids = None
        if recipients is not None:
            player_ids = frozenset(
                pid for pid in (_player_id_of(c) for c in connections) if pid is not None
            )
        record = _Billboard(
            entity_id=entity_id,
            key=key,
            icon_name=icon_name,
            position=pos,
            tracking=bool(tracking),
            color=_color(color, (255, 255, 255)),
            player_ids=player_ids,
        )
        data = _billboard_packet(record)
        _state(server).billboards[entity_id] = record
        _send(_billboard_targets(record, connections), data)
        return True
    except Exception:
        logger.debug("add_billboard failed", exc_info=True)
        return False


def remove_billboard(server, entity_id, *, recipients=None) -> bool:
    """Remove one minimap billboard (MinimapBillboardClear 42).

    With ``recipients=None`` the billboard is forgotten server-side and
    cleared on every in-game client; otherwise only those clients clear it
    (and they are dropped from its replay audience).
    """

    try:
        entity_id = int(entity_id)
        if not (_SHORT_MIN <= entity_id <= _SHORT_MAX):
            return False
        state = _state(server)
        record = state.billboards.get(entity_id)
        connections = _resolve(server, recipients)
        if recipients is None:
            state.billboards.pop(entity_id, None)
        elif record is not None:
            drop = {pid for pid in (_player_id_of(c) for c in connections) if pid is not None}
            if record.player_ids is None:
                everyone = {
                    pid
                    for pid in (_player_id_of(c) for c in _in_game_connections(server))
                    if pid is not None
                }
                record.player_ids = frozenset(everyone - drop)
            else:
                record.player_ids = frozenset(record.player_ids - drop)
            if not record.player_ids:
                state.billboards.pop(entity_id, None)
        _send(connections, _clear_packet(entity_id))
        return True
    except Exception:
        logger.debug("remove_billboard failed", exc_info=True)
        return False


def reveal_billboards(server, connection) -> int:
    """Send every live billboard addressed to ``connection`` (late joiner).

    Call after the joiner's entity reveal: tracking billboards are only sent
    once the peer knows the tracked entity.
    """

    try:
        conns = _resolve(server, connection)
        if not conns:
            return 0
        conn = conns[0]
        pid = _player_id_of(conn)
        sent = 0
        for record in tuple(_state(server).billboards.values()):
            if record.player_ids is not None and (pid is None or pid not in record.player_ids):
                continue
            sent += _send(_billboard_targets(record, (conn,)), _billboard_packet(record))
        return sent
    except Exception:
        logger.debug("reveal_billboards failed", exc_info=True)
        return 0


def clear_all_billboards(server) -> None:
    """Remove every tracked billboard from every in-game client.

    The minimap survives a same-map round restart, so modes must clear their
    billboards when a round ends rather than rely on a new GameScene.
    """

    try:
        for entity_id in tuple(_state(server).billboards):
            remove_billboard(server, entity_id)
    except Exception:
        logger.debug("clear_all_billboards failed", exc_info=True)


# --------------------------------------------------------------------------
# POIFocus(18)
# --------------------------------------------------------------------------


def poi_focus(server, position, *, recipients=None) -> bool:
    """Lock the recipients' camera onto ``position`` (LookAtController).

    There is no release packet: the stock client keeps looking at the point
    until the local player dies or is spawned again. Only use it where a
    respawn or map change follows (round end), and never address spectators
    (a spectator is not respawned by the round restart).
    """

    try:
        from shared.packet import POIFocus

        pos = _position(position)
        if pos is None:
            return False
        pkt = POIFocus()
        pkt.target_x, pkt.target_y, pkt.target_z = pos
        _send(_resolve(server, recipients), _generate(pkt))
        return True
    except Exception:
        logger.debug("poi_focus failed", exc_info=True)
        return False


# --------------------------------------------------------------------------
# ProgressBar(65) -- disabled
# --------------------------------------------------------------------------

_progress_warned = False


def _warn_progress_disabled() -> None:
    global _progress_warned
    if not _progress_warned:
        _progress_warned = True
        logger.warning(
            "ProgressBar(65) is not sent: the stock client crashes drawing it "
            "(hud ProgressBar.draw -> draw_progress_bar arity mismatch)"
        )


def set_progress(server, recipients, progress, rate, *, color=None) -> bool:
    """Would show / update the centre progress bar; always a no-op.

    Returns False. The stock client crashes on the first frame a
    ProgressBar(65) is visible (docs/PROTOCOL.md row 65); use TeamProgress
    (117) or LocalisedMessage (50) for objective progress instead.
    """

    del server, recipients, progress, rate, color
    _warn_progress_disabled()
    return False


def clear_progress(server, recipients) -> bool:
    """Counterpart of :func:`set_progress`; nothing is ever shown, so no-op."""

    del server, recipients
    return False


# --------------------------------------------------------------------------
# TeamLockScore(81) / TeamInfiniteBlocks(82)
# --------------------------------------------------------------------------


def _team_obj(server, team):
    team_id = getattr(team, "id", team)
    try:
        return (getattr(server, "teams", None) or {}).get(int(team_id))
    except Exception:
        return None


def team_score_locked(server, team) -> bool:
    """Server-side truth for "this team's score must not change"."""

    obj = _team_obj(server, team)
    return bool(getattr(obj, "locked_score", False))


def team_infinite_blocks(server, team) -> bool:
    """Server-side truth for "this team's block wallet is not charged"."""

    obj = _team_obj(server, team)
    return bool(getattr(obj, "infinite_blocks", False))


def _team_rule_packet(kind: str, wire_team: int, value: bool) -> bytes:
    from shared.packet import TeamInfiniteBlocks, TeamLockScore

    if kind == "lock_score":
        pkt = TeamLockScore()
        pkt.team_id = wire_team
        pkt.locked = 1 if value else 0
    else:
        pkt = TeamInfiniteBlocks()
        pkt.team_id = wire_team
        pkt.infinite_blocks = 1 if value else 0
    return _generate(pkt)


def _team_score_packet(wire_team: int, score: int) -> bytes:
    import shared.constants as C
    from shared.packet import SetScore

    pkt = SetScore()
    pkt.type = int(getattr(C, "SCORE_TEAM", 0))
    pkt.reason = int(getattr(C, "NO_SCORE_REASON", 0))
    pkt.specifier = int(wire_team)
    pkt.value = int(score)
    return _generate(pkt)


def _set_team_rule(server, team, kind: str, attr: str, value: bool) -> bool:
    try:
        obj = _team_obj(server, team)
        wire = _team_wire(team)
        if obj is None or wire is None:
            return False
        value = bool(value)
        setattr(obj, attr, value)
        getattr(_state(server), kind)[int(getattr(obj, "id", wire))] = value
        connections = _in_game_connections(server)
        _send(connections, _team_rule_packet(kind, wire, value))
        if kind == "lock_score" and not value:
            # The client dropped every team SetScore while locked; resync.
            _send(connections, _team_score_packet(wire, int(getattr(obj, "score", 0) or 0)))
        return True
    except Exception:
        logger.debug("set team %s failed", kind, exc_info=True)
        return False


def set_team_lock_score(server, team, locked) -> bool:
    """Freeze / unfreeze one team's score (``team.locked_score`` + packet 81).

    While locked the stock client ignores team SetScore rows for the team;
    score writers must consult :func:`team_score_locked`. Unlocking re-sends
    the team's current score.
    """

    return _set_team_rule(server, team, "lock_score", "locked_score", locked)


def set_team_infinite_blocks(server, team, enabled) -> bool:
    """Toggle one team's infinite block wallet (``team.infinite_blocks`` + 82)."""

    return _set_team_rule(server, team, "infinite_blocks", "infinite_blocks", enabled)


def reveal_team_rules(server, connection) -> int:
    """Replay runtime team-rule changes StateData does not carry."""

    try:
        conns = _resolve(server, connection)
        if not conns:
            return 0
        state = _state(server)
        sent = 0
        for kind, table in (
            ("lock_score", state.lock_score),
            ("infinite_blocks", state.infinite_blocks),
        ):
            for team_id, value in tuple(table.items()):
                wire = _team_wire(team_id)
                if wire is None:
                    continue
                sent += _send(conns[:1], _team_rule_packet(kind, wire, value))
        return sent
    except Exception:
        logger.debug("reveal_team_rules failed", exc_info=True)
        return 0


def reveal_hud_state(server, connection) -> int:
    """Everything a late joiner needs from this module (one hook call)."""

    return reveal_team_rules(server, connection) + reveal_billboards(server, connection)
