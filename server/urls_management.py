"""Root URLconf for the management workload.

The API and the dashboard together: both are management-side, both are behind
the same tenancy, and neither belongs on a decision worker (D7).
"""

from django.urls import include, path

from complylayer.api.health import healthz, metrics_view, readyz

urlpatterns = [
    path("", include("complylayer.api.management.urls")),
    path("dashboard/", include("complylayer.dashboard.urls")),
    path("healthz", healthz),
    path("readyz", readyz),
    path("metrics", metrics_view),
]
