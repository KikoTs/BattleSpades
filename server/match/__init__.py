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


def _carries_over_scene(connection) -> bool:
    """Whether a peer can take the in-place MapEnded -> reload handshake.

    True for in-game peers and for peers whose loader handshake finished
    (MapSync delivered and StateData sent) but who have not produced
    ClientData yet: team select, class select, or a join in flight. False only
    for a peer still inside InitialInfo/MapSync, whose own handshake
    coroutine would otherwise interleave with the replacement one.
    """

    if bool(getattr(connection, "in_game", False)):
        return True
    return bool(getattr(connection, "map_sent", False)) and bool(
        getattr(connection, "state_sent", False)
    )


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
        requested = str(mode_name).strip().lower()
        normalized = self._canonical_mode(requested)
        if normalized is None:
            return TransitionResult(False, f"Unknown mode: {requested}")
        if self._is_current_mode(normalized):
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
                # Queued leaves may still decide the old round; the retiring
                # flag keeps them from opening a vote or an end sequence
                # underneath the restart (on_mode_start clears it).
                self._begin_mode_retirement(mode, end_round=False)
                await self._drain_leave_events(mode)
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

        return await self._change_map(map_name, end_screen_seconds=None)

    async def change_map_after_end_screen(
        self,
        map_name: str,
        *,
        end_screen_seconds: float,
        headline_message_id: int | None = None,
    ) -> TransitionResult:
        """Preflight a voted map, show scores, then commit the rollover.

        Packet 53 is emitted only after the VXL candidate exists. This avoids
        stranding clients in the terminal statistics overlay when a stale or
        invalid vote target fails validation. The later packet-52 boundary and
        loader handshake remain owned by :meth:`_rollover`.
        """

        return await self._change_map(
            map_name,
            end_screen_seconds=end_screen_seconds,
            headline_message_id=headline_message_id,
        )

    async def _change_map(
        self,
        map_name: str,
        *,
        end_screen_seconds: float | None,
        headline_message_id: int | None = None,
    ) -> TransitionResult:
        """Prepare one map and optionally hold the native statistics screen."""

        if self._transition_busy(allow_current_request=True):
            return TransitionResult(False, "Another match transition is already in progress")
        normalized = self._normalize_map_name(map_name)
        if not normalized:
            return TransitionResult(False, "Invalid or empty map name")
        if self._is_current_map(normalized):
            return TransitionResult(True, f"Map {normalized} is already loaded")
        current_mode = str(self.server.config.default_mode).strip().lower()
        mode_name = self._canonical_mode(current_mode) or current_mode
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
            if end_screen_seconds is not None:
                from server.builders.initial_info import supports_game_stats_screen
                from server.scoreboard import show_game_stats

                dwell = min(120.0, max(0.0, float(end_screen_seconds)))
                if supports_game_stats_screen(self.server):
                    # The round end holds ViewScores with ForceShowScores(1),
                    # which locks the menu (manager.locked_to_scene): live
                    # 2026-09-26, packet 53 then left the plain scoreboard up
                    # and the stats screen, its headline (73) and the
                    # client's own end-screen music never appeared. Release
                    # the hold first so show_game_statistics can switch to
                    # ViewGameStats.
                    from server.audio import al_error_flush_bytes
                    from server.scoreboard import force_show_scores

                    # ViewGameStats starts its own menu music stream; clear a
                    # stale client OpenAL error first (server/audio.py).
                    self.server.broadcast(al_error_flush_bytes())
                    force_show_scores(self.server, False)
                    # IDA: packet 53 calls GameScene.show_game_statistics(False).
                    # It is a terminal overlay for this scene, but the scene
                    # remains available to receive packet 52 after the dwell.
                    show_game_stats(self.server)
                    if headline_message_id is not None:
                        # IDA: GameScene.show_text_message only sets the
                        # message when the active menu is ViewGameStats,
                        # so the retail headline must follow packet 53.
                        from server.scoreboard import send_show_text_message

                        send_show_text_message(
                            self.server, int(headline_message_id), dwell
                        )
                    host = getattr(self.server, "host", None)
                    if host is not None:
                        host.flush()
                else:
                    logger.info(
                        "Skipping ShowGameStats for %s: no stock level screenshot",
                        self.server.config.default_map,
                    )
                if dwell > 0.0:
                    await asyncio.sleep(dwell)
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
        requested = str(mode_name).strip().lower()
        normalized = self._canonical_mode(requested)
        if normalized is None:
            return TransitionResult(False, f"Unknown mode: {requested}")
        if self._is_current_mode(normalized):
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
            all_connections = tuple(server.connections.values())
            # Two kinds of peer are carried into the new map over their
            # retained ENet peer (MapEnded -> ClientInMenu ack -> InitialInfo
            # -> MapSync -> StateData):
            #   * in-game peers (first ClientData seen), and
            #   * pre-game peers whose loader handshake already COMPLETED
            #     (StateData sent) — they sit in the GameScene on team /
            #     class select without ClientData yet. Before 2026-09-26 these
            #     were kicked with ERROR_MATCH_ENDED on every rollover, so a
            #     player choosing a team during a round end never came back.
            # Only a peer still inside InitialInfo/MapSync is retired: starting
            # reload_scene on it would cancel its waiter while its original
            # coroutine can still emit old VXL chunks, splicing two map epochs.
            connections = tuple(
                connection
                for connection in all_connections
                if _carries_over_scene(connection)
            )
            loading_connections = tuple(
                connection
                for connection in all_connections
                if not _carries_over_scene(connection)
            )
            pregame_connections = tuple(
                connection
                for connection in connections
                if not bool(getattr(connection, "in_game", False))
            )
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

                # Arm readiness before MapEnded so a fast client cannot race
                # its acknowledgement ahead of the server-side waiter.
                for connection in connections:
                    connection.arm_scene_transition()

                # MapEnded(52) freezes the compiled GameScene. BattleSpades'
                # maintained client opens LoadingMenu on the same GameClient
                # and acknowledges that state with ClientInMenu(110).
                map_ended = bytes(MapEnded().generate())
                # The loader swaps the menu music and retires the in-game
                # stream right after packet 52. With an OpenAL error pending
                # the stock client orphans that stream in ALURE and freezes
                # on its next sound (live 2026-09-26, START after rollover).
                # A silent one-shot processed just before 52 clears the
                # error; client_patches/session_transition_patch.py closes
                # the hole on patched clients.
                from server.audio import al_error_flush_bytes

                flush = al_error_flush_bytes()
                server.broadcast(flush)
                server.broadcast(map_ended)
                # broadcast() is gameplay-gated on in_game; a peer on team /
                # class select has a live GameScene too and must see the
                # same freeze to open its loader and acknowledge.
                for connection in pregame_connections:
                    connection.send(flush)
                    connection.send(map_ended)
                host = getattr(server, "host", None)
                if host is not None:
                    host.flush()

                # This is the crash boundary.  Detach the old Player objects
                # immediately so late movement packets cannot be queued against
                # the retired map while the client changes scenes.
                for connection in all_connections:
                    connection.in_game = False
                for connection in loading_connections:
                    connection.disconnect(
                        reason=int(DISCONNECT.ERROR_MATCH_ENDED)
                    )
                if old_mode is not None:
                    await self._cancel_mode_end(old_mode)
                    # Bot retirement and roster detach run the old mode's
                    # leave hooks. A retiring mode must not finish a round
                    # (Zombie/VIP/Arena elimination), open a map vote that
                    # leaks into the new map, or start its end sequence.
                    self._begin_mode_retirement(old_mode, end_round=True)
                bots = getattr(server, "bots", None)
                prepare_bots = getattr(bots, "prepare_for_game_transition", None)
                if callable(prepare_bots):
                    await prepare_bots()
                if old_mode is not None:
                    await self._drain_leave_events(old_mode)
                for connection in all_connections:
                    await self._detach_transition_player(connection, old_mode)
                if old_mode is not None:
                    deactivate = getattr(old_mode, "deactivate", None)
                    if callable(deactivate):
                        await deactivate()
                # Defense in depth: nothing the old mode did while retiring
                # may carry a ballot or a staged next map into the new one.
                self._reset_vote_state()
                self._discard_old_timeline_work()
                ready_timeout = min(
                    5.0,
                    max(
                        0.25,
                        float(
                            getattr(
                                server.config,
                                "transition_grace_seconds",
                                1.25,
                            )
                        ),
                    ),
                )
                readiness = await asyncio.gather(
                    *(
                        connection.wait_for_scene_transition(ready_timeout)
                        for connection in connections
                    ),
                    return_exceptions=True,
                )
                ready_connections = []
                failed_connections = list(loading_connections)
                for connection, outcome in zip(connections, readiness):
                    if outcome is True:
                        ready_connections.append(connection)
                        continue
                    failed_connections.append(connection)
                    logger.warning(
                        "client did not acknowledge transition loader at %s; "
                        "withholding InitialInfo",
                        getattr(
                            getattr(connection, "peer", None),
                            "address",
                            "unknown",
                        ),
                    )
                    connection.disconnect(
                        reason=int(DISCONNECT.ERROR_MATCH_ENDED)
                    )

                server.reset_round_runtime()
                repair = getattr(server, "terrain_repair", None)
                if repair is not None:
                    repair.reset()
                self._reset_map_journal()
                # New map, new match: admin team rules (81/82) and billboards
                # do not carry over.
                from server import hud_packets

                hud_packets.reset_state(server)

                # Map-vote history: the map being left counts as played even
                # when an admin (not a ballot) chose the next one.
                note_played = getattr(
                    getattr(server, "vote_manager", None), "note_map_played", None
                )
                if callable(note_played):
                    note_played(old_map)
                server.config.default_map = map_name
                server.config.default_mode = mode_name
                if candidate_world is not None:
                    candidate_world.config = server.config
                    server.world_manager = candidate_world
                    bind_journal = getattr(
                        server, "_bind_world_mutation_journal", None
                    )
                    if callable(bind_journal):
                        bind_journal()
                # An admin fog command belongs to the retired map epoch. The
                # replacement StateData must use its own authored atmosphere.
                server.fog_color_override = None

                for team in server.teams.values():
                    team.reset()

                server.mode = mode_class(server)
                await server.mode.on_mode_start()

                # Rejoin peerless bots with fresh scores and personalities
                # before the replacement roster is streamed to any client.
                bots = getattr(server, "bots", None)
                rebind_bots = getattr(
                    bots, "rebind_after_match_transition", None
                )
                if callable(rebind_bots):
                    result = rebind_bots()
                    if result is not None:
                        await result

                # Each acknowledged peer now receives the normal initial-join
                # loader ordering. MapDataValidation is the second-phase proof
                # that InitialInfo was parsed and the advertised VXL can be
                # synchronized; unacknowledged clients never reach this line.
                reloads = await asyncio.gather(
                    *(
                        connection.reload_scene()
                        for connection in ready_connections
                    ),
                    return_exceptions=True,
                )
                for connection, outcome in zip(ready_connections, reloads):
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
                bind_journal = getattr(
                    server, "_bind_world_mutation_journal", None
                )
                if callable(bind_journal):
                    bind_journal()
                server.fog_color_override = old_fog_override
                failed_mode = server.mode
                server.mode = old_mode
                for connection in all_connections:
                    connection.in_game = False
                    try:
                        connection.disconnect(reason=int(DISCONNECT.ERROR_DATA))
                    except Exception:
                        logger.debug("failed to retire transition client", exc_info=True)
                await self._revive_rolled_back_mode(failed_mode, old_mode)
                return TransitionResult(
                    False,
                    "Session change failed safely; reconnect after checking server log",
                    reconnect_required=bool(all_connections),
                )
            finally:
                self.in_progress = False

    async def _revive_rolled_back_mode(self, failed_mode, old_mode) -> None:
        """Restart the restored mode after a failed rollover.

        The old mode was already deactivated (``ended``) and the bots retired
        by the time most failures can happen. Restoring ``server.mode``
        alone left a dead round forever: no clock, no win checks, no bots.
        Every step is individually guarded; this runs inside an ``except``.
        """

        server = self.server
        if failed_mode is not None and failed_mode is not old_mode:
            deactivate = getattr(failed_mode, "deactivate", None)
            if callable(deactivate):
                try:
                    await deactivate()
                except Exception:
                    logger.exception("failed to retire the aborted mode")
        self._discard_old_timeline_work()
        if old_mode is not None:
            try:
                for team in server.teams.values():
                    team.reset()
                await old_mode.on_mode_start()
            except Exception:
                logger.exception("failed to restart the restored mode")
        bots = getattr(server, "bots", None)
        rebind_bots = getattr(bots, "rebind_after_match_transition", None)
        if callable(rebind_bots):
            try:
                result = rebind_bots()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception("failed to rebind bots after rollback")

    async def _drain_leave_events(self, mode) -> None:
        """Run queued ``on_player_leave`` events before the queue is dropped.

        Leave hooks release mode ownership (VIP, intel, bomb, diamond). A
        restart that simply cleared ``_mode_events`` could lose one and keep
        a departed id as a carrier or VIP into the next round.
        """

        queue = getattr(self.server, "_mode_events", None)
        if not queue:
            return
        leaves = [args for name, args in list(queue) if name == "on_player_leave"]
        if not leaves:
            return
        kept = [item for item in list(queue) if item[0] != "on_player_leave"]
        queue.clear()
        queue.extend(kept)
        plugins = getattr(self.server, "plugin_manager", None)
        for args in leaves:
            handler = getattr(mode, "on_player_leave", None)
            try:
                if callable(handler):
                    result = handler(*args)
                    if inspect.isawaitable(result):
                        await result
                call_event = getattr(plugins, "call_event", None)
                if callable(call_event):
                    result = call_event("on_player_leave", *args)
                    if inspect.isawaitable(result):
                        await result
            except Exception:
                logger.exception("queued on_player_leave failed during transition")

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

    @staticmethod
    def _begin_mode_retirement(mode, *, end_round: bool) -> None:
        """Flag ``mode`` as being replaced before its roster is detached."""

        begin = getattr(mode, "begin_retirement", None)
        if callable(begin):
            begin(end_round=end_round)
            return
        try:
            mode.retiring = True
            if end_round:
                mode.ended = True
        except AttributeError:
            pass

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
        clear_kicks = getattr(vote_manager, "clear_match_kicks", None)
        if callable(clear_kicks):
            clear_kicks()

    def _reset_map_journal(self) -> None:
        """Forget terrain replay packets belonging to a replaced VXL."""

        journal = getattr(self.server, "_map_mutation_journal", None)
        if journal is not None:
            journal.clear()
        cell_journal = getattr(
            self.server, "_map_cell_journal", None
        )
        if cell_journal is not None:
            cell_journal.clear()
        self.server._map_mutation_sequence = 0
        self.server._map_cell_sequence = 0
        for connection in self.server.connections.values():
            connection.map_mutation_watermark = None
            connection.map_mutation_overflow = False
            connection.map_cell_watermark = None
            connection.map_cell_overflow = False
            connection.map_cell_replay = None

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
    def _canonical_mode(mode_name: str) -> str | None:
        """Collapse an admin alias ("occupation") to its registry short code.

        Map metadata, mode_data and ``[modes.*]`` overlays key on the short
        code, so storing the alias verbatim silently dropped mode-tagged map
        objectives (occupation base, diamond bases) after ``/mode``.
        """

        from modes import canonical_mode_code

        return canonical_mode_code(mode_name)

    def _is_current_mode(self, mode_code: str) -> bool:
        """Alias-aware "already active" check."""

        current = str(self.server.config.default_mode).strip().lower()
        return (self._canonical_mode(current) or current) == mode_code

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
