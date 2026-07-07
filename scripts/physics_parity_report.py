from __future__ import annotations

import argparse
import ast
import json
import operator
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
# Canonical client tree is the sibling repo at G:\AoSRevival\aceofspades_nonsteam.
# (The old BattleSpades-local 'aceofspades/' tree was archived to
# G:\AoSRevival\archive\BattleSpades-aceofspades on 2026-04-25.)
CLIENT_ROOT = ROOT.parent / 'aceofspades_nonsteam'
DEFAULT_CAPTURE_DIR = CLIENT_ROOT / 'logs'
DEFAULT_SERVER_CAPTURE_DIR = ROOT / 'logs'
DEFAULT_CURRENT_WORLD = ROOT / 'aoslib' / 'world.pyx'
DEFAULT_BASELINE_WORLD = ROOT / 'aoslib' / '.deleted' / 'world_reversed.pyx'
DEFAULT_CLIENT_CONSTANTS = CLIENT_ROOT / 'shared' / 'constants.py'

WORLD_CONSTANT_NAMES = (
    '_PLAYER_RADIUS',
    '_PLAYER_HEIGHT',
    '_PLAYER_CROUCH_HEIGHT',
    '_PLAYER_CROUCH_SHIFT',
    '_GLOBAL_GRAVITY',
    '_FALL_SLOW_DOWN',
    '_FALL_DAMAGE_VELOCITY',
    '_PHYSICS_SCALE',
)

CLIENT_CONSTANT_NAMES = (
    'MAP_Z',
    'PLAYER_RADIUS',
    'PLAYER_STANDING_POS_ABOVE_GROUND',
    'PLAYER_CROUCHING_POS_ABOVE_GROUND',
    'PLAYER_STANDING_HEIGHT',
    'PLAYER_CROUCHING_HEIGHT',
    'Z_ABOVE_WATERPLANE',
)

_SAFE_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_SAFE_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


@dataclass
class WorldPhysicsProfile:
    path: Path
    constants: dict[str, Any]
    jump_impulse_expr: str | None
    jump_requires_grounded: bool
    climb_requires_shallow_pitch: bool
    horizontal_friction_lines: list[str]
    water_friction_lines: list[str]


def _extract_named_constants(text: str, names: tuple[str, ...]) -> dict[str, Any]:
    extracted: dict[str, Any] = {}
    for name in names:
        pattern = re.compile(rf'^{re.escape(name)}\s*=\s*([^\n#]+)', re.MULTILINE)
        match = pattern.search(text)
        if not match:
            continue
        raw_value = match.group(1).strip()
        try:
            extracted[name] = ast.literal_eval(raw_value)
        except Exception:
            extracted[name] = raw_value
    return extracted


def _extract_first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.MULTILINE)
    if not match:
        return None
    return match.group(1).strip()


