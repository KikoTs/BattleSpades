"""Generate the curated community arenas, compatibility layouts and launch presets."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modes.unofficial.maps import write_pack


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("maps/unofficial"))
    parser.add_argument("--configs", type=Path, default=Path("configs/unofficial"))
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    paths = write_pack(args.output, args.configs, overwrite=args.overwrite)
    print(f"Generated {len(paths)} files. Start with: py -3.12 run_server.py --config {args.configs / 'block_hunt.toml'}")


if __name__ == "__main__":
    main()
