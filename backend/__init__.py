"""Quotex Signal AI — backend package.

Layering (strictly separated, see README):
    data/        market data providers (never fabricate live data)
    features/    technical feature engineering (M1)
    models/      ML models + ensemble scoring
    research/    offline training / walk-forward validation CLI
    validation/  pre-signal checks (data/model/feature/regime/confidence/consistency)
    signals/     signal engine, confidence engine, outcome tracking
    risk/        position/expiry/lockout policy (paper trading)
    execution/   broker adapters (paper adapter only — no live money execution)
    telegram/    notification adapter (env-var credentials only)
    database/    SQLite persistence
"""

__version__ = "1.0.0"
