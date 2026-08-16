"""The management API.

Separate from the decision path by design (D1, D7). It runs on DRF, from its own
settings module, so a decision worker has no route to rule management at all.
"""
