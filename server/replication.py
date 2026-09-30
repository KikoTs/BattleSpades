"""Network snapshot replication for the Battle Builders retail client.

This module deliberately owns the WorldUpdate cadence and grouping rules.  The
client reconciles its predicted local player against the packet loop stamp, so
changing these rules without a two-client movement capture can reintroduce the
historic random rollback bug.
"""

from __future__ import annotations

import math
import zlib
from typing import Optional, TYPE_CHECKING

import shared.constants as C
from shared.packet import WorldUpdate

from server.class_selection import equipped_tool_authorized
from server.lag_compensation import shooter_rtt_ms

if TYPE_CHECKING:
    from .main import BattleSpadesServer


# WorldUpdate's player section is the only recipient-specific part of an
# otherwise immutable snapshot.  The retail packet has a seven-byte header
# (id, loop, player count), then fixed 56-byte player rows.  Within each row,
# the equipped-tool byte is at +48.  Keep these wire offsets beside the code
# that patches them, and guard them with round-trip tests.
_WORLD_UPDATE_HEADER_SIZE = 7
_WORLD_UPDATE_PLAYER_ROW_SIZE = 56
_WORLD_UPDATE_PLAYER_TOOL_OFFSET = 48
_WORLD_UPDATE_TRAILER_MIN_SIZE = 4  # entity count + turret count
_WORLD_UPDATE_EMPTY_TRAILER = bytes(_WORLD_UPDATE_TRAILER_MIN_SIZE)
# Entity row without its int/float properties (shared.packet.Entity.write).
_WORLD_UPDATE_ENTITY_ROW_SIZE = 33
# Packet body that still leaves as one ENet command at the default MTU
# (1372 wire bytes; see server.connection.max_unframed_payload).
_WORLD_UPDATE_DEFAULT_PAYLOAD_LIMIT = 1329

# Reorder guard for the unsequenced observer stream. A ClientData label that
# arrived this many frames after a newer one proves the link can swap two
# datagrams sent a snapshot interval (two frames) apart.
REORDER_GUARD_MIN_FRAMES = 2
# A displacement of d frames, the maximum over some 1200 ClientData, bounds
# the link's delay spread below (d + 1) frames. Snapshots spaced d + 1 frames
# apart therefore cannot overtake one another.
REORDER_GUARD_MARGIN_FRAMES = 1
# Widest spacing (10 Hz). A link that reorders further gets sequenced
# snapshots instead: ENet then discards a stale one at the receiver.
REORDER_GUARD_MAX_INTERVAL = 6
# Fire, aim/display, and deployed-state bits must not be paired with a rejected
# tool. Flight active/passive (0x04/0x08) and fire (0x20) are independent state.
_WORLD_UPDATE_WEAPON_ACTION_MASK = 0x01 | 0x02 | 0x10 | 0x40 | 0x80


def wire_entity_id(entity_id: int) -> int:
    """Return the signed-short value the retail client knows ``entity_id`` by.

    EntityRegistry allocates uint16 ids (0..65535).  CreateEntity/Entity rows,
    DestroyEntity and ChangeEntity declare ``entity_id`` as a C ``int`` and
    write it through ``write_short(short)``, so Cython truncates ids above
    32767 to their two's-complement short (40000 -> -25536) and the client
    reads that same signed value back.  WorldUpdate's rocket-turret rows pass
    a Python object instead, which Cython range-checks and raises
    ``OverflowError`` on.  Converting here produces identical wire bytes to
    the CreateEntity the client received for the same turret.
    """

    return ((int(entity_id) + 0x8000) & 0xFFFF) - 0x8000


def _wire_turret_row(row) -> tuple:
    entity_id, yaw, pitch = row[0], row[1], row[2]
    return (wire_entity_id(entity_id), float(yaw), float(pitch))


# Scoreboard ping.  The retail client copies each WorldUpdate row's signed
# short ``ping`` (row offset +37) into ``player.ping`` and the scoreboard
# prints ``str(player.ping)`` in its PING column, so the unit is whole
# milliseconds.  Clamp to three digits: the column is narrow and a peer this
# far gone is about to time out anyway.
WIRE_PING_MAX_MS = 999
# Bots have no network peer.  A fixed 0 marks every bot on the scoreboard, so
# each bot shows a stable, plausible ping of its own with a few milliseconds of
# drift, re-rolled about every two seconds (120 loops at 60 Hz).
_BOT_PING_BASE_MS = (24, 72)
_BOT_PING_JITTER_MS = 3
_BOT_PING_BUCKET_LOOPS = 120


def human_ping_ms(player) -> int:
    """Return one human's ENet smoothed round trip in whole milliseconds."""

    # Same accessor lag compensation rewinds by (ENet peer.roundTripTime).
    rtt = shooter_rtt_ms(player)
    if not math.isfinite(rtt) or rtt <= 0.0:
        return 0
    return int(min(WIRE_PING_MAX_MS, round(rtt)))


