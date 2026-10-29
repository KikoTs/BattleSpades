"""
Capture the Flag game mode.
Two teams fight to capture the enemy's intel and return it to their base.
"""

import math
import time
import logging
from typing import Optional, Tuple, TYPE_CHECKING

import shared.constants as C
import shared.constants_gamemode as CG

from server import mode_data
from server.game_constants import (
    PLAYER_STANDING_POS_ABOVE_GROUND,
    TEAM1,
    TEAM2,
    TEAM_NEUTRAL,
)

from . import objective_guard
from .base_mode import BaseMode

if TYPE_CHECKING:
    from server.player import Player

logger = logging.getLogger(__name__)

_FALLBACK_BASE_RADIUS = float(CG.CLASSIC_CTF_BASE_CAPTURE_DISTANCE)


def _ground_anchor(
    server,
    x: float,
    y: float,
    fallback_z: float = 62.0 - PLAYER_STANDING_POS_ABOVE_GROUND,
) -> tuple[float, float, float]:
    world_manager = getattr(server, "world_manager", None)
    if world_manager is None:
        return (x, y, fallback_z)
    try:
        # Anchor on the nearest DRY column so a base/intel whose nominal spot is
        # over water snaps to the shoreline instead of the seabed.
        return world_manager.dry_ground_anchor(x, y)
    except Exception:
        return (x, y, fallback_z)


def _falling_ground_anchor(
    server,
    x: float,
    y: float,
    start_z: float,
) -> tuple[float, float, float]:
    """Settle a dropped intel on the first support below it.

    AoS Z grows downward. A normal dry-ground lookup starts at the sky and
    therefore mistakes any roof above the carrier for the landing surface.
    Drop physics must instead scan from the submitted drop height downward,
    while preserving the exact horizontal coordinates (including water).
    The returned Z is in the player-space used by ``intel_positions``.
    """
    try:
        drop_x, drop_y, drop_z = float(x), float(y), float(start_z)
    except (TypeError, ValueError):
        return _ground_anchor(server, x, y)
    if not all(math.isfinite(value) for value in (drop_x, drop_y, drop_z)):
        return _ground_anchor(server, drop_x, drop_y)

    world = getattr(server, "world_manager", None)
    if world is None:
        return drop_x, drop_y, drop_z

    surface_z = None
    falling_anchor = getattr(world, "falling_surface_anchor", None)
    if callable(falling_anchor):
        try:
            candidate = falling_anchor(drop_x, drop_y, drop_z)
            candidate_z = float(candidate[2])
            candidate_is_support = True
            if math.isclose(candidate_z, drop_z, abs_tol=1e-9):
                # WorldManager returns the unchanged input when no voxel can
                # be found. An integer-height solid at the exact start is the
                # only legitimate same-Z landing.
                get_solid = getattr(world, "get_solid", None)
                cell_x, cell_y = int(math.floor(drop_x)), int(math.floor(drop_y))
                cell_z = int(math.floor(candidate_z))
                if callable(get_solid):
                    candidate_is_support = (
                        0 <= cell_x < int(C.MAP_X)
                        and 0 <= cell_y < int(C.MAP_Y)
                        and 0 <= cell_z < int(C.MAP_Z)
                        and math.isclose(candidate_z, float(cell_z), abs_tol=1e-9)
                        and bool(get_solid(cell_x, cell_y, cell_z))
                    )
            if (
                candidate_is_support
                and math.isfinite(candidate_z)
                and candidate_z >= drop_z
            ):
                surface_z = candidate_z
        except Exception:
            surface_z = None

    if surface_z is None:
        get_solid = getattr(world, "get_solid", None)
        cell_x, cell_y = int(math.floor(drop_x)), int(math.floor(drop_y))
        if (
            callable(get_solid)
            and 0 <= cell_x < int(C.MAP_X)
            and 0 <= cell_y < int(C.MAP_Y)
        ):
            for cell_z in range(
                max(0, int(math.ceil(drop_z))),
                int(C.MAP_Z),
            ):
                try:
                    solid = bool(get_solid(cell_x, cell_y, cell_z))
                except Exception:
                    break
                if solid:
                    surface_z = float(cell_z)
                    break

    # Compatibility for lightweight worlds and older plugin world managers.
    # Accept a legacy top-surface result only when it is actually below the
    # drop; a roof above must never pull the intel upward.
    if surface_z is None:
        surface_anchor = getattr(world, "dry_surface_anchor", None)
        if callable(surface_anchor):
            try:
                candidate = surface_anchor(drop_x, drop_y, search=0)
            except TypeError:
                try:
                    candidate = surface_anchor(drop_x, drop_y)
                except Exception:
                    candidate = None
            except Exception:
                candidate = None
            if candidate is not None:
                try:
                    candidate_z = float(candidate[2])
                    if math.isfinite(candidate_z) and candidate_z >= drop_z:
                        surface_z = candidate_z
                except (TypeError, ValueError, IndexError):
                    pass

    if surface_z is None:
        # Without a same-column terrain oracle it is safer to leave a drop at
        # its submitted height than to teleport it onto an overhead roof.
        return drop_x, drop_y, drop_z
    return (
        drop_x,
        drop_y,
        surface_z - PLAYER_STANDING_POS_ABOVE_GROUND,
    )


def _intel_entity_anchor(position) -> tuple[float, float, float]:
    """Convert an authoritative player-space intel anchor to entity space."""
    x, y, z = (float(value) for value in position)
    return x, y, z + PLAYER_STANDING_POS_ABOVE_GROUND


def _unburied_intel_anchor(world, position):
    """Lift intel only through the connected stack occupying its model.

    A settled intel rests at ``logical Z + STANDING`` and occupies roughly the
    voxel immediately above that surface. A detached ceiling is harmless; a
    solid in the occupied voxel raises the intel to the top of that connected
    vertical stack. Returns a new logical anchor, or ``None`` when unobstructed.
    """
    try:
        x, y, logical_z = (float(value) for value in position)
    except (TypeError, ValueError):
        return None
    get_solid = getattr(world, "get_solid", None)
    if not callable(get_solid):
        return None
    cell_x, cell_y = int(math.floor(x)), int(math.floor(y))
    if not (
        0 <= cell_x < int(C.MAP_X)
        and 0 <= cell_y < int(C.MAP_Y)
    ):
        return None
    surface_z = logical_z + PLAYER_STANDING_POS_ABOVE_GROUND
    occupied_z = int(math.ceil(surface_z)) - 1
    if occupied_z < 0 or occupied_z >= int(C.MAP_Z):
        return None
    try:
        if not get_solid(cell_x, cell_y, occupied_z):
            return None
        while occupied_z > 0 and get_solid(cell_x, cell_y, occupied_z - 1):
            occupied_z -= 1
    except Exception:
        return None
    return (
        x,
        y,
        float(occupied_z) - PLAYER_STANDING_POS_ABOVE_GROUND,
    )


