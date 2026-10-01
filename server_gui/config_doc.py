"""Comment-preserving config.toml editing, validated by the server's loader.

``ConfigDocument`` keeps the operator's file byte-for-byte except for the
values that were changed (tomlkit). ``describe()`` turns the file's own
comments into per-key help, and ``validate_text()`` runs the exact
``server.config.load_config`` used at startup, so the window can never save a
file the server would refuse.
"""

from __future__ import annotations

import io
import logging
import os
import re
import tempfile
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    tomllib = None

import tomlkit
from tomlkit.items import AoT, Array, InlineTable, Table

_HEADER = re.compile(r"^\s*\[(\[)?\s*([A-Za-z0-9_.\-\"' ]+?)\s*\]?\]\s*(#.*)?$")
_KEY = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_\-]*)\s*=")
_COMMENTED_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$")

#: Keys whose accepted values are a closed set in server/config.py.
STATIC_ENUMS: dict[tuple[str, str], tuple[str, ...]] = {
    ("bots", "population_mode"): ("backfill", "fixed", "admin"),
    ("bots", "difficulty"): ("casual", "normal", "hard", "mixed"),
    ("bots", "worker"): ("thread", "process"),
    ("bots", "behavior_version"): ("cooperative", "classic"),
    ("network", "worldupdate_delivery"): ("split", "sequenced"),
    ("game", "movement_authority"): ("server", "client"),
    ("game", "map_sync_mode"): ("full", "auto"),
    ("logging", "level"): ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
    ("map_creator", "terrain"): (
        "", "desert", "lunar", "mountain", "grassland", "temple", "urban",
        "marsh", "snowy", "water",
    ),
    ("map_creator", "target_mode"): (
        "", "tdm", "ctf", "dem", "mh", "oc", "tc", "vip", "zom", "dia",
    ),
}

#: Everything in config.toml is read once at startup.
RESTART_NOTE = "Applies the next time the server starts."

#: Keys the host window itself validates beyond the server loader.
PORT_KEYS = {("server", "port"), ("steam", "steam_port"), ("steam", "query_port")}


@dataclass
class KeySpec:
    """One editable key with the help text its comments provide."""

    section: str
    key: str
    kind: str                       # bool/int/float/str/enum/list/table
    description: str = ""           # comment block directly above the key
    group: str = ""                 # block shared with keys above (no own block)
    inline: str = ""                # trailing comment
    choices: tuple[tuple[str, Any], ...] = ()
    optional: bool = False          # only present as a commented example
    example: Any = None             # value of the commented example

    @property
    def help_text(self) -> str:
        parts = [p for p in (self.description, self.inline) if p]
        return "\n".join(parts)


@dataclass
class SectionSpec:
    name: str
    description: str = ""
    keys: list[KeySpec] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.name.replace("_", " ").replace(".", " / ")


@dataclass
class ValidationResult:
    ok: bool
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    config: Any = None


# --------------------------------------------------------------------------
# Comments -> help text


def _comment_text(line: str) -> str:
    text = line.strip()[1:]
    return text[1:] if text.startswith(" ") else text


def _parse_comments(text: str) -> tuple[dict[str, str], dict[tuple[str, str], dict]]:
    """Return section descriptions and per-key comment data from raw text."""

    sections: dict[str, str] = {}
    keys: dict[tuple[str, str], dict] = {}
    section = ""
    pending: list[str] = []
    group = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            pending = []
            group = ""
            continue
        if stripped.startswith("#"):
            body = _comment_text(stripped)
            match = _COMMENTED_KEY.match(body.strip())
            if match and section and _parses_as_value(match.group(2)):
                name = match.group(1)
                keys.setdefault((section, name), {
                    "description": "\n".join(pending),
                    "group": group,
                    "optional": True,
                    "example": _parse_value(match.group(2)),
                })
                pending = []
                continue
            pending.append(body)
            continue
        header = _HEADER.match(stripped)
        if header and stripped.startswith("["):
            section = header.group(2).replace('"', "").replace("'", "").strip()
            sections[section] = "\n".join(pending)
            pending = []
            group = ""
            continue
        key = _KEY.match(stripped)
        if key:
            description = "\n".join(pending)
            if description:
                group = description
            entry = keys.get((section, key.group(1)), {})
            entry.update({
                "description": description,
                "group": "" if description else group,
                "optional": False,
            })
            keys[(section, key.group(1))] = entry
            pending = []
    return sections, keys


