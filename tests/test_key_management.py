"""Issuing and revoking API keys through the product rather than through psql.

This endpoint exists because the security review found that revocation was a
comment: `revoked_at` was a column, nothing set it, and the README claimed keys
could be revoked. The fix for the *mechanism* landed first — the row is read on
every authentication, so a revoked key stops on its next request — but there was
still no way to revoke one without a database client.

The interesting part is not create and revoke. It is that a key-management
endpoint is a privilege escalation waiting to happen: a key that may issue keys
can mint itself a more powerful one unless something stops it.
"""

from __future__ import annotations

import pytest
from django.test import Client

from complylayer.api import auth
from complylayer.models import ApiKey, Tenant
from complylayer.tenancy import PERMISSIONS, Action, Role, may_issue

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def management_settings(management):
    return management


def issue_key(tenant, role: Role, key_id: str = "key_root") -> str:
    full_key, prefix = auth.generate_key("live")
    ApiKey.objects.create(
        id=key_id,
        tenant=tenant,
        name=f"{role} key",
        prefix=prefix,
        hashed_secret=auth.hash_secret(full_key),
        environment="live",
        role=str(role),
        created_by="setup",
    )
    return full_key


@pytest.fixture
def tenant():
    return Tenant.objects.create(id="tnt_keys", name="Keys")


def client_for(key: str) -> Client:
    return Client(headers={"authorization": f"Bearer {key}"})


class TestIssuing:
    def test_the_secret_is_returned_once_and_never_again(self, tenant):
        key = issue_key(tenant, Role.COMPLIANCE_OFFICER)
        client = client_for(key)

        created = client.post(
            "/v1/keys/",
            {"name": "ledger service", "role": "engineer", "environment": "live"},
            content_type="application/json",
        )
        assert created.status_code == 201
        secret = created.json()["secret"]
        assert secret.startswith("cl_live_")

        listed = client.get("/v1/keys/").json()
        rows = listed["results"] if isinstance(listed, dict) else listed
        assert all("secret" not in row for row in rows), "a list must never carry a secret"

        one = client.get(f"/v1/keys/{created.json()['id']}/").json()
        assert "secret" not in one
        assert "hashed_secret" not in one

    def test_the_returned_secret_actually_authenticates(self, tenant):
        """A key nobody can use is not a key. This is the whole round trip."""
        key = issue_key(tenant, Role.COMPLIANCE_OFFICER)
        created = client_for(key).post(
            "/v1/keys/",
            {"name": "ledger service", "role": "engineer"},
            content_type="application/json",
        )
        secret = created.json()["secret"]

        auth.clear_cache()
        credentials = auth.authenticate(f"Bearer {secret}")
        assert credentials.tenant_id == tenant.id
        assert credentials.actor.role == Role.ENGINEER

    def test_issuing_is_recorded(self, tenant):
        from complylayer.models import AuditRecord

        key = issue_key(tenant, Role.COMPLIANCE_OFFICER)
        client_for(key).post(
            "/v1/keys/", {"name": "ledger", "role": "engineer"}, content_type="application/json"
        )

        record = AuditRecord.objects.filter(event_type="apikey.issued").first()
        assert record is not None
        assert "secret" not in str(record.payload), "the audit trail must not hold a credential"
        assert record.payload["role"] == "engineer"

    def test_a_role_that_does_not_exist_is_refused(self, tenant):
        key = issue_key(tenant, Role.COMPLIANCE_OFFICER)
        response = client_for(key).post(
            "/v1/keys/", {"name": "x", "role": "superuser"}, content_type="application/json"
        )
        assert response.status_code == 400


class TestAKeyCannotMintAMorePowerfulKey:
    """The reason this endpoint is safe to have at all."""

    def test_an_engineer_cannot_issue_a_compliance_officer_key(self, tenant):
        """Otherwise an integration credential is one request away from being
        able to activate compliance rules."""
        key = issue_key(tenant, Role.ENGINEER)
        response = client_for(key).post(
            "/v1/keys/",
            {"name": "escalation", "role": "compliance_officer"},
            content_type="application/json",
        )
        assert response.status_code == 403
        assert "issuer does not have" in response.json()["message"]

    def test_an_engineer_may_issue_another_engineer_key(self, tenant):
        key = issue_key(tenant, Role.ENGINEER)
        response = client_for(key).post(
            "/v1/keys/", {"name": "second", "role": "engineer"}, content_type="application/json"
        )
        assert response.status_code == 201

    def test_an_analyst_cannot_issue_anything(self, tenant):
        """A compliance analyst has no MANAGE_KEYS at all."""
        key = issue_key(tenant, Role.COMPLIANCE_ANALYST)
        response = client_for(key).post(
            "/v1/keys/", {"name": "x", "role": "auditor"}, content_type="application/json"
        )
        assert response.status_code == 403

    def test_the_rule_is_subset_rather_than_seniority(self):
        """Roles are not ranked — an engineer is not below an auditor, they do
        different things. Comparing rank would be meaningless, and a separate
        who-may-issue-what table would drift from the permission matrix."""
        # A risk manager holds everything a compliance officer does, plus the
        # emergency override, so it may issue one. The reverse must not hold —
        # that single extra permission is the whole difference, and a compliance
        # officer minting a risk manager key would hand itself the override that
        # §11.4 pages the risk lead about.
        assert may_issue(Role.RISK_MANAGER, Role.COMPLIANCE_OFFICER) is True
        assert may_issue(Role.COMPLIANCE_OFFICER, Role.RISK_MANAGER) is False

        # And an auditor, who may only read, is issuable by anyone and may issue
        # nothing but its own kind.
        assert may_issue(Role.COMPLIANCE_OFFICER, Role.AUDITOR) is True
        assert may_issue(Role.AUDITOR, Role.COMPLIANCE_OFFICER) is False
        for role in Role:
            assert may_issue(role, role), f"{role} must be able to issue its own kind"

    def test_every_role_that_may_issue_is_checked_against_the_matrix(self):
        """A guard on the guard: if a role gains MANAGE_KEYS, the subset rule
        still has to hold for every target."""
        issuers = [role for role in Role if Action.MANAGE_KEYS in PERMISSIONS[role]]
        assert issuers, "somebody removed MANAGE_KEYS from every role"
        for issuer in issuers:
            for target in Role:
                expected = PERMISSIONS[target] <= PERMISSIONS[issuer]
                assert may_issue(issuer, target) is expected


