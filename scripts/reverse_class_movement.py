"""Resolve only literal/name tables from the official Python constant source."""
import ast
import argparse
import hashlib
import json
import re
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True,
                    help='Original shared/constants.py with adjacent .pyc')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
source = args.source
text = source.read_text()
cache = {}
class_enum_source = re.search(r'^(CLASS_SOLDIER, .+) = xrange\(19\)', text, re.M)
if class_enum_source is None:
    raise ValueError('Source is missing the expected 19-name CLASS_SOLDIER enum')
class_names = class_enum_source[1]
cache.update((name.strip(), index) for index,name in enumerate(class_names.split(',')))


def resolve_name(name):
    if name in cache:
        return cache[name]
    match = re.search(r'^' + re.escape(name) + r' = (.+)$', text, re.M)
    if match is None:
        raise ValueError(f'Source is missing constant {name}')
    expression = match[1]
    if expression == '{':
        end = text.index('\n}', match.start()) + 2
        expression = text[match.start():end].split('=', 1)[1].strip()
    result = resolve(ast.parse(expression, mode='eval').body)
    cache[name] = result
    return result


def resolve(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return resolve_name(node.id)
    if isinstance(node, ast.Dict):
        return {resolve(k): resolve(v) for k,v in zip(node.keys,node.values)}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -resolve(node.operand)
    raise ValueError(ast.dump(node))


fields = {
    'accel_multiplier':'CLASS_ACCEL_MULTIPLIER',
    'sprint_multiplier':'CLASS_SPRINT_MULTIPLIER',
    'jump_multiplier':'CLASS_JUMP_MULTIPLIER',
    'crouch_sneak_multiplier':'CLASS_CROUCH_SNEAK_MULTIPLIER',
    'can_sprint_uphill':'CLASS_CAN_SPRINT_UPHILL',
    'water_friction':'CLASS_WATER_FRICTION',
    'fall_on_water_damage_multiplier':'CLASS_FALL_ON_WATER_DAMAGE_MULTIPLIER',
    'falling_damage_min_distance':'CLASS_FALLING_DAMAGE_MIN_DISTANCE',
    'falling_damage_max_distance':'CLASS_FALLING_DAMAGE_MAX_DISTANCE',
    'falling_damage_max_damage':'CLASS_FALLING_DAMAGE_MAX_DAMAGE',
}
tables = {field:resolve_name(name) for field,name in fields.items()}
# Check every recovered value against Python 2 bytecode without importing it.
from xdis.load import load_module
pyc = source.with_suffix('.pyc')
version, _, _, code, *_ = load_module(str(pyc))
if version[:2] != (2, 7):
    raise ValueError(f'Expected Python 2.7 bytecode, got {version}')
instructions = []
offset = 0
while offset < len(code.co_code):
    op = code.co_code[offset]
    offset += 1
    arg = None
    if op >= 90:
        if offset + 1 >= len(code.co_code):
            raise ValueError(f'Truncated bytecode argument at offset {offset - 1}')
        arg = code.co_code[offset] + 256 * code.co_code[offset + 1]
        offset += 2
    instructions.append((op, arg))
namespace = {name:index for name,index in cache.items() if name.startswith('CLASS_') and isinstance(index,int)}
namespace.update({'True':True, 'False':False, 'None':None})
class_enum = next((index for index, item in enumerate(instructions[:-1])
                  if item == (92, 19) and instructions[index+1][0] == 90
                  and code.co_names[instructions[index+1][1]] == 'CLASS_SOLDIER'), None)
if class_enum is None:
    raise ValueError('Bytecode is missing the expected CLASS_SOLDIER enum unpack')
enum_stores = instructions[class_enum+1:class_enum+20]
if (len(enum_stores) != 19 or any(op != 90 for op, arg in enum_stores)
        or [code.co_names[arg] for op, arg in enum_stores]
        != [name.strip() for name in class_names.split(',')]):
    raise ValueError('Source class enum does not match the bytecode assignments')
required_tables = set(fields.values())
verified_tables = set()
for index, (op,arg) in enumerate(instructions):
    if op != 90 or index == 0:
        continue
    previous, previous_arg = instructions[index-1]
    name = code.co_names[arg]
    if previous == 100:
        namespace[name] = code.co_consts[previous_arg]
    elif previous == 101 and code.co_names[previous_arg] in namespace:
        namespace[name] = namespace[code.co_names[previous_arg]]
    if name not in required_tables:
        continue
    start = index - 1
    while start >= 0 and instructions[start][0] != 105:
        start -= 1
    if start < 0:
        raise ValueError(f'Bytecode table {name} has no preceding BUILD_MAP')
    entries = instructions[start+1:index]
    if len(entries) % 3 != 0:
        raise ValueError(f'Unsupported bytecode table shape for {name}')
    actual = {}
    for p in range(0,len(entries),3):
        value, key, store = entries[p:p+3]
        if not (value[0] in (100,101) and key[0] == 101 and store[0] == 54):
            raise ValueError(f'Unsupported bytecode table entry for {name} at entry {p // 3}')
        actual[namespace[code.co_names[key[1]]]] = (
            code.co_consts[value[1]] if value[0] == 100 else namespace[code.co_names[value[1]]]
        )
    if actual != resolve_name(name):
        raise ValueError(f'Source table {name} does not match the bytecode assignments')
    verified_tables.add(name)
    namespace[name] = actual
missing_tables = required_tables - verified_tables
if missing_tables:
    raise ValueError(f'Bytecode did not verify required tables: {", ".join(sorted(missing_tables))}')
result = {'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
          'bytecode_sha256':hashlib.sha256(pyc.read_bytes()).hexdigest(),
          'bytecode_verified':True,
          'source':'official Python client shared/constants.py; literal tables only',
          'fields':fields, 'tables':tables}
args.output.write_text(json.dumps(result, indent=2), encoding='utf-8')
print(f'Extracted {len(tables)} literal class tables; no original code executed.')
