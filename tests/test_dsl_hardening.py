"""The edges of the hardening added after the phase 1 security review.

`test_dsl_escapes.py` proves the reported payloads are refused. This file covers
the boundaries around those fixes: the cases just inside the limits, the ones
just outside, and the guards that are only reachable by a caller other than
`validate_source`.
"""

from __future__ import annotations

import ast

import pytest

from complylayer.dsl import RuleSyntaxError, limits, validate, validate_source
from complylayer.dsl.structured import MAX_STRUCTURED_DEPTH, to_expression
from complylayer.dsl.validator import MAX_NUMBER_DIGITS


class TestWordBoundaryScanning:
    """`not` is matched as a word, so names that merely start with it are fine."""

    @pytest.mark.parametrize(
        "source",
        ["nothing > 5", "notes_count > 5", "notional_minor > 5", "note > 5"],
    )
    def test_names_beginning_with_not_are_not_operators(self, source: str):
        validate_source(source)

    def test_not_followed_by_a_bracket_still_counts_as_an_operator(self):
        validate_source("not (kyc_tier == 3)")


class TestNumberSize:
    def test_a_realistic_amount_is_fine(self):
        """₦10bn in kobo is 12 digits. The ceiling is nowhere near real money."""
        validate_source("amount_minor > 1_000_000_000_000")

    def test_a_number_at_the_ceiling_is_accepted(self):
        validate_source(f"amount_minor > {'9' * MAX_NUMBER_DIGITS}")

    def test_a_number_past_the_ceiling_is_refused(self):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source(f"amount_minor > {'9' * (MAX_NUMBER_DIGITS + 1)}")
        assert "digits" in str(exc.value)


class TestSnapshotRevalidation:
    """`validate` is also called on trees that did not come from `validate_source`.

    Phase 4 re-runs the allowlist when a pod loads a frozen rule set, because a
    snapshot is data in a database and a database is something an attacker who
    has got that far can edit. So the validator's own checks have to hold
    against a tree built directly, not only against one the source scan already
    filtered.
    """

    def test_a_non_ascii_name_in_a_hand_built_tree_is_refused(self):
        tree = ast.Expression(
            body=ast.Compare(
                # U+0430 CYRILLIC SMALL LETTER A, built by codepoint so the
                # source of this test is not itself ambiguous.
                left=ast.Name(id="\u0430mount_minor", ctx=ast.Load()),
                ops=[ast.Gt()],
                comparators=[ast.Constant(value=5)],
            )
        )
        ast.fix_missing_locations(tree)
        with pytest.raises(RuleSyntaxError) as exc:
            validate(tree)
        assert "plain English" in str(exc.value)

    def test_a_reserved_name_in_a_hand_built_tree_is_refused(self):
        tree = ast.Expression(body=ast.Name(id="__builtins__", ctx=ast.Load()))
        ast.fix_missing_locations(tree)
        with pytest.raises(RuleSyntaxError):
            validate(tree)

    def test_a_valid_hand_built_tree_passes(self):
        tree = ast.Expression(
            body=ast.Compare(
                left=ast.Name(id="amount_minor", ctx=ast.Load()),
                ops=[ast.Gt()],
                comparators=[ast.Constant(value=5)],
            )
        )
        ast.fix_missing_locations(tree)
        assert validate(tree) is tree


class TestBuilderDepth:
    """Every recursive entry point in the renderer carries the same cap.

    They are separate checks rather than one, because the recursion has four
    mutually-recursive doors — group, call, value and literal — and a cap on
    only the door you thought of is a cap on nothing.
    """

    def test_nested_groups(self):
        node: dict = {"fact": "a", "op": ">", "value": 1}
        for _ in range(MAX_STRUCTURED_DEPTH + 5):
            node = {"all": [node]}
        with pytest.raises(RuleSyntaxError):
            to_expression(node)

    def test_nested_negations(self):
        node: dict = {"fact": "a", "op": ">", "value": 1}
        for _ in range(MAX_STRUCTURED_DEPTH + 5):
            node = {"not": node}
        with pytest.raises(RuleSyntaxError):
            to_expression(node)

    def test_nested_calls_in_arguments(self):
        node: dict = {"fact": "a"}
        for _ in range(MAX_STRUCTURED_DEPTH + 5):
            node = {"call": "percent_of", "positional": [node, 90]}
        with pytest.raises(RuleSyntaxError):
            to_expression(
                {"call": "velocity_count", "args": {"window": node}, "op": ">", "value": 1}
            )

    def test_nested_values(self):
        node: dict = {"fact": "a"}
        for _ in range(MAX_STRUCTURED_DEPTH + 5):
            node = {"call": "abs", "positional": [node]}
        with pytest.raises(RuleSyntaxError):
            to_expression({"fact": "b", "op": ">", "value": node})

    def test_nested_lists(self):
        value: list = [1]
        for _ in range(MAX_STRUCTURED_DEPTH + 5):
            value = [value]
        with pytest.raises(RuleSyntaxError):
            to_expression({"fact": "a", "op": "in", "value": value})

    def test_a_realistic_nesting_depth_is_fine(self):
        """Three levels is more than any of the §4.4 rules needs."""
        to_expression(
            {
                "all": [
                    {"fact": "a", "op": ">", "value": 1},
                    {"any": [{"not": {"fact": "b", "op": "<", "value": 2}}]},
                ]
            }
        )


