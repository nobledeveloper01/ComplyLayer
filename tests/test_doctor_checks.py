"""The doctor's checks, exercised without a live Postgres or Redis.

Every check takes its dependencies as arguments precisely so this file can drive
the failure paths — which are the paths that matter, since a preflight is only
ever read when something is wrong.
"""

from __future__ import annotations

import pytest

from complylayer import checks


class TestPythonVersion:
    def test_accepts_312(self):
        assert checks.check_python_version((3, 12)).ok

    @pytest.mark.parametrize("version", [(3, 11), (3, 13), (4, 0)])
    def test_rejects_anything_else(self, version):
        result = checks.check_python_version(version)
        assert not result.ok
        assert "3.12" in result.remediation


class TestDatabase:
    def test_accepts_postgres_16(self):
        assert checks.check_database(160004).ok

    def test_rejects_postgres_15(self):
        result = checks.check_database(150010)
        assert not result.ok
        assert "partitioning" in result.remediation

    def test_reports_connection_failure_with_the_cause(self):
        result = checks.check_database(None, error=RuntimeError("connection refused"))
        assert not result.ok
        assert "connection refused" in result.detail

    def test_handles_a_connection_that_yields_no_version(self):
        result = checks.check_database(None)
        assert not result.ok
        assert result.fatal


class TestRedis:
    @staticmethod
    def clock_advancing_by(*per_sample_ms: float):
        """A clock where each measured sample takes the given number of milliseconds.

        The check reads the clock twice per sample, so the sequence alternates
        start, end, start, end. The warm-up ping is unmeasured and reads nothing.
        """
        readings: list[float] = []
        elapsed = 0.0
        for ms in per_sample_ms:
            readings.append(elapsed)
            elapsed += ms / 1000
            readings.append(elapsed)
        iterator = iter(readings)
        return lambda: next(iterator)

    def test_fast_round_trip_passes(self):
        result = checks.check_redis(lambda: True, self.clock_advancing_by(0.5, 0.5, 0.5), samples=3)
        assert result.ok

    def test_slow_round_trip_warns_without_failing(self):
        result = checks.check_redis(lambda: True, self.clock_advancing_by(4.0, 4.0, 4.0), samples=3)
        assert not result.ok
        assert not result.fatal
        assert result.status == "warn"

    def test_very_slow_round_trip_fails(self):
        """Above this, the 100 ms p99 contract cannot be met and the install should say so."""
        result = checks.check_redis(
            lambda: True, self.clock_advancing_by(25.0, 25.0, 25.0), samples=3
        )
        assert not result.ok
        assert result.fatal
        assert "availability zone" in result.remediation

    def test_the_first_ping_is_not_measured(self):
        """Connection setup is not round-trip time.

        A slow connect followed by fast pings is a healthy co-located Redis, and
        reporting the connect cost would send somebody chasing a problem that is
        not there.
        """
        pings = []
        result = checks.check_redis(
            lambda: pings.append(1),
            self.clock_advancing_by(0.3, 0.3, 0.3),
            samples=3,
        )
        assert result.ok
        assert len(pings) == 4, "expected one warm-up ping plus three measured"

    def test_one_slow_sample_does_not_decide_the_answer(self):
        """The median is why: a scheduler hiccup should not fail an install."""
        result = checks.check_redis(
            lambda: True, self.clock_advancing_by(0.4, 30.0, 0.4), samples=3
        )
        assert result.ok

    def test_unreachable_redis_fails_with_the_cause(self):
        def boom():
            raise ConnectionError("no route to host")

        result = checks.check_redis(boom)
        assert not result.ok
        assert "no route to host" in result.detail


class TestClockSkew:
    def test_agreeing_clocks_pass(self):
        assert checks.check_clock_skew(1_000_000.10, 1_000_000.05).ok

    def test_drift_beyond_a_second_fails(self):
        result = checks.check_clock_skew(1_000_000.0, 1_000_002.5)
        assert not result.ok
        assert "NTP" in result.remediation

    def test_drift_is_symmetric(self):
        """Fast and slow are equally wrong — the window is trimmed either way."""
        assert not checks.check_clock_skew(1_000_005.0, 1_000_000.0).ok

    def test_unreadable_server_time_warns_rather_than_fails(self):
        result = checks.check_clock_skew(None, 1_000_000.0)
        assert not result.ok
        assert not result.fatal


class TestSummarise:
    def test_counts_failures_and_warnings_separately(self):
        results = [
            checks.CheckResult("a", ok=True, detail=""),
            checks.CheckResult("b", ok=False, detail="", fatal=True),
            checks.CheckResult("c", ok=False, detail="", fatal=False),
            checks.CheckResult("d", ok=False, detail="", fatal=True),
        ]
        assert checks.summarise(results) == (2, 1)

    def test_all_passing_is_zero_zero(self):
        assert checks.summarise([checks.CheckResult("a", ok=True, detail="")]) == (0, 0)

    def test_status_labels(self):
        assert checks.CheckResult("a", ok=True, detail="").status == "ok"
        assert checks.CheckResult("a", ok=False, detail="", fatal=False).status == "warn"
        assert checks.CheckResult("a", ok=False, detail="", fatal=True).status == "FAIL"


class TestAuditImmutability:
    """Phase 5's check. The doctor grows one per phase, so a new failure mode
    always arrives with its own preflight."""

    def test_both_triggers_present_passes(self):
        result = checks.check_audit_immutability(
            ["complylayer_audit_append_only", "complylayer_audit_no_truncate"]
        )
        assert result.ok

    def test_a_missing_trigger_is_fatal_and_names_it(self):
        """A deployment whose triggers were dropped looks completely healthy and
        quietly accepts an UPDATE on an audit record."""
        result = checks.check_audit_immutability(["complylayer_audit_append_only"])
        assert not result.ok
        assert result.fatal
        assert "no_truncate" in result.detail
        assert "evidence" in result.remediation

    def test_no_triggers_at_all(self):
        assert not checks.check_audit_immutability([]).ok
        assert not checks.check_audit_immutability(None).ok

    def test_an_unreadable_schema_is_reported_rather_than_assumed_fine(self):
        result = checks.check_audit_immutability(None, error=RuntimeError("permission denied"))
        assert not result.ok
        assert "permission denied" in result.detail
