"""The sign-in throttle, and the arithmetic that makes it worth having.

Before this, `/dashboard/verify` accepted unlimited guesses at a six-digit code
that `valid_window=1` makes three-in-a-million per attempt. An attacker holding
a stolen password expected to be inside within the hour, and no counter, log or
alert anywhere would have recorded the attempt.
"""

from __future__ import annotations

import pytest
from django.test import Client

from complylayer.dashboard import throttle


class FakeRedis:
    """Enough Redis for a counter with a TTL.

    Real Redis is exercised by the view tests below; this makes the backoff
    arithmetic assertable without waiting fifteen real minutes.
    """

    def __init__(self, broken: bool = False):
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.broken = broken

    def _check(self):
        if self.broken:
            raise ConnectionError("Connection refused")

    def get(self, key):
        self._check()
        return self.values.get(key)

    def incr(self, key):
        self._check()
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    def expire(self, key, seconds):
        self._check()
        self.ttls[key] = seconds

    def ttl(self, key):
        self._check()
        return self.ttls.get(key, -2)

    def delete(self, key):
        self._check()
        self.values.pop(key, None)
        self.ttls.pop(key, None)

    def set(self, key, value, nx=False, ex=None):
        self._check()
        if nx and key in self.values:
            return None
        self.values[key] = value
        return True


class TestBackoff:
    def test_the_first_failures_are_free(self):
        """A person mistyping a code, or a phone clock a few seconds out, is not
        an attacker and must not be treated as one."""
        client = FakeRedis()
        for _ in range(throttle.FREE_ATTEMPTS):
            throttle.record_failure(client, "second-factor", "user-1")
        assert throttle.lockout_seconds(client, "second-factor", "user-1") == 0

    def test_the_next_failure_starts_the_wait(self):
        client = FakeRedis()
        for _ in range(throttle.FREE_ATTEMPTS + 1):
            throttle.record_failure(client, "second-factor", "user-1")
        assert throttle.lockout_seconds(client, "second-factor", "user-1") > 0

    def test_the_wait_doubles(self):
        client = FakeRedis()
        waits = []
        for _ in range(throttle.FREE_ATTEMPTS + 4):
            throttle.record_failure(client, "second-factor", "user-1")
            waits.append(throttle.lockout_seconds(client, "second-factor", "user-1"))

        growing = [w for w in waits if w > 0]
        assert growing == sorted(growing)
        assert growing[1] == growing[0] * 2

    def test_the_wait_is_capped(self):
        """A forgotten authenticator must not become an all-day lockout."""
        client = FakeRedis()
        for _ in range(40):
            throttle.record_failure(client, "second-factor", "user-1")
        assert throttle.lockout_seconds(client, "second-factor", "user-1") == (
            throttle.MAX_LOCKOUT_SECONDS
        )

    def test_success_clears_the_counter(self):
        client = FakeRedis()
        for _ in range(throttle.FREE_ATTEMPTS + 3):
            throttle.record_failure(client, "second-factor", "user-1")
        throttle.clear(client, "second-factor", "user-1")
        assert throttle.lockout_seconds(client, "second-factor", "user-1") == 0

    def test_one_account_locking_does_not_lock_another(self):
        client = FakeRedis()
        for _ in range(throttle.FREE_ATTEMPTS + 3):
            throttle.record_failure(client, "second-factor", "user-1")
        assert throttle.lockout_seconds(client, "second-factor", "user-2") == 0

    def test_the_scopes_are_separate(self):
        """Failing the password is not failing the second factor."""
        client = FakeRedis()
        for _ in range(throttle.FREE_ATTEMPTS + 3):
            throttle.record_failure(client, "sign-in", "user-1")
        assert throttle.lockout_seconds(client, "second-factor", "user-1") == 0

    def test_the_identity_is_not_stored_in_the_clear(self):
        """These keys hold email addresses, and Redis is somewhere people run
        `KEYS *` during an incident."""
        client = FakeRedis()
        throttle.record_failure(client, "sign-in", "adaeze@example.com")
        assert not any("adaeze@example.com" in key for key in client.values)

    def test_the_arithmetic_actually_defeats_a_search(self):
        """The point of the whole module, computed rather than asserted.

        Three valid codes in a million, so even odds need about 231,000 guesses.
        Unthrottled at 200 requests a second that is nineteen minutes. At the
        cap — five guesses per fifteen-minute lockout, twenty an hour — it is
        about fifteen months.

        Written down because the first version of this test claimed centuries
        and was wrong by two orders of magnitude, and the docstrings quoting it
        were wrong with it. A security control described by a number nobody
        checked is the thing this project keeps finding.
        """
        import math

        attempts = math.log(2) / (3 / 1_000_000)
        assert 200_000 < attempts < 260_000

        unthrottled_minutes = attempts / 200 / 60
        assert unthrottled_minutes < 30, "the problem, if nothing throttles"

        per_hour = throttle.FREE_ATTEMPTS * (3600 / throttle.MAX_LOCKOUT_SECONDS)
        assert per_hour == 20

        months = attempts / per_hour / 24 / 30
        assert months > 12, f"only {months:.1f} months of guessing"


class TestReplay:
    def test_a_code_works_once(self):
        client = FakeRedis()
        assert throttle.consume_code(client, "user-1", "123456") is True
        assert throttle.consume_code(client, "user-1", "123456") is False

    def test_a_different_code_is_unaffected(self):
        client = FakeRedis()
        throttle.consume_code(client, "user-1", "123456")
        assert throttle.consume_code(client, "user-1", "654321") is True

    def test_the_same_code_for_another_account_is_unaffected(self):
        """Two people can legitimately hold the same six digits at once."""
        client = FakeRedis()
        throttle.consume_code(client, "user-1", "123456")
        assert throttle.consume_code(client, "user-2", "123456") is True


