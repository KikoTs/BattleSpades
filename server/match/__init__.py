"""Serialized, retail-client-safe match transitions.

The client only consumes map/mode identity while ``LoadingMenu`` constructs a
new ``GameScene``.  Full transitions therefore pause the old scene with packet
52, retain the authenticated ENet peer, and run a fresh loader handshake after
the client enters that menu.  Same-map round restarts continue in-place.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from shared.constants import DISCONNECT
from server.game_constants import CHAT_SYSTEM
from shared.packet import ChatMessage, MapEnded

if TYPE_CHECKING:
    from server.main import BattleSpadesServer


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransitionResult:
    """Outcome returned to an admin command without exposing lifecycle state."""

    ok: bool
    message: str
    reconnect_required: bool = False


class MatchTransitionService:
    """Own atomic round restarts and full map/mode session rollovers."""

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server
        self._lock = asyncio.Lock()
        self.in_progress = False
        self._preparing_map = False
        # Retain fire-and-forget admin map preparation so GC cannot cancel it
        # and a second transition can be rejected while VXL parsing is active.
        self._request_task: asyncio.Task | None = None

    def request_map_change(self, map_name: str, requester=None) -> TransitionResult:
        """Schedule map loading outside the fixed-step packet-drain call.

        Parsing a retail VXL takes roughly 0.6 seconds on the validation host.
        The chat command itself runs inside the simulation tick, so awaiting
        that parse there freezes movement. This method returns immediately;
        the retained task preloads in a worker thread and later commits under
        the transition lock.
        """

        if self._transition_busy():
            return TransitionResult(False, "Another match transition is already in progress")
        normalized = self._normalize_map_name(map_name)
        if not normalized:
            return TransitionResult(False, "Invalid or empty map name")
        if self._is_current_map(normalized):
            return TransitionResult(True, f"Map {normalized} is already loaded")
        self._request_task = asyncio.create_task(
            self._run_map_request(normalized, requester)
        )
        return TransitionResult(True, f"Preparing map {normalized}")

    async def _run_map_request(self, map_name: str, requester):
        """Run one retained map request and report preflight errors privately."""

        task = asyncio.current_task()
        try:
            result = await self.change_map(map_name)
            if not result.ok and requester is not None:
                self._send_private_notice(requester, result.message)
            return result
        finally:
            if self._request_task is task:
                self._request_task = None

    def request_mode_change(self, mode_name: str, requester=None) -> TransitionResult:
        """Schedule a mode rollover without holding the simulation packet drain."""

        if self._transition_busy():
            return TransitionResult(False, "Another match transition is already in progress")
        normalized = str(mode_name).strip().lower()
        if self._resolve_mode_class(normalized) is None:
            return TransitionResult(False, f"Unknown mode: {normalized}")
        if str(self.server.config.default_mode).strip().lower() == normalized:
            return TransitionResult(True, f"Mode {normalized.upper()} is already active")
        self._request_task = asyncio.create_task(
            self._run_mode_request(normalized, requester)
        )
        return TransitionResult(True, f"Preparing mode {normalized.upper()}")

    async def _run_mode_request(self, mode_name: str, requester):
        """Run one retained mode request and report preflight errors privately."""

        task = asyncio.current_task()
        try:
            result = await self.change_mode(mode_name)
            if not result.ok and requester is not None:
                self._send_private_notice(requester, result.message)
            return result
        finally:
            if self._request_task is task:
                self._request_task = None

    async def restart_round(self) -> TransitionResult:
        """Restart the current round without destroying the retail GameScene."""

        if self._transition_busy():
            return TransitionResult(False, "Another match transition is already in progress")
        async with self._lock:
            self.in_progress = True
            try:
                mode = self.server.mode
                if mode is None:
                    return TransitionResult(False, "No active game mode")
                await self._cancel_mode_end(mode)
                self._reset_vote_state()
                self._discard_old_timeline_work()
                await mode._restart_round()
                return TransitionResult(True, "Match restarted")
            except Exception:
                logger.exception("same-map round restart failed")
                return TransitionResult(False, "Match restart failed; see server log")
            finally:
                self.in_progress = False

    async def change_map(self, map_name: str) -> TransitionResult:
        """Preload ``map_name`` and replace the client session if it is valid."""

        if self._transition_busy(allow_current_request=True):
            return TransitionResult(False, "Another match transition is already in progress")
        normalized = self._normalize_map_name(map_name)
        if not normalized:
            return TransitionResult(False, "Invalid or empty map name")
        if self._is_current_map(normalized):
            return TransitionResult(True, f"Map {normalized} is already loaded")
        mode_name = str(self.server.config.default_mode).lower()
        self._preparing_map = True
        try:
            try:
                candidate = await asyncio.to_thread(
                    self._load_world_candidate,
                    normalized,
                    mode_name,
                )
            except (OSError, ValueError) as exc:
                return TransitionResult(False, str(exc))
            except Exception:
                logger.exception("unexpected map preflight failure for %s", normalized)
                return TransitionResult(False, f"Failed to load map: {normalized}")
            return await self._rollover(
                map_name=normalized,
                mode_name=mode_name,
                candidate_world=candidate,
            )
        finally:
            self._preparing_map = False

    async def change_mode(self, mode_name: str) -> TransitionResult:
        """Replace the active mode through a clean client-session boundary."""

        if self._transition_busy(allow_current_request=True):
            return TransitionResult(False, "Another match transition is already in progress")
        normalized = str(mode_name).strip().lower()
        if self._resolve_mode_class(normalized) is None:
            return TransitionResult(False, f"Unknown mode: {normalized}")
        if str(self.server.config.default_mode).strip().lower() == normalized:
            return TransitionResult(True, f"Mode {normalized.upper()} is already active")
        map_name = self._normalize_map_name(self.server.config.default_map)
        self._preparing_map = True
        try:
            try:
                # A mode boundary is also a fresh map epoch. Reusing the old
                # world after clearing its mutation journal would let old
                # construction survive server-side while rejoiners receive the
                # pristine cached VXL. Reloading also re-filters authored map
                # zones/entities for the target mode.
                candidate = await asyncio.to_thread(
                    self._load_world_candidate,
                    map_name,
                    normalized,
                )
            except (OSError, ValueError) as exc:
                return TransitionResult(False, str(exc))
            except Exception:
                logger.exception(
                    "unexpected map preflight failure for mode %s", normalized
                )
                return TransitionResult(
                    False,
                    f"Failed to prepare current map for mode {normalized.upper()}",
                )
            return await self._rollover(
                map_name=map_name,
                mode_name=normalized,
                candidate_world=candidate,
            )
        finally:
            self._preparing_map = False

    async def _rollover(
        self,
        *,
        map_name: str,
        mode_name: str,
        candidate_world,
    ) -> TransitionResult:
        """Commit one full-scene replacement over retained ENet peers."""

        mode_class = self._resolve_mode_class(mode_name)
        if mode_class is None:
            return TransitionResult(False, f"Unknown mode: {mode_name}")

        async with self._lock:
            self.in_progress = True
            server = self.server
            connections = tuple(server.connections.values())
            old_mode = server.mode
            old_world = server.world_manager
            old_map = str(server.config.default_map)
            old_mode_name = str(server.config.default_mode)
            old_fog_override = getattr(server, "fog_color_override", None)
            try:
                self._broadcast_notice(
                    f"Loading {map_name} ({mode_name.upper()})..."
                )
                # Close any old overlay while GameScene can still render the
                # CLOSED packet, and prevent a selected map leaking into the
                # replacement round.
                self._reset_vote_state()

                # MapEnded(52) freezes the compiled GameScene.  BattleSpades'
                # lightweight client compatibility hook responds by opening
                # LoadingMenu on the SAME GameClient; it is not a reconnect or
                # an ENet disconnect packet (verified in gameScene.pyd).
                server.broadcast(bytes(MapEnded().generate()))
                host = getattr(server, "host", None)
                if host is not None:
                    host.flush()

                # This is the crash boundary.  Detach the old Player objects
                # immediately so late movement packets cannot be queued against
                # the retired map while the client changes scenes.
                for connection in connections:
                    connection.in_game = False
                if old_mode is not None:
                    await self._cancel_mode_end(old_mode)
                for connection in connections:
                    await self._detach_transition_player(connection, old_mode)
                if old_mode is not None:
                    deactivate = getattr(old_mode, "deactivate", None)
                    if callable(deactivate):
                        await deactivate()
                self._discard_old_timeline_work()
                grace = min(
                    5.0,
                    max(
                        0.0,
                        float(
                            getattr(
                                server.config,
                                "transition_grace_seconds",
                                1.25,
                            )
                        ),
                    ),
                )
                if grace > 0.0:
                    await asyncio.sleep(grace)

                server.reset_round_runtime()
                repair = getattr(server, "terrain_repair", None)
                if repair is not None:
                    repair.reset()
                self._reset_map_journal()

                server.config.default_map = map_name
                server.config.default_mode = mode_name
                if candidate_world is not None:
                    candidate_world.config = server.config
                    server.world_manager = candidate_world
                # An admin fog command belongs to the retired map epoch. The
                # replacement StateData must use its own authored atmosphere.
                server.fog_color_override = None

                for team in server.teams.values():
                    team.reset()

                server.mode = mode_class(server)
                await server.mode.on_mode_start()

                # Each retained peer now receives the same loader ordering as
                # an initial join.  Requiring MapDataValidation is important:
                # it proves that peer actually entered LoadingMenu.  A clean
                # stock client without the compatibility hook is retired
                # individually instead of receiving VXL bytes in GameScene.
                reloads = await asyncio.gather(
                    *(connection.reload_scene() for connection in connections),
                    return_exceptions=True,
                )
                failed_connections = []
                for connection, outcome in zip(connections, reloads):
                    if outcome is True:
                        continue
                    failed_connections.append(connection)
                    if isinstance(outcome, BaseException):
                        logger.warning(
                            "scene reload failed for %s",
                            getattr(
                                getattr(connection, "peer", None),
                                "address",
                                "unknown",
                            ),
                            exc_info=(type(outcome), outcome, outcome.__traceback__),
                        )
                    else:
                        logger.warning(
                            "scene reload timed out for %s; retiring incompatible peer",
                            getattr(
                                getattr(connection, "peer", None),
                                "address",
                                "unknown",
                            ),
                        )
                    connection.disconnect(reason=int(DISCONNECT.ERROR_MATCH_ENDED))
                if host is not None:
                    host.flush()
                return TransitionResult(
                    True,
                    f"Session changed to {map_name} ({mode_name.upper()})",
                    reconnect_required=bool(failed_connections),
                )
            except Exception:
                logger.exception(
                    "session rollover failed for map=%s mode=%s", map_name, mode_name
                )
                # Once the gate is down, reconnect is safer than re-admitting
                # clients to a partially rebuilt native scene.
                server.config.default_map = old_map
                server.config.default_mode = old_mode_name
                server.world_manager = old_world
                server.fog_color_override = old_fog_override
                server.mode = old_mode
                for connection in connections:
                    connection.in_game = False
                    try:
                        connection.disconnect(reason=int(DISCONNECT.ERROR_DATA))
                    except Exception:
                        logger.debug("failed to retire transition client", exc_info=True)
                return TransitionResult(
                    False,
                    "Session change failed safely; reconnect after checking server log",
                    reconnect_required=bool(connections),
                )
            finally:
                self.in_progress = False

    async def _detach_transition_player(self, connection, old_mode) -> None:
        """Retire one old-scene Player while preserving its network peer.

        Mode ownership is released before deactivation; combat/entity credit,
        team membership, and the global id slot are then removed atomically.
        No ``PlayerLeft`` is broadcast because every human recipient is gated
        and about to receive a complete roster in the new map handshake.
        """
        player = getattr(connection, "player", None)
        if player is None:
            return

        on_leave = getattr(old_mode, "on_player_leave", None)
        if callable(on_leave):
            result = on_leave(player)
            if inspect.isawaitable(result):
                await result

        lifecycle = getattr(self.server, "round_lifecycle", None)
        forget = getattr(lifecycle, "forget_player", None)
        if callable(forget):
            forget(player)
        team = self.server.teams.get(getattr(player, "team", None))
        if team is not None:
            team.remove_player(player)
        player_id = getattr(player, "id", None)
        if self.server.players.get(player_id) is player:
            self.server.players.pop(player_id, None)
        connection.player = None

    async def _cancel_mode_end(self, mode) -> None:
        """Cancel a delayed victory task before another lifecycle mutates state."""

        cancel = getattr(mode, "cancel_end_sequence", None)
        if callable(cancel):
            await cancel()
            return
        task = getattr(mode, "_end_task", None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        mode._end_sequence_running = False

    def _discard_old_timeline_work(self) -> None:
        """Drop inputs and mode callbacks stamped against the prior timeline."""

        for name in ("_pending_ingame_packets", "_mode_events"):
            queue = getattr(self.server, name, None)
            if queue is not None:
                queue.clear()

    def _reset_vote_state(self) -> None:
        """Close the old vote overlay and forget a pending next-map choice."""

        vote_manager = getattr(self.server, "vote_manager", None)
        cancel = getattr(vote_manager, "cancel", None)
        if callable(cancel):
            cancel()
        consume = getattr(vote_manager, "consume_next_map", None)
        if callable(consume):
            consume()

    def _reset_map_journal(self) -> None:
        """Forget terrain replay packets belonging to a replaced VXL."""

        journal = getattr(self.server, "_map_mutation_journal", None)
        if journal is not None:
            journal.clear()
        self.server._map_mutation_sequence = 0
        for connection in self.server.connections.values():
            connection.map_mutation_watermark = None
            connection.map_mutation_overflow = False

    def _load_world_candidate(self, map_name: str, mode_name: str):
        """Load a VXL off to the side so a typo cannot destroy the live world."""

        from server.world_manager import WorldManager

        maps_root = Path(self.server.config.maps_path).resolve()
        filename = map_name if map_name.lower().endswith(".vxl") else f"{map_name}.vxl"
        map_path = (maps_root / filename).resolve()
        try:
            map_path.relative_to(maps_root)
        except ValueError as exc:
            raise ValueError("Map path must stay inside the configured maps directory") from exc
        if not map_path.is_file():
            raise ValueError(f"Map not found: {map_name}")

        candidate_config = copy.copy(self.server.config)
        candidate_config.default_map = map_path.stem
        candidate_config.default_mode = mode_name
        candidate = WorldManager(candidate_config)
        if not candidate.load_map(map_path.stem):
            raise ValueError(f"Failed to load map: {map_name}")
        return candidate

    def _transition_busy(self, *, allow_current_request: bool = False) -> bool:
        """Return whether an admin lifecycle operation already owns the epoch."""

        pending = self._request_task
        pending_busy = bool(pending is not None and not pending.done())
        if allow_current_request and pending is asyncio.current_task():
            pending_busy = False
        return bool(
            self.in_progress
            or self._preparing_map
            or pending_busy
        )

    def _is_current_map(self, map_name: str) -> bool:
        """Compare protocol map stems case-insensitively."""

        current = self._normalize_map_name(self.server.config.default_map)
        return current.casefold() == map_name.casefold()

    @staticmethod
    def _resolve_mode_class(mode_name: str):
        """Resolve a registered mode without retaining a stale class object."""

        from modes import get_mode_class

        return get_mode_class(mode_name)

    @staticmethod
    def _normalize_map_name(map_name: str) -> str:
        """Return the protocol map stem while rejecting path-shaped input."""

        value = str(map_name).strip()
        if not value:
            return ""
        path = Path(value)
        if path.name != value or value in (".", ".."):
            return ""
        return path.stem if path.suffix.lower() == ".vxl" else value

    def _broadcast_notice(self, message: str) -> None:
        """Send the final old-scene packet before gameplay is gated."""

        from server.announcements import broadcast_overlay

        broadcast_overlay(self.server, message)

    @staticmethod
    def _send_private_notice(player, message: str) -> None:
        """Report an asynchronous preflight failure if the admin is connected."""

        send = getattr(player, "send", None)
        if not callable(send):
            return
        packet = ChatMessage()
        packet.player_id = 255
        packet.chat_type = CHAT_SYSTEM
        packet.value = message
        send(bytes(packet.generate()))


__all__ = ["MatchTransitionService", "TransitionResult"]
