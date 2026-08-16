"""The dashboard's pages.

Every view asks the same permission functions the API asks (`complylayer.tenancy`),
so a button that should not exist is absent for the same reason the endpoint
returns 403. One implementation of who-may-do-what — ADR-0004's main argument for
server rendering.
"""

from __future__ import annotations

from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render

from complylayer.dashboard import states
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


def sign_in(request):
    if request.method == "POST":
        try:
            start_session(request, request.POST.get("email", ""), request.POST.get("password", ""))
        except SignInFailed as exc:
            return render(request, "complylayer/sign_in.html", {"error": str(exc)}, status=401)
        return redirect("dashboard:verify")
    return render(request, "complylayer/sign_in.html", {})


def verify(request):
    profile = current_profile(request)
    if profile is None:
        return redirect("dashboard:sign-in")
    if not profile.has_second_factor:
        return redirect("dashboard:enrol")

    if request.method == "POST":
        if verify_second_factor(request, profile, request.POST.get("code", "")):
            return redirect("dashboard:rules")
        return render(
            request,
            "complylayer/verify.html",
            {"error": "That code is not right. Codes change every 30 seconds."},
            status=401,
        )
    return render(request, "complylayer/verify.html", {})


def enrol(request):
    profile = current_profile(request)
    if profile is None:
        return redirect("dashboard:sign-in")

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