class TestRevoking:
    def test_a_revoked_key_stops_authenticating(self, tenant):
        key = issue_key(tenant, Role.COMPLIANCE_OFFICER)
        created = (
            client_for(key)
            .post(
                "/v1/keys/", {"name": "doomed", "role": "engineer"}, content_type="application/json"
            )
            .json()
        )

        auth.clear_cache()
        assert auth.authenticate(f"Bearer {created['secret']}").tenant_id == tenant.id

        response = client_for(key).post(f"/v1/keys/{created['id']}/revoke/")
        assert response.status_code == 200
        assert response.json()["active"] is False

        with pytest.raises(auth.AuthenticationFailed):
            auth.authenticate(f"Bearer {created['secret']}")

    def test_revoking_twice_is_refused_rather_than_silently_fine(self, tenant):
        """Two revocations look like two incidents in the audit trail."""
        key = issue_key(tenant, Role.COMPLIANCE_OFFICER)
        created = (
            client_for(key)
            .post(
                "/v1/keys/", {"name": "doomed", "role": "engineer"}, content_type="application/json"
            )
            .json()
        )

        assert client_for(key).post(f"/v1/keys/{created['id']}/revoke/").status_code == 200
        assert client_for(key).post(f"/v1/keys/{created['id']}/revoke/").status_code == 409

    def test_revoking_is_recorded_with_its_reason(self, tenant):
        from complylayer.models import AuditRecord

        key = issue_key(tenant, Role.COMPLIANCE_OFFICER)
        created = (
            client_for(key)
            .post(
                "/v1/keys/", {"name": "leaked", "role": "engineer"}, content_type="application/json"
            )
            .json()
        )
        client_for(key).post(
            f"/v1/keys/{created['id']}/revoke/",
            {"reason": "posted in a support ticket"},
            content_type="application/json",
        )

        record = AuditRecord.objects.filter(event_type="apikey.revoked").first()
        assert record.payload["reason"] == "posted in a support ticket"

    def test_a_key_is_never_deleted(self, tenant):
        """Decisions reference the key that made them. Deleting it makes six
        months of audit history unattributable."""
        key = issue_key(tenant, Role.COMPLIANCE_OFFICER)
        created = (
            client_for(key)
            .post("/v1/keys/", {"name": "x", "role": "engineer"}, content_type="application/json")
            .json()
        )

        assert client_for(key).delete(f"/v1/keys/{created['id']}/").status_code == 405
        assert ApiKey.objects.filter(id=created["id"]).exists()

    def test_a_key_cannot_be_re_pointed_at_another_role(self, tenant):
        key = issue_key(tenant, Role.COMPLIANCE_OFFICER)
        created = (
            client_for(key)
            .post("/v1/keys/", {"name": "x", "role": "engineer"}, content_type="application/json")
            .json()
        )

        response = client_for(key).patch(
            f"/v1/keys/{created['id']}/",
            {"role": "compliance_officer"},
            content_type="application/json",
        )
        assert response.status_code == 405
        assert ApiKey.objects.get(id=created["id"]).role == "engineer"


class TestKeysAreTenantScopedLikeEverythingElse:
    def test_another_tenants_key_is_a_404_not_a_403(self, tenant):
        other = Tenant.objects.create(id="tnt_other", name="Other")
        theirs = ApiKey.objects.create(
            id="key_theirs",
            tenant=other,
            name="theirs",
            prefix="cl_live_zzzzzzzz",
            hashed_secret=auth.hash_secret("cl_live_zzzzzzzzOTHER"),
            environment="live",
            role=str(Role.ENGINEER),
            created_by="setup",
        )

        key = issue_key(tenant, Role.COMPLIANCE_OFFICER)
        assert client_for(key).get(f"/v1/keys/{theirs.id}/").status_code == 404
        assert client_for(key).post(f"/v1/keys/{theirs.id}/revoke/").status_code == 404
        assert ApiKey.objects.get(id="key_theirs").revoked_at is None

    def test_a_list_shows_only_this_tenants_keys(self, tenant):
        Tenant.objects.create(id="tnt_other2", name="Other")
        ApiKey.objects.create(
            id="key_other2",
            tenant_id="tnt_other2",
            name="theirs",
            prefix="cl_live_yyyyyyyy",
            hashed_secret=auth.hash_secret("cl_live_yyyyyyyyOTHER"),
            environment="live",
            role=str(Role.ENGINEER),
            created_by="setup",
        )
        key = issue_key(tenant, Role.COMPLIANCE_OFFICER)

        listed = client_for(key).get("/v1/keys/").json()
        rows = listed["results"] if isinstance(listed, dict) else listed
        assert {row["id"] for row in rows} == {"key_root"}
