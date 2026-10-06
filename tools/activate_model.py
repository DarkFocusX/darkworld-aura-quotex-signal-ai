"""Activate a trained model artifact — `backend/models/artifacts/current`.

`backend/research/train.py` normally points `artifacts/current` at the freshly trained
version with a symlink. Some environments (portable workspaces, snapshots, Windows
without developer mode, container images) do not carry symlinks reliably, and the engine
then reports `MODELS NOT TRAINED` even though the version directory is present.

This tool re-creates the active artifact directory from a named version directory,
validating the provenance first so an operator can never silently activate a DEMO
artifact as the market model:

    python3 tools/activate_model.py                 # list the versions found
    python3 tools/activate_model.py v1-market       # activate it
    python3 tools/activate_model.py v1-market --link   # use a symlink instead of a copy

Safety rules:
    * the version must exist and contain models.pkl + metadata.json
    * `current` is only replaced after the source validated
    * activating an artifact whose dataset provenance is simulated prints a warning
      (the engine will label it READY_DEMO — that is not a live market model)
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

ARTIFACTS = PROJECT_ROOT / "backend" / "models" / "artifacts"
REQUIRED = ("models.pkl", "metadata.json")


def versions() -> list[Path]:
    if not ARTIFACTS.exists():
        return []
    return sorted((p for p in ARTIFACTS.iterdir() if p.is_dir() and p.name != "current"),
                  key=lambda p: p.stat().st_mtime)


def describe(path: Path) -> dict:
    meta_path = path / "metadata.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {"ERROR": "metadata.json is not valid JSON"}
    provenance = meta.get("data_provenance") or {}
    simulated = bool(provenance.get("is_simulated")) or str(meta.get("data_source_class", "")).upper() == "SIMULATED"
    try:
        from backend.models import integrity

        integrity_status = integrity.verify(path)["status"]
    except Exception:  # noqa: BLE001 — listing must never fail on a missing manifest
        integrity_status = "UNKNOWN"
    return {
        "integrity": integrity_status,
        "version": meta.get("version") or path.name,
        "trained_at_utc": meta.get("trained_at_utc") or meta.get("training_timestamp"),
        "data_source": meta.get("data_source"),
        "is_simulated": simulated,
        "state": "READY_DEMO" if simulated else "READY_MARKET",
        "files": sorted(p.name for p in path.iterdir() if p.is_file()),
    }


def activate(name: str, *, link: bool = False) -> int:
    source = ARTIFACTS / name
    if not source.is_dir():
        print(f"✗ no such model version: {source}")
        return 2
    missing = [f for f in REQUIRED if not (source / f).exists()]
    if missing:
        print(f"✗ {name} is incomplete — missing {missing}; it will not be activated")
        return 2

    info = describe(source)
    target = ARTIFACTS / "current"
    if target.is_symlink() or target.exists():
        if target.is_symlink() or target.is_file():
            target.unlink()
        else:
            shutil.rmtree(target)

    if link:
        try:
            target.symlink_to(source.name, target_is_directory=True)
        except OSError as exc:
            print(f"  symlink not supported here ({exc}); falling back to a copy")
            link = False
    if not link:
        target.mkdir(parents=True)
        for item in source.iterdir():
            if item.is_file():
                shutil.copy2(item, target / item.name)

    # Hash manifest: written next to the active copy so the loader can refuse a file that
    # no longer matches what was activated (spec §17 — artifact integrity).
    from backend.models import integrity

    manifest = integrity.write_manifest(target, note=f"activated from {source.name} by tools/activate_model.py")
    print(f"   manifest: {manifest['count']} file(s) hashed")

    print(f"✅ active model: {info['version']} ({info['state']}) → {target}")
    if info["is_simulated"]:
        print("⚠️  this artifact was trained on SIMULATED data — it can never signal in "
              "production (READY_DEMO)")
    print("   restart the API or POST /api/model/reload to load it")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?", help="version directory to activate")
    parser.add_argument("--link", action="store_true", help="symlink instead of copying")
    args = parser.parse_args()

    found = versions()
    if not found:
        print(f"✗ no model versions found in {ARTIFACTS}")
        return 2
    if not args.version:
        print(f"model versions in {ARTIFACTS}:")
        active = ARTIFACTS / "current"
        active_name = None
        if active.exists():
            try:
                active_name = active.resolve().name if active.is_symlink() else active.name
            except OSError:
                active_name = None
        for path in found:
            info = describe(path)
            marker = " ← active" if path.name == active_name else ""
            print(f"  {path.name:14s} {info['state']:12s} integrity {info['integrity']:8s} "
                  f"trained {info['trained_at_utc']} source {info['data_source']}{marker}")
        print("\nusage: python3 tools/activate_model.py <version>")
        return 0
    return activate(args.version, link=args.link)


if __name__ == "__main__":
    raise SystemExit(main())
