"""Execute the shipped x86 arithmetic, stopping before peer/map collisions.

No server or gameplay capture is used to generate expected results. Only the
external CRT sqrt import is replaced with x87 fsqrt. The original core and
normalization instructions execute unmodified in Unicorn.
"""
import argparse
import hashlib
import itertools
import json
import struct
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--binary', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--dependency-dir', type=Path,
                    help='Optional isolated installation of pefile/unicorn')
args = parser.parse_args()
if args.dependency_dir:
    sys.path.insert(0, str(args.dependency_dir))
import pefile
from unicorn import Uc, UC_ARCH_X86, UC_MODE_32
from unicorn.x86_const import UC_X86_REG_ESP, UC_X86_REG_EIP, UC_X86_REG_FPCW

BINARY = args.binary
EXPECTED_HASH = 'ae45ec007e312c8d650620bc2779169f7b7461c74192b7a7480342c21237c1a0'
actual_hash = hashlib.sha256(BINARY.read_bytes()).hexdigest()
if actual_hash != EXPECTED_HASH:
    raise SystemExit(f'Original binary SHA-256 mismatch: expected {EXPECTED_HASH}, got {actual_hash}')
pe = pefile.PE(str(BINARY))
uc = Uc(UC_ARCH_X86, UC_MODE_32)
image = pe.get_memory_mapped_image()
uc.mem_map(pe.OPTIONAL_HEADER.ImageBase, (len(image) + 4095) & ~4095)
uc.mem_write(pe.OPTIONAL_HEADER.ImageBase, image)
uc.mem_map(0x20000000, 0x20000)
uc.mem_write(0x20010000, bytes.fromhex('DD442404 D9FA C3'))
uc.mem_write(0x20010020, bytes.fromhex('D9FA C3'))
for dll in pe.DIRECTORY_ENTRY_IMPORT:
    for entry in dll.imports:
        if entry.name == b'sqrt':
            uc.mem_write(entry.address, struct.pack('<I', 0x20010000))
        elif entry.name == b'_CIsqrt':
            uc.mem_write(entry.address, struct.pack('<I', 0x20010020))
uc.reg_write(UC_X86_REG_FPCW, 0x37f)
baseline = uc.context_save()
PLAYER, STACK = 0x20000000, 0x20008000
FLAGS = {'up':84, 'down':88, 'left':92, 'right':96, 'jump':100,
         'crouch':108, 'sneak':112, 'sprint':116, 'burdened':120,
         'hover':124, 'airborne':152, 'wade':156, 'active':176,
         'passive':180, 'parachute':184, 'parachute_active':188}


def original_step(case):
    uc.context_restore(baseline)
    player = bytearray(256)
    def f(offset, value):
        struct.pack_into('<f', player, offset, value)
    def i(offset, value):
        struct.pack_into('<I', player, offset, int(value))
    for offset, value in {0:100.5, 4:100.5, 8:100.0,
                          24:case['velocity'][0], 28:case['velocity'][1],
                          32:case['velocity'][2], 40:1.0, 60:1.0,
                          192:0.7, 196:1.2, 200:1.4, 204:0.5,
                          208:1.0, 212:8.0, 216:3, 220:40, 224:100,
                          232:1.0}.items():
        f(offset, value)
    for name, offset in FLAGS.items():
        i(offset, case.get(name, False))
    i(164, 1)
    i(172, case['pack'])
    uc.mem_write(PLAYER, bytes(player))
    uc.mem_write(0x10022D40, struct.pack('<f', case['dt']))
    uc.mem_write(0x10022DF0, struct.pack('<f', 1.0))
    uc.mem_write(STACK, struct.pack('<5I', 0x2001ffff, PLAYER, 0, 0, 0))
    uc.reg_write(UC_X86_REG_ESP, STACK)
    try:
        uc.emu_start(0x10012B80, 0x1001304A, count=10000)
    except Exception as error:
        raise RuntimeError(f"at {uc.reg_read(UC_X86_REG_EIP):#x}: {case}") from error
    stopped_at = uc.reg_read(UC_X86_REG_EIP)
    if stopped_at != 0x1001304A:
        raise RuntimeError(f'Original movement execution stopped at {stopped_at:#x}, expected 0x1001304a')
    return {'velocity': list(struct.unpack('<3f', uc.mem_read(PLAYER + 24, 12))),
            'jump': bool(struct.unpack('<I', uc.mem_read(PLAYER + 100, 4))[0]),
            'jump_this_frame': bool(struct.unpack('<I', uc.mem_read(PLAYER + 104, 4))[0])}


def main():
    cases = []
    controls = [{}, {'up':True}, {'up':True,'right':True,'sprint':True},
                {'up':True,'down':True,'left':True,'right':True},
                {'up':True,'jump':True}, {'up':True,'crouch':True},
                {'jump':True,'crouch':True,'hover':True},
                {'hover':True,'crouch':True}, {'up':True,'sneak':True},
                {'up':True,'sprint':True,'burdened':True}]
    for dt, airborne, wade, pack, mode in itertools.product(
            (1/60, 1/20), (False, True), (False, True), range(5),
            ('inactive', 'active', 'passive', 'parachute')):
        for control in controls:
            case = dict(dt=dt, airborne=airborne, wade=wade, pack=pack,
                        active=mode=='active', passive=mode=='passive',
                        parachute=mode=='parachute', parachute_active=mode=='parachute',
                        velocity=[0.1234567, -0.2345678, 0.3456789], **control)
            case['expected'] = original_step(case)
            cases.append(case)
    document = {'binary_sha256':EXPECTED_HASH, 'entry':'0x10012B80',
                'stop_before_collision':'0x1001304A', 'sqrt':'x87 fsqrt stub',
                'harness_version':1, 'x87_control_word':'0x037f',
                'fixed_basis':{'orientation':[1,0,0], 'side':[0,1,0]},
                'scope':'Single-step arithmetic; collision, fuel, scheduling and deployed CRT/FPU state are not verified.',
                'cases':cases}
    # Compact one vector per line keeps generated fixture diffs readable.
    header = {key:value for key,value in document.items() if key != 'cases'}
    content = json.dumps(header, indent=2).rstrip()[:-1]
    content += ',\n  "cases": [\n'
    content += ',\n'.join('    ' + json.dumps(case, separators=(',', ':')) for case in cases)
    content += '\n  ]\n}\n'
    args.output.write_text(content, encoding='utf-8')
    print(f'Executed original x86 movement arithmetic for {len(cases)} vectors.')


if __name__ == '__main__':
    main()
