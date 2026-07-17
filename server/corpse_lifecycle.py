"""Classic-corpse ownership and packet-36 replication.

``KillAction`` creates the dead Character on the retail client.  Classic mode
selects ``ClassicCorpse.kv6`` for that Character; it is not a packet-21 entity
and therefore must never consume an entity-registry id.  This service retains
only enough authoritative state to hit, explode, clean up, and late-join that
client-owned corpse.

All methods run on the gameplay thread.  The service performs no blocking I/O
and every operation is bounded by the server's player limit.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import TYPE_CHECKING, Iterable

import shared.constants as C

if TYPE_CHECKING:
    from server.main import BattleSpadesServer
    from server.player import Player


CLASSIC_CORPSE_REPRESENTATION = "classic_corpse"


@dataclass(slots=True)
class ClassicCorpseState:
    """One dead Classic Character that still exists in retail GameScenes.

    ``generation`` prevents a delayed shot or cleanup from targeting a reused
    player id or the player's next life.  Position and orientation are frozen
    at death because the Classic corpse is a single static KV6 model.
    """

    player_id: int
    generation: int
    position: tuple[float, float, float]
    orientation: tuple[float, float, float]
    created_at: float
    exploded: bool = False

    @property
    def x(self) -> float:
        return self.position[0]

    @property
    def y(self) -> float:
        return self.position[1]

    @property
    def z(self) -> float:
        return self.position[2]


class CorpseLifecycle:
    """Own static Classic corpses without creating normal grave entities.

    The native ordering invariant is:

    1. ``KillAction`` changes the existing Character into its dead model.
    2. The corpse remains visible and hittable.
    3. ``ExplodeCorpse`` (36) marks that Character's corpse exploded.  A zero
       effect byte is cleanup; a non-zero byte requests the native effect.
    4. Cleanup always precedes the next ``CreatePlayer`` for that numeric id.

    Packet 36 is deliberately never sent at initial death: doing so removes the
    corpse in the same frame that ``KillAction`` creates it.
    """

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server
        self._classic_corpses: dict[int, ClassicCorpseState] = {}

    def on_player_death(self, player: "Player") -> bool:
        """Record a Classic corpse and report whether it replaces a grave.

        Returns ``True`` only for a mode that explicitly selects the Classic
        corpse representation.  The caller uses that result to suppress the
        ordinary entity-11 gravestone path.
        """

        player_id = int(player.id)
        self._classic_corpses.pop(player_id, None)
        mode = getattr(self.server, "mode", None)
        if (
            getattr(mode, "death_representation", "grave")
            != CLASSIC_CORPSE_REPRESENTATION
        ):
            return False

        self._classic_corpses[player_id] = ClassicCorpseState(
            player_id=player_id,
            generation=int(getattr(player, "replication_generation", 0)),
            position=tuple(float(value) for value in player.position),
            orientation=tuple(float(value) for value in player.orientation),
            created_at=time.monotonic(),
        )
        return True

    def get(self, player_or_id) -> ClassicCorpseState | None:
        """Return the corpse belonging to the supplied current player life."""

        player_id = int(getattr(player_or_id, "id", player_or_id))
        state = self._classic_corpses.get(player_id)
        if state is None:
            return None

        player = self.server.players.get(player_id)
        if player is None:
            self._classic_corpses.pop(player_id, None)
            return None
        if int(getattr(player, "replication_generation", -1)) != state.generation:
            self._classic_corpses.pop(player_id, None)
            return None
        return state

    def active_for_join(self, player: "Player") -> bool:
        """Whether a joining client must receive CreatePlayer + KillAction."""

        state = self.get(player)
        return state is not None and not state.exploded

    def iter_hittable(self) -> Iterable[ClassicCorpseState]:
        """Yield current unexploded corpses when the game rule permits it."""

        from server.game_rules import get_rules

        if not get_rules(self.server.config).enabled(
            "RULE_ENABLE_CORPSE_EXPLOSION"
        ):
            return ()

        states: list[ClassicCorpseState] = []
        for player_id in tuple(self._classic_corpses):
            state = self.get(player_id)
            if state is not None and not state.exploded:
                states.append(state)
        return tuple(states)

    def explode(
        self,
        state: ClassicCorpseState,
        attacker: "Player | None",
        *,
        show_explosion_effect: bool = True,
    ) -> bool:
        """Explode one current corpse once and replicate its native effect.

        A visible explosion also applies the recovered corpse constants through
        the shared blast path: zero player damage, one block damage, radius
        three, and the small corpse knockback.  Silent cleanup changes no world
        state and is used only before a respawn or round reset.
        """

        current = self._classic_corpses.get(int(state.player_id))
        if current is not state or state.exploded:
            return False

        self._send_packet(
            state.player_id,
            show_explosion_effect=show_explosion_effect,
        )
        # Advance only after the reliable broadcast was accepted. A partial
        # multi-peer failure may duplicate this idempotent packet on retry,
        # which is safer than leaving the remaining clients with a live corpse.
        state.exploded = True

        if not show_explosion_effect:
            return True

        from server.game_rules import get_rules

        if not get_rules(self.server.config).enabled(
            "RULE_ENABLE_CORPSE_EXPLOSION"
        ):
            return True

        apply_blast = getattr(self.server, "_apply_blast", None)
        if callable(apply_blast):
            apply_blast(
                *state.position,
                float(getattr(C, "CORPSE_EXPLOSION_DAMAGE", 0.0)),
                float(getattr(C, "CORPSE_EXPLOSION_BLOCK_DAMAGE", 1.0)),
                int(getattr(C.KILL, "CORPSE_KILL", 12)),
                attacker,
                crater_radius=1,
                force_destroy=False,
                blast_radius=float(getattr(C, "CORPSE_EXPLOSION_RADIUS", 3.0)),
                knockback_min=float(
                    getattr(C, "CORPSE_EXPLOSION_KNOCKBACK_MIN", 0.05)
                ),
                knockback_max=float(
                    getattr(C, "CORPSE_EXPLOSION_KNOCKBACK_MAX", 0.1)
                ),
            )
        return True

    def before_player_spawn(self, player: "Player") -> None:
        """Remove the previous corpse before this id creates a new Character."""

        state = self.get(player)
        if state is None:
            return
        if not state.exploded:
            # Packet 36 must precede CreatePlayer. Otherwise a delayed cleanup
            # can mark the newly spawned Character exploded on the stock client.
            self._send_packet(state.player_id, show_explosion_effect=False)
        # A failed reliable send aborts spawn and retains the corpse so the
        # caller's next attempt can clean it before changing the generation.
        self._classic_corpses.pop(state.player_id, None)

    def forget_player(self, player_or_id) -> None:
        """Discard state for a departing id; PlayerLeft owns client cleanup."""

        player_id = int(getattr(player_or_id, "id", player_or_id))
        self._classic_corpses.pop(player_id, None)

    def clear(self, *, notify: bool = False) -> None:
        """Clear round state, optionally removing visible corpses first."""

        states = tuple(self._classic_corpses.values())
        self._classic_corpses.clear()
        if notify:
            for state in states:
                if not state.exploded:
                    self._send_packet(
                        state.player_id,
                        show_explosion_effect=False,
                    )

    def send_catchup_state(self, connection, player: "Player") -> bool:
        """Close a death/explosion race for a client entering GameScene.

        If the initial roster created a corpse while the connection was still
        gameplay-gated and that corpse exploded meanwhile, replaying only
        ``KillAction`` would resurrect it locally.  Follow with silent packet 36
        when the authoritative corpse is already gone. Returns whether a new
        cleanup packet was queued for this connection.
        """

        state = self.get(player)
        if state is None or not state.exploded:
            return False
        player_id = int(state.player_id)
        token = (id(player), int(state.generation))
        known = getattr(connection, "known_corpse_cleanups", None)
        if known is None:
            known = {}
            connection.known_corpse_cleanups = known
        if known.get(player_id) == token:
            return False
        self._send_packet(
            player_id,
            show_explosion_effect=False,
            connection=connection,
        )
        # Advance only after the reliable send succeeds. A failed ENet enqueue
        # therefore retries this exact packet without replaying KillAction.
        known[player_id] = token
        return True

    def _send_packet(
        self,
        player_id: int,
        *,
        show_explosion_effect: bool,
        connection=None,
    ) -> None:
        """Emit the three-byte server-to-client ExplodeCorpse packet."""

        from shared.packet import ExplodeCorpse

        packet = ExplodeCorpse()
        packet.player_id = int(player_id)
        packet.show_explosion_effect = int(bool(show_explosion_effect))
        data = bytes(packet.generate())
        if connection is not None:
            connection.send(data, reliable=True)
            return
        self.server.broadcast(data, reliable=True)


__all__ = [
    "CLASSIC_CORPSE_REPRESENTATION",
    "ClassicCorpseState",
    "CorpseLifecycle",
]
