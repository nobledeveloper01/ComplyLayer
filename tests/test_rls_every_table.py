"""Row level security, on every table that claims it — and switched on at all.

Two findings, both from noticing what the existing suite did *not* cover.

**The policies were only ever executed against one table.** Migration 0005 puts
seven tables behind a tenant policy, and `test_tenant_isolation.py` exercised
`complylayer_rule`. The other six were configured, visible in `\\d`, and never
run: the decisions table recording what money was allowed to do, the audit chain
that is meant to be evidence, and the key table. So the list drives the test
here — a table added to the migration is covered automatically.

**`tenant_scope()` had no caller outside the tests.** Which meant
`current_setting('complylayer.tenant_id')` was NULL on every real request, so
under `complylayer_app` every policy matched nothing and the whole application
returned empty results. Authentication was fixed first (migration 0008); this
covers the rest of it. `TestTheApplicationSetsTheScopeItself` is the part that
would have caught it, because it goes through the middleware rather than setting
the scope by hand.

`SET LOCAL ROLE` rather than a second connection: tests connect as the compose
superuser, and Postgres exempts superusers from every policy unconditionally, so
asserting on that connection asserts nothing.
"""

from __future__ import annotations

import importlib

import pytest
from django.apps import apps
from django.db import connection

from complylayer.tenancy import tenant_scope

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

# Imported rather than restated, so this file cannot fall behind the migration.
_rls = importlib.import_module("complylayer.migrations.0005_row_level_security")
SCOPED_TABLES = _rls.TENANT_SCOPED_TABLES


def as_app_role(tenant_id: str, sql: str, params=None):
    """Run one query as the restricted role, scoped to one tenant."""
    with tenant_scope(tenant_id), connection.cursor() as cursor:
        cursor.execute("SET LOCAL ROLE complylayer_app")
        cursor.execute(sql, params or [])
        rows = cursor.fetchall()
        cursor.execute("RESET ROLE")
    return rows


@pytest.fixture
def two_tenants_with_rows():
    """One row in every tenant-scoped table, for each of two tenants.

    Written through the ORM as a superuser, which is how the application creates
    them; the assertions then read them back as the restricted role.
    """
    from datetime import UTC, datetime

    from django.contrib.auth.models import User

    from complylayer.api import auth
    from complylayer.models import (
        ApiKey,
        AuditCheckpoint,
        AuditRecord,
        DashboardUser,
        Decision,
        IdempotencyRecord,
        NamedList,
        Rule,
        RuleSetVersion,
        Tenant,
    )

    made = {}
    for suffix in ("alpha", "bravo"):
        tenant = Tenant.objects.create(id=f"tnt_{suffix}", name=suffix)
        moment = datetime(2026, 8, 1, tzinfo=UTC)

        Rule.objects.create(
            id=f"rul_{suffix}",
            tenant=tenant,
            name="limit",
            category="aml",
            expression="amount_minor > 100",
            severity="block",
            created_by="setup",
        )
        RuleSetVersion.objects.create(
            tenant=tenant,
            version=1,
            rules_snapshot=[],
            lists_snapshot={},
            published_by="setup",
        )
        decision = Decision.objects.create(
            id=f"dec_{suffix}",
            tenant=tenant,
            decided_at=moment,
            idempotency_key=f"idem-{suffix}",
            ruleset_version=1,
            transaction_ref=f"TXN-{suffix}",
            customer_ref_hash="x" * 64,
            amount_minor=1000,
            currency="NGN",
            context={},
            outcome="allow",
            latency_ms=5,
        )
        IdempotencyRecord.objects.create(
            tenant=tenant,
            key=f"idem-{suffix}",
            decision_id=decision.id,
            decision_decided_at=moment,
            response_body={},
        )
        AuditRecord.objects.create(
            id=f"aud_{suffix}",
            tenant=tenant,
            event_type="rule.created",
            occurred_at=moment,
            actor={},
            subject={},
            payload={},
            prev_hash="sha256:" + "0" * 64,
            hash="sha256:" + "1" * 64,
        )
        NamedList.objects.create(tenant=tenant, name="countries", values=["NG"], updated_by="setup")
        full_key, prefix = auth.generate_key("live")
        ApiKey.objects.create(
            id=f"key_{suffix}",
            tenant=tenant,
            name="k",
            prefix=prefix,
            hashed_secret=auth.hash_secret(full_key),
            role="compliance_officer",
            created_by="setup",
        )
        DashboardUser.objects.create(
            user=User.objects.create_user(
                username=f"{suffix}@example.com", email=f"{suffix}@example.com"
            ),
            tenant=tenant,
            role="compliance_officer",
            totp_secret="JBSWY3DPEHPK3PXP",  # noqa: S106
        )
        AuditCheckpoint.objects.create(
            tenant=tenant,
            chain_length=1,
            head_hash="sha256:" + "1" * 64,
            signed_at=moment,
            signature="00" * 32,
        )
        made[suffix] = (tenant, full_key)
    return made


