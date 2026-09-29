"""Read retail Python 2.7 bytecode as inert data, from Python 3.

The retail game ships its per-map metadata as compiled Python 2.7 modules
(``maps/<Map>.txtc``) and its map catalogue as a module inside the PyInstaller
archive ``aos.pkg`` (``playlists.mapinfo``).  Those modules only assign
literals, so the recovery path never executes them: this module unmarshals the
Python 2.7 code object with a small reader and replays the module body on a
stack machine that knows only constant-building opcodes.  Any name lookup
other than ``True``/``False``/``None``, any call, attribute access or import
raises ``InertError`` instead of running.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path


class InertError(ValueError):
    """Raised when retail bytecode is not a pure literal-assignment module."""


@dataclass(frozen=True)
class Code27:
    """The fields of a Python 2.7 code object the evaluator needs."""

    code: bytes
    consts: tuple
    names: tuple
    filename: str
    name: str


class _Reader:
    """Python 2.7 ``marshal`` format (version 2) reader."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0
        self.interned: list[str] = []

    def _take(self, count: int) -> bytes:
        end = self.pos + count
        if count < 0 or end > len(self.data):
            raise InertError("truncated marshal data")
        chunk = self.data[self.pos:end]
        self.pos = end
        return chunk

    def _int32(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def _string(self) -> bytes:
        return self._take(self._int32())

    def read(self, depth: int = 0):
        if depth > 64:
            raise InertError("marshal nesting too deep")
        kind = self._take(1)
        if kind == b"N":
            return None
        if kind == b"F":
            return False
        if kind == b"T":
            return True
        if kind == b"0":
            return _NULL
        if kind == b"i":
            return self._int32()
        if kind == b"I":
            return struct.unpack("<q", self._take(8))[0]
        if kind == b"g":
            return struct.unpack("<d", self._take(8))[0]
        if kind == b"f":
            return float(self._take(self._take(1)[0]).decode("ascii"))
        if kind == b"l":
            count = self._int32()
            value = 0
            for shift, _ in enumerate(range(abs(count))):
                digit = struct.unpack("<H", self._take(2))[0]
                value |= digit << (15 * shift)
            return -value if count < 0 else value
        if kind == b"s":
            raw = self._string()
            try:
                return raw.decode("ascii")
            except UnicodeDecodeError:
                return raw
        if kind == b"t":
            text = self._string().decode("latin-1")
            self.interned.append(text)
            return text
        if kind == b"R":
            index = self._int32()
            if not 0 <= index < len(self.interned):
                raise InertError("bad interned string reference")
            return self.interned[index]
        if kind == b"u":
            return self._string().decode("utf-8")
        if kind in (b"(", b"[", b"<", b">"):
            count = self._int32()
            if not 0 <= count <= 1_000_000:
                raise InertError("bad container size")
            items = [self.read(depth + 1) for _ in range(count)]
            if kind == b"(":
                return tuple(items)
            if kind == b"[":
                return items
            return frozenset(items)
        if kind == b"{":
            result = {}
            while True:
                key = self.read(depth + 1)
                if key is _NULL:
                    return result
                result[key] = self.read(depth + 1)
        if kind == b"c":
            self._take(16)  # argcount, nlocals, stacksize, flags
            code = self.read(depth + 1)
            consts = self.read(depth + 1)
            names = self.read(depth + 1)
            for _ in range(3):  # varnames, freevars, cellvars
                self.read(depth + 1)
            filename = self.read(depth + 1)
            name = self.read(depth + 1)
            self._take(4)  # firstlineno
            self.read(depth + 1)  # lnotab
            if isinstance(code, str):
                code = code.encode("latin-1")
            return Code27(
                code=bytes(code),
                consts=tuple(consts),
                names=tuple(names),
                filename=str(filename),
                name=str(name),
            )
        raise InertError(f"unsupported marshal type {kind!r}")


_NULL = object()


def loads(data: bytes):
    """Unmarshal one Python 2.7 object."""

    return _Reader(data).read()


def load_pyc(path: str | Path) -> Code27:
    """Read a Python 2.7 ``.pyc``-layout file (retail ``.txtc``)."""

    data = Path(path).read_bytes()
    if len(data) < 8 or data[2:4] != b"\r\n":
        raise InertError(f"{path} is not a Python 2 compiled module")
    code = loads(data[8:])
    if not isinstance(code, Code27):
        raise InertError(f"{path} does not contain a module code object")
    return code


# Python 2.7 opcodes accepted by the evaluator.
_POP_TOP = 1
_ROT_TWO = 2
_DUP_TOP = 4
_UNARY_POSITIVE = 10
_UNARY_NEGATIVE = 11
_BINARY_MULTIPLY = 20
_BINARY_DIVIDE = 21
_BINARY_ADD = 23
_BINARY_SUBTRACT = 24
_BINARY_TRUE_DIVIDE = 27
_STORE_MAP = 54
_RETURN_VALUE = 83
_HAVE_ARGUMENT = 90
_STORE_NAME = 90
_LOAD_CONST = 100
_LOAD_NAME = 101
_BUILD_TUPLE = 102
_BUILD_LIST = 103
_BUILD_MAP = 105
_EXTENDED_ARG = 145

_SAFE_NAMES = {"True": True, "False": False, "None": None}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InertError("arithmetic on a non-number")
    return value


def evaluate_module(code: Code27) -> dict[str, object]:
    """Replay a literal-only module body and return its assignments in order.

    ``dict`` preserves assignment order so generated sidecars keep the retail
    author's field order, which keeps diffs against the source readable.
    """

    namespace: dict[str, object] = {}
    stack: list[object] = []
    raw = code.code
    pos = 0
    extended = 0
    steps = 0
    while pos < len(raw):
        steps += 1
        if steps > 1_000_000:
            raise InertError("module body too long")
        opcode = raw[pos]
        pos += 1
        arg = 0
        if opcode >= _HAVE_ARGUMENT:
            if pos + 2 > len(raw):
                raise InertError("truncated bytecode")
            arg = raw[pos] | (raw[pos + 1] << 8) | extended
            pos += 2
            extended = 0
        try:
            if opcode == _EXTENDED_ARG:
                extended = arg << 16
            elif opcode == _LOAD_CONST:
                value = code.consts[arg]
                if isinstance(value, Code27):
                    raise InertError("nested code objects are not data")
                stack.append(value)
            elif opcode == _LOAD_NAME:
                name = code.names[arg]
                if name in namespace:
                    stack.append(namespace[name])
                elif name in _SAFE_NAMES:
                    stack.append(_SAFE_NAMES[name])
                else:
                    raise InertError(f"module reads unknown name {name!r}")
            elif opcode == _STORE_NAME:
                namespace[code.names[arg]] = stack.pop()
            elif opcode in (_BUILD_TUPLE, _BUILD_LIST):
                items = stack[len(stack) - arg:] if arg else []
                del stack[len(stack) - arg:]
                stack.append(tuple(items) if opcode == _BUILD_TUPLE else list(items))
            elif opcode == _BUILD_MAP:
                stack.append({})
            elif opcode == _STORE_MAP:
                key = stack.pop()
                value = stack.pop()
                target = stack[-1]
                if not isinstance(target, dict):
                    raise InertError("STORE_MAP without a dict")
                target[key] = value
            elif opcode == _UNARY_NEGATIVE:
                stack.append(-_number(stack.pop()))
            elif opcode == _UNARY_POSITIVE:
                stack.append(+_number(stack.pop()))
            elif opcode in (
                _BINARY_ADD, _BINARY_SUBTRACT, _BINARY_MULTIPLY,
                _BINARY_DIVIDE, _BINARY_TRUE_DIVIDE,
            ):
                right = _number(stack.pop())
                left = _number(stack.pop())
                if opcode == _BINARY_ADD:
                    stack.append(left + right)
                elif opcode == _BINARY_SUBTRACT:
                    stack.append(left - right)
                elif opcode == _BINARY_MULTIPLY:
                    stack.append(left * right)
                elif opcode == _BINARY_DIVIDE and all(
                    isinstance(value, int) for value in (left, right)
                ):
                    stack.append(left // right)  # Python 2 integer division
                else:
                    stack.append(left / right)
            elif opcode == _POP_TOP:
                stack.pop()
            elif opcode == _DUP_TOP:
                stack.append(stack[-1])
            elif opcode == _ROT_TWO:
                stack[-1], stack[-2] = stack[-2], stack[-1]
            elif opcode == _RETURN_VALUE:
                return namespace
            else:
                raise InertError(f"opcode {opcode} is not a literal operation")
        except (IndexError, ZeroDivisionError) as exc:
            raise InertError(f"malformed module body: {exc}") from exc
    raise InertError("module body did not return")


def load_literal_module(path: str | Path) -> dict[str, object]:
    """Return the literal assignments of a retail ``.txtc`` without running it."""

    return evaluate_module(load_pyc(path))


# --------------------------------------------------------------------------
# PyInstaller archive (aos.pkg) access
# --------------------------------------------------------------------------

_CARCHIVE_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"


def _pyz_bytes(package: bytes) -> bytes:
    """Return the embedded PYZ archive from a PyInstaller 2.x CArchive."""

    if package.startswith(b"PYZ\x00"):
        return package
    cookie = package.rfind(_CARCHIVE_MAGIC)
    if cookie < 0:
        raise InertError("no PyInstaller cookie")
    _magic, length, toc_offset, toc_length, _pyver = struct.unpack(
        "!8siiii", package[cookie:cookie + 24]
    )
    # The cookie closes the package; ``length`` spans the whole package, which
    # may itself be appended to an executable.
    base = max(0, len(package) - length)
    pos = base + toc_offset
    end = pos + toc_length
    while pos < end:
        entry_size, entry_pos, entry_len, _ulen, flag, typecode = struct.unpack(
            "!iiiiBc", package[pos:pos + 18]
        )
        if entry_size <= 0:
            break
        if typecode == b"z":
            blob = package[base + entry_pos:base + entry_pos + entry_len]
            return zlib.decompress(blob) if flag else blob
        pos += entry_size
    raise InertError("archive has no PYZ entry")


def load_archive_module(package_path: str | Path, module: str) -> dict[str, object]:
    """Evaluate one literal-only module stored in ``aos.pkg`` (or a ``.pyz``)."""

    pyz = _pyz_bytes(Path(package_path).read_bytes())
    if not pyz.startswith(b"PYZ\x00"):
        raise InertError("not a PYZ archive")
    toc_offset = struct.unpack("!i", pyz[8:12])[0]
    toc = loads(pyz[toc_offset:])
    if isinstance(toc, list):
        toc = dict(toc)
    if not isinstance(toc, dict) or module not in toc:
        raise InertError(f"module {module!r} not in archive")
    _is_package, pos, length = toc[module]
    code = loads(zlib.decompress(pyz[pos:pos + length]))
    if not isinstance(code, Code27):
        raise InertError(f"archive entry {module!r} is not code")
    return evaluate_module(code)
