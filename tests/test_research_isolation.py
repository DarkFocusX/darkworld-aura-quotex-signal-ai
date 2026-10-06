"""Research isolation (final master phase §2): the oracle can never reach production.

The oracle diagnostic in `tools/gate_study.py` looks at the direction the *settlement
candle actually moved*. That is future information, so it is allowed to exist only in an
offline research tool. These tests prove, mechanically, that:

1. no module under `backend/` (the code the API, scanner, engine and Telegram run)
   imports or mentions the oracle tool, its helpers or its artefacts;
2. no API route exposes it;
3. the production engine's model service is always the real artifact service — a scan
   never installs a model double by itself;
4. the research tool is one-way: it imports production code, never the other way round.

If a future change breaks any of these, a signal could be produced with knowledge of the
future — this file exists so that cannot happen silently.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "backend"
FORBIDDEN_TOKENS = (
    "gate_study",
    "oracle_direction",
    "_OracleModel",
    "_OracleEngine",
    "docs/qualified_minute",
    "docs/blocking_reasons",
    "tools.harness_model",
)


def _backend_sources() -> list[Path]:
    return [path for path in BACKEND.rglob("*.py") if "__pycache__" not in path.parts]


def test_no_backend_module_mentions_the_oracle_tool():
    offenders: list[str] = []
    for path in _backend_sources():
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN_TOKENS:
            if token in text:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {token}")
    assert offenders == [], f"production code references research-only tooling: {offenders}"


def test_backend_does_not_import_the_tools_package():
    """`tools/` is a consumer of production code, never a dependency of it."""
    offenders: list[str] = []
    for path in _backend_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name == "tools" or name.startswith("tools."):
                    offenders.append(f"{path.relative_to(PROJECT_ROOT)}: {name}")
    assert offenders == [], f"backend imports the research tooling: {offenders}"


def test_no_api_route_exposes_the_oracle():
    from backend.main import app

    paths = [getattr(route, "path", "") for route in app.routes]
    matches = [path for path in paths if re.search(r"oracle|future|hindsight", path, re.I)]
    assert matches == [], f"API exposes a research-only route: {matches}"


def test_production_engine_uses_the_real_model_service():
    """A bare SignalEngine wires the real artifact service — never a double."""
    from backend.signals.engine import SignalEngine

    engine = SignalEngine()
    service = engine.model_service()
    assert service.__class__.__module__.startswith("backend.models.service")
    assert not service.__class__.__name__.startswith("_")


def test_oracle_is_only_constructible_inside_the_research_tool():
    source = (PROJECT_ROOT / "tools" / "gate_study.py").read_text(encoding="utf-8")
    assert "class _OracleModel" in source
    # the future is read from the frame only in the research tool
    assert "entry_index" in source and "settlement_close" in source
    for path in _backend_sources():
        assert "entry_index" not in path.read_text(encoding="utf-8")
