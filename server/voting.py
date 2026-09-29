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
from collections import deque
import logging
from pathlib import Path
import random
import struct
import time
import zlib

import shared.constants_gamemode as CG
from shared.packet import GenericVoteMessage


logger = logging.getLogger(__name__)

VOTE_START = 0
VOTE_CAST = 1
VOTE_UPDATE = 2
VOTE_CLOSED = 3

KICK_GRIEFING, KICK_HACKING, KICK_ABUSE, KICK_CANCEL = range(4)
# shared.constants.KICK_REASONS: the string id the vote HUD localises.
KICK_REASON_IDS = {
    KICK_GRIEFING: "KICK_REASON_GRIEFING",
    KICK_HACKING: "KICK_REASON_HACKING",
    KICK_ABUSE: "KICK_REASON_ABUSE",
}
# shared.constants DISCONNECT.ERROR_KICK_GRIEFING/HACKING/ABUSE (23/24/25):
# the stock GameManager.set_big_text_message shows the localised reason for
# these instead of the bare "You have been kicked." of ERROR_KICKED (2).
KICK_DISCONNECT_REASONS = {
    KICK_GRIEFING: 23,
    KICK_HACKING: 24,
    KICK_ABUSE: 25,
}

VOTE_DURATION = 30.0
# Stock TIME_AFTER_MAP_VOTE_START_BEFORE_END (A2502 = 10 s): the map ballot
# opens 10 s before the round's end and closes with it (rules audit
# 2026-09-27 #29; reading of the constant name -- the ballot used to open
# 60 s early and run 15 s).
MAP_VOTE_DURATION = float(getattr(CG, "TIME_AFTER_MAP_VOTE_START_BEFORE_END", 10.0))
MAP_VOTE_LEAD_SECONDS = MAP_VOTE_DURATION
# Retail shared constants (C:5269-5273): MIN_TIME_BETWEEN_KICK_VOTES = 5 * 60
# and MIN_TIME_BETWEEN_CANCELLED_KICK_VOTES = 45. Both are per starter here
# (keyed by address); config [lobby] votekick_*_cooldown_seconds override.
VOTE_COOLDOWN = 300.0
CANCELLED_VOTE_COOLDOWN = 45.0
# KICK_NOT_ENOUGH_PLAYERS: "There must be at least 3 players on a team to
# initiate a kick". Counted on the starter's team (inferred scope).
KICK_MIN_TEAM_PLAYERS = 3

# Server-sent LocalisedMessage(50) denials. The stock KickVotePlayerSelect
# (hud.pyd packet_received) closes itself 0.5 s after it receives
# VOTE_TOO_SOON, VOTE_IN_PROGRESS, FOR_SPECTATOR or NOT_ENOUGH_PLAYERS; the
# client never validates a kick itself (GameScene.initiate_kick just sends 48).
KICK_DENIED_SELF_KICK = "KICK_DENIED_REASON_SELF_KICK"
KICK_DENIED_KICK_HOST = "KICK_DENIED_REASON_KICK_HOST"
KICK_DENIED_VOTE_IN_PROGRESS = "KICK_DENIED_REASON_VOTE_IN_PROGRESS"
KICK_DENIED_VOTE_TOO_SOON = "KICK_DENIED_REASON_VOTE_TOO_SOON"  # {0} seconds
KICK_DENIED_FOR_SPECTATOR = "KICK_DENIED_FOR_SPECTATOR"
KICK_DENIED_NOT_ENOUGH_PLAYERS = "KICK_NOT_ENOUGH_PLAYERS"

# Map-vote fit to the lobby size. A map's playable area is its count of dry,
# standable columns, read from the bot navigation cache (``<map>.botnav``)
# that ships next to every map, so no VXL is parsed. Measured 2026-09-26:
# DragonIsland ~27k, TheColosseum ~44k, London ~69k, CastleWars ~190k,
# WW1 ~259k (of 262144).
MAP_VOTE_AREA_PER_PLAYER = 8000.0
MAP_VOTE_MIN_AREA = 24000.0
MAP_VOTE_RECENT_EXCLUDE = 2
MAP_SIZE_CLASS_AREAS = {"small": 50000, "medium": 130000, "large": 230000}
_FULL_MAP_AREA = 512 * 512
_BOTNAV_HEADER = struct.Struct("<8sHHHH16sII")
_BOTNAV_MAGIC = b"BSNAV01\0"
_BOTNAV_DRY_FLAG = 1 << 1
_DRY_TABLE = bytes(1 if value & _BOTNAV_DRY_FLAG else 0 for value in range(256))
# path -> ((mtime_ns, size), area or None); shared by every VoteManager.
_AREA_CACHE: dict[str, tuple[tuple[int, int], int | None]] = {}


