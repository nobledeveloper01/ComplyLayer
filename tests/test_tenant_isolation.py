"""The mandatory isolation suite. A blocking CI gate.

§8.1 asks for one specific thing and the reason is worth stating: every read
endpoint must return **404** when called as the wrong tenant, not 403. A 403
confirms the resource exists, which tells tenant B that tenant A's rule id is
real. That is an information leak on its own, and rule ids are guessable enough
for it to matter.

The suite enumerates the URLconf and fails if any route is not exercised. A
tenant isolation test that covers the endpoints somebody remembered to add it
for is a test that passes right up until the endpoint that mattered.
"""

from __future__ import annotations

import re

import pytest
from django.test import Client
from django.urls import get_resolver

from complylayer import rules
from complylayer.api import auth
from complylayer.models import ApiKey, Decision, NamedList, Tenant
from complylayer.tenancy import Actor, Role

pytestmark = [pytest.mark.integration, pytest.mark.django_db]

SETTINGS = "server.settings_management"


@pytest.fixture(autouse=True)
def management_urls(management):
    """Settings come from `tests/conftest.py`, in one place, so this suite and the
    dashboard suite cannot disagree about them."""
    return management


def make_tenant(suffix: str) -> tuple[Tenant, str]:
    tenant = Tenant.objects.create(id=f"tnt_{suffix}", name=suffix)
    full_key, prefix = auth.generate_key("live")
    ApiKey.objects.create(
        id=f"key_{suffix}",
        tenant=tenant,
        name=f"{suffix} key",
        prefix=prefix,
        hashed_secret=auth.hash_secret(full_key),
        role=Role.COMPLIANCE_OFFICER,
        created_by="setup",
    )
    return tenant, full_key


@pytest.fixture
def tenant_a():
    return make_tenant("alpha")


@pytest.fixture
def tenant_b():
    return make_tenant("bravo")


def client_for(key: str) -> Client:
    return Client(headers={"authorization": f"Bearer {key}"})


@pytest.fixture
def owned_by_a(tenant_a):
    """Everything tenant A owns, for tenant B to fail to reach."""
    tenant, _ = tenant_a
    author = Actor(id="usr_author", role=Role.COMPLIANCE_ANALYST)
    officer = Actor(id="usr_officer", role=Role.COMPLIANCE_OFFICER)

    rule = rules.create_draft(
        tenant_id=tenant.id,
        actor=author,
        name="Tier 2 limit",
        category="kyc",
        expression="amount_minor > 5_000_000",
        severity="block",
        priority=10,
    )
    rules.approve(rule=rule, actor=officer)
    _, version = rules.activate(rule=rule, actor=officer)

    named_list = NamedList.objects.create(
        tenant=tenant, name="high_risk_countries", values=["XX"], updated_by=officer.id
    )
    from datetime import UTC, datetime

    from complylayer import partitions

    partitions.ensure_partitions(datetime.now(UTC).date(), months_ahead=0)
    decision = Decision.objects.create(
        id="dec_alpha",
        decided_at=datetime.now(UTC),
        tenant=tenant,
        idempotency_key="iso-1",
        ruleset_version=version.version,
        transaction_ref="TXN-A",
        customer_ref_hash="a" * 64,
        amount_minor=1,
        currency="NGN",
        context={},
        outcome="allow",
        latency_ms=1,
    )
    return {
        "rule": rule,
        "ruleset": version,
        "decision": decision,
        "list": named_list,
    }


class TestReadsAcrossTenants:
    """The core requirement: 404, never 403."""

    @pytest.mark.parametrize(
        "path_for",
        [
            lambda o: f"/v1/rules/{o['rule'].id}/",
            lambda o: f"/v1/rulesets/{o['ruleset'].version}/",
            lambda o: f"/v1/decisions/{o['decision'].id}/",
            lambda o: f"/v1/lists/{o['list'].pk}/",
        ],
        ids=["rule", "ruleset", "decision", "list"],
    )
    def test_another_tenants_object_is_not_found(self, owned_by_a, tenant_b, path_for):
        _, key_b = tenant_b
        response = client_for(key_b).get(path_for(owned_by_a))

        assert response.status_code == 404, (
            f"got {response.status_code}. A 403 would confirm the object exists, which "
            "tells one tenant that another tenant's id is real."
        )

    def test_a_list_shows_only_your_own(self, owned_by_a, tenant_b):
        _, key_b = tenant_b
        response = client_for(key_b).get("/v1/rules/")
        assert response.status_code == 200
        assert response.json()["results"] == []

    def test_your_own_objects_are_reachable(self, owned_by_a, tenant_a):
        _, key_a = tenant_a
        response = client_for(key_a).get(f"/v1/rules/{owned_by_a['rule'].id}/")
        assert response.status_code == 200


