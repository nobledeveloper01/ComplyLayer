"""The decision URLconf.

The decision endpoint and the probes. The management API mounts from its own
settings module in phase 5, so a decision worker has no route to rule management
at all — not a 403, no such URL (D7).
"""

from django.urls import path

from complylayer.api.decision import decisions
from complylayer.api.health import healthz, metrics_view, readyz

app_name = "complylayer"

urlpatterns = [
    path("v1/decisions", decisions, name="decisions"),
    path("healthz", healthz, name="healthz"),
    path("readyz", readyz, name="readyz"),
    path("metrics", metrics_view, name="metrics"),
]
