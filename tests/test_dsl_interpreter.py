"""Evaluating validated rules against real transactions.

Two things get most of the attention here. First, that a rule means what a
compliance officer would think it means. Second, that the guards the validator
could not apply — because they depend on values only a live transaction supplies
— actually hold at run time.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from complylayer.dsl import functions, validate_source
from complylayer.dsl.errors import RuleEvaluationError
from complylayer.dsl.interpreter import MAX_RESULT_BITS, EvaluationContext, evaluate

NOW = datetime(2026, 8, 16, 14, 30, tzinfo=UTC)


class FakeVelocity:
    """An in-memory stand-in for the Redis-backed provider phase 3 supplies."""

    def __init__(self, transactions: list[tuple[int, str]] | None = None):
        self.transactions = transactions or []
        self.calls: list[tuple] = []

    def _matching(self, min_amount_minor, max_amount_minor, transaction_type):
        for amount, kind in self.transactions:
            if min_amount_minor is not None and amount < min_amount_minor:
                continue
            if max_amount_minor is not None and amount > max_amount_minor:
                continue
            if transaction_type is not None and kind != transaction_type:
                continue
            yield amount

    def count(self, window_seconds, **filters) -> int:
        self.calls.append(("count", window_seconds, filters))
        return sum(1 for _ in self._matching(**filters))

    def total(self, window_seconds, **filters) -> int:
        self.calls.append(("total", window_seconds, filters))
        return sum(self._matching(**filters))


def context(facts=None, velocity=None, now=NOW) -> EvaluationContext:
    return EvaluationContext(
        facts=facts or {},
        functions=functions.build(velocity or FakeVelocity(), now),
    )


def run(source: str, facts=None, velocity=None, now=NOW, max_steps=1000) -> bool:
    return evaluate(validate_source(source), context(facts, velocity, now), max_steps=max_steps)


class TestTheSpecificationExamples:
    """The six §4.4 rules, evaluated rather than merely parsed."""

    def test_kyc_tier_daily_limit(self):
        facts = {"amount_minor": 7_500_000, "tier_daily_limit_minor": 5_000_000}
        assert run("amount_minor > tier_daily_limit_minor", facts) is True
        facts["amount_minor"] = 4_000_000
        assert run("amount_minor > tier_daily_limit_minor", facts) is False

    def test_velocity_over_a_rolling_window(self):
        velocity = FakeVelocity([(60_000_000, "transfer")] * 6)
        source = "velocity_count(window='1h', min_amount_minor=50_000_000) > 5"
        assert run(source, velocity=velocity) is True

        velocity = FakeVelocity([(60_000_000, "transfer")] * 5)
        assert run(source, velocity=velocity) is False

    def test_structuring_just_below_a_threshold(self):
        """Three transactions between 90% and 100% of the reporting threshold."""
        velocity = FakeVelocity([(9_500_000, "transfer")] * 3)
        source = (
            "velocity_count(window='24h', "
            "min_amount_minor=percent_of(reporting_threshold_minor, 90), "
            "max_amount_minor=reporting_threshold_minor) >= 3"
        )
        assert run(source, {"reporting_threshold_minor": 10_000_000}, velocity) is True

    def test_structuring_ignores_transactions_well_below_the_threshold(self):
        velocity = FakeVelocity([(1_000_000, "transfer")] * 9)
        source = (
            "velocity_count(window='24h', "
            "min_amount_minor=percent_of(reporting_threshold_minor, 90), "
            "max_amount_minor=reporting_threshold_minor) >= 3"
        )
        assert run(source, {"reporting_threshold_minor": 10_000_000}, velocity) is False

    def test_dormant_account_reactivated(self):
        source = "days_since(last_transaction_at) > 90 and amount_minor > 20_000_000"
        assert run(
            source, {"last_transaction_at": "2026-01-01T00:00:00Z", "amount_minor": 25_000_000}
        )
        assert not run(
            source, {"last_transaction_at": "2026-08-10T00:00:00Z", "amount_minor": 25_000_000}
        )

    def test_high_risk_corridor_outside_business_hours(self):
        source = (
            "in_list(destination_country, high_risk_countries) "
            "and kyc_tier < 3 "
            "and (hour_of_day() < 6 or hour_of_day() > 22)"
        )
        facts = {"destination_country": "XX", "high_risk_countries": ("XX", "YY"), "kyc_tier": 2}
        assert run(source, facts, now=NOW.replace(hour=3)) is True
        assert run(source, facts, now=NOW.replace(hour=14)) is False

    def test_new_account_large_first_transaction(self):
        source = "days_since(account_created_at) < 7 and amount_minor > 10_000_000"
        assert run(
            source, {"account_created_at": "2026-08-14T00:00:00Z", "amount_minor": 12_000_000}
        )


class TestRuntimeGuardsTheValidatorCannotApply:
    """The validator sees constants. Only the interpreter sees facts."""

    def test_multiplying_a_text_fact_is_refused(self):
        """`'a' * 999999999` was caught at publish time. This is the same bomb
        assembled from facts, which no amount of static checking could see."""
        with pytest.raises(RuleEvaluationError) as exc:
            run("padding * repeat_count > 0", {"padding": "a", "repeat_count": 999_999_999})
        assert "whole numbers" in str(exc.value)

    def test_comparing_a_number_against_text_is_refused(self):
        """Unhandled, this is a TypeError traceback on the decision path."""
        with pytest.raises(RuleEvaluationError) as exc:
            run(
                "amount_minor > destination_country",
                {"amount_minor": 1, "destination_country": "NG"},
            )
        assert "cannot compare" in str(exc.value)

    def test_equality_across_types_is_allowed_and_simply_false(self):
        """`==` is a question with a sensible answer for unlike things; `>` is not."""
        assert (
            run(
                "amount_minor == destination_country",
                {"amount_minor": 1, "destination_country": "NG"},
            )
            is False
        )

    def test_text_can_be_ordered_against_text(self):
        assert run("country_a < country_b", {"country_a": "GH", "country_b": "NG"}) is True

    def test_a_chain_of_multiplications_cannot_build_a_giant_number(self):
        with pytest.raises(RuleEvaluationError) as exc:
            run("big * big * big * big > 0", {"big": 2**200})
        assert "arithmetic produced" in str(exc.value) or "limit" in str(exc.value)

    def test_a_normal_multiplication_is_fine(self):
        assert run("amount_minor * 2 > 100", {"amount_minor": 51}) is True

    def test_the_magnitude_guard_checks_before_multiplying_not_after(self):
        """Checked first because the chain doubles bit length each step, and the
        cost of computing the answer arrives before any check of it could."""
        with pytest.raises(RuleEvaluationError):
            run("big * big > 0", {"big": 2 ** (MAX_RESULT_BITS // 2 + 10)})

    def test_a_boolean_fact_does_not_take_part_in_arithmetic(self):
        """isinstance(True, int) is true in Python, which is not what a rule means."""
        with pytest.raises(RuleEvaluationError):
            run("is_flagged * 5 > 1", {"is_flagged": True})

    def test_dividing_by_a_zero_fact_is_a_rule_error_not_a_crash(self):
        with pytest.raises(RuleEvaluationError) as exc:
            run("amount_minor // divisor > 1", {"amount_minor": 10, "divisor": 0})
        assert "zero" in str(exc.value)

    def test_remainder_by_a_zero_fact(self):
        with pytest.raises(RuleEvaluationError):
            run("amount_minor % divisor > 1", {"amount_minor": 10, "divisor": 0})


class TestMissingFacts:
    def test_an_unknown_fact_raises_rather_than_defaulting(self):
        """Silently reading as "no match" would turn a broken control into an
        absent one, and nothing anywhere would report it."""
        with pytest.raises(RuleEvaluationError) as exc:
            run("mystery_fact > 5")
        assert "mystery_fact" in str(exc.value)

    def test_short_circuiting_can_avoid_touching_a_missing_fact(self):
        """`and` stops at the first false clause, so ordering matters and the
        author gets to control what is even looked at."""
        assert run("amount_minor > 100 and mystery_fact > 5", {"amount_minor": 1}) is False

    def test_or_short_circuits_the_same_way(self):
        assert run("amount_minor > 0 or mystery_fact > 5", {"amount_minor": 1}) is True


class TestStepBudget:
    def test_a_normal_rule_uses_very_few_steps(self):
        from complylayer.dsl.interpreter import Interpreter

        interpreter = Interpreter(context({"amount_minor": 1, "kyc_tier": 2}))
        interpreter.run(validate_source("amount_minor > 100 and kyc_tier < 3"))
        assert interpreter.steps < 20

    def test_the_budget_is_enforced(self):
        source = " and ".join(["amount_minor > 0"] * 30)
        with pytest.raises(RuleEvaluationError) as exc:
            run(source, {"amount_minor": 1}, max_steps=10)
        assert "steps" in str(exc.value)

    def test_short_circuiting_keeps_a_rule_inside_the_budget(self):
        """The cheap clause first means the expensive ones are never reached."""
        source = "amount_minor > 1_000_000 and " + " and ".join(["kyc_tier < 9"] * 30)
        assert run(source, {"amount_minor": 1, "kyc_tier": 2}, max_steps=10) is False


class TestFunctions:
    def test_percent_of_rounds_down(self):
        assert run("percent_of(amount_minor, 90) == 900", {"amount_minor": 1000}) is True
        # 999 * 90 // 100 == 899, not 899.1
        assert run("percent_of(amount_minor, 90) == 899", {"amount_minor": 999}) is True

    def test_percent_of_refuses_a_non_number(self):
        with pytest.raises(RuleEvaluationError):
            run("percent_of(country, 90) > 1", {"country": "NG"})

    def test_min_max_abs(self):
        facts = {"a": -5, "b": 3}
        assert run("abs(a) == 5", facts)
        assert run("min(a, b) == -5", facts)
        assert run("max(a, b) == 3", facts)

    def test_days_since_accepts_an_iso_timestamp(self):
        assert run("days_since(t) == 10", {"t": "2026-08-06T14:30:00+00:00"})

    def test_days_since_treats_a_naive_timestamp_as_utc(self):
        """Stated in the code rather than left to whichever server ran it."""
        assert run("days_since(t) == 10", {"t": "2026-08-06T14:30:00"})

    def test_days_since_rejects_something_that_is_not_a_date(self):
        with pytest.raises(RuleEvaluationError) as exc:
            run("days_since(t) > 1", {"t": "not a date"})
        assert "not a date" in str(exc.value)

    def test_days_since_rejects_a_number(self):
        with pytest.raises(RuleEvaluationError):
            run("days_since(t) > 1", {"t": 12345})

    def test_an_unsupported_window_names_the_supported_ones(self):
        with pytest.raises(RuleEvaluationError) as exc:
            run("velocity_count(window='2h') > 1")
        assert "1h" in str(exc.value) and "24h" in str(exc.value)

    def test_in_list_against_an_inline_list(self):
        assert run("in_list(country, ['NG', 'GH'])", {"country": "GH"}) is True
        assert run("in_list(country, ['NG', 'GH'])", {"country": "KE"}) is False

    def test_in_list_needs_something_to_look_in(self):
        with pytest.raises(RuleEvaluationError):
            run("in_list(country, limit)", {"country": "NG", "limit": 5})

    def test_the_in_operator_works_on_a_fact_list(self):
        facts = {"country": "NG", "high_risk_countries": ("NG", "XX")}
        assert run("country in high_risk_countries", facts) is True
        assert run("country not in high_risk_countries", facts) is False

    def test_in_against_a_number_is_refused(self):
        with pytest.raises(RuleEvaluationError):
            run("country in limit", {"country": "NG", "limit": 5})

    def test_hour_of_day_uses_the_supplied_instant(self):
        """Not the wall clock: two calls in one evaluation must agree, and a
        replay for an audit has to reproduce the original instant."""
        assert run("hour_of_day() == 3", now=NOW.replace(hour=3)) is True


class TestOperators:
    @pytest.mark.parametrize(
        "source,expected",
        [
            ("a == 5", True),
            ("a != 5", False),
            ("a > 4", True),
            ("a >= 5", True),
            ("a < 6", True),
            ("a <= 5", True),
            ("not (a == 5)", False),
            ("-a < 0", True),
            ("a + 1 == 6", True),
            ("a - 1 == 4", True),
            ("a * 2 == 10", True),
            ("a % 2 == 1", True),
            ("a // 2 == 2", True),
        ],
    )
    def test_each_operator(self, source: str, expected: bool):
        assert run(source, {"a": 5}) is expected

    def test_a_chained_comparison(self):
        assert run("1 < a < 10", {"a": 5}) is True
        assert run("1 < a < 3", {"a": 5}) is False

    def test_negating_a_text_fact_is_refused(self):
        with pytest.raises(RuleEvaluationError):
            run("-country < 0", {"country": "NG"})


class TestRemainingPaths:
    """Branches that a normal rule never reaches, tested because they are the
    ones that would be wrong without anyone noticing."""

    def test_velocity_sum_reaches_the_provider(self):
        velocity = FakeVelocity([(100, "transfer"), (250, "transfer")])
        assert run("velocity_sum(window='24h') == 350", velocity=velocity) is True
        assert (
            "total",
            86_400,
            {"min_amount_minor": None, "max_amount_minor": None, "transaction_type": None},
        ) in velocity.calls

    def test_days_since_accepts_a_datetime_object_as_well_as_a_string(self):
        """Facts arrive as JSON today, but a fact provider handing over a real
        datetime should not be a failure."""
        facts = {"t": datetime(2026, 8, 6, 14, 30, tzinfo=UTC)}
        assert run("days_since(t) == 10", facts) is True

    def test_a_right_hand_text_operand_is_refused(self):
        """The left operand is checked first, so this covers the other side."""
        with pytest.raises(RuleEvaluationError) as exc:
            run("amount_minor * suffix > 0", {"amount_minor": 2, "suffix": "x"})
        assert "whole numbers" in str(exc.value)

    def test_addition_cannot_exceed_the_magnitude_ceiling(self):
        """Multiplication is guarded before the operation; addition after it,
        because adding cannot explode the way a chain of products can."""
        big = 2 ** (MAX_RESULT_BITS - 1)
        with pytest.raises(RuleEvaluationError) as exc:
            run("big + big > 0", {"big": big})
        assert "limit" in str(exc.value) or "produced" in str(exc.value)

    def test_calling_a_function_that_is_not_bound_in_this_context(self):
        """The validator allows the name; a context that did not bind an
        implementation is a wiring bug, and it should say so rather than
        resolve to something unexpected."""
        from complylayer.dsl.interpreter import EvaluationContext, evaluate

        bare = EvaluationContext(facts={}, functions={})
        with pytest.raises(RuleEvaluationError) as exc:
            evaluate(validate_source("hour_of_day() > 1"), bare)
        assert "not available" in str(exc.value)

    def test_a_node_the_validator_would_have_rejected(self):
        """Unreachable through validate_source. Tested because 'the validator
        would have caught it' is an assumption, and this is the one place where
        being wrong would mean evaluating something unexamined."""
        import ast as ast_module

        from complylayer.dsl.interpreter import EvaluationContext, evaluate

        tree = ast_module.Expression(body=ast_module.Set(elts=[ast_module.Constant(1)]))
        ast_module.fix_missing_locations(tree)
        with pytest.raises(RuleEvaluationError) as exc:
            evaluate(tree, EvaluationContext(facts={}, functions={}))
        assert "cannot evaluate" in str(exc.value)
