"""Phase 6's exit gate.

The roadmap asks for three things and the first is the one that decides whether
the product's premise holds: **every §4.4 rule must be buildable through the UI
alone.** If a compliance officer has to drop into the expression editor for the
rules that matter, an engineer is back in the loop and the pitch is false.
"""

from __future__ import annotations

import pytest

from complylayer.dashboard import builder, states
from complylayer.dashboard.builder import SHAPES, SHAPES_BY_KEY, build
from complylayer.dashboard.diff import BacktestImpact, ThresholdChange, compare
from complylayer.dsl import RuleSyntaxError, validate_source

# The six from §4.4, each expressed as builder inputs rather than as text. The
# expected expression is the specification's own wording, normalised for the
# underscores the builder does not emit.
SPEC_RULES = {
    "KYC tier daily limit": (
        "amount_limit",
        {"limit": "tier_daily_limit_minor"},
        "amount_minor > tier_daily_limit_minor",
    ),
    "velocity": (
        "velocity_count",
        {"window": "1h", "count": 5, "min_amount_minor": 50_000_000},
        "velocity_count(window='1h', min_amount_minor=50000000) > 5",
    ),
    "structuring": (
        "structuring",
        {"window": "24h", "count": 3, "percent": 90},
        "velocity_count(window='24h', "
        "min_amount_minor=percent_of(reporting_threshold_minor, 90), "
        "max_amount_minor=reporting_threshold_minor) >= 3",
    ),
    "dormant reactivation": (
        "dormant_reactivation",
        {"days": 90, "amount_minor": 20_000_000},
        "days_since(last_transaction_at) > 90 and amount_minor > 20000000",
    ),
    "high-risk corridor": (
        "corridor",
        {"max_kyc_tier": 3, "from_hour": 6, "to_hour": 22},
        "in_list(destination_country, high_risk_countries) and kyc_tier < 3 "
        "and (hour_of_day() < 6 or hour_of_day() > 22)",
    ),
    "new account": (
        "new_account",
        {"days": 7, "amount_minor": 10_000_000},
        "days_since(account_created_at) < 7 and amount_minor > 10000000",
    ),
}


class TestTheBuilderReachesEveryRuleThatMatters:
    """The exit gate. If this fails, the product's premise fails with it."""

    @pytest.mark.parametrize("name", SPEC_RULES, ids=list(SPEC_RULES))
    def test_each_specification_rule_is_buildable(self, name: str):
        shape_key, values, expected = SPEC_RULES[name]
        result = build(shape_key, values)

        assert result.valid, result.error
        assert result.expression == expected

    @pytest.mark.parametrize("name", SPEC_RULES, ids=list(SPEC_RULES))
    def test_what_the_builder_emits_passes_the_same_validator(self, name: str):
        """One code path. A built rule earns no trust a typed one would not."""
        shape_key, values, _ = SPEC_RULES[name]
        validate_source(build(shape_key, values).expression)

    def test_every_shape_has_a_worked_example(self):
        """A shape nobody exercised is a shape that is wrong."""
        exercised = {shape_key for shape_key, _, _ in SPEC_RULES.values()}
        assert {shape.key for shape in SHAPES} == exercised


class TestTheShapesAreWrittenForACompliancOfficer:
    def test_each_shape_asks_a_question_rather_than_naming_a_construct(self):
        """ "Is this transaction larger than a limit?" not "Comparison rule"."""
        for shape in SHAPES:
            assert shape.question.endswith("?"), shape.key
            assert "expression" not in shape.question.lower()

    def test_each_shape_prefills_the_regulation_it_implements(self):
        """A rule that claims a regulation is one an approver can weigh; a blank
        reference is one they have to take on trust."""
        for shape in SHAPES:
            assert shape.regulatory_reference, shape.key

    def test_inputs_are_labelled_in_plain_language(self):
        for shape in SHAPES:
            for control in shape.inputs:
                assert control.label[0].isupper()
                assert "_" not in control.label, f"{shape.key}.{control.key} leaks a fact name"

    def test_the_transaction_limit_defaults_to_blocking(self):
        """A tier limit that only flags is a limit that does not limit."""
        assert SHAPES_BY_KEY["amount_limit"].severity == "block"

    def test_optional_inputs_say_what_leaving_them_blank_means(self):
        optional = [
            control
            for shape in SHAPES
            for control in shape.inputs
            if not control.required and control.hint
        ]
        assert any("blank" in control.hint for control in optional)