class TestBuilderOutputSize:
    def test_a_wide_but_shallow_rule_is_capped_on_what_it_renders(self):
        """Depth is not the only way to be too big.

        A flat list of thousands of conditions never recurses, so the depth cap
        never fires — but it rendered a 420 KB expression before the source
        length guard got a look at it.
        """
        node = {"all": [{"fact": "amount_minor", "op": ">", "value": 12345678} for _ in range(200)]}
        with pytest.raises(RuleSyntaxError) as exc:
            to_expression(node)
        assert "too many parts" in str(exc.value) or "long" in str(exc.value).lower()

    def test_a_normal_rule_renders(self):
        node = {"all": [{"fact": "amount_minor", "op": ">", "value": 1} for _ in range(5)]}
        assert to_expression(node).count("and") == 4


class TestBuilderInputTypes:
    @pytest.mark.parametrize("name", [123, None, ["a"], {"a": 1}, 1.5])
    def test_a_name_must_be_text(self, name):
        with pytest.raises(RuleSyntaxError):
            to_expression({"fact": name, "op": ">", "value": 1})

    def test_an_enormous_integer_is_refused_without_stringifying_it(self):
        """The first version of this guard measured the number with str(abs(value)).

        That is the same conversion CPython refuses past 4,300 digits, so the
        check raised the ValueError it existed to prevent. The digit count is
        estimated from the bit length instead.
        """
        with pytest.raises(RuleSyntaxError) as exc:
            to_expression({"fact": "a", "op": ">", "value": 10**5000})
        assert "digits" in str(exc.value)

    def test_a_large_but_reasonable_integer_is_fine(self):
        to_expression({"fact": "a", "op": ">", "value": 10**15})


class TestArityMessages:
    def test_a_function_taking_no_arguments_says_so(self):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source("hour_of_day(1) > 5")
        assert "nothing in the brackets" in exc.value.fix

    def test_a_function_taking_arguments_shows_the_shape(self):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source("percent_of(1) > 5")
        assert "percent_of(" in exc.value.fix


class TestStringAwareScanning:
    """The scan skips string literals, so their contents neither inflate nor deflate depth."""

    def test_brackets_inside_a_text_value_do_not_count_towards_depth(self):
        """Before the fix this was a false rejection as well as a bypass."""
        validate_source("destination_country == '((((((((((((((((((((((((('")

    def test_a_quote_escaped_inside_a_string_does_not_end_it(self):
        validate_source(r"destination_country == 'it\'s fine' and kyc_tier < 3")

    def test_a_comment_is_skipped(self):
        validate_source("amount_minor > 5  # ))))))))))))))))))))))))))))")

    def test_an_unterminated_string_is_a_rule_error_not_a_crash(self):
        with pytest.raises(RuleSyntaxError):
            validate_source("destination_country == 'unterminated")

    def test_real_nesting_is_still_counted_when_a_string_is_present(self):
        source = (
            "'text' == 'text' and "
            + "(" * (limits.MAX_NESTING_DEPTH + 1)
            + "1"
            + ")" * (limits.MAX_NESTING_DEPTH + 1)
        )
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source(source)
        assert "nest" in str(exc.value).lower()


class TestRendererDepthContract:
    def test_render_call_guards_its_own_depth(self):
        """Tested directly because no current caller can reach it.

        Every path into `_render_call` checks the depth first, so this guard is
        unreachable through `to_expression` today. It stays because the renderer
        is mutually recursive through four functions, and the next person to add
        a fifth entry point should inherit the cap rather than have to notice it.
        """
        from complylayer.dsl.structured import _render_call

        with pytest.raises(RuleSyntaxError):
            _render_call({"call": "abs", "positional": [1]}, depth=MAX_STRUCTURED_DEPTH + 1)

    def test_render_call_works_within_the_cap(self):
        from complylayer.dsl.structured import _render_call

        assert _render_call({"call": "abs", "positional": [1]}, depth=0) == "abs(1)"
