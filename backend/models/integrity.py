"""Artifact integrity — hashes written at training/activation time, verified on load.

`models.pkl` / `scaler.pkl` / `isolation_forest.pkl` are deserialised with joblib: that is
unavoidable for scikit-learn estimators, so the defence is provenance rather than format —
an artifact is only trusted if its files match the hash manifest recorded when it was
written (`manifest.json`, produced by the training run, `tools/activate_model.py` or
`tools/model_manifest.py`).

Statuses:

* ``OK``       — every hashed file matches.
* ``ABSENT``   — no manifest (legacy artifact). The loader still starts but records the
                 fact, so an operator can see the artifact was not hash-verified.
* ``MISMATCH`` — a file differs from the manifest, or one hashed file is missing.
                 The loader refuses it: a corrupted or substituted artifact must never
                 silently become the source of production predictions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"
HASHED_SUFFIXES = (".pkl", ".json", ".txt", ".npy", ".joblib")
CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _hashable(directory: Path) -> list[Path]:
    return sorted(
        (p for p in directory.iterdir()
         if p.is_file() and p.name != MANIFEST_NAME and p.suffix.lower() in HASHED_SUFFIXES),
        key=lambda p: p.name,
    )


def manifest_payload(directory: Path, *, note: str | None = None) -> dict[str, Any]:
    directory = Path(directory)
    files = {p.name: sha256_file(p) for p in _hashable(directory)}
    payload: dict[str, Any] = {
        "algorithm": "sha256",
        "files": files,
        "count": len(files),
        "loaded_from": str(directory),
    }
    if note:
        payload["note"] = note
    return payload


def write_manifest(directory: Path, *, note: str | None = None) -> dict[str, Any]:
    directory = Path(directory)
    payload = manifest_payload(directory, note=note)
    (directory / MANIFEST_NAME).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return payload


def verify(directory: Path) -> dict[str, Any]:
    """Compare the directory against its manifest without deserialising anything."""
    directory = Path(directory)
    manifest = directory / MANIFEST_NAME
    if not manifest.exists():
        return {"status": "ABSENT", "detail": f"no {MANIFEST_NAME} — artifact not hash-verified",
                "checked": 0, "mismatched": []}
    try:
        expected = json.loads(manifest.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"status": "MISMATCH", "detail": f"manifest unreadable: {type(exc).__name__}",
                "checked": 0, "mismatched": []}
    files = expected.get("files") or {}
    mismatched: list[str] = []
    for name, digest in files.items():
        target = directory / name
        if not target.exists() or sha256_file(target) != digest:
            mismatched.append(name)
    return {
        "status": "OK" if not mismatched else "MISMATCH",
        "detail": ("all hashed files match the manifest" if not mismatched
                   else f"does not match the manifest: {', '.join(sorted(mismatched))}"),
        "checked": len(files),
        "mismatched": sorted(mismatched),
    }
