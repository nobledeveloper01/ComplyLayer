"""What an API key actually proves.

Every test here is a security review finding that was exploitable on `main`,
kept as a test because a fix without the exploit beside it is a fix nobody can
tell got undone. Each was proven by running it before it was proven by fixing it.

1. The verification cache was keyed on the key *prefix*, which is stored in the
   clear so a dashboard can display it, and a cache hit returned credentials
   without comparing the secret to anything. A 192-bit key was a 16-character
   public string for the life of every cache entry.
2. Revocation was a comment. `revoke_from_cache` had no caller outside the test
   suite, and nothing re-read `revoked_at`, so a revoked key kept working for up
   to a minute — for different lengths of time in different workers.
3. Under the role that makes row level security mean anything, the key lookup
   returned nothing at all, because `complylayer_apikey` was scoped by the
   tenant that the lookup exists to determine. Authentication and layer three
   were mutually exclusive and only the superuser configuration ever ran.
"""

from __future__ import annotations

import pytest
from django.db import connection
from django.utils import timezone

from complylayer.api import auth

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _clean_cache():
    auth.clear_cache()
    yield
    auth.clear_cache()


def issue(tenant_id: str = "tnt_bank", *, key_id: str = "key_1", role: str = "compliance_officer"):
    """A tenant with one live key. Returns (full_key, prefix)."""
    from complylayer.models import ApiKey, Tenant

    tenant, _ = Tenant.objects.get_or_create(id=tenant_id, defaults={"name": tenant_id})
    full_key, prefix = auth.generate_key("live")
    ApiKey.objects.create(
        id=key_id,
        tenant=tenant,
        name="production",
        prefix=prefix,
        hashed_secret=auth.hash_secret(full_key),
        environment="live",
        role=role,
        created_by="admin",
    )
    return full_key, prefix


@pytest.mark.django_db
class TestThePrefixIdentifiesAKeyAndDoesNotAuthenticateOne:
    def test_a_forged_key_sharing_only_the_prefix_is_refused(self):
        """The exploit, verbatim.

        The prefix is public by design — models.py stores it in the clear so a
        dashboard can show which key is which. Anyone who has seen one must not
        be able to authenticate with it.
        """
        full_key, prefix = issue()
        auth.authenticate(f"Bearer {full_key}")  # warm the cache, as real traffic does

        forged = f"Bearer {prefix}" + "X" * 40
        assert forged.removeprefix("Bearer ")[: auth.PREFIX_LENGTH] == prefix

        with pytest.raises(auth.AuthenticationFailed):
            auth.authenticate(forged)

    def test_the_real_key_still_works_from_the_cache(self):
        """The fix must not have turned the cache off, only keyed it correctly."""
        full_key, _ = issue()
        first = auth.authenticate(f"Bearer {full_key}")
        second = auth.authenticate(f"Bearer {full_key}")
        assert first == second

    def test_a_key_differing_only_in_its_last_character_is_refused(self):
        """Contrived, because the prefix is unique — but it is the property that
        was actually broken, and a unique index is not the reason it is safe.

        The substitute character is derived rather than fixed: `"Z"` produced an
        identical key whenever the real one already ended in Z, which is one run
        in sixty-four and duly failed on about the tenth.
        """
        full_key, _ = issue()
        auth.authenticate(f"Bearer {full_key}")

        altered = full_key[:-1] + ("A" if full_key[-1] != "A" else "B")
        assert altered != full_key

        with pytest.raises(auth.AuthenticationFailed):
            auth.authenticate(f"Bearer {altered}")

    def test_the_cache_is_not_keyed_on_anything_shorter_than_the_whole_key(self):
        """A guard on the shape of the fix rather than on its effect: every
        cached entry is keyed on a full-length digest."""
        full_key, prefix = issue()
        auth.authenticate(f"Bearer {full_key}")
        assert all(len(digest) == 64 for digest in auth._verified)
        assert prefix not in auth._verified


