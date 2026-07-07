# -*- coding: utf-8 -*-
"""
Probe the original Ace of Spades 1.x compiled libraries from
../aceofspades_nonsteam/ to verify the API contracts the test
harness will rely on.

Run with Python 2.7 32-bit:
    py2 scripts/probe_originals.py [--out path.json]

Prints one JSON line per check to stdout. Each line:
    {"check": "<name>", "ok": true/false, "data": {...}, "error": "..."}

Exit code: 0 if all checks pass, 1 otherwise.

This script intentionally has no dependencies beyond the bundled Python 2
stdlib + the compiled .pyd modules in ../aceofspades_nonsteam/.
"""
from __future__ import print_function

import json
import os
import sys
import time
import traceback


HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
NONSTEAM = os.path.normpath(os.path.join(REPO_ROOT, '..', 'aceofspades_nonsteam'))


_results = []


def check(name):
    """Decorator: run the function, capture pass/fail + data, append to results."""
    def deco(fn):
        rec = {'check': name, 'ok': False}
        t0 = time.time()
        try:
            data = fn()
            rec['ok'] = True
            if data is not None:
                rec['data'] = data
        except Exception as e:
            rec['error'] = '{}: {}'.format(type(e).__name__, e)
            rec['traceback'] = traceback.format_exc().splitlines()[-6:]
        rec['ms'] = int((time.time() - t0) * 1000)
        _results.append(rec)
        # stream as we go so a hang shows partial output
        print(json.dumps(rec))
        sys.stdout.flush()
        return fn
    return deco


# ---------------------------------------------------------------------------
# 0. Environment sanity
# ---------------------------------------------------------------------------

@check('python.version_and_bitness')
def _env():
    return {
        'version': sys.version.splitlines()[0],
        'is_32bit': sys.maxsize <= 2**32,
        'platform': sys.platform,
        'nonsteam_root': NONSTEAM,
        'nonsteam_exists': os.path.isdir(NONSTEAM),
    }


# Make originals importable
sys.path.insert(0, NONSTEAM)


# ---------------------------------------------------------------------------
# 1. enet.pyd
# ---------------------------------------------------------------------------

@check('enet.import')
def _enet_import():
    import enet  # noqa
    return {
        'module_file': getattr(enet, '__file__', None),
        'has_Host': hasattr(enet, 'Host'),
        'has_Address': hasattr(enet, 'Address'),
        'has_Peer': hasattr(enet, 'Peer'),
        'has_Packet': hasattr(enet, 'Packet'),
        'event_types': [e for e in dir(enet) if e.startswith('EVENT_TYPE_')],
        'packet_flags': [e for e in dir(enet) if e.startswith('PACKET_FLAG_')],
    }


@check('enet.client_host_construct')
def _enet_client():
    """Verify we can create a client-mode Host (no bind address, peerCount=1)."""
    import enet
    host = enet.Host(None, peerCount=1, channelLimit=1,
                     incomingBandwidth=0, outgoingBandwidth=0)
    out = {'host_repr': repr(host)}
    # Try compress_with_range_coder — server uses it; client must too.
    try:
        host.compress_with_range_coder()
        out['compress_with_range_coder'] = 'ok'
    except Exception as e:
        out['compress_with_range_coder'] = 'FAIL: ' + repr(e)
    return out


# ---------------------------------------------------------------------------
# 2. shared.bytes
# ---------------------------------------------------------------------------