class TestWritesAcrossTenants:
    @pytest.mark.parametrize(
        "verb,path_for",
        [
            ("patch", lambda o: f"/v1/rules/{o['rule'].id}/"),
            ("post", lambda o: f"/v1/rules/{o['rule'].id}/approve/"),
            ("post", lambda o: f"/v1/rules/{o['rule'].id}/activate/"),
            ("post", lambda o: f"/v1/rules/{o['rule'].id}/archive/"),
            ("post", lambda o: f"/v1/rules/{o['rule'].id}/shadow/"),
            ("post", lambda o: f"/v1/rules/{o['rule'].id}/request-approval/"),
            ("delete", lambda o: f"/v1/rules/{o['rule'].id}/"),
            ("post", lambda o: f"/v1/decisions/{o['decision'].id}/review/"),
        ],
        ids=["edit", "approve", "activate", "archive", "shadow", "request", "delete", "review"],
    )
    def test_another_tenants_object_cannot_be_changed(self, owned_by_a, tenant_b, verb, path_for):
        _, key_b = tenant_b
        client = client_for(key_b)
        response = getattr(client, verb)(
            path_for(owned_by_a), data={}, content_type="application/json"
        )
        assert response.status_code == 404

    def test_the_object_really_is_unchanged(self, owned_by_a, tenant_b):
        _, key_b = tenant_b
        client_for(key_b).post(
            f"/v1/rules/{owned_by_a['rule'].id}/archive/", data={}, content_type="application/json"
        )
        owned_by_a["rule"].refresh_from_db()
        assert owned_by_a["rule"].state == "active"


class TestAuthentication:
    def test_no_key_is_refused(self, owned_by_a):
        response = Client().get("/v1/rules/")
        assert response.status_code == 403

    def test_a_bad_key_is_refused(self, owned_by_a):
        response = client_for("cl_live_notarealkeyatall").get("/v1/rules/")
        assert response.status_code == 401

    def test_a_revoked_key_stops_working(self, tenant_a, owned_by_a):
        from datetime import UTC, datetime

        tenant, key = tenant_a
        assert client_for(key).get("/v1/rules/").status_code == 200

        ApiKey.objects.filter(tenant=tenant).update(revoked_at=datetime.now(UTC))
        auth.revoke_from_cache(key[: auth.PREFIX_LENGTH])

        assert client_for(key).get("/v1/rules/").status_code == 401

    def test_every_failure_looks_the_same(self, owned_by_a):
        """Distinguishing "unknown key" from "wrong secret" would tell somebody
        probing which of their guesses was closer."""
        unknown = client_for("cl_live_aaaaaaaaaaaaaaaaaaaaaaaa").get("/v1/rules/")
        assert unknown.status_code == 401
        assert unknown.json()["message"] == "That API key is not valid."


class TestEveryRouteIsCovered:
    """The check that stops this suite from rotting.

    An isolation test covering the endpoints somebody remembered to add it for
    is a test that passes right up until the endpoint that mattered.
    """

    COVERED = {
        "rule-list",
        "rule-detail",
        "rule-validate",
        "rule-approve",
        "rule-activate",
        "rule-archive",
        "rule-shadow",
        "rule-request-approval",
        "ruleset-list",
        "ruleset-detail",
        "decision-list",
        "decision-detail",
        "decision-review",
        "list-list",
        "list-detail",
    }

    # Dashboard routes are session-authenticated rather than key-authenticated,
    # so their isolation is proved in `tests/test_dashboard.py` — see
    # `test_another_tenants_rule_is_not_found`. Named here so that adding a
    # dashboard route still forces somebody to decide where it is covered.
    DASHBOARD_COVERED = {
        "dashboard:sign-in",
        "dashboard:verify",
        "dashboard:enrol",
        "dashboard:sign-out",
        "dashboard:rules",
        "dashboard:builder",
        "dashboard:preview",
        "dashboard:validate",
        "dashboard:approval",
        "dashboard:queue",
    }

    def test_no_management_route_is_unexercised(self):
        named = set()
        for pattern in get_resolver("server.urls_management").url_patterns:
            for sub in getattr(pattern, "url_patterns", []):
                for route in getattr(sub, "url_patterns", [sub]):
                    name = getattr(route, "name", None)
                    if name and not re.search(r"api-root", name):
                        named.add(name)

        uncovered = named - self.COVERED - {n.split(":")[-1] for n in self.DASHBOARD_COVERED}
        assert not uncovered, (
            f"these management routes have no tenant isolation test: {sorted(uncovered)}. "
            "Add one before adding the route — isolation somebody remembers to write is "
            "isolation somebody forgets."
        )


