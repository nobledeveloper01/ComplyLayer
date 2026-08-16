"""The visual builder's grammar must reach every rule the product advertises.

This is the phase 1 exit criterion that decides whether the product's premise
holds. If a compliance officer cannot build the six §4.4 rules without dropping
into the expression editor, then an engineer is still in the loop and the pitch
is false.

Tested here rather than in phase 6 because the builder's expressiveness and the
function set are one decision, and freezing the function set first would leave
the builder to fit whatever grammar it happened to get.
"""

from __future__ import annotations

import pytest

from complylayer.dsl import RuleSyntaxError, validate_source
from complylayer.dsl.structured import to_expression

# The six from §4.4, as a builder would emit them.
SPEC_RULES = {
    "KYC tier daily limit": (
        {"fact": "amount_minor", "op": ">", "value": {"fact": "tier_daily_limit_minor"}},
        "amount_minor > tier_daily_limit_minor",
    ),
    "velocity": (
        {
            "call": "velocity_count",
            "args": {"window": "1h", "min_amount_minor": 50_000_000},
            "op": ">",
            "value": 5,
        },
        "velocity_count(window='1h', min_amount_minor=50000000) > 5",
    ),
    "structuring": (
        {
            "call": "velocity_count",
            "args": {
                "window": "24h",
                "min_amount_minor": {
                    "call": "percent_of",
                    "positional": [{"fact": "reporting_threshold_minor"}, 90],
                },
                "max_amount_minor": {"fact": "reporting_threshold_minor"},
            },
            "op": ">=",
            "value": 3,
        },
        "velocity_count(window='24h', "
        "min_amount_minor=percent_of(reporting_threshold_minor, 90), "
        "max_amount_minor=reporting_threshold_minor) >= 3",
    ),
    "dormant reactivation": (
        {
            "all": [
                {
                    "call": "days_since",
                    "positional": [{"fact": "last_transaction_at"}],
                    "op": ">",
                    "value": 90,
                },
                {"fact": "amount_minor", "op": ">", "value": 20_000_000},
            ]
        },
        "days_since(last_transaction_at) > 90 and amount_minor > 20000000",
    ),
    "high-risk corridor": (
        {
            "all": [
                {
                    "call": "in_list",
                    "positional": [
                        {"fact": "destination_country"},
                        {"fact": "high_risk_countries"},
                    ],
                },
                {"fact": "kyc_tier", "op": "<", "value": 3},
                {
                    "any": [
                        {"call": "hour_of_day", "op": "<", "value": 6},
                        {"call": "hour_of_day", "op": ">", "value": 22},
                    ]
                },
            ]
        },
        "in_list(destination_country, high_risk_countries) and kyc_tier < 3 "
        "and (hour_of_day() < 6 or hour_of_day() > 22)",
    ),
    "new account": (
        {
            "all": [
                {
                    "call": "days_since",
                    "positional": [{"fact": "account_created_at"}],
                    "op": "<",
                    "value": 7,
                },
                {"fact": "amount_minor", "op": ">", "value": 10_000_000},
            ]
        },
        "days_since(account_created_at) < 7 and amount_minor > 10000000",
    ),
}


@pytest.mark.parametrize("name", SPEC_RULES, ids=list(SPEC_RULES))
def test_the_builder_can_express_every_specification_rule(name: str):
    """The exit criterion. If this fails, the product's premise fails with it."""
    structured, expected = SPEC_RULES[name]
    assert to_expression(structured) == expected


@pytest.mark.parametrize("name", SPEC_RULES, ids=list(SPEC_RULES))
def test_everything_the_builder_emits_passes_the_same_validator(name: str):
    """One code path. A built rule earns no trust a typed rule would not."""
    structured, _ = SPEC_RULES[name]
    validate_source(to_expression(structured))


class TestGrammar:
    def test_a_single_condition_needs_no_brackets(self):
        assert to_expression({"all": [{"fact": "kyc_tier", "op": "==", "value": 1}]}) == (
            "kyc_tier == 1"
        )

    def test_nested_groups_are_bracketed_but_the_top_level_is_not(self):
        source = to_expression(
            {
                "all": [
                    {"fact": "a", "op": ">", "value": 1},
                    {
                        "any": [
                            {"fact": "b", "op": ">", "value": 2},
                            {"fact": "c", "op": ">", "value": 3},
                        ]
                    },
                ]
            }
        )
        assert source == "a > 1 and (b > 2 or c > 3)"

    def test_negation(self):
        assert to_expression({"not": {"fact": "kyc_tier", "op": "==", "value": 3}}) == (
            "not (kyc_tier == 3)"
        )

    def test_membership(self):
        assert (
            to_expression({"fact": "destination_country", "op": "in", "value": ["NG", "GH"]})
            == "destination_country in ['NG', 'GH']"
        )

    def test_a_bare_call_is_a_truth_test(self):
        assert to_expression({"call": "hour_of_day"}) == "hour_of_day()"


class TestTheBuilderIsTreatedAsUntrusted:
    """A builder is a client, and a client is something an attacker can also be."""

    def test_a_fact_name_cannot_smuggle_an_expression(self):
        with pytest.raises(RuleSyntaxError):
            to_expression({"fact": "amount_minor > 0 or ().__class__", "op": ">", "value": 1})

    def test_a_function_name_cannot_smuggle_an_expression(self):
        with pytest.raises(RuleSyntaxError):
            to_expression({"call": "velocity_count(1) or eval"})

    def test_a_text_value_cannot_close_its_own_quote(self):
        with pytest.raises(RuleSyntaxError):
            to_expression({"fact": "country", "op": "==", "value": "NG' or '1'=='1"})

    def test_a_text_value_cannot_contain_a_backslash(self):
        with pytest.raises(RuleSyntaxError):
            to_expression({"fact": "country", "op": "==", "value": "NG\\'"})

    def test_reserved_names_are_refused_here_too(self):
        with pytest.raises(RuleSyntaxError):
            to_expression({"fact": "__builtins__"})

    def test_even_if_a_name_slipped_through_the_validator_would_still_catch_it(self):
        """Defence in depth, stated as a test so the second layer is not assumed."""
        with pytest.raises(RuleSyntaxError):
            validate_source("amount_minor > 0 or ().__class__")


class TestMalformedInput:
    @pytest.mark.parametrize(
        "structured",
        [
            "not an object",
            {},
            {"all": []},
            {"all": "not a list"},
            {"fact": "a", "op": "~~", "value": 1},
            {"fact": "a", "op": ">"},
            {"call": "velocity_count", "args": "not an object"},
            {"call": "velocity_count", "positional": "not a list"},
            {"fact": "a", "op": ">", "value": {"neither": 1}},
            {"fact": "a", "op": ">", "value": {1, 2}},
        ],
    )
    def test_is_refused_with_a_rule_error(self, structured):
        with pytest.raises(RuleSyntaxError):
            to_expression(structured)

    def test_floats_are_refused_because_amounts_are_minor_units(self):
        with pytest.raises(RuleSyntaxError) as exc:
            to_expression({"fact": "amount_minor", "op": ">", "value": 1.5})
        assert "minor units" in str(exc.value)

    def test_booleans_are_refused(self):
        with pytest.raises(RuleSyntaxError):
            to_expression({"fact": "flag", "op": "==", "value": True})
