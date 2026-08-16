"""Resource limits on rule parsing.

The specification caps node count and evaluation steps, and enforces both after
``ast.parse`` returns. But ``ast.parse`` is itself the exposed surface: deeply
nested parentheses or unary operators drive CPython's parser into the C stack,
and a 200-node ceiling protects nothing from a parser that has already been
handed fifty thousand open brackets.

So the guards run in order — length, then nesting, then parse, then node count.
These tests fix that order in place.
"""

from __future__ import annotations

import pytest

from complylayer.dsl import RuleSyntaxError, limits, validate_source


class TestSourceLength:
    def test_a_reasonable_rule_is_fine(self):
        source = "velocity_count(window='1h', min_amount_minor=50_000_000) > 5"
        assert len(source) < limits.MAX_SOURCE_CHARS
        validate_source(source)

    def test_absurdly_long_source_is_refused_before_parsing(self):
        source = "amount_minor > 1 and " * 5000 + "amount_minor > 1"
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source(source)
        assert "long" in str(exc.value).lower()

    def test_the_limit_is_a_character_count_not_a_line_count(self):
        """One very long line is the shape this actually arrives in."""
        source = "1" * (limits.MAX_SOURCE_CHARS + 1)
        with pytest.raises(RuleSyntaxError):
            validate_source(source)


class TestNestingDepth:
    def test_ordinary_nesting_is_fine(self):
        validate_source("((amount_minor > 5) and (kyc_tier < 3)) or (amount_minor > 100)")

    def test_deep_bracket_nesting_is_refused_before_parsing(self):
        """The construct that reaches the C stack. It must not reach the parser at all.

        Kept short on purpose: a ten-thousand-bracket source would trip the
        length guard first, which is correct but tests the wrong thing. Fifty
        brackets is well inside the length limit and well past the depth limit,
        so only the nesting guard can be what rejects it.
        """
        source = "(" * 50 + "1" + ")" * 50
        assert len(source) < limits.MAX_SOURCE_CHARS
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source(source)
        assert "nest" in str(exc.value).lower() or "deep" in str(exc.value).lower()

    def test_unbalanced_deep_nesting_is_also_refused(self):
        """Unbalanced input still costs the tokenizer, so the guard runs first regardless."""
        with pytest.raises(RuleSyntaxError):
            validate_source("(" * 10_000)

    def test_long_runs_of_unary_operators_are_refused(self):
        """`not not not ...` and `---...` recurse in the parser just as brackets do."""
        with pytest.raises(RuleSyntaxError):
            validate_source("-" * 500 + "amount_minor")

    def test_a_few_unary_operators_are_fine(self):
        validate_source("not (amount_minor > 5)")
        validate_source("-amount_minor < 0")


class TestNodeCount:
    def test_a_realistic_rule_is_well_inside_the_ceiling(self):
        source = (
            "in_list(destination_country, high_risk_countries) "
            "and kyc_tier < 3 "
            "and (hour_of_day < 6 or hour_of_day > 22)"
        )
        validate_source(source)

    def test_an_expression_with_too_many_nodes_is_refused(self):
        """Valid, shallow, short — and still too big.

        Single-character names keep this comfortably inside the length limit, so
        the node ceiling is provably the guard doing the work. This is the case
        the textual guards cannot catch: nothing about the source looks hostile.
        """
        source = "+".join("abcdefghij" * 7) + ">0"
        assert len(source) < limits.MAX_SOURCE_CHARS
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source(source)
        assert "complex" in str(exc.value).lower() or "large" in str(exc.value).lower()


class TestGuardOrder:
    def test_length_is_checked_before_nesting(self):
        """A source that violates both should report length, the cheaper and more obvious fault."""
        source = "(" * (limits.MAX_SOURCE_CHARS + 100)
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source(source)
        assert "long" in str(exc.value).lower()

    def test_pathological_input_returns_quickly(self):
        """The guards exist to bound work, so this must not take measurable time.

        Anything slow here means the input reached the parser, which is the whole
        thing being prevented.
        """
        import time

        source = "(" * 100_000
        start = time.perf_counter()
        with pytest.raises(RuleSyntaxError):
            validate_source(source)
        assert time.perf_counter() - start < 0.1


class TestEmptyAndMalformed:
    @pytest.mark.parametrize("source", ["", "   ", "\n", "\t"])
    def test_empty_source_is_refused_clearly(self, source: str):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source(source)
        assert "empty" in str(exc.value).lower()

    @pytest.mark.parametrize(
        "source",
        ["amount_minor >", "> 5", "and and", "velocity_count(", "amount_minor > 5)"],
    )
    def test_syntax_errors_are_reported_as_rule_errors(self, source: str):
        """A compliance officer must never see a raw Python SyntaxError."""
        with pytest.raises(RuleSyntaxError):
            validate_source(source)
