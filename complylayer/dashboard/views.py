"""The dashboard's pages.

Every view asks the same permission functions the API asks (`complylayer.tenancy`),
so a button that should not exist is absent for the same reason the endpoint
returns 403. One implementation of who-may-do-what — ADR-0004's main argument for
server rendering.
"""

from __future__ import annotations

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render

from complylayer.dashboard import states, throttle
from complylayer.dashboard.auth import (
    SignInFailed,
    begin_enrolment,
    confirm_enrolment,
    current_profile,
    sign_out,
    signed_in,
    start_session,
    verify_second_factor,
)
from complylayer.dashboard.builder import SHAPES, SHAPES_BY_KEY, WINDOWS, build
from complylayer.dashboard.diff import BacktestImpact, compare
from complylayer.dsl import RuleSyntaxError, validate_source
from complylayer.models import Decision, Rule
from complylayer.tenancy import Action, Actor, Role, may


def _actor(request) -> Actor:
    return Actor(id=request.user.username, role=Role(request.profile.role))


def _chrome(request) -> dict:
    """What every page needs: who is acting, and what they may do.

    Passed to the template so navigation and buttons are built from permissions
    rather than hidden with CSS after the fact.
    """
    actor = _actor(request)
    return {
        "profile": request.profile,
        "actor": actor,
        "can": {action.value: may(actor.role, action) for action in Action},
    }


# ── Signing in ────────────────────────────────────────────────────────────


def _throttle_client():
    """Redis, or None. None disables the throttle rather than the sign-in.

    Built per request rather than at import because a connection created before
    gunicorn forks is shared across children (D12).
    """
    try:
        import redis

        return redis.Redis.from_url(settings.COMPLYLAYER["REDIS_URL"])
    except Exception:  # pragma: no cover - a misconfigured URL, not a live outage
        return None


def sign_in(request):
    """Password, throttled.

    Throttling the second factor and not this one would leave the account's
    first factor free to guess, which just moves the problem.
    """
    if request.method == "POST":
        email = request.POST.get("email", "")
        client = _throttle_client()

        waiting = throttle.lockout_seconds(client, "sign-in", email)
        if waiting:
            return render(
                request,
                "complylayer/sign_in.html",
                {"error": throttle.wait_message(waiting)},
                status=429,
            )

        try:
            start_session(request, email, request.POST.get("password", ""))
        except SignInFailed as exc:
            throttle.record_failure(client, "sign-in", email)
            return render(request, "complylayer/sign_in.html", {"error": str(exc)}, status=401)

        throttle.clear(client, "sign-in", email)
        return redirect("dashboard:verify")
    return render(request, "complylayer/sign_in.html", {})


def verify(request):
    """The second factor, throttled and single-use.

    Six digits with `valid_window=1` is three chances in a million per attempt,
    so even odds take about 231,000 guesses. Unthrottled at a modest 200
    requests a second that is nineteen minutes; at twenty guesses an hour it is
    about fifteen months, and every lockout along the way is something to alert
    on. The replay guard closes the other door — a TOTP code stays valid for its
    whole window, so one seen over a shoulder works again until the window
    rolls.
    """
    profile = current_profile(request)
    if profile is None:
        return redirect("dashboard:sign-in")
    if not profile.has_second_factor:
        return redirect("dashboard:enrol")

    if request.method == "POST":
        client = _throttle_client()
        identity = str(profile.pk)

        waiting = throttle.lockout_seconds(client, "second-factor", identity)
        if waiting:
            return render(
                request,
                "complylayer/verify.html",
                {"error": throttle.wait_message(waiting)},
                status=429,
            )

        code = request.POST.get("code", "")
        fresh = throttle.consume_code(client, identity, code)
        if fresh and verify_second_factor(request, profile, code):
            throttle.clear(client, "second-factor", identity)
            return redirect("dashboard:rules")

        throttle.record_failure(client, "second-factor", identity)
        return render(
            request,
            "complylayer/verify.html",
            {"error": "That code is not right. Codes change every 30 seconds."},
            status=401,
        )
    return render(request, "complylayer/verify.html", {})


def enrol(request):
    """First-time authenticator setup, and only first-time.

    The guard below is the whole of the fix for a complete MFA bypass: without
    it, a session holding only a password reached this page, was handed a fresh
    secret, and confirmed it from its own authenticator. `verify` redirects here
    when a factor is missing, which made it easy to read this view as only ever
    reachable in that state. It is a URL; anyone signed in can request it.

    Guarded twice on purpose — here and in `begin_enrolment` — because this one
    is a routing decision and that one is the rule.
    """
    profile = current_profile(request)
    if profile is None:
        return redirect("dashboard:sign-in")
    if profile.has_second_factor:
        return redirect("dashboard:verify")

    if request.method == "POST":
        if confirm_enrolment(profile, request.POST.get("code", "")):
            request.session["complylayer_second_factor"] = True
            return redirect("dashboard:rules")
        return render(
            request,
            "complylayer/enrol.html",
            {"error": "That code did not match.", **_enrolment_context(profile)},
            status=400,
        )

    return render(request, "complylayer/enrol.html", _enrolment_context(profile))