def _parses_as_value(text: str) -> bool:
    try:
        _parse_value(text)
        return True
    except Exception:
        return False


def _parse_value(text: str) -> Any:
    """Parse the value of a commented ``# key = value  # note`` example."""

    candidate = text.strip()
    while True:
        try:
            return tomlkit.parse(f"v = {candidate}")["v"].unwrap()
        except Exception:
            if "#" not in candidate:
                raise
            candidate = candidate.rsplit("#", 1)[0].strip()


# --------------------------------------------------------------------------
# Value kinds and choices


def _rule_choices(key: str, current: Any) -> tuple[tuple[str, Any], ...] | None:
    """Match Lobby rule choices exactly as server/game_rules.py accepts them."""

    try:
        from server.game_rules import RULE_DEFINITIONS
    except Exception:
        return None
    definition = RULE_DEFINITIONS.get(key)
    if definition is None:
        return None
    if all(isinstance(value, bool) for value in definition.choices):
        return None  # plain switch
    as_text = isinstance(current, str)
    options: list[tuple[str, Any]] = []
    for value in definition.choices:
        if isinstance(value, bool):
            options.append(("OFF", "OFF" if as_text else False))
        elif isinstance(value, float):
            if key == "RULE_SPAWN_PROTECTION_TIME":
                text = "OFF" if value == 0 else str(int(value))
                label = "OFF" if value == 0 else f"{int(value)} s"
            else:
                text = label = f"{int(round(value * 100))}%"
            options.append((label, text if as_text else value))
        else:
            options.append((str(value), str(value) if as_text else int(value)))
    return tuple(options)


def kind_of(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "table"
    return "str"


def format_toml_value(value: Any) -> str:
    """One TOML literal for lists/tables shown as editable text."""

    return tomlkit.dumps({"v": value}).split("=", 1)[1].strip()


def parse_toml_value(text: str) -> Any:
    """Parse one TOML literal typed by the operator; raises ValueError."""

    try:
        return tomlkit.parse(f"v = {text.strip()}")["v"].unwrap()
    except Exception as exc:
        raise ValueError(f"not a valid TOML value: {text!r}") from exc


def coerce(spec: KeySpec, raw: Any) -> Any:
    """Convert widget input to the TOML type the key uses; raises ValueError."""

    kind = spec.kind
    if kind == "bool":
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)
    if kind == "int":
        text = str(raw).strip()
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{spec.section}.{spec.key} must be a whole number") from None
    if kind == "float":
        text = str(raw).strip()
        try:
            return float(text)
        except ValueError:
            raise ValueError(f"{spec.section}.{spec.key} must be a number") from None
    if kind == "enum":
        for label, value in spec.choices:
            if raw == label or raw == value:
                return value
        raise ValueError(f"{spec.section}.{spec.key}: {raw!r} is not one of the choices")
    if kind in ("list", "table"):
        value = parse_toml_value(raw) if isinstance(raw, str) else raw
        if kind == "list" and not isinstance(value, list):
            raise ValueError(f"{spec.section}.{spec.key} must be an array like [\"a\", \"b\"]")
        if kind == "table" and not isinstance(value, dict):
            raise ValueError(f"{spec.section}.{spec.key} must be an inline table like {{ a = 1 }}")
        return value
    return str(raw)


# --------------------------------------------------------------------------
# The document


