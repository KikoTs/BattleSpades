"""Round, death, and respawn lifecycle ownership.

All spawn paths pass through this service so class/loadout selection is applied
at one boundary.  This prevents a client-rendered Miner from being simulated
with a stale Medic movement profile or equipment list.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .game_constants import TEAM1, TEAM2

if TYPE_CHECKING:
    from .main import BattleSpadesServer


def resolve_player_spawn(server, player) -> tuple[float, float, float]:
    """Resolve and validate the final coordinates for one new player life."""
    spawn_resolver = getattr(server.mode, "get_spawn_point", None)
    candidate = (
        spawn_resolver(player)
        if callable(spawn_resolver)
        else server.world_manager.get_spawn_point(player.team)
    )
    sanitizer = getattr(server.world_manager, "sanitize_spawn_point", None)
    if callable(sanitizer):
        candidate = sanitizer(candidate, player.team)
    return tuple(float(value) for value in candidate)


class RoundLifecycle:
    """Own respawn scheduling and transient same-map round cleanup."""

    def __init__(self, server: "BattleSpadesServer") -> None:
        self.server = server

    async def process_respawns(self) -> None:
        """Respawn players whose wall-clock death delay has elapsed."""
        server = self.server
        if not server.players:
            return
        now = time.time()
        for player in list(server.players.values()):
            if player.alive or player.spawned or player.death_time <= 0.0:
                continue
            # Spectator is a native roster/camera team, not a playable life.
            # A stale death timestamp must never manufacture a team-0 body.
            teams = getattr(server, "teams", None)
            if teams is not None and player.team not in teams:
                continue
            can_respawn = getattr(server.mode, "can_player_respawn", None)
            if callable(can_respawn) and not can_respawn(player):
                continue
            respawn_time_for = getattr(server.mode, "respawn_time_for", None)
            respawn_time = (
                float(respawn_time_for(player))
                if callable(respawn_time_for)
                else float(server.config.respawn_time)
            )
            if now - player.death_time < respawn_time:
                continue
            # GraveBehavior owns its seven-second fuse independently of the
            # usually shorter player respawn timer.
            player._grave_entity_id = None
            self.respawn_player(player)

    def respawn_player(self, player) -> None:
        """Apply pending equipment atomically, then create the new life."""
        server = self.server
        apply_pending = getattr(player, "apply_pending_selection", None)
        if callable(apply_pending):
            apply_pending()
        else:
            # Compatibility for lightweight tests and older plugins during the
            # staged migration to ClassSelection.
            pending_class = getattr(player, "pending_class_id", None)
            if pending_class is not None:
                player.class_id = int(pending_class)
                player.pending_class_id = None
            pending_loadout = getattr(player, "pending_loadout", None)
            if pending_loadout is not None:
                player.loadout = list(pending_loadout)
                player.pending_loadout = None

        # This synchronous pre-spawn hook runs before both Player.spawn and
        # CreatePlayer.  It is the safe boundary for a mode-specific initial
        # weapon; on_player_spawn is intentionally later and cannot alter the
        # native Character construction packet already sent to observers.
        prepare_spawn = getattr(server.mode, "prepare_player_spawn", None)
        if callable(prepare_spawn):
            prepare_spawn(player)

        spawn = resolve_player_spawn(server, player)
        orientation_resolver = getattr(
            server.mode, "get_spawn_orientation", None
        )
        if callable(orientation_resolver):
            player.set_orientation_vector(*orientation_resolver(player))
        player.spawn(spawn[0], spawn[1], spawn[2])
        player.death_time = 0.0
        server._broadcast_create_player(player, spawn)
        player.restock_ammo()
        if server.mode is not None:
            server.queue_mode_event("on_player_spawn", player)

    def forget_player(self, player) -> None:
        """Retire all runtime state keyed by a departing player's wire id.

        The protocol's small numeric player ids are reused immediately.  This
        method therefore runs synchronously before ``server.players`` releases
        the id.  Persistent construction and objectives are deliberately left
        alone; only state whose behavior, ownership, credit, or carrier points
        at the departing identity is cancelled or destroyed.
        """

        server = self.server
        player_id = int(player.id)

        world_mutations = getattr(server, "world_mutations", None)
        cancel_owner = getattr(world_mutations, "cancel_owner", None)
        if callable(cancel_owner):
            cancel_owner(player_id)
        prefab_actions = getattr(server, "prefab_actions", None)
        cancel_prefabs = getattr(prefab_actions, "cancel_owner", None)
        if callable(cancel_prefabs):
            cancel_prefabs(player_id)

        projectile_engine = getattr(server, "projectile_engine", None)
        remove_projectiles = getattr(projectile_engine, "remove_by_thrower", None)
        if callable(remove_projectiles):
            for projectile in remove_projectiles(player_id):
                entity_id = projectile.entity_id
                if entity_id is None:
                    continue
                if server.entity_registry.remove(entity_id) is not None:
                    server.broadcast_destroy_entity(entity_id)

        fire_controller = getattr(server, "fire_controller", None)
        forget_fire = getattr(fire_controller, "forget_player", None)
        if callable(forget_fire):
            forget_fire(player_id)

        combat = getattr(server, "combat", None)
        forget_combat = getattr(combat, "forget_player", None)
        if callable(forget_combat):
            forget_combat(player_id)

        corpse_lifecycle = getattr(server, "corpse_lifecycle", None)
        forget_corpse = getattr(corpse_lifecycle, "forget_player", None)
        if callable(forget_corpse):
            forget_corpse(player_id)

        vote_manager = getattr(server, "vote_manager", None)
        forget_vote = getattr(vote_manager, "forget_player", None)
        if callable(forget_vote):
            forget_vote(player_id)

        replication = getattr(server, "replication", None)
        forget_replication = getattr(replication, "forget_player", None)
        if callable(forget_replication):
            forget_replication(player_id)

        self.remove_owned_deployables(player)

    def remove_owned_deployables(self, player) -> None:
        """Retire entities whose allegiance is bound to the owner's team.

        Called for disconnects and before a live team change. A deployable may
        retain its placement team for targeting/minimap behavior, so carrying
        it across the transition would make a turret shoot its owner and leave
        radar visibility enabled for the old roster.
        """

        turret_controller = getattr(
            self.server,
            "rocket_turret_controller",
            None,
        )
        remove_turrets = getattr(turret_controller, "remove_by_owner", None)
        if callable(remove_turrets):
            remove_turrets(int(player.id))
        self._remove_owned_entities(player)

    def _remove_owned_entities(self, player) -> None:
        """Destroy owner-sensitive entities and release foreign MG mounts."""

        from server.entities.behaviors import RadarStationBehavior
        from server.entities.machine_gun import MachineGunBehavior

        server = self.server
        player_id = int(player.id)
        owner_bound_kinds = {"deployable", "medpack", "grave"}

        for entity in list(server.entity_registry.all()):
            behavior = entity.behavior
            if isinstance(behavior, MachineGunBehavior):
                owned = int(behavior.owner_id) == player_id
                carried = behavior.carrier_id == player_id
                if carried and not owned:
                    # A mounted gun belongs to its placer, not its carrier.
                    behavior.unmount(entity, server)
                    continue
                if not owned:
                    continue
                if behavior.carrier_id is not None:
                    behavior.unmount(entity, server)
            else:
                owned = (
                    entity.kind in owner_bound_kinds
                    and int(entity.player_id) == player_id
                )
                if not owned:
                    continue

            if isinstance(behavior, RadarStationBehavior):
                server._radar_station_removed(behavior.team)
            if server.entity_registry.remove(entity.entity_id) is not None:
                server.broadcast_destroy_entity(entity.entity_id)

        player.mounted_entity_id = None
        player._c4_entity_ids = []
        player._radar_entity_id = None

    def reset_round_runtime(self) -> None:
        """Clear transient server and client entity state for a new round."""
        server = self.server
        # Pending edits carry old-life inventory reservations and client-loop
        # labels.  Never let them cross the round's timeline reset.
        world_mutations = getattr(server, "world_mutations", None)
        if world_mutations is not None:
            world_mutations.cancel_all()
        prefab_actions = getattr(server, "prefab_actions", None)
        if prefab_actions is not None:
            prefab_actions.cancel_all()
        construction = getattr(server, "construction", None)
        if construction is not None:
            construction.clear()
        for team, count in list(server._radar_station_counts.items()):
            if count > 0:
                for player in server.players.values():
                    if player.team == team:
                        server._send_radar_visibility(player, False)
        server._radar_station_counts = {TEAM1: 0, TEAM2: 0}

        # The retail GameScene survives a same-map restart. Destroy visible
        # ids before resetting the allocator or later packets target old models.
        if bool(getattr(server.config, "entities_wire_ready", False)):
            for entity in list(server.entity_registry.all()):
                # Server-only objective markers were never announced through
                # CreateEntity, so destroying them creates noisy invalid-id
                # lookups in the native client (and risks future strict-client
                # crashes).  Keep create/destroy visibility symmetric.
                if getattr(entity, "wire_visible", True):
                    server.broadcast_destroy_entity(entity.entity_id)
        server.entity_registry.clear()
        server.entities.clear()
        server.rocket_turrets.clear()
        server.projectile_engine.projectiles.clear()
        server.fire_controller.clear()
        corpse_lifecycle = getattr(server, "corpse_lifecycle", None)
        clear_corpses = getattr(corpse_lifecycle, "clear", None)
        if callable(clear_corpses):
            clear_corpses(notify=True)

        for player in server.players.values():
            # KillAction.kill_count is a current-life streak used by the
            # retail multikill HUD, not the cumulative scoreboard total.
            player.kill_streak = 0
            player.mounted_entity_id = None
            player._c4_entity_ids = []
            player._radar_entity_id = None