@check('shared.bytes.import_and_api')
def _bytes():
    import shared.bytes as B
    # Find ByteReader / ByteWriter
    public = [n for n in dir(B) if not n.startswith('_')]
    out = {
        'module_file': getattr(B, '__file__', None),
        'has_ByteReader': hasattr(B, 'ByteReader'),
        'has_ByteWriter': hasattr(B, 'ByteWriter'),
        'public_count': len(public),
        'sample_names': sorted([n for n in public
                                if any(k in n for k in ('Byte', 'Reader', 'Writer', 'NoData'))])[:20],
    }
    if hasattr(B, 'ByteWriter'):
        w = B.ByteWriter()
        out['ByteWriter_methods'] = sorted([m for m in dir(w) if not m.startswith('_')])[:30]
        # Try a tiny round-trip
        try:
            w.writeByte(0x42)
            w.writeShort(0x1234)
            w.writeInt(0xCAFEBABE)
            raw = bytes(w)
            out['BW_writeByte_writeShort_writeInt'] = 'ok'
            out['BW_round_trip_bytes_hex'] = ''.join('{:02x}'.format(ord(c)) for c in raw)
        except Exception:
            # Try alternate names
            try:
                w2 = B.ByteWriter()
                w2.write_byte(0x42)
                w2.write_short(0x1234)
                w2.write_int(0xCAFEBABE)
                out['BW_snake_case_works'] = True
                out['BW_round_trip_bytes_hex'] = ''.join('{:02x}'.format(ord(c)) for c in bytes(w2))
            except Exception as e:
                out['BW_neither_camel_nor_snake'] = repr(e)
    if hasattr(B, 'ByteReader'):
        r = B.ByteReader('\x42\x34\x12\xbe\xba\xfe\xca')
        out['ByteReader_methods'] = sorted([m for m in dir(r) if not m.startswith('_')])[:30]
    return out


# ---------------------------------------------------------------------------
# 3. shared.lzf — REAL lzf or our fake-chunking?
# ---------------------------------------------------------------------------

@check('shared.lzf.import_and_compress')
def _lzf():
    import shared.lzf as L
    out = {
        'module_file': getattr(L, '__file__', None),
        'public': sorted([n for n in dir(L) if not n.startswith('_')]),
    }
    # Test a known input: 64 'A' bytes. Real LZF would emit ~3 bytes of run-length;
    # our server's "fake LZF" chunking emits 32 bytes + length-byte + 32 bytes (~66 bytes).
    sample = b'A' * 64
    if hasattr(L, 'compress'):
        try:
            comp = L.compress(sample)
            out['compress_64A_len'] = len(comp) if comp else None
            out['compress_64A_first16_hex'] = ''.join('{:02x}'.format(ord(c)) for c in comp[:16]) if comp else None
        except Exception as e:
            out['compress_error'] = repr(e)
    # Test decompress round-trip
    if hasattr(L, 'compress') and hasattr(L, 'decompress'):
        try:
            comp = L.compress(sample)
            dec = L.decompress(comp, len(sample))
            out['round_trip_ok'] = (dec == sample) if dec else False
        except Exception as e:
            try:
                # decompress may not need length
                dec2 = L.decompress(comp)
                out['round_trip_ok_no_len'] = (dec2 == sample)
            except Exception as e2:
                out['decompress_error'] = repr(e) + ' / ' + repr(e2)
    return out


@check('shared.lzf.vs_server_chunking')
def _lzf_compare():
    """
    Compare original shared.lzf.compress() output to BattleSpades server's
    util.lzf_compress() chunking-only output. This tells us whether the
    server's wire format is REAL lzf or a custom chunking format.
    """
    import shared.lzf as L

    # Reimplement server util.lzf_compress here (server is py3, we are py2).
    def server_chunk_compress(s):
        result = bytearray()
        while len(s) > 32:
            result.extend(b'\x1F' + s[:32])
            s = s[32:]
        if len(s) > 0:
            result.extend(bytes(bytearray([len(s) - 1])) + s)
        return bytes(result)

    sample = b'A' * 64
    server_out = server_chunk_compress(sample)
    real_out = L.compress(sample) if hasattr(L, 'compress') else None
    return {
        'sample_input_len': len(sample),
        'server_chunking_len': len(server_out),
        'server_chunking_hex_first32': ''.join('{:02x}'.format(b) for b in bytearray(server_out[:32])),
        'real_lzf_len': len(real_out) if real_out else None,
        'real_lzf_hex_first32': (''.join('{:02x}'.format(ord(c)) for c in real_out[:32])
                                 if real_out else None),
        'are_they_equal': server_out == real_out,
    }


# ---------------------------------------------------------------------------
# 4. shared.packet — class catalog and API shape
# ---------------------------------------------------------------------------

# The classes we care about for the handshake / first scenarios.
KEY_CLASSES = [
    'SteamSessionTicket',  # 105
    'InitialInfo',         # 114
    'MapDataValidation',   # 60
    'MapSyncStart',        # 55
    'MapSyncChunk',        # 57
    'MapSyncEnd',          # 59
    'StateData',           # 45
    'SkyboxData',          # 51
    'ExistingPlayer',      # 14
    'NewPlayerConnection', # 15
    'CreatePlayer',        # 28
    'SetHP',               # 5
    'ClockSync',           # 0
    'ClientData',          # 4
    'PositionData',        # 116
    'WorldUpdate',         # 2
    'ChatMessage',         # 49
    'PlayerLeft',          # 64
]


