"""Retail GenericVote ballots for kick and next-map selection.

Wire protocol recovered from ``GameScene`` and ``shared.packet``:

* Client -> server ``InitiateKickMessage(48)`` starts/cancels a kick vote.
* Server -> clients ``GenericVoteMessage(47)`` opens and updates the stock
  overlay. The shipped client binds its first three candidates to F1/F2/F3.
* Client -> server ``GenericVoteMessage(47)`` with ``message_type=CAST``
  returns the selected candidate record.

Voting only selects the next map. The round lifecycle consumes that selection
at a safe scene boundary; a packet handler never swaps the authoritative VXL.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import time

from shared.packet import GenericVoteMessage


logger = logging.getLogger(__name__)

VOTE_START = 0
VOTE_CAST = 1
VOTE_UPDATE = 2
VOTE_CLOSED = 3

KICK_GRIEFING, KICK_HACKING, KICK_ABUSE, KICK_CANCEL = range(4)

VOTE_DURATION = 30.0
MAP_VOTE_DURATION = 15.0
MAP_VOTE_LEAD_SECONDS = 60.0
VOTE_COOLDOWN = 60.0


def _retail_localised_text(identifier: str) -> str:
    """Encode one string for ``GenericVotingHUD.decode_string``.

    The retail HUD literal-evaluates the field and unconditionally reads both
    tuple indexes: ``value[0]`` is the string-table identifier and ``value[1]``
    is an iterable of format arguments.  A one-item tuple reaches native line
    67 and raises ``IndexError``, terminating the client.  Empty text therefore
    still needs an explicit empty argument tuple.
    """

    return repr((str(identifier), ()))


class VoteManager:
    """Own at most one bounded retail vote overlay at a time."""

    def __init__(self, server) -> None:
        self.server = server
        self.active = False
        self.kind: str | None = None
        self.target_id: int | None = None
        self.starter_id: int | None = None
        # Compatibility views retained for callers and operational tests.
        self.yes: set[int] = set()
        self.no: set[int] = set()
        self.candidates: tuple[str, ...] = ()
        self.votes: dict[int, int] = {}
        self.next_map: str | None = None
        self.opened_at = 0.0
        self._map_result_event = asyncio.Event()
        self._last_start: dict[int, float] = {}
        # Map discovery is startup work. Never glob the filesystem from the
        # 60 Hz mode tick when the final-minute vote is opened.
        self._available_maps = self._discover_maps()

    def _discover_maps(self) -> tuple[str, ...]:
        """Return the deterministic map catalog captured at server startup."""

        config = getattr(self.server, "config", None)
        maps_root = Path(getattr(config, "maps_path", "maps"))
        discovered = tuple(
            sorted(
                (
                    path.stem
                    for path in maps_root.glob("*.vxl")
                    if path.is_file()
                ),
                key=str.casefold,
            )
        )
        requested = tuple(getattr(config, "map_rotation", ()) or ())
        if not requested:
            return discovered
        by_name = {name.casefold(): name for name in discovered}
        result = tuple(
            by_name[str(name).casefold()]
            for name in requested
            if str(name).casefold() in by_name
        )
        missing = [name for name in requested if str(name).casefold() not in by_name]
        if missing:
            logger.warning("Ignoring unavailable lobby maps: %s", ", ".join(missing))
        return result

    def _eligible_count(self) -> int:
        return sum(
            1
            for connection in self.server.connections.values()
            if connection.in_game
        )

    def _mode_available_maps(self) -> tuple[str, ...]:
        """Return the cached operator catalog narrowed by a stock playlist.

        An explicit ``lobby.map_rotation`` always wins.  With an empty
        rotation, modes may publish their recovered retail ``stock_maps``;
        this keeps Classic CTF votes on its seven purpose-built layouts while
        retaining custom-map support for operators who request it.
        """

        available = self._available_maps
        config = getattr(self.server, "config", None)
        if tuple(getattr(config, "map_rotation", ()) or ()):
            return available
        playlist = tuple(
            getattr(getattr(self.server, "mode", None), "stock_maps", ()) or ()
        )
        if not playlist:
            return available
        by_name = {name.casefold(): name for name in available}
        filtered = tuple(
            by_name[name.casefold()]
            for name in playlist
            if name.casefold() in by_name
        )
        # A partial release bundle must still offer a vote instead of wedging
        # the end sequence when none of a playlist's maps were installed.
        return filtered or available

    def _needed(self) -> int:
        # Kick targets are not eligible, so a majority of the remaining
        # in-game population is sufficient.
        eligible = max(1, self._eligible_count() - 1)
        config = getattr(self.server, "config", None)
        ratio = float(
            getattr(getattr(config, "game_rules", None), "get", lambda _key: 0.5)(
                "RULE_VOTES_REQUIRED_FOR_KICK"
            )
        )
        import math
        return max(1, int(math.ceil(eligible * ratio)))

    def start_kick(self, starter, target, reason: int, now: float) -> bool:
        """Open a majority kick ballot if identity and cooldown are valid."""

        if self.active or target is None or target.id == starter.id:
            return False
        last = self._last_start.get(int(starter.id), -1e9)
        if float(now) - last < VOTE_COOLDOWN:
            return False

        self.active = True
        self.kind = "kick"
        self.target_id = int(target.id)
        self.starter_id = int(starter.id)
        self.candidates = ("Kick {}".format(target.name), "Keep")
        self.votes = {int(starter.id): 0}
        self.yes = {int(starter.id)}
        self.no = set()
        self.opened_at = float(now)
        self._last_start[int(starter.id)] = float(now)
        self._broadcast(VOTE_START, target)
        logger.info(
            "VOTE-KICK started by %s against %s (reason %d)",
            starter.name,
            target.name,
            reason,
        )
        return True

    def start_map_vote(self, candidates, now: float) -> bool:
        """Open the stock F1/F2/F3 next-map ballot."""

        if self.active:
            return False
        normalized: list[str] = []
        seen: set[str] = set()
        available = {
            value.casefold(): value for value in self._available_maps
        }
        for raw in candidates:
            value = str(raw).strip()
            path = Path(value)
            if not value or path.name != value:
                continue
            value = path.stem if path.suffix.lower() == ".vxl" else value
            key = value.casefold()
            # Internal callers use the startup catalog. Keep the direct API
            # useful for map-less unit/plugin servers, but when a real catalog
            # exists never advertise a target that cannot pass map preflight.
            if available:
                value = available.get(key, "")
                if not value:
                    continue
                key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(value)
            if len(normalized) == 3:
                break
        if not normalized:
            return False

        self.active = True
        self.kind = "map"
        self.target_id = None
        self.starter_id = None
        self.candidates = tuple(normalized)
        self.votes = {}
        self.yes = set()
        self.no = set()
        self.next_map = None
        self.opened_at = float(now)
        self._map_result_event = asyncio.Event()
        self._broadcast(VOTE_START, None)
        logger.info("MAP VOTE opened: %s", ", ".join(self.candidates))
        return True

    def ensure_map_vote(self, now: float) -> bool:
        """Present up to three cached deterministic map candidates."""

        if self.active or self.next_map is not None:
            return False
        available = list(self._mode_available_maps())
        current = str(
            getattr(self.server.config, "default_map", "")
        ).casefold()
        current_index = next(
            (
                index
                for index, name in enumerate(available)
                if name.casefold() == current
            ),
            -1,
        )
        if current_index >= 0:
            ordered = available[current_index + 1 :] + available[:current_index]
        else:
            ordered = available
        choices = [name for name in ordered if name.casefold() != current]
        return self.start_map_vote(choices[:3], now) if choices else False

    def ensure_round_end_map_vote(self, now: float) -> bool:
        """Guarantee that the round boundary owns the retail vote overlay.

        A kick ballot is useful during play but must not consume the complete
        end-of-round voting window. Closing it before opening the map ballot
        also prevents its delayed timeout from mutating a replacement scene.
        An already-running map ballot or a staged winner is preserved.
        """

        if self.active and self.kind == "map":
            return False
        if self.active:
            self.cancel()
        return self.ensure_map_vote(now)

    async def wait_for_map_result(self) -> str | None:
        """Wait non-blockingly for votes or the bounded map-vote deadline.

        The simulation tick normally resolves the timeout. This waiter owns a
        second deterministic timeout so a paused/slow scheduler cannot let the
        end sequence consume an unresolved ballot and restart the wrong map.
        """

        if not self.active or self.kind != "map":
            return self.next_map
        remaining = max(
            0.0,
            MAP_VOTE_DURATION - (time.time() - float(self.opened_at)),
        )
        if remaining > 0.0:
            try:
                await asyncio.wait_for(
                    self._map_result_event.wait(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                pass
        if self.active and self.kind == "map":
            self._resolve_map()
        return self.next_map

    def reveal_to(self, connection) -> None:
        """Open the current ballot for a client that just entered GameScene."""

        if not self.active:
            return
        send = getattr(connection, "send", None)
        if callable(send):
            send(bytes(self._build_packet(VOTE_START).generate()), reliable=True)

    def cast(self, voter, yes: bool) -> None:
        """Compatibility API for a yes/no kick choice."""

        self._cast_index(voter, 0 if yes else 1)

    def cast_candidate(self, voter, candidate) -> None:
        """Record the exact candidate returned by the retail vote packet."""

        if not self.active:
            return
        if isinstance(candidate, int):
            index = int(candidate)
        else:
            name = str(candidate).strip().casefold()
            index = next(
                (
                    position
                    for position, value in enumerate(self.candidates)
                    if value.casefold() == name
                ),
                -1,
            )
        self._cast_index(voter, index)

    def _cast_index(self, voter, index: int) -> None:
        player_id = int(voter.id)
        if (
            not self.active
            or not 0 <= int(index) < len(self.candidates)
            or (self.kind == "kick" and player_id == self.target_id)
        ):
            return
        self.votes[player_id] = int(index)
        self.yes.discard(player_id)
        self.no.discard(player_id)
        if self.kind == "kick":
            (self.yes if int(index) == 0 else self.no).add(player_id)

        target = self.server.players.get(self.target_id)
        self._broadcast(VOTE_UPDATE, target)
        if self.kind == "kick" and len(self.yes) >= self._needed():
            self._resolve_kick(passed=True)
        elif self.kind == "map" and len(self.votes) >= self._eligible_count():
            self._resolve_map()

    def tick(self, now: float) -> None:
        """Resolve an expired ballot without blocking the simulation tick."""

        if not self.active:
            return
        duration = MAP_VOTE_DURATION if self.kind == "map" else VOTE_DURATION
        if float(now) - self.opened_at < duration:
            return
        if self.kind == "map":
            self._resolve_map()
        else:
            self._resolve_kick(passed=len(self.yes) >= self._needed())

    def cancel(self) -> None:
        if not self.active:
            return
        if self.kind == "kick":
            self._resolve_kick(passed=False)
        else:
            self._broadcast(VOTE_CLOSED, None)
            self._clear_active()
            self._map_result_event.set()

    def forget_player(self, player_id: int) -> None:
        """Remove vote state before a compact player id is reassigned."""

        player_id = int(player_id)
        self._last_start.pop(player_id, None)
        if self.active and player_id in (self.target_id, self.starter_id):
            self.cancel()
            return
        removed = self.votes.pop(player_id, None) is not None
        removed = player_id in self.yes or player_id in self.no or removed
        self.yes.discard(player_id)
        self.no.discard(player_id)
        if removed and self.active:
            self._broadcast(
                VOTE_UPDATE,
                self.server.players.get(self.target_id),
            )

    def consume_next_map(self) -> str | None:
        """Return and clear the map chosen for the next round boundary."""

        result = self.next_map
        self.next_map = None
        return result

    def _resolve_kick(self, passed: bool) -> None:
        target = self.server.players.get(self.target_id)
        self._broadcast(VOTE_CLOSED, target)
        name = target.name if target is not None else "?"
        if passed and target is not None:
            logger.info("VOTE-KICK PASSED - kicking %s", name)
            try:
                target.disconnect(reason=2)
            except Exception:
                logger.debug("vote-kick disconnect failed", exc_info=True)
        else:
            logger.info(
                "VOTE-KICK failed against %s (%d yes / %d no)",
                name,
                len(self.yes),
                len(self.no),
            )
        self._clear_active()

    def _resolve_map(self) -> None:
        if not self.candidates:
            self._clear_active()
            self._map_result_event.set()
            return
        counts = self._candidate_counts()
        # ``max`` keeps the lowest candidate index on ties. Candidate order is
        # already rotated by map, so the result is stable without RNG state.
        winner_index = max(range(len(counts)), key=lambda index: counts[index])
        self.next_map = self.candidates[winner_index]
        self._broadcast_map_result(self.next_map)
        self._broadcast(VOTE_CLOSED, None)
        logger.info(
            "MAP VOTE selected %s (%s)",
            self.next_map,
            ", ".join(str(value) for value in counts),
        )
        self._clear_active()
        self._map_result_event.set()

    def _clear_active(self) -> None:
        self.active = False
        self.kind = None
        self.target_id = None
        self.starter_id = None
        self.yes = set()
        self.no = set()
        self.candidates = ()
        self.votes = {}

    def _candidate_counts(self) -> list[int]:
        counts = [0] * len(self.candidates)
        for index in self.votes.values():
            if 0 <= int(index) < len(counts):
                counts[int(index)] += 1
        return counts

    def _broadcast_map_result(self, map_name: str) -> None:
        from server.announcements import broadcast_localised_overlay

        broadcast_localised_overlay(
            self.server, "MAP_VOTED_MESSAGE", (map_name,)
        )

    def _broadcast(self, message_type: int, target) -> None:
        self.server.broadcast(bytes(self._build_packet(message_type).generate()))

    def _build_packet(self, message_type: int) -> GenericVoteMessage:
        """Build one literal-safe retail vote packet for broadcast or replay."""

        packet = GenericVoteMessage()
        packet.player_id = (
            int(self.starter_id) if self.starter_id is not None else 255
        )
        packet.message_type = int(message_type)
        counts = self._candidate_counts()
        packet.candidates = [
            {"name": name, "votes": counts[index]}
            for index, name in enumerate(self.candidates)
        ]
        # The client literal-evaluates these fields into (id, arguments).
        # Omitting the empty arguments tuple is a native exception hazard.
        if self.kind == "map":
            packet.title = _retail_localised_text("VOTE_MAP_TITLE")
            packet.description = _retail_localised_text(
                "VOTE_MAP_DESCRIPTION"
            )
        else:
            packet.title = _retail_localised_text("KICK_PLAYER")
            packet.description = _retail_localised_text("KICK_PLAYER")
        packet.allow_revote = 1
        packet.can_vote = int(message_type != VOTE_CLOSED)
        return packet


__all__ = [
    "KICK_ABUSE",
    "KICK_CANCEL",
    "KICK_GRIEFING",
    "KICK_HACKING",
    "MAP_VOTE_DURATION",
    "MAP_VOTE_LEAD_SECONDS",
    "VOTE_CAST",
    "VOTE_CLOSED",
    "VOTE_COOLDOWN",
    "VOTE_DURATION",
    "VOTE_START",
    "VOTE_UPDATE",
    "VoteManager",
]
