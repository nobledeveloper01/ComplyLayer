"""Signing in to the dashboard.

Password then TOTP, in two steps, with the session marked at each. §8.2 asks for
email plus TOTP or OIDC; this is the first, and OIDC slots in beside it because
both end at the same place — a request that knows which person is acting and for
which tenant.

**The second factor is not optional.** A user without a confirmed authenticator
is sent to enrolment and can reach nothing else. Offering it as a setting would
mean the people most likely to skip it are the ones whose approval carries the
most weight.

**Passing the password is not being signed in.** The session records the two
steps separately, so a stolen password on its own reaches the TOTP prompt and
stops there. `SESSION_FACTOR_KEY` is set only after a verified code, and every
view checks that rather than `request.user.is_authenticated`.
"""

from __future__ import annotations

import functools
from datetime import UTC, datetime

import pyotp
from django.contrib.auth import authenticate as django_authenticate
from django.contrib.auth import login as django_login
from django.contrib.auth import logout as django_logout
from django.db import connection
from django.shortcuts import redirect

from complylayer.tenancy import tenant_scope

SESSION_FACTOR_KEY = "complylayer_second_factor"
ISSUER = "ComplyLayer"


class SignInFailed(Exception):
    """One message for every failure.

    Wrong password, unknown email, no profile — all the same. Telling somebody
    which half they got right is telling them half of what they need.
    """


class AlreadyEnrolled(Exception):
    """Enrolment was attempted for an account that already has a second factor.

    Raised rather than handled quietly, because the request that triggers it is
    either a bug or somebody holding a stolen password. See `begin_enrolment`.
    """


def start_session(request, email: str, password: str):
    """Step one. Returns the profile; does not grant access on its own."""
    from complylayer.models import DashboardUser

    user = django_authenticate(request, username=email, password=password)
    if user is None:
        raise SignInFailed("That email address and password do not match.")

    resolved = _resolve(user.pk)
    profile = None
    if resolved is not None:
        profile_id, tenant_id = resolved
        with tenant_scope(tenant_id):
            profile = (
                DashboardUser.objects.filter(pk=profile_id).select_related("tenant", "user").first()
            )
    if profile is None:
        # A Django user with no ComplyLayer profile has no tenant, so there is
        # nothing they could be authorised to see.
        raise SignInFailed("That email address and password do not match.")

    django_login(request, user)
    request.session[SESSION_FACTOR_KEY] = False
    return profile


def verify_second_factor(request, profile, code: str) -> bool:
    """Step two. Only this sets the session flag every view checks.

    `valid_window=1` accepts the adjacent 30-second step, because a phone clock
    a few seconds out is not an attacker and locking somebody out of the
    approval queue over it helps nobody.
    """
    if not profile.totp_secret:
        return False

    if not pyotp.TOTP(profile.totp_secret).verify(code, valid_window=1):
        return False

    request.session[SESSION_FACTOR_KEY] = True
    request.session.cycle_key()  # new session id once the factors are complete
    return True


def begin_enrolment(profile) -> tuple[str, str]:
    """Return (secret, provisioning URI) for an authenticator app.

    The secret is stored but not confirmed. Until a code verifies against it the
    account has no second factor, so a half-finished enrolment cannot be mistaken
    for a complete one.

    **Refuses an account that already has a confirmed factor.** This used to
    issue a fresh secret to anybody who asked, and the enrolment page asked on
    every render — so a session holding only a stolen password could walk to
    `/dashboard/enrol`, be handed a new secret, confirm it from its own
    authenticator and be signed in as a compliance officer. It also destroyed
    the real owner's factor on the way past. Proven end to end before it was
    changed; `tests/test_dashboard.py` keeps the exploit as a test.

    Re-enrolling a lost device is a recovery flow that has to re-establish who
    is asking. It is not something one factor may do to itself.

    An unconfirmed secret already on the profile is reused rather than replaced,
    so reloading the page does not invalidate the entry somebody is part-way
    through typing into their authenticator.
    """
    if profile.has_second_factor:
        raise AlreadyEnrolled(
            "this account already has an authenticator; "
            "replacing it is a recovery flow, not an enrolment"
        )

    secret = profile.totp_secret or pyotp.random_base32()
    if profile.totp_secret != secret or profile.totp_confirmed_at is not None:
        profile.totp_secret = secret
        profile.totp_confirmed_at = None
        # Scoped because the row level security policy's WITH CHECK refuses a
        # write with no tenant set, and enrolment runs before any view has
        # established one.
        with tenant_scope(profile.tenant_id):
            profile.save(update_fields=["totp_secret", "totp_confirmed_at"])

    uri = pyotp.TOTP(secret).provisioning_uri(name=profile.user.email, issuer_name=ISSUER)
    return secret, uri


def confirm_enrolment(profile, code: str) -> bool:
    if not profile.totp_secret or not pyotp.TOTP(profile.totp_secret).verify(code, valid_window=1):
        return False
    profile.totp_confirmed_at = datetime.now(UTC)
    with tenant_scope(profile.tenant_id):
        profile.save(update_fields=["totp_confirmed_at"])
    return True


def sign_out(request) -> None:
    django_logout(request)


def _resolve(user_id) -> tuple | None:
    """Find which profile and tenant a signed-in Django user belongs to.

    Goes through `complylayer_resolve_dashboard_user` rather than the ORM, for
    the same reason the API key lookup does (migration 0009): this is the query
    that *determines* the tenant, so it cannot be scoped by one. It returns the
    pk and the tenant id only — enough to open a real scope and then query
    normally, and not enough to hand out a `totp_secret` through an exemption.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT id, tenant_id FROM complylayer_resolve_dashboard_user(%s)", [user_id]
        )
        return cursor.fetchone()


def current_profile(request):
    from complylayer.models import DashboardUser

    if not request.user.is_authenticated:
        return None

    resolved = _resolve(request.user.pk)
    if resolved is None:
        return None

    profile_id, tenant_id = resolved
    with tenant_scope(tenant_id):
        return DashboardUser.objects.filter(pk=profile_id).select_related("tenant", "user").first()


def signed_in(view):
    """Both factors, or nowhere.

    Deliberately checks the session flag rather than `is_authenticated`: a user
    who has given a password and not a code is authenticated as far as Django is
    concerned, and has proved half of what this product needs.
    """

    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        profile = current_profile(request)
        if profile is None:
            return redirect("dashboard:sign-in")
        if not profile.has_second_factor:
            return redirect("dashboard:enrol")
        if not request.session.get(SESSION_FACTOR_KEY):
            return redirect("dashboard:verify")

        request.profile = profile
        # The whole view runs scoped, so a query inside it that forgets its own
        # tenant filter returns nothing rather than everything. Before this,
        # nothing in the request path called `tenant_scope` at all.
        with tenant_scope(profile.tenant_id):
            return view(request, *args, **kwargs)

    return wrapper
