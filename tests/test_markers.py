"""`make test` must run on a machine with no Docker.

The Makefile says so — "unit tests, no Postgres or Redis needed" — and the
README repeats it as the reason a clean checkout can run the suite. That promise
is kept by one thing: every test that touches a database is marked `integration`,
which `make test` excludes.

It was false. `tests/test_metrics_access.py` and one class in
`tests/test_dashboard.py` were marked `django_db` and not `integration`, so on a
machine without Postgres they errored at setup and `make test` failed at the
first thing a new contributor runs.

Found the same way as everything else here: by running it in the stated
conditions, which happened by accident when Docker stopped mid-session.

This file is the durable half of that fix. The markers were easy to correct; the
drift is what recurs, because nobody adding a test thinks about which subset it
lands in.
"""

from __future__ import annotations

import pathlib

TESTS = pathlib.Path(__file__).parent


def test_every_database_test_is_marked_integration():
    """A test needing Postgres must say so, or it lands in the docker-free set."""
    offenders = []
    for path in sorted(TESTS.glob("test_*.py")):
        source = path.read_text()
        if "django_db" not in source:
            continue
        # `benchmark` is excluded from `make test` too, so it is equally safe.
        if "mark.integration" in source or "mark.benchmark" in source:
            continue
        offenders.append(path.name)

    assert not offenders, (
        f"these files use django_db without an integration or benchmark marker: {offenders}. "
        "`make test` excludes integration so a clean checkout runs without Docker; an "
        "unmarked database test breaks that for everyone who has not started the services."
    )


def test_the_marker_names_match_the_ones_pyproject_declares():
    """A typo in a marker name is silent: pytest warns, the test still runs, and
    the subset it was meant to leave is the subset it joins."""
    declared = (pathlib.Path(__file__).parent.parent / "pyproject.toml").read_text()
    for marker in ("integration:", "benchmark:"):
        assert marker in declared, f"{marker} is not declared in pyproject.toml"