@check('shared.packet.import_and_class_ids')
def _packet_classes():
    import shared.packet as P
    out = {'module_file': getattr(P, '__file__', None), 'classes': {}}
    for name in KEY_CLASSES:
        cls = getattr(P, name, None)
        if cls is None:
            out['classes'][name] = {'present': False}
            continue
        info = {'present': True, 'has_id_attr': hasattr(cls, 'id')}
        if hasattr(cls, 'id'):
            info['id'] = cls.id
        info['has_read'] = hasattr(cls, 'read')
        info['has_generate'] = hasattr(cls, 'generate')
        info['has_write'] = hasattr(cls, 'write')
        out['classes'][name] = info
    return out


@check('shared.packet.api_shape_steam_ticket')
def _api_shape_steam_ticket():
    """
    SteamSessionTicket(105). Try both constructor patterns to see which the
    real .pyd uses:
      a) p = Cls(); p.field = ...; p.read(reader)
      b) p = Cls(reader)              (pre-fills from reader)
      c) p = Cls(); p.write(writer)
      d) p = Cls(); buf = p.generate() (returns ByteWriter or bytes)
    """
    import shared.packet as P
    Cls = P.SteamSessionTicket
    out = {}

    # Pattern (a): default-construct, set fields, generate
    try:
        p = Cls()
        # Find the public field names
        out['default_ctor_fields'] = sorted([m for m in dir(p)
                                              if not m.startswith('_') and not callable(getattr(p, m, None))])[:20]
        # Set ticket to a tiny placeholder and call generate
        if hasattr(p, 'ticket'):
            p.ticket = b''
        if hasattr(p, 'ticket_size'):
            p.ticket_size = 0
        gen = None
        if hasattr(p, 'generate'):
            gen = p.generate()
        out['generate_type'] = type(gen).__name__ if gen is not None else None
        if gen is not None:
            try:
                raw = bytes(gen)
                out['generate_bytes_len'] = len(raw)
                out['generate_first16_hex'] = ''.join('{:02x}'.format(ord(c)) for c in raw[:16])
                # Crucial: does the first byte == cls.id (105 = 0x69)?
                if hasattr(Cls, 'id') and len(raw) > 0:
                    out['first_byte_equals_id'] = (ord(raw[0]) == Cls.id)
                    out['first_byte'] = ord(raw[0])
            except Exception as e:
                out['generate_to_bytes_error'] = repr(e)
    except Exception as e:
        out['default_ctor_error'] = repr(e)

    # Pattern (b): construct-from-reader — only meaningful if we have bytes to feed
    return out


@check('shared.packet.api_shape_clock_sync')
def _api_shape_clock_sync():
    """
    ClockSync(0). Smallest packet: client_time + server_loop_count.
    Round-trip: build, generate, then read back, compare fields.
    """
    import shared.packet as P
    import shared.bytes as B
    Cls = P.ClockSync
    out = {}

    p = Cls()
    # client_time is a signed int32 — keep value within range
    if hasattr(p, 'client_time'):
        p.client_time = 0x12345678
    if hasattr(p, 'server_loop_count'):
        p.server_loop_count = 12345
    out['fields_set'] = {
        'client_time': getattr(p, 'client_time', None),
        'server_loop_count': getattr(p, 'server_loop_count', None),
    }
    gen = p.generate()
    raw = bytes(gen)
    out['raw_len'] = len(raw)
    out['raw_hex'] = ''.join('{:02x}'.format(ord(c)) for c in raw)
    out['first_byte'] = ord(raw[0]) if raw else None
    out['id_attr'] = getattr(Cls, 'id', None)
    out['first_byte_is_id'] = (out['first_byte'] == out['id_attr'])

    # Read back: skip first byte (id), feed rest to ByteReader, parse
    body = raw[1:] if out['first_byte_is_id'] else raw
    try:
        r = B.ByteReader(body)
        # Try ctor-from-reader first
        try:
            p2 = Cls(r)
            out['readback_ctor_from_reader'] = 'ok'
        except Exception as e_ctor:
            # Fall back to default ctor + read()
            try:
                p2 = Cls()
                p2.read(B.ByteReader(body))
                out['readback_default_then_read'] = 'ok'
                out['ctor_from_reader_failed'] = repr(e_ctor)
            except Exception as e_read:
                out['readback_failed'] = '{} / {}'.format(repr(e_ctor), repr(e_read))
                p2 = None
        if p2 is not None:
            out['fields_after_read'] = {
                'client_time': getattr(p2, 'client_time', None),
                'server_loop_count': getattr(p2, 'server_loop_count', None),
            }
            out['round_trip_ok'] = (
                getattr(p2, 'client_time', None) == p.client_time and
                getattr(p2, 'server_loop_count', None) == p.server_loop_count
            )
    except Exception as e:
        out['readback_error'] = repr(e)
    return out


