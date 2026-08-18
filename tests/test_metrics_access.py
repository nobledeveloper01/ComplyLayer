"""`/metrics` is a customer list, so it is not served to anyone who asks.

Every series is labelled by tenant — that is deliberate, because the failure
`complylayer_ruleset_version` exists to catch is one worker serving a retired
rule set, and a per-pod label would hide it. The consequence is that the
endpoint enumerates every fintech using ComplyLayer, along with each one's rule
set version and how often it changes, and it is served on the same port as
`/v1/decisions`.

Open when no token is configured, because a self-hoster on a laptop reading
their own numbers should not have to set one up first.
"""

from __future__ import annotations

import pytest
from django.test import Client

pytestmark = pytest.mark.django_db

TOKEN = "a-scrape-token"  # noqa: S105


@pytest.fixture
def open_metrics(settings):
    settings.COMPLYLAYER = {**settings.COMPLYLAYER, "METRICS_TOKEN": ""}
    return settings


@pytest.fixture
def guarded_metrics(settings):
    settings.COMPLYLAYER = {**settings.COMPLYLAYER, "METRICS_TOKEN": TOKEN}
    return settings


class TestWhenAScrapeTokenIsConfigured:
    def test_no_token_is_refused(self, guarded_metrics):
        response = Client().get("/metrics")
        assert response.status_code == 401

    def test_a_wrong_token_is_refused(self, guarded_metrics):
        response = Client().get("/metrics", headers={"authorization": "Bearer not-the-token"})
        assert response.status_code == 401

    def test_the_right_token_is_served(self, guarded_metrics):
        response = Client().get("/metrics", headers={"authorization": f"Bearer {TOKEN}"})
        assert response.status_code == 200

    def test_the_refusal_says_how_prometheus_sends_it(self, guarded_metrics):
        """An operator who hits this needs the scrape-config line, not a 401."""
        response = Client().get("/metrics")
        assert b"bearer_token_file" in response.content

    def test_no_tenant_identifier_escapes_in_the_refusal(self, guarded_metrics):
        """The point of the control is that the body is the sensitive part."""
        response = Client().get("/metrics")
        assert b"complylayer_ruleset_version" not in response.content

    def test_a_prefix_of_the_token_does_not_work(self, guarded_metrics):
        """Compared with compare_digest, so a near-miss is not a hint."""
        response = Client().get("/metrics", headers={"authorization": f"Bearer {TOKEN[:-1]}"})
        assert response.status_code == 401


class TestWhenNoTokenIsConfigured:
    def test_metrics_are_open(self, open_metrics):
        """A laptop, where requiring setup to read your own numbers helps nobody."""
        assert Client().get("/metrics").status_code == 200

    def test_the_liveness_probe_never_needs_a_token(self, guarded_metrics):
        """A probe that has to authenticate is a probe that reports an outage
        when the token rotates."""
        assert Client().get("/healthz").status_code == 200