class TestTheBuilderIsNotTrusted:
    """A shape is a convenience for a person, not a licence for the code."""

    def test_a_shape_producing_an_invalid_rule_is_caught(self, monkeypatch):
        monkeypatch.setitem(
            builder.BUILDERS, "amount_limit", lambda values: {"fact": "__builtins__"}
        )
        result = build("amount_limit", {"limit": 1})
        assert result.valid is False
        assert result.error["problem"]

    def test_a_hostile_value_cannot_reach_the_expression(self):
        result = build("amount_limit", {"limit": "amount_minor > 0 or ().__class__"})
        assert result.valid is False


class TestTheApprovalDiff:
    """Not a text diff. A reviewer scanning red and green lines sees a
    one-character change and approves it."""

    def test_a_tenfold_increase_says_so(self):
        diff = compare("amount_minor > 5000000", "amount_minor > 50000000")
        assert diff.threshold.magnitude == "10× higher"
        assert diff.threshold.looser is True

    def test_the_amount_is_rendered_in_the_unit_a_human_thinks_in(self):
        change = ThresholdChange("amount_minor", 5_000_000, 50_000_000, "NGN")
        assert change.render(change.before) == "₦50,000.00"
        assert change.render(change.after) == "₦500,000.00"

    def test_a_whole_multiple_is_not_mangled(self):
        """`"10".rstrip("0")` gives `"1"`.

        On the one screen whose job is showing that a limit moved tenfold, that
        would have understated it by an order of magnitude. Kept as a test
        because the next person to tidy the formatting will reach for rstrip.
        """
        for before, after, expected in [
            (5_000_000, 50_000_000, "10× higher"),
            (1_00, 10_000, "100× higher"),
            (2000, 5000, "2.5× higher"),
            (1000, 3000, "3× higher"),
        ]:
            assert compare(f"a_minor > {before}", f"a_minor > {after}").threshold.magnitude == (
                expected
            )

    def test_a_tightening_is_coloured_the_other_way(self):
        """A limit going down is a control being strengthened, and must not look
        like an alarm."""
        diff = compare("amount_minor > 5000000", "amount_minor > 2500000")
        assert diff.threshold.looser is False
        assert diff.threshold.magnitude == "2× lower"

    def test_a_small_change_is_a_percentage_rather_than_a_multiple(self):
        """ "1.5× higher" is a worse sentence than "50% higher"."""
        assert compare("a_minor > 1000", "a_minor > 1500").threshold.magnitude == "50% higher"

    def test_a_threshold_only_change_is_recognised_as_such(self):
        """The dangerous case: the rule looks almost identical."""
        assert compare("amount_minor > 5000000", "amount_minor > 50000000").is_threshold_only

    def test_a_structural_change_is_not_reported_as_a_threshold_move(self):
        diff = compare(
            "amount_minor > 5000000",
            "amount_minor > 5000000 and kyc_tier < 3",
        )
        assert diff.threshold is None

    def test_a_non_amount_threshold_is_not_dressed_up_as_money(self):
        """`kyc_tier < 3` is not ₦0.03."""
        change = ThresholdChange("kyc_tier", 2, 3)
        assert change.is_amount is False
        assert change.render(3) == "3"

    def test_a_currency_without_minor_units_is_not_divided_by_a_hundred(self):
        assert ThresholdChange("amount_minor", 1000, 2000, "JPY").render(1000) == "¥1,000"

    def test_the_diff_carries_who_asked_and_why(self):
        """The approver is being asked to trust a person as much as a diff."""
        diff = compare(
            "amount_minor > 1",
            "amount_minor > 2",
            regulatory_reference="CBN KYC Tier 2",
            author="usr_adaeze",
            reason="board approved a higher cap",
        )
        assert diff.regulatory_reference == "CBN KYC Tier 2"
        assert diff.author == "usr_adaeze"
        assert diff.reason

    def test_a_change_from_zero_does_not_divide_by_zero(self):
        change = ThresholdChange("amount_minor", 0, 5_000_000)
        assert change.factor is None
        assert change.magnitude == "higher"


