"""The other half of the boundary: what a rule *may* say.

A validator that rejects everything is trivially secure and useless. These tests
fix the floor — every rule the specification advertises must validate, forever.
If a hardening change breaks one of these, the hardening went too far and the
product lost a capability it was sold on.
"""

from __future__ import annotations

import ast

import pytest

from complylayer.dsl import ALLOWED_NODES, RuleSyntaxError, functions, validate_source

# The six worked examples from §4.4, verbatim. These are the product's own
# advertisement of what a rule looks like.
SPEC_EXAMPLES = {
    "KYC tier daily limit": "amount_minor > tier_daily_limit_minor",
    "velocity": "velocity_count(window='1h', min_amount_minor=50_000_000) > 5",
    "structuring": (
        "velocity_count(window='24h', "
        "min_amount_minor=percent_of(reporting_threshold_minor, 90), "
        "max_amount_minor=reporting_threshold_minor) >= 3"
    ),
    "dormant reactivation": "days_since(last_transaction_at) > 90 and amount_minor > 20_000_000",
    "high-risk corridor": (
        "in_list(destination_country, high_risk_countries) "
        "and kyc_tier < 3 "
        "and (hour_of_day < 6 or hour_of_day > 22)"
    ),
    "new account": "days_since(account_created_at) < 7 and amount_minor > 10_000_000",
}


@pytest.mark.parametrize("name,source", SPEC_EXAMPLES.items(), ids=SPEC_EXAMPLES.keys())
def test_every_specification_example_validates(name: str, source: str):
    assert validate_source(source) is not None


class TestOrdinaryRules:
    @pytest.mark.parametrize(
        "source",
        [
            "amount_minor > 5_000_000",
            "amount_minor >= 5_000_000 and kyc_tier <= 2",
            "kyc_tier == 1 or kyc_tier == 2",
            "kyc_tier != 3",
            "not (kyc_tier == 3)",
            "destination_country in high_risk_countries",
            "destination_country not in safe_countries",
            "in_list(destination_country, ['NG', 'GH', 'KE'])",
            "amount_minor > velocity_sum(window='24h')",
            "velocity_count(window='7d') > 20",
            "abs(amount_minor - average_amount_minor) > 10_000_000",
            "min(amount_minor, balance_minor) > 1_000",
            "max(amount_minor, 1) > 0",
            "percent_of(balance_minor, 90) < amount_minor",
            "amount_minor * 2 > balance_minor",
            "amount_minor + fee_minor > balance_minor",
            "amount_minor - fee_minor > 0",
            "amount_minor % 100 == 0",
            "amount_minor // 100 > 5",
            "-amount_minor < 0",
            "hour_of_day() < 6",
        ],
    )
    def test_validates(self, source: str):
        validate_source(source)


class TestTheGrammarIsWhatItClaims:
    def test_division_is_not_in_the_allowlist(self):
        """D6: division produces floats, and floats sit badly with 100% reproducibility.

        Asserted by reading the set rather than by trying an expression, so the
        guarantee cannot be quietly removed while the tests stay green.
        """
        assert ast.Div not in ALLOWED_NODES

    def test_division_therefore_fails_with_a_readable_message(self):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source("amount_minor / 2 > 100")
        assert "Div" in str(exc.value) or "cannot" in str(exc.value).lower()

    def test_floor_division_is_the_supported_alternative(self):
        validate_source("amount_minor // 2 > 100")

    @pytest.mark.parametrize("node", [ast.Attribute, ast.Subscript, ast.Lambda, ast.JoinedStr])
    def test_the_dangerous_constructs_are_absent_from_the_allowlist(self, node):
        assert node not in ALLOWED_NODES

    def test_every_allowlisted_node_is_an_ast_type(self):
        assert all(isinstance(node, type) and issubclass(node, ast.AST) for node in ALLOWED_NODES)