@pytest.mark.parametrize("table", SCOPED_TABLES)
class TestEveryScopedTable:
    """Driven from the migration's own list, so a new table is covered the day
    it is added rather than the day somebody remembers."""

    def test_a_scoped_query_sees_only_its_own_tenant(self, table, two_tenants_with_rows):
        rows = as_app_role("tnt_alpha", f"SELECT count(*) FROM {table}")  # noqa: S608
        assert rows[0][0] == 1, f"{table}: expected exactly tenant alpha's row"

    def test_the_other_tenants_row_is_invisible(self, table, two_tenants_with_rows):
        """The query has no WHERE clause at all — this is the case where somebody
        writes raw SQL and forgets to scope it."""
        rows = as_app_role(
            "tnt_alpha",
            f"SELECT count(*) FROM {table} WHERE tenant_id = 'tnt_bravo'",  # noqa: S608
        )
        assert rows[0][0] == 0, f"{table}: tenant alpha could see tenant bravo's row"

    def test_no_tenant_set_returns_nothing(self, table, two_tenants_with_rows):
        """The safe direction: a forgotten scope yields an empty result rather
        than an unscoped one."""
        rows = as_app_role("", f"SELECT count(*) FROM {table}")  # noqa: S608
        assert rows[0][0] == 0, f"{table}: an unscoped connection saw rows"


class TestNothingScopedEscapedTheList:
    """The direction the mistake actually gets made.

    Nobody removes a table from the migration; somebody adds a model with a
    tenant on it and does not think about row level security. This caught
    `complylayer_dashboarduser`, which carries a tenant *and every user's TOTP
    secret* and had no policy at all.
    """

    def test_every_model_carrying_a_tenant_has_a_policy(self):
        carrying = {
            model._meta.db_table
            for model in apps.get_app_config("complylayer").get_models()
            if any(field.name == "tenant" for field in model._meta.fields)
        }
        missing = sorted(carrying - set(SCOPED_TABLES))
        assert not missing, (
            f"these tables carry a tenant and have no row level security policy: {missing}. "
            "Add them to TENANT_SCOPED_TABLES in a migration, or explain in that migration "
            "why the table is exempt."
        )

    def test_the_policies_actually_exist_in_the_database(self):
        """The migration ran, rather than merely being committed."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tablename, policyname FROM pg_policies WHERE schemaname='public'"
            )
            by_table = {row[0] for row in cursor.fetchall()}
        assert set(SCOPED_TABLES) <= by_table

    def test_force_is_set_so_the_owner_is_not_exempt(self):
        """`ENABLE` alone leaves the table owner bypassing every policy, and D4's
        "the app role should not own any table" is a rule somebody eventually
        forgets."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relname FROM pg_class WHERE relrowsecurity AND relforcerowsecurity"
            )
            forced = {row[0] for row in cursor.fetchall()}
        assert set(SCOPED_TABLES) <= forced