@check('shared.packet.full_id_catalog')
def _packet_id_catalog():
    """List every shared.packet class that has an integer .id attribute,
    so we know the full receivable surface and can detect ID collisions."""
    import shared.packet as P
    catalog = {}
    for name in dir(P):
        if name.startswith('_'):
            continue
        cls = getattr(P, name)
        try:
            cid = getattr(cls, 'id', None)
            if isinstance(cid, int):
                catalog.setdefault(cid, []).append(name)
        except Exception:
            pass
    return {
        'unique_ids': len(catalog),
        'collisions': {k: v for k, v in catalog.items() if len(v) > 1},
        'min_id': min(catalog.keys()) if catalog else None,
        'max_id': max(catalog.keys()) if catalog else None,
        'sample': dict(sorted(catalog.items())[:5]),
    }


# ---------------------------------------------------------------------------
# 5. shared.constants — protocol version + key XOR constants
# ---------------------------------------------------------------------------

@check('shared.constants.protocol_version_etc')
def _constants():
    import shared.constants as C
    keys = [
        'PROTOCOL_VERSION', 'MASTER_VERSION', 'PACKET_COMPRESSION',
        'MAX_PACKET_DECOMPRESSION_SIZE', 'SPADES_GAME_APP_ID',
        'TEAM1', 'TEAM2', 'TEAM_SPECTATOR', 'TEAM_NEUTRAL',
        'PROXY_PACKET', 'PROXY_BYTES',
    ]
    return {k: getattr(C, k, '<missing>') for k in keys}


# ---------------------------------------------------------------------------
# 6. The XOR-with-steam-ticket layer — is it actually in shared.packet,
#    or did we invent it server-side?
# ---------------------------------------------------------------------------

@check('xor_layer.search_in_shared_packet')
def _xor_search():
    """Look for any obvious encrypt/decrypt or xor surface in shared.packet."""
    import shared.packet as P
    suspicious = []
    for name in dir(P):
        low = name.lower()
        if any(k in low for k in ('xor', 'crypt', 'cipher', 'decode_packet',
                                   'encode_packet', 'unpack', 'pack_packet')):
            suspicious.append(name)
    return {'suspicious_names': suspicious[:30]}


# ---------------------------------------------------------------------------
# 7. proxy.network — does the original network module reveal the wire layer?
# ---------------------------------------------------------------------------

@check('shared.lzf.decompress_client_with_server_chunking')
def _lzf_decompress_client():
    """
    The server emits a fake-LZF "chunking" format (0x1F + 32 bytes per chunk,
    last chunk = length-1 + body). Real shared.lzf.compress emits proper LZF
    bytes. The original module also exposes decompress_client(), which may
    accept the chunking format. Test it.
    """
    import shared.lzf as L

    def server_chunk(s):
        result = bytearray()
        while len(s) > 32:
            result.extend(b'\x1F' + s[:32])
            s = s[32:]
        if len(s) > 0:
            result.extend(bytes(bytearray([len(s) - 1])) + s)
        return bytes(result)

    sample = b'A' * 64
    chunked = server_chunk(sample)
    out = {'chunked_input_len': len(chunked)}

    # Try decompress_client — maybe length-prefixed?
    if hasattr(L, 'decompress_client'):
        for trial_name, trial_args in [
            ('no_args',         (chunked,)),
            ('with_outsize_64', (chunked, 64)),
            ('with_outsize_66', (chunked, 66)),
        ]:
            try:
                res = L.decompress_client(*trial_args)
                out['decompress_client.' + trial_name] = {
                    'len': len(res) if res else None,
                    'first16_hex': (''.join('{:02x}'.format(ord(c)) for c in res[:16])
                                     if res else None),
                    'matches_sample': (res == sample) if res else False,
                }
            except Exception as e:
                out['decompress_client.' + trial_name + '_error'] = repr(e)

    # Also test: what does real lzf.decompress() do with our chunked bytes?
    if hasattr(L, 'decompress'):
        try:
            res = L.decompress(chunked, 64)
            out['real_decompress_with_chunked.matches'] = (res == sample) if res else False
            out['real_decompress_with_chunked.bytes_first16'] = (
                ''.join('{:02x}'.format(ord(c)) for c in res[:16]) if res else None)
        except Exception as e:
            out['real_decompress_with_chunked_error'] = repr(e)
    return out


