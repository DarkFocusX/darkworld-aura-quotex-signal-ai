# Research report — targets, models, features, regimes, calibration, thresholds

- generated: 2026-10-02T10:45:10+00:00
- experiments mined: **33** (records in `backend/research/experiments/`)
- dataset fingerprint(s): ds-d21e6369e945
- walk-forward folds per experiment: [2, 3] — the same chronological fold construction is used everywhere; a cell only has fewer folds when its regime slice has fewer rows
- this report re-runs nothing: every number is read from the stored experiment records, so it cannot drift from the measurement it describes

## 4. Targets — NEXT_CANDLE_DIRECTION and M1_EXPIRY_OUTCOME, kept separate

The two targets are *never* silently substituted for one another: each cell states which target it measured, and the neutral band is the ATR-normalised one the config names (`neutral_mode=atr`, fraction 0.25). Horizons 1/2/3/5 are measured, not assumed.

| target | horizon | neutral | cells | holdout samples | accuracy | macro F1 | AUC | Brier | directional hit rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| M1_EXPIRY_OUTCOME | 1 | atr0.25 | 13 | 11280 | 0.3676 | 0.3657 | 0.5448 | 0.6602 | 0.3315 |
| M1_EXPIRY_OUTCOME | 2 | atr0.25 | 2 | 11279 | 0.3829 | 0.3792 | 0.5533 | 0.6575 | 0.3866 |
| M1_EXPIRY_OUTCOME | 3 | atr0.25 | 2 | 11279 | 0.3981 | 0.3852 | 0.5559 | 0.6549 | 0.4190 |
| M1_EXPIRY_OUTCOME | 5 | atr0.25 | 2 | 11278 | 0.4106 | 0.3865 | 0.5542 | 0.6531 | 0.4394 |
| NEXT_CANDLE_DIRECTION | 1 | atr0.25 | 8 | 11280 | 0.3703 | 0.3699 | 0.5570 | 0.6594 | 0.3447 |
| NEXT_CANDLE_DIRECTION | 2 | atr0.25 | 2 | 11279 | 0.3929 | 0.3834 | 0.5670 | 0.6533 | 0.4026 |
| NEXT_CANDLE_DIRECTION | 3 | atr0.25 | 2 | 11279 | 0.4032 | 0.3859 | 0.5603 | 0.6541 | 0.4233 |
| NEXT_CANDLE_DIRECTION | 5 | atr0.25 | 2 | 11278 | 0.4152 | 0.3893 | 0.5587 | 0.6517 | 0.4415 |

Class balance is stored per experiment (`per_class` supports PUT / NEUTRAL / CALL); a benchmark that predicts NEUTRAL is judged on macro F1, never on raw accuracy — with three classes ~0.33 accuracy is chance, not skill.

## 5. Model families and challengers

| family | cells | median holdout accuracy | median macro F1 | median AUC | median Brier |
| --- | --- | --- | --- | --- | --- |
| lightgbm | 21 | 0.3829 | 0.3789 | 0.5538 | 0.6563 |
| logistic_regression | 4 | 0.3703 | 0.3691 | 0.5561 | 0.6615 |
| logistic_regression+random_forest+lightgbm | 4 | 0.3676 | 0.3657 | 0.5645 | 0.6599 |
| random_forest | 4 | 0.3650 | 0.3619 | 0.5625 | 0.6603 |

Challenger availability (a missing dependency is reported, never fabricated):

- `xgboost`: UNAVAILABLE (ModuleNotFoundError)
- `torch`: UNAVAILABLE (ModuleNotFoundError)
- `tensorflow`: UNAVAILABLE (ModuleNotFoundError)
- `keras`: UNAVAILABLE (ModuleNotFoundError)

Per-fold detail (same chronological train/test construction in every cell, so cells are comparable; fold counts differ only where a slice is short):

