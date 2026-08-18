"""The decision endpoint, end to end over HTTP.

No database here — the store is injected. Phase 2's job is to settle the
contract; the integration tests that need real Postgres are marked and run
separately.
"""

from __future__ import annotations

import orjson
import pytest
from django.test import Client

from complylayer.api.handler import DecisionHandler
from complylayer.api.store import InMemoryStore
from complylayer.dsl import validate_source
from complylayer.engine import CompiledRule, RuleSet, Severity, State

VALID_BODY = {
    "transaction_ref": "TXN-2026-08-16-8842",
    "customer_ref": "usr_9931",
    "amount_minor": 75_000_000,
    "currency": "NGN",
    "transaction_type": "transfer",
    "channel": "mobile",
    "customer": {"kyc_tier": 2, "account_created_at": "2026-07-30T10:00:00Z", "country": "NG"},
    "destination": {"country": "NG", "bank_code": "058", "is_new_beneficiary": True},
    "device": {"id": "dev_a83f", "ip_country": "NG"},
}


def ruleset() -> RuleSet:
    return RuleSet(
        47,
        (
            CompiledRule(
                "rul_kyc_t2",
                "Tier 2 single transaction limit",
                validate_source("amount_minor > 50_000_000"),
                Severity.BLOCK,
                priority=10,
                regulatory_reference="CBN KYC Tier 2",
                customer_message="This transfer is above your tier 2 limit.",
            ),
            CompiledRule(
                "rul_large",
                "Large transfer",
                validate_source("amount_minor > 1_000_000"),
                Severity.FLAG,
                priority=20,
            ),
            CompiledRule(
                "rul_shadow",
                "Shadow rule",
                validate_source("amount_minor > 1"),
                Severity.BLOCK,
                state=State.SHADOW,
                priority=30,
            ),
        ),
    )


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def client(store, monkeypatch, view_only) -> Client:
    """Attaches a handler to each request, standing in for the auth middleware
    that resolves an API key to exactly one tenant in phase 5."""
    handler = DecisionHandler("tnt_test", ruleset(), store)

    from django.test import client as client_module

    original = client_module.Client.request

    def request_with_handler(self, **kwargs):
        from django.core.handlers.base import BaseHandler

        original_get_response = BaseHandler.get_response

        def get_response(handler_self, request):
            request.decision_handler = handler
            return original_get_response(handler_self, request)

        monkeypatch.setattr(BaseHandler, "get_response", get_response)
        return original(self, **kwargs)

    monkeypatch.setattr(client_module.Client, "request", request_with_handler)
    return Client()


def post(client: Client, body=None, key: str = "TXN-2026-08-16-8842", **extra):
    return client.post(
        "/v1/decisions",
        data=orjson.dumps(VALID_BODY if body is None else body),
        content_type="application/json",
        headers={"idempotency-key": key} if key else {},
        **extra,
    )


class TestASuccessfulDecision:
    def test_returns_the_contract_from_the_specification(self, client):
        response = post(client)
        assert response.status_code == 200

        body = orjson.loads(response.content)
        assert body["outcome"] == "block"
        assert body["reason"] == "Tier 2 single transaction limit"
        assert body["evaluated_rules"] == 3
        assert body["ruleset_version"] == 47
        assert body["degraded"] is False
        assert body["decision_id"].startswith("dec_")

    def test_matched_rules_carry_their_regulatory_reference(self, client):
        body = orjson.loads(post(client).content)
        blocking = next(r for r in body["matched_rules"] if r["id"] == "rul_kyc_t2")
        assert blocking["regulatory_reference"] == "CBN KYC Tier 2"
        assert blocking["severity"] == "block"

    def test_a_block_carries_the_message_compliance_wrote(self, client):
        """The wording a customer sees is a compliance decision, not an
        engineering one (§7.1)."""
        body = orjson.loads(post(client).content)
        assert body["customer_message"] == "This transfer is above your tier 2 limit."

    def test_a_flag_carries_no_customer_message(self, client):
        body = orjson.loads(post(client, {**VALID_BODY, "amount_minor": 2_000_000}).content)
        assert body["outcome"] == "flag"
        assert "customer_message" not in body

    def test_shadow_matches_never_reach_the_caller(self, client):
        """Evaluated and recorded, but invisible in the response and in the
        outcome — otherwise shadow mode would affect a customer."""
        body = orjson.loads(post(client, {**VALID_BODY, "amount_minor": 500}).content)
        assert body["outcome"] == "allow"
        assert "_shadow_matches" not in body
        assert not any(key.startswith("_") for key in body)

    def test_latency_is_reported_and_quantised(self, client):
        body = orjson.loads(post(client).content)
        assert body["latency_ms"] % 5 == 0