@check('shared.packet.chat_message_round_trip')
def _chat_round_trip():
    """ChatMessage(49): byte player_id, byte chat_type, string value."""
    import shared.packet as P
    import shared.bytes as B
    p = P.ChatMessage()
    p.player_id = 7
    p.chat_type = 0  # all
    p.value = u'hello world'
    raw = bytes(p.generate())
    out = {
        'raw_len': len(raw),
        'first_byte_is_id': (ord(raw[0]) == P.ChatMessage.id),
        'raw_hex': ''.join('{:02x}'.format(ord(c)) for c in raw),
    }
    # Read back
    body = raw[1:]
    p2 = P.ChatMessage()
    p2.read(B.ByteReader(body))
    out['readback'] = {
        'player_id': p2.player_id,
        'chat_type': p2.chat_type,
        'value': p2.value,
    }
    out['round_trip_ok'] = (p2.player_id == 7 and p2.chat_type == 0
                            and p2.value == u'hello world')
    return out


@check('shared.packet.map_data_validation_round_trip')
def _mdv_round_trip():
    """MapDataValidation(60): just a CRC int."""
    import shared.packet as P
    import shared.bytes as B
    p = P.MapDataValidation()
    p.crc = 0x12345678
    raw = bytes(p.generate())
    body = raw[1:]
    p2 = P.MapDataValidation()
    p2.read(B.ByteReader(body))
    return {
        'raw_len': len(raw),
        'first_byte': ord(raw[0]),
        'id': P.MapDataValidation.id,
        'crc_after_read': p2.crc,
        'round_trip_ok': p2.crc == 0x12345678,
    }


@check('shared.packet.block_coord_format')
def _block_coord_format():
    """Determine the wire format for block-position packets (x,y,z fields).
    Send a known integer x and inspect the bytes the original encoder writes.
    If raw short, our shared/packet.pyx is wrong to use fromfixed/tofixed.
    Tested packets: BlockBuild(32), BlockBuildColored(33), BlockOccupy(34),
    BlockLiberate(35), BlockLine(40)."""
    import shared.packet as P
    import struct

    def encode_x(cls_name, x_value):
        Cls = getattr(P, cls_name, None)
        if Cls is None:
            return None
        p = Cls()
        # All these have loop_count, player_id, x, y, z, ... fields
        if hasattr(p, 'loop_count'):
            p.loop_count = 0
        if hasattr(p, 'player_id'):
            p.player_id = 0
        if hasattr(p, 'shooter_id'):
            p.shooter_id = 0
        p.x = int(x_value)
        p.y = 0
        p.z = 0
        # BlockBuildColored may want color; BlockOccupy may want type; etc.
        for attr, default in (('block_type', 0), ('color', (0, 0, 0)),
                               ('color1', (0, 0, 0)), ('color2', (0, 0, 0)),
                               ('block_type2', 0), ('face', 0)):
            if hasattr(p, attr):
                try:
                    setattr(p, attr, default)
                except Exception:
                    pass
        try:
            raw = bytes(p.generate())
        except Exception as e:
            return {'encode_error': repr(e)}
        # x is at offset id(1) + loop_count(4) + player_id(1) = 6, 2 bytes LE
        if len(raw) < 8:
            return {'too_short': len(raw)}
        x_bytes = raw[6:8]
        x_short = struct.unpack('<h', x_bytes)[0]
        x_ushort = struct.unpack('<H', x_bytes)[0]
        return {
            'wire_hex': ''.join('{:02x}'.format(ord(c) if isinstance(c, str) else c)
                                 for c in raw),
            'x_bytes_hex': ''.join('{:02x}'.format(ord(c) if isinstance(c, str) else c)
                                    for c in x_bytes),
            'x_as_short': x_short,
            'x_as_ushort': x_ushort,
            'x_input': int(x_value),
            'is_raw_short': x_short == int(x_value),
            'is_fixed_x64': x_short == int(x_value) * 64,
        }

    out = {}
    for name in ('BlockBuild', 'BlockBuildColored', 'BlockOccupy',
                 'BlockLiberate'):
        try:
            out[name] = encode_x(name, 77)
        except Exception as e:
            out[name] = {'error': repr(e)}
    return out


