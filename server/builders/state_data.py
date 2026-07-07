"""Builder for StateData(45) — the per-spawn snapshot of game/team state.

Drives mode_type, score limits, team class lists, lighting, etc. from the
active mode + config + map metadata. Replaces the hardcoded version that
lived in connection.py (which assumed CTF + a specific lighting setup).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from shared.packet import StateData

from server import mode_data
from server.game_constants import TEAM1, TEAM2

if TYPE_CHECKING:
    from server.main import BattleSpadesServer


# These visual constants come from the original London-style preset and
# work for most maps. Per-map skybox/light overrides belong in map metadata
# parsing once we wire that up — for now serve consistent values.
_DEFAULT_LIGHT_COLOR = (180, 192, 220)
_DEFAULT_LIGHT_DIR = (0.203125, 0.796875, 0.0)
_DEFAULT_BACK_LIGHT_COLOR = (64, 64, 64)
_DEFAULT_BACK_LIGHT_DIR = (-0.078125, -0.578125, 0.296875)
_DEFAULT_AMBIENT_COLOR = (52, 56, 64)
_DEFAULT_AMBIENT_INTENSITY = 0.203125

# `team_headcount_type` controls UI rendering of team sizes. 6 mirrors the
# original CTF default; it's safe for most modes.
_DEFAULT_TEAM_HEADCOUNT_TYPE = 6


def build_state_data(server: 'BattleSpadesServer',
                      player_id: int = -1) -> StateData:
    cfg = server.config
    mode = mode_data.get(cfg.game_mode)

    pkt = StateData()
    pkt.player_id = player_id if player_id >= 0 else 0

    # ---- World lighting / skybox visuals -------------------------------
    pkt.fog_color = cfg.fog_color
    pkt.gravity = 1.0
    pkt.light_color = _DEFAULT_LIGHT_COLOR
    pkt.light_direction = _DEFAULT_LIGHT_DIR
    pkt.back_light_color = _DEFAULT_BACK_LIGHT_COLOR
    pkt.back_light_direction = _DEFAULT_BACK_LIGHT_DIR
    pkt.ambient_light_color = _DEFAULT_AMBIENT_COLOR
    pkt.ambient_light_intensity = _DEFAULT_AMBIENT_INTENSITY
    pkt.time_scale = 1.0

    # ---- Mode metadata --------------------------------------------------
    # Prefer the ACTIVE mode instance's score_limit (the rules class) so the
    # HUD limit and the win threshold are always the same number; fall back to
    # the config / mode-data default before the mode is constructed.
    active_limit = getattr(getattr(server, 'mode', None), 'score_limit', None)
    if active_limit is None:
        active_limit = getattr(cfg, 'score_limit', None) or mode.default_score_limit
    pkt.score_limit = int(active_limit)
    pkt.mode_type = mode.mode_id
    pkt.team_headcount_type = _DEFAULT_TEAM_HEADCOUNT_TYPE

    # ---- Teams ----------------------------------------------------------
    team1 = server.teams[TEAM1]
    team2 = server.teams[TEAM2]
    pkt.team1_name = team1.name
    pkt.team1_color = team1.color
    pkt.team1_score = int(getattr(team1, 'score', 0))
    pkt.team1_classes = list(mode.allowed_classes) or []

    pkt.team2_name = team2.name
    pkt.team2_color = team2.color
    pkt.team2_score = int(getattr(team2, 'score', 0))
    pkt.team2_classes = list(mode.allowed_classes) or []

    # Show the on-screen team score bar (TDM/CTF are score races). Without
    # these the compiled client hides the score even though it receives it.
    pkt.team1_show_score = True
    pkt.team2_show_score = True
    pkt.team1_show_max_score = True
    pkt.team2_show_max_score = True

    # ---- Prefabs / entities --------------------------------------------
    # StateData.prefabs is the MAP-SPECIFIC prefab set (client map_prefabs);
    # the class-select screen appends each entry to EVERY class's prefab list
    # as a MAP_PREFAB and looks its image up by name in the prefab palette.
    # A bogus/hardcoded name (the old 'supertower') therefore injected a
    # wrong/overflowing entry into every class. Ship none — each class's real
    # prefabs come from the client's local CLASS_ITEMS/PREFAB_LISTS.
    pkt.prefabs = []
    # The join handshake NEVER carries entities. Cramming crates into the
    # join-time StateData makes the compiled client process them mid-
    # GameScene-transition (world still building) and crash natively at
    # "delete ugc palette". Entities are delivered AFTER spawn via per-player
    # CreateEntity (server.schedule_entity_reveal); see _process_entity_reveals.
    pkt.entities = []
    pkt.screenshot_cameras_points = [(0.0, 0.0, 0.0)]
    pkt.screenshot_cameras_rotations = [(0.0, 0.0, 0.0)]
    pkt.has_map_ended = 0
    return pkt
