"""Execute original peer resolver with Python C-API object access stubs."""
import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--binary', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--dependency-dir', type=Path)
args = parser.parse_args()
if args.dependency_dir:
    sys.path.insert(0, str(args.dependency_dir))
import pefile
from unicorn import Uc, UC_ARCH_X86, UC_MODE_32, UC_HOOK_CODE
from unicorn.x86_const import UC_X86_REG_ESP, UC_X86_REG_EIP, UC_X86_REG_EAX, UC_X86_REG_FPCW

EXPECTED_HASH = 'ae45ec007e312c8d650620bc2779169f7b7461c74192b7a7480342c21237c1a0'
actual_hash = hashlib.sha256(args.binary.read_bytes()).hexdigest()
if actual_hash != EXPECTED_HASH:
    raise SystemExit(f'Original binary SHA-256 mismatch: expected {EXPECTED_HASH}, got {actual_hash}')
pe = pefile.PE(str(args.binary))
uc = Uc(UC_ARCH_X86, UC_MODE_32)
image = pe.get_memory_mapped_image()
uc.mem_map(pe.OPTIONAL_HEADER.ImageBase, (len(image) + 4095) & ~4095)
uc.mem_write(pe.OPTIONAL_HEADER.ImageBase, image)
uc.mem_map(0x20000000, 0x20000)
uc.reg_write(UC_X86_REG_FPCW, 0x37f)
ref = SimpleNamespace(pe=pe, baseline=uc.context_save(), PLAYER=0x20000000,
                      STACK=0x20008000, END=0x2001ffff, EXPECTED_HASH=EXPECTED_HASH)


def f32(value):
    return struct.unpack('<f', struct.pack('<f', value))[0]


ref.f32 = f32
uc.mem_write(0x20010020,bytes.fromhex('D9FA C3'))
ADDRESSES={b'PyList_GetItem':0x20010100,b'PyTuple_GetItem':0x20010110,b'PyTuple_Size':0x20010120,b'PyFloat_AsDouble':0x20010130}
for dll in ref.pe.DIRECTORY_ENTRY_IMPORT:
    for entry in dll.imports:
        if entry.name in ADDRESSES:uc.mem_write(entry.address,struct.pack('<I',ADDRESSES[entry.name]))
        elif entry.name==b'_CIsqrt':uc.mem_write(entry.address,struct.pack('<I',0x20010020))
uc.mem_write(0x20010130,bytes.fromhex('DD05')+struct.pack('<I',0x2001e000)+bytes.fromhex('C3'))
peers=[]


def hook(_uc,address,size,data):
    stack=uc.reg_read(UC_X86_REG_ESP)
    ret,arg0,arg1=struct.unpack('<3I',uc.mem_read(stack,12))
    if address==ADDRESSES[b'PyList_GetItem']:result=0x30000000+arg1*32
    elif address==ADDRESSES[b'PyTuple_GetItem']:result=0x40000000+((arg0-0x30000000)//32)*32+arg1
    elif address==ADDRESSES[b'PyTuple_Size']:result=len(peers[(arg0-0x30000000)//32])
    else:
        index,part=divmod(arg0-0x40000000,32)
        uc.mem_write(0x2001e000,struct.pack('<d',peers[index][part]))
        return
    uc.reg_write(UC_X86_REG_EAX,result)
    uc.reg_write(UC_X86_REG_ESP,stack+4)
    uc.reg_write(UC_X86_REG_EIP,ret)


for addr in ADDRESSES.values():uc.hook_add(UC_HOOK_CODE,hook,begin=addr,end=addr)


def original(case):
    global peers
    peers=case['peers']
    uc.context_restore(ref.baseline)
    data=bytearray(256)
    struct.pack_into('<3f',data,0,*case['position'])
    struct.pack_into('<3f',data,24,*case['velocity'])
    for name,offset in [('crouch',108),('hover',124),('alive',164),('exploded',168)]:
        struct.pack_into('<I',data,offset,int(case.get(name,name=='alive')))
    uc.mem_write(ref.PLAYER,bytes(data))
    scale=ref.f32(ref.f32(case['dt'])*32.)
    uc.mem_write(ref.STACK,struct.pack('<4IfI',ref.END,ref.PLAYER,0x50000000,len(peers),scale,int(case['resolve'])))
    uc.reg_write(UC_X86_REG_ESP,ref.STACK)
    uc.emu_start(0x10012710,ref.END,count=100000)
    stopped_at = uc.reg_read(UC_X86_REG_EIP)
    if stopped_at != ref.END:
        raise RuntimeError(f'Original peer execution stopped at {stopped_at:#x}, expected {ref.END:#x}')
    return dict(velocity=list(struct.unpack('<3f',uc.mem_read(ref.PLAYER+24,12))),count=uc.reg_read(UC_X86_REG_EAX))


def main():
    import random
    rng=random.Random(168)
    cases=[]
    for _ in range(2000):
        pos=[100.5,100.5,100.0]
        velocity=list(map(ref.f32,[rng.uniform(-.1,.1),rng.uniform(-.1,.1),rng.uniform(-.1,.1)]))
        peerlist=[list(map(ref.f32,[100.5+rng.uniform(-.6,.6),100.5+rng.uniform(-.6,.6),100.+rng.uniform(-2.7,2.7)])) for i in range(rng.randint(1,4))]
        cases.append(dict(position=pos,velocity=velocity,peers=peerlist,dt=rng.choice([1/60,1/20]),resolve=rng.choice([False,True]),crouch=rng.choice([False,True]),hover=rng.choice([False,True])))
    for case in cases:
        case['expected']=original(case)
    header=dict(binary_sha256=EXPECTED_HASH,entry='0x10012710',
                hooks=['Python list/tuple access', 'Python float conversion', 'CRT sqrt'])
    content = json.dumps(header, indent=2).rstrip()[:-1]
    content += ',\n  "cases": [\n'
    content += ',\n'.join('    ' + json.dumps(case, separators=(',', ':')) for case in cases)
    content += '\n  ]\n}\n'
    args.output.write_text(content, encoding='utf-8')
    print(f'Executed original x86 peer resolver for {len(cases)} vectors.')


if __name__=='__main__':main()
