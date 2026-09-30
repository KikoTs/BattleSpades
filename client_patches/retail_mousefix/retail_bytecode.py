"""Test-only, read-only access to methods in the supported Python 2.7 bundle.

No game module is imported or executed. This avoids treating decompiler
control-flow errors as defects in the real retail game.
"""
import hashlib
import marshal
import struct
import types
import zlib


class RetailBundle(object):
    def __init__(self, path):
        with open(path, 'rb') as stream:
            archive = stream.read()
        expected = 'c0d0cdc6f61f4b58172f74faf036c6f323b1cdbe59193f595fcce7d2a524e52c'
        if hashlib.sha256(archive).hexdigest() != expected:
            raise ValueError('Only the inspected retail bundle is supported')
        cookie = archive.rfind(b'MEI\x0c\x0b\x0a\x0b\x0e')
        length, offset, table_length = struct.unpack('>III', archive[cookie + 8:cookie + 20])
        base = len(archive) - length
        cursor = base + offset
        while cursor < base + offset + table_length:
            size, position, packed, plain, compressed, kind = struct.unpack('>IIIIBc', archive[cursor:cursor + 18])
            if kind == b'z':
                data = archive[base + position:base + position + packed]
                self.pyz = zlib.decompress(data) if compressed else data
                break
            cursor += size
        else:
            raise ValueError('Missing PYZ archive')
        table = struct.unpack('>I', self.pyz[8:12])[0]
        self.index = marshal.loads(self.pyz[table:])

    def code(self, module, *names):
        package, start, length = self.index[module]
        code = marshal.loads(zlib.decompress(self.pyz[start:start + length]))
        for name in names:
            code = next(value for value in code.co_consts
                        if isinstance(value, types.CodeType) and value.co_name == name)
        return code

    def method(self, module, class_name, name, namespace=None):
        scope = {'__builtins__': __builtins__}
        scope.update(namespace or {})
        return types.FunctionType(self.code(module, class_name, name), scope)