def map_playable_area(maps_root, map_name: str) -> int | None:
    """Dry standable columns of ``map_name`` from its ``.botnav`` cache.

    Returns ``None`` when the cache is missing or unreadable (custom maps
    without a navigation cache are treated as a neutral fit).
    """

    path = Path(maps_root) / f"{map_name}.botnav"
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (int(stat.st_mtime_ns), int(stat.st_size))
    cached = _AREA_CACHE.get(str(path))
    if cached is not None and cached[0] == key:
        return cached[1]
    area: int | None = None
    try:
        data = path.read_bytes()
        (magic, _version, width, height, _water, _digest, _raw,
         _compressed) = _BOTNAV_HEADER.unpack_from(data)
        columns = int(width) * int(height)
        if magic == _BOTNAV_MAGIC and 0 < columns <= _FULL_MAP_AREA:
            # Array order: primary_support, layer_count, flags, ... Only the
            # first three planes are inflated.
            payload = zlib.decompressobj().decompress(
                data[_BOTNAV_HEADER.size:], 3 * columns
            )
            flags = payload[2 * columns:3 * columns]
            if len(flags) == columns:
                area = flags.translate(_DRY_TABLE).count(1)
    except (OSError, struct.error, zlib.error, ValueError):
        area = None
    _AREA_CACHE[str(path)] = (key, area)
    return area


def map_fit_tier(area: int | None, players: float, *, per_player: float,
                 min_area: float) -> int:
    """0 = good fit, 1 = acceptable (or unknown size), 2 = poor fit."""

    if area is None or area <= 0:
        return 1
    ideal = max(float(min_area), min(float(_FULL_MAP_AREA), players * per_player))
    ratio = max(area / ideal, ideal / area)
    if ratio <= 1.75:
        return 0
    if ratio <= 3.0:
        return 1
    return 2


def _retail_localised_text(
    identifier: str, arguments: tuple[object, ...] = ()
) -> str:
    """Encode one string for ``GenericVotingHUD.decode_string``.

    The retail HUD literal-evaluates the field and unconditionally reads both
    tuple indexes: ``value[0]`` is the string-table identifier and ``value[1]``
    is an iterable of format arguments.  A one-item tuple reaches native line
    67 and raises ``IndexError``, terminating the client.  Empty text therefore
    still needs an explicit empty argument tuple.
    """

    identifier = str(identifier)
    if (
        not identifier
        or identifier.upper() != identifier
        or not identifier.replace("_", "").isalnum()
    ):
        raise ValueError("invalid retail localization identifier")
    if not isinstance(arguments, tuple):
        raise TypeError("retail localization arguments must be a tuple")
    return repr((identifier, arguments))


def _py2_literal(value: object) -> str:
    """Render ``value`` as a literal the retail Python 2 client evaluates.

    ``str`` arguments are written with ``ascii()`` escapes; any non-ASCII
    text gets a ``u`` prefix so Python 2 yields ``unicode`` rather than a
    UTF-8 byte string (``u'...{0}'.format(b'\\xc3\\xa9')`` raises
    ``UnicodeDecodeError`` inside the native HUD).
    """

    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, str):
        encoded = ascii(value)
        return encoded if value.isascii() else "u" + encoded
    if isinstance(value, tuple):
        items = [_py2_literal(item) for item in value]
        if len(items) == 1:
            return "(%s,)" % items[0]
        return "(%s)" % ", ".join(items)
    raise TypeError("unsupported retail literal value %r" % (value,))


