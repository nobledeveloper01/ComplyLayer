"""The management URLconf.

Mounted only by the management settings module. A decision worker never loads
this, so `POST /v1/rules/{id}/activate` is not a forbidden route there — it is
not a route at all (D7).
"""

from django.urls import include, path
from rest_framework.routers import DefaultRouter

from complylayer.api.management.views import (
    DecisionViewSet,
    NamedListViewSet,
    RuleSetVersionViewSet,
    RuleViewSet,
)

router = DefaultRouter()
router.register("rules", RuleViewSet, basename="rule")
router.register("rulesets", RuleSetVersionViewSet, basename="ruleset")
router.register("decisions", DecisionViewSet, basename="decision")
router.register("lists", NamedListViewSet, basename="list")

urlpatterns = [path("v1/", include(router.urls))]