def _intel_near(server, base_pos, dx: float) -> tuple[float, float, float]:
    """Place the intel `dx` blocks along +x from the base, re-anchored to dry
    ground (keeps it out of the water near shoreline bases)."""
    return _ground_anchor(server, base_pos[0] + dx, base_pos[1])


def _intel_home(
    server,
    base_pos,
    enemy_base_pos,
    offset: float,
    base_zone=None,
    fallback_sign: float = 1.0,
) -> tuple[float, float, float]:
    """Intel home: ``offset`` blocks from the base toward the enemy base.

    Retail map data (the ``.txtc`` ``ctf_base_points``/``ctf_base_w_h_d``
    and the UGC editor, which offers base zones but no intel item) never
    authors an intel point, so the server derives it from the base. The
    caller picks ``offset`` (see ``CTFMode.intel_offset_from_base``): 0 puts
    the intel on the base point itself (retail CTF), Classic CTF uses the
    retail ``CLASSIC_CTF_INTEL_MIN_RADIUS_FROM_BASE`` (3). A non-zero offset
    runs along the axis between the two bases, toward midfield, so north/south
    maps (WW1, ToTheBridge, Crossroads) do not put it sideways. With an
    authored base box the intel stays inside it, but never closer than the
    retail 3-block minimum. Coincident bases fall back to the historical x
    axis (``fallback_sign``).
    """
    bx, by = float(base_pos[0]), float(base_pos[1])
    dx = float(enemy_base_pos[0]) - bx
    dy = float(enemy_base_pos[1]) - by
    length = math.hypot(dx, dy)
    if length < 1e-6:
        ux, uy = float(fallback_sign), 0.0
    else:
        ux, uy = dx / length, dy / length
    distance = max(0.0, float(offset))
    if base_zone is not None and distance > 0.0:
        x0, x1, y0, y1 = base_zone.xy_bounds()
        limit = distance
        for position, unit, low, high in ((bx, ux, x0, x1), (by, uy, y0, y1)):
            if unit > 1e-9:
                limit = min(limit, (high - 1 + 0.5 - position) / unit)
            elif unit < -1e-9:
                limit = min(limit, (low + 1 + 0.5 - position) / unit)
        floor = min(distance, float(CG.CLASSIC_CTF_INTEL_MIN_RADIUS_FROM_BASE))
        distance = max(floor, limit)
    return _ground_anchor(server, bx + ux * distance, by + uy * distance)