def bot_ping_ms(player, loop_count: int) -> int:
    """Return a bot's stable per-bot ping with slow, small drift."""

    seed = "%s:%s" % (getattr(player, "name", ""), getattr(player, "id", 0))
    low, high = _BOT_PING_BASE_MS
    base = low + zlib.crc32(seed.encode("utf-8", "replace")) % (high - low + 1)
    bucket = max(0, int(loop_count)) // _BOT_PING_BUCKET_LOOPS
    drift = zlib.crc32(("%s#%d" % (seed, bucket)).encode("utf-8", "replace"))
    jitter = drift % (2 * _BOT_PING_JITTER_MS + 1) - _BOT_PING_JITTER_MS
    return int(max(1, min(WIRE_PING_MAX_MS, base + jitter)))


def wire_ping_ms(player, loop_count: int) -> int:
    """Return the scoreboard ping carried in ``player``'s WorldUpdate row."""

    if bool(getattr(player, "is_bot", False)):
        return bot_ping_ms(player, loop_count)
    return human_ping_ms(player)


class ReplicationService:
    """Build and broadcast immutable 30 Hz WorldUpdate snapshots.

    The service runs on the gameplay thread immediately after simulation.  It
    performs no blocking I/O: ``Connection.send`` only queues ENet packets.
    Connections sharing the same acknowledgement stamp reuse serialized bytes.
    """

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server
        self._last_broadcast_bucket: Optional[int] = None
        self._last_self_row_loop: dict[int, int] = {}
        self._last_advertised_jetpack_active: dict[int, bool] = {}
        # Parachute canopy (state bit 0x01) is also server-owned on the retail
        # client: send its transitions as urgent owner rows like the jetpack.
        self._last_advertised_parachute_active: dict[int, bool] = {}
        # Last handled launch, last possibly replayed input label, timeout.
        self._retail_jump_recovery: dict[int, tuple[int, int, int]] = {}

    def forget_player(self, player_id: int) -> None:
        """Discard recipient state at disconnect or a new-life boundary.

        Player ids are reused immediately.  Retaining the prior owner's
        cadence or jetpack transition state can suppress the replacement
        owner's first self row, so lifecycle code must call this before that
        id represents another retail Character (or the same id respawns).
        This method runs only on the gameplay thread.
        """
        player_id = int(player_id)
        self._last_self_row_loop.pop(player_id, None)
        self._last_advertised_jetpack_active.pop(player_id, None)
        self._last_advertised_parachute_active.pop(player_id, None)
        self._retail_jump_recovery.pop(player_id, None)

    def broadcast_world_updates(self) -> None:
        """Send one grouped snapshot at the configured retail cadence."""
        server = self.server
        config = server.config
        interval = max(
            1,
            int(getattr(config, "worldupdate_broadcast_interval", 2)),
        )
        self_row_interval = max(
            interval,
            int(getattr(config, "worldupdate_self_row_interval", 20)),
        )
        if (
            not server.connections
            or not config.broadcast_world_updates
        ):
            return

        ingame_connections = tuple(
            connection
            for connection in server.connections.values()
            if connection.in_game
        )
        urgent_connections = self._jetpack_transition_connections(
            ingame_connections
        )
        urgent_player_ids = {
            connection.player.id
            for connection in urgent_connections
            if connection.player is not None
        }

        # SimulationRuntime can execute several fixed steps in one catch-up
        # batch and invokes replication once at the latest state. A modulo
        # check at that endpoint loses a 30 Hz boundary whenever the batch
        # crosses an even loop but ends on an odd one. Track cadence buckets
        # instead: publish the newest snapshot once for every advanced bucket,
        # without duplicating calls made at the same endpoint.
        bucket = server.loop_count // interval
        if self._last_broadcast_bucket is None:
            if server.loop_count % interval != 0:
                self._send_urgent_owner_rows(urgent_connections)
                return
        elif bucket <= self._last_broadcast_bucket:
            self._send_urgent_owner_rows(urgent_connections)
            return
        self._last_broadcast_bucket = bucket

        offset = config.worldupdate_loop_offset
        for player in server.players.values():
            if bool(getattr(player, "is_bot", False)):
                # Retail deduplicates every Character's network position by
                # the row ``pong`` value, including remote/server-owned bots.
                # Bots have no ClientData stream, so leaving their ack at the
                # default zero makes the client accept one snapshot and then
                # extrapolate that stale velocity forever.  A bot can never
                # be a retail owner, therefore the authoritative server loop
                # is its correct monotonic remote-snapshot stamp.
                player.wu_ack_loop = max(0, int(server.loop_count))
            elif player.last_applied_input_loop is not None:
                player.wu_ack_loop = max(
                    0, player.last_applied_input_loop + offset
                )

        if self._split_delivery() and self._broadcast_split(
            ingame_connections,
            urgent_player_ids,
            self_row_interval,
            interval,
        ):
            return

        groups: dict[tuple, list] = {}
        for connection in ingame_connections:
            player = connection.player
            if self._owner_row_due(player, urgent_player_ids, self_row_interval):
                # A self row is recipient-specific: its own tool byte must be
                # the retail no-op sentinel, while every observer still needs
                # this player's real equipped tool for animation/rendering.
                # Per-player pong values are already embedded in their rows,
                # so differing owner stamps do not require another base
                # serialization. Only delivery reliability splits a group.
                key = (
                    "transition"
                    if player.id in urgent_player_ids
                    else "self",
                )
            else:
                # Production excludes only the recipient's local player row.
                # That same player is still present in every observer's group,
                # preserving authoritative remote animation and hitboxes.
                key = ("exclude", player.id if player is not None else None)
            groups.setdefault(key, []).append(connection)

        for key, connections in groups.items():
            kind = key[0]
            if kind in ("self", "transition"):
                # Route through the server compatibility seam so packet tools
                # and characterization tests can replace serialization.  The
                # immutable base keeps real tools for observer rows; a single
                # byte is changed in each owner's derived payload below.
                # Header loop_count is the global snapshot/entity clock.  The
                # local reconciliation label lives in each player row's pong.
                data = server.build_world_update_data(
                    loop_count_override=int(server.loop_count),
                )
                tool_offsets = self._player_tool_offsets(data)
                if config.debug_selfrow:
                    for connection in connections:
                        server._log_selfrow(
                            connection.player,
                            int(connection.player.wu_ack_loop),
                        )
            else:
                _kind, value = key
                data = server.build_world_update_data(
                    exclude_player_id=value,
                    loop_count_override=int(server.loop_count),
                )
                tool_offsets = {}
            for connection in connections:
                is_transition = kind == "transition"
                payload = data
                if kind in ("self", "transition"):
                    player = connection.player
                    if player is not None:
                        payload = self._with_local_owner_overrides(
                            data,
                            tool_offsets,
                            player.id,
                        )
                connection.send(payload, reliable=is_transition)
                if is_transition and connection.player is not None:
                    self._flush_transition_delivery(connection)
                if (
                    kind in ("self", "transition")
                    and connection.player is not None
                ):
                    self._record_owner_row(
                        connection.player,
                        int(connection.player.wu_ack_loop),
                        transition=is_transition,
                    )
            server.metrics.record_world_packet(len(data), len(connections))

    # -- split delivery ------------------------------------------------------
    #
    # One ENet channel is all the stock client opens. On it a sequenced
    # unreliable packet is held behind any lost reliable packet sent before
    # it, so one lost event packet froze every remote player until the
    # retransmission arrived. Split delivery sends two streams instead:
    #
    # * observer rows (everyone but the recipient, entities, turrets) leave
    #   UNSEQUENCED: nothing can hold them. Neither client orders
    #   WorldUpdates itself, so the server keeps two of them from crossing
    #   (reorder guard) and keeps a new life's rows behind its CreatePlayer
    #   (Connection.held_snapshot_rows);
    # * the recipient's own row keeps the ordered stream it always had
    #   (sequenced, reliable on a flight transition). A late owner row is
    #   invisible to the player, a stale one can roll the player back.

    def _split_delivery(self) -> bool:
        mode = getattr(self.server.config, "worldupdate_delivery", "sequenced")
        return str(mode).lower() == "split"

    @staticmethod
    def _parse_snapshot(data: bytes):
        """Return ``(prefix, rows, tail)`` of a WorldUpdate, else None.

        ``prefix`` is the packet id and loop, ``rows`` maps player id to its
        56-byte row in wire order, ``tail`` is the entity and turret section.
        """
        if (
            not isinstance(data, (bytes, bytearray))
            or len(data) < _WORLD_UPDATE_HEADER_SIZE
            or data[0] != WorldUpdate.id
        ):
            return None
        player_count = int.from_bytes(data[5:7], "little", signed=False)
        rows_end = (
            _WORLD_UPDATE_HEADER_SIZE
            + player_count * _WORLD_UPDATE_PLAYER_ROW_SIZE
        )
        if rows_end + _WORLD_UPDATE_TRAILER_MIN_SIZE > len(data):
            return None
        rows: dict[int, bytes] = {}
        for index in range(player_count):
            start = (
                _WORLD_UPDATE_HEADER_SIZE
                + index * _WORLD_UPDATE_PLAYER_ROW_SIZE
            )
            player_id = data[start]
            if player_id in rows:
                return None
            rows[player_id] = bytes(
                data[start:start + _WORLD_UPDATE_PLAYER_ROW_SIZE]
            )
        return bytes(data[:5]), rows, bytes(data[rows_end:])

    @staticmethod
    def _tail_without(tail: bytes, entity_ids) -> bytes:
        """Return the entity and turret section without ``entity_ids``.

        An entity row is 33 bytes plus its properties (four bytes per int,
        two per float; the two counts sit at +30 and +31), a turret row is
        six. Ids are the signed shorts CreateEntity wrote. A section that
        does not parse is withheld whole rather than sent unfiltered.
        """
        try:
            position = 0
            count = int.from_bytes(tail[position:position + 2], "little")
            position += 2
            entities = []
            for _ in range(count):
                if position + _WORLD_UPDATE_ENTITY_ROW_SIZE > len(tail):
                    raise ValueError("truncated entity row")
                length = (
                    _WORLD_UPDATE_ENTITY_ROW_SIZE
                    + 4 * tail[position + 30]
                    + 2 * tail[position + 31]
                )
                row = tail[position:position + length]
                if len(row) != length:
                    raise ValueError("truncated entity properties")
                entities.append(row)
                position += length
            turret_count = int.from_bytes(
                tail[position:position + 2], "little"
            )
            position += 2
            if position + 6 * turret_count != len(tail):
                raise ValueError("turret rows do not end the packet")
            turrets = [
                tail[position + 6 * index:position + 6 * index + 6]
                for index in range(turret_count)
            ]
        except (ValueError, IndexError):
            return _WORLD_UPDATE_EMPTY_TRAILER

        def wire_id(row: bytes) -> int:
            return int.from_bytes(row[:2], "little", signed=True)

        entities = [row for row in entities if wire_id(row) not in entity_ids]
        turrets = [row for row in turrets if wire_id(row) not in entity_ids]
        return b"".join((
            len(entities).to_bytes(2, "little"), *entities,
            len(turrets).to_bytes(2, "little"), *turrets,
        ))

    @staticmethod
    def _assemble_snapshot(prefix: bytes, rows: list, tail: bytes) -> bytes:
        return b"".join(
            (prefix, len(rows).to_bytes(2, "little"), *rows, tail)
        )

    @classmethod
    def _observer_parts(
        cls,
        prefix: bytes,
        rows: list,
        tail: bytes,
        limit: int,
    ) -> list:
        """Pack rows and the tail into packets of at most ``limit`` bytes.

        A packet above the peer's fragment limit cannot be unsequenced (ENet
        fragments it, reliably unless flagged), so rows that do not fit
        beside the tail travel in further packets with an empty tail. The
        stock client looks rows up per player it knows and skips the absent
        ones, so a snapshot may arrive in parts.
        """
        row_size = _WORLD_UPDATE_PLAYER_ROW_SIZE
        room = int(limit) - _WORLD_UPDATE_HEADER_SIZE
        beside_tail = max(0, (room - len(tail)) // row_size)
        per_part = max(
            1, (room - _WORLD_UPDATE_TRAILER_MIN_SIZE) // row_size
        )
        parts = [cls._assemble_snapshot(prefix, rows[:beside_tail], tail)]
        for start in range(beside_tail, len(rows), per_part):
            parts.append(cls._assemble_snapshot(
                prefix,
                rows[start:start + per_part],
                _WORLD_UPDATE_EMPTY_TRAILER,
            ))
        return parts

    @staticmethod
    def _owner_prefix(connection, prefix: bytes) -> bytes:
        """Packet id and header loop for one recipient's own-row packet.

        The stock client stores every WorldUpdate's header loop in
        ``last_world_update`` and reports it in ShootPacket
        ``shot_on_world_update``; lag compensation reads it as the age of
        the remote bodies the shooter saw. Those bodies come from the
        observer stream, so the own-row packet repeats the loop of the
        newest observer snapshot sent to this recipient. Reconciliation
        does not use the header (it pairs by the row's pong).
        """
        observer_loop = getattr(connection, "_wu_observer_loop", None)
        if not isinstance(observer_loop, int) or isinstance(observer_loop, bool):
            return prefix
        return prefix[:1] + max(0, observer_loop).to_bytes(
            4, "little", signed=True
        )

    @classmethod
    def _owner_payload(cls, prefix: bytes, row: bytes) -> bytes:
        """One-row WorldUpdate for the row's owner (tool sentinel 0xFF)."""
        if row[_WORLD_UPDATE_PLAYER_TOOL_OFFSET] != 0xFF:
            patched = bytearray(row)
            patched[_WORLD_UPDATE_PLAYER_TOOL_OFFSET] = 0xFF
            row = bytes(patched)
        return cls._assemble_snapshot(
            prefix, [row], _WORLD_UPDATE_EMPTY_TRAILER
        )

    def _owner_row_due(
        self,
        player,
        urgent_player_ids,
        self_row_interval: int,
    ) -> bool:
        """Whether this broadcast refreshes ``player``'s own anchor."""
        config = self.server.config
        if not (
            config.worldupdate_include_self
            and player is not None
            and self.self_row_is_safe(player)
            and player.last_applied_input_loop is not None
            # Never stamp an owner row with a refilled (guessed) label.
            and not getattr(player, "last_applied_input_synthesized", False)
        ):
            return False
        if player.id in urgent_player_ids:
            return True
        recovery = self._retail_jump_recovery.get(player.id)
        if (
            recovery is not None
            and bool(getattr(player, "airborne", False))
            and self._retail_jump_recovery_allowed(player)
        ):
            launch, through, expires = recovery
            if (
                launch <= player.last_applied_input_loop <= through
                and int(self.server.loop_count) < expires
            ):
                return False
        interval = self._owner_interval(player, self_row_interval)
        return self._should_send_self_row(player.id, interval)

    @staticmethod
    def _retail_jump_recovery_allowed(player) -> bool:
        connection = getattr(player, "connection", None)
        return (
            getattr(connection, "flight_profile_capable", None) is False
            and not getattr(player, "is_bot", False)
            # Merely carrying a pack does not change an ordinary tap-jump.
            # Only actual flight/handoff must bypass the replay spacing.
            and not getattr(player, "jetpack_active", False)
            and not getattr(player, "_jetpack_physics_active", False)
            and not getattr(player, "_jetpack_activation_defer_remaining", 0)
            and not getattr(player, "_jetpack_exhaustion_tail_remaining", 0)
            and not getattr(player, "parachute_active", False)
            and not getattr(player, "_parachute_physics_active", False)
            and not getattr(player, "_parachute_deploy_pending", False)
        )

    def _record_retail_jump_recovery(self, player, stamp: int) -> None:
        """Let the next owner row target fresh history after one jump replay.

        Retail normally records history before movement, but correction replay
        rebuilds it AFTER movement under the old label. Sending another anchor
        into that rebuilt window causes repeated corrections while walking.
        Space only the row following a launch's first queued correction row;
        ordinary airborne cadence resumes afterwards. Labels/state stay true.
        """
        if not self._retail_jump_recovery_allowed(player):
            self._retail_jump_recovery.pop(player.id, None)
            return
        launch = getattr(player, "last_retail_jump_loop", None)
        old = self._retail_jump_recovery.get(player.id)
        if launch is None:
            return
        if not bool(getattr(player, "airborne", False)):
            # A blocked jump/climb can set jump_this_frame while grounded.
            # Stock Character repeatedly restores its owner cache there;
            # withholding fresh anchors stalls climbing and magnifies the
            # next correction. Close any gap on contact and remember this
            # attempt so merely walking off the ledge cannot restart it.
            self._retail_jump_recovery[player.id] = (
                int(launch), int(stamp) - 1, int(self.server.loop_count)
            )
            return
        if old is not None and launch <= old[0]:
            return
        newest = max(stamp, max(getattr(player, "input_history", {}), default=stamp))
        rtt = shooter_rtt_ms(player)
        rtt = min(1000.0, max(0.0, rtt)) if math.isfinite(rtt) else 1000.0
        # ClientData is produced after movement. Include the round trip and
        # three scene/service phases; this is a delivery estimate, not an ACK.
        through = newest + math.ceil(rtt * 60.0 / 1000.0) + 3
        expires = int(self.server.loop_count) + min(120, max(15, 2 * (through - stamp)))
        self._retail_jump_recovery[player.id] = (int(launch), through, expires)

    def _owner_interval(self, player, grounded_interval: int) -> int:
        """Apply retail's jump-anchor workaround only to retail peers.

        BattleSpades advertises BSCF during its existing ticket handshake.
        Its prediction has no stock jump-position restore, so retain the
        configured native airborne cadence. Physics and observer rows do not
        depend on this recipient-specific owner refresh.
        """
        if not bool(getattr(player, "airborne", False)):
            return grounded_interval
        config = self.server.config
        interval = int(getattr(
            config, "worldupdate_airborne_self_row_interval", grounded_interval,
        ))
        connection = getattr(player, "connection", None)
        if not bool(getattr(connection, "flight_profile_capable", False)):
            interval = int(getattr(
                config, "worldupdate_retail_airborne_self_row_interval", interval,
            ))
        return max(grounded_interval, interval)

    def _observer_delivery(self, connection, interval: int):
        """Return ``(unsequenced, spacing in ticks)`` for one recipient.

        None means no observer snapshot may be sent to it this tick.
        """
        config = self.server.config
        unsequenced = True
        spacing = int(interval)
        player = getattr(connection, "player", None)
        spread_of = getattr(player, "input_reorder_spread_frames", None)
        if (
            bool(getattr(config, "worldupdate_reorder_guard", True))
            and callable(spread_of)
        ):
            spread = int(spread_of(int(self.server.loop_count)))
            if spread >= REORDER_GUARD_MIN_FRAMES:
                # Whatever is sent next must not overtake the previous
                # snapshot, in either delivery mode.
                spacing = max(spacing, spread + REORDER_GUARD_MARGIN_FRAMES)
                if spacing > REORDER_GUARD_MAX_INTERVAL:
                    unsequenced = False
        was_sequenced = bool(
            getattr(connection, "_wu_observer_sequenced", False)
        )
        if unsequenced and was_sequenced:
            # An ordered snapshot may still wait at the receiver behind a
            # reliable packet sent before it; an unsequenced one sent now
            # would be applied first and the held one after it. Another
            # ordered one would only move that risk along, so the stream
            # pauses until those packets are acknowledged (about one round
            # trip, once, when a link stops reordering this badly).
            if self._ordered_snapshot_may_be_held(connection):
                return None
        if not unsequenced and was_sequenced:
            # Sequenced snapshots order themselves: full cadence.
            spacing = int(interval)
        return unsequenced, spacing

    @staticmethod
    def _ordered_snapshot_may_be_held(connection) -> bool:
        """Whether a reliable packet older than the last ordered snapshot
        is still unacknowledged by this peer."""
        pending = getattr(connection, "reliable_unacked_through", None)
        barrier = getattr(connection, "_wu_ordered_barrier", None)
        if callable(pending) and isinstance(barrier, int):
            return bool(pending(barrier))
        in_transit = getattr(
            getattr(connection, "peer", None), "reliableDataInTransit", 0
        )
        return isinstance(in_transit, int) and in_transit > 0

    def _observer_due(self, connection, spacing: int, interval: int) -> bool:
        loop_count = int(self.server.loop_count)
        last = getattr(connection, "_wu_observer_loop", None)
        if (
            spacing > interval
            and isinstance(last, int)
            and 0 <= loop_count - last < spacing
        ):
            return False
        try:
            connection._wu_observer_loop = loop_count
        except AttributeError:
            pass
        return True

    @staticmethod
    def _send_observer(connection, payload: bytes, unsequenced: bool) -> None:
        send_snapshot = getattr(connection, "send_snapshot", None)
        if callable(send_snapshot):
            send_snapshot(payload, unsequenced=unsequenced)
        else:
            # Test and tooling doubles expose only the ordered send.
            unsequenced = False
            connection.send(payload, reliable=False)
        try:
            connection._wu_observer_sequenced = not unsequenced
            if not unsequenced:
                # Reliable packets numbered up to here precede this snapshot.
                connection._wu_ordered_barrier = int(
                    getattr(connection, "reliable_send_index", 0) or 0
                )
        except AttributeError:
            pass

    def _settle_owner_state(self, player) -> None:
        """Mark a flight transition handled when there is no row to send."""
        self._last_self_row_loop[player.id] = self.server.loop_count
        self._last_advertised_jetpack_active[player.id] = bool(
            getattr(player, "jetpack_active", False)
        )
        self._last_advertised_parachute_active[player.id] = bool(
            getattr(player, "parachute_active", False)
        )

    def _record_world_delivery(self, sends: int, total_bytes: int) -> None:
        metrics = self.server.metrics
        record = getattr(metrics, "record_world_delivery", None)
        if callable(record):
            record(sends, total_bytes)
        elif sends:
            metrics.record_world_packet(total_bytes // sends, sends)

    def _broadcast_split(
        self,
        connections: tuple,
        urgent_player_ids: set,
        self_row_interval: int,
        interval: int,
    ) -> bool:
        """Send this cadence tick as two streams; False if not applicable."""
        server = self.server
        config = server.config
        data = server.build_world_update_data(
            loop_count_override=int(server.loop_count),
        )
        snapshot = self._parse_snapshot(data)
        if snapshot is None:
            return False
        prefix, rows, tail = snapshot
        sends = 0
        total_bytes = 0
        for connection in connections:
            player = connection.player
            player_id = None if player is None else player.id
            delivery = self._observer_delivery(connection, interval)
            unsequenced, spacing = delivery or (True, interval)
            if delivery is not None and self._observer_due(
                connection, spacing, interval
            ):
                held_rows = getattr(connection, "held_snapshot_rows", None)
                held = held_rows() if callable(held_rows) else ()
                limit_of = getattr(connection, "snapshot_payload_limit", None)
                limit = (
                    int(limit_of()) if callable(limit_of)
                    else _WORLD_UPDATE_DEFAULT_PAYLOAD_LIMIT
                )
                observer_rows = [
                    row for row_id, row in rows.items()
                    if row_id != player_id and row_id not in held
                ]
                held_entities = {
                    key[1] for key in held
                    if isinstance(key, tuple) and key[0] == "entity"
                }
                observer_tail = (
                    self._tail_without(tail, held_entities)
                    if held_entities else tail
                )
                for payload in self._observer_parts(
                    prefix, observer_rows, observer_tail, limit
                ):
                    self._send_observer(connection, payload, unsequenced)
                    sends += 1
                    total_bytes += len(payload)
            if not self._owner_row_due(
                player, urgent_player_ids, self_row_interval
            ):
                continue
            row = rows.get(player_id)
            if row is None:
                # No live body (dead owner): nothing to anchor.
                self._settle_owner_state(player)
                continue
            is_transition = player_id in urgent_player_ids
            payload = self._owner_payload(
                self._owner_prefix(connection, prefix), row
            )
            connection.send(payload, reliable=is_transition)
            if is_transition:
                self._flush_transition_delivery(connection)
            self._record_owner_row(
                player,
                int(player.wu_ack_loop),
                transition=is_transition,
            )
            if config.debug_selfrow:
                server._log_selfrow(player, int(player.wu_ack_loop))
            sends += 1
            total_bytes += len(payload)
        self._record_world_delivery(sends, total_bytes)
        return True

    @staticmethod
    def _player_tool_offsets(data: bytes) -> dict[int, int]:
        """Return verified player-id to equipped-tool byte offsets.

        The player rows precede variable-size entity and turret sections, so
        their offsets can be derived without decoding the packet tail.  Opaque
        non-WorldUpdate payloads are accepted for compatibility with test and
        diagnostic seams; a real packet with an incomplete player section is
        rejected instead of patching an unproven byte.
        """
        if (
            len(data) < _WORLD_UPDATE_HEADER_SIZE
            or data[0] != WorldUpdate.id
        ):
            return {}

        player_count = int.from_bytes(data[5:7], "little", signed=False)
        rows_end = (
            _WORLD_UPDATE_HEADER_SIZE
            + player_count * _WORLD_UPDATE_PLAYER_ROW_SIZE
        )
        if rows_end + _WORLD_UPDATE_TRAILER_MIN_SIZE > len(data):
            raise ValueError("truncated WorldUpdate player section")

        offsets: dict[int, int] = {}
        for index in range(player_count):
            row_start = (
                _WORLD_UPDATE_HEADER_SIZE
                + index * _WORLD_UPDATE_PLAYER_ROW_SIZE
            )
            player_id = data[row_start]
            if player_id in offsets:
                raise ValueError("duplicate player id in WorldUpdate")
            offsets[player_id] = (
                row_start + _WORLD_UPDATE_PLAYER_TOOL_OFFSET
            )
        return offsets

    @staticmethod
    def _with_local_owner_overrides(
        data: bytes,
        tool_offsets: dict[int, int],
        player_id: int,
    ) -> bytes:
        """Derive one owner row without mutating the shared base payload.

        The local tool sentinel prevents palette/tool replay. No action or
        state byte is recipient-specific: repurposing a gameplay bit as an
        acknowledgement visibly changes the stock client's character state.
        """
        tool_offset = tool_offsets.get(player_id)
        if tool_offset is None:
            return data
        if data[tool_offset] == 0xFF:
            return data
        payload = bytearray(data)
        payload[tool_offset] = 0xFF
        return bytes(payload)

    def _jetpack_transition_connections(self, connections: tuple) -> list:
        """Return owners whose advertised jetpack state changed this tick.

        The retail client does not echo jetpack-active state in ClientData.  It
        learns the transition from WorldUpdate action bit 0x04, so activation
        and release cannot wait behind the normal reduced airborne self-row
        cadence.  Missing entries intentionally mean ``False``: a first active
        snapshot is urgent, while an ordinary inactive spawn is not.
        """
        if not self.server.config.worldupdate_include_self:
            return []
        urgent = []
        for connection in connections:
            player = connection.player
            if (
                player is None
                or player.last_applied_input_loop is None
                or not self.self_row_is_safe(player)
            ):
                continue
            active = bool(getattr(player, "jetpack_active", False))
            advertised = self._last_advertised_jetpack_active.get(
                player.id, False
            )
            canopy = bool(getattr(player, "parachute_active", False))
            canopy_advertised = self._last_advertised_parachute_active.get(
                player.id, False
            )
            if active != advertised or canopy != canopy_advertised:
                urgent.append(connection)
        return urgent

    def _send_urgent_owner_rows(self, connections: list) -> None:
        """Send transition-only owner snapshots between 30 Hz cadence rows."""
        if not connections:
            return
        server = self.server
        offset = server.config.worldupdate_loop_offset
        for connection in connections:
            player = connection.player
            stamp = max(0, player.last_applied_input_loop + offset)
            player.wu_ack_loop = stamp
            data = server.build_world_update_data(
                loop_count_override=int(server.loop_count),
                local_player_id=player.id,
            )
            snapshot = (
                self._parse_snapshot(data) if self._split_delivery() else None
            )
            if snapshot is not None:
                # Reliable delivery can arrive after newer unsequenced
                # observer rows, so the transition carries the owner alone.
                row = snapshot[1].get(player.id)
                if row is None:
                    self._settle_owner_state(player)
                    continue
                data = self._owner_payload(
                    self._owner_prefix(connection, snapshot[0]), row
                )
            else:
                data = self._with_local_owner_overrides(
                    data,
                    self._player_tool_offsets(data),
                    player.id,
                )
            # Unlike ordinary 30 Hz snapshots, this rare state transition is
            # reliable so packet loss cannot leave effects/flight stuck until
            # a later cadence row. Physics phase remains an estimate; ENet
            # delivery is not a GameScene application ACK. Ordinary snapshots
            # continue at the configured cadence for deterministic prediction.
            connection.send(data, reliable=True)
            self._flush_transition_delivery(connection)
            self._record_owner_row(player, int(stamp), transition=True)
            if server.config.debug_selfrow:
                server._log_selfrow(player, int(stamp))
            server.metrics.record_world_packet(len(data), 1)

    def _flush_transition_delivery(self, connection) -> None:
        """Flush one rare reliable transition promptly to the ENet socket.

        ``peer.send`` only queues an ENet command. This flush reduces avoidable
        local queue delay on activation/release; it does not wait for an ACK
        and provides no proof that retail applied the row. It never runs for
        ordinary 30 Hz snapshots.
        """
        host = getattr(self.server, "host", None)
        if host is None:
            host = getattr(getattr(connection, "peer", None), "host", None)
        flush = getattr(host, "flush", None)
        if not callable(flush):
            return
        try:
            flush()
        except (OSError, RuntimeError):
            # A disconnect can invalidate the peer between grouping and send.
            # The connection can disappear between grouping and flush. The
            # next normal send/disconnect cleanup owns recovery.
            return

    def _record_owner_row(
        self, player, stamp: int, *, transition: bool = False
    ) -> None:
        """Remember the state actually queued to one retail owner."""
        self._record_retail_jump_recovery(player, stamp)
        self._last_self_row_loop[player.id] = self.server.loop_count
        self._last_advertised_jetpack_active[player.id] = bool(
            getattr(player, "jetpack_active", False)
        )
        self._last_advertised_parachute_active[player.id] = bool(
            getattr(player, "parachute_active", False)
        )
        snapshot = getattr(player, "world_update_snapshot", None)
        if callable(snapshot):
            # Retain the actual queued owner rows for protocol diagnostics.
            # Authoritative movement never rewinds to this output history.
            # A built or excluded snapshot was not queued to the owner.
            row = snapshot()
            position = tuple(row[0])
            velocity = (
                tuple(row[2])
                if len(row) > 2
                else (0.0, 0.0, 0.0)
            )
            record = getattr(player, "record_owner_anchor", None)
            if callable(record):
                record(
                    int(stamp),
                    position,
                    velocity,
                    queued_server_tick=int(self.server.loop_count),
                )
            else:
                # Compatibility for lightweight packet/replication test
                # doubles which do not implement the Player facade method.
                player.last_advertised_owner_position = position
        if transition:
            note_transition = getattr(
                player, "note_jetpack_transition_sent", None
            )
            if callable(note_transition):
                note_transition(
                    bool(getattr(player, "jetpack_active", False)),
                    int(stamp),
                )

    def _should_send_self_row(self, player_id: int, interval: int) -> bool:
        """Return whether the local correction anchor needs a refresh now."""
        last_loop = self._last_self_row_loop.get(player_id)
        if last_loop is None:
            return True
        return (self.server.loop_count - last_loop) >= interval

    def build_world_update_packet(
        self,
        exclude_player_id: Optional[int] = None,
        loop_count_override: Optional[int] = None,
        local_player_id: Optional[int] = None,
    ) -> WorldUpdate:
        """Return one recipient-compatible stamped snapshot.

        ``local_player_id`` retains that player's reconciliation row but
        serializes tool ``0xFF``.  Retail applies network position first, then
        rejects that tool id as outside its selectable range.  This prevents a
        delayed self row from switching the local tool or resetting its block
        palette without hiding real tools from observers.
        """
        server = self.server
        world_update = WorldUpdate()
        if loop_count_override is not None:
            world_update.loop_count = max(0, loop_count_override)
        else:
            # The packet header drives global snapshot/entity timing.  Local
            # prediction uses each row's pong (Player.wu_ack_loop) instead.
            world_update.loop_count = max(0, server.loop_count)

        corpse_lifecycle = getattr(server, "corpse_lifecycle", None)
        should_replicate_corpse = getattr(
            corpse_lifecycle,
            "should_replicate_normal_corpse",
            None,
        )
        for player_id, player in server.players.items():
            if player_id == exclude_player_id:
                continue
            is_live = bool(player.alive and player.spawned)
            is_flying_corpse = bool(
                not is_live
                and callable(should_replicate_corpse)
                and should_replicate_corpse(player)
            )
            if not is_live and not is_flying_corpse:
                continue
            snapshot = self._sanitize_player_snapshot(
                player, player.world_update_snapshot()
            )
            # Player.world_update_snapshot leaves the ping field 0; the
            # scoreboard's PING column reads it, so fill in the real value.
            snapshot = (
                snapshot[:3]
                + (wire_ping_ms(player, world_update.loop_count),)
                + snapshot[4:]
            )
            if player_id == local_player_id:
                snapshot = snapshot[:9] + (0xFF,) + snapshot[10:]
            world_update[player_id] = snapshot

        world_update.updated_entities = list(server.entities.values())
        world_update.rocket_turrets = [
            _wire_turret_row(turret.world_update())
            for turret in server.rocket_turrets.values()
        ]
        return world_update

    @staticmethod
    def _sanitize_player_snapshot(player, snapshot: tuple) -> tuple:
        """Return a WorldUpdate row that cannot construct an invalid weapon.

        The retail client calls ``Player.set_tool`` for every remote row.  In
        particular, a naked MG_TOOL constructs ``MGWeapon`` without the entity
        models supplied by ChangeEntity and crashes at ``entity_display[0]``.
        Mounted-gun selection is therefore represented only by its reliable
        entity transition; its WorldUpdate tool byte is always the proven
        invalid/no-op sentinel.

        Other tools must still agree with the live, normalized loadout.  This
        outbound boundary is deliberate defense in depth: even accidental
        internal state corruption cannot be reflected into every client.
        """

        try:
            tool_id = int(snapshot[9])
        except (IndexError, TypeError, ValueError):
            tool_id = -1
        authorized = equipped_tool_authorized(player, tool_id)
        mounted_mg = tool_id == int(C.MG_TOOL) and authorized
        if authorized and not mounted_mg:
            return snapshot

        action = int(snapshot[7]) & 0xFF
        if not mounted_mg:
            action &= ~_WORLD_UPDATE_WEAPON_ACTION_MASK
        return (
            snapshot[:7]
            + (action,)
            + snapshot[8:9]
            + (0xFF,)
            + snapshot[10:]
        )

    def build_world_update_data(
        self,
        exclude_player_id: Optional[int] = None,
        loop_count_override: Optional[int] = None,
        local_player_id: Optional[int] = None,
    ) -> bytes:
        """Serialize a snapshot once for all recipients in its group."""
        return bytes(
            self.build_world_update_packet(
                exclude_player_id,
                loop_count_override,
                local_player_id,
            ).generate()
        )

    @staticmethod
    def self_row_is_safe(player) -> bool:
        """Return whether the local reconciliation anchor may be refreshed.

        All spawned tools need a current self row.  Suppressing the row while
        the block tool is held leaves ``network_position`` at the last weapon
        anchor; repeated jump/build input then corrects against that stale
        position and can roll the retail client back by dozens of blocks.

        Block drag completion remains owned by the dedicated BlockLine echo.
        The WorldUpdate row mirrors the already-selected tool and must not be
        used as a replacement for that reliable placement acknowledgement.
        """
        return True
