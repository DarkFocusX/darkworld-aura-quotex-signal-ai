# Quotex Signal AI — validation report 20261002T070250Z

- Model version: `v1-market`
- Trained at: 2026-10-02T07:02:50+00:00
- Rows: 56396
- Data source: **market**
- Horizon: 1 candle(s); neutral band: 0.25 × ATR

## Walk-forward (out-of-fold)
- Accuracy: 0.3677
- Macro F1: 0.3664
- ROC-AUC (macro): 0.5638
- Brier: 0.6557
- Expected calibration error: 0.0249
- Directional win rate (gated rows): 34.52%
- Max losing streak: 24

## Final holdout
- Rows: 11280
- Accuracy: 0.3673
- Macro F1: 0.3658
- ROC-AUC (macro): 0.5622
- Directional win rate: 34.5%

## Confusion matrix (holdout, rows = actual, cols = predicted)

| actual \ predicted | PUT | NEUTRAL | CALL |
| --- | --- | --- | --- |
| PUT | 1438 | 703 | 1316 |
| NEUTRAL | 1503 | 1147 | 1600 |
| CALL | 1268 | 747 | 1558 |

## Calibration (holdout)

| bin | count | mean confidence | realised accuracy | gap |
| --- | --- | --- | --- | --- |
| 0.0-0.1 | 0 | None | None | None |
| 0.1-0.2 | 0 | None | None | None |
| 0.2-0.3 | 0 | None | None | None |
| 0.3-0.4 | 8430 | 0.3665 | 0.3473 | 0.0192 |
| 0.4-0.5 | 2505 | 0.4289 | 0.396 | 0.0329 |
| 0.5-0.6 | 258 | 0.5439 | 0.6124 | -0.0685 |
| 0.6-0.7 | 78 | 0.6362 | 0.7308 | -0.0946 |
| 0.7-0.8 | 9 | 0.7204 | 0.8889 | -0.1685 |
| 0.8-0.9 | 0 | None | None | None |
| 0.9-1.0 | 0 | None | None | None |

## Notes
- No random shuffling is used anywhere: folds are chronological with an embargo ≥ horizon.
- The holdout block is never used for model selection or calibration.
- Reported numbers come from stored evaluation output; nothing is hard-coded.