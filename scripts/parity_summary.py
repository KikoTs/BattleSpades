"""parity_summary.py - quick summary of the latest physics-parity capture.

Reads logs/physics_parity_server_<session>.ndjson (produced when the
in-game tracer is active and the server has debug_parity=true), and prints
a digest of the client/server divergence:
  - max position delta
  - max velocity delta
  - histogram of state-flag mismatches
  - the first N samples where divergence exceeded a threshold

Usage:
    py scripts/parity_summary.py             # latest capture
    py scripts/parity_summary.py --path logs/physics_parity_server_xxx.ndjson
    py scripts/parity_summary.py --first 5   # show first 5 divergent frames
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT / 'logs'


def latest_capture() -> Path | None:
    paths = sorted(LOG_DIR.glob('physics_parity_server_*.ndjson'))
    return paths[-1] if paths else None


def load_records(path: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with path.open('r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def vec_distance(a: dict | None, b: dict | None) -> float | None:
    if not isinstance(a, dict) or not isinstance(b, dict):
        return None
    try:
        return math.sqrt(sum((float(a[k]) - float(b[k])) ** 2 for k in ('x', 'y', 'z')))
    except Exception:
        return None


def summarize(samples: list[dict]) -> dict:
    deltas = []
    flag_mismatches: dict[str, int] = {}
    for s in samples:
        if s.get('kind') != 'sample':
            continue
        client = (s.get('client_payload') or {}).get('snapshot', {}).get('player', {})
        server = s.get('server_snapshot') or {}
        if not client or not server:
            continue
        pd = vec_distance(client.get('position'), server.get('position'))
        vd = vec_distance(client.get('velocity'), server.get('velocity'))
        if pd is None and vd is None:
            continue
        deltas.append({
            'sample_id': s.get('sample_id'),
            'pd': pd,
            'vd': vd,
            'client_pos': client.get('position'),
            'server_pos': server.get('position'),
            'client_vel': client.get('velocity'),
            'server_vel': server.get('velocity'),
            'states_mismatch': diff_states(client.get('states', {}),
                                            server.get('states', {})),
        })
        for k in deltas[-1]['states_mismatch']:
            flag_mismatches[k] = flag_mismatches.get(k, 0) + 1
    return {'deltas': deltas, 'flag_mismatches': flag_mismatches}


def diff_states(client: dict, server: dict) -> list[str]:
    out = []
    keys = set((client or {}).keys()) | set((server or {}).keys())
    for k in sorted(keys):
        if (client or {}).get(k) != (server or {}).get(k):
            out.append(k)
    return out


def render(summary: dict, capture_path: Path, first: int) -> str:
    deltas = summary['deltas']
    if not deltas:
        return f'no client/server sample pairs in {capture_path}\n'
    pds = [d['pd'] for d in deltas if d['pd'] is not None]
    vds = [d['vd'] for d in deltas if d['vd'] is not None]
    lines = [
        f'capture: {capture_path}',
        f'pairs:   {len(deltas)}',
        '',
    ]
    if pds:
        lines.append('position delta (client - server):')
        lines.append(f'  max  = {max(pds):.4f}')
        lines.append(f'  mean = {sum(pds)/len(pds):.4f}')
        lines.append(f'  >0.5 = {sum(1 for d in pds if d > 0.5)}/{len(pds)}')
        lines.append(f'  >2.0 = {sum(1 for d in pds if d > 2.0)}/{len(pds)}')
    if vds:
        lines.append('')
        lines.append('velocity delta (client - server):')
        lines.append(f'  max  = {max(vds):.4f}')
        lines.append(f'  mean = {sum(vds)/len(vds):.4f}')
    if summary['flag_mismatches']:
        lines.append('')
        lines.append('state-flag mismatches (count of samples where flag differs):')
        for k, n in sorted(summary['flag_mismatches'].items(),
                           key=lambda kv: -kv[1]):
            lines.append(f'  {k:<14} {n}')

    # Show first N divergent frames
    bad = [d for d in deltas if (d['pd'] or 0) > 0.5 or (d['vd'] or 0) > 0.5
           or d['states_mismatch']]
    if bad:
        lines.append('')
        lines.append(f'first {min(first, len(bad))} divergent samples:')
        for d in bad[:first]:
            lines.append(f"  sample={d['sample_id']:>5} pd={d['pd']!s:>10} vd={d['vd']!s:>10}"
                         f"  states_diff={','.join(d['states_mismatch']) or '-'}")
            lines.append(f"    client.pos={d['client_pos']}  server.pos={d['server_pos']}")
            lines.append(f"    client.vel={d['client_vel']}  server.vel={d['server_vel']}")
    return '\n'.join(lines) + '\n'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--path', type=Path, default=None,
                    help='specific capture file; default = latest in logs/')
    ap.add_argument('--first', type=int, default=5,
                    help='show first N divergent samples (default 5)')
    args = ap.parse_args()

    path = args.path or latest_capture()
    if path is None or not path.exists():
        print(f'no capture found at {path or LOG_DIR}/physics_parity_server_*.ndjson')
        return 1

    records = load_records(path)
    summary = summarize(records)
    print(render(summary, path, args.first))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
