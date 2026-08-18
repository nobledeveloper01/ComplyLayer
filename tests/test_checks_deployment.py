"""The two preflight checks that guard published defaults, and the boot refusal.

Both findings came out of the phase 2-8 security review and share a shape: a
value with a development default that nothing ever objected to, in a product
where the consequence is silent.

`SECRET_KEY` signs the session cookie, and the dashboard's second-factor flag
lives inside that session — so on the published default a forged cookie is a
complete sign-in with both factors, while every probe reports healthy.

`CUSTOMER_SALT` is the HMAC key pseudonymising customer references. It used to
fall back to the tenant id, which is a column on every row it protects.
"""

from __future__ import annotations

import pytest
from django.core.exceptions import ImproperlyConfigured

from complylayer import checks

INSECURE_KEY = "insecure-development-key-do-not-deploy"
INSECURE_SALT = "insecure-development-salt-do-not-deploy"


def secrets_check(key=INSECURE_KEY, salt=INSECURE_SALT, debug=False):
    return checks.check_deployment_secrets(
        key,
        salt,
        debug,
        insecure_secret_key=INSECURE_KEY,
        insecure_customer_salt=INSECURE_SALT,
    )


class TestDeploymentSecrets:
    def test_both_set_passes(self):
        result = secrets_check("a-real-key", "a-real-salt")
        assert result.ok

    def test_the_published_signing_key_is_fatal_with_debug_off(self):
        result = secrets_check(salt="a-real-salt")
        assert not result.ok
        assert result.fatal
        assert "COMPLYLAYER_SECRET_KEY" in result.detail

    def test_the_published_salt_is_fatal_too(self):
        result = secrets_check(key="a-real-key")
        assert not result.ok
        assert result.fatal
        assert "COMPLYLAYER_CUSTOMER_SALT" in result.detail

    def test_both_are_named_when_both_are_unset(self):
        result = secrets_check()
        assert "COMPLYLAYER_SECRET_KEY" in result.detail
        assert "COMPLYLAYER_CUSTOMER_SALT" in result.detail

    def test_a_laptop_gets_a_warning_rather_than_a_failure(self):
        """DEBUG on is a development machine, and blocking one helps nobody.
        `--strict` promotes it, which is what a deploy pipeline runs."""
        result = secrets_check(debug=True)
        assert not result.ok
        assert not result.fatal

    def test_the_remediation_says_how_to_generate_them(self):
        result = secrets_check()
        assert "token_urlsafe" in result.remediation

    def test_the_remediation_warns_that_the_salt_cannot_be_rotated_freely(self):
        """Changing it re-pseudonymises every future decision, so history stops
        joining to the new value. Somebody has to be told before, not after."""
        assert "history" in secrets_check().remediation


class TestTransportSecurity:
    def test_all_set_passes(self):
        assert checks.check_transport_security(True, True, 31_536_000, False).ok

    def test_a_session_cookie_without_secure_is_reported(self):
        result = checks.check_transport_security(False, True, 31_536_000, False)
        assert not result.ok
        assert "SESSION_COOKIE_SECURE" in result.detail

    def test_the_csrf_cookie_counts_too(self):
        result = checks.check_transport_security(True, False, 31_536_000, False)
        assert "CSRF_COOKIE_SECURE" in result.detail

    def test_missing_hsts_is_reported(self):
        result = checks.check_transport_security(True, True, 0, False)
        assert "SECURE_HSTS_SECONDS" in result.detail

    def test_debug_downgrades_it_to_a_warning(self):
        result = checks.check_transport_security(False, False, 0, True)
        assert not result.ok
        assert not result.fatal


