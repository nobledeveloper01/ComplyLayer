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

from django.core.management.base import BaseCommand
from django.db import transaction

from complylayer import rules as lifecycle
from complylayer.api import auth
from complylayer.tenancy import Actor, Role

# The author and the approver. §10.2's separation of duties means these cannot
# be the same person, so a demo needs both.
ANALYST = Actor(id="ada@demo.ng", role=Role.COMPLIANCE_ANALYST)
OFFICER = Actor(id="chidi@demo.ng", role=Role.COMPLIANCE_OFFICER)

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

        say()
        say(f"      author   {ANALYST.id} ({ANALYST.role})")
        say(f"      approver {OFFICER.id} ({OFFICER.role})")
        say("      two people, because nobody approves their own change")
        say(f"      published rule set version {version.version if version else 0}")
        say()

        # Last line, always, so a script can take it with `tail -1`.
        self.stdout.write(full_key)
