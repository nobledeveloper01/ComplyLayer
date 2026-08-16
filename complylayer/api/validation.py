"""Validating a decision request.

Hand-written because the schema is small, closed and versioned — and because an
unknown field has to be *rejected* rather than ignored. §8.4 lists what
ComplyLayer must never collect, and the strongest version of "we cannot leak what
we never collected" is a payload that will not parse if it carries something
unexpected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import orjson

from complylayer.api.errors import ApiError

MAX_BODY_BYTES = 16 * 1024

TOP_LEVEL_FIELDS = {
    "transaction_ref",
    "customer_ref",
    "amount_minor",
    "currency",
    "transaction_type",
    "channel",
    "customer",
    "destination",
    "device",
}
REQUIRED_FIELDS = {"transaction_ref", "customer_ref", "amount_minor", "currency"}

CUSTOMER_FIELDS = {"kyc_tier", "account_created_at", "country", "last_transaction_at"}
DESTINATION_FIELDS = {"country", "bank_code", "is_new_beneficiary"}
DEVICE_FIELDS = {"id", "ip_country"}

NESTED_FIELDS = {
    "customer": CUSTOMER_FIELDS,
    "destination": DESTINATION_FIELDS,
    "device": DEVICE_FIELDS,
}


@dataclass(frozen=True)
class Transaction:
    transaction_ref: str
    customer_ref: str
    amount_minor: int
    currency: str
    transaction_type: str = ""
    channel: str = ""
    customer: dict[str, Any] = field(default_factory=dict)
    destination: dict[str, Any] = field(default_factory=dict)
    device: dict[str, Any] = field(default_factory=dict)

    def as_context(self) -> dict[str, Any]:
        """The stored input, exactly as received."""
        return {
            "transaction_ref": self.transaction_ref,
            "customer_ref": self.customer_ref,
            "amount_minor": self.amount_minor,
            "currency": self.currency,
            "transaction_type": self.transaction_type,
            "channel": self.channel,
            "customer": self.customer,
            "destination": self.destination,
            "device": self.device,
        }


def parse_body(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError("body_too_large", f"Request body exceeds {MAX_BODY_BYTES} bytes.", 413)
    if not raw:
        raise ApiError("empty_body", "A decision request needs a JSON body.")
    try:
        payload = orjson.loads(raw)
    except orjson.JSONDecodeError:
        raise ApiError("invalid_json", "The request body is not valid JSON.") from None
    if not isinstance(payload, dict):
        raise ApiError("invalid_json", "The request body must be a JSON object.")
    return payload


def require_idempotency_key(headers) -> str:
    """Required, not optional.

    An optional header would mean A4's guarantee silently does not apply to
    whoever left it out — and the retry that needed it is the one nobody is
    watching. Better to refuse the integration than to quietly not protect it.
    """
    key = headers.get("Idempotency-Key", "")
    if not key:
        raise ApiError(
            "idempotency_key_required",
            "Send an Idempotency-Key header so a retry returns the original decision.",
        )
    if len(key) > 128:
        raise ApiError(
            "idempotency_key_too_long", "Idempotency-Key must be 128 characters or fewer."
        )
    return key


def parse_transaction(payload: dict[str, Any]) -> Transaction:
    _reject_unknown(payload, TOP_LEVEL_FIELDS, "")

    missing = REQUIRED_FIELDS - payload.keys()
    if missing:
        name = sorted(missing)[0]
        raise ApiError("missing_field", f"{name} is required.", field=name)

    for name in ("transaction_ref", "customer_ref", "currency"):
        _require_text(payload, name)
    for name in ("transaction_type", "channel"):
        if name in payload:
            _require_text(payload, name)

    amount = payload["amount_minor"]
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise ApiError(
            "invalid_amount",
            "amount_minor must be a whole number of minor units (kobo, cents).",
            field="amount_minor",
        )
    if amount < 0:
        raise ApiError("invalid_amount", "amount_minor cannot be negative.", field="amount_minor")

    currency = payload["currency"]
    if len(currency) != 3 or not currency.isalpha():
        raise ApiError(
            "invalid_currency", "currency must be a 3-letter ISO code.", field="currency"
        )

    nested = {}
    for name, allowed in NESTED_FIELDS.items():
        value = payload.get(name, {})
        if not isinstance(value, dict):
            raise ApiError("invalid_field", f"{name} must be an object.", field=name)
        _reject_unknown(value, allowed, f"{name}.")
        nested[name] = value

    return Transaction(
        transaction_ref=payload["transaction_ref"],
        customer_ref=payload["customer_ref"],
        amount_minor=amount,
        currency=currency.upper(),
        transaction_type=payload.get("transaction_type", ""),
        channel=payload.get("channel", ""),
        customer=nested["customer"],
        destination=nested["destination"],
        device=nested["device"],
    )


def _reject_unknown(payload: dict[str, Any], allowed: set[str], prefix: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        name = f"{prefix}{unknown[0]}"
        raise ApiError(
            "unknown_field",
            f"{name} is not a field ComplyLayer accepts. Unknown fields are refused rather "
            "than ignored, so nothing is stored that was never meant to arrive.",
            field=name,
        )


def _require_text(payload: dict[str, Any], name: str) -> None:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ApiError("invalid_field", f"{name} must be a non-empty string.", field=name)
    if len(value) > 128:
        raise ApiError("invalid_field", f"{name} must be 128 characters or fewer.", field=name)