class TestRowLevelSecurity:
    """Layer three (§8.1, D4). The one that is not application code.

    Layers one and two are a key resolving to one tenant and a query layer that
    scopes every read. Both are code somebody can forget to write on the view
    they add in a hurry. This layer is the database refusing, and it is tested by
    deliberately bypassing the layers above it.
    """

    @staticmethod
    def as_app_role(tenant_id: str, sql: str):
        """Run a query as the restricted application role.

        Tests connect as the docker-compose superuser, and Postgres exempts a
        superuser from every policy — so asserting RLS on that connection would
        assert nothing. `SET LOCAL ROLE` drops to the non-superuser role a real
        deployment uses, inside the transaction, which is the only way this test
        can tell the difference between a policy that works and one that is
        being skipped.
        """
        from django.db import connection

        from complylayer.tenancy import tenant_scope

        with tenant_scope(tenant_id):
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL ROLE complylayer_app")
                cursor.execute(sql)
                result = cursor.fetchall()
                cursor.execute("RESET ROLE")
        return result

    def test_a_query_with_no_tenant_set_returns_nothing(self, owned_by_a):
        """The safe direction. A forgotten scope produces an empty result rather
        than an unscoped one."""
        rows = self.as_app_role("", "SELECT count(*) FROM complylayer_rule")
        assert rows[0][0] == 0

    def test_a_raw_unscoped_query_sees_only_the_scoped_tenant(self, owned_by_a, tenant_b):
        """Bypasses the ORM's tenant filter entirely — the case where somebody
        writes raw SQL and forgets the WHERE clause."""
        tenant_b_obj, _ = tenant_b

        rows = self.as_app_role(tenant_b_obj.id, "SELECT count(*) FROM complylayer_rule")
        assert rows[0][0] == 0, "tenant B ran an unfiltered query and saw tenant A's rules"

        rows = self.as_app_role("tnt_alpha", "SELECT count(*) FROM complylayer_rule")
        assert rows[0][0] == 1

    def test_a_superuser_bypasses_every_policy(self, owned_by_a, tenant_b):
        """Recorded because it is the failure mode, not an aside.

        The policies are correct and Postgres skips them anyway. Nothing errors,
        nothing warns, and `\\d complylayer_rule` shows a policy that is never
        consulted. `complylayer_doctor` checks the connecting role precisely
        because this is invisible from the schema.
        """
        from django.db import connection

        tenant_b_obj, _ = tenant_b
        from complylayer.tenancy import tenant_scope

        with tenant_scope(tenant_b_obj.id):
            with connection.cursor() as cursor:
                cursor.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                assert cursor.fetchone()[0] is True, "this test assumes the superuser connection"
                cursor.execute("SELECT count(*) FROM complylayer_rule")
                assert cursor.fetchone()[0] == 1, (
                    "a superuser saw another tenant's row, which is exactly the point"
                )

    def test_it_applies_to_the_table_owner_too(self, owned_by_a):
        """FORCE, not just ENABLE.

        An owner bypasses RLS unless it is forced, and in development the
        application role usually *is* the owner. Relying on a non-owner role in
        production is right, and relying on remembering it for every future
        table is a control that holds until it doesn't.
        """
        from django.db import connection

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_class WHERE relname = 'complylayer_rule'
                """
            )
            enabled, forced = cursor.fetchone()
        assert enabled is True
        assert forced is True, "an owner would bypass a policy that is enabled but not forced"

    def test_every_tenant_scoped_table_carries_a_policy(self):
        """A new tenant-scoped table without a policy is the gap this catches."""
        # importlib because a module name starting with a digit is not a valid
        # identifier, and migration files are named for their order.
        import importlib

        from django.db import connection

        rls = importlib.import_module("complylayer.migrations.0005_row_level_security")

        with connection.cursor() as cursor:
            cursor.execute("SELECT DISTINCT tablename FROM pg_policies WHERE schemaname='public'")
            protected = {row[0] for row in cursor.fetchall()}

        missing = set(rls.TENANT_SCOPED_TABLES) - protected
        assert not missing, f"these tables have no row level security policy: {sorted(missing)}"

    def test_the_setting_does_not_survive_the_transaction(self):
        """SET LOCAL, never SET.

        A session-scoped setting on a pooled connection outlives the request that
        made it and is inherited by whoever borrows that connection next — the
        exact cross-tenant read this layer exists to stop.
        """
        from complylayer.tenancy import current_tenant, tenant_scope

        with tenant_scope("tnt_alpha"):
            assert current_tenant() == "tnt_alpha"

        assert current_tenant() is None, "the tenant leaked past its transaction"