class TestBacktestImpact:
    def test_it_reads_as_a_sentence_a_reviewer_can_weigh(self):
        impact = BacktestImpact(total=48_190, before_matches=118, after_matches=1_204)
        assert impact.sentence == (
            "Would have matched 1,204 of 48,190 transactions, 1,086 more than the current rule."
        )

    def test_a_tightening_reads_as_fewer(self):
        impact = BacktestImpact(total=1_000, before_matches=100, after_matches=10)
        assert "90 fewer" in impact.sentence


class TestTheSixStates:
    """The states the plan review found missing. A state nobody designed becomes
    an empty table with a shrug in it."""

    def test_every_named_state_has_a_builder(self):
        built = {
            states.pending_approval_seen_by_author("usr").key,
            states.backtest_running(10, 100).key,
            states.shadow_no_divergence(7, 1000).key,
            states.review_queue_empty().key,
            states.review_queue_under_pressure(600).key,
            states.degraded_banner(42, "09:14").key,
        }
        assert built == set(states.ALL_STATES)

    def test_an_author_is_told_that_editing_clears_the_approval(self):
        """Before they discover it by losing an approval they waited two days for."""
        state = states.pending_approval_seen_by_author("usr_adaeze")
        assert "cannot approve it yourself" in state.detail
        assert "clear any approval" in state.detail

    def test_a_running_backtest_says_leaving_is_safe(self):
        state = states.backtest_running(12_000, 48_000)
        assert "25%" in state.title
        assert "leave this page" in state.detail
        assert "cannot affect live decisions" in state.detail

    def test_zero_divergence_is_the_most_reassuring_screen(self):
        """This is the moment an officer decides whether to trust a control."""
        state = states.shadow_no_divergence(7, 48_190)
        assert state.tone == "reassuring"
        assert "what you want to see" in state.detail

    def test_an_empty_queue_is_a_goal_not_an_error(self):
        assert states.review_queue_empty().tone == "reassuring"

    def test_a_queue_under_pressure_points_at_the_likely_cause(self):
        """A queue of 500 is not a queue of 20 with more rows — it is a different
        problem, and the useful action is finding the over-firing rule."""
        state = states.review_queue_under_pressure(612)
        assert state.tone == "pressure"
        assert "612" in state.title
        assert "one rule firing too broadly" in state.detail

    def test_the_pressure_threshold_matches_the_alert(self):
        assert states.QUEUE_PRESSURE_THRESHOLD == 500

    def test_the_degraded_banner_says_what_a_fallback_did(self):
        state = states.degraded_banner(42, "09:14")
        assert "failed closed" in state.detail
        assert "failed open" in state.detail
        assert "no gap" in state.detail


class TestErrorsReachTheOfficerIntact:
    """Phase 1 wrote the error catalogue for this moment."""

    def test_a_dotted_fact_produces_the_three_part_error(self):
        with pytest.raises(RuleSyntaxError) as exc:
            validate_source("customer.kyc_tier > 2")
        error = exc.value.as_dict()
        assert "dot" in error["problem"].lower()
        assert error["fix"]
        assert error["reason"]

    def test_no_error_a_builder_can_produce_is_a_python_traceback(self):
        for values in [{"limit": "a b c"}, {"limit": "().__class__"}, {"limit": 1.5}]:
            result = build("amount_limit", values)
            if not result.valid:
                assert "Traceback" not in result.error["problem"]
                assert result.error["fix"]