class TestErrorsAreWrittenForACompliancOfficer:
    def test_the_dot_error_says_what_to_write_instead(self):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source("customer.kyc_tier > 2")
        error = exc.value
        assert "dot" in error.problem.lower()
        assert "kyc_tier" in error.fix
        assert error.reason

    def test_it_quotes_back_what_they_wrote(self):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source("customer.kyc_tier > 2")
        assert "customer.kyc_tier" in str(exc.value)

    def test_a_mistyped_function_gets_a_suggestion(self):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source("velocity_cout(window='1h') > 5")
        assert "velocity_count" in exc.value.fix

    def test_an_unrecognisable_function_gets_the_list_instead_of_a_bad_guess(self):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source("zzzzz(1) > 5")
        assert "velocity_count" in exc.value.fix
        assert "Did you mean" not in exc.value.fix

    def test_a_wrong_keyword_names_the_ones_that_exist(self):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source("velocity_count(windwo='1h') > 5")
        assert "window" in exc.value.fix

    def test_every_error_carries_a_problem_and_a_fix(self):
        """The catalogue's contract: never a bare complaint."""
        rejected = [
            "customer.kyc_tier",
            "high_risk_countries[0]",
            "(lambda: 1)()",
            "f'{amount_minor}'",
            "zzzzz(1)",
            "",
            "amount_minor >",
            "__builtins__",
        ]
        for source in rejected:
            with pytest.raises(RuleSyntaxError) as exc:
                validate_source(source)
            assert exc.value.problem, f"no problem stated for {source!r}"
            assert exc.value.fix, f"no fix offered for {source!r}"

    def test_errors_serialise_for_the_api(self):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source("customer.kyc_tier")
        payload = exc.value.as_dict()
        assert set(payload) == {"problem", "fix", "reason", "offset"}

    def test_no_error_message_leaks_python_vocabulary_at_the_reader(self):
        """A traceback or an AST class name reaching a compliance officer is a bug."""
        for source in ["customer.kyc_tier", "high_risk_countries[0]", "amount_minor >"]:
            with pytest.raises(RuleSyntaxError) as exc:
                validate_source(source)
            assert "Traceback" not in str(exc.value)
            assert "ast." not in str(exc.value)


class TestFunctionReference:
    def test_every_allowed_function_has_a_spec(self):
        assert set(functions.ALLOWED_FUNCTIONS) == set(functions.SPECS)

    def test_the_reference_lists_every_function(self):
        text = functions.reference()
        for name in functions.ALLOWED_FUNCTIONS:
            assert name in text

    def test_percent_of_is_documented_as_rounding_down(self):
        """D6 made this integer-only, so the direction has to be written down somewhere."""
        assert "down" in functions.SPECS["percent_of"].summary.lower()


class TestMessageAssembly:
    def test_a_problem_on_its_own_renders_as_just_the_problem(self):
        """Not every rejection needs three sentences, and none should get stray punctuation."""
        error = RuleSyntaxError(problem="The rule is empty.")
        assert str(error) == "The rule is empty."
        assert error.fix == ""

    def test_the_reason_is_parenthesised_when_present(self):
        error = RuleSyntaxError(problem="No.", fix="Do this.", reason="because")
        assert str(error) == "No. Do this. (because)"


class TestStatementsGetTheSpecificMessage:
    @pytest.mark.parametrize(
        "source", ["import os", "x = 1", "def f(): pass", "amount_minor > 5; kyc_tier > 1"]
    )
    def test_valid_python_that_is_not_an_expression(self, source: str):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source(source)
        assert "single question" in exc.value.problem

    @pytest.mark.parametrize("source", ["amount_minor >", "> 5", "velocity_count("])
    def test_input_that_is_not_valid_python_at_all(self, source: str):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source(source)
        assert "not written correctly" in exc.value.problem


class TestUnaryRunGuard:
    def test_a_long_run_of_not_is_refused(self):
        """`not` recurses in the parser exactly as `-` does, and is easier to type by accident."""
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source("not " * 50 + "amount_minor")
        assert "row" in str(exc.value).lower()

    def test_a_short_run_of_not_is_fine(self):
        validate_source("not not amount_minor > 5")
