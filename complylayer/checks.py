"""Preflight checks behind ``manage.py complylayer_doctor``.

The checks exist because the failure modes that matter most in this product are
silent. A self-hosted deployment with Redis in another availability zone will
serve correct decisions and miss its latency SLA; a host whose clock has drifted
will evaluate velocity windows against the wrong instant and nothing will error.
Both should be found during installation, not during an incident.

One check per failure mode, and the roadmap adds a check per phase. Each returns
a result carrying its own remediation, because a preflight that reports a problem
without saying what to do about it has only moved the confusion.

Every check takes its dependencies as arguments rather than reaching for globals,
so they are testable without a live Postgres or Redis.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from statistics import median

# Redis sits inside the 15 ms fact-gathering budget (see docs/plan-architecture.md).
# A single PING is one round trip with no work attached, so it should be far
# under that. Above the warn threshold the deployment is probably crossing an
# availability zone; above the fail threshold the latency contract is unmeetable.
REDIS_PING_WARN_MS = 2.0
REDIS_PING_FAIL_MS = 10.0

# Velocity windows are computed against wall-clock time. A host a second adrift
# trims the wrong members from a sorted set, and nothing anywhere reports an error.
CLOCK_SKEW_FAIL_SECONDS = 1.0

MIN_POSTGRES_VERSION = 16


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one preflight check."""

    name: str
    ok: bool
    detail: str
    remediation: str = ""
    fatal: bool = True

    @property
    def status(self) -> str:
        if self.ok:
            return "ok"
        return "FAIL" if self.fatal else "warn"


def check_python_version(version_info: tuple[int, int] | None = None) -> CheckResult:
    """The project pins 3.12; running on anything else is unsupported, not merely untested."""
    major, minor = version_info or sys.version_info[:2]
    ok = (major, minor) == (3, 12)
    return CheckResult(
        name="python version",
        ok=ok,
        detail=f"running {major}.{minor}, expected 3.12",
        remediation="" if ok else "Install Python 3.12 (`uv python install 3.12`) and re-sync.",
    )


def check_database(server_version: int | None, error: Exception | None = None) -> CheckResult:
    """Postgres must be reachable and recent enough for the features the schema relies on.

    ``server_version`` is Django's integer form, e.g. 160004 for 16.4.
    """
    if error is not None:
        return CheckResult(
            name="database",
            ok=False,
            detail=f"could not connect: {error}",
            remediation="Check COMPLYLAYER_DATABASE_URL, and that Postgres is running.",
        )
    if server_version is None:
        return CheckResult(
            name="database",
            ok=False,
            detail="connected but the server version could not be read",
            remediation="Unexpected. Check the connecting role has permission to read settings.",
        )

    major = server_version // 10000
    ok = major >= MIN_POSTGRES_VERSION
    return CheckResult(
        name="database",
        ok=ok,
        detail=f"Postgres {major} (need {MIN_POSTGRES_VERSION}+)",
        remediation=""
        if ok
        else f"Upgrade to Postgres {MIN_POSTGRES_VERSION}+. Declarative partitioning on the "
        "decisions table and row level security both depend on it.",
    )


def check_redis(
    ping: Callable[[], object],
    clock: Callable[[], float] = time.perf_counter,
    samples: int = 5,
) -> CheckResult:
    """Redis must be reachable, and close enough that the latency budget survives.

    The first ping is thrown away. It carries TCP connection setup, which on a
    laptop running Docker can be several milliseconds and has nothing to do with
    how far away Redis is. Reporting it would send somebody hunting a network
    problem they do not have — and the decision path reuses a pooled connection,
    so steady state is what the budget actually spends.

    The median of the remaining samples, rather than the mean, so one scheduler
    hiccup does not decide the answer.
    """
    try:
        ping()  # warm-up, deliberately unmeasured
        timings = []
        for _ in range(samples):
            start = clock()
            ping()
            timings.append((clock() - start) * 1000)
    except Exception as exc:
        return CheckResult(
            name="redis",
            ok=False,
            detail=f"could not connect: {exc}",
            remediation="Check COMPLYLAYER_REDIS_URL, and that Redis is running.",
        )
    elapsed_ms = median(timings)

    if elapsed_ms >= REDIS_PING_FAIL_MS:
        return CheckResult(
            name="redis",
            ok=False,
            detail=f"round trip {elapsed_ms:.2f} ms (limit {REDIS_PING_FAIL_MS:.0f} ms)",
            remediation="Redis is too far away to meet the 100 ms p99 contract. Co-locate it in "
            "the same availability zone as the decision workload.",
        )
    if elapsed_ms >= REDIS_PING_WARN_MS:
        return CheckResult(
            name="redis",
            ok=False,
            fatal=False,
            detail=f"round trip {elapsed_ms:.2f} ms (want under {REDIS_PING_WARN_MS:.0f} ms)",
            remediation="Slower than expected for a co-located Redis. Worth confirming it is in "
            "the same zone before the latency work in phase 4.",
        )
    return CheckResult(name="redis", ok=True, detail=f"round trip {elapsed_ms:.2f} ms")