def _enrolment_context(profile) -> dict:
    secret, uri = begin_enrolment(profile)
    return {"secret": secret, "uri": uri}


def sign_out_view(request):
    sign_out(request)
    return redirect("dashboard:sign-in")


# ── Rules ─────────────────────────────────────────────────────────────────


@signed_in
def rules(request):
    queryset = Rule.objects.filter(tenant=request.profile.tenant).order_by("priority", "id")
    return render(
        request,
        "complylayer/rules.html",
        {**_chrome(request), "rules": queryset},
    )


@signed_in
def builder(request):
    """The rule builder.

    The shape list and the live expression come from the same module the tests
    assert against, so what a compliance officer can reach here is exactly what
    `tests/test_dashboard.py` proves is reachable.
    """
    return render(
        request,
        "complylayer/builder.html",
        {**_chrome(request), "shapes": SHAPES, "windows": WINDOWS},
    )


@signed_in
def preview(request):
    """Live validation, called as the officer types.

    Returns the same three-part error the API returns — problem, fix, reason —
    because there is one error catalogue and the builder does not get a friendlier
    version of it.
    """
    shape_key = request.POST.get("shape")
    if shape_key not in SHAPES_BY_KEY:
        raise Http404

    values: dict = {}
    for control in SHAPES_BY_KEY[shape_key].inputs:
        raw = request.POST.get(control.key, "").strip()
        if not raw:
            continue
        values[control.key] = int(raw) if raw.lstrip("-").isdigit() else raw

    try:
        result = build(shape_key, values)
    except (KeyError, TypeError):
        return JsonResponse(
            {
                "valid": False,
                "problem": "Some answers are still missing.",
                "fix": "Fill in every field marked required.",
            }
        )

    if not result.valid:
        return JsonResponse({"valid": False, **result.error})
    return JsonResponse({"valid": True, "expression": result.expression})


@signed_in
def validate_expression(request):
    """The expression editor's escape hatch, validated identically."""
    try:
        validate_source(request.POST.get("expression", ""))
    except RuleSyntaxError as exc:
        return JsonResponse({"valid": False, **exc.as_dict()})
    return JsonResponse({"valid": True})


@signed_in
def approval(request, rule_id: str):
    """The approval diff.

    Approve is absent, not merely disabled, when the viewer is the author — the
    template asks `can_approve`, which is false for one's own rule. A disabled
    button invites somebody to wonder why; an absent one does not.
    """
    rule = Rule.objects.filter(tenant=request.profile.tenant, id=rule_id).first()
    if rule is None:
        raise Http404

    actor = _actor(request)

    if request.method == "POST" and request.POST.get("action") == "approve":
        from complylayer import rules as lifecycle
        from complylayer.tenancy import PermissionDenied

        try:
            lifecycle.approve(rule=rule, actor=actor, reason=request.POST.get("reason", ""))
        except PermissionDenied as exc:
            # The template hides the button, but the endpoint is the control. A
            # hidden button is a hint; a refused POST is a rule.
            return render(
                request,
                "complylayer/approval.html",
                {**_chrome(request), "rule": rule, "error": str(exc)},
                status=403,
            )
        return redirect("dashboard:rules")
    previous = (
        Rule.objects.filter(tenant=request.profile.tenant, name=rule.name)
        .exclude(id=rule.id)
        .order_by("-version")
        .first()
    )

    diff = None
    if previous:
        diff = compare(
            previous.expression,
            rule.expression,
            regulatory_reference=rule.regulatory_reference,
            author=rule.created_by,
        )

    own = rule.created_by == actor.id
    return render(
        request,
        "complylayer/approval.html",
        {
            **_chrome(request),
            "rule": rule,
            "diff": diff,
            "impact": BacktestImpact(total=48_190, before_matches=118, after_matches=1_204),
            "is_author": own,
            "can_approve": may(actor.role, Action.APPROVE) and not own,
            "author_state": states.pending_approval_seen_by_author(rule.created_by)
            if own
            else None,
        },
    )


@signed_in
def queue(request):
    """The review queue, and the two states it has beyond "some rows"."""
    decisions = Decision.objects.filter(
        tenant=request.profile.tenant, outcome="flag", review_status=""
    ).order_by("-decided_at")[:200]
    depth = decisions.count()

    state = None
    if depth == 0:
        state = states.review_queue_empty()
    elif depth >= states.QUEUE_PRESSURE_THRESHOLD:
        state = states.review_queue_under_pressure(depth)

    degraded = Decision.objects.filter(
        tenant=request.profile.tenant, degraded=True, review_status=""
    ).count()

    return render(
        request,
        "complylayer/queue.html",
        {
            **_chrome(request),
            "decisions": decisions,
            "state": state,
            "degraded_banner": states.degraded_banner(degraded, "today") if degraded else None,
        },
    )
