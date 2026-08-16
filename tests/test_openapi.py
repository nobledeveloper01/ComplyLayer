"""The OpenAPI spec, checked against the routes that actually exist.

A spec drifts silently. It is documentation, so nothing fails when it stops being
true, and the first person to notice is a customer generating a client against an
endpoint that moved. So it is tested the way the README is: enumerate the real
routes, and fail if the document disagrees.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from django.urls import get_resolver

SPEC_PATH = Path(__file__).resolve().parent.parent / "docs" / "openapi.yaml"


@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text())


def normalise(pattern: str) -> str:
    """Turn a Django route into an OpenAPI path.

    `^rules/(?P<pk>[^/.]+)/$` becomes `/v1/rules/{id}`, so the two vocabularies
    can be compared at all.
    """
    # Anchors can appear mid-path once prefixes are concatenated: a router's
    # `^rules/$` under an include's `v1/` gives `v1/^rules/$`. Stripping only the
    # ends leaves the caret in the middle.
    path = re.sub(r"[\^$]", "", pattern).rstrip("/")
    path = re.sub(r"\(\?P<[^>]+>[^)]+\)", "{id}", path)
    return "/" + path


def real_routes() -> set[str]:
    """Walk the URLconf recursively, accumulating prefixes.

    Written recursively after a flat two-level version silently dropped the
    `v1/` prefix contributed by a nested include — which made every route look
    undocumented, and would have made the opposite mistake just as quietly.
    """
    routes: set[str] = set()

    def walk(patterns, prefix: str) -> None:
        for entry in patterns:
            pattern = prefix + str(entry.pattern)
            nested = getattr(entry, "url_patterns", None)
            if nested is not None:
                walk(nested, pattern)
                continue
            name = getattr(entry, "name", "") or ""
            if "format" in pattern or "api-root" in name:
                continue
            # The dashboard serves HTML to a signed-in person, not an API to a
            # key holder. `openapi.yaml` documents the contract an integrator
            # generates a client from; a login page is not part of it.
            if pattern.startswith("dashboard/"):
                continue
            routes.add(normalise(pattern))

    walk(get_resolver("server.urls_management").url_patterns, "")
    return routes


def documented(spec: dict) -> set[str]:
    return {
        path.replace("{ruleId}", "{id}")
        .replace("{version}", "{id}")
        .replace("{decisionId}", "{id}")
        .replace("{listId}", "{id}")
        .rstrip("/")
        for path in spec["paths"]
    }


class TestTheSpecMatchesReality:
    def test_every_management_route_is_documented(self, spec):
        """A route nobody documented is a route a customer cannot use."""
        undocumented = {route.rstrip("/") for route in real_routes()} - documented(spec)
        assert not undocumented, f"these routes are not in openapi.yaml: {sorted(undocumented)}"

    def test_the_decision_endpoint_is_documented(self, spec):
        assert "post" in spec["paths"]["/v1/decisions"]

    def test_the_probes_are_documented_and_unauthenticated(self, spec):
        for path in ("/healthz", "/readyz", "/metrics"):
            assert spec["paths"][path]["get"]["security"] == []


class TestTheContractsThatMatter:
    """The spec is where an integrator learns the things that will bite them."""

    def test_the_idempotency_header_is_documented_as_required(self, spec):
        parameters = spec["paths"]["/v1/decisions"]["post"]["parameters"]
        header = next(p for p in parameters if p["name"] == "Idempotency-Key")
        assert header["required"] is True

    def test_unknown_fields_are_documented_as_refused(self, spec):
        """§8.4. An integrator who learns this from a 400 in production learned
        it too late."""
        request = spec["components"]["schemas"]["DecisionRequest"]
        assert request["additionalProperties"] is False
        for nested in ("customer", "destination", "device"):
            assert request["properties"][nested]["additionalProperties"] is False

    def test_amount_is_documented_as_whole_minor_units(self, spec):
        amount = spec["components"]["schemas"]["DecisionRequest"]["properties"]["amount_minor"]
        assert amount["type"] == "integer"
        assert "minor units" in amount["description"]

    def test_the_degraded_flag_explains_itself(self, spec):
        """A caller who does not know what `degraded` means will ignore it, and
        it is the field that says a control did not run."""
        # Whitespace-normalised: a YAML block scalar wraps lines, so asserting on
        # a phrase that happens to straddle a wrap tests the line width rather
        # than the wording.
        description = " ".join(
            spec["components"]["schemas"]["Decision"]["properties"]["degraded"][
                "description"
            ].split()
        )
        assert "fail closed" in description
        assert "fail open" in description

    def test_the_403_versus_404_choice_is_explained(self, spec):
        description = " ".join(spec["components"]["responses"]["NotFound"]["description"].split())
        assert "another tenant" in description

    def test_rule_errors_document_their_three_parts(self, spec):
        properties = spec["components"]["schemas"]["RuleError"]["properties"]
        assert {"problem", "fix", "reason"} <= set(properties)

    def test_self_approval_is_documented_where_somebody_will_meet_it(self, spec):
        description = " ".join(
            spec["paths"]["/v1/rules/{ruleId}/approve"]["post"]["description"].split()
        )
        assert "Never your own" in description


class TestTheDocumentIsValidEnough:
    def test_it_declares_openapi_31(self, spec):
        assert spec["openapi"].startswith("3.1")

    def test_every_reference_resolves(self, spec):
        """A broken $ref is a spec that will not generate a client."""
        text = SPEC_PATH.read_text()
        for section, name in set(re.findall(r"#/components/(\w+)/(\w+)", text)):
            assert name in spec["components"][section], f"dangling ref: {section}/{name}"

    def test_every_operation_has_an_id_and_a_summary(self, spec):
        for path, operations in spec["paths"].items():
            for verb, operation in operations.items():
                if verb == "parameters":
                    continue
                assert operation.get("operationId"), f"{verb} {path} has no operationId"
                assert operation.get("summary"), f"{verb} {path} has no summary"

    def test_operation_ids_are_unique(self, spec):
        """Generators produce method names from these; a collision is a client
        that will not compile."""
        ids = [
            operation["operationId"]
            for operations in spec["paths"].values()
            for verb, operation in operations.items()
            if verb != "parameters"
        ]
        assert len(ids) == len(set(ids))