def check_clock_skew(redis_time_seconds: float | None, local_time_seconds: float) -> CheckResult:
    """Compare the local clock against Redis, which is the clock velocity windows are trimmed by."""
    if redis_time_seconds is None:
        return CheckResult(
            name="clock skew",
            ok=False,
            fatal=False,
            detail="could not read the Redis server time",
            remediation="Not fatal, but velocity windows depend on these clocks agreeing.",
        )

    skew = abs(local_time_seconds - redis_time_seconds)
    ok = skew < CLOCK_SKEW_FAIL_SECONDS
    return CheckResult(
        name="clock skew",
        ok=ok,
        detail=f"{skew:.3f} s between this host and Redis",
        remediation=""
        if ok
        else "Enable NTP on this host. Velocity windows are trimmed by timestamp, so a drifting "
        "clock silently evaluates the wrong window and never raises an error.",
    )


def check_audit_immutability(trigger_names: list[str] | None, error: Exception | None = None):
    """The audit trail is only evidence if the database enforces it.

    §8.3's claim is that immutability is enforced rather than promised, and the
    difference is exactly these two triggers. A deployment whose migrations ran
    but whose triggers were dropped — by a restore, by a schema tool, by
    somebody debugging — looks completely healthy and quietly accepts an UPDATE
    on an audit record.
    """
    required = {"complylayer_audit_append_only", "complylayer_audit_no_truncate"}

    if error is not None:
        return CheckResult(
            name="audit trail",
            ok=False,
            detail=f"could not read the triggers: {error}",
            remediation="Check the database connection and that migrations have run.",
        )

    missing = sorted(required - set(trigger_names or []))
    if missing:
        return CheckResult(
            name="audit trail",
            ok=False,
            detail=f"append-only enforcement is missing: {', '.join(missing)}",
            remediation="Run `manage.py migrate complylayer`. Until these exist the audit "
            "trail can be edited, which means it is a log rather than evidence.",
        )

    return CheckResult(name="audit trail", ok=True, detail="append-only enforced by the database")


def check_rls_effective(
    role: str | None,
    is_superuser: bool | None,
    bypasses_rls: bool | None,
    error: Exception | None = None,
) -> CheckResult:
    """Row Level Security is inert for a superuser, and says nothing about it.

    This is the failure the whole project keeps aiming at: a control that looks
    configured and does nothing. The policies exist, `FORCE ROW LEVEL SECURITY`
    is set, `\\d` shows everything correct — and every policy is skipped, because
    Postgres exempts superusers and roles with BYPASSRLS unconditionally.

    Found by writing the test: the default docker-compose role is a superuser,
    so layer three was silently doing nothing in development and would have been
    equally silent in any deployment that reused those credentials.
    """
    if error is not None:
        return CheckResult(
            name="row level security",
            ok=False,
            detail=f"could not read the connecting role: {error}",
            remediation="Check the database connection.",
        )

    if is_superuser or bypasses_rls:
        why = "a superuser" if is_superuser else "granted BYPASSRLS"
        return CheckResult(
            name="row level security",
            ok=False,
            # A warning rather than a failure, because the default docker-compose
            # setup connects as a superuser and a development machine should not
            # be blocked by that. `--strict` makes every warning fatal, which is
            # what a deploy pipeline should run: this must not reach production.
            fatal=False,
            detail=f"connecting as {role!r}, which is {why} — every policy is skipped",
            remediation="Connect as a role that is neither SUPERUSER nor BYPASSRLS, and which "
            "does not own the tables. Until then tenant isolation rests on application code "
            "alone, and the database layer is decoration.",
        )

    return CheckResult(
        name="row level security",
        ok=True,
        detail=f"policies apply to {role!r}",
    )


