"""Entities that ride on something else: stuck sticky grenades and riot shields.

Both are retail client classes recovered from ``gameScene.pyd`` (2026-09-29):

* ``StickyGrenadeEntity`` (type 34) is only the flying grenade. It collides
  with terrain, has no sound and no ``on_delete``. The stuck grenade is a
  second class, ``AttachedStickyGrenadeEntity`` (type 35): it plays the attach
  cue and the countdown loop, follows a player after ``set_target`` and
  explodes visually in ``on_delete``. The server therefore swaps 34 for 35 at
  the moment of the stick.
* ``RiotShieldEntity`` (type 39) draws nothing. It is the anchor the client
  needs for ``HitEntity(20)``: its ``hit`` plays the shield's bullet or melee
  impact at the bearer (``type == MELEE_KILL`` selects the melee sample).

The retail server is not available, so lifetimes (when 39 is created, what
happens to a sticky whose carrier dies) are the smallest behaviour the client
code supports. See docs/PROTOCOL.md, "Attached entities".
"""
from __future__ import annotations

import logging
import time

import shared.constants as C
from shared.packet import ChangeEntity, HitEntity

from server.entities.registry import send_create_entity_to
from server.projectiles import StickyAttachment, StickyDetachment

logger = logging.getLogger(__name__)

ATTACHED_STICKY_ENTITY = int(getattr(C, "ATTACHED_STICKY_GRENADE_ENTITY", 35))
RIOT_SHIELD_ENTITY = int(getattr(C, "RIOT_SHIELD_ENTITY", 39))

_SET_POSITION = int(getattr(C, "SET_POSITION", 1))
_SET_TARGET = int(getattr(C, "SET_TARGET", 5))
_NO_TARGET = -1


def _target_packet(entity_id: int, target_id: int) -> bytes:
    packet = ChangeEntity()
    packet.entity_id = int(entity_id)
    packet.action = _SET_TARGET
    packet.target_id = int(target_id)
    return bytes(packet.generate())


def _position_packet(entity_id: int, position) -> bytes:
    packet = ChangeEntity()
    packet.entity_id = int(entity_id)
    packet.action = _SET_POSITION
    packet.pos_x, packet.pos_y, packet.pos_z = (float(v) for v in position)
    return bytes(packet.generate())


def _in_game_connections(server):
    for connection in tuple(getattr(server, "connections", {}).values()):
        if bool(getattr(connection, "in_game", False)):
            yield connection


def _knows_entity(connection, entity_id: int) -> bool:
    known = getattr(connection, "known_entity_ids", None)
    return known is None or int(entity_id) in known


def _knows_player(connection, player_id: int) -> bool:
    """The retail scene looks the target id up in its own player table."""
    known = getattr(connection, "known_player_lives", None)
    return known is None or int(player_id) in known


def _send_target(server, entity_id: int, target_id: int) -> None:
    data = _target_packet(entity_id, target_id)
    for connection in _in_game_connections(server):
        if not _knows_entity(connection, entity_id):
            continue
        if target_id != _NO_TARGET and not _knows_player(connection, target_id):
            # This peer sees the grenade stay at the contact point.
            continue
        connection.send(data, reliable=True)


# -- sticky grenade -----------------------------------------------------


def publish_sticky_events(server) -> int:
    """Turn the engine's sticky transitions into entity packets."""
    engine = getattr(server, "projectile_engine", None)
    drain = getattr(engine, "drain_attachment_events", None)
    if not callable(drain):
        return 0
    events = drain()
    active = {id(projectile) for projectile in engine.projectiles}
    for event in events:
        # Resets and owner cleanup can retire a projectile after its physics
        # event was queued. Its numeric id may already name a new entity.
        if id(event.projectile) not in active:
            continue
        if isinstance(event, StickyAttachment):
            # The carrier may have left between the stick and this tick.
            attach_sticky(
                server, event.projectile, event.projectile.attached_player_id
            )
        elif isinstance(event, StickyDetachment):
            detach_sticky(server, event.projectile)
    return len(events)


def attach_sticky(server, projectile, target_id=None):
    """Replace the flying grenade (34) with the stuck one (35)."""
    flying_id = projectile.entity_id
    registry = server.entity_registry
    flying = registry.get(flying_id)
    if flying is None or int(flying.type) != int(C.STICKY_GRENADE_ENTITY):
        # Cleanup already retired the visual; the blast stays server-side.
        return None
    state, owner_id = int(flying.state), int(flying.player_id)
    if registry.remove(flying_id) is not None:
        server.broadcast_destroy_entity(flying_id)

    explode_at = getattr(projectile, "explode_at", None)
    fuse = float(getattr(C, "STICKY_GRENADE_STICK_FUSE", 5.0))
    if explode_at is not None:
        fuse = max(0.0, min(fuse, float(explode_at) - time.time()))
    stuck = registry.place(
        ATTACHED_STICKY_ENTITY,
        float(projectile.x), float(projectile.y), float(projectile.z),
        state=state, kind="projectile", player_id=owner_id,
        radius=float(flying.radius), fuse=fuse,
    )
    projectile.entity_id = stuck.entity_id
    server.broadcast_create_entity(stuck)
    if target_id is not None:
        _send_target(server, stuck.entity_id, int(target_id))
    logger.info(
        "STICKY id=%d stuck at (%.1f,%.1f,%.1f) target=%s",
        stuck.entity_id, projectile.x, projectile.y, projectile.z, target_id,
    )
    return stuck