class TestDesignTokens:
    """DESIGN.md is the source of truth and the stylesheet is its expression.
    These are the two rules easiest to break by accident."""

    @staticmethod
    def stylesheets() -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent / "complylayer/static/complylayer"
        return (root / "tokens.css").read_text() + (root / "dashboard.css").read_text()

    def test_colour_is_reserved_for_severity(self):
        """No brand colour, no blue buttons. Every saturated token is a severity.

        Checked mechanically because this is the rule a component library or a
        hurried afternoon breaks first.
        """
        import re

        css = self.stylesheets()
        severity = {"--block", "--flag", "--allow", "--degraded"}
        declared = re.findall(r"(--[a-z-]+):\s*(#[0-9a-fA-F]{6})", css)

        for name, hex_value in declared:
            base = name.rstrip("-wash").replace("-wash", "")
            if any(name.startswith(token) for token in severity):
                continue
            red, green, blue = (int(hex_value[i : i + 2], 16) for i in (1, 3, 5))
            saturation = max(red, green, blue) - min(red, green, blue)
            assert saturation <= 24, (
                f"{name}: {hex_value} is a colour, and the only colours in this "
                f"product are severity. See DESIGN.md."
            )
            assert base

    def test_the_severity_tokens_all_exist(self):
        css = self.stylesheets()
        for token in ("--block", "--flag", "--allow", "--degraded"):
            assert f"{token}:" in css

    def test_reduced_motion_is_honoured(self):
        assert "prefers-reduced-motion" in self.stylesheets()

    def test_focus_is_never_removed(self):
        """A keyboard user working a 500-row queue needs to know where they are."""
        css = self.stylesheets()
        assert ":focus-visible" in css
        assert "outline: none" not in css

    def test_amounts_are_tabular(self):
        """A column whose digits do not align is a column somebody misreads."""
        assert "tabular-nums" in self.stylesheets()