def _retail_vote_text(
    identifier: str,
    arguments: tuple[object, ...] = (),
    localised: tuple[int, ...] = (),
) -> str:
    """Encode a vote-HUD string whose chosen arguments are string ids.

    Recovered ``GenericVotingHUD.decode_string`` (stock ``hud.pyd``
    0x1004AE90, source lines 54-74)::

        value = ast.literal_eval(text)
        if isinstance(value[0], tuple):
            template = strings.get_by_id(value[0][0])
            localise = value[0][1]
        else:
            template = strings.get_by_id(value[0])
            localise = []
        args = ()
        for i in range(len(value[1])):
            arg = value[1][i]
            if i in localise:
                arg = strings.get_by_id(arg)
            args += (arg,)
        return template.format(*args)

    ``localised`` lists argument indexes that are themselves string ids
    (the kick reason); every other argument is shown verbatim.
    """

    _retail_localised_text(identifier)  # identifier validation only
    if not isinstance(arguments, tuple):
        raise TypeError("retail localization arguments must be a tuple")
    for index in localised:
        if not 0 <= int(index) < len(arguments):
            raise ValueError("localised argument index out of range")
    for argument in arguments:
        if isinstance(argument, str) and (
            "\x00" in argument or len(argument) > 128
        ):
            raise ValueError("invalid retail vote argument")
    head = (
        (str(identifier), tuple(int(index) for index in localised))
        if localised else str(identifier)
    )
    return _py2_literal((head, tuple(arguments)))


def _vote_player_name(player) -> str:
    """Player name as a vote-HUD format argument (bounded, NUL-free)."""

    return str(getattr(player, "name", "") or "").replace("\x00", "")[:64]


def _retail_dynamic_text(value: object) -> str:
    """Encode operator-authored text for the retail vote HUD.

    ``GenericVotingHUD`` does not accept plain strings: it literal-evaluates
    every title, description *and candidate name* as ``(identifier, args)``.
    Unknown identifiers are returned verbatim by the retail string table, so
    an ordinary map name can safely be used as the identifier.  Braces are
    doubled because the HUD subsequently calls ``str.format`` on that value.
    """

    text = str(value)
    if not text or "\x00" in text or "\r" in text or "\n" in text:
        raise ValueError("invalid retail dynamic vote text")
    if len(text) > 128:
        raise ValueError("retail dynamic vote text is too long")
    return repr((text.replace("{", "{{").replace("}", "}}"), ()))


