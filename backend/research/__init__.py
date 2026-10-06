"""Offline research: data preparation, training, walk-forward validation, reporting.

Nothing in this package is imported by the live signal path — training is a
separate step that writes artifacts which the model service then loads.
"""
