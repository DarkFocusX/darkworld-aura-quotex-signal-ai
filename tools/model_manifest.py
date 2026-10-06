"""Write / verify the hash manifest of a model artifact.

    python3 tools/model_manifest.py              # report every version + the active artifact
    python3 tools/model_manifest.py --write       # (re)write manifests where missing
    python3 tools/model_manifest.py --write current  v1-market

The manifest records the sha256 of every artifact file. `ModelService.load()` verifies it
before deserialising anything: a file that does not match its recorded hash is refused
(`ARTIFACT INTEGRITY MISMATCH`) instead of being loaded. Re-run this tool after you have
deliberately replaced an artifact.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.models import integrity  # noqa: E402

ARTIFACTS = PROJECT_ROOT / "backend" / "models" / "artifacts"


def directories() -> list[Path]:
    if not ARTIFACTS.exists():
        return []
    return sorted(p for p in ARTIFACTS.iterdir() if p.is_dir() and p.name != "current")


def report(paths: list[Path], *, write: bool) -> int:
    problems = 0
    for path in paths:
        if not path.exists():
            print(f"✗ {path} does not exist")
            problems += 1
            continue
        before = integrity.verify(path)
        if write and before["status"] != "OK":
            payload = integrity.write_manifest(path, note="written by tools/model_manifest.py")
            after = integrity.verify(path)
            print(f"✅ {path.name:14s} manifest written ({payload['count']} files) · "
                  f"{after['status']}")
            continue
        marker = {"OK": "✅", "ABSENT": "⚠️", "MISMATCH": "❌"}.get(before["status"], "?")
        print(f"{marker} {path.name:14s} {before['status']:8s} {before['detail']} "
              f"(hashed files: {before['checked']})")
        if before["status"] == "MISMATCH":
            problems += 1
    if write and problems:
        print("\n⚠️  mismatched artifacts are never re-hashed automatically — restore the "
              "original files, or delete manifest.json and re-run --write if the change was "
              "deliberate.")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="*", help="artifact folders (default: all + current)")
    parser.add_argument("--write", action="store_true", help="write manifests that are missing")
    args = parser.parse_args()

    if args.names:
        targets = [ARTIFACTS / name for name in args.names]
    else:
        targets = directories() + [ARTIFACTS / "current"]
    if not ARTIFACTS.exists():
        print(f"✗ no artifacts directory: {ARTIFACTS}")
        return 2
    return report(targets, write=args.write)


if __name__ == "__main__":
    raise SystemExit(main())
