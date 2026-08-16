"""Preflight the failure modes that are silent.

Run after installation and before believing any latency number.
"""

from __future__ import annotations

import time

import redis
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection

from complylayer import checks


class Command(BaseCommand):
    help = "Check that this deployment can actually meet ComplyLayer's contracts."

    def handle(self, *args, **options) -> None:
        results = [checks.check_python_version()]
        results.append(self._database())
        client = self._redis_client()
        results.append(checks.check_redis(client.ping))
        results.append(self._clock_skew(client))
        results.append(self._audit_immutability())

        width = max(len(r.name) for r in results)
        for r in results:
            marker = {"ok": "  ok  ", "warn": " warn ", "FAIL": " FAIL "}[r.status]
            self.stdout.write(f"[{marker}] {r.name.ljust(width)}  {r.detail}")
            if r.remediation:
                self.stdout.write(f"           {' ' * width}  -> {r.remediation}")

        failures, warnings = checks.summarise(results)
        self.stdout.write("")
        if failures:
            self.stdout.write(
                self.style.ERROR(f"{failures} check(s) failed, {warnings} warning(s).")
            )
            raise SystemExit(1)
        if warnings:
            self.stdout.write(self.style.WARNING(f"All checks passed with {warnings} warning(s)."))
            return
        self.stdout.write(self.style.SUCCESS("All checks passed."))

    def _database(self) -> checks.CheckResult:
        try:
            with connection.cursor():
                version = connection.pg_version
        except Exception as exc:
            return checks.check_database(None, error=exc)
        return checks.check_database(version)

    def _audit_immutability(self) -> checks.CheckResult:
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT tgname FROM pg_trigger
                    WHERE tgrelid = 'complylayer_auditrecord'::regclass
                      AND NOT tgisinternal
                    """
                )
                names = [row[0] for row in cursor.fetchall()]
        except Exception as exc:
            return checks.check_audit_immutability(None, error=exc)
        return checks.check_audit_immutability(names)

    def _redis_client(self) -> redis.Redis:
        return redis.Redis.from_url(settings.COMPLYLAYER["REDIS_URL"])

    def _clock_skew(self, client: redis.Redis) -> checks.CheckResult:
        try:
            seconds, microseconds = client.time()
            server_time = seconds + microseconds / 1_000_000
        except Exception:
            server_time = None
        return checks.check_clock_skew(server_time, time.time())
