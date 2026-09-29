"""Server-side state behind the retail KillAction presentation fields.

The stock client (``GameScene.process_packet_kill_action``, gameScene.pyd
0x10194940, see docs/KILLFEED_RETAIL.md) decides nothing itself: it shows
the multikill banner from ``kill_count`` (2 ``KILL2`` .. 5 ``KILL5``, above
that ``KILLM``) and the domination/revenge banners, sounds and scoreboard
flags from ``isDominationKill`` / ``isRevengeKill``. The server owns:

* ``kill_count``: kills chained with gaps of at most ``MULTIKILLMAXTIMEGAP``
  (6.0 s). That constant is in shared/constants.py but referenced by no
  client binary, so it is the retail server's multikill window.
* domination: ``DOMINATION_KILLS`` unanswered kills on one enemy. The retail
  threshold is not recoverable from the client; 4 is the established
  convention for the domination/revenge pair this HUD implements.
* revenge: killing an enemy who is dominating you. The client clears the
  victim's ``dominatingLocalPlayer`` on it, so the pair is the
  domination/revenge model, not "kill your last killer".
"""

from __future__ import annotations

import shared.constants as C

MULTIKILL_MAX_TIME_GAP = float(getattr(C, "MULTIKILLMAXTIMEGAP", 6.0))
DOMINATION_KILLS = 4

# The client resets both players' domination flags on these kill types
# (process_packet_kill_action lines 3671-3678: A429/A430). CLASS_CHANGE_KILL
# is deliberately not in the client's test.
DOMINATION_RESET_KILL_TYPES = frozenset(
    (int(C.FORCED_TEAM_CHANGE_KILL), int(C.TEAM_CHANGE_KILL))
)


# HUD.add_kill (hud.pyd 0x1008CD70) icon per kill type, in the client's own
# test order. "weapon" is the killer's currently held tool (get_weapon /
# get_tool().tool_id); ROCKET_KILL shows RocketTurretWeapon when the killer's
# class loadout holds ROCKET_TURRET_TOOL, else RPGWeapon. ENTITY_KILL logs
# "no image for entity kill"; types missing here get no icon at all.
HUD_KILL_ICONS: dict[int, str] = {
    int(C.WEAPON_KILL): "weapon",
    int(C.MELEE_KILL): "weapon",
    int(C.GRENADE_KILL): "GrenadeTool",
    int(C.CLASSIC_GRENADE_KILL): "ClassicGrenadeTool",
    int(C.ANTIPERSONNEL_GRENADE_KILL): "AntipersonnelGrenadeTool",
    int(C.ROCKET_KILL): "RPGWeapon|RocketTurretWeapon",
    int(C.ROCKET2_KILL): "RPG2Weapon",
    int(C.DRILL_KILL): "DrillgunWeapon",
    int(C.HEADSHOT_KILL): "KILL_IMAGES['headshot']",
    int(C.ENTITY_KILL): "<none: 'no image for entity kill'>",
    int(C.CORPSE_KILL): "KILL_IMAGES['corpse']",
    int(C.GRAVE_KILL): "KILL_IMAGES['grave']",
    int(C.ROCKET_TURRET_KILL): "RocketTurretWeapon",
    int(C.LANDMINE_KILL): "LandmineWeapon",
    int(C.DYNAMITE_KILL): "DynamiteWeapon",
    int(C.AIRSTRIKE_KILL): "KILL_IMAGES['airstrike']",
    int(C.BOMB_KILL): "BombTool",
    int(C.SHRAPNEL_KILL): "KILL_IMAGES['shrapnel']",
    int(C.FALL_KILL): "KILL_IMAGES['fall']",
    int(C.TEAM_CHANGE_KILL): "KILL_IMAGES['change_team']",
    int(C.FORCED_TEAM_CHANGE_KILL): "KILL_IMAGES['change_team']",
    int(C.CLASS_CHANGE_KILL): "KILL_IMAGES['change_class']",
    int(C.SNOWBALL_KILL): "SnowBlowerWeapon",
    int(C.MOLOTOV_KILL): "MolotovWeapon",
    int(C.BLOCKFIRE_KILL): "MolotovWeapon",
    int(C.VIP_MODE_KILL): "KILL_IMAGES['sudden_death']",
    int(C.GRENADE_LAUNCHER_KILL): "GrenadeLauncherWeapon",
    int(C.RADAR_STATION_KILL): "RadarStationWeapon",
    int(C.STICKY_GRENADE_KILL): "StickyGrenadeWeapon",
    int(C.C4_KILL): "C4Weapon",
    int(C.MINE_KILL): "MineLauncherWeapon",
    int(C.CHEMICALBOMB_KILL): "ChemicalBombWeapon",
}


def _unanswered(player) -> dict:
    table = getattr(player, "_unanswered_kills", None)
    if not isinstance(table, dict):
        table = {}
        player._unanswered_kills = table
    return table


def _dominating(player) -> set:
    victims = getattr(player, "_dominating_ids", None)
    if not isinstance(victims, set):
        victims = set()
        player._dominating_ids = victims
    return victims


def register_multikill(killer, now: float) -> int:
    """Count one credited kill into ``killer``'s multikill chain."""

    last = getattr(killer, "_multikill_last_at", None)
    count = int(getattr(killer, "_multikill_count", 0) or 0)
    if (
        last is None
        or count <= 0
        or float(now) - float(last) > MULTIKILL_MAX_TIME_GAP
    ):
        count = 0
    count = min(255, count + 1)
    killer._multikill_count = count
    killer._multikill_last_at = float(now)
    return count


def reset_multikill(player) -> None:
    player._multikill_count = 0
    player._multikill_last_at = None


def register_domination(killer, victim) -> tuple[bool, bool]:
    """Record an enemy kill; return ``(is_domination, is_revenge)``."""

    killer_id = int(killer.id)
    victim_id = int(victim.id)
    is_revenge = killer_id in _dominating(victim)
    if is_revenge:
        _dominating(victim).discard(killer_id)
    # The victim's run of unanswered kills on the killer is over.
    _unanswered(victim).pop(killer_id, None)

    kills = _unanswered(killer)
    kills[victim_id] = int(kills.get(victim_id, 0)) + 1
    is_domination = False
    if (
        kills[victim_id] >= DOMINATION_KILLS
        and victim_id not in _dominating(killer)
    ):
        _dominating(killer).add(victim_id)
        is_domination = True
    return is_domination, is_revenge


def is_dominating(killer, victim) -> bool:
    return int(victim.id) in _dominating(killer)


def clear_player(server, player) -> None:
    """Drop every domination relation involving ``player`` (both ways)."""

    player_id = int(player.id)
    _unanswered(player).clear()
    _dominating(player).clear()
    players = getattr(server, "players", None)
    others = players.values() if isinstance(players, dict) else ()
    for other in others:
        if other is player:
            continue
        table = getattr(other, "_unanswered_kills", None)
        if isinstance(table, dict):
            table.pop(player_id, None)
        victims = getattr(other, "_dominating_ids", None)
        if isinstance(victims, set):
            victims.discard(player_id)


def forget_player(server, player) -> None:
    """A departing id is reused at once: no relation may survive it."""

    clear_player(server, player)
    reset_multikill(player)


def reset_all(server) -> None:
    players = getattr(server, "players", None)
    for player in (players.values() if isinstance(players, dict) else ()):
        _unanswered(player).clear()
        _dominating(player).clear()
        reset_multikill(player)