def _safe_eval_expr(node: ast.AST, env: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in env:
            raise KeyError(node.id)
        return env[node.id]
    if isinstance(node, ast.BinOp):
        op = _SAFE_BIN_OPS.get(type(node.op))
        if op is None:
            raise ValueError(type(node.op).__name__)
        return op(_safe_eval_expr(node.left, env), _safe_eval_expr(node.right, env))
    if isinstance(node, ast.UnaryOp):
        op = _SAFE_UNARY_OPS.get(type(node.op))
        if op is None:
            raise ValueError(type(node.op).__name__)
        return op(_safe_eval_expr(node.operand, env))
    raise ValueError(type(node).__name__)


def _extract_assignments_in_order(text: str) -> list[tuple[str, str]]:
    assignments: list[tuple[str, str]] = []
    pattern = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$')
    for line in text.splitlines():
        if not line or line[0].isspace():
            continue
        raw_line = line.split('#', 1)[0].rstrip()
        if not raw_line:
            continue
        match = pattern.match(raw_line)
        if not match:
            continue
        assignments.append((match.group(1), match.group(2).strip()))
    return assignments


def _extract_named_constants_with_eval(text: str, names: tuple[str, ...]) -> dict[str, Any]:
    requested = set(names)
    extracted: dict[str, Any] = {}
    env: dict[str, Any] = {}
    for name, raw_expr in _extract_assignments_in_order(text):
        try:
            parsed = ast.parse(raw_expr, mode='eval')
            value = _safe_eval_expr(parsed.body, env)
        except Exception:
            try:
                value = ast.literal_eval(raw_expr)
            except Exception:
                value = raw_expr
        env[name] = value
        if name in requested:
            extracted[name] = value
    return extracted


def _extract_lines_containing(text: str, fragments: tuple[str, ...]) -> list[str]:
    results: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and any(fragment in stripped for fragment in fragments):
            results.append(stripped)
    return results


def build_world_profile(path: Path) -> WorldPhysicsProfile:
    text = path.read_text(encoding='utf-8')
    return WorldPhysicsProfile(
        path=path,
        constants=_extract_named_constants_with_eval(text, WORLD_CONSTANT_NAMES),
        jump_impulse_expr=_extract_first_match(text, r'_velocity\.z\s*=\s*([^\n#]+)'),
        jump_requires_grounded='if self._jump and grounded:' in text,
        climb_requires_shallow_pitch='self._orientation.z < 0.5' in text,
        horizontal_friction_lines=_extract_lines_containing(text, ('horizontal_divisor', 'friction =')),
        water_friction_lines=_extract_lines_containing(text, ('self._wade', 'self._water_friction')),
    )


def build_client_constant_profile(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding='utf-8')
    constants = _extract_named_constants_with_eval(text, CLIENT_CONSTANT_NAMES)
    constants.pop('MAP_Z', None)
    return constants


def summarize_capture(path: Path) -> dict[str, Any]:
    records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not records:
        return {'path': str(path), 'sample_count': 0}

    def pick(path_parts: tuple[str, ...]) -> list[float]:
        values: list[float] = []
        for record in records:
            value: Any = record
            for part in path_parts:
                if not isinstance(value, dict):
                    value = None
                    break
                value = value.get(part)
            if isinstance(value, (int, float)):
                values.append(float(value))
        return values

    timestamps = pick(('timestamp',))
    horiz_speeds = pick(('player', 'horizontal_speed'))
    vertical_speeds = pick(('player', 'vertical_speed'))
    surface_z = pick(('derived', 'surface_z'))
    landing_speeds = pick(('derived', 'last_landing_speed'))
    feet_delta = pick(('derived', 'delta_feet_to_surface'))

    summary: dict[str, Any] = {'path': str(path), 'sample_count': len(records)}
    if timestamps:
        summary['duration_seconds'] = round(max(timestamps) - min(timestamps), 3)
    if horiz_speeds:
        summary['max_horizontal_speed'] = round(max(horiz_speeds), 4)
    if vertical_speeds:
        summary['min_vertical_speed'] = round(min(vertical_speeds), 4)
        summary['max_vertical_speed'] = round(max(vertical_speeds), 4)
    if surface_z:
        summary['surface_z_range'] = [round(min(surface_z), 4), round(max(surface_z), 4)]
    if landing_speeds:
        summary['max_landing_speed'] = round(max(landing_speeds), 4)
    if feet_delta:
        summary['feet_delta_range'] = [round(min(feet_delta), 4), round(max(feet_delta), 4)]
    return summary


def summarize_parity_capture(path: Path) -> dict[str, Any]:
    records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    session_ids = sorted({record.get('session_id') for record in records if record.get('session_id')})
    sample_records = [record for record in records if record.get('kind') == 'sample']
    event_records = [record for record in records if record.get('kind') == 'event']
    override_records = [record for record in records if str(record.get('kind', '')).startswith('override_')]
    flag_counter: Counter[str] = Counter()
    category_counter: Counter[str] = Counter()
    for record in sample_records:
        diff = record.get('diff', {})
        for flag in diff.get('flags', []):
            flag_counter[str(flag)] += 1
        for category, enabled in (diff.get('categories', {}) or {}).items():
            if enabled:
                category_counter[str(category)] += 1
    return {
        'path': str(path),
        'session_ids': session_ids,
        'sample_count': len(sample_records),
        'event_count': len(event_records),
        'override_event_count': len(override_records),
        'top_flags': flag_counter.most_common(5),
        'top_categories': category_counter.most_common(5),
    }


def _load_ndjson(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def compare_parity_captures(client_path: Path, server_path: Path) -> dict[str, Any]:
    client_records = _load_ndjson(client_path)
    server_records = _load_ndjson(server_path)
    client_samples = {record.get('sample_id'): record for record in client_records if record.get('kind') == 'sample'}
    server_samples = {record.get('sample_id'): record for record in server_records if record.get('kind') == 'sample'}
    matched_ids = sorted(set(client_samples) & set(server_samples))
    flag_counter: Counter[str] = Counter()
    category_counter: Counter[str] = Counter()
    max_position_distance = 0.0
    for sample_id in matched_ids:
        diff = server_samples[sample_id].get('diff', {})
        for flag in diff.get('flags', []):
            flag_counter[str(flag)] += 1
        for category, enabled in (diff.get('categories', {}) or {}).items():
            if enabled:
                category_counter[str(category)] += 1
        position_distance = diff.get('position_distance')
        if isinstance(position_distance, (int, float)):
            max_position_distance = max(max_position_distance, float(position_distance))
    session_ids = sorted({record.get('session_id') for record in client_records + server_records if record.get('session_id')})
    return {
        'client_path': str(client_path),
        'server_path': str(server_path),
        'session_ids': session_ids,
        'matched_sample_count': len(matched_ids),
        'mismatch_flag_counts': dict(flag_counter),
        'mismatch_category_counts': dict(category_counter),
        'max_position_distance': round(max_position_distance, 4),
    }


def latest_capture_path(capture_dir: Path, prefix: str = 'physics_debug_') -> Path | None:
    if not capture_dir.exists():
        return None
    captures = sorted(capture_dir.glob('%s*.ndjson' % prefix))
    if not captures:
        return None
    return captures[-1]


def build_report(current_world: Path = DEFAULT_CURRENT_WORLD, baseline_world: Path = DEFAULT_BASELINE_WORLD, client_constants: Path = DEFAULT_CLIENT_CONSTANTS, capture_path: Path | None = None, client_parity_path: Path | None = None, server_parity_path: Path | None = None) -> dict[str, Any]:
    current_profile = build_world_profile(current_world)
    baseline_profile = build_world_profile(baseline_world)
    client_profile = build_client_constant_profile(client_constants)

    report: dict[str, Any] = {
        'client_constants': client_profile,
        'current_world': {
            'path': str(current_profile.path),
            'constants': current_profile.constants,
            'jump_impulse_expr': current_profile.jump_impulse_expr,
            'jump_requires_grounded': current_profile.jump_requires_grounded,
            'climb_requires_shallow_pitch': current_profile.climb_requires_shallow_pitch,
            'horizontal_friction_lines': current_profile.horizontal_friction_lines,
            'water_friction_lines': current_profile.water_friction_lines,
        },
        'baseline_world': {
            'path': str(baseline_profile.path),
            'constants': baseline_profile.constants,
            'jump_impulse_expr': baseline_profile.jump_impulse_expr,
            'jump_requires_grounded': baseline_profile.jump_requires_grounded,
            'climb_requires_shallow_pitch': baseline_profile.climb_requires_shallow_pitch,
            'horizontal_friction_lines': baseline_profile.horizontal_friction_lines,
            'water_friction_lines': baseline_profile.water_friction_lines,
        },
        'mismatches': {
            'standing_height_current_vs_client': [current_profile.constants.get('_PLAYER_HEIGHT'), client_profile.get('PLAYER_STANDING_HEIGHT')],
            'standing_height_baseline_vs_client': [baseline_profile.constants.get('_PLAYER_HEIGHT'), client_profile.get('PLAYER_STANDING_HEIGHT')],
            'crouch_height_current_vs_client': [current_profile.constants.get('_PLAYER_CROUCH_HEIGHT'), client_profile.get('PLAYER_CROUCHING_HEIGHT')],
            'jump_impulse_current_vs_baseline': [current_profile.jump_impulse_expr, baseline_profile.jump_impulse_expr],
            'jump_requires_grounded_current_vs_baseline': [current_profile.jump_requires_grounded, baseline_profile.jump_requires_grounded],
            'climb_gate_current_vs_baseline': [current_profile.climb_requires_shallow_pitch, baseline_profile.climb_requires_shallow_pitch],
        },
    }
    if capture_path is not None:
        report['capture_summary'] = summarize_capture(capture_path)
    if client_parity_path is not None:
        report['client_parity_summary'] = summarize_parity_capture(client_parity_path)
    if server_parity_path is not None:
        report['server_parity_summary'] = summarize_parity_capture(server_parity_path)
    if client_parity_path is not None and server_parity_path is not None:
        report['live_parity_comparison'] = compare_parity_captures(client_parity_path, server_parity_path)
    return report


def render_text_report(report: dict[str, Any]) -> str:
    current = report['current_world']
    baseline = report['baseline_world']
    client = report['client_constants']
    mismatches = report['mismatches']

    lines = [
        'Client Physics Parity Report',
        '',
        'Client constants:',
        '  radius={PLAYER_RADIUS} stand_ag={PLAYER_STANDING_POS_ABOVE_GROUND} crouch_ag={PLAYER_CROUCHING_POS_ABOVE_GROUND}'.format(**client),
        '  stand_h={PLAYER_STANDING_HEIGHT} crouch_h={PLAYER_CROUCHING_HEIGHT} water={Z_ABOVE_WATERPLANE}'.format(**client),
        '',
        'Current world:',
        '  path=%s' % current['path'],
        '  height=%s crouch_height=%s crouch_shift=%s' % (current['constants'].get('_PLAYER_HEIGHT'), current['constants'].get('_PLAYER_CROUCH_HEIGHT'), current['constants'].get('_PLAYER_CROUCH_SHIFT')),
        '  jump_impulse=%s requires_grounded=%s' % (current['jump_impulse_expr'], current['jump_requires_grounded']),
        '  climb_requires_shallow_pitch=%s' % current['climb_requires_shallow_pitch'],
        '',
        'Baseline world:',
        '  path=%s' % baseline['path'],
        '  height=%s crouch_height=%s crouch_shift=%s' % (baseline['constants'].get('_PLAYER_HEIGHT'), baseline['constants'].get('_PLAYER_CROUCH_HEIGHT'), baseline['constants'].get('_PLAYER_CROUCH_SHIFT')),
        '  jump_impulse=%s requires_grounded=%s' % (baseline['jump_impulse_expr'], baseline['jump_requires_grounded']),
        '  climb_requires_shallow_pitch=%s' % baseline['climb_requires_shallow_pitch'],
        '',
        'Highlighted mismatches:',
        '  standing_height current/client = %s / %s' % tuple(mismatches['standing_height_current_vs_client']),
        '  standing_height baseline/client = %s / %s' % tuple(mismatches['standing_height_baseline_vs_client']),
        '  crouch_height current/client = %s / %s' % tuple(mismatches['crouch_height_current_vs_client']),
        '  jump_impulse current/baseline = %s / %s' % tuple(mismatches['jump_impulse_current_vs_baseline']),
        '  jump_requires_grounded current/baseline = %s / %s' % tuple(mismatches['jump_requires_grounded_current_vs_baseline']),
        '  climb_gate current/baseline = %s / %s' % tuple(mismatches['climb_gate_current_vs_baseline']),
    ]
    if report.get('capture_summary'):
        capture_summary = report['capture_summary']
        lines.extend(['', 'Capture summary:', '  path=%s' % capture_summary.get('path'), '  sample_count=%s duration=%s' % (capture_summary.get('sample_count'), capture_summary.get('duration_seconds')), '  max_horizontal_speed=%s landing_speed=%s' % (capture_summary.get('max_horizontal_speed'), capture_summary.get('max_landing_speed')), '  feet_delta_range=%s' % capture_summary.get('feet_delta_range')])
    if report.get('live_parity_comparison'):
        live = report['live_parity_comparison']
        lines.extend(['', 'Live parity comparison:', '  sessions=%s' % live.get('session_ids'), '  matched_samples=%s' % live.get('matched_sample_count'), '  mismatch_flags=%s' % live.get('mismatch_flag_counts'), '  mismatch_categories=%s' % live.get('mismatch_category_counts'), '  max_position_distance=%s' % live.get('max_position_distance')])
    return '\n'.join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Compare client physics captures against current and baseline server world logic.')
    parser.add_argument('--current-world', type=Path, default=DEFAULT_CURRENT_WORLD)
    parser.add_argument('--baseline-world', type=Path, default=DEFAULT_BASELINE_WORLD)
    parser.add_argument('--client-constants', type=Path, default=DEFAULT_CLIENT_CONSTANTS)
    parser.add_argument('--capture', type=Path, default=None)
    parser.add_argument('--latest-capture', action='store_true')
    parser.add_argument('--client-parity', type=Path, default=None)
    parser.add_argument('--server-parity', type=Path, default=None)
    parser.add_argument('--latest-parity', action='store_true')
    parser.add_argument('--json', action='store_true', dest='json_output')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    capture_path = args.capture
    if args.latest_capture and capture_path is None:
        capture_path = latest_capture_path(DEFAULT_CAPTURE_DIR, 'physics_debug_')
    client_parity_path = args.client_parity
    server_parity_path = args.server_parity
    if args.latest_parity:
        client_parity_path = client_parity_path or latest_capture_path(DEFAULT_CAPTURE_DIR, 'physics_parity_client_')
        server_parity_path = server_parity_path or latest_capture_path(DEFAULT_SERVER_CAPTURE_DIR, 'physics_parity_server_')
    report = build_report(current_world=args.current_world, baseline_world=args.baseline_world, client_constants=args.client_constants, capture_path=capture_path, client_parity_path=client_parity_path, server_parity_path=server_parity_path)
    if args.json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_text_report(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
