"""The HTTP surface.

Two paths, deliberately (D1 and D7). The decision endpoint here is plain Django
and carries the latency contract; the management API arrives in phase 5 on DRF,
in a separate settings module, so a decision worker has no route to it at all.
"""