@pytest.mark.integration
# `replica` is declared because the approval page backtests the proposed rule
# against recorded history, and §11.1 puts that read on the replica. In tests it
# is a MIRROR of `default`, so this costs nothing but the declaration.
@pytest.mark.django_db(databases=["default", "replica"])
class TestTheDashboardRenders:
    """The pages, end to end, through Django's test client.

    Auth is exercised for real rather than stubbed: the two-step sign-in is a
    control, and a test that skips it would not notice the day it stops working.
    """

    @pytest.fixture(autouse=True)
    def management_settings(self, management):
        return management

    @pytest.fixture
    def officer(self):
        from datetime import UTC, datetime

        import pyotp
        from django.contrib.auth.models import User

        from complylayer.models import DashboardUser, Tenant

        tenant = Tenant.objects.create(id="tnt_ui", name="UI")
        user = User.objects.create_user(
            username="adaeze@example.com", email="adaeze@example.com", password="correct-horse"
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

    def sign_in(self, client, officer):
        import pyotp

        profile, secret = officer
        client.post(
            "/dashboard/sign-in", {"email": profile.user.email, "password": "correct-horse"}
        )
        client.post("/dashboard/verify", {"code": pyotp.TOTP(secret).now()})
        return profile

    def test_a_password_alone_does_not_get_you_in(self, client, officer):
        """Django considers them authenticated. This product does not."""
        profile, _ = officer
        client.post(
            "/dashboard/sign-in", {"email": profile.user.email, "password": "correct-horse"}
        )
        response = client.get("/dashboard/")
        assert response.status_code == 302
        assert response.url == "/dashboard/verify"

    def test_a_password_alone_cannot_re_enrol_its_way_past_the_second_factor(self, client, officer):
        """The security review's first finding, kept as its exploit.

        `enrol` had no guard and generated a secret on render, so a session
        holding only a stolen password could collect a fresh authenticator,
        confirm it, and reach the dashboard as a compliance officer — while
        destroying the real owner's factor on the way. It ran end to end.
        """
        profile, victim_secret = officer
        client.post(
            "/dashboard/sign-in", {"email": profile.user.email, "password": "correct-horse"}
        )

        response = client.get("/dashboard/enrol")
        assert response.status_code == 302, "enrolment is not reachable once a factor exists"
        assert response.url == "/dashboard/verify"

        profile.refresh_from_db()
        assert profile.totp_secret == victim_secret, "the owner's factor is untouched"
        assert profile.has_second_factor

    def test_posting_to_enrolment_cannot_replace_a_confirmed_factor_either(self, client, officer):
        """The GET is the convenient route. The POST must be shut too."""
        import pyotp

        profile, victim_secret = officer
        client.post(
            "/dashboard/sign-in", {"email": profile.user.email, "password": "correct-horse"}
        )

        attacker_secret = pyotp.random_base32()
        response = client.post("/dashboard/enrol", {"code": pyotp.TOTP(attacker_secret).now()})
        assert response.status_code == 302
        assert response.url == "/dashboard/verify"

        profile.refresh_from_db()
        assert profile.totp_secret == victim_secret

    def test_begin_enrolment_refuses_an_enrolled_account_on_its_own(self, officer):
        """Guarded in the view and in the rule, so a future caller cannot reopen
        it by routing around the view."""
        from complylayer.dashboard.auth import AlreadyEnrolled, begin_enrolment

        profile, _ = officer
        with pytest.raises(AlreadyEnrolled):
            begin_enrolment(profile)

    def test_a_first_enrolment_still_works(self, client):
        """The guard must not have shut the door on the people who need it."""
        import pyotp
        from django.contrib.auth.models import User

        from complylayer.models import DashboardUser, Tenant

        tenant = Tenant.objects.create(id="tnt_new", name="New")
        user = User.objects.create_user(
            username="new@example.com", email="new@example.com", password="correct-horse"
        )
        DashboardUser.objects.create(user=user, tenant=tenant, role="compliance_officer")

        client.post("/dashboard/sign-in", {"email": "new@example.com", "password": "correct-horse"})
        response = client.get("/dashboard/enrol")
        assert response.status_code == 200

        secret = response.context["secret"]
        response = client.post("/dashboard/enrol", {"code": pyotp.TOTP(secret).now()})
        assert response.status_code == 302
        assert response.url == "/dashboard/"

    def test_reloading_enrolment_keeps_the_secret_being_typed_in(self, client):
        """A fresh secret on every render invalidates the one somebody is
        half-way through entering into their phone."""
        from django.contrib.auth.models import User

        from complylayer.models import DashboardUser, Tenant

        tenant = Tenant.objects.create(id="tnt_new2", name="New")
        user = User.objects.create_user(
            username="new2@example.com", email="new2@example.com", password="correct-horse"
        )
        DashboardUser.objects.create(user=user, tenant=tenant, role="compliance_officer")

        client.post(
            "/dashboard/sign-in", {"email": "new2@example.com", "password": "correct-horse"}
        )
        first = client.get("/dashboard/enrol").context["secret"]
        second = client.get("/dashboard/enrol").context["secret"]
        assert first == second

    def test_a_wrong_password_says_nothing_useful(self, client, officer):
        profile, _ = officer
        response = client.post(
            "/dashboard/sign-in", {"email": profile.user.email, "password": "wrong"}
        )
        assert response.status_code == 401
        assert b"do not match" in response.content

    def test_an_unknown_email_gives_the_identical_message(self, client, officer):
        """Telling somebody which half they got right is telling them half of
        what they need."""
        response = client.post(
            "/dashboard/sign-in", {"email": "nobody@example.com", "password": "correct-horse"}
        )
        assert b"do not match" in response.content

    def test_both_factors_reach_the_rules_page(self, client, officer):
        self.sign_in(client, officer)
        response = client.get("/dashboard/")
        assert response.status_code == 200
        assert b"Rules" in response.content

    def test_a_wrong_code_is_refused(self, client, officer):
        profile, _ = officer
        client.post(
            "/dashboard/sign-in", {"email": profile.user.email, "password": "correct-horse"}
        )
        response = client.post("/dashboard/verify", {"code": "000000"})
        assert response.status_code == 401

    def test_signing_out_ends_the_session(self, client, officer):
        self.sign_in(client, officer)
        client.get("/dashboard/sign-out")
        assert client.get("/dashboard/").status_code == 302

    def test_the_builder_offers_every_shape(self, client, officer):
        self.sign_in(client, officer)
        content = client.get("/dashboard/new").content.decode()
        for shape in SHAPES:
            assert shape.name in content
            assert shape.question in content

    def test_the_builder_previews_a_valid_rule(self, client, officer):
        self.sign_in(client, officer)
        response = client.post(
            "/dashboard/preview",
            {"shape": "velocity_count", "window": "1h", "count": "5"},
        )
        body = response.json()
        assert body["valid"] is True
        assert body["expression"] == "velocity_count(window='1h') > 5"

    def test_the_builder_returns_the_three_part_error(self, client, officer):
        """The same catalogue the API returns. The builder does not get a
        friendlier version of it."""
        self.sign_in(client, officer)
        body = client.post("/dashboard/validate", {"expression": "customer.kyc_tier > 2"}).json()
        assert body["valid"] is False
        assert "dot" in body["problem"].lower()
        assert body["fix"]
        assert body["reason"]

    def test_a_half_finished_rule_says_what_is_missing(self, client, officer):
        self.sign_in(client, officer)
        body = client.post("/dashboard/preview", {"shape": "velocity_count"}).json()
        assert body["valid"] is False
        assert "missing" in body["problem"].lower()

    def test_an_engineer_sees_no_new_rule_link(self, client, officer):
        """Navigation is built from permissions, not hidden with CSS."""
        profile, _ = officer
        profile.role = "engineer"
        profile.save()
        self.sign_in(client, officer)
        content = client.get("/dashboard/").content.decode()
        assert "/dashboard/new" not in content

    def test_the_approval_diff_shows_the_change_in_naira(self, client, officer):
        from complylayer.models import Rule

        profile = self.sign_in(client, officer)
        Rule.objects.create(
            id="rul_old",
            tenant=profile.tenant,
            name="Tier 2 limit",
            category="kyc",
            expression="amount_minor > 5000000",
            severity="block",
            state="archived",
            version=1,
            created_by="usr_someone",
        )
        Rule.objects.create(
            id="rul_new",
            tenant=profile.tenant,
            name="Tier 2 limit",
            category="kyc",
            expression="amount_minor > 50000000",
            severity="block",
            state="draft",
            version=2,
            created_by="usr_someone",
            regulatory_reference="CBN KYC Tier 2",
        )

        content = client.get("/dashboard/rules/rul_new").content.decode()
        assert "₦50,000.00" in content
        assert "₦500,000.00" in content
        assert "10× higher" in content
        assert "CBN KYC Tier 2" in content
        assert "lets more transactions through" in content

    def test_the_author_cannot_approve_their_own_change(self, client, officer):
        """Absent, not disabled. A disabled button invites somebody to wonder
        why; an absent one does not."""
        from complylayer.models import Rule

        profile = self.sign_in(client, officer)
        Rule.objects.create(
            id="rul_own",
            tenant=profile.tenant,
            name="Own rule",
            category="kyc",
            expression="amount_minor > 1",
            severity="flag",
            state="draft",
            created_by=profile.user.username,
        )
        content = client.get("/dashboard/rules/rul_own").content.decode()
        assert "Approve this change" not in content
        assert "cannot approve it yourself" in content

    def test_another_tenants_rule_is_not_found(self, client, officer):
        from complylayer.models import Rule, Tenant

        self.sign_in(client, officer)
        other = Tenant.objects.create(id="tnt_other_ui", name="Other")
        Rule.objects.create(
            id="rul_other",
            tenant=other,
            name="Theirs",
            category="kyc",
            expression="amount_minor > 1",
            severity="flag",
            state="draft",
            created_by="x",
        )
        assert client.get("/dashboard/rules/rul_other").status_code == 404

    def test_an_empty_queue_reads_as_the_goal_state(self, client, officer):
        self.sign_in(client, officer)
        content = client.get("/dashboard/queue").content.decode()
        assert "Nothing waiting for review" in content
        assert "state--reassuring" in content


@pytest.mark.django_db
class TestCsrfIsEnforcedOnTheSignInForms:
    """Evidence against a static-analysis finding, and a control worth a test anyway.

    `semgrep`'s django-no-csrf-token rule flags `sign_in.html` and `verify.html`
    and does not flag the other four templates, which are written identically.
    Both do carry `{% csrf_token %}`, and `nosemgrep` in a Django template
    comment or an HTML comment does not suppress it. So the claim is settled the
    way claims in this repository get settled: by running it.

    Sign-in CSRF is a real class of bug — an attacker who can forge a login
    request can sign a victim into the attacker's own account and watch what
    they do next — so this is worth asserting whatever the linter thinks.
    """

    @pytest.fixture(autouse=True)
    def management_settings(self, management):
        return management

    def test_the_sign_in_form_carries_a_token(self, client):
        assert b"csrfmiddlewaretoken" in client.get("/dashboard/sign-in").content

    def test_the_verify_form_carries_a_token(self, client):
        """Reached only with the first factor already given, so the test has to
        get there the same way a person does."""
        from datetime import UTC, datetime

        import pyotp
        from django.contrib.auth.models import User

        from complylayer.models import DashboardUser, Tenant

        tenant = Tenant.objects.create(id="tnt_csrf", name="CSRF")
        user = User.objects.create_user(
            username="csrf@example.com", email="csrf@example.com", password="correct-horse"
        )
        DashboardUser.objects.create(
            user=user,
            tenant=tenant,
            role="compliance_officer",
            totp_secret=pyotp.random_base32(),
            totp_confirmed_at=datetime.now(UTC),
        )
        client.post(
            "/dashboard/sign-in", {"email": "csrf@example.com", "password": "correct-horse"}
        )

        response = client.get("/dashboard/verify")
        assert response.status_code == 200
        assert b"csrfmiddlewaretoken" in response.content

    def test_a_post_without_a_token_is_refused(self, client):
        """The middleware, not the template, is the control."""
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        response = strict.post("/dashboard/sign-in", {"email": "a@example.com", "password": "x"})
        assert response.status_code == 403

    def test_a_post_with_the_token_gets_past_csrf(self, client):
        """The other half: the control must not be refusing everything."""
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        page = strict.get("/dashboard/sign-in")
        token = page.cookies["csrftoken"].value
        response = strict.post(
            "/dashboard/sign-in",
            {"email": "a@example.com", "password": "x", "csrfmiddlewaretoken": token},
        )
        # 401 is the credentials being wrong, which means CSRF let it through.
        assert response.status_code == 401


class TestTheApprovalImpactIsRealOrAbsent:
    """The number on the screen where a control gets loosened.

    It used to be `BacktestImpact(total=48_190, before_matches=118,
    after_matches=1_204)` — a literal in the view, identical for every rule and
    every tenant. A reviewer weighing "10x higher" was being handed an invented
    figure to weigh it against, on the one page where that matters most.

    `complylayer/backtest/` exists precisely for this and reports EXACT, PARTIAL
    or UNAVAILABLE with the caveat inside the sentence. The dashboard went
    around it with a constant. Found by photographing the page for the README.
    """

    class FakeRule:
        def __init__(self, expression):
            self.expression = expression

    class StoredDecision:
        def __init__(self, amount):
            self.id = f"dec_{amount}"
            self.transaction_ref = f"TXN-{amount}"
            self.amount_minor = amount
            self.currency = "NGN"
            self.decided_at = "2026-08-01T00:00:00Z"
            self.resolved_facts = {"amount_minor": amount}
            self.outcome = "allow"

    def test_no_history_shows_no_figure_at_all(self):
        """ "Matched 0 of 0" reads like a finding. It is an absence."""
        from complylayer.dashboard.views import impact_of

        assert impact_of(self.FakeRule("amount_minor > 1000000"), []) is None

    def test_the_figure_comes_from_the_rows(self):
        from complylayer.backtest import Confidence
        from complylayer.dashboard.views import impact_of

        decisions = [self.StoredDecision(5_000_000) for _ in range(4)]
        decisions += [self.StoredDecision(1_000) for _ in range(6)]

        impact = impact_of(self.FakeRule("amount_minor > 1000000"), decisions)
        assert impact.confidence is Confidence.EXACT
        assert impact.considered == 10
        assert impact.matched == 4, "the figure must come from the rows, not a constant"

    def test_a_rule_needing_an_unrecorded_fact_says_so_rather_than_guessing(self):
        from complylayer.backtest import Confidence
        from complylayer.dashboard.views import impact_of

        impact = impact_of(
            self.FakeRule("days_since(last_transaction_at) > 90"),
            [self.StoredDecision(1_000)],
        )
        assert impact.confidence is Confidence.UNAVAILABLE
        assert "shadow mode" in impact.sentence

    def test_the_history_read_goes_to_the_replica(self):
        """§11.1: the heaviest read in the product stays off the database
        serving decisions."""
        import inspect

        from complylayer.dashboard import views

        source = inspect.getsource(views._recent_decisions)
        assert "using(REPLICA)" in source

    def test_the_rendered_page_carries_no_invented_figures(self):
        """A behavioural guard rather than a grep: the docstring above quotes
        the old literal on purpose, so searching the source for it would find
        the explanation and fail forever."""
        from complylayer.dashboard.views import impact_of

        impact = impact_of(self.FakeRule("amount_minor > 1000000"), [])
        assert impact is None
        rendered = impact.sentence if impact else ""
        for invented in ("48,190", "1,204", "118"):
            assert invented not in rendered
