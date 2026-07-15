"""Per-mode wire data — mode_type byte, default score limit, allowed
classes, mode title strings.

The original protocol's InitialInfo carries `mode_key` and StateData carries
`mode_type` — both are mode-id bytes the client uses to select UI/audio/etc.
We source these from `shared.constants_gamemode` (`MODE_*` ints + the
`MODE_MAP_TITLES` string-id table).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import shared.constants as C
import shared.constants_gamemode as MG


@dataclass(frozen=True)
class ModeData:
    """Static data for a game mode — mostly wire-format defaults the
    client expects in InitialInfo / StateData."""
    code: str           # short code like 'ctf', 'tdm', 'zom'
    mode_id: int        # MODE_NORMAL=0, MODE_DEMOLITION=1, ... MODE_UGC=12
    title_string: str   # localized string id, e.g. 'CTF_TITLE'
    description_string: str
    infographic1: str = ''
    infographic2: str = ''
    infographic3: str = ''
    default_score_limit: int = 10
    default_time_limit: float = 1200.0   # 20 min
    classic: bool = False
    mafia: bool = False
    allowed_classes: tuple[int, ...] = field(default_factory=tuple)


def _all_classes() -> tuple[int, ...]:
    """Every MENU-SELECTABLE class. DLC-gated classes still appear (the client
    greys them via its own dlc_manager if not owned).

    EXCLUDES FAST_ZOMBIE(14) and JUMP_ZOMBIE(15): the client's
    global_images.class_icons has NO icon for those two zombie-mode-internal
    variants, so listing them in a team's class_list makes selectClass.py crash
    with KeyError when it renders the class picker (verified live 2026-07-08).
    """
    from server.class_data import CLASS_IDS
    excluded = {int(C.CLASS_FAST_ZOMBIE), int(C.CLASS_JUMP_ZOMBIE)}
    return tuple(int(x) for x in CLASS_IDS if int(x) not in excluded)


def _allowed_for(code: str) -> tuple[int, ...]:
    classic = code == 'cctf'
    mafia = code in ('tc', 'vip')
    if classic:
        return tuple(int(x) for x in C.CLASSIC_TEAM_CLASSES)
    if mafia:
        return tuple(int(x) for x in C.MAFIA_TEAM_CLASSES)
    if code == 'tdm':
        # Deathmatch: every class available (incl. DLC), per user request.
        return _all_classes()
    if code == 'zom':
        # Zombie mode has asymmetric class menus.  This union is used by
        # InitialInfo.disabled_classes; ZombieMode.configure_state_data splits
        # it into survivor and infected lists for the two teams.  Fast/Jump
        # Zombie have no ordinary class-picker icons in this retail build, so
        # exposing them here crashes selectClass.py instead of adding choices.
        survivors = tuple(int(x) for x in C.DEFAULT_TEAM_CLASSES)
        legacy_rocketeer = (int(C.CLASS_ROCKETEER),)
        return tuple(dict.fromkeys(survivors + legacy_rocketeer + (
            int(C.CLASS_ZOMBIE),
        )))
    if code == 'ugc':
        return tuple(int(x) for x in C.UGC_TEAM_CLASSES)
    return tuple(int(x) for x in C.DEFAULT_TEAM_CLASSES)


def _mode_data(code: str) -> ModeData:
    mode_id = int(MG.MODE_MODE_IDS.get(code, MG.MODE_NORMAL))
    title = MG.MODE_MAP_TITLES.get(code, 'TDM_TITLE')
    desc = MG.MODE_DESCRIPTIONS.get(code, '')
    code_upper = code.upper()
    return ModeData(
        code=code,
        mode_id=mode_id,
        title_string=title,
        description_string=desc,
        infographic1='{}_INFOGRAPHIC_TEXT1'.format(code_upper),
        infographic2='{}_INFOGRAPHIC_TEXT2'.format(code_upper),
        infographic3='{}_INFOGRAPHIC_TEXT3'.format(code_upper),
        # Score/win limits. TDM plays to 200 team kills (user spec). CTF to
        # the intel-capture count. Sourced here so wire HUD + rules agree.
        default_score_limit={
            'ctf': 10, 'cctf': 10, 'tdm': 200, 'dem': 5, 'mh': 100,
            'oc': 100, 'dia': 10, 'tc': 100,
            'vip': int(MG.VIP_NOOF_ROUNDS_BEFORE_NEXT_MAP), 'zom': 1,
            'ugc': 0,
        }.get(code, 10),
        # Round clock (seconds). Original game lengths from
        # constants_gamemode.DEFAULT_MODE_GAME_LENGTH (TDM = 900 = 15 min).
        default_time_limit={
            'ctf': 1800.0, 'cctf': 5400.0, 'tdm': 900.0, 'dem': 900.0,
            'mh': 1500.0, 'oc': 900.0, 'dia': 900.0, 'tc': 1500.0,
            'vip': 900.0, 'zom': 600.0, 'ugc': 0.0,
        }.get(code, 900.0),
        classic=(code == 'cctf'),
        mafia=(code in ('tc', 'vip')),
        allowed_classes=_allowed_for(code),
    )


# All 13 modes pre-registered. `get(code)` falls back to NORMAL if unknown.
MODES: dict[str, ModeData] = {
    code: _mode_data(code)
    for code in ('nor', 'ctf', 'cctf', 'tdm', 'dem', 'mh', 'oc',
                  'dia', 'tc', 'vip', 'zom', 'tut', 'ugc')
}


def get(code: str) -> ModeData:
    # Human-facing commands use "zombie" while the retail protocol table uses
    # the historical short code "zom". Resolve aliases before packet builders
    # read mode_id/class data, otherwise the rules object is ZombieMode but the
    # client is incorrectly told MODE_NORMAL.
    normalized = str(code).strip().lower()
    normalized = {
        'zombie': 'zom',
        'classic_ctf': 'cctf',
        'classic-ctf': 'cctf',
    }.get(normalized, normalized)
    return MODES.get(normalized, MODES['nor'])
