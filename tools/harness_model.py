"""Harness-only model-layer double (spec §18 E/F) — one definition, clearly labelled.

`QualifiedModelLayer` stands in for the *learned model layer* only. It returns a
confident, unanimous ensemble so a scenario can ask "does a setup that reaches the
gates complete the signal path?" without the answer depending on the quality of the
current artifact.

It is deliberately NOT a shortcut around the system: prices, candles, freshness, the
feature block, the rule scores, the volatility/regime/anomaly/consistency gates, the
confidence arithmetic, the eligibility policy, the ledger and the per-candle lock all
stay production code on real operator candles. Anything measured while this double is
installed must be labelled "with a qualified model view" and can never be presented as
a result of the real model — `docs/blocking_reasons.json` and
`docs/VERIFICATION_PAPER_PHASE.md` state this explicitly wherever they use it.
"""
from __future__ import annotations


class QualifiedModelLayer:
    """A confident, unanimous stand-in for the learned model layer."""

    def __init__(self, real, direction: str = "CALL", probability: float = 0.93) -> None:
        self._real = real
        self._direction = direction
        self._probability = probability

    @property
    def metadata(self):
        return self._real.metadata

    def status(self) -> dict:
        status = dict(self._real.status())
        status["state"] = "READY_MARKET"      # the double claims a market-ready artifact
        status["production_usable"] = True
        return status

    def predict(self, values):  # noqa: ARG002 — the double ignores the features by design
        from backend.models.ensemble import EnsembleResult, ModelVote

        p = float(self._probability)
        other = round((1 - p) / 2, 4)
        votes = [
            ModelVote(model=name, p_put=other, p_neutral=other, p_call=p, weight=1.0,
                      vote=self._direction, status="OK")
            for name in ("logistic_regression", "random_forest", "lightgbm")
        ]
        return EnsembleResult(
            p_put=other if self._direction == "CALL" else p,
            p_neutral=other,
            p_call=p if self._direction == "CALL" else other,
            direction=self._direction,
            direction_probability=p,
            directional_probability_normalised=0.96,
            agreement=1.0,
            spread=0.01,
            votes=votes,
        )