@check('shared.packet.client_data_orientation_scale')
def _client_data_orientation_scale():
    """Find the scale factor the original ClientData uses to encode orientation
    floats into 16-bit shorts. Send known values, read back the raw bytes,
    derive scale by reading the s16 magnitude.

    ClientData wire layout (from protocol doc):
      0    : packet id (4)
      1-4  : loop_count (int32 LE)
      5    : player_id (byte)
      6    : tool_id (byte)
      7-8  : o_x  (orientation, 2 bytes)
      9-10 : o_y
      11-12: o_z
    """
    import shared.packet as P
    import struct

    def encode_o_y(value):
        cd = P.ClientData()
        cd.loop_count = 0
        cd.player_id = 0
        cd.tool_id = 0
        cd.o_x = 0.0
        cd.o_y = value
        cd.o_z = 0.0
        cd.ooo = 0
        cd.up = cd.down = cd.left = cd.right = False
        cd.jump = cd.crouch = cd.sneak = cd.sprint = False
        cd.primary = cd.secondary = cd.zoom = False
        cd.can_pickup = cd.can_display_weapon = False
        cd.is_on_fire = cd.is_weapon_deployed = cd.hover = False
        cd.weapon_deployment_yaw = 0.0
        raw = bytes(cd.generate())
        # o_y at offset 9, signed 16-bit little-endian
        s16 = struct.unpack('<h', raw[9:11])[0]
        u16 = struct.unpack('<H', raw[9:11])[0]
        return raw, s16, u16

    out = {}
    for v in (0.1, 0.25, 0.5, 0.75, 0.9, 0.99, 1.0, 1.001, 1.1, 1.5, 1.9, 1.999, 2.0,
              -0.5, -1.0, -1.5, -2.0):
        raw, s16, u16 = encode_o_y(v)
        out['o_y={}'.format(v)] = {
            'hex_o_y': ''.join('{:02x}'.format(ord(c) if isinstance(c, str) else c)
                                for c in raw[9:11]),
            's16': s16,
            'u16': u16,
            'derived_scale_if_signed_magnitude': (
                # signed-magnitude: bit 15 = sign, bits 0-14 = magnitude
                (u16 & 0x7FFF) / abs(v) if v else None
            ),
            'derived_scale_if_twos_complement': (
                s16 / v if v else None
            ),
        }
    return out


# Note: an earlier probe scanned ../archive/nonsteam-aceofspades_decompiled/
# for "decrypt" / "steam_key" hits and matched them inside what we initially
# thought was the original AoS dedicated server. On second look the import
# patterns and Py2/3 compat shims in that tree were AI-generated, not
# decompiler output — it was a previous server-rewrite attempt, archived.
# The XOR layer in our server matches the protocol observed on the wire;
# we simply don't have the original server's source as a reference.


# ---------------------------------------------------------------------------
# Summary / exit
# ---------------------------------------------------------------------------

def main():
    summary = {
        'check': '__summary__',
        'total': len(_results),
        'passed': sum(1 for r in _results if r.get('ok')),
        'failed': sum(1 for r in _results if not r.get('ok')),
        'failures': [r['check'] for r in _results if not r.get('ok')],
    }
    print(json.dumps(summary))
    sys.stdout.flush()

    # Optional --out path.json
    out_path = None
    args = sys.argv[1:]
    if '--out' in args:
        i = args.index('--out')
        if i + 1 < len(args):
            out_path = args[i + 1]
    if out_path:
        with open(out_path, 'wb') as f:
            f.write(json.dumps({'results': _results, 'summary': summary},
                                indent=2).encode('utf-8'))

    sys.exit(0 if summary['failed'] == 0 else 1)


if __name__ == '__main__':
    main()