| experiment | target | h | family | feature set | fold accuracies | fold macro F1 |
| --- | --- | --- | --- | --- | --- | --- |
| `M1_EXPIRY_OUTCOME-h1-base-lightgbm` | M1_EXPIRY_OUTCOME | 1 | lightgbm | m1-features-1.0 | 0.3688, 0.3545, 0.3226 | 0.3653, 0.3537, 0.3218 |
| `M1_EXPIRY_OUTCOME-h1-base-logistic_regression` | M1_EXPIRY_OUTCOME | 1 | logistic_regression | m1-features-1.0 | 0.3572, 0.3518, 0.3528 | 0.3494, 0.3463, 0.3528 |
| `M1_EXPIRY_OUTCOME-h1-base-logistic_regression-random_forest-lightgbm` | M1_EXPIRY_OUTCOME | 1 | logistic_regression+random_forest+lightgbm | m1-features-1.0 | 0.3755, 0.3572, 0.3312 | 0.3715, 0.3566, 0.3292 |
| `M1_EXPIRY_OUTCOME-h1-base-random_forest` | M1_EXPIRY_OUTCOME | 1 | random_forest | m1-features-1.0 | 0.3651, 0.3558, 0.3392 | 0.3511, 0.3547, 0.3343 |
| `M1_EXPIRY_OUTCOME-h1-extended-anomaly_clean` | M1_EXPIRY_OUTCOME | 1 | lightgbm | m1-features-lab-1.0 | 0.3638, 0.3611, 0.3366 | 0.3607, 0.3598, 0.3351 |
| `M1_EXPIRY_OUTCOME-h1-extended-high_vol` | M1_EXPIRY_OUTCOME | 1 | lightgbm | m1-features-lab-1.0 | 0.3728, 0.3395, 0.3581 | 0.3656, 0.3394, 0.3576 |
| `M1_EXPIRY_OUTCOME-h1-extended-lightgbm` | M1_EXPIRY_OUTCOME | 1 | lightgbm | m1-features-lab-1.0 | 0.3568, 0.3608, 0.3382 | 0.3523, 0.3601, 0.3370 |
| `M1_EXPIRY_OUTCOME-h1-extended-logistic_regression` | M1_EXPIRY_OUTCOME | 1 | logistic_regression | m1-features-lab-1.0 | 0.3761, 0.3525, 0.3575 | 0.3696, 0.3491, 0.3574 |
| `M1_EXPIRY_OUTCOME-h1-extended-logistic_regression-random_forest-lightgbm` | M1_EXPIRY_OUTCOME | 1 | logistic_regression+random_forest+lightgbm | m1-features-lab-1.0 | 0.3645, 0.3562, 0.3432 | 0.3595, 0.3555, 0.3415 |
| `M1_EXPIRY_OUTCOME-h1-extended-low_vol` | M1_EXPIRY_OUTCOME | 1 | lightgbm | m1-features-lab-1.0 | 0.3417, 0.3296, 0.3589 | 0.3337, 0.3290, 0.3537 |
| `M1_EXPIRY_OUTCOME-h1-extended-random_forest` | M1_EXPIRY_OUTCOME | 1 | random_forest | m1-features-lab-1.0 | 0.3668, 0.3518, 0.3425 | 0.3549, 0.3501, 0.3367 |
| `M1_EXPIRY_OUTCOME-h1-extended-range` | M1_EXPIRY_OUTCOME | 1 | lightgbm | m1-features-lab-1.0 | 0.3562, 0.3571, 0.3451 | 0.3544, 0.3547, 0.3436 |
| `M1_EXPIRY_OUTCOME-h1-extended-trend` | M1_EXPIRY_OUTCOME | 1 | lightgbm | m1-features-lab-1.0 | 0.3356, 0.3356 | 0.3307, 0.3244 |
| `M1_EXPIRY_OUTCOME-h2-base-lightgbm` | M1_EXPIRY_OUTCOME | 2 | lightgbm | m1-features-1.0 | 0.3645, 0.3878, 0.3612 | 0.3634, 0.3832, 0.3464 |
| `M1_EXPIRY_OUTCOME-h2-extended-lightgbm` | M1_EXPIRY_OUTCOME | 2 | lightgbm | m1-features-lab-1.0 | 0.3642, 0.3814, 0.3555 | 0.3632, 0.3754, 0.3414 |
| `M1_EXPIRY_OUTCOME-h3-base-lightgbm` | M1_EXPIRY_OUTCOME | 3 | lightgbm | m1-features-1.0 | 0.3765, 0.3951, 0.3904 | 0.3699, 0.3861, 0.3632 |
| `M1_EXPIRY_OUTCOME-h3-extended-lightgbm` | M1_EXPIRY_OUTCOME | 3 | lightgbm | m1-features-lab-1.0 | 0.3655, 0.3854, 0.3901 | 0.3592, 0.3786, 0.3642 |
| `M1_EXPIRY_OUTCOME-h5-base-lightgbm` | M1_EXPIRY_OUTCOME | 5 | lightgbm | m1-features-1.0 | 0.3871, 0.4017, 0.3705 | 0.3680, 0.3703, 0.3385 |
| `M1_EXPIRY_OUTCOME-h5-extended-lightgbm` | M1_EXPIRY_OUTCOME | 5 | lightgbm | m1-features-lab-1.0 | 0.3881, 0.3981, 0.3824 | 0.3708, 0.3720, 0.3472 |
| `NEXT_CANDLE_DIRECTION-h1-base-lightgbm` | NEXT_CANDLE_DIRECTION | 1 | lightgbm | m1-features-1.0 | 0.3881, 0.3738, 0.3502 | 0.3879, 0.3733, 0.3427 |
| `NEXT_CANDLE_DIRECTION-h1-base-logistic_regression` | NEXT_CANDLE_DIRECTION | 1 | logistic_regression | m1-features-1.0 | 0.3878, 0.3512, 0.3678 | 0.3869, 0.3489, 0.3614 |
| `NEXT_CANDLE_DIRECTION-h1-base-logistic_regression-random_forest-lightgbm` | NEXT_CANDLE_DIRECTION | 1 | logistic_regression+random_forest+lightgbm | m1-features-1.0 | 0.3984, 0.3635, 0.3568 | 0.3959, 0.3597, 0.3431 |
| `NEXT_CANDLE_DIRECTION-h1-base-random_forest` | NEXT_CANDLE_DIRECTION | 1 | random_forest | m1-features-1.0 | 0.3911, 0.3575, 0.3552 | 0.3855, 0.3500, 0.3283 |
| `NEXT_CANDLE_DIRECTION-h1-extended-lightgbm` | NEXT_CANDLE_DIRECTION | 1 | lightgbm | m1-features-lab-1.0 | 0.3778, 0.3668, 0.3542 | 0.3775, 0.3664, 0.3493 |
| `NEXT_CANDLE_DIRECTION-h1-extended-logistic_regression` | NEXT_CANDLE_DIRECTION | 1 | logistic_regression | m1-features-lab-1.0 | 0.3898, 0.3635, 0.3622 | 0.3895, 0.3626, 0.3583 |
| `NEXT_CANDLE_DIRECTION-h1-extended-logistic_regression-random_forest-lightgbm` | NEXT_CANDLE_DIRECTION | 1 | logistic_regression+random_forest+lightgbm | m1-features-lab-1.0 | 0.3871, 0.3622, 0.3615 | 0.3855, 0.3591, 0.3515 |
| `NEXT_CANDLE_DIRECTION-h1-extended-random_forest` | NEXT_CANDLE_DIRECTION | 1 | random_forest | m1-features-lab-1.0 | 0.3858, 0.3572, 0.3562 | 0.3793, 0.3496, 0.3323 |
| `NEXT_CANDLE_DIRECTION-h2-base-lightgbm` | NEXT_CANDLE_DIRECTION | 2 | lightgbm | m1-features-1.0 | 0.3781, 0.3928, 0.3628 | 0.3755, 0.3821, 0.3485 |
| `NEXT_CANDLE_DIRECTION-h2-extended-lightgbm` | NEXT_CANDLE_DIRECTION | 2 | lightgbm | m1-features-lab-1.0 | 0.3848, 0.3908, 0.3718 | 0.3814, 0.3809, 0.3587 |
| `NEXT_CANDLE_DIRECTION-h3-base-lightgbm` | NEXT_CANDLE_DIRECTION | 3 | lightgbm | m1-features-1.0 | 0.3921, 0.3997, 0.3911 | 0.3768, 0.3821, 0.3561 |
| `NEXT_CANDLE_DIRECTION-h3-extended-lightgbm` | NEXT_CANDLE_DIRECTION | 3 | lightgbm | m1-features-lab-1.0 | 0.3947, 0.3914, 0.3957 | 0.3816, 0.3734, 0.3586 |
| `NEXT_CANDLE_DIRECTION-h5-base-lightgbm` | NEXT_CANDLE_DIRECTION | 5 | lightgbm | m1-features-1.0 | 0.3971, 0.4157, 0.3881 | 0.3751, 0.3876, 0.3561 |
| `NEXT_CANDLE_DIRECTION-h5-extended-lightgbm` | NEXT_CANDLE_DIRECTION | 5 | lightgbm | m1-features-lab-1.0 | 0.3891, 0.4130, 0.4014 | 0.3697, 0.3888, 0.3684 |

