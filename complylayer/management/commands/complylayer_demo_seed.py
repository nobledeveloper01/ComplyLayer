"""Seed a tenant that can actually take a decision, for `make demo`.

Everything here goes through the real lifecycle rather than writing rows: a
draft is created, approval is requested, **a different person approves it**, and
activation publishes the rule set version. That is slower than inserting the
snapshot directly and it is the point — the demo would otherwise skip the one
workflow the product exists to enforce, and a seed that bypasses separation of
duties is a seed that cannot prove separation of duties works.

Two actors, for the same reason. `require_not_author` refuses an approval from
the person who wrote the rule, whatever their role, so seeding with one identity
fails at the approve step. Anyone reading this file learns that in ten lines.

Prints the API key once, on stdout, because that is the only time it exists in
the clear — the database holds an Argon2id hash.
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.core.management.base import BaseCommand
from django.db import transaction

from complylayer import rules as lifecycle
from complylayer.api import auth
from complylayer.tenancy import Actor, Role

# The author and the approver. §10.2's separation of duties means these cannot
# be the same person, so a demo needs both.
ANALYST = Actor(id="ada@demo.ng", role=Role.COMPLIANCE_ANALYST)
OFFICER = Actor(id="chidi@demo.ng", role=Role.COMPLIANCE_OFFICER)

# Fixed, printed, and worthless: this account only ever exists on a throwaway
# database that the demo drops on exit.
DEMO_PASSWORD = "demo-password-not-for-production"  # noqa: S105
DEMO_TOTP_SECRET = "JBSWY3DPEHPK3PXP"  # noqa: S105

# Three rules, chosen so one transaction hits each outcome. Kept small on
# purpose: the demo is a proof that decisions happen, not a tour of the DSL.
RULES = [
    {
        "name": "Above the tier 1 daily limit",
        "category": "limits",
        "expression": "amount_minor > 1000000",
        "severity": "block",
        "priority": 10,
        "regulatory_reference": "CBN AML/CFT §4.2.1",
        "customer_message": "This transfer is above your daily limit. "
        "Upgrade your tier to continue.",
    },
    {
        "name": "More than five transfers in an hour",
        "category": "velocity",
        "expression": "velocity_count(window='1h') > 5",
        "severity": "flag",
        "priority": 20,
        "regulatory_reference": "CBN AML/CFT §6.1",
    },
    {
        "name": "Structuring under the reporting threshold",
        "category": "aml",
        "expression": (
            "velocity_count(window='24h', min_amount_minor=450000, max_amount_minor=500000) >= 3"
        ),
        "severity": "block",
        "priority": 5,
        "regulatory_reference": "NFIU Reporting Threshold Guidance",
        "customer_message": "We cannot complete this transfer. Please contact support.",
    },
]


class Command(BaseCommand):
    help = "Create a demo tenant, an API key and an active rule set. For `make demo`."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--tenant", default="tnt_demo")
        parser.add_argument("--name", default="Demo Fintech")
        parser.add_argument(
            "--quiet-narrative",
            action="store_true",
            help="Print only the API key, for scripts that want to capture it.",
        )
        parser.add_argument(
            "--dashboard",
            action="store_true",
            help=(
                "Also create two sign-in accounts and a rule awaiting approval, so the "
                "dashboard has something to show. Used to capture the screenshots in "
                "the README."
            ),
        )

    def handle(self, *args, **options) -> None:
        from complylayer.models import ApiKey, Tenant

        tenant_id = options["tenant"]
        narrate = not options["quiet_narrative"]

        def say(line: str = "") -> None:
            if narrate:
                self.stdout.write(line)

        with transaction.atomic():
            tenant, _ = Tenant.objects.get_or_create(
                id=tenant_id, defaults={"name": options["name"]}
            )

            full_key, prefix = auth.generate_key("live")
            ApiKey.objects.create(
                id=f"key_{prefix[-8:]}",
                tenant=tenant,
                name="demo integration",
                prefix=prefix,
                hashed_secret=auth.hash_secret(full_key),
                environment="live",
                role=Role.COMPLIANCE_OFFICER,
                created_by=OFFICER.id,
            )

            version = None
            for spec in RULES:
                rule = lifecycle.create_draft(tenant_id=tenant_id, actor=ANALYST, **spec)
                lifecycle.request_approval(rule=rule, actor=ANALYST)
                lifecycle.approve(rule=rule, actor=OFFICER, reason="demo seed")
                rule, version = lifecycle.activate(rule=rule, actor=OFFICER)
                say(f"      {rule.id}  {rule.name}  [{rule.severity}]")

        if options["dashboard"]:
            self._seed_dashboard(tenant, say)

        say()
        say(f"      author   {ANALYST.id} ({ANALYST.role})")
        say(f"      approver {OFFICER.id} ({OFFICER.role})")
        say("      two people, because nobody approves their own change")
        say(f"      published rule set version {version.version if version else 0}")
        say()

        # Last line, always, so a script can take it with `tail -1`.
        self.stdout.write(full_key)

    def _seed_history(self, tenant, count: int = 400) -> None:
        """Enough decision history for the approval page to backtest against.

        The impact panel is a real backtest over recorded decisions now, and it
        renders nothing at all when there is no history — which is correct, and
        which would make the screenshot show an empty panel. So the demo gets a
        plausible spread: mostly small transfers, a tail of large ones.
        """
        from datetime import timedelta

        from complylayer.models import Decision

        moment = datetime.now(UTC)
        rows = []
        for index in range(count):
            # Three bands, so the approval diff has something to say: one in
            # eight clears ₦100,000 (both the current rule and the proposed
            # one), one in eight sits between the two thresholds (the current
            # rule only), the rest are ordinary transfers.
            if index % 8 == 0:
                amount = 10_500_000 + index * 40_000
            elif index % 8 == 4:
                amount = 1_500_000 + index * 15_000
            else:
                amount = 20_000 + index * 37
            rows.append(
                Decision(
                    id=f"dec_seed_{index:04d}",
                    tenant=tenant,
                    decided_at=moment - timedelta(minutes=index),
                    idempotency_key=f"seed-{index:04d}",
                    ruleset_version=1,
                    transaction_ref=f"TXN-SEED-{index:04d}",
                    customer_ref_hash="s" * 64,
                    amount_minor=amount,
                    currency="NGN",
                    context={},
                    resolved_facts={"amount_minor": amount, "currency": "NGN"},
                    outcome="block" if amount > 1_000_000 else "allow",
                    latency_ms=5,
                )
            )
        Decision.objects.bulk_create(rows, ignore_conflicts=True)

    def _seed_dashboard(self, tenant, say) -> None:
        """Two sign-in accounts and a rule waiting to be approved.

        The approval diff compares a pending rule against the most recent other
        rule of the same *name*, so a diff needs two rows: the active one and
        the proposed replacement. Without that second row the approval page
        renders with nothing to compare and the screenshot shows an empty panel.

        The TOTP secret is fixed and printed. That is fine here and nowhere
        else: this account exists on a throwaway database that is dropped when
        the demo exits.
        """
        from django.contrib.auth.models import User

        from complylayer.models import DashboardUser, Rule

        for actor, password in ((ANALYST, DEMO_PASSWORD), (OFFICER, DEMO_PASSWORD)):
            user, created = User.objects.get_or_create(
                username=actor.id, defaults={"email": actor.id}
            )
            if created:
                user.set_password(password)
                user.save(update_fields=["password"])
            DashboardUser.objects.get_or_create(
                user=user,
                defaults={
                    "tenant": tenant,
                    "role": str(actor.role),
                    "totp_secret": DEMO_TOTP_SECRET,
                    "totp_confirmed_at": datetime.now(UTC),
                },
            )

        # The proposed change: the same rule, ten times looser. This is the case
        # the approval diff exists for — a reviewer scanning a text diff sees one
        # character move and approves it.
        active = (
            Rule.objects.filter(tenant=tenant, name=RULES[0]["name"]).order_by("-version").first()
        )
        # A proposed replacement is a new *version* of the same name — the model
        # is unique on (tenant, name, version), and the approval view finds the
        # thing to diff against by name. Reusing version 1 fails the constraint,
        # which is the schema saying the same thing.
        proposed = lifecycle.create_draft(
            tenant_id=tenant.id,
            actor=ANALYST,
            **{
                **RULES[0],
                "expression": "amount_minor > 10000000",
                "version": (active.version if active else 1) + 1,
            },
        )
        lifecycle.request_approval(rule=proposed, actor=ANALYST)

        self._seed_history(tenant)

        say()
        say("      dashboard  http://127.0.0.1:8421/dashboard/sign-in")
        say(f"      sign in as {OFFICER.id} / {DEMO_PASSWORD}")
        say(f"      TOTP secret {DEMO_TOTP_SECRET}")
        say(f"      {proposed.id} is awaiting approval: ₦10,000 → ₦100,000")
        if active is None:  # pragma: no cover - defensive, the seed just made it
            say("      (no active version to diff against)")
