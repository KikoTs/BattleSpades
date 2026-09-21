"""Execute original voxel mover; hook only map storage and CRT floor.

Collision branches, corner float stores and voxel conversion execute in the
untouched world.pyd. Sparse voxel membership is the same external input to both
implementations; no gameplay capture or rewritten mover generates expectations.
"""
import argparse
import hashlib
import itertools
import json
import math
import random
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
from unicorn import Uc, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE
from unicorn.x86_const import UC_X86_REG_ESP, UC_X86_REG_EIP, UC_X86_REG_EAX, UC_X86_REG_FPCW
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
PLAYER, STACK, FLOOR, END = 0x20000000, 0x20008000, 0x20010040, 0x2001ffff
uc.mem_write(FLOOR, bytes.fromhex('DD442404 C3'))
for dll in pe.DIRECTORY_ENTRY_IMPORT:
    for entry in dll.imports:
        if entry.name == b'floor':
            uc.mem_write(entry.address, struct.pack('<I', FLOOR))
uc.reg_write(UC_X86_REG_FPCW, 0x37f)
baseline = uc.context_save()
terrain = set()


def hook(_uc, address, size, data):
    stack = uc.reg_read(UC_X86_REG_ESP)
    if address == FLOOR:
        value = struct.unpack('<d', uc.mem_read(stack + 4, 8))[0]
        uc.mem_write(stack + 4, struct.pack('<d', float(math.floor(value))))
    elif address == 0x10001210:
        ret, x, y, z = struct.unpack('<I3i', uc.mem_read(stack, 16))
        uc.reg_write(UC_X86_REG_EAX, int((x, y, z) in terrain))
        uc.reg_write(UC_X86_REG_ESP, stack + 4)
        uc.reg_write(UC_X86_REG_EIP, ret)


uc.hook_add(UC_HOOK_CODE, hook, begin=FLOOR, end=FLOOR)
uc.hook_add(UC_HOOK_CODE, hook, begin=0x10001210, end=0x10001210)


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


def original(case):
    uc.context_restore(baseline)
    data = bytearray(256)
    for offset, values in [(0, case['position']), (12, case['position']), (24, case['velocity'])]:
        struct.pack_into('<3f', data, offset, *values)
    for flag, offset in [('crouch',108),('hover',124),('sprint',116),('airborne',152),('wade',156),('can_uphill',228)]:
        struct.pack_into('<I', data, offset, int(case[flag]))
    uc.mem_write(PLAYER, bytes(data))
    uc.mem_write(0x10022D40, struct.pack('<f', case['dt']))
    uc.mem_write(STACK, struct.pack('<5I', END, PLAYER, 0, 0, 0))
    uc.reg_write(UC_X86_REG_ESP, STACK)
    uc.emu_start(0x10001710, END, count=50000)
    stopped_at = uc.reg_read(UC_X86_REG_EIP)
    if stopped_at != END:
        raise RuntimeError(f'Original voxel execution stopped at {stopped_at:#x}, expected {END:#x}')
    data = uc.mem_read(PLAYER, 256)
    return dict(position=list(struct.unpack_from('<3f',data,12)),
                velocity=list(struct.unpack_from('<3f',data,24)),
                crouch=bool(struct.unpack_from('<I',data,108)[0]),
                airborne=bool(struct.unpack_from('<I',data,152)[0]),
                wade=bool(struct.unpack_from('<I',data,156)[0]),
                climbed=struct.unpack_from('<f',data,148)[0] > 0)


def main():
    global terrain
    rng=random.Random(168)
    flat={(x,y,62) for x in range(97,105) for y in range(97,105)}
    terrains={
        'empty':set(),
        'floor':flat,
        'step':flat|{(x,y,61) for x in range(101,105) for y in range(97,105)},
        'wall':flat|{(101,y,z) for y in range(97,105) for z in range(58,62)},
        'corner':flat|{(x,y,z) for x in range(101,105) for y in range(101,105) for z in (60,61)},
        'tunnel':flat|{(x,y,59) for x in range(101,105) for y in range(97,105)},
    }
    cases=[]
    for name in terrains:
        for x,z,v,control in itertools.product(
                [100.5, f32(101-f32(.45)), 100.55001], [59.7,59.75,60.6,60.65],
                [[.3,0,-.3377],[.3,.3,.1],[0,0,.5],[0,0,-.4]],
                [(False,False,False,True),(True,False,False,True),(True,True,False,True),(False,False,True,False)]):
            cases.append(dict(terrain=name,position=list(map(f32,[x,100.5,z])),velocity=list(map(f32,v)),
                              dt=1/60,crouch=control[0],hover=control[1],sprint=control[2],can_uphill=control[3],
                              airborne=True,wade=False))
        for _ in range(200):
            cases.append(dict(terrain=name,position=list(map(f32,[rng.uniform(99,102),rng.uniform(99,102),rng.uniform(57,63)])),
                              velocity=list(map(f32,[rng.uniform(-.5,.5),rng.uniform(-.5,.5),rng.uniform(-.5,.5)])),
                              dt=rng.choice([1/60,1/20]),crouch=rng.choice([False,True]),hover=False,sprint=False,
                              can_uphill=True,airborne=rng.choice([False,True]),wade=False))
    for case in cases:
        terrain=terrains[case['terrain']]
        case['expected']=original(case)
    header = dict(binary_sha256=EXPECTED_HASH, entry='0x10001710',
                  hooks=['CRT floor', 'sparse voxel storage at0x10001210'],
                  terrain={k:[list(v) for v in sorted(values)] for k,values in terrains.items()})
    content = json.dumps(header, indent=2).rstrip()[:-1]
    content += ',\n  "cases": [\n'
    content += ',\n'.join('    ' + json.dumps(case, separators=(',', ':')) for case in cases)
    content += '\n  ]\n}\n'
    args.output.write_text(content, encoding='utf-8')
    print(f'Executed original x86 voxel mover for {len(cases)} vectors.')


if __name__=='__main__':main()
