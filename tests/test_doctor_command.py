"""The doctor command itself: what it prints, and what it exits with.

The exit code is the part that matters. `complylayer_doctor` is meant to be run
in a deployment pipeline, where a preflight that reports problems on stdout and
still exits zero is indistinguishable from one that found nothing.
"""

from __future__ import annotations

from contextlib import contextmanager
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command

MODULE = "complylayer.management.commands.complylayer_doctor"


class FakeRedis:
    def __init__(self, server_time=(1_700_000_000, 0), ping_raises=None, time_raises=None):
        self._server_time = server_time
        self._ping_raises = ping_raises
        self._time_raises = time_raises

    def ping(self):
        if self._ping_raises:
            raise self._ping_raises
        return True

    def time(self):
        if self._time_raises:
            raise self._time_raises
        return self._server_time


@contextmanager
def doctor_environment(*, pg_version=160004, db_error=None, redis_client=None, now=1_700_000_000.0):
    """Stand in for Postgres, Redis, the wall clock and a configured deployment.

    The last of those arrived with the deployment-secrets and transport checks:
    a test run has DEBUG off and the published defaults still in place, which is
    precisely the state those checks exist to fail. Setting them here keeps
    "healthy" meaning healthy.
    """
    from django.test import override_settings

    connection = mock.MagicMock()
    connection.pg_version = pg_version
    if db_error is not None:
        connection.cursor.side_effect = db_error
    else:
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.return_value = [
            ("complylayer_audit_append_only",),
            ("complylayer_audit_no_truncate",),
        ]
        cursor.fetchone.return_value = ("complylayer_app", False, False)

    from django.conf import settings as django_settings

    with (
        mock.patch(f"{MODULE}.connection", connection),
        mock.patch(f"{MODULE}.redis.Redis.from_url", return_value=redis_client or FakeRedis()),
        mock.patch(f"{MODULE}.time.time", return_value=now),
        override_settings(
            SECRET_KEY="a-configured-signing-key",  # noqa: S106
            COMPLYLAYER={
                **django_settings.COMPLYLAYER,
                "CUSTOMER_SALT": "a-configured-salt",
                # The audit anchoring check joined the same family: a test run
                # has no checkpoint keys, which is exactly the state that check
                # exists to report.
                "CHECKPOINT_PRIVATE_KEY": "a-configured-private-key",
                "CHECKPOINT_PUBLIC_KEY": "a-configured-public-key",
            },
            SESSION_COOKIE_SECURE=True,
            CSRF_COOKIE_SECURE=True,
            SECURE_HSTS_SECONDS=31_536_000,
        ),
    ):
        yield


def run_doctor() -> str:
    out = StringIO()
    call_command("complylayer_doctor", stdout=out)
    return out.getvalue()


class TestHealthyDeployment:
    def test_reports_success(self):
        with doctor_environment():
            output = run_doctor()
        assert "All checks passed." in output

    def test_lists_every_check(self):
        with doctor_environment():
            output = run_doctor()
        for name in (
            "python version",
            "database",
            "redis",
            "clock skew",
            "audit trail",
            "row level security",
            "deployment secrets",
            "transport security",
            "audit anchoring",
        ):
            assert name in output


class TestFailingDeployment:
    def test_unreachable_database_exits_non_zero(self):
        with doctor_environment(db_error=RuntimeError("connection refused")):
            with pytest.raises(SystemExit) as exc:
                run_doctor()
        assert exc.value.code == 1

    def test_unreachable_redis_exits_non_zero(self):
        client = FakeRedis(ping_raises=ConnectionError("no route to host"))
        with doctor_environment(redis_client=client):
            with pytest.raises(SystemExit) as exc:
                run_doctor()
        assert exc.value.code == 1

    def test_old_postgres_exits_non_zero(self):
        with doctor_environment(pg_version=150010):
            with pytest.raises(SystemExit) as exc:
                run_doctor()
        assert exc.value.code == 1

    def test_drifted_clock_exits_non_zero(self):
        """Ten seconds adrift. Velocity windows would be trimmed against the wrong instant."""
        with doctor_environment(now=1_700_000_010.0):
            with pytest.raises(SystemExit) as exc:
                run_doctor()
        assert exc.value.code == 1

    def test_failure_output_carries_its_remediation(self):
        out = StringIO()
        with doctor_environment(pg_version=150010):
            with pytest.raises(SystemExit):
                call_command("complylayer_doctor", stdout=out)
        assert "->" in out.getvalue()
        assert "Upgrade to Postgres" in out.getvalue()


class TestWarnings:
    def test_unreadable_redis_time_warns_without_failing(self):
        """A warning is not a failure: the deployment works, it is just worth knowing."""
        client = FakeRedis(time_raises=RuntimeError("TIME unsupported"))
        with doctor_environment(redis_client=client):
            output = run_doctor()
        assert "warning" in output.lower()