class VoteManager:
    """Own at most one bounded retail vote overlay at a time."""

    def __init__(self, server) -> None:
        self.server = server
        self.active = False
        # host -> retail kick disconnect reason, cleared when the match ends.
        self._match_kicks: dict[str, int] = {}
        self.kind: str | None = None
        self.target_id: int | None = None
        self.starter_id: int | None = None
        # Kick ballot presentation: names are captured at the start so the
        # result line still reads correctly after the target has left.
        self.kick_reason = KICK_GRIEFING
        self.target_name = ""
        self.starter_name = ""
        # CLOSED title of the kick ballot being resolved (None while open).
        self._kick_result: str | None = None
        # Compatibility views retained for callers and operational tests.
        self.yes: set[int] = set()
        self.no: set[int] = set()
        self.candidates: tuple[str, ...] = ()
        self.votes: dict[int, int] = {}
        self.next_map: str | None = None
        self.opened_at = 0.0
        self._deadline = 0.0
        self._map_result_event = asyncio.Event()
        self._last_start: dict[int, float] = {}
        # Cooldown key -> earliest time that starter may open another kick.
        self._cooldown_until: dict = {}
        self._starter_cooldown_key = None
        # Maps played on this server, oldest first (distinct consecutive
        # entries); the vote avoids the most recent ones.
        self.map_history: deque[str] = deque(maxlen=16)
        # Map discovery is startup work. Never glob the filesystem from the
        # 60 Hz mode tick when the final-minute vote is opened.
        self._available_maps = self._discover_maps()
        # Retail baseSquadLobbyMenu: random.shuffle(map_list) once at lobby
        # creation.  Only a real ServerConfig opts in by default, so bare
        # embedders keep the deterministic listed order.
        if bool(getattr(
            getattr(server, "config", None), "map_rotation_shuffle", False
        )) and len(self._available_maps) > 1:
            shuffled = list(self._available_maps)
            random.shuffle(shuffled)
            self._available_maps = tuple(shuffled)
        self._map_areas = self._measure_maps(self._available_maps)

    def _maps_root(self) -> Path:
        config = getattr(self.server, "config", None)
        return Path(getattr(config, "maps_path", "maps"))

    def _measure_maps(self, names) -> dict[str, int | None]:
        """Playable area per catalog map (startup work, cached per file)."""

        config = getattr(self.server, "config", None)
        overrides = getattr(config, "map_size_overrides", None) or {}
        by_name = {}
        if isinstance(overrides, dict):
            for name, value in overrides.items():
                if isinstance(value, str):
                    value = MAP_SIZE_CLASS_AREAS.get(value.strip().lower())
                try:
                    by_name[str(name).casefold()] = (
                        int(value) if value is not None else None
                    )
                except (TypeError, ValueError):
                    continue
        root = self._maps_root()
        areas: dict[str, int | None] = {}
        for name in names:
            key = str(name).casefold()
            if key in by_name:
                areas[key] = by_name[key]
                continue
            try:
                areas[key] = map_playable_area(root, str(name))
            except Exception:  # noqa: BLE001 - size fit is advisory
                areas[key] = None
        return areas

    def map_area(self, name: str) -> int | None:
        key = str(name).casefold()
        if key not in self._map_areas:
            self._map_areas.update(self._measure_maps((name,)))
        return self._map_areas.get(key)

    def lobby_size(self) -> float:
        """Players the next map is chosen for (bots weighted by config)."""

        config = getattr(self.server, "config", None)
        try:
            bot_weight = float(getattr(config, "map_vote_bot_weight", 1.0))
        except (TypeError, ValueError):
            bot_weight = 1.0
        total = 0.0
        for player in getattr(self.server, "players", {}).values():
            total += bot_weight if bool(getattr(player, "is_bot", False)) else 1.0
        return total

    def note_map_played(self, map_name) -> None:
        name = str(map_name or "").strip()
        if not name:
            return
        if self.map_history and self.map_history[-1].casefold() == name.casefold():
            return
        self.map_history.append(name)

    def _config_number(self, name: str, default: float) -> float:
        value = getattr(getattr(self.server, "config", None), name, None)
        if value is None:
            return float(default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return float(default)

    def rank_map_candidates(self, available, current: str) -> list[str]:
        """Order next-map candidates: never ``current``; maps whose retail
        max_players the humans exceed last, then recent maps last, then maps
        sized for the lobby, then the (startup-shuffled) rotation."""

        current_key = str(current or "").casefold()
        available = list(available)
        current_index = next(
            (
                index
                for index, name in enumerate(available)
                if name.casefold() == current_key
            ),
            -1,
        )
        if current_index >= 0:
            ordered = available[current_index + 1:] + available[:current_index]
        else:
            ordered = available
        choices = [name for name in ordered if name.casefold() != current_key]
        exclude = max(0, int(self._config_number(
            "map_vote_recent_exclude", MAP_VOTE_RECENT_EXCLUDE
        )))
        recent_order: dict[str, int] = {}
        if exclude:
            earlier = [
                name.casefold() for name in self.map_history
                if name.casefold() != current_key
            ]
            # Most recent first; rank 0 is the map just before this one.
            for rank, key in enumerate(reversed(earlier)):
                if key not in recent_order and len(recent_order) < exclude:
                    recent_order[key] = rank
        size_fit = bool(getattr(
            getattr(self.server, "config", None), "map_vote_size_fit", True
        ))
        players = self.lobby_size()
        per_player = self._config_number(
            "map_vote_area_per_player", MAP_VOTE_AREA_PER_PLAYER
        )
        min_area = self._config_number("map_vote_min_area", MAP_VOTE_MIN_AREA)

        over_cap = self._retail_over_cap_maps(choices)

        def key(item):
            index, name = item
            folded = name.casefold()
            recent_rank = recent_order.get(folded)
            # Recent maps go last, the least recently played of them first.
            recency = (1, -recent_rank) if recent_rank is not None else (0, 0)
            tier = (
                map_fit_tier(self.map_area(name), players,
                             per_player=per_player, min_area=min_area)
                if size_fit else 0
            )
            return (int(folded in over_cap), recency, tier, index)

        return [name for _index, name in sorted(enumerate(choices), key=key)]

    def _human_count(self) -> int:
        return sum(
            1 for player in getattr(self.server, "players", {}).values()
            if not bool(getattr(player, "is_bot", False))
        )

    def _retail_over_cap_maps(self, names) -> set[str]:
        """Casefolded maps whose retail ``max_players`` the humans exceed."""

        config = getattr(self.server, "config", None)
        if not bool(getattr(config, "map_vote_retail_max_players", False)):
            return set()
        from server.map_metadata import retail_map_catalogue

        catalogue = retail_map_catalogue(self._maps_root())
        humans = self._human_count()
        result: set[str] = set()
        for name in names:
            entry = catalogue.get(str(name).casefold()) or {}
            try:
                cap = int(entry.get("max_players"))
            except (TypeError, ValueError):
                continue
            if humans > cap:
                result.add(str(name).casefold())
        return result

    def _mode_code(self) -> str:
        from server.map_metadata import canonical_mode_code

        config = getattr(self.server, "config", None)
        return canonical_mode_code(getattr(config, "game_mode", "") or "")

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

    def _eligible_ids(self, *, exclude: int | None = None) -> set[int]:
        """Count connected human voters, not peerless bots or loading peers."""
        connections = {id(value) for value in self.server.connections.values()}
        return {
            int(player.id)
            for player in self.server.players.values()
            if int(player.id) != exclude
            and not bool(getattr(player, "is_bot", False))
            and id(getattr(player, "connection", None)) in connections
            and bool(getattr(player.connection, "in_game", False))
        }

    def _eligible_count(self) -> int:
        return len(self._eligible_ids())

    def _mode_available_maps(self) -> tuple[str, ...]:
        """Return the cached operator catalog narrowed by a stock playlist.

        An explicit ``lobby.map_rotation`` wins (minus stock maps whose
        retail ``invalid_modes`` list the mode).  With an empty rotation the
        mode's ``stock_maps`` or else its retail single-mode playlist,
        filtered like retail ``PlayList`` (invalid_modes, classic, mafia,
        release), narrows the catalogue; custom maps stay available to
        operators who list them.
        """

        available = self._available_maps
        config = getattr(self.server, "config", None)
        from server.map_metadata import retail_map_catalogue, retail_mode_pool

        code = self._mode_code()
        retail_code = "ctf" if code == "cctf" else code
        if tuple(getattr(config, "map_rotation", ()) or ()):
            # Retail PlayList never pairs a mode with a map whose mapinfo
            # invalid_modes lists it; honour that for stock maps even in an
            # explicit rotation (custom maps have no catalogue row).
            catalogue = retail_map_catalogue(self._maps_root())
            kept = tuple(
                name for name in available
                if retail_code not in tuple(
                    (catalogue.get(name.casefold()) or {}).get(
                        "invalid_modes", ()
                    )
                )
            )
            dropped = [name for name in available if name not in kept]
            if dropped and dropped != getattr(self, "_warned_invalid", None):
                self._warned_invalid = dropped
                logger.warning(
                    "Rotation maps retail marks invalid for mode %s skipped: %s",
                    code, ", ".join(dropped),
                )
            return kept or available
        playlist = tuple(
            getattr(getattr(self.server, "mode", None), "stock_maps", ()) or ()
        ) or retail_mode_pool(self._maps_root(), code)
        if not playlist:
            return available
        wanted = {name.casefold() for name in playlist}
        # Keep the (shuffled) catalogue order.
        filtered = tuple(name for name in available if name.casefold() in wanted)
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

    def match_kick_reason(self, host: str):
        """Disconnect reason for an address vote-kicked this match, else None."""

        return getattr(self, "_match_kicks", {}).get(str(host))

    def clear_match_kicks(self) -> None:
        """A new match: vote-kicked players may join again."""

        self._match_kicks = {}

    @staticmethod
    def _cooldown_key(starter):
        """Key the kick-vote cooldown by address: reconnecting (new slot id)
        must not reset it. Falls back to the slot id for peerless players."""

        try:
            from server.bans import address_host

            host = address_host(starter.connection.peer)
        except Exception:
            host = None
        return f"ip:{host}" if host and host != "unknown" else int(starter.id)

    def _team_player_count(self, team) -> int:
        """Human players on ``team``: bots cannot vote, so they never make
        a team big enough to start a kick (retail had no bots; a human alone
        with bots gets KICK_NOT_ENOUGH_PLAYERS instead of silence)."""
        return sum(
            1 for player in self.server.players.values()
            if getattr(player, "team", None) == team
            and not bool(getattr(player, "is_bot", False))
        )

    def _kick_denial(self, starter, target, now: float):
        """Return the retail (string_id, params) refusing this kick, if any.

        The check order is ours (retail's is server-side and unrecovered);
        every id and template is the stock one.
        """

        from server.game_constants import TEAM_SPECTATOR

        team = getattr(starter, "team", None)
        if team is not None and int(team) == int(TEAM_SPECTATOR):
            return KICK_DENIED_FOR_SPECTATOR, ()
        if int(target.id) == int(starter.id):
            return KICK_DENIED_SELF_KICK, ()
        is_host = getattr(getattr(self.server, "mode", None), "is_host", None)
        if callable(is_host):
            try:
                if is_host(target):
                    return KICK_DENIED_KICK_HOST, ()
            except Exception:  # noqa: BLE001 - host lookup is advisory
                logger.debug("kick host check failed", exc_info=True)
        if self.active:
            return KICK_DENIED_VOTE_IN_PROGRESS, ()
        until = getattr(self, "_cooldown_until", {}).get(
            self._cooldown_key(starter)
        )
        if until is not None and float(now) < float(until):
            import math

            remaining = max(1, int(math.ceil(float(until) - float(now))))
            return KICK_DENIED_VOTE_TOO_SOON, (str(remaining),)
        minimum = int(self._config_number(
            "votekick_min_team_players", KICK_MIN_TEAM_PLAYERS
        ))
        if team is not None and self._team_player_count(team) < minimum:
            return KICK_DENIED_NOT_ENOUGH_PLAYERS, ()
        return None

    def _deny_kick(self, starter, string_id: str, params=()) -> None:
        """Send one retail kick denial to the starter only."""

        send = getattr(starter, "send", None)
        if not callable(send):
            return
        try:
            from server.announcements import build_localised_overlay

            send(build_localised_overlay(string_id, params), reliable=True)
        except Exception:  # noqa: BLE001 - the notice is best effort
            logger.debug("kick denial %s not sent", string_id, exc_info=True)

    def start_kick(self, starter, target, reason: int, now: float) -> bool:
        """Open a majority kick ballot if identity and cooldown are valid.

        A refusal the stock client has a string for is answered with that
        LocalisedMessage(50) to the starter (the kick menu closes on it);
        malformed packets and non-player targets stay silent.
        """

        if (target is None
                or int(reason) not in KICK_REASON_IDS
                or self.server.players.get(int(starter.id)) is not starter
                or int(starter.id) not in self._eligible_ids()):
            return False
        denial = self._kick_denial(starter, target, now)
        if denial is not None:
            self._deny_kick(starter, *denial)
            logger.info(
                "VOTE-KICK by %s against %s denied: %s",
                getattr(starter, "name", starter.id),
                getattr(target, "name", target.id),
                denial[0],
            )
            return False
        if (self.server.players.get(int(target.id)) is not target
                or int(target.id) not in self._eligible_ids()):
            return False
        cooldown_key = self._cooldown_key(starter)

        self.active = True
        self.kind = "kick"
        self.target_id = int(target.id)
        self.starter_id = int(starter.id)
        self.candidates = ("Kick {}".format(target.name), "Keep")
        self.kick_reason = int(reason)
        self.target_name = _vote_player_name(target)
        self.starter_name = _vote_player_name(starter)
        self._kick_result = None
        self.votes = {int(starter.id): 0}
        self.yes = {int(starter.id)}
        self.no = set()
        self.opened_at = float(now)
        self._deadline = time.monotonic() + VOTE_DURATION
        self._last_start[cooldown_key] = float(now)
        if not hasattr(self, "_cooldown_until"):
            self._cooldown_until = {}
        self._cooldown_until[cooldown_key] = float(now) + self._config_number(
            "votekick_cooldown_seconds", VOTE_COOLDOWN
        )
        self._starter_cooldown_key = cooldown_key
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
            try:
                _retail_dynamic_text(value)
            except ValueError:
                continue
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
        self._deadline = time.monotonic() + MAP_VOTE_DURATION
        self._map_result_event = asyncio.Event()
        self._broadcast(VOTE_START, None)
        logger.info("MAP VOTE opened: %s", ", ".join(self.candidates))
        return True

    def ensure_map_vote(self, now: float) -> bool:
        """Present up to three cached deterministic map candidates."""

        if self.active or self.next_map is not None:
            return False
        current = str(getattr(self.server.config, "default_map", ""))
        self.note_map_played(current)
        choices = self.rank_map_candidates(self._mode_available_maps(), current)
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
        event = self._map_result_event
        remaining = max(0.0, self._deadline - time.monotonic())
        if remaining > 0.0:
            try:
                await asyncio.wait_for(
                    event.wait(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                pass
        if self._map_result_event is not event:
            # A cancelled ballot's waiter must never close a replacement vote.
            return None
        if self.active and self.kind == "map":
            self._resolve_map()
        return self.next_map

    def reveal_to(self, connection) -> None:
        """Open the current ballot for a client that just entered GameScene."""

        self._expire_due()
        if not self.active:
            return
        send = getattr(connection, "send", None)
        if callable(send):
            send(bytes(self._build_packet(VOTE_START).generate()), reliable=True)

    def cast(self, voter, yes: bool) -> None:
        """Compatibility API for a yes/no kick choice."""

        self._cast_index(voter, 0 if yes else 1)

    def cast_candidate(self, voter, candidate) -> None:
        """Record an internal/raw candidate name (compatibility API)."""

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

    def cast_wire_candidate(self, voter, candidate) -> None:
        """Record a candidate exactly as advertised to the retail client.

        The native client echoes the selected candidate record from packet 47.
        Compare that opaque token against this ballot's own wire values rather
        than evaluating client-controlled Python literals or accepting a raw
        name that was never displayed.
        """

        if not self.active:
            return
        token = str(candidate)
        index = next(
            (
                position
                for position, name in enumerate(self.candidates)
                if self._wire_candidate_name(position, name) == token
            ),
            -1,
        )
        self._cast_index(voter, index)

    def _cast_index(self, voter, index: int) -> None:
        self._expire_due()
        player_id = int(voter.id)
        if (
            not self.active
            or not 0 <= int(index) < len(self.candidates)
            or (self.kind == "kick" and player_id == self.target_id)
            or self.server.players.get(player_id) is not voter
            or player_id not in self._eligible_ids()
        ):
            return
        if self.votes.get(player_id) == int(index):
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

    def tick(self, now: float | None = None) -> None:
        """Resolve an expired ballot without blocking the simulation tick."""

        if not self.active:
            return
        duration = MAP_VOTE_DURATION if self.kind == "map" else VOTE_DURATION
        forced_elapsed = now is not None and float(now) - self.opened_at >= duration
        if not forced_elapsed and time.monotonic() < self._deadline:
            return
        if self.kind == "map":
            self._resolve_map()
        else:
            self._resolve_kick(passed=len(self.yes) >= self._needed())

    def _expire_due(self) -> None:
        """Enforce the deadline even before the next simulation tick."""
        if self.active and time.monotonic() >= self._deadline:
            if self.kind == "map":
                self._resolve_map()
            else:
                self._resolve_kick(passed=len(self.yes) >= self._needed())

    def cancel(self, *, by_starter: bool = False, now: float | None = None) -> None:
        """Close the ballot without a result.

        ``by_starter`` marks the starter's own InitiateKickMessage(CANCEL):
        the result panel then reads ``VOTE_KICK_CANCELLED`` instead of the
        ordinary failed-vote line, and the starter's next kick waits the
        retail MIN_TIME_BETWEEN_CANCELLED_KICK_VOTES instead of the full
        cooldown.
        """
        if not self.active:
            return
        if self.kind == "kick" and by_starter:
            key = getattr(self, "_starter_cooldown_key", None)
            if key is not None:
                moment = time.time() if now is None else float(now)
                if not hasattr(self, "_cooldown_until"):
                    self._cooldown_until = {}
                self._cooldown_until[key] = moment + self._config_number(
                    "votekick_cancelled_cooldown_seconds",
                    CANCELLED_VOTE_COOLDOWN,
                )
        if self.kind == "kick":
            self._resolve_kick(passed=False, cancelled=by_starter)
        else:
            self._broadcast(VOTE_CLOSED, None)
            self._clear_active()
            self._map_result_event.set()

    def forget_player(self, player_id: int) -> None:
        """Remove vote state before a compact player id is reassigned."""

        player_id = int(player_id)
        self._last_start.pop(player_id, None)
        getattr(self, "_cooldown_until", {}).pop(player_id, None)
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
        if self.active and self.kind == "map":
            remaining = self._eligible_ids(exclude=player_id)
            if remaining and remaining.issubset(self.votes):
                self._resolve_map()

    def consume_next_map(self) -> str | None:
        """Return and clear the map chosen for the next round boundary."""

        result = self.next_map
        self.next_map = None
        return result

    def _kick_result_text(self, passed: bool, cancelled: bool) -> str:
        """Stock result line shown by the HUD for the CLOSED ballot."""

        target, starter = self.target_name, self.starter_name
        if passed:
            # "{0} has been kicked by {1} for {2}"
            return _retail_vote_text(
                "VOTE_KICK_SUCCESSFUL",
                (target, starter, KICK_REASON_IDS[self.kick_reason]),
                localised=(2,),
            )
        if cancelled:
            # "Vote to kick {0} cancelled by {1}"
            return _retail_vote_text("VOTE_KICK_CANCELLED", (target, starter))
        # "{0}'s vote to kick {1} failed"
        return _retail_vote_text("VOTE_KICK_UNSUCCESSFUL", (starter, target))

    def _resolve_kick(self, passed: bool, cancelled: bool = False) -> None:
        target = self.server.players.get(self.target_id)
        passed = bool(passed and target is not None)
        self._kick_result = self._kick_result_text(passed, cancelled)
        self._broadcast(VOTE_CLOSED, target)
        self._kick_result = None
        name = target.name if target is not None else "?"
        reason = self.kick_reason
        yes_count, no_count = len(self.yes), len(self.no)
        # Disconnect invokes forget_player synchronously. Retire this ballot
        # before that callback to avoid recursively resolving the same kick.
        self._clear_active()
        if passed:
            logger.info("VOTE-KICK PASSED - kicking %s", name)
            # The client says "kicked ... until the end of the current match":
            # refuse this address until the match ends (clear_match_kicks).
            try:
                from server.bans import address_host

                host = address_host(target.connection.peer)
            except Exception:
                host = None
            if host and host != "unknown":
                self._match_kicks[host] = KICK_DISCONNECT_REASONS[reason]
            try:
                target.disconnect(reason=KICK_DISCONNECT_REASONS[reason])
            except Exception:
                logger.debug("vote-kick disconnect failed", exc_info=True)
        else:
            logger.info(
                "VOTE-KICK failed against %s (%d yes / %d no)",
                name,
                yes_count,
                no_count,
            )

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

    def _wire_candidate_name(self, index: int, name: str) -> str:
        """Return one crash-safe label consumed and echoed by the vote HUD."""

        if self.kind == "kick":
            # Stock strings avoid exposing the target name twice and preserve
            # the retail Yes/No wording for every installed language.
            return _retail_localised_text(
                "KICK_YES" if int(index) == 0 else "KICK_NO"
            )
        return _retail_dynamic_text(name)

    def _build_packet(self, message_type: int) -> GenericVoteMessage:
        """Build one literal-safe retail vote packet for broadcast or replay."""

        packet = GenericVoteMessage()
        packet.player_id = (
            int(self.starter_id) if self.starter_id is not None else 255
        )
        packet.message_type = int(message_type)
        counts = self._candidate_counts()
        packet.candidates = [
            {
                "name": self._wire_candidate_name(index, name),
                "votes": counts[index],
            }
            for index, name in enumerate(self.candidates)
        ]
        # The client literal-evaluates these fields into (id, arguments).
        # Omitting the empty arguments tuple is a native exception hazard.
        if self.kind == "map":
            # GenericVotingHUD uses CLOSED.title for the six-second result
            # panel. Repeating the START title leaves "VOTE MAP" onscreen
            # without ever saying which map won, even though the tally has
            # closed and the next-map choice is already authoritative.
            packet.title = _retail_localised_text(
                "MAP_VOTED_MESSAGE", (self.next_map,)
            ) if message_type == VOTE_CLOSED and self.next_map is not None else (
                _retail_localised_text("VOTE_MAP_TITLE")
            )
            packet.description = _retail_localised_text(
                "VOTE_MAP_DESCRIPTION"
            )
        else:
            # Stock kick ballot: "Vote Kick" / "Vote to Kick {target} for
            # {reason}? Vote initiated by {starter}" with the reason id
            # (argument 1) localised by the HUD. CLOSED.title carries the
            # result line the HUD shows once the ballot ends.
            packet.title = (
                self._kick_result
                if message_type == VOTE_CLOSED and self._kick_result
                else _retail_localised_text("VOTE_TO_KICK_TITLE")
            )
            packet.description = _retail_vote_text(
                "VOTE_TO_KICK_DESCRIPTION",
                (
                    self.target_name,
                    KICK_REASON_IDS.get(self.kick_reason, "KICK_REASON_GRIEFING"),
                    self.starter_name,
                ),
                localised=(1,),
            )
        packet.allow_revote = 1
        packet.can_vote = int(message_type != VOTE_CLOSED)
        return packet


__all__ = [
    "KICK_ABUSE",
    "KICK_CANCEL",
    "KICK_DISCONNECT_REASONS",
    "KICK_GRIEFING",
    "KICK_HACKING",
    "KICK_REASON_IDS",
    "MAP_VOTE_DURATION",
    "MAP_VOTE_LEAD_SECONDS",
    "VOTE_CAST",
    "VOTE_CLOSED",
    "CANCELLED_VOTE_COOLDOWN",
    "KICK_MIN_TEAM_PLAYERS",
    "VOTE_COOLDOWN",
    "VOTE_DURATION",
    "VOTE_START",
    "VOTE_UPDATE",
    "VoteManager",
]