class CTFMode(BaseMode):
    """
    Capture the Flag mode.
    
    Rules:
    - Each team has an intel (flag) at their base
    - Pick up enemy intel by walking over it
    - Return to your base while holding intel to score
    - Dying while holding intel drops it
    - Score limit or time limit determines winner
    """
    
    name = "Capture the Flag"
    description = "Capture the enemy intel and return it to your base!"
    
    score_limit = 10
    time_limit = 1200  # 20 minutes
    mode_code = "ctf"
    intel_auto_return_default = True
    shoot_with_intel_default = False
    # Retail CTF: the intel sits on the team's base point (the centre of the
    # authored ``ctf_base_points`` box). The retail tables have no CTF intel
    # offset/radius constant (only the Classic-only
    # ``CLASSIC_CTF_INTEL_MIN_RADIUS_FROM_BASE``) and no map format carries an
    # intel point. See docs/RETAIL_VALUES.md "CTF intel placement".
    intel_offset_from_base = 0.0
    # Server choice for maps whose retail base data was not recovered: the
    # "base" is then only our inferred team anchor (usually the spawn area),
    # so keep the intel 12 blocks out of the spawn toward midfield.
    intel_fallback_offset_from_base = 12.0

    def __init__(self, server):
        super().__init__(server)

        data = mode_data.get(self.mode_code)
        overlay = getattr(server.config, "mode_settings", {}).get(
            self.mode_code, {}
        )
        from server.game_rules import get_rules
        rules = get_rules(server.config)
        self.score_limit = int(overlay.get(
            "score_limit", rules.get("RULE_CTF_SCORE_TARGET")
        ))
        resolve_time = getattr(server.config, "configured_time_limit", None)
        self.time_limit = (
            resolve_time(self.mode_code, data.default_time_limit)
            if callable(resolve_time)
            else float(overlay.get("time_limit", data.default_time_limit))
        )
        explicit = getattr(rules, "explicit", set())
        self.intel_auto_return = bool(overlay.get(
            "intel_auto_return",
            rules.get("RULE_CTF_ENABLE_INTEL_AUTO_RETURN")
            if "RULE_CTF_ENABLE_INTEL_AUTO_RETURN" in explicit
            else self.intel_auto_return_default,
        ))
        self.intel_return_on_touch = bool(overlay.get(
            "intel_return_on_touch",
            rules.get("RULE_CTF_ENABLE_INTEL_RETURN_ON_TOUCH"),
        ))
        self.intel_in_own_base_to_score = bool(overlay.get(
            "intel_in_own_base_to_score",
            rules.get("RULE_CTF_ENABLE_INTEL_IN_OWN_BASE_TO_SCORE"),
        ))
        self.shoot_with_intel = bool(overlay.get(
            "shoot_with_intel",
            self.shoot_with_intel_default
            if "RULE_CTF_ENABLE_SHOOT_WITH_INTEL" not in explicit
            else rules.get("RULE_CTF_ENABLE_SHOOT_WITH_INTEL"),
        ))
        
        # Intel positions (set during on_mode_start)
        self.intel_positions = {
            TEAM1: (0.0, 0.0, 0.0),
            TEAM2: (0.0, 0.0, 0.0),
        }
        
        # Base positions (tent locations)
        self.base_positions = {
            TEAM1: (0.0, 0.0, 0.0),
            TEAM2: (0.0, 0.0, 0.0),
        }
        
        # Intel state
        self.intel_holder = {
            TEAM1: None,
            TEAM2: None,
        }
        
        # Pickup cooldown (to prevent instant re-grab)
        self.intel_drop_time = {TEAM1: 0.0, TEAM2: 0.0}
        self.pickup_cooldown = float(C.NO_PICKUP_AFTER_DROP_TIME)
        self.intel_home_positions = dict(self.intel_positions)
        self._intel_entities = {TEAM1: None, TEAM2: None}
        self._base_entities = {TEAM1: None, TEAM2: None}
        self.base_bounds = {
            TEAM1: (0, 0, 0, 0, 0, 0),
            TEAM2: (0, 0, 0, 0, 0, 0),
        }
        self._reset_carry_state()

    def _reset_carry_state(self) -> None:
        """Per-intel carry clock, minimap exposure and escort membership.

        ``_carry_started`` is armed on the first tick that sees a holder (the
        pickup path stays clock-free); ``_carry_next`` is the next CTF_CARRY/
        CTF_ESCORT payout; ``_carrier_exposed`` records whether this mode has
        sent the high-minimap marker for the current holder.
        """
        from server.combat_scores import EscortTracker

        self._carry_started = {TEAM1: None, TEAM2: None}
        self._carry_next = {TEAM1: None, TEAM2: None}
        self._carrier_exposed = {TEAM1: False, TEAM2: False}
        self._escorts = EscortTracker(
            float(CG.CTF_ESCORT_RADIUS), float(CG.CTF_ESCORT_HYSTERESIS)
        )

    def _clear_carry_state(self, intel_team: int) -> bool:
        """Forget one intel's carry clock; True if its marker was exposed."""
        exposed = bool(self._carrier_exposed.get(intel_team))
        holder = self.intel_holder.get(intel_team)
        if holder is not None:
            self._escorts.forget_carrier(holder)
        self._carry_started[intel_team] = None
        self._carry_next[intel_team] = None
        self._carrier_exposed[intel_team] = False
        return exposed

    async def on_mode_start(self):
        """Initialize intel and base positions."""
        await super().on_mode_start()
        # A same-scene round restart preserves native Player objects. Clear a
        # previous carrier marker before replacing the authoritative holders.
        old_holders = []
        for team_id, holder in self.intel_holder.items():
            if (
                holder is not None
                and self._carrier_exposed.get(team_id)
                and all(holder is not old for old in old_holders)
            ):
                old_holders.append(holder)
        for holder in old_holders:
            if self._is_connected(holder):
                self._set_carrier_visibility(holder, False)
        self._reset_carry_state()
        self.intel_holder = {TEAM1: None, TEAM2: None}
        self.intel_drop_time = {TEAM1: 0.0, TEAM2: 0.0}
        
        # Prefer authored sidecar base/spawn zones, falling back to validated
        # dry terrain in the legacy west/east team regions on voxel-only maps.
        wm = getattr(self.server, "world_manager", None)
        if wm is not None and hasattr(wm, "team_base_anchor"):
            self.base_positions[TEAM1] = wm.team_base_anchor(TEAM1)
            self.base_positions[TEAM2] = wm.team_base_anchor(TEAM2)
        else:
            self.base_positions[TEAM1] = _ground_anchor(self.server, 64.0, 256.0)
            self.base_positions[TEAM2] = _ground_anchor(self.server, 448.0, 256.0)

        # Retail rule: the intel is derived from the authored base (CTF: on
        # the base point; Classic: the retail 3-block radius toward the
        # enemy). Maps without recovered base data use the fallback offset
        # from the inferred anchor. Re-anchored to dry ground either way.
        metadata = getattr(wm, "map_metadata", None)
        for team, enemy, sign in ((TEAM1, TEAM2, 1.0), (TEAM2, TEAM1, -1.0)):
            authored = [] if metadata is None else metadata.base_zones.get(team, [])
            offset = float(
                self.intel_offset_from_base
                if authored
                else self.intel_fallback_offset_from_base
            )
            self.intel_positions[team] = _intel_home(
                self.server,
                self.base_positions[team],
                self.base_positions[enemy],
                offset,
                base_zone=authored[0] if authored else None,
                fallback_sign=sign,
            )
        self.intel_home_positions = dict(self.intel_positions)
        self.base_bounds = {
            TEAM1: self._base_zone_bounds(TEAM1),
            TEAM2: self._base_zone_bounds(TEAM2),
        }
        
        # Update team objects
        for team_id, pos in self.intel_positions.items():
            self.server.teams[team_id].set_intel_position(*pos)

        self._place_objective_entities()
        self._send_base_zones()
        
        logger.info("CTF mode started")

    def _place_objective_entities(self):
        """Create CTF objective markers without unsafe legacy entity packets.

        The retail ``GameScene.ENTITIES`` mapping has ``INTEL_PICKUP`` (16),
        but not the legacy ``BASE`` type (1).  A BASE sent through packet 21
        crashes/freeze-loops the client during CTF join.  We retain a private
        base marker for authoritative capture logic and expose only the intel;
        the base itself is represented by the map's authored base/tent area.
        """
        reg = getattr(self.server, "entity_registry", None)
        wm = getattr(self.server, "world_manager", None)
        if reg is None or wm is None:
            return

        # BaseMode has already rebuilt the map-owned crates/lights.  Remove
        # only stale CTF markers here: clearing the registry used to erase all
        # shared resources in CTF.  Do not trust the remembered ids alone;
        # RoundLifecycle resets the allocator and a new crate can legitimately
        # reuse an old intel id before this method runs.
        for ent in reg.all():
            if getattr(ent, "kind", "") not in ("base", "intel"):
                continue
            removed = reg.remove(ent.entity_id)
            if (
                removed is not None
                and removed.alive
                and getattr(removed, "wire_visible", True)
            ):
                self.server.broadcast_destroy_entity(removed.entity_id)
        self._base_entities = {TEAM1: None, TEAM2: None}
        self._intel_entities = {TEAM1: None, TEAM2: None}

        for team in (TEAM1, TEAM2):
            bx, by, _bz = self.base_positions[team]
            x, y, z = wm.dry_surface_anchor(bx, by)
            base = reg.place(
                int(C.BASE), x, y, z, state=team, kind="base",
                wire_visible=False,
            )
            self._base_entities[team] = base.entity_id

            x, y, z = _intel_entity_anchor(self.intel_positions[team])
            flag = reg.place(int(C.INTEL_PICKUP), x, y, z, state=team, kind="intel")
            self._intel_entities[team] = flag.entity_id

            if getattr(self.server.config, "entities_wire_ready", False):
                self.server.broadcast_create_entity(flag)

    def _set_intel_entity(
        self,
        team: int,
        visible: bool,
        *,
        broadcast: bool = True,
        entity_position=None,
    ):
        reg = getattr(self.server, "entity_registry", None)
        if reg is None:
            return
        old_id = self._intel_entities.get(team)
        if old_id is not None:
            if reg.remove(old_id) is not None:
                self.server.broadcast_destroy_entity(old_id)
            self._intel_entities[team] = None
        if not visible:
            return
        if entity_position is None:
            entity_position = _intel_entity_anchor(self.intel_positions[team])
        x, y, z = (float(value) for value in entity_position)
        flag = reg.place(int(C.INTEL_PICKUP), x, y, z, state=team, kind="intel")
        self._intel_entities[team] = flag.entity_id
        if broadcast and getattr(self.server.config, "entities_wire_ready", False):
            self.server.broadcast_create_entity(flag)

    def _move_ground_intel(self, team: int, position) -> None:
        """Move trigger, team state and visible model as one transaction."""
        settled = tuple(float(value) for value in position)
        dropped = self.intel_drop_time[team] > 0.0
        self.intel_positions[team] = settled
        if dropped:
            self.server.teams[team].drop_intel(*settled)
        else:
            self.intel_home_positions[team] = settled
            self.server.teams[team].return_intel(settled)
        self._set_intel_entity(
            team,
            True,
            entity_position=_intel_entity_anchor(settled),
        )
        timer = getattr(self, "_intel_trap", None)
        if timer is not None:
            timer.due(team, False, time.monotonic(), 0.0)

    def _raise_buried_intel(self, team: int) -> bool:
        """Immediately lift one ground intel out of its occupied model cell."""
        if self.intel_holder.get(team) is not None:
            return False
        world = getattr(self.server, "world_manager", None)
        if world is None:
            return False
        raised = _unburied_intel_anchor(world, self.intel_positions[team])
        if raised is None:
            return False
        self._move_ground_intel(team, raised)
        return True

    def _accessible_home_anchor(self, team: int):
        """Return-home anchor on top of the intel's exact home column.

        Returning intel is different from dropped intel: builders may have
        covered its base while it was being carried.  Resolve the current
        topmost voxel so the pickup trigger and visible model both reappear
        above that construction, while preserving the authored X/Y exactly.
        """
        home = self.intel_home_positions[team]
        world = getattr(self.server, "world_manager", None)
        if world is None:
            return home
        try:
            home_x, home_y, _home_z = (float(value) for value in home)
        except (TypeError, ValueError):
            return home

        surface_z = None
        get_height = getattr(world, "get_height", None)
        if callable(get_height):
            try:
                surface_z = float(
                    get_height(int(math.floor(home_x)), int(math.floor(home_y)))
                )
            except (TypeError, ValueError, RuntimeError):
                surface_z = None

        # Compatibility with lightweight plugin/test worlds that expose only
        # the entity-space surface helper. ``search=0`` is essential: a return
        # must never move the authored home X/Y to a neighbouring column.
        if surface_z is None:
            surface_anchor = getattr(world, "dry_surface_anchor", None)
            if callable(surface_anchor):
                try:
                    candidate = surface_anchor(home_x, home_y, search=0)
                except TypeError:
                    try:
                        candidate = surface_anchor(home_x, home_y)
                    except Exception:
                        candidate = None
                except Exception:
                    candidate = None
                if candidate is not None:
                    try:
                        surface_z = float(candidate[2])
                    except (TypeError, ValueError, IndexError):
                        surface_z = None

        if surface_z is None or not math.isfinite(surface_z):
            return _unburied_intel_anchor(world, home) or home
        return (
            home_x,
            home_y,
            surface_z - PLAYER_STANDING_POS_ABOVE_GROUND,
        )

    def _base_zone_bounds(self, team: int) -> tuple[int, int, int, int, int, int]:
        """Return the native minimap/capture bounds for one team's base.

        Authored UGC bounds are retained when present. Voxel-only stock maps
        receive the retail classic five-block capture box around the stable
        terrain base anchor. Values are raw voxel coordinates, not fixed-point
        packet values; packet 43 writes these six fields as signed shorts.
        """
        wm = getattr(self.server, "world_manager", None)
        metadata = getattr(wm, "map_metadata", None)
        authored = [] if metadata is None else metadata.base_zones.get(team, [])
        if authored:
            zone = authored[0]
            x0, x1, y0, y1, z0, z1 = zone.extents
            shift = int(getattr(getattr(wm, "map", None), "source_z_shift", 0))
            bounds = (
                zone.x + x0, zone.x + x1,
                zone.y + y0, zone.y + y1,
                zone.z + z0 + shift, zone.z + z1 + shift,
            )
        else:
            x, y, z = self.base_positions[team]
            radius = _FALLBACK_BASE_RADIUS
            bounds = (x - radius, x + radius, y - radius, y + radius, z - 3, z + 6)

        x0, x1, y0, y1, z0, z1 = bounds
        return (
            max(0, min(int(C.MAP_X) - 1, int(round(x0)))),
            max(0, min(int(C.MAP_X) - 1, int(round(x1)))),
            max(0, min(int(C.MAP_Y) - 1, int(round(y0)))),
            max(0, min(int(C.MAP_Y) - 1, int(round(y1)))),
            max(0, min(int(C.MAP_Z) - 1, int(round(z0)))),
            max(0, min(int(C.MAP_Z) - 1, int(round(z1)))),
        )

    def _base_zone_packet(self, team: int):
        """Build the retail packet-43 base zone and its CTF icon billboard."""
        from shared.packet import MinimapZone

        x0, x1, y0, y1, z0, z1 = self.base_bounds[team]
        packet = MinimapZone()
        # The native HUD stores this byte as ``visible_team``. CTF objectives
        # are shared map knowledge: every player needs both coloured bases so
        # they can route a stolen intel home and identify the enemy target.
        # TEAM_NEUTRAL is the retail shared-visibility key; using ``team`` here
        # hid the opposing base and left each side with only half the HUD.
        packet.key = int(TEAM_NEUTRAL)
        packet.color = tuple(int(value) for value in self.server.teams[team].color)
        packet.A2018, packet.A2019 = x0, x1
        packet.A2020, packet.A2021 = y0, y1
        packet.A2022, packet.A2023 = z0, z1
        packet.icon_scale = 1.0
        packet.icon_id = int(CG.ZONE_ICON_CTF)
        packet.locked_in_zone = 0
        return packet

    def _send_base_zones(self, connection=None) -> None:
        """Send both native base zones to all clients or one joining client."""
        for team in (TEAM1, TEAM2):
            data = bytes(self._base_zone_packet(team).generate())
            if connection is None:
                self.server.broadcast(data, reliable=True)
            else:
                connection.send(data, reliable=True)

    def _set_carrier_visibility(self, player, visible: bool, connection=None) -> None:
        """Expose or clear an intel carrier through ChangePlayer action 8.

        Ground intel owns its native type-16 minimap icon. Retail exposes a
        carrier with the high-visibility player marker only after carrying
        for INTEL_MINIMAP_EXPOSURE_TIME (30 s, shared/constants.py; the
        client never reads it, so the timer is server-side) and clears it
        when the intel is dropped or captured.
        """
        from shared.packet import ChangePlayer

        player_id = getattr(player, "id", None)
        if player_id is None:
            return
        packet = ChangePlayer()
        packet.player_id = int(player_id)
        packet.type = int(C.SET_HIGH_MINIMAP_VISIBILITY)
        packet.high_minimap_visibility = int(bool(visible))
        data = bytes(packet.generate())
        if connection is None:
            self.server.broadcast(data, reliable=True)
        else:
            connection.send(data, reliable=True)

    def _is_connected(self, player) -> bool:
        """True while ``player`` still owns its id in the live roster.

        Player ids are reused from the lowest free slot, so a departed carrier
        must never have a player-bound packet (DropPickup, ChangePlayer) sent
        under its id: the client would apply it to nobody or, worse, to the
        next player who took that slot.
        """
        player_id = getattr(player, "id", None)
        if player is None or player_id is None:
            return False
        players = getattr(self.server, "players", None) or {}
        try:
            return players.get(int(player_id)) is player
        except (TypeError, ValueError):
            return False

    def reveal_to(self, connection) -> None:
        """Send CTF-only minimap state after a late joiner's world reveal."""
        super().reveal_to(connection)
        self._send_base_zones(connection)
        for team_id, holder in self.intel_holder.items():
            if (
                holder is not None
                and self._carrier_exposed.get(team_id)
                and self._is_connected(holder)
            ):
                self._set_carrier_visibility(holder, True, connection)
    
    async def on_tick(self, tick: int):
        """Check for intel pickups and captures."""
        await super().on_tick(tick)
        # The base tick may just have ended the match on time. Objectives are
        # frozen for the whole end screen: no pickups, returns or captures may
        # change the score the scoreboard is already showing.
        if self.ended:
            return

        current_time = time.time()

        await self._guard_ground_intel()
        if self.ended:
            return

        if self.intel_auto_return:
            for team in (TEAM1, TEAM2):
                dropped_at = self.intel_drop_time[team]
                if (
                    self.intel_holder[team] is None
                    and dropped_at > 0.0
                    and current_time - dropped_at
                    >= float(CG.CTF_INTEL_RETURN_TIME)
                ):
                    await self._return_intel(team)
        
        for player in list(self.server.players.values()):
            # A capture earlier in this same pass can win the match; stop
            # before a second carrier scores or anyone grabs intel after it.
            if self.ended:
                return
            if not player.alive:
                continue
            
            if player.team not in (TEAM1, TEAM2):
                continue

            if (
                self.intel_return_on_touch
                and self.intel_holder[player.team] is None
                and self.intel_drop_time[player.team] > 0.0
                and self._is_near(
                    player,
                    self.intel_positions[player.team],
                    radius=float(C.PICKUP_DISTANCE),
                )
                and self._sees(player, self.intel_positions[player.team])
            ):
                await self._return_intel(player.team, returned_by=player)

            # Check intel pickup
            enemy_team = TEAM2 if player.team == TEAM1 else TEAM1
            if self.intel_holder[enemy_team] is None:
                # Intel is on ground
                intel_pos = self.intel_positions[enemy_team]
                if self._is_near(
                    player, intel_pos, radius=float(C.PICKUP_DISTANCE)
                ) and self._sees(player, intel_pos):
                    # Check cooldown
                    if current_time - self.intel_drop_time[enemy_team] > self.pickup_cooldown:
                        await self._pickup_intel(player, enemy_team)
            
            # Check intel capture
            if self.intel_holder[enemy_team] == player:
                # Player is holding enemy intel
                own_intel_home = (
                    self.intel_holder[player.team] is None
                    and self.intel_drop_time[player.team] <= 0.0
                )
                if self._is_at_base(player, player.team) and (
                    not self.intel_in_own_base_to_score or own_intel_home
                ):
                    await self._capture_intel(player, enemy_team)

        if not self.ended:
            self._tick_carriers(current_time)

    def _tick_carriers(self, now: float) -> None:
        """Carrier minimap exposure plus CTF_CARRY / CTF_ESCORT payouts.

        Retail: the carrier earns CTF_SCORE_CARRY_SCORE (50) and each living
        teammate escorting it (CTF_ESCORT_RADIUS 20, hysteresis 1) earns
        CTF_SCORE_ESCORT_SCORE (10) every CTF_SCORE_CARRY/ESCORT_INTERVAL
        (5 s) of carrying. The deadline re-arms from ``now``: a stalled tick
        pays once, never a catch-up burst.
        """
        from server.combat_scores import award_score_event

        interval = float(CG.CTF_SCORE_CARRY_INTERVAL)
        exposure = float(C.INTEL_MINIMAP_EXPOSURE_TIME)
        players = tuple(getattr(self.server, "players", {}).values())
        for intel_team in (TEAM1, TEAM2):
            holder = self.intel_holder[intel_team]
            if holder is None or not self._is_connected(holder):
                continue
            if self._carry_started[intel_team] is None:
                self._carry_started[intel_team] = now
                self._carry_next[intel_team] = now + interval
                continue
            if (
                not self._carrier_exposed[intel_team]
                and now - self._carry_started[intel_team] >= exposure
            ):
                self._carrier_exposed[intel_team] = True
                self._set_carrier_visibility(holder, True)
            if now < self._carry_next[intel_team]:
                continue
            self._carry_next[intel_team] = now + interval
            if not bool(getattr(holder, "alive", True)):
                continue
            award_score_event(
                self.server, holder, int(CG.CTF_SCORE_CARRY_SCORE),
                int(C.SCORE_REASON.CTF_CARRY_SCORE_REASON), mode=self,
            )
            for escort in self._escorts.escorts(holder, players):
                award_score_event(
                    self.server, escort, int(CG.CTF_SCORE_ESCORT_SCORE),
                    int(C.SCORE_REASON.CTF_ESCORT_SCORE_REASON), mode=self,
                )

    # Retail objective kill events (see server.combat_scores).
    _KILL_EVENT_AMOUNTS = {
        "intercept": int(CG.CTF_SCORE_INTERCEPT),
        "carrier_defend": int(CG.CTF_SCORE_CARRIER_DEFEND),
        "defend": int(CG.CTF_SCORE_DEFEND),
        "assault": int(CG.CTF_SCORE_ASSAULT),
        "assault_enemy": int(CG.CTF_SCORE_ASSAULT_ENEMY),
        "distract": int(CG.CTF_SCORE_DISTRACT),
    }
    _KILL_EVENT_REASONS = {
        "intercept": int(C.SCORE_REASON.CTF_INTERCEPT_SCORE_REASON),
        "carrier_defend": int(C.SCORE_REASON.CTF_CARRIER_DEFEND_SCORE_REASON),
        "defend": int(C.SCORE_REASON.CTF_DEFEND_SCORE_REASON),
        "assault": int(C.SCORE_REASON.CTF_ASSAULT_SCORE_REASON),
        "assault_enemy": int(C.SCORE_REASON.CTF_ASSAULT_ENEMY_SCORE_REASON),
        "distract": int(C.SCORE_REASON.CTF_DISTRACT_SCORE_REASON),
    }

    def _ground_intel(self, team: int):
        """Position of ``team``'s intel while it is on the ground, else None."""
        if self.intel_holder.get(team) is not None:
            return None
        return self.intel_positions.get(team)

    def _award_kill_events(self, victim, killer, kill_type: int) -> None:
        """Objective kill events, evaluated BEFORE the victim drops intel.

        Flag Intercept: kill the enemy carrying your intel. Flag Carrier
        Defend: kill an enemy near your carrier (CTF_CARRIER_THREAT_RADIUS
        10). Flag Defend: kill an enemy near your ground intel; Close to
        Flag / Flag Assault: kill while near / kill a defender near the
        enemy's ground intel (CTF_THREAT_RADIUS 20). Flag Distraction: the
        victim died to an enemy within CTF_ESCORT_RADIUS of its own carrier.
        """
        from server import combat_scores as cs

        if self.ended or not cs.eligible_kill(killer, victim, kill_type):
            return
        killer_team, victim_team = int(killer.team), int(victim.team)
        event = cs.classify_objective_kill(
            killer, victim,
            victim_carrying=self.intel_holder.get(killer_team) is victim,
            killer_team_carriers=[
                holder for holder in (self.intel_holder.get(victim_team),)
                if holder is not None and getattr(holder, "team", None) == killer_team
            ],
            defend_points=[p for p in (self._ground_intel(killer_team),) if p],
            attack_points=[p for p in (self._ground_intel(victim_team),) if p],
            carrier_threat_radius=float(CG.CTF_CARRIER_THREAT_RADIUS),
            threat_radius=float(CG.CTF_THREAT_RADIUS),
        )
        cs.award_kill_event(
            self.server, killer, event,
            self._KILL_EVENT_AMOUNTS, self._KILL_EVENT_REASONS, mode=self,
        )
        victim_carriers = [
            holder for holder in (self.intel_holder.get(killer_team),)
            if holder is not None and getattr(holder, "team", None) == victim_team
        ]
        if cs.is_distraction(
            victim, killer, kill_type, victim_carriers, float(CG.CTF_ESCORT_RADIUS)
        ):
            cs.award_kill_event(
                self.server, victim, "distract",
                self._KILL_EVENT_AMOUNTS, self._KILL_EVENT_REASONS, mode=self,
            )

    def configure_initial_info(self, packet) -> None:
        """Keep the client carrier weapon gate equal to server authority."""

        packet.allow_shooting_holding_intel = int(self.shoot_with_intel)
    
    async def _pickup_intel(self, player: 'Player', intel_team: int):
        """Player picks up intel."""
        from server.pickups import broadcast_pickup
        if self.ended:
            return
        if not broadcast_pickup(
            self.server, player, int(C.INTEL_PICKUP),
            burdensome=True, state=intel_team,
        ):
            return
        from server import achievements

        achievements.intel_picked_up(self.server, intel_team)
        # "First to Claim Flag" (reason 52, CTF_SCORE_CLAIM = 100): the first
        # grab of an intel resting at its home, once per home cycle (a
        # dropped intel re-taken in the field pays nothing).
        from_home = (
            float(self.intel_drop_time.get(intel_team, 0.0) or 0.0) == 0.0
            and tuple(self.intel_positions.get(intel_team, ()) or ())
            == tuple(self.intel_home_positions.get(intel_team, ()) or ())
        )
        self.intel_holder[intel_team] = player
        self.intel_drop_time[intel_team] = 0.0
        if from_home:
            self._award_player_score(
                player,
                int(CG.CTF_SCORE_CLAIM),
                int(C.SCORE_REASON.CTF_CLAIM_SCORE_REASON),
            )
        self.server.teams[intel_team].pick_up_intel(player)
        self._set_intel_entity(intel_team, False)
        # The high-minimap marker waits for INTEL_MINIMAP_EXPOSURE_TIME; the
        # carry clock starts on the next tick (see _tick_carriers).
        self._clear_carry_state(intel_team)
        # No invulnerable intel runs: grabbing the objective ends protection.
        objective_guard.end_spawn_protection_for_objective(self.server, player)
        
        team_name = self.server.teams[intel_team].name
        # Retail team-relative trio: carrier, carrier's team, intel's owners.
        self.announce_localised_to_player(player, "CTF_YOU_HAVE_FLAG")
        self.announce_localised_to_team(
            int(player.team), "CTF_TEAM_HAS_FLAG", (str(player.name),), exclude=player
        )
        self.announce_localised_to_team(
            intel_team, "CTF_ENEMY_HAS_FLAG", (str(player.name),)
        )
        # The client plays its own positioned pickup sound for the bomb and
        # the diamond only; the intel has none, so the server sends
        # CLASSIC_PICKUP at the carrier (docs/SOUNDS_RETAIL.md).
        from server.audio import SND_CLASSIC_PICKUP, play_sound

        play_sound(
            self.server,
            SND_CLASSIC_PICKUP,
            position=(float(player.x), float(player.y), float(player.z)),
        )
        
        logger.info(f"{player.name} picked up {team_name} intel")
    
    async def _capture_intel(self, player: 'Player', intel_team: int):
        """Player captures intel."""
        from server.pickups import broadcast_drop
        if self.ended:
            return
        broadcast_drop(
            self.server, player,
            (player.x, player.y, player.z), (0.0, 0.0, 0.0),
        )
        if self._clear_carry_state(intel_team):
            self._set_carrier_visibility(player, False)
        self.intel_holder[intel_team] = None
        
        # Reset intel to base
        home_pos = self._accessible_home_anchor(intel_team)
        self.intel_drop_time[intel_team] = 0.0
        self._move_ground_intel(intel_team, home_pos)
        
        # Add score
        player.captures += 1
        self._award_player_score(
            player,
            int(CG.CTF_INDIVIDUAL_SCORE_FOR_CAPTURED_INTEL),
            int(C.SCORE_REASON.CTF_CAPTURE_SCORE_REASON),
        )
        capturing_team = self.server.teams[player.team]
        capturing_team.add_capture()
        # Push the new team score to the HUD (CTF never did this, so the
        # score bar stayed frozen at its spawn value).
        try:
            self.server.broadcast_set_score(
                capturing_team,
                reason=int(C.SCORE_REASON.CTF_CAPTURE_SCORE_REASON),
            )
        except TypeError:
            # Compatibility with plugin/test facades predating score reasons.
            self.server.broadcast_set_score(capturing_team)

        # Check for win
        winning = capturing_team.score >= self.score_limit
        
        team_name = self.server.teams[intel_team].name
        self.announce_localised_to_team(
            int(player.team), "CTF_TEAM_SCORE", (str(player.name),)
        )
        self.announce_localised_to_team(
            intel_team, "CTF_ENEMY_SCORE", (str(player.name),)
        )
        from server.audio import play_team_relative

        play_team_relative(self.server, int(player.team))
        
        logger.info(f"{player.name} captured {team_name} intel")
        
        if winning:
            await self._end_by_score(player.team)
    
    async def on_player_death(self, player: 'Player', killer: Optional['Player'], kill_type: int):
        """Drop intel if player was holding it."""
        await super().on_player_death(player, killer, kill_type)
        self._award_kill_events(player, killer, kill_type)
        for team_id in (TEAM1, TEAM2):
            if self.intel_holder[team_id] == player:
                from server import achievements

                achievements.intel_carrier_killed(
                    self.server, killer, player, team_id, kill_type
                )
                await self._drop_intel(player, team_id)
                break

    # Per-kill scoring is BaseMode.on_player_kill's generic retail score
    # (GENERIC_SCORE_KILL/HEADSHOT/MELEE/SUICIDE/TEAMKILL). CTF and Classic
    # CTF add only their objective bonuses (capture, touch-return) on top, so
    # there is deliberately no on_player_kill override here.

    async def _drop_intel(self, player: 'Player', intel_team: int,
                          position=None, velocity=None, *, forced: bool = True):
        """Release a carried intel onto the ground.

        ``forced`` drops (death, disconnect, team change) always release the
        objective, even when the carrier has already left the roster or its
        native pickup state was lost: a holder that can never drop would
        freeze that intel for the rest of the match. Only a voluntary packet-71
        drop with rejected vectors is ignored.
        """
        from server.pickups import broadcast_drop
        if position is None:
            position = (player.x, player.y, player.z)
        if velocity is None:
            velocity = (
                float(getattr(player, "vx", 0.0)),
                float(getattr(player, "vy", 0.0)),
                float(getattr(player, "vz", 0.0)),
            )
        connected = self._is_connected(player)
        dropped = (
            broadcast_drop(self.server, player, position, velocity)
            if connected else None
        )
        if dropped is not None:
            drop_x, drop_y, drop_z = dropped[2]
        elif not forced:
            return
        else:
            # Departed (or desynced) carrier: settle the objective without any
            # packet naming its stale, possibly already reused, player id.
            try:
                drop_x, drop_y, drop_z = (
                    float(position[0]),
                    float(position[1]),
                    float(position[2]),
                )
            except (TypeError, ValueError, IndexError):
                drop_x, drop_y, drop_z = (
                    float(player.x),
                    float(player.y),
                    float(player.z),
                )
            player.pickup_id = None
            player.pickup_burdensome = False
            player.pickup_state = None
        if self._clear_carry_state(intel_team) and connected:
            self._set_carrier_visibility(player, False)
        self.intel_holder[intel_team] = None
        
        # DropPickup removes the carried native tool but does not create a
        # persistent entity. Settle the authoritative type-16 objective on the
        # first support BELOW the submitted height and explicitly CreateEntity
        # it for every client. A roof above the carrier is not a landing point.
        drop_pos = _falling_ground_anchor(
            self.server,
            drop_x,
            drop_y,
            drop_z,
        )
        self.intel_positions[intel_team] = drop_pos
        self.intel_drop_time[intel_team] = time.time()
        
        self.server.teams[intel_team].drop_intel(*drop_pos)
        self._set_intel_entity(
            intel_team,
            True,
            broadcast=True,
            entity_position=_intel_entity_anchor(drop_pos),
        )
        
        team_name = self.server.teams[intel_team].name
        # Retail has no drop announcement; the intel entity reappearing is the cue.
        logger.info(
            "%s dropped %s intel at %s%s", getattr(player, "name", "?"),
            team_name, drop_pos, "" if connected else " (departed)",
        )

    async def _return_intel(self, intel_team: int, returned_by=None) -> None:
        """Return ground intel and award the recovered touch-return point."""
        home_pos = self._accessible_home_anchor(intel_team)
        self.intel_drop_time[intel_team] = 0.0
        self._move_ground_intel(intel_team, home_pos)
        from server import achievements

        achievements.intel_returned(self.server, intel_team)
        if returned_by is not None:
            # Retail has no "returned the flag" score reason; the +1
            # CTF_INDIVIDUAL_SCORE_FOR_RETURNING_INTEL is paid unlabelled
            # (NO_SCORE_REASON) instead of as "First to Claim Flag".
            self._award_player_score(
                returned_by,
                int(CG.CTF_INDIVIDUAL_SCORE_FOR_RETURNING_INTEL),
                int(C.SCORE_REASON.NO_SCORE_REASON),
            )
        team_name = self.server.teams[intel_team].name
        # FLAG_RETURNED for every return (touch or timer): the intel jumping
        # home is a world event, even where retail printed no line for it.
        from server.audio import SND_FLAG_RETURNED, play_sound

        play_sound(self.server, SND_FLAG_RETURNED)
        returner = str(getattr(returned_by, "name", "") or "")
        if returner:
            self.announce_localised(
                "CTF_FLAG_RETURNED", (returner, team_name), localise_parameters=True
            )
        logger.info("%s intel returned (by=%s)", team_name,
                    getattr(returned_by, "name", "timer"))

    def _award_player_score(self, player, amount: int, reason: int) -> None:
        """Apply and replicate one native CTF personal-score event."""

        if amount <= 0 or not self._owns_slot(player):
            return
        player.score = int(getattr(player, "score", 0)) + int(amount)
        from server.scoreboard import send_player_score

        send_player_score(self.server, player, reason=int(reason))

    async def handle_drop_pickup(self, player, position, velocity) -> bool:
        """Packet-71 mode hook; only the actual enemy-intel holder may drop."""
        for team_id in (TEAM1, TEAM2):
            if self.intel_holder[team_id] is player:
                await self._drop_intel(
                    player, team_id, position, velocity, forced=False
                )
                return True
        return False
    
    async def on_player_leave(self, player: 'Player'):
        """Handle player leaving with intel."""
        for team_id in (TEAM1, TEAM2):
            if self.intel_holder[team_id] == player:
                await self._drop_intel(player, team_id)
                break
    
    async def on_player_team_change(self, player: 'Player', old_team: int, new_team: int):
        """Handle player changing team while holding intel."""
        # Scan both holders: ``old_team`` may be the spectator roster, and the
        # carrier's queued death has usually released the intel already.
        for team_id in (TEAM1, TEAM2):
            if self.intel_holder[team_id] is player:
                await self._drop_intel(player, team_id)
                break

    async def on_blocks_built(self, player, positions) -> None:
        """Lift an intel immediately when a committed block entombs it."""
        if self.ended:
            return
        columns = set()
        for position in positions:
            try:
                columns.add((int(position[0]), int(position[1])))
            except (TypeError, ValueError, IndexError):
                continue
        if not columns:
            return
        for team in (TEAM1, TEAM2):
            if self.intel_holder[team] is not None:
                continue
            x, y, _z = self.intel_positions[team]
            if (int(math.floor(x)), int(math.floor(y))) not in columns:
                continue
            if self._raise_buried_intel(team):
                logger.info(
                    "%s intel raised out of a built block",
                    self.server.teams[team].name,
                )
    
    def _is_near(self, player: 'Player', pos: Tuple[float, float, float], radius: float) -> bool:
        """Check if player is within radius of a position."""
        dx = player.x - pos[0]
        dy = player.y - pos[1]
        dz = player.z - pos[2]
        dist_sq = dx*dx + dy*dy + dz*dz
        return dist_sq <= radius * radius

    def _is_at_base(self, player: 'Player', team: int) -> bool:
        """Check the same visible base box used by the packet-43 HUD zone.

        Anti-abuse: when the base floor has been dug out into a pit, a
        carrier standing in that pit (open sky above, at most
        ``ctf_base_pit_depth`` blocks below the base, default 24) still
        scores, so defenders cannot make a capture impossible by
        excavating their own base. Tunnels under the base (solid overhead)
        do not count.
        """
        x0, x1, y0, y1, _z0, _z1 = self.base_bounds[team]
        base_z = self.base_positions[team][2]
        # Stock server-only BASE_ZONE_DISTANCE_TOLERANCE (A2287 = 0.5): the
        # base box is forgiving by half a block (the client's own HUD test
        # uses BASE_PLAYER_ZONE_DISTANCE_TOLERANCE_XY 0.3).
        tolerance = float(getattr(C, "BASE_ZONE_DISTANCE_TOLERANCE", 0.5))
        if not (
            x0 - tolerance <= float(player.x) <= x1 + tolerance
            and y0 - tolerance <= float(player.y) <= y1 + tolerance
        ):
            return False
        dz = float(player.z) - float(base_z)
        if abs(dz) <= 6.0:
            return True
        pit_depth = float(getattr(self.server.config, "ctf_base_pit_depth", 24.0))
        if not (6.0 < dz <= pit_depth):
            return False
        wm = getattr(self.server, "world_manager", None)
        if wm is None or getattr(wm, "map", None) is None:
            return False
        try:
            top = objective_guard._column_top(
                wm, int(float(player.x)), int(float(player.y))
            )
        except Exception:
            return False
        # Open sky: the column's first solid voxel is at or below the head.
        return top >= int(float(player.z))

    # ------------------------------------------------------------------
    # Anti-abuse: LOS pickups, buried/floating intel, escape-watch hooks
    # ------------------------------------------------------------------

    def _sees(self, player, intel_pos) -> bool:
        return objective_guard.pickup_line_of_sight(
            self.server, player, intel_pos, player_space=True
        )

    def mode_marks_player(self, player) -> bool:
        """Only an exposed carrier carries this mode's marker; before the
        exposure delay the escape watch may still reveal an escaper."""
        return any(
            holder is player and self._carrier_exposed.get(team_id)
            for team_id, holder in self.intel_holder.items()
        )

    def escape_watch_objective_player(self, player) -> bool:
        return any(holder is player for holder in self.intel_holder.values())

    async def _guard_ground_intel(self) -> None:
        """Keep ground intel obtainable.

        The cheap occupied-cell check runs every game tick so blocks placed by
        any mutation path raise the intel through their connected stack. The
        heavier sealed/floating checks stay at 1 Hz and recover after
        ``objective_entomb_seconds`` (default 5): a dropped intel returns home
        when auto return is on; otherwise it resettles.
        """
        wm = getattr(self.server, "world_manager", None)
        if wm is None or getattr(wm, "map", None) is None:
            return

        # Not every construction source emits ``on_blocks_built`` (prefabs and
        # other direct terrain mutations are examples). Keep this before the
        # 1 Hz throttle so they cannot leave an intel embedded in a new block.
        for team in (TEAM1, TEAM2):
            if self._raise_buried_intel(team):
                logger.info(
                    "%s intel buried; raised through connected stack",
                    self.server.teams[team].name,
                )

        now = time.monotonic()
        if now < getattr(self, "_guard_next_at", 0.0):
            return
        self._guard_next_at = now + 1.0
        timer = getattr(self, "_intel_trap", None)
        if timer is None:
            timer = self._intel_trap = objective_guard.TrapTimer()
        grace = objective_guard.entomb_seconds(self.server)
        for team in (TEAM1, TEAM2):
            if self.intel_holder[team] is not None:
                timer.due(team, False, now, grace)
                continue
            position = self.intel_positions[team]
            reason = objective_guard.ground_objective_trapped(
                wm, position, player_space=True
            )
            if reason == "buried" and self._raise_buried_intel(team):
                logger.info(
                    "%s intel buried; raised through connected stack",
                    self.server.teams[team].name,
                )
                continue
            if not timer.due(team, reason is not None, now, grace):
                continue
            dropped = self.intel_drop_time[team] > 0.0
            logger.info("%s intel %s; recovering", self.server.teams[team].name, reason)
            if dropped and self.intel_auto_return:
                await self._return_intel(team)
                continue
            try:
                settled = objective_guard.resettle_surface(
                    wm, position, player_space=True
                )
            except Exception:
                logger.exception("intel resettle failed")
                continue
            self._move_ground_intel(team, settled)
