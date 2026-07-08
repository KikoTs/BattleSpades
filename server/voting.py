"""Vote-kick system.

Wire protocol (verified against the client's constants + packet layout):
- Client -> Server InitiateKickMessage(48): {player_id, target_id, reason}
  starts a kick vote (reason: 0=GRIEFING, 1=HACKING, 2=ABUSE, 3=CANCEL).
- Server -> Clients GenericVoteMessage(47): opens/updates/closes the vote
  overlay. message_type: 0=START, 1=CAST, 2=UPDATE_VOTE_COUNT, 3=CLOSED.
  candidates = [{'name': 'Yes', 'votes': n}, {'name': 'No', 'votes': n}].
  vote flags byte: bit0 allow_revote, bit2 can_vote.
- Client -> Server GenericVoteMessage(47) type=CAST: the player picks a
  candidate index (carried in message_type/candidate — the client sends
  its choice; we tally Yes/No).

A vote passes when yes-votes exceed half the eligible (in-game) players.
On pass the target is disconnected (kick reason 2). Votes auto-close after
VOTE_DURATION seconds.
"""
from __future__ import annotations

import logging

from shared.packet import GenericVoteMessage

logger = logging.getLogger(__name__)

# GenericVoteMessage.message_type values (constants.py:2759).
VOTE_START = 0
VOTE_CAST = 1
VOTE_UPDATE = 2
VOTE_CLOSED = 3

# Kick reasons (constants.py:2719).
KICK_GRIEFING, KICK_HACKING, KICK_ABUSE, KICK_CANCEL = range(4)

VOTE_DURATION = 30.0        # seconds before an unresolved vote auto-fails
VOTE_COOLDOWN = 60.0        # seconds before the same starter can vote again


class VoteManager:
    """Owns at most one active vote at a time."""

    def __init__(self, server):
        self.server = server
        self.active = False
        self.target_id = None
        self.starter_id = None
        self.yes: set = set()      # player ids that voted yes
        self.no: set = set()
        self.opened_at = 0.0
        self._last_start: dict = {}   # starter_id -> monotonic time

    # -- lifecycle ------------------------------------------------------

    def _eligible_count(self) -> int:
        return sum(1 for c in self.server.connections.values() if c.in_game)

    def _needed(self) -> int:
        # Majority of eligible voters (excluding the target).
        return max(1, (self._eligible_count() - 1) // 2 + 1)

    def start_kick(self, starter, target, reason: int, now: float) -> bool:
        """Open a kick vote. Returns False if one is already running, the
        starter is on cooldown, or the target is invalid."""
        if self.active:
            return False
        if target is None or target.id == starter.id:
            return False
        last = self._last_start.get(starter.id, -1e9)
        if now - last < VOTE_COOLDOWN:
            return False

        self.active = True
        self.target_id = target.id
        self.starter_id = starter.id
        self.yes = {starter.id}      # the initiator implicitly votes yes
        self.no = set()
        self.opened_at = now
        self._last_start[starter.id] = now

        self._broadcast(VOTE_START, target)
        logger.info("VOTE-KICK started by %s against %s (reason %d)",
                    starter.name, target.name, reason)
        return True

    def cast(self, voter, yes: bool) -> None:
        """Record a vote (a player may change their mind — revote allowed)."""
        if not self.active or voter.id == self.target_id:
            return
        self.yes.discard(voter.id)
        self.no.discard(voter.id)
        (self.yes if yes else self.no).add(voter.id)
        target = self.server.players.get(self.target_id)
        self._broadcast(VOTE_UPDATE, target)
        if len(self.yes) >= self._needed():
            self._resolve(passed=True)

    def tick(self, now: float) -> None:
        """Resolve a vote that outlives VOTE_DURATION. It only passes if it
        reached the required yes threshold — a lone initiator's vote never
        kicks someone by default."""
        if self.active and now - self.opened_at >= VOTE_DURATION:
            self._resolve(passed=len(self.yes) >= self._needed())

    def cancel(self) -> None:
        if self.active:
            self._resolve(passed=False)

    # -- internals ------------------------------------------------------

    def _resolve(self, passed: bool) -> None:
        target = self.server.players.get(self.target_id)
        self._broadcast(VOTE_CLOSED, target)
        name = target.name if target is not None else "?"
        if passed and target is not None:
            logger.info("VOTE-KICK PASSED — kicking %s", name)
            try:
                target.disconnect(reason=2)  # DISCONNECT_KICKED
            except Exception:
                logger.debug("vote-kick disconnect failed", exc_info=True)
        else:
            logger.info("VOTE-KICK failed against %s (%d yes / %d no)",
                        name, len(self.yes), len(self.no))
        self.active = False
        self.target_id = None
        self.starter_id = None
        self.yes = set()
        self.no = set()

    def _broadcast(self, message_type: int, target) -> None:
        pkt = GenericVoteMessage()
        pkt.player_id = int(self.starter_id) if self.starter_id is not None else 255
        pkt.message_type = int(message_type)
        # Candidate names carry the human-readable target (the client renders
        # these directly, no eval). Vote counts drive the tally bars.
        tname = target.name if target is not None else "player"
        pkt.candidates = [
            {"name": "Kick {}".format(tname), "votes": len(self.yes)},
            {"name": "Keep", "votes": len(self.no)},
        ]
        # NOTE: the compiled client ast.literal_evals title/description as a
        # localized-string structure (a (string_id, args...) tuple) — arbitrary
        # text throws "malformed string". Until the exact arity is decompiled
        # from gameScene.pyd, send the one form that parses cleanly (a bare
        # string-id 1-tuple) so the overlay opens without a client crash.
        pkt.title = repr(("KICK_PLAYER",))
        pkt.description = repr(("KICK_PLAYER",))
        pkt.allow_revote = 1
        pkt.can_vote = 1
        self.server.broadcast(bytes(pkt.generate()))
