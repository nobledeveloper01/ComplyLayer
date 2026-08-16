"""Root URLconf for the management workload."""

from django.urls import include, path

from complylayer.api.health import healthz, metrics_view, readyz

urlpatterns = [
    path("", include("complylayer.api.management.urls")),
    path("healthz", healthz),
    path("readyz", readyz),
    path("metrics", metrics_view),
]