def check_deployment_secrets(
    secret_key: str,
    customer_salt: str,
    debug: bool,
    *,
    insecure_secret_key: str,
    insecure_customer_salt: str,
) -> CheckResult:
    """The two secrets whose development defaults are published in this repository.

    Neither has ever announced itself. `SECRET_KEY` signs the session cookie,
    and the dashboard's second-factor flag lives inside that session — so on the
    default key a forged cookie is a full sign-in with both factors, and nothing
    anywhere reports it. `CUSTOMER_SALT` used to fall back to the tenant id,
    which is stored on every row it protects, which makes it not a key at all.

    Fatal rather than a warning, unlike the row level security check. A superuser
    connection is wrong but still isolates tenants through two other layers;
    these two have nothing behind them.
    """
    unset = []
    if secret_key == insecure_secret_key:
        unset.append("COMPLYLAYER_SECRET_KEY")
    if customer_salt == insecure_customer_salt:
        unset.append("COMPLYLAYER_CUSTOMER_SALT")

    if not unset:
        return CheckResult(
            name="deployment secrets",
            ok=True,
            detail="the signing key and the customer salt are both set",
        )

    named = ", ".join(unset)
    if debug:
        return CheckResult(
            name="deployment secrets",
            ok=False,
            fatal=False,
            detail=f"still on the development value: {named} (DEBUG is on)",
            remediation="Fine for a laptop. `--strict` makes this fatal, which is what a deploy "
            "pipeline should run.",
        )

    return CheckResult(
        name="deployment secrets",
        ok=False,
        detail=f"still on the development value, and DEBUG is off: {named}",
        remediation=(
            "Generate both and set them in the environment:\n"
            "      COMPLYLAYER_SECRET_KEY=$(python -c "
            "'import secrets; print(secrets.token_urlsafe(64))')\n"
            "      COMPLYLAYER_CUSTOMER_SALT=$(python -c "
            "'import secrets; print(secrets.token_urlsafe(32))')\n"
            "    Changing the salt later re-pseudonymises every future decision, so the "
            "history stops joining to the new one. Set it once, keep it in a secret manager, "
            "and back it up with the database."
        ),
    )


def check_transport_security(
    session_cookie_secure: bool,
    csrf_cookie_secure: bool,
    hsts_seconds: int,
    debug: bool,
    serves_sessions: bool = True,
) -> CheckResult:
    """A session cookie without `Secure` travels over plain HTTP.

    The dashboard session carries the completed second factor, so one
    intercepted request on a hostile network is both factors. The other cookie
    flags were set and this one was not, which is the kind of omission that
    reads as deliberate.

    Only the management workload issues a session. Reporting this against a
    decision worker would be a warning nobody can act on, and a preflight that
    cries wolf is one people learn to skim — which is the failure mode this
    whole command exists to avoid.
    """
    if not serves_sessions:
        return CheckResult(
            name="transport security",
            ok=True,
            detail="not applicable: this workload issues no session cookie",
        )

    missing = []
    if not session_cookie_secure:
        missing.append("SESSION_COOKIE_SECURE")
    if not csrf_cookie_secure:
        missing.append("CSRF_COOKIE_SECURE")
    if hsts_seconds <= 0:
        missing.append("SECURE_HSTS_SECONDS")

    if not missing:
        return CheckResult(
            name="transport security",
            ok=True,
            detail="cookies are HTTPS-only and HSTS is set",
        )

    return CheckResult(
        name="transport security",
        ok=False,
        fatal=not debug,
        detail=f"not set: {', '.join(missing)}",
        remediation="Serve the dashboard over HTTPS and set these. A session cookie without "
        "Secure is one an attacker on the network reads in clear text, and it already "
        "carries the second factor.",
    )


def check_audit_anchoring(private_key: str, public_key: str, debug: bool) -> CheckResult:
    """Whether anything outside the database vouches for the audit trail.

    The chain is unkeyed SHA-256, so it catches a record altered in place and
    not a rewrite that recomputes every hash after it. Without a checkpoint key
    the trail is exactly as strong as the write access around it, and §8.3's
    claim that immutability is enforced quietly narrows to "enforced against
    accidents".

    A warning rather than a failure: a self-hoster evaluating the product should
    not be blocked, and `--strict` promotes it for the pipeline that ships.
    """
    if private_key and public_key:
        return CheckResult(
            name="audit anchoring",
            ok=True,
            detail="chain heads are signed and verifiable",
        )

    missing = []
    if not private_key:
        missing.append("COMPLYLAYER_CHECKPOINT_PRIVATE_KEY")
    if not public_key:
        missing.append("COMPLYLAYER_CHECKPOINT_PUBLIC_KEY")

    return CheckResult(
        name="audit anchoring",
        ok=False,
        fatal=not debug,
        detail=f"not set: {', '.join(missing)} — nothing external anchors the audit chain",
        remediation="Generate a pair with `manage.py complylayer_checkpoint --generate-key`, "
        "keep the private half in a secret manager rather than in this database, and run "
        "`manage.py complylayer_checkpoint` on a schedule. The interval between runs is the "
        "window in which a rewrite is undetectable.",
    )


def summarise(results: list[CheckResult]) -> tuple[int, int]:
    """Return (failures, warnings). The exit code is the count of fatal failures."""
    failures = sum(1 for r in results if not r.ok and r.fatal)
    warnings = sum(1 for r in results if not r.ok and not r.fatal)
    return failures, warnings