class TestRedisBeingDown:
    """Fails open, deliberately, and the module docstring says why.

    Failing closed locks every compliance officer out of the approval queue
    during an infrastructure outage, including the people responding to it. This
    endpoint is not the money path, and the exposure is one small window.
    """

    def test_a_dead_redis_does_not_lock_anybody_out(self):
        client = FakeRedis(broken=True)
        assert throttle.lockout_seconds(client, "second-factor", "user-1") == 0

    def test_recording_a_failure_does_not_raise(self):
        assert throttle.record_failure(FakeRedis(broken=True), "second-factor", "user-1") == 0

    def test_clearing_does_not_raise(self):
        throttle.clear(FakeRedis(broken=True), "second-factor", "user-1")

    def test_the_replay_guard_lets_the_code_through(self):
        assert throttle.consume_code(FakeRedis(broken=True), "user-1", "123456") is True

    def test_no_client_at_all_is_the_same_as_a_dead_one(self):
        assert throttle.lockout_seconds(None, "second-factor", "user-1") == 0
        assert throttle.consume_code(None, "user-1", "123456") is True


class TestTheMessage:
    def test_seconds_are_reported_as_seconds(self):
        assert "seconds" in throttle.wait_message(30)

    def test_minutes_are_reported_as_minutes(self):
        assert "minutes" in throttle.wait_message(600)

    def test_one_minute_is_singular(self):
        assert "1 minute." in throttle.wait_message(60)

    def test_it_says_how_long_rather_than_try_again_later(self):
        """ "Try again later" is the sentence that generates a support ticket."""
        assert "later" not in throttle.wait_message(90)


@pytest.mark.django_db
@pytest.mark.integration
class TestThroughTheViews:
    """Against real Redis and the real views, because the arithmetic being right
    is not the same as it being wired in."""

    @pytest.fixture(autouse=True)
    def management_settings(self, management):
        return management

    @pytest.fixture(autouse=True)
    def clean_redis(self):
        import redis
        from django.conf import settings

        client = redis.Redis.from_url(settings.COMPLYLAYER["REDIS_URL"])
        for pattern in ("cl:throttle:*", "cl:otp:*"):
            keys = list(client.scan_iter(pattern))
            if keys:
                client.delete(*keys)
        yield

    @pytest.fixture
    def officer(self):
        from datetime import UTC, datetime

        import pyotp
        from django.contrib.auth.models import User

        from complylayer.models import DashboardUser, Tenant

        tenant = Tenant.objects.create(id="tnt_thr", name="Throttle")
        user = User.objects.create_user(
            username="thr@example.com", email="thr@example.com", password="correct-horse"
        )
        secret = pyotp.random_base32()
        profile = DashboardUser.objects.create(
            user=user,
            tenant=tenant,
            role="compliance_officer",
            totp_secret=secret,
            totp_confirmed_at=datetime.now(UTC),
        )
        return profile, secret

    def test_guessing_the_second_factor_stops_being_free(self, client, officer):
        """The exploit the finding described, run against the real endpoint."""
        _profile, _ = officer
        client.post("/dashboard/sign-in", {"email": "thr@example.com", "password": "correct-horse"})

        statuses = []
        for attempt in range(throttle.FREE_ATTEMPTS + 3):
            response = client.post("/dashboard/verify", {"code": f"{attempt:06d}"})
            statuses.append(response.status_code)

        assert 429 in statuses, "unlimited guessing is still possible"
        assert statuses[-1] == 429

    def test_guessing_the_password_stops_being_free_too(self, client, officer):
        statuses = []
        for attempt in range(throttle.FREE_ATTEMPTS + 3):
            response = client.post(
                "/dashboard/sign-in", {"email": "thr@example.com", "password": f"wrong-{attempt}"}
            )
            statuses.append(response.status_code)

        assert statuses[-1] == 429

    def test_a_correct_code_still_signs_in(self, client, officer):
        """The control must not have broken the thing it protects."""
        import pyotp

        _profile, secret = officer
        client.post("/dashboard/sign-in", {"email": "thr@example.com", "password": "correct-horse"})
        response = client.post("/dashboard/verify", {"code": pyotp.TOTP(secret).now()})

        assert response.status_code == 302
        assert client.get("/dashboard/").status_code == 200

    def test_a_used_code_cannot_be_used_again(self, client, officer):
        """A code stays valid for its whole window, so one seen over a shoulder
        works again until the window rolls."""
        import pyotp

        _profile, secret = officer
        code = pyotp.TOTP(secret).now()

        client.post("/dashboard/sign-in", {"email": "thr@example.com", "password": "correct-horse"})
        assert client.post("/dashboard/verify", {"code": code}).status_code == 302

        second = Client()
        second.post("/dashboard/sign-in", {"email": "thr@example.com", "password": "correct-horse"})
        assert second.post("/dashboard/verify", {"code": code}).status_code == 401

    def test_a_wrong_password_then_the_right_one_works(self, client, officer):
        """Below the threshold, nothing changes for a real person."""
        client.post("/dashboard/sign-in", {"email": "thr@example.com", "password": "wrong"})
        response = client.post(
            "/dashboard/sign-in", {"email": "thr@example.com", "password": "correct-horse"}
        )
        assert response.status_code == 302
