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


def _allowed_for(code: str) -> tuple[int, ...]:
    classic = code == 'cctf'
    mafia = code in ('tc', 'vip')
    if classic:
        return tuple(int(x) for x in C.CLASSIC_TEAM_CLASSES)
    if mafia:
        return tuple(int(x) for x in C.MAFIA_TEAM_CLASSES)
    if code == 'zom':
        # Survivors get default classes; zombies are a separate team.
        return tuple(int(x) for x in C.DEFAULT_TEAM_CLASSES)
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
        default_score_limit={
            'ctf': 10, 'cctf': 10, 'tdm': 50, 'dem': 5, 'mh': 100,
            'oc': 100, 'dia': 10, 'tc': 100, 'vip': 5, 'zom': 1,
            'ugc': 0,
        }.get(code, 10),
        default_time_limit={
            'ctf': 1200.0, 'cctf': 1200.0, 'tdm': 600.0, 'dem': 300.0,
            'mh': 900.0, 'oc': 600.0, 'dia': 600.0, 'tc': 900.0,
            'vip': 300.0, 'zom': 600.0, 'ugc': 0.0,
        }.get(code, 600.0),
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
    return MODES.get(code, MODES['nor'])
