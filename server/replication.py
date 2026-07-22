"""Network snapshot replication for the Battle Builders retail client.

This module deliberately owns the WorldUpdate cadence and grouping rules.  The
client reconciles its predicted local player against the packet loop stamp, so
changing these rules without a two-client movement capture can reintroduce the
historic random rollback bug.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from shared.packet import WorldUpdate

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
        self._jetpack_owner_handoff_deadline: dict[int, int] = {}
        self._jetpack_owner_handoff_target: dict[int, bool] = {}
        self._jetpack_owner_release_settle_deadline: dict[int, int] = {}
        self._last_owner_airborne: dict[int, bool] = {}
        self._landing_owner_handoff_deadline: dict[int, int] = {}

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
        self._jetpack_owner_handoff_deadline.pop(player_id, None)
        self._jetpack_owner_handoff_target.pop(player_id, None)
        self._jetpack_owner_release_settle_deadline.pop(player_id, None)
        self._last_owner_airborne.pop(player_id, None)
        self._landing_owner_handoff_deadline.pop(player_id, None)

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

        groups: dict[tuple, list] = {}
        for connection in ingame_connections:
            player = connection.player
            if (
                config.worldupdate_include_self
                and player is not None
                and self.self_row_is_safe(player)
                and player.last_applied_input_loop is not None
                and (
                    player.id in urgent_player_ids
                    or (
                        not self._jetpack_owner_handoff_active(player)
                        and not self._landing_owner_handoff_active(player)
                        and self._should_send_self_row(
                            player.id,
                            max(
                                self_row_interval,
                                int(getattr(
                                    config,
                                    "worldupdate_airborne_self_row_interval",
                                    self_row_interval,
                                )),
                            ) if bool(getattr(player, "airborne", False))
                            else self_row_interval,
                        )
                    )
                )
            ):
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
            if active != advertised:
                if (
                    advertised
                    and not active
                    and self._defer_jetpack_release_transition(player)
                ):
                    # Key-up and fuel exhaustion already stop local thrust.
                    # Sending the inactive row in mid-air only adds a position
                    # correction; wait until the owner is settled while every
                    # observer continues receiving authoritative false state.
                    continue
                urgent.append(connection)
        return urgent

    def _defer_jetpack_release_transition(self, player) -> bool:
        """Hold the owner's inactive action row until grounded and released.

        Retail stops local thrust from physical SPACE key-up or zero fuel even
        while its last WorldUpdate action bit remains active. Position and
        velocity in that same row are inseparable from the bit. During fast
        Jump Pack flight an integer server/client phase differs by either
        0.63 or 4.9 blocks at exhaustion, so transmitting the row there causes
        the reported ADJUST/SNAP. Observers are unaffected because their rows
        are never suppressed.
        """
        input_state = getattr(player, "input", None)
        if input_state is None:
            # Lightweight tests and non-retail facades have no physical-input
            # witness; preserve their immediate transition behavior.
            return False

        player_id = int(player.id)
        jetpack_id = int(getattr(player, "jetpack_id", 0))
        activation_held = bool(
            getattr(input_state, "hover", False)
            if jetpack_id == 69
            else getattr(input_state, "jump", False)
        )
        if activation_held or bool(getattr(player, "airborne", False)):
            self._jetpack_owner_release_settle_deadline.pop(player_id, None)
            return True

        received = int(getattr(player, "_input_receive_sequence", 0))
        settle_deadline = self._jetpack_owner_release_settle_deadline.get(
            player_id
        )
        if settle_deadline is None:
            settle_frames = max(1, min(120, int(getattr(
                getattr(self.server, "config", None),
                "jetpack_owner_handoff_input_frames",
                30,
            ))))
            settle_deadline = received + settle_frames
            self._jetpack_owner_release_settle_deadline[player_id] = (
                settle_deadline
            )
        return received < settle_deadline

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
            data = self._with_local_owner_overrides(
                data,
                self._player_tool_offsets(data),
                player.id,
            )
            # Unlike ordinary 30 Hz snapshots, this rare state transition is
            # reliable so packet loss cannot leave effects/flight stuck until
            # a later cadence row. Physics phase remains an estimate; ENet
            # delivery is not a GameScene application ACK, so the bounded
            # owner-row handoff below protects the asynchronous interval.
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
        self._last_self_row_loop[player.id] = self.server.loop_count
        self._last_advertised_jetpack_active[player.id] = bool(
            getattr(player, "jetpack_active", False)
        )
        snapshot = getattr(player, "world_update_snapshot", None)
        if callable(snapshot):
            # Retail Character caches the local row as network_position and
            # restores that exact XYZ on jump_this_frame after native physics.
            # Update only after Connection.send queued this owner packet; a
            # merely built or excluded snapshot was never visible to retail.
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
            self._begin_jetpack_owner_handoff(player)
            note_transition = getattr(
                player, "note_jetpack_transition_sent", None
            )
            if callable(note_transition):
                note_transition(
                    bool(getattr(player, "jetpack_active", False)),
                    int(stamp),
                )

    def _begin_jetpack_owner_handoff(self, player) -> None:
        """Start a bounded no-correction window after a jetpack state row.

        The reliable WorldUpdate is queued before this method runs, but neither
        ENet delivery nor subsequent ClientData proves that retail GameScene
        has applied it.  During the short window, this owner's ordinary row is
        excluded so an old prediction phase cannot pull the camera backward.
        The same authoritative player row continues to reach every observer.
        """
        config = getattr(self.server, "config", None)
        target_active = bool(getattr(player, "jetpack_active", False))
        if target_active:
            frames = max(0, min(120, int(getattr(
                config,
                "jetpack_owner_handoff_input_frames",
                30,
            ))))
        else:
            frames = max(0, min(1200, int(getattr(
                config,
                "jetpack_owner_release_handoff_input_frames",
                600,
            ))))
        player_id = int(player.id)
        if frames == 0:
            self._jetpack_owner_handoff_deadline.pop(player_id, None)
            self._jetpack_owner_handoff_target.pop(player_id, None)
            self._jetpack_owner_release_settle_deadline.pop(player_id, None)
            return
        received = int(getattr(player, "_input_receive_sequence", 0))
        self._jetpack_owner_handoff_deadline[player_id] = received + frames
        self._jetpack_owner_handoff_target[player_id] = target_active
        self._jetpack_owner_release_settle_deadline.pop(player_id, None)

    def _jetpack_owner_handoff_active(self, player) -> bool:
        """Return whether ordinary local position rows remain suppressed.

        Progress is measured in accepted ClientData frames instead of wall
        time, so a stalled client cannot make the server guess that GameScene
        advanced.  An active pack keeps the owner row suppressed for the
        finite fuel burn: retail and the server have independent native frame
        clocks, so resuming at an arbitrary input count causes a mid-flight
        correction.  Observers still receive the authoritative row. Release
        uses the bounded settle/deadline path below, and lifecycle cleanup
        removes reused player ids.
        """
        player_id = int(player.id)
        deadline = self._jetpack_owner_handoff_deadline.get(player_id)
        if deadline is None:
            return False
        target_active = self._jetpack_owner_handoff_target.get(player_id)
        received = int(getattr(player, "_input_receive_sequence", 0))
        if target_active is True and bool(
            getattr(player, "jetpack_active", False)
        ):
            return True
        if (
            target_active is True
            and not bool(getattr(player, "jetpack_active", False))
            and self._defer_jetpack_release_transition(player)
        ):
            return True
        if received >= deadline:
            self._clear_jetpack_owner_handoff(player_id)
            return False
        if target_active is False:
            input_state = getattr(player, "input", None)
            if input_state is not None:
                jetpack_id = int(getattr(player, "jetpack_id", 0))
                activation_held = bool(
                    getattr(input_state, "hover", False)
                    if jetpack_id == 69
                    else getattr(input_state, "jump", False)
                )
                if activation_held:
                    # Fuel exhaustion while SPACE remains held can make the
                    # two native worlds touch ground and relaunch on different
                    # frames. A server-side ground contact is therefore not a
                    # safe owner-row release witness.
                    self._jetpack_owner_release_settle_deadline.pop(
                        player_id, None
                    )
                    return True
                settle_deadline = (
                    self._jetpack_owner_release_settle_deadline.get(player_id)
                )
                if settle_deadline is None:
                    settle_frames = max(1, min(120, int(getattr(
                        getattr(self.server, "config", None),
                        "jetpack_owner_handoff_input_frames",
                        30,
                    ))))
                    settle_deadline = received + settle_frames
                    self._jetpack_owner_release_settle_deadline[player_id] = (
                        settle_deadline
                    )
                if (
                    received < settle_deadline
                    or bool(getattr(player, "airborne", False))
                ):
                    return True
                self._clear_jetpack_owner_handoff(player_id)
                return False
        if received < deadline:
            return True
        self._clear_jetpack_owner_handoff(player_id)
        return False

    def _clear_jetpack_owner_handoff(self, player_id: int) -> None:
        """Remove all bounded handoff state for one connected owner."""
        self._jetpack_owner_handoff_deadline.pop(player_id, None)
        self._jetpack_owner_handoff_target.pop(player_id, None)
        self._jetpack_owner_release_settle_deadline.pop(player_id, None)

    def _should_send_self_row(self, player_id: int, interval: int) -> bool:
        """Return whether the local correction anchor needs a refresh now."""
        last_loop = self._last_self_row_loop.get(player_id)
        if last_loop is None:
            return True
        return (self.server.loop_count - last_loop) >= interval

    def _landing_owner_handoff_active(self, player) -> bool:
        """Suppress one early owner row at the airborne-to-ground boundary.

        Foreground Python 2 captures show the authoritative body can report
        grounded/zero vertical velocity one recurrence before the owner's
        movement history lands. Sending that inseparable position row starts
        a visible correction exactly as the camera touches terrain. Observers
        still receive the authoritative row; only the owner's correction row
        waits for a tiny accepted-input handoff.
        """
        player_id = int(player.id)
        airborne = bool(getattr(player, "airborne", False))
        was_airborne = self._last_owner_airborne.get(player_id, airborne)
        self._last_owner_airborne[player_id] = airborne
        received = int(getattr(player, "_input_receive_sequence", 0))

        if was_airborne and not airborne:
            settle_frames = max(0, min(6, int(getattr(
                getattr(self.server, "config", None),
                "landing_owner_handoff_input_frames",
                2,
            ))))
            if settle_frames > 0:
                self._landing_owner_handoff_deadline[player_id] = (
                    received + settle_frames
                )

        deadline = self._landing_owner_handoff_deadline.get(player_id)
        if deadline is None:
            return False
        if received < deadline:
            return True
        self._landing_owner_handoff_deadline.pop(player_id, None)
        return False

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

        for player_id, player in server.players.items():
            if player_id == exclude_player_id:
                continue
            if not player.alive or not player.spawned:
                continue
            snapshot = player.world_update_snapshot()
            if player_id == local_player_id:
                snapshot = snapshot[:9] + (0xFF,) + snapshot[10:]
            world_update[player_id] = snapshot

        world_update.updated_entities = list(server.entities.values())
        world_update.rocket_turrets = [
            turret.world_update() for turret in server.rocket_turrets.values()
        ]
        return world_update

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