class TestTheApplicationSetsTheScopeItself:
    """The finding this file exists for.

    Every assertion above sets the scope by hand, which is exactly how the
    policies looked correct for eight phases while no request ever set one.
    These go through the real code path instead.
    """

    def test_the_rule_set_load_scopes_its_own_read(self, two_tenants_with_rows):
        """Called by the middleware *and* by the background version watcher,
        which has no request and so no request-level scope."""
        from complylayer.api.decision_middleware import _load_published

        with connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE complylayer_app")
            try:
                loaded = _load_published("tnt_alpha")
            finally:
                cursor.execute("RESET ROLE")

        assert loaded is not None, "the rule set load returned nothing under the app role"
        assert loaded[0] == 1

    def test_the_decision_middleware_scopes_the_request(self, two_tenants_with_rows, settings):
        """Asserted on the setting itself rather than on a query result, because
        the point is that the request path sets it at all.

        The version watcher is off here for the reason `tests/conftest.py`
        gives: it polls in a background thread, and this test's database is
        about to be rolled back underneath it.
        """
        from complylayer.api import decision_middleware
        from complylayer.tenancy import current_tenant

        settings.COMPLYLAYER = {**settings.COMPLYLAYER, "WATCH_RULESET_VERSIONS": False}
        decision_middleware.shutdown()

        seen = {}

        def capture(request):
            seen["tenant"] = current_tenant()
            from django.http import HttpResponse

            return HttpResponse("ok")

        from complylayer.api.decision_middleware import DecisionMiddleware

        _, full_key = two_tenants_with_rows["alpha"]
        middleware = DecisionMiddleware(capture)

        from django.test import RequestFactory

        request = RequestFactory().post(
            "/v1/decisions", headers={"authorization": f"Bearer {full_key}"}
        )
        try:
            middleware(request)
        finally:
            decision_middleware.shutdown()

        assert seen.get("tenant") == "tnt_alpha", (
            "the decision path ran without setting complylayer.tenant_id, so every "
            "row level security policy would match nothing"
        )

    def test_a_whole_decision_is_served_under_the_application_role(self, settings):
        """The one that settles it.

        Everything else here asserts a piece. This runs `POST /v1/decisions`
        end to end — authenticate, load the rule set, evaluate, record — with
        the connection dropped to `complylayer_app`, which is the configuration
        the doctor recommends and which no part of this product had ever been
        run in. Before the fixes it authenticated and then answered 409 with no
        rule set, because nothing set the tenant.
        """
        import json

        from django.test import Client

        from complylayer.api import auth, decision_middleware
        from complylayer.models import ApiKey, RuleSetVersion, Tenant

        settings.COMPLYLAYER = {**settings.COMPLYLAYER, "WATCH_RULESET_VERSIONS": False}
        settings.MIDDLEWARE = [
            "django.middleware.security.SecurityMiddleware",
            "django.middleware.common.CommonMiddleware",
            "complylayer.api.decision_middleware.DecisionMiddleware",
        ]
        settings.ROOT_URLCONF = "server.urls"
        auth.clear_cache()
        decision_middleware.shutdown()

        tenant = Tenant.objects.create(id="tnt_e2e", name="Bank")
        full_key, prefix = auth.generate_key("live")
        ApiKey.objects.create(
            id="key_e2e",
            tenant=tenant,
            name="prod",
            prefix=prefix,
            hashed_secret=auth.hash_secret(full_key),
            environment="live",
            role="compliance_officer",
            created_by="admin",
        )
        RuleSetVersion.objects.create(
            tenant=tenant,
            version=1,
            rules_snapshot=[
                {
                    "id": "rul_big",
                    "name": "Over the limit",
                    "expression": "amount_minor > 100000",
                    "severity": "block",
                }
            ],
            lists_snapshot={},
            published_by="admin",
        )

        payload = {
            "transaction_ref": "TXN-E2E-1",
            "customer_ref": "usr_1",
            "amount_minor": 5_000_000,
            "currency": "NGN",
            "transaction_type": "transfer",
        }

        with connection.cursor() as cursor:
            cursor.execute("SET ROLE complylayer_app")
            try:
                response = Client().post(
                    "/v1/decisions",
                    data=json.dumps(payload),
                    content_type="application/json",
                    headers={
                        "authorization": f"Bearer {full_key}",
                        "idempotency-key": "e2e-1",
                    },
                )
            finally:
                cursor.execute("RESET ROLE")
                decision_middleware.shutdown()

        assert response.status_code == 200, (
            f"the decision path does not work under complylayer_app: "
            f"{response.status_code} {response.content[:200]!r}"
        )
        body = response.json()
        assert body["outcome"] == "block"
        assert [rule["id"] for rule in body["matched_rules"]] == ["rul_big"]

    def test_the_scope_does_not_outlive_the_request(self):
        """A session-scoped setting on a pooled connection is inherited by
        whoever borrows that connection next, which is the cross-tenant read RLS
        was added to prevent (D4)."""
        from complylayer.tenancy import current_tenant

        with tenant_scope("tnt_alpha"):
            assert current_tenant() == "tnt_alpha"
        assert current_tenant() is None