## 6. Features

Every feature set is causal by construction and covered by the leakage tests (`tests/test_features.py`): a feature may only use candles at or before the scanned candle. The extended lab block adds momentum-slope and structure terms on top of the base block; it is kept only when the untouched holdout agrees.

| feature version | count | cells | median macro F1 | best macro F1 | delta vs base (same target/h |
| --- | --- | --- | --- | --- | --- |
| m1-features-1.0 | 44 | 14 | 0.3699 | 0.3893 | 0.0000 |
| m1-features-lab-1.0 | 56 | 19 | 0.3655 | 0.3885 | -0.0008 |

No feature is kept for an in-sample gain; a delta that is not visible on the untouched holdout is reported as zero evidence, not as an improvement.

## 7. Regimes

| regime filter | cells | median holdout macro F1 | median accuracy | note |
| --- | --- | --- | --- | --- |
| all | 28 | 0.3691 | 0.3703 | all minutes (reference) |
| anomaly_clean | 1 | 0.3600 | 0.3610 | OOS slice — no improvement may be claimed before untouched evidence |
| high_vol | 1 | 0.3655 | 0.3684 | OOS slice — no improvement may be claimed before untouched evidence |
| low_vol | 1 | 0.3506 | 0.3554 | OOS slice — no improvement may be claimed before untouched evidence |
| range | 1 | 0.3615 | 0.3674 | OOS slice — no improvement may be claimed before untouched evidence |
| trend | 1 | 0.3452 | 0.3457 | OOS slice — no improvement may be claimed before untouched evidence |

## 8. Calibration

The stored calibration is the reliability of the predicted class: bins with sample counts, mean confidence and realised accuracy. A bucket is only reported as meaningful with enough observations; the records flag under ~200 samples as unproven, and confidence is never described as a probability of profit.

| experiment | temperature | validation ECE | holdout ECE | holdout samples | largest holdout bucket | bucket samples | bin confidence → accuracy |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `M1_EXPIRY_OUTCOME-h1-base-lightgbm` | 1.8310 | 0.0400 | 0.0325 | 11280 | 0.3-0.4 | 8589 | 0.3669 → 0.3408 |
| `M1_EXPIRY_OUTCOME-h1-base-logistic_regression` | 1.3758 | 0.0152 | 0.0071 | 11280 | 0.3-0.4 | 10391 | 0.3607 → 0.3635 |
| `M1_EXPIRY_OUTCOME-h1-base-logistic_regression-random` | 0.9625 | 0.0149 | 0.0089 | 11280 | 0.3-0.4 | 10342 | 0.3573 → 0.3535 |
| `M1_EXPIRY_OUTCOME-h1-base-random_forest` | 0.7159 | 0.0099 | 0.0078 | 11280 | 0.3-0.4 | 10726 | 0.3508 → 0.3538 |
| `M1_EXPIRY_OUTCOME-h1-extended-anomaly_clean` | 1.7284 | 0.0346 | 0.0295 | 11167 | 0.3-0.4 | 8517 | 0.3671 → 0.3439 |
| `M1_EXPIRY_OUTCOME-h1-extended-high_vol` | 2.6372 | 0.0674 | 0.0521 | 3835 | 0.3-0.4 | 1918 | 0.3731 → 0.3441 |
| `M1_EXPIRY_OUTCOME-h1-extended-lightgbm` | 1.7283 | 0.0363 | 0.0301 | 11280 | 0.3-0.4 | 8605 | 0.3670 → 0.3468 |
| `M1_EXPIRY_OUTCOME-h1-extended-logistic_regression` | 1.2865 | 0.0124 | 0.0072 | 11280 | 0.3-0.4 | 10085 | 0.3621 → 0.3575 |
| `M1_EXPIRY_OUTCOME-h1-extended-logistic_regression-ra` | 0.9388 | 0.0154 | 0.0066 | 11280 | 0.3-0.4 | 10349 | 0.3580 → 0.3547 |
| `M1_EXPIRY_OUTCOME-h1-extended-low_vol` | 3.0000 | 0.0805 | 0.0611 | 3723 | 0.3-0.4 | 1757 | 0.3737 → 0.3352 |
| `M1_EXPIRY_OUTCOME-h1-extended-random_forest` | 0.7233 | 0.0105 | 0.0079 | 11280 | 0.3-0.4 | 10795 | 0.3505 → 0.3541 |
| `M1_EXPIRY_OUTCOME-h1-extended-range` | 3.0000 | 0.0606 | 0.0431 | 4056 | 0.3-0.4 | 2179 | 0.3720 → 0.3653 |
| `M1_EXPIRY_OUTCOME-h1-extended-trend` | 3.0000 | 0.1344 | 0.1109 | 1611 | 0.4-0.5 | 771 | 0.4458 → 0.3256 |
| `M1_EXPIRY_OUTCOME-h2-base-lightgbm` | 1.2666 | 0.0261 | 0.0147 | 11279 | 0.3-0.4 | 7829 | 0.3701 → 0.3620 |
| `M1_EXPIRY_OUTCOME-h2-extended-lightgbm` | 1.4094 | 0.0289 | 0.0145 | 11279 | 0.3-0.4 | 7873 | 0.3695 → 0.3661 |
| `M1_EXPIRY_OUTCOME-h3-base-lightgbm` | 1.2535 | 0.0169 | 0.0186 | 11279 | 0.3-0.4 | 6919 | 0.3715 → 0.3765 |
| `M1_EXPIRY_OUTCOME-h3-extended-lightgbm` | 1.2810 | 0.0208 | 0.0156 | 11279 | 0.3-0.4 | 7090 | 0.3715 → 0.3793 |
| `M1_EXPIRY_OUTCOME-h5-base-lightgbm` | 1.3360 | 0.0238 | 0.0214 | 11278 | 0.3-0.4 | 6189 | 0.3729 → 0.3905 |
| `M1_EXPIRY_OUTCOME-h5-extended-lightgbm` | 1.2730 | 0.0212 | 0.0204 | 11278 | 0.3-0.4 | 6268 | 0.3726 → 0.3886 |
| `NEXT_CANDLE_DIRECTION-h1-base-lightgbm` | 1.4125 | 0.0343 | 0.0346 | 11280 | 0.3-0.4 | 7343 | 0.3688 → 0.3402 |
| `NEXT_CANDLE_DIRECTION-h1-base-logistic_regression` | 1.2732 | 0.0220 | 0.0177 | 11280 | 0.3-0.4 | 8193 | 0.3713 → 0.3549 |
| `NEXT_CANDLE_DIRECTION-h1-base-logistic_regression-ra` | 0.9282 | 0.0155 | 0.0204 | 11280 | 0.3-0.4 | 9007 | 0.3656 → 0.3437 |
| `NEXT_CANDLE_DIRECTION-h1-base-random_forest` | 0.7250 | 0.0081 | 0.0138 | 11280 | 0.3-0.4 | 10157 | 0.3600 → 0.3494 |
| `NEXT_CANDLE_DIRECTION-h1-extended-lightgbm` | 1.4171 | 0.0378 | 0.0363 | 11280 | 0.3-0.4 | 7222 | 0.3689 → 0.3412 |
| `NEXT_CANDLE_DIRECTION-h1-extended-logistic_regressio` | 1.2311 | 0.0245 | 0.0261 | 11280 | 0.3-0.4 | 7760 | 0.3717 → 0.3519 |
| `NEXT_CANDLE_DIRECTION-h1-extended-logistic_regressio` | 0.9242 | 0.0191 | 0.0173 | 11280 | 0.3-0.4 | 8887 | 0.3664 → 0.3497 |
| `NEXT_CANDLE_DIRECTION-h1-extended-random_forest` | 0.7206 | 0.0072 | 0.0109 | 11280 | 0.3-0.4 | 10287 | 0.3602 → 0.3528 |
| `NEXT_CANDLE_DIRECTION-h2-base-lightgbm` | 1.2990 | 0.0274 | 0.0117 | 11279 | 0.3-0.4 | 7201 | 0.3698 → 0.3723 |
| `NEXT_CANDLE_DIRECTION-h2-extended-lightgbm` | 1.2924 | 0.0227 | 0.0107 | 11279 | 0.3-0.4 | 7024 | 0.3700 → 0.3685 |
| `NEXT_CANDLE_DIRECTION-h3-base-lightgbm` | 1.2516 | 0.0191 | 0.0125 | 11279 | 0.3-0.4 | 6861 | 0.3715 → 0.3731 |
| `NEXT_CANDLE_DIRECTION-h3-extended-lightgbm` | 1.3173 | 0.0146 | 0.0168 | 11279 | 0.3-0.4 | 6893 | 0.3713 → 0.3836 |
| `NEXT_CANDLE_DIRECTION-h5-base-lightgbm` | 1.1607 | 0.0144 | 0.0184 | 11278 | 0.3-0.4 | 5765 | 0.3732 → 0.3906 |
| `NEXT_CANDLE_DIRECTION-h5-extended-lightgbm` | 1.1208 | 0.0151 | 0.0208 | 11278 | 0.3-0.4 | 5923 | 0.3733 → 0.3917 |

The live-paper view uses the spec's operator buckets (50–60 … 95–100) and prints the decided-trade count next to every rate in `PAPER_TRADING_REPORT.md`; the offline view above is the model-side reliability, kept separate from paper performance on purpose.

## 9. Thresholds and the evidence bar

Published bar: **≥ 100 signals AND Wilson 95 % lower bound > 0.50**. A threshold is selected on validation only and then tested once on the untouched holdout.

| experiment | thresholds tried | thresholds with signals | max signals | best Wilson LB | selected | qualifying cells |
| --- | --- | --- | --- | --- | --- | --- |
| `M1_EXPIRY_OUTCOME-h1-base-lightgbm` | 8 | 1 | 1 | 0.2065 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-base-logistic_regression` | 8 | 0 | 0 | 0.0000 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-base-logistic_regression-random` | 8 | 0 | 0 | 0.0000 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-base-random_forest` | 8 | 0 | 0 | 0.0000 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-extended-anomaly_clean` | 8 | 1 | 7 | 0.0822 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-extended-high_vol` | 8 | 3 | 18 | 0.2505 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-extended-lightgbm` | 8 | 1 | 2 | 0.0945 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-extended-logistic_regression` | 8 | 0 | 0 | 0.0000 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-extended-logistic_regression-ra` | 8 | 0 | 0 | 0.0000 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-extended-low_vol` | 8 | 2 | 10 | 0.2366 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-extended-random_forest` | 8 | 0 | 0 | 0.0000 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-extended-range` | 8 | 1 | 8 | 0.4093 | none | 0 |
| `M1_EXPIRY_OUTCOME-h1-extended-trend` | 8 | 3 | 7 | 0.2065 | none | 0 |
| `M1_EXPIRY_OUTCOME-h2-base-lightgbm` | 8 | 2 | 2 | 0.3424 | none | 0 |
| `M1_EXPIRY_OUTCOME-h2-extended-lightgbm` | 8 | 1 | 4 | 0.1500 | none | 0 |
| `M1_EXPIRY_OUTCOME-h3-base-lightgbm` | 8 | 3 | 16 | 0.4440 | none | 0 |
| `M1_EXPIRY_OUTCOME-h3-extended-lightgbm` | 8 | 2 | 17 | 0.3755 | none | 0 |
| `M1_EXPIRY_OUTCOME-h5-base-lightgbm` | 8 | 2 | 27 | 0.4423 | none | 0 |
| `M1_EXPIRY_OUTCOME-h5-extended-lightgbm` | 8 | 2 | 26 | 0.7102 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h1-base-lightgbm` | 8 | 3 | 19 | 0.5121 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h1-base-logistic_regression` | 8 | 2 | 5 | 0.2307 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h1-base-logistic_regression-ra` | 8 | 0 | 0 | 0.0000 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h1-base-random_forest` | 8 | 0 | 0 | 0.0000 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h1-extended-lightgbm` | 8 | 2 | 16 | 0.4440 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h1-extended-logistic_regressio` | 8 | 3 | 7 | 0.1582 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h1-extended-logistic_regressio` | 8 | 0 | 0 | 0.0000 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h1-extended-random_forest` | 8 | 0 | 0 | 0.0000 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h2-base-lightgbm` | 8 | 5 | 16 | 0.4440 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h2-extended-lightgbm` | 8 | 4 | 22 | 0.4732 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h3-base-lightgbm` | 8 | 3 | 27 | 0.5152 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h3-extended-lightgbm` | 8 | 3 | 16 | 0.3318 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h5-base-lightgbm` | 8 | 3 | 43 | 0.5017 | none | 0 |
| `NEXT_CANDLE_DIRECTION-h5-extended-lightgbm` | 8 | 2 | 47 | 0.6986 | none | 0 |

### Verdict

- 0 of 33 experiment cells selected a threshold; the rest produced **NO QUALIFIED EDGE** — a valid final result, not a failure to be papered over.
- largest signal count any threshold produced: 47 (bar: 100).
- best Wilson lower bound observed: 0.7102 (bar: > 0.50) — a high lower bound on a handful of trades is not evidence, which is exactly why the signal-count condition exists.
- no threshold was adopted into production by this report; adopting one would require the bar to clear on untouched data, and the audit would have to be superseded by new evidence.

## 10. What this report does not claim

- no profitability claim, no accuracy claim for live trading, no best-model ranking;
- the holdout numbers describe this operator export only (one instrument, one period) and may not transfer;
- paper performance is tracked separately in `PAPER_TRADING_REPORT.md` and is currently at zero decided trades, so the live evidence bar is NOT PASSED.

_generated by `tools/research_report.py` at 2026-10-02T10:45:10+00:00; sources: 33 experiment records + `backend/research/reports/latest.json`._