def detach_sticky(server, projectile) -> bool:
    """Tell clients the grenade no longer follows anyone and where it rests."""
    registry = getattr(server, "entity_registry", None)
    entity = registry.get(projectile.entity_id) if registry is not None else None
    if entity is None or int(entity.type) != ATTACHED_STICKY_ENTITY:
        return False
    entity.x = float(projectile.x)
    entity.y = float(projectile.y)
    entity.z = float(projectile.z)
    _send_target(server, entity.entity_id, _NO_TARGET)
    server.broadcast_known_entity_packet(
        _position_packet(entity.entity_id, (entity.x, entity.y, entity.z)),
        entity.entity_id,
    )
    return True


# -- riot shield --------------------------------------------------------


def _shield_ledger(server) -> dict:
    ledger = getattr(server, "_riot_shield_entities", None)
    if ledger is None:
        ledger = {}
        server._riot_shield_entities = ledger
    return ledger


def _live_shield(server, bearer):
    """Return the bearer's live anchor, or None."""
    record = _shield_ledger(server).get(int(bearer.id))
    if record is None or record[1] is not bearer:
        return None
    entity = record[0]
    if server.entity_registry.get(entity.entity_id) is not entity:
        return None
    return entity


def riot_shield_hit(server, bearer, position, *, melee: bool) -> bool:
    """Play the shield impact for a hit the bearer's shield absorbed."""
    from server.connection import internal_team_to_wire

    if getattr(server, "entity_registry", None) is None:
        return False
    entity = _live_shield(server, bearer)
    if entity is None:
        entity = server.entity_registry.place(
            RIOT_SHIELD_ENTITY,
            float(bearer.x), float(bearer.y), float(bearer.z),
            state=internal_team_to_wire(bearer.team),
            # Not a join-snapshot entity and nothing a bot should react to:
            # each peer gets it, with its target, right before its first hit.
            kind="projectile",
            player_id=int(bearer.id),
        )
        _shield_ledger(server)[int(bearer.id)] = (entity, bearer)

    hit = HitEntity()
    hit.entity_id = int(entity.entity_id)
    hit.x, hit.y, hit.z = (float(v) for v in position)
    hit.type = int(C.MELEE_KILL if melee else C.WEAPON_KILL)
    hit_data = bytes(hit.generate())
    target_data = _target_packet(entity.entity_id, int(bearer.id))

    sent = False
    for connection in _in_game_connections(server):
        if not _knows_player(connection, int(bearer.id)):
            continue
        if send_create_entity_to(connection, entity):
            connection.send(target_data, reliable=True)
        elif not _knows_entity(connection, entity.entity_id):
            continue
        connection.send(hit_data, reliable=True)
        sent = True
    return sent


def _release_shield(server, player_id: int) -> None:
    ledger = getattr(server, "_riot_shield_entities", None)
    if not ledger:
        return
    record = ledger.pop(int(player_id), None)
    if record is None:
        return
    entity = record[0]
    registry = getattr(server, "entity_registry", None)
    if registry is None or registry.get(entity.entity_id) is not entity:
        return  # a round reset already destroyed it
    if registry.remove(entity.entity_id) is not None:
        server.broadcast_destroy_entity(entity.entity_id)


def sweep_riot_shields(server) -> None:
    """Retire the anchors of bearers that died or changed team."""
    ledger = getattr(server, "_riot_shield_entities", None)
    if not ledger:
        return
    from server.connection import internal_team_to_wire

    players = getattr(server, "players", {})
    for player_id, (entity, bearer) in tuple(ledger.items()):
        if (
            server.entity_registry.get(entity.entity_id) is entity
            and players.get(player_id) is bearer
            and bool(getattr(bearer, "alive", False))
            and bool(getattr(bearer, "spawned", False))
            and int(entity.state) == internal_team_to_wire(bearer.team)
        ):
            continue
        _release_shield(server, player_id)


def forget_player(server, player) -> None:
    """Detach everything from a player whose id is about to be reused."""
    player_id = int(player.id)
    engine = getattr(server, "projectile_engine", None)
    detach = getattr(engine, "detach_from_player", None)
    if callable(detach):
        for projectile in detach(player_id):
            detach_sticky(server, projectile)
    _release_shield(server, player_id)