class TestTheManagementWorkloadIsConfiguredThatWay:
    """Asserted against the real module, not against a copy of it.

    `tests/conftest.py` used to restate these settings, so anything added to
    `server/settings_management.py` was invisible to every test.
    """

    def test_the_session_cookie_is_https_only_outside_debug(self):
        from server import settings_management

        assert settings_management.SESSION_COOKIE_SECURE is True
        assert settings_management.CSRF_COOKIE_SECURE is True

    def test_hsts_is_a_year_and_covers_subdomains(self):
        from server import settings_management

        assert settings_management.SECURE_HSTS_SECONDS == 31_536_000
        assert settings_management.SECURE_HSTS_INCLUDE_SUBDOMAINS is True

    def test_the_proxy_header_is_trusted_so_django_knows_it_is_behind_tls(self):
        from server import settings_management

        assert settings_management.SECURE_PROXY_SSL_HEADER == (
            "HTTP_X_FORWARDED_PROTO",
            "https",
        )

    def test_the_earlier_flags_are_still_there(self):
        """The regression this pair sits beside: four flags were set and the
        fifth was not, which read as a decision."""
        from server import settings_management

        assert settings_management.SESSION_COOKIE_HTTPONLY is True
        assert settings_management.SESSION_COOKIE_SAMESITE == "Lax"


class TestTheServerRefusesToStartOnPublishedSecrets:
    """The stop, as opposed to the report.

    In `server/boot.py`, called at import by `server/asgi.py` rather than sitting
    in settings: settings are imported by every test and every management
    command, while asgi is imported by one thing — a process about to take
    traffic.
    """

    def test_it_refuses_when_the_signing_key_is_the_published_one(self, settings):
        from server.boot import refuse_development_secrets

        settings.DEBUG = False
        settings.SECRET_KEY = settings.INSECURE_SECRET_KEY
        settings.COMPLYLAYER = {**settings.COMPLYLAYER, "CUSTOMER_SALT": "a-real-salt"}

        with pytest.raises(ImproperlyConfigured, match="COMPLYLAYER_SECRET_KEY"):
            refuse_development_secrets()

    def test_it_refuses_when_the_salt_is_the_published_one(self, settings):
        from server.boot import refuse_development_secrets

        settings.DEBUG = False
        settings.SECRET_KEY = "a-real-key"
        settings.COMPLYLAYER = {
            **settings.COMPLYLAYER,
            "CUSTOMER_SALT": settings.INSECURE_CUSTOMER_SALT,
        }

        with pytest.raises(ImproperlyConfigured, match="COMPLYLAYER_CUSTOMER_SALT"):
            refuse_development_secrets()

    def test_it_starts_when_both_are_set(self, settings):
        from server.boot import refuse_development_secrets

        settings.DEBUG = False
        settings.SECRET_KEY = "a-real-key"
        settings.COMPLYLAYER = {**settings.COMPLYLAYER, "CUSTOMER_SALT": "a-real-salt"}

        refuse_development_secrets()

    def test_debug_is_allowed_to_run_on_them(self, settings):
        """A laptop, where the whole point is not having to set anything."""
        from server.boot import refuse_development_secrets

        settings.DEBUG = True
        settings.SECRET_KEY = settings.INSECURE_SECRET_KEY
        settings.COMPLYLAYER = {
            **settings.COMPLYLAYER,
            "CUSTOMER_SALT": settings.INSECURE_CUSTOMER_SALT,
        }

        refuse_development_secrets()

    def test_the_message_says_what_to_do(self, settings):
        from server.boot import refuse_development_secrets

        settings.DEBUG = False
        settings.SECRET_KEY = settings.INSECURE_SECRET_KEY

        with pytest.raises(ImproperlyConfigured) as raised:
            refuse_development_secrets()
        assert "token_urlsafe" in str(raised.value)


class TestTransportSecurityKnowsWhichWorkloadItIsChecking:
    """A preflight that cries wolf is one people learn to skim."""

    def test_a_workload_with_no_session_is_not_told_to_set_cookie_flags(self):
        result = checks.check_transport_security(False, False, 0, False, serves_sessions=False)
        assert result.ok
        assert "no session cookie" in result.detail

    def test_a_workload_with_a_session_still_is(self):
        result = checks.check_transport_security(False, False, 0, False, serves_sessions=True)
        assert not result.ok