class ConfigDocument:
    """config.toml held in memory with formatting and comments intact."""

    def __init__(self, text: str, path: Path | None = None) -> None:
        self.path = path
        self.original_text = text
        self.doc = tomlkit.parse(text)
        self._sections, self._comments = _parse_comments(text)

    # ---- loading / saving -------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "ConfigDocument":
        return cls(Path(path).read_text(encoding="utf-8"), Path(path))

    def text(self) -> str:
        return tomlkit.dumps(self.doc)

    @property
    def dirty(self) -> bool:
        return self.text() != self.original_text

    def save(self, path: Path | None = None, *, maps_dir: Path | None = None) -> ValidationResult:
        """Validate, then atomically replace the file. Never writes invalid TOML."""

        target = Path(path or self.path)
        result = validate_text(self.text(), maps_dir=maps_dir)
        if not result.ok:
            return result
        data = self.text()
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".toml", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(data)
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        self.original_text = data
        self.path = target
        return result

    # ---- values -----------------------------------------------------------
    def _table(self, section: str, create: bool = False):
        node: Any = self.doc
        for part in section.split("."):
            if part not in node:
                if not create:
                    return None
                node.add(part, tomlkit.table())
            node = node[part]
        return node

    def has(self, section: str, key: str) -> bool:
        table = self._table(section)
        return table is not None and key in table

    def get(self, section: str, key: str, default: Any = None) -> Any:
        table = self._table(section)
        if table is None or key not in table:
            return default
        value = table[key]
        return value.unwrap() if hasattr(value, "unwrap") else value

    def set(self, section: str, key: str, value: Any) -> None:
        """Assign one value; existing trailing comments are preserved."""

        table = self._table(section, create=True)
        if key in table and self.get(section, key) == value and type(self.get(section, key)) is type(value):
            return
        table[key] = value

    def remove(self, section: str, key: str) -> None:
        table = self._table(section)
        if table is not None and key in table:
            del table[key]

    # ---- schema -----------------------------------------------------------
    def section_names(self) -> list[str]:
        names: list[str] = []

        def walk(node, prefix: str) -> None:
            for name, value in node.items():
                full = f"{prefix}{name}"
                if isinstance(value, Table) and not isinstance(value, InlineTable):
                    scalars = [k for k, v in value.items() if not (isinstance(v, Table) and not isinstance(v, InlineTable))]
                    if scalars or not any(isinstance(v, Table) for v in value.values()):
                        names.append(full)
                    walk(value, f"{full}.")

        walk(self.doc, "")
        return names

    def describe(self, extra_enums: dict[tuple[str, str], tuple[str, ...]] | None = None) -> list[SectionSpec]:
        """Every section and key with help text, widget kind and choices."""

        enums = dict(STATIC_ENUMS)
        if extra_enums:
            enums.update(extra_enums)
        result: list[SectionSpec] = []
        for name in self.section_names():
            table = self._table(name)
            section = SectionSpec(name, self._sections.get(name, ""))
            present = []
            for key, item in table.items():
                if isinstance(item, (Table, AoT)) and not isinstance(item, InlineTable):
                    continue
                present.append(key)
                section.keys.append(self._spec(name, key, item, enums))
            for (sec, key), info in self._comments.items():
                if sec == name and info.get("optional") and key not in present:
                    example = info.get("example")
                    spec = KeySpec(
                        section=name, key=key, kind=kind_of(example),
                        description=info.get("description", ""), group=info.get("group", ""),
                        optional=True, example=example,
                    )
                    self._apply_choices(spec, example, enums)
                    section.keys.append(spec)
            result.append(section)
        return result

    def _spec(self, section: str, key: str, item: Any, enums) -> KeySpec:
        value = item.unwrap() if hasattr(item, "unwrap") else item
        info = self._comments.get((section, key), {})
        inline = ""
        trivia = getattr(item, "trivia", None)
        if trivia is not None and trivia.comment:
            inline = trivia.comment.lstrip("#").strip()
        spec = KeySpec(
            section=section, key=key, kind=kind_of(value),
            description=info.get("description", ""), group=info.get("group", ""),
            inline=inline,
        )
        self._apply_choices(spec, value, enums)
        return spec

    @staticmethod
    def _apply_choices(spec: KeySpec, value: Any, enums) -> None:
        options = enums.get((spec.section, spec.key))
        if options is not None and isinstance(value, str):
            values = list(options)
            if value not in values:
                values.insert(0, value)
            spec.kind = "enum"
            spec.choices = tuple((v if v else "(blank)", v) for v in values)
            return
        if spec.section == "game_rules" and spec.key.startswith("RULE_"):
            choices = _rule_choices(spec.key, value)
            if choices:
                spec.kind = "enum"
                spec.choices = choices

    # ---- defaults ---------------------------------------------------------
    def restore_section(self, section: str, defaults: "ConfigDocument") -> list[str]:
        """Reset one section to the shipped values. Returns changed keys."""

        default_table = defaults._table(section)
        table = self._table(section)
        if default_table is None or table is None:
            return []
        changed = []
        default_keys = {
            k for k, v in default_table.items()
            if not (isinstance(v, Table) and not isinstance(v, InlineTable))
        }
        for key in default_keys:
            value = defaults.get(section, key)
            if self.get(section, key) != value or not self.has(section, key):
                self.set(section, key, value)
                changed.append(key)
        for key in list(table.keys()):
            item = table[key]
            if isinstance(item, Table) and not isinstance(item, InlineTable):
                continue
            if key not in default_keys:
                del table[key]
                changed.append(key)
        return changed


