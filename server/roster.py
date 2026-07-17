"""Reliable player-roster catch-up for the native retail client.

Gameplay packets are gated until a connection's first ``ClientData`` frame.
A player can join, respawn, die, or disconnect while another client loads, so
the roster snapshot taken before map loading is not sufficient by itself.
This module records the concrete player life each connection was told about
and emits only missing transitions when that client enters the game scene.
"""

from __future__ import annotations

from shared.packet import CreatePlayer, KillAction, PlayerLeft


def player_life_token(player) -> tuple[int, int]:
    """Return a process-local identity for one concrete spawned life."""

    # Player ids are reused. Object identity prevents a new player whose first
    # life is also generation one from aliasing the disconnected old player.
    return (id(player), int(getattr(player, "replication_generation", 0)))


def known_player_lives(connection) -> dict[int, tuple[int, int]]:
    """Return the roster ledger owned by ``connection``, creating it lazily."""

    known = getattr(connection, "known_player_lives", None)
    if known is None:
        known = {}
        connection.known_player_lives = known
    return known


def known_player_deaths(connection) -> dict[int, tuple[int, int]]:
    """Return the per-connection ledger of death transitions already sent.

    A life token alone cannot distinguish "CreatePlayer was sent" from
    "CreatePlayer and KillAction were sent".  Classic corpse catch-up needs
    that distinction because repeating KillAction can recreate/reset the
    native corpse while a joining GameScene is being revealed.
    """

    known = getattr(connection, "known_player_deaths", None)
    if known is None:
        known = {}
        connection.known_player_deaths = known
    return known


def remember_player_life(connection, player) -> None:
    """Record that ``connection`` was queued this player's current life."""

    player_id = int(player.id)
    known_player_lives(connection)[player_id] = player_life_token(player)
    # A CreatePlayer for this life always precedes any later KillAction.
    known_player_deaths(connection).pop(player_id, None)
    cleanup = getattr(connection, "known_corpse_cleanups", None)
    if cleanup is not None:
        cleanup.pop(player_id, None)


def send_player_death(connection, player, server=None) -> None:
    """Send one native death transition and any Classic corpse cleanup.

    ``KillAction`` is replay-safe only when it is followed by packet 36 for a
    corpse that exploded while this connection was still gameplay-gated.  Keep
    both packets at one boundary so roster repair cannot resurrect a corpse.
    """

    player_id = int(player.id)
    death = getattr(player, "last_kill_action_data", None)
    if death:
        connection.send(bytes(death), reliable=True)
    else:
        packet = KillAction()
        packet.player_id = player_id
        packet.killer_id = player_id
        packet.kill_type = 0
        packet.respawn_time = 0
        # Synthetic self-death repairs representation only; cumulative score
        # is not a native multikill streak.
        packet.kill_count = 0
        packet.isDominationKill = 0
        packet.isRevengeKill = 0
        connection.send(bytes(packet.generate()), reliable=True)

    # Commit immediately after the reliable death packet was accepted. If the
    # optional cleanup below fails, the next reveal retries packet 36 only.
    known_player_deaths(connection)[player_id] = player_life_token(player)

    if server is None:
        server = getattr(connection, "server", None)
    corpse_lifecycle = getattr(server, "corpse_lifecycle", None)
    send_catchup = getattr(corpse_lifecycle, "send_catchup_state", None)
    if callable(send_catchup):
        send_catchup(connection, player)


def build_create_player(player, position=None) -> CreatePlayer:
    """Build the canonical live-character announcement for ``player``."""

    from server.connection import internal_team_to_wire

    packet = CreatePlayer()
    packet.player_id = int(player.id)
    packet.demo_player = 0
    packet.class_id = int(player.class_id)
    packet.team = internal_team_to_wire(player.team)
    packet.dead = 0
    packet.local_language = int(getattr(player, "local_language", 0))
    spawn = player.position if position is None else position
    packet.x, packet.y, packet.z = spawn
    # A degenerate vector NaNs the native remote-character look-at basis.
    packet.ori_x = float(player.o_x)
    packet.ori_y = float(player.o_y)
    packet.ori_z = float(player.o_z)
    packet.name = player.name
    packet.loadout = list(getattr(player, "loadout", []) or [])
    packet.prefabs = list(getattr(player, "prefabs", []) or [])
    return packet


def catch_up_roster(server, connection) -> None:
    """Reconcile one newly revealed client with the current player roster.

    This closes the simultaneous-join race without duplicate-creating entries
    already delivered during the handshake. It also removes ids disconnected
    while loading and applies a missed death to an announced life.
    """

    known = known_player_lives(connection)
    known_deaths = known_player_deaths(connection)
    local_player = getattr(connection, "player", None)
    local_id = getattr(local_player, "id", None)
    current_ids = {int(player_id) for player_id in server.players}

    for stale_id in tuple(known):
        if stale_id == local_id or stale_id in current_ids:
            continue
        packet = PlayerLeft()
        packet.player_id = stale_id
        connection.send(bytes(packet.generate()), reliable=True)
        known.pop(stale_id, None)
        known_deaths.pop(stale_id, None)
        cleanup = getattr(connection, "known_corpse_cleanups", None)
        if cleanup is not None:
            cleanup.pop(stale_id, None)

    for player in server.players.values():
        player_id = int(player.id)
        token = player_life_token(player)
        from server.game_constants import TEAM_SPECTATOR

        spectator = int(player.team) == TEAM_SPECTATOR
        if player_id == local_id:
            # Own CreatePlayer is sent directly during join and must not be
            # duplicated when the first movement frame reveals the world.
            known[player_id] = token
            continue

        if (player.alive and player.spawned) or spectator:
            if known.get(player_id) == token:
                continue
            packet = build_create_player(player)
            connection.send(bytes(packet.generate()), reliable=True)
            remember_player_life(connection, player)
            continue

        if player_id not in known:
            continue
        if known_deaths.get(player_id) == token:
            # The initial roster may already have created and killed this
            # exact life. Only its corpse-explosion state can have changed
            # while gameplay packets were gated; packet 36 repairs that race
            # without replaying KillAction.
            corpse_lifecycle = getattr(server, "corpse_lifecycle", None)
            send_catchup = getattr(
                corpse_lifecycle, "send_catchup_state", None
            )
            if callable(send_catchup):
                send_catchup(connection, player)
            continue
        send_player_death(connection, player, server)
        known[player_id] = token