@pytest.mark.django_db
class TestRevocationTakesEffectImmediately:
    def test_a_revoked_key_stops_working_on_its_next_request(self):
        """Not when a TTL expires, and not only in the worker that revoked it."""
        from complylayer.models import ApiKey

        full_key, _ = issue()
        assert auth.authenticate(f"Bearer {full_key}").tenant_id == "tnt_bank"

        ApiKey.objects.filter(id="key_1").update(revoked_at=timezone.now())

        with pytest.raises(auth.AuthenticationFailed):
            auth.authenticate(f"Bearer {full_key}")

    def test_revocation_does_not_depend_on_the_cache_being_cleared(self):
        """The old design's mechanism. Correctness must not rest on it now."""
        from complylayer.models import ApiKey

        full_key, _ = issue()
        auth.authenticate(f"Bearer {full_key}")
        ApiKey.objects.filter(id="key_1").update(revoked_at=timezone.now())

        # Deliberately no revoke_from_cache call.
        with pytest.raises(auth.AuthenticationFailed):
            auth.authenticate(f"Bearer {full_key}")

    def test_revoke_from_cache_evicts_by_prefix(self):
        """It is no longer the mechanism, but it should still do what it says."""
        full_key, prefix = issue()
        auth.authenticate(f"Bearer {full_key}")
        assert auth._verified

        auth.revoke_from_cache(prefix)
        assert not auth._verified

    def test_a_deleted_key_stops_working(self):
        from complylayer.models import ApiKey

        full_key, _ = issue()
        auth.authenticate(f"Bearer {full_key}")
        ApiKey.objects.filter(id="key_1").delete()

        with pytest.raises(auth.AuthenticationFailed):
            auth.authenticate(f"Bearer {full_key}")


@pytest.mark.benchmark
@pytest.mark.django_db
class TestTheRowIsReadOnEveryRequestAndThatIsAffordable:
    """The tradeoff the revocation fix makes, measured rather than asserted.

    Reading `revoked_at` on every request is what makes revocation immediate. It
    costs one indexed lookup on a unique column. The Argon2id verification —
    which is the part worth caching, and the only part still cached — costs two
    orders of magnitude more.
    """

    def test_cached_authentication_stays_inside_the_auth_budget(self):
        import statistics
        import time

        full_key, _ = issue()
        header = f"Bearer {full_key}"
        auth.authenticate(header)

        timings = []
        for _ in range(200):
            start = time.perf_counter()
            auth.authenticate(header)
            timings.append((time.perf_counter() - start) * 1000)
        timings.sort()

        p99 = timings[int(len(timings) * 0.99)]
        print(f"\ncached auth p50={statistics.median(timings):.3f}ms p99={p99:.3f}ms")
        assert p99 < 3.0, f"authentication p99 {p99:.3f}ms exceeds §4.2's 3 ms budget"

    def test_the_cache_is_still_earning_its_place(self):
        """If this ever stops being a large number, the cache can go and the
        prefix-keying bug it caused goes with it."""
        import time

        full_key, _ = issue()
        header = f"Bearer {full_key}"

        auth.clear_cache()
        start = time.perf_counter()
        auth.authenticate(header)
        cold = (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        auth.authenticate(header)
        warm = (time.perf_counter() - start) * 1000

        print(f"\nuncached {cold:.1f}ms, cached {warm:.3f}ms, saved {cold - warm:.1f}ms")
        assert cold > warm * 10


@pytest.mark.django_db
class TestAuthenticationSurvivesRowLevelSecurity:
    """The configuration the doctor recommends, which had never been run.

    `SET ROLE` rather than a second connection: it is the same thing Postgres
    checks policies against, and it keeps the test to one database.

    Deliberately not `transaction=True`. That teardown flushes with TRUNCATE,
    which `complylayer_audit_no_truncate` refuses — correctly, since that is the
    trigger's whole job — so the rollback never happens and rows leak into the
    next test. Inside the wrapping transaction `SET ROLE` is checked against
    exactly the same policies.
    """

    def test_a_key_resolves_as_the_application_role(self):
        full_key, _ = issue()

        with connection.cursor() as cursor:
            cursor.execute("SET ROLE complylayer_app")
            try:
                credentials = auth.authenticate(f"Bearer {full_key}")
                assert credentials.tenant_id == "tnt_bank"
            finally:
                cursor.execute("RESET ROLE")

    def test_the_resolution_policy_is_a_door_and_not_a_hole(self):
        """The narrow exemption must not make the table generally readable.

        An ordinary query with no tenant set still sees nothing — otherwise the
        fix for the bootstrap problem would have removed layer three instead of
        working with it.
        """
        issue("tnt_a", key_id="key_a")
        issue("tnt_b", key_id="key_b")

        with connection.cursor() as cursor:
            cursor.execute("SET ROLE complylayer_app")
            try:
                cursor.execute("SELECT count(*) FROM complylayer_apikey")
                assert cursor.fetchone()[0] == 0
            finally:
                cursor.execute("RESET ROLE")

    def test_the_resolver_returns_one_tenants_key_and_not_anothers(self):
        full_a, prefix_a = issue("tnt_a", key_id="key_a")
        issue("tnt_b", key_id="key_b")

        with connection.cursor() as cursor:
            cursor.execute("SET ROLE complylayer_app")
            try:
                cursor.execute(auth.RESOLVE_SQL, [prefix_a])
                rows = cursor.fetchall()
                assert len(rows) == 1
                assert rows[0][1] == "tnt_a"
            finally:
                cursor.execute("RESET ROLE")

        assert auth.authenticate(f"Bearer {full_a}").tenant_id == "tnt_a"
