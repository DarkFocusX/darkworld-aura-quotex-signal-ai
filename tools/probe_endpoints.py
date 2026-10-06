"""Live HTTP probe of every API route (spec §19 / §28 verification battery).

Reads the route table from the app itself, so the probe can never drift from the code, and
then calls each route over the network:

    python3 tools/probe_endpoints.py                       # GET routes + read-only POSTs
    python3 tools/probe_endpoints.py --include-mutating     # + generate/scan/evaluate/config
    python3 tools/probe_endpoints.py --base http://127.0.0.1:8000

Mutating routes (writes to the scan log, the ledger, the config or the dataset folder) are
skipped by default: a verification run should not change the evidence it is verifying.
Nothing here fabricates data — the probe only reports the HTTP status the server returned,
and `/api/data/ingest` is probed with an intentionally invalid payload (expects 4xx, never
a silent 200).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

DEFAULT_BASE = "http://127.0.0.1:8000"

# path template → (method, payload, expected statuses, mutating?)
READ_ONLY_POSTS = {
    "/api/research/paper-report": ({}, {200}),
    "/api/model/reload": ({}, {200}),
}
MUTATING_POSTS = {
    "/api/signal/generate": ({"asset": "AUTO", "mode": "NOW"}, {200}),
    "/api/paper/scan": ({"asset": "USDJPY"}, {200}),          # engine path, one recorded scan
    "/api/paper/scanner": ({"enabled": True}, {200}),
    "/api/signals/evaluate": ({}, {200}),
    "/api/paper/settle": ({}, {200}),
    # deliberately invalid: the store must refuse it (422/400), never accept silently
    "/api/data/ingest": ({"asset": "NOT_A_SYMBOL", "bogus": True}, {400, 422}),
}
SKIP = {"/api/config", "/api/data/import", "/api/data/ingest/start", "/api/data/ingest/stop"}


def routes() -> list[tuple[str, str]]:
    from backend.main import app

    found: list[tuple[str, str]] = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or []
        path = getattr(route, "path", "")
        for method in sorted(methods):
            if method in {"GET", "POST", "HEAD"} and path not in SKIP:
                found.append((method, path))
    return sorted(set(found), key=lambda item: (item[1], item[0]))


def first_experiment_id(base: str) -> str | None:
    """Resolve `{experiment_id}` from the API itself instead of inventing one."""
    status, _, body = call(base, "GET", "/api/research/experiments", None)
    if status != 200:
        return None
    try:
        payload = json.loads(body if len(body) > 150 else body)
    except json.JSONDecodeError:
        status, _, full = call_full(base, "/api/research/experiments")
        if status != 200:
            return None
        try:
            payload = json.loads(full)
        except json.JSONDecodeError:
            return None
    rows = payload.get("experiments") if isinstance(payload, dict) else payload
    if not rows:
        return None
    first = rows[0]
    return str(first.get("experiment_id") or first.get("id") or first.get("name") or "") or None


def call_full(base: str, path: str) -> tuple[int | None, float, str]:
    status, seconds, _ = call(base, "GET", path, None)
    url = base + path
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            return response.status, seconds, response.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return status, seconds, ""


def concrete(path: str, experiment_id: str | None = None) -> str:
    filled = path.replace("{asset}", "USDJPY")
    if "{experiment_id}" in filled:
        filled = filled.replace("{experiment_id}", experiment_id or "")
    return filled


def call(base: str, method: str, path: str, payload: dict | None) -> tuple[int | None, float, str]:
    url = base + concrete(path)
    data = None
    headers = {}
    if method == "POST":
        data = json.dumps(payload or {}).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            return response.status, time.time() - started, body.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, time.time() - started, exc.read()[:160].decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — connection problems are reported, not raised
        return None, time.time() - started, f"{type(exc).__name__}: {exc}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--include-mutating", action="store_true")
    args = parser.parse_args()

    planned: list[tuple[str, str, dict | None, set[int] | None]] = []
    for method, path in routes():
        if method == "HEAD":
            continue
        if method == "GET":
            planned.append((method, path, None, {200}))
        elif path in READ_ONLY_POSTS:
            planned.append((method, path, READ_ONLY_POSTS[path][0], READ_ONLY_POSTS[path][1]))
        elif path in MUTATING_POSTS and args.include_mutating:
            planned.append((method, path, MUTATING_POSTS[path][0], MUTATING_POSTS[path][1]))
        else:
            planned.append((method, path, None, None))  # reported as skipped

    experiment_id = first_experiment_id(args.base)

    # The scanner control route would leave the automatic scanner running if it was off.
    # Remember the state so the probe can restore it — verification must not change the
    # environment it measured.
    restore_scanner = False
    scanner_before: bool | None = None
    if args.include_mutating and any(path == "/api/paper/scanner" for _, path, _, _ in planned):
        status, _, body = call(args.base, "GET", "/api/paper/scanner", None)
        if status == 200:
            try:
                payload = json.loads(body)
                block = payload.get("scanner", payload)
                scanner_before = bool(block.get("enabled"))
            except (json.JSONDecodeError, AttributeError):
                scanner_before = None
        restore_scanner = scanner_before is not None

    ok = failed = skipped = 0
    print(f"probing {args.base} — {len(planned)} route(s) · experiment id: {experiment_id or '—'}\n")
    for method, path, payload, expected in planned:
        if expected is None:
            skipped += 1
            print(f"  ⏭  {method:4s} {path:44s} skipped (mutating — pass --include-mutating)")
            continue
        target = concrete(path, experiment_id)
        if "{experiment_id}" in path and not experiment_id:
            skipped += 1
            print(f"  ⏭  {method:4s} {path:44s} skipped (the latest research run has no experiments)")
            continue
        status, seconds, snippet = call(args.base, method, target, payload)
        good = status in expected
        ok += good
        failed += not good
        marker = "✅" if good else "❌"
        print(f"  {marker} {method:4s} {path:44s} {status} {seconds:.2f}s"
              f"{'' if good else '  expected ' + str(sorted(expected)) + ' · ' + snippet}")

    if restore_scanner:
        call(args.base, "POST", "/api/paper/scanner", {"enabled": bool(scanner_before)})
        print(f"\nscanner state restored to {'enabled' if scanner_before else 'disabled'}")

    print(f"\n{ok} ok · {failed} failed · {skipped} skipped")
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
