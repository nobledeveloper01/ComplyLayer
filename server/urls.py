"""Root URLconf.

The decision endpoint only. The management API arrives in phase 5 from a
separate settings module, so a decision worker never routes a management
request (D7).
"""

from django.urls import include, path

urlpatterns = [
    path("", include("complylayer.urls")),
]
