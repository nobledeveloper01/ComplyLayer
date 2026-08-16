"""Fuzzing the validator.

The corpus proves that the escapes we know about are rejected. This proves the
thing the corpus cannot: that no input at all — structured, malformed, hostile
or absurd — gets the validator to fail in some way other than a clean
``RuleSyntaxError``.

The property is deliberately narrow and absolute. For any input whatsoever,
``validate_source`` either returns a validated tree or raises ``RuleSyntaxError``.
Anything else — a ``SyntaxError`` escaping, a ``RecursionError``, a ``TypeError``
from an unexpected node shape — is a hole, because every one of those would
reach a compliance officer as a traceback and would mean a code path nobody
considered.
"""

from __future__ import annotations

import random
import string

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from complylayer.dsl import RuleSyntaxError, validate_source

# How many generated expressions the deterministic sweep runs. The roadmap's
# phase 1 gate names this number.
SWEEP_SIZE = 100_000

FACTS = [
    "amount_minor",
    "kyc_tier",
    "balance_minor",
    "destination_country",
    "high_risk_countries",
    "account_created_at",
    "hour_of_day",
]
FUNCS = ["velocity_count", "velocity_sum", "days_since", "in_list", "abs", "min", "max"]
OPERATORS = ["+", "-", "*", "%", "//", ">", "<", ">=", "<=", "==", "!=", "and", "or", "/"]
# The characters that make a parser interesting, weighted toward the ones that
# have historically mattered.
HOSTILE = list("()[]{}.,:;'\"\\`~!@#$%^&*_=|?<>\n\t") + ["__", "lambda", "for", "import"]


def assert_clean(source: str) -> None:
    """The whole property, in one place."""
    try:
        validate_source(source)
    except RuleSyntaxError:
        pass
    except Exception as exc:
        pytest.fail(f"{type(exc).__name__} escaped for {source!r}: {exc}")


class TestDeterministicSweep:
    """Seeded, so a failure is reproducible rather than a story about last Tuesday."""

    def test_random_token_soup(self):
        rng = random.Random(20260816)
        alphabet = FACTS + FUNCS + OPERATORS + HOSTILE + ["1", "0", "999", "'NG'"]

        for _ in range(SWEEP_SIZE // 2):
            length = rng.randint(1, 12)
            source = " ".join(rng.choice(alphabet) for _ in range(length))
            assert_clean(source)

    def test_random_character_soup(self):
        rng = random.Random(20260817)
        alphabet = string.printable

        for _ in range(SWEEP_SIZE // 2):
            length = rng.randint(0, 40)
            source = "".join(rng.choice(alphabet) for _ in range(length))
            assert_clean(source)


class TestStructuredGeneration:
    """Hypothesis explores the shapes random tokens rarely stumble into."""

    @settings(max_examples=500, suppress_health_check=[HealthCheck.too_slow])
    @given(st.text(max_size=200))
    def test_arbitrary_text(self, source: str):
        assert_clean(source)

    @settings(max_examples=500)
    @given(
        st.lists(
            st.sampled_from(FACTS + FUNCS + OPERATORS + ["(", ")", "1"]),
            min_size=1,
            max_size=15,
        )
    )
    def test_arbitrary_token_sequences(self, tokens: list[str]):
        assert_clean(" ".join(tokens))

    @settings(max_examples=200)
    @given(st.integers(min_value=0, max_value=200))
    def test_nesting_at_every_depth(self, depth: int):
        """Across the boundary in both directions, one bracket at a time."""
        assert_clean("(" * depth + "1" + ")" * depth)

    @settings(max_examples=200)
    @given(st.integers(min_value=0, max_value=100))
    def test_unary_runs_at_every_length(self, count: int):
        assert_clean("-" * count + "amount_minor")

    @settings(max_examples=200)
    @given(st.integers(min_value=0, max_value=400))
    def test_expressions_of_every_size(self, terms: int):
        assert_clean("+".join(["a"] * terms) + ">0" if terms else "")


class TestValidExpressionsAlwaysSurvive:
    """The mirror property: anything built only from the permitted grammar validates.

    Without this, a validator that rejected everything would pass every test
    above and the product would have no rule language at all.
    """

    @settings(max_examples=300)
    @given(
        left=st.sampled_from(FACTS),
        op=st.sampled_from([">", "<", ">=", "<=", "==", "!="]),
        right=st.integers(min_value=-1_000_000, max_value=1_000_000),
    )
    def test_simple_comparisons(self, left: str, op: str, right: int):
        validate_source(f"{left} {op} {right}")

    @settings(max_examples=300)
    @given(
        clauses=st.lists(
            st.tuples(st.sampled_from(FACTS), st.sampled_from([">", "<"]), st.integers()),
            min_size=1,
            max_size=8,
        ),
        joiner=st.sampled_from(["and", "or"]),
    )
    def test_conjunctions_of_comparisons(self, clauses, joiner: str):
        source = f" {joiner} ".join(f"{fact} {op} {value}" for fact, op, value in clauses)
        validate_source(source)
