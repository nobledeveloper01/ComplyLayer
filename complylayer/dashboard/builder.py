"""What the visual rule builder offers, and what it produces.

The builder's job is stated in one line in the roadmap and it is the whole
product: a compliance officer must be able to build every rule that matters
without dropping into the expression editor. If the editor is where the real
rules live, an engineer is back in the loop and the premise is false.

So the shapes here are not a convenience layer over the DSL. They are the
product's claim, and `tests/test_dashboard.py` asserts that each of the six
§4.4 examples is reachable through one of them.

Every shape renders to the structured JSON in `complylayer/dsl/structured.py`,
which renders to an expression, which goes through the same validator a typed
rule does. One code path — a built rule earns no trust a typed one would not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from complylayer.dsl import validate_source
from complylayer.dsl.structured import to_expression


@dataclass(frozen=True)
class Input:
    """One control in the builder form."""

    key: str
    label: str
    kind: str  # amount | number | text | window | fact | list
    hint: str = ""
    required: bool = True
    default: Any = None


@dataclass(frozen=True)
class Shape:
    """A rule pattern a compliance officer recognises without being taught it.

    `regulatory_reference` is prefilled rather than left blank. A rule that
    claims a regulation is a rule an approver can weigh; a rule with an empty
    reference is one they have to take on trust.
    """

    key: str
    name: str
    question: str
    category: str
    regulatory_reference: str
    inputs: tuple[Input, ...]
    severity: str = "flag"

    def build(self, values: dict[str, Any]) -> dict[str, Any]:
        return BUILDERS[self.key](values)


def _amount_limit(values: dict[str, Any]) -> dict[str, Any]:
    """More than a fixed amount, or more than a fact that holds the limit."""
    limit = values["limit"]
    comparand = {"fact": limit} if isinstance(limit, str) else limit
    return {"fact": values.get("subject", "amount_minor"), "op": ">", "value": comparand}


def _velocity_count(values: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {"window": values["window"]}
    if values.get("min_amount_minor"):
        args["min_amount_minor"] = values["min_amount_minor"]
    return {
        "call": "velocity_count",
        "args": args,
        "op": ">",
        "value": values["count"],
    }


def _structuring(values: dict[str, Any]) -> dict[str, Any]:
    """Repeated transactions sitting just below a reporting threshold.

    The percentage is a builder input rather than a constant because the
    threshold band is a supervisory judgement, not a number this product gets to
    pick on a customer's behalf.
    """
    threshold = values.get("threshold_fact", "reporting_threshold_minor")
    return {
        "call": "velocity_count",
        "args": {
            "window": values["window"],
            "min_amount_minor": {
                "call": "percent_of",
                "positional": [{"fact": threshold}, values.get("percent", 90)],
            },
            "max_amount_minor": {"fact": threshold},
        },
        "op": ">=",
        "value": values["count"],
    }


def _dormant_reactivation(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "all": [
            {
                "call": "days_since",
                "positional": [{"fact": values.get("since_fact", "last_transaction_at")}],
                "op": ">",
                "value": values["days"],
            },
            {"fact": "amount_minor", "op": ">", "value": values["amount_minor"]},
        ]
    }


def _corridor(values: dict[str, Any]) -> dict[str, Any]:
    """A high-risk corridor, optionally narrowed by tier and by hour."""
    conditions: list[dict[str, Any]] = [
        {
            "call": "in_list",
            "positional": [
                {"fact": "destination_country"},
                {"fact": values.get("list_name", "high_risk_countries")},
            ],
        }
    ]
    if values.get("max_kyc_tier"):
        conditions.append({"fact": "kyc_tier", "op": "<", "value": values["max_kyc_tier"]})
    if values.get("from_hour") is not None and values.get("to_hour") is not None:
        conditions.append(
            {
                "any": [
                    {"call": "hour_of_day", "op": "<", "value": values["from_hour"]},
                    {"call": "hour_of_day", "op": ">", "value": values["to_hour"]},
                ]
            }
        )
    return {"all": conditions}


def _new_account(values: dict[str, Any]) -> dict[str, Any]:
    return {
        "all": [
            {
                "call": "days_since",
                "positional": [{"fact": "account_created_at"}],
                "op": "<",
                "value": values["days"],
            },
            {"fact": "amount_minor", "op": ">", "value": values["amount_minor"]},
        ]
    }


BUILDERS = {
    "amount_limit": _amount_limit,
    "velocity_count": _velocity_count,
    "structuring": _structuring,
    "dormant_reactivation": _dormant_reactivation,
    "corridor": _corridor,
    "new_account": _new_account,
}

WINDOWS = ("1m", "5m", "15m", "1h", "6h", "24h", "7d", "30d")

SHAPES: tuple[Shape, ...] = (
    Shape(
        key="amount_limit",
        name="Transaction limit",
        question="Is this transaction larger than a limit?",
        category="kyc",
        regulatory_reference="CBN KYC tiering",
        severity="block",
        inputs=(
            Input("subject", "What to measure", "fact", default="amount_minor"),
            Input(
                "limit",
                "Limit",
                "amount",
                hint="A fixed amount, or the name of a fact holding the customer's limit.",
            ),
        ),
    ),
    Shape(
        key="velocity_count",
        name="Too many transactions",
        question="Has this customer made too many transactions in a period?",
        category="velocity",
        regulatory_reference="AML transaction monitoring",
        inputs=(
            Input("window", "Over what period", "window", default="1h"),
            Input("count", "More than how many", "number", default=5),
            Input(
                "min_amount_minor",
                "Counting only transactions above",
                "amount",
                required=False,
                hint="Leave blank to count every transaction.",
            ),
        ),
    ),
    Shape(
        key="structuring",
        name="Structuring",
        question="Is this customer splitting transactions to sit under a reporting threshold?",
        category="aml",
        regulatory_reference="NFIU reporting obligations",
        inputs=(
            Input("window", "Over what period", "window", default="24h"),
            Input("count", "At least how many", "number", default=3),
            Input(
                "percent",
                "Within what percentage below the threshold",
                "number",
                default=90,
                hint="90 means between 90% and 100% of the reporting threshold.",
            ),
            Input(
                "threshold_fact",
                "Threshold",
                "fact",
                default="reporting_threshold_minor",
                required=False,
            ),
        ),
    ),
    Shape(
        key="dormant_reactivation",
        name="Dormant account reactivated",
        question="Has a quiet account suddenly moved a large amount?",
        category="fraud",
        regulatory_reference="AML suspicious pattern monitoring",
        inputs=(
            Input("days", "Quiet for more than how many days", "number", default=90),
            Input("amount_minor", "And now moving more than", "amount"),
        ),
    ),
    Shape(
        key="corridor",
        name="High-risk corridor",
        question="Is this going somewhere risky, and does that matter more right now?",
        category="aml",
        regulatory_reference="AML high-risk jurisdiction monitoring",
        inputs=(
            Input("list_name", "Which list of countries", "list", default="high_risk_countries"),
            Input("max_kyc_tier", "Only for tiers below", "number", required=False, default=3),
            Input("from_hour", "Only before hour", "number", required=False),
            Input("to_hour", "And after hour", "number", required=False),
        ),
    ),
    Shape(
        key="new_account",
        name="Large first transaction",
        question="Is a brand-new account moving an unusually large amount?",
        category="fraud",
        regulatory_reference="AML onboarding risk",
        inputs=(
            Input("days", "Opened fewer than how many days ago", "number", default=7),
            Input("amount_minor", "And moving more than", "amount"),
        ),
    ),
)

SHAPES_BY_KEY = {shape.key: shape for shape in SHAPES}


@dataclass
class BuildResult:
    expression: str
    structured: dict[str, Any]
    valid: bool
    error: dict[str, Any] | None = field(default=None)


def build(shape_key: str, values: dict[str, Any]) -> BuildResult:
    """Turn builder inputs into a validated expression.

    Runs the real validator rather than trusting the shape. A shape is a
    convenience for a person, not a licence for the code — and the day somebody
    adds a shape with a bug, this is what catches it.
    """
    from complylayer.dsl import RuleSyntaxError

    shape = SHAPES_BY_KEY[shape_key]
    structured = shape.build(values)

    try:
        expression = to_expression(structured)
        validate_source(expression)
    except RuleSyntaxError as exc:
        return BuildResult(expression="", structured=structured, valid=False, error=exc.as_dict())

    return BuildResult(expression=expression, structured=structured, valid=True)