# --------------------------------------------------------------------------
# Validation through the real loader


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _port_problems(data: dict) -> list[str]:
    problems = []
    for section, key in sorted(PORT_KEYS):
        value = data.get(section, {}).get(key) if isinstance(data.get(section), dict) else None
        if value is None:
            continue
        minimum = 0 if key == "query_port" else 1
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= 65535:
            problems.append(f"{section}.{key} must be a UDP port number between {minimum} and 65535")
    server = data.get("server", {})
    if isinstance(server, dict):
        name = server.get("name")
        if name is not None and (not isinstance(name, str) or not name.strip()):
            problems.append("server.name cannot be empty")
        players = server.get("max_players")
        if players is not None and (isinstance(players, bool) or not isinstance(players, int) or not 1 <= players <= 255):
            problems.append("server.max_players must be between 1 and 255")
    return problems


def validate_text(text: str, *, maps_dir: Path | None = None) -> ValidationResult:
    """Validate TOML text exactly as the server would load it at startup."""

    try:
        data = tomllib.loads(text) if tomllib is not None else tomlkit.parse(text).unwrap()
    except Exception as exc:
        return ValidationResult(False, f"TOML syntax error: {exc}")
    problems = _port_problems(data)
    if problems:
        return ValidationResult(False, "; ".join(problems))

    from server.config import admin_password_problem, load_config

    capture = _Capture()
    root_logger = logging.getLogger()
    root_logger.addHandler(capture)
    printed = io.StringIO()
    fd, temporary = tempfile.mkstemp(prefix="battlespades-validate-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        with redirect_stdout(printed):
            config = load_config(Path(temporary))
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return ValidationResult(False, str(exc))
    finally:
        root_logger.removeHandler(capture)
        try:
            os.unlink(temporary)
        except OSError:
            pass
    if printed.getvalue().strip():
        # load_config prints and falls back to defaults on a parse failure.
        return ValidationResult(False, printed.getvalue().strip())
    warnings = [
        message.replace(temporary, "config.toml")
        for message in dict.fromkeys(capture.messages)
        if "/admin login" not in message
    ]
    password_problem = admin_password_problem(getattr(config, "admin_password", ""))
    if password_problem:
        warnings.append(
            f"In-game /admin login stays disabled: {password_problem} "
            "(needs a unique secret of 12+ characters)."
        )
    if maps_dir is not None:
        map_name = str(getattr(config, "default_map", ""))
        if map_name and not (Path(maps_dir) / f"{map_name}.vxl").is_file():
            return ValidationResult(False, f"game.default_map {map_name!r} is not in {maps_dir}")
    return ValidationResult(True, warnings=warnings, config=config)


def load_defaults(candidates: Iterable[Path]) -> ConfigDocument | None:
    for path in candidates:
        try:
            if Path(path).is_file():
                return ConfigDocument.load(Path(path))
        except Exception:
            continue
    return None
