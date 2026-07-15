"""Verify the complete native archive matrix and write SHA256SUMS.txt."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.runtime_paths import read_version


_TARGETS = (
    ("windows", "x86_64"),
    ("windows", "arm64"),
    ("linux", "x86_64"),
    ("linux", "arm64"),
    ("macos", "x86_64"),
    ("macos", "arm64"),
)


def expected_archive_names(version: str) -> tuple[str, ...]:
    """Return the sorted, exact six-archive publication contract."""

    return tuple(
        sorted(
            f"BattleSpades-{version}-{platform}-{architecture}.zip"
            for platform, architecture in _TARGETS
        )
    )


def write_checksum_manifest(asset_dir: Path, version: str) -> Path:
    """Require all six native zips and write their lowercase SHA-256 values."""

    directory = Path(asset_dir).resolve()
    expected = expected_archive_names(version)
    actual = tuple(sorted(path.name for path in directory.glob("*.zip")))
    if actual != expected:
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        raise RuntimeError(
            "expected exactly 6 release archives; "
            f"missing={missing!r} unexpected={unexpected!r}"
        )

    lines = []
    for name in expected:
        archive = directory / name
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    manifest = directory / "SHA256SUMS.txt"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    """Validate downloaded matrix artifacts before GitHub publication."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-dir", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    arguments = parser.parse_args(argv)
    manifest = write_checksum_manifest(
        arguments.asset_dir,
        read_version(arguments.project_root),
    )
    print(manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