class TestIdempotency:
    def test_a_retry_returns_the_original_decision_verbatim(self, client):
        """Including the original timestamp. A retry reporting today's time
        would be a different decision wearing the same id (A4)."""
        first = orjson.loads(post(client, key="same-key").content)
        second = orjson.loads(post(client, key="same-key").content)
        assert first == second
        assert first["decision_id"] == second["decision_id"]
        assert first["decided_at"] == second["decided_at"]

    def test_a_different_key_is_a_different_decision(self, client):
        first = orjson.loads(post(client, key="key-a").content)
        second = orjson.loads(post(client, key="key-b").content)
        assert first["decision_id"] != second["decision_id"]

    def test_the_header_is_required(self, client):
        response = post(client, key="")
        assert response.status_code == 400
        body = orjson.loads(response.content)
        assert body["error"] == "idempotency_key_required"

    def test_a_replay_is_not_stored_twice(self, client, store):
        post(client, key="same-key")
        post(client, key="same-key")
        assert len(store.saved) == 1


class TestRejectedRequests:
    def test_a_get_is_refused(self, client):
        assert client.get("/v1/decisions").status_code == 405

    def test_an_empty_body(self, client):
        response = client.post(
            "/v1/decisions",
            data=b"",
            content_type="application/json",
            headers={"idempotency-key": "k"},
        )
        assert orjson.loads(response.content)["error"] == "empty_body"

    def test_invalid_json(self, client):
        response = client.post(
            "/v1/decisions",
            data=b"{not json",
            content_type="application/json",
            headers={"idempotency-key": "k"},
        )
        assert orjson.loads(response.content)["error"] == "invalid_json"

    @pytest.mark.parametrize(
        "field", ["transaction_ref", "customer_ref", "amount_minor", "currency"]
    )
    def test_a_missing_required_field_names_itself(self, client, field):
        body = {key: value for key, value in VALID_BODY.items() if key != field}
        response = post(client, body)
        assert response.status_code == 400
        assert orjson.loads(response.content)["field"] == field

    def test_an_unknown_field_is_refused_rather_than_ignored(self, client):
        """§8.4: the strongest version of "we cannot leak what we never
        collected" is a payload that will not parse if it carries something
        unexpected."""
        response = post(client, {**VALID_BODY, "pan": "4111111111111111"})
        assert response.status_code == 400
        body = orjson.loads(response.content)
        assert body["error"] == "unknown_field"
        assert body["field"] == "pan"

    def test_an_unknown_nested_field_is_refused_too(self, client):
        response = post(client, {**VALID_BODY, "customer": {"kyc_tier": 2, "bvn": "12345678901"}})
        assert orjson.loads(response.content)["field"] == "customer.bvn"

    @pytest.mark.parametrize("amount", [1.5, "75000000", True, -1])
    def test_the_amount_must_be_a_whole_non_negative_number(self, client, amount):
        response = post(client, {**VALID_BODY, "amount_minor": amount})
        assert response.status_code == 400

    @pytest.mark.parametrize("currency", ["NGNN", "N1", "12"])
    def test_the_currency_must_be_a_three_letter_code(self, client, currency):
        response = post(client, {**VALID_BODY, "currency": currency})
        assert orjson.loads(response.content)["error"] == "invalid_currency"

    def test_the_currency_is_normalised_to_upper_case(self, client, store):
        post(client, {**VALID_BODY, "currency": "ngn"})
        assert store.saved[0]["body"]["_resolved_facts"]["currency"] == "NGN"

    def test_an_oversized_body_is_refused_before_parsing(self, client):
        response = post(client, {**VALID_BODY, "transaction_ref": "x" * 20_000})
        assert response.status_code == 413


class TestDegradedDecisions:
    def test_a_missing_fact_fails_closed_for_a_block_rule(self, store, monkeypatch):
        """The control did not run, so the safe reading is the one that does not
        let the transaction through — and it is recorded as degraded."""
        rules = RuleSet(
            1,
            (
                CompiledRule(
                    "b",
                    "Needs a fact nobody sent",
                    validate_source("mystery_fact > 1"),
                    Severity.BLOCK,
                ),
            ),
        )
        handler = DecisionHandler("tnt", rules, store)
        from complylayer.api.validation import parse_transaction

        body = handler.decide(parse_transaction(VALID_BODY), "k")
        assert body["outcome"] == "block"
        assert body["degraded"] is True
        assert body["_errored_rules"][0]["id"] == "b"
