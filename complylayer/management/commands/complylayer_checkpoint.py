"""Sign every tenant's audit chain head.

Run on a schedule. The interval is the window in which an attacker with write
access can rewrite history undetected, so it is a real operational parameter
rather than a tidiness one: hourly means at most an hour of forgeable past.

`--generate-key` prints a fresh Ed25519 pair and writes nothing. The private half
goes in a secret manager and never in the database this anchors.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from complylayer import audit
from complylayer.audit import checkpoint as cp


class Command(BaseCommand):
    help = "Sign the current audit chain head for every tenant."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--generate-key",
            action="store_true",
            help="Print a new Ed25519 key pair and exit. Writes nothing.",
        )
        parser.add_argument("--tenant", default="", help="Only this tenant.")

    def handle(self, *args, **options) -> None:
        from complylayer.models import Tenant

        if options["generate_key"]:
            private, public = cp.generate_key()
            self.stdout.write(self.style.WARNING("Private key — secret manager, never the DB:"))
            self.stdout.write(private)
            self.stdout.write(self.style.SUCCESS("Public key — safe to publish:"))
            self.stdout.write(public)
            return

        private_pem = settings.COMPLYLAYER["CHECKPOINT_PRIVATE_KEY"]
        if not private_pem:
            self.stdout.write(
                self.style.ERROR(
                    "COMPLYLAYER_CHECKPOINT_PRIVATE_KEY is not set, so there is nothing to "
                    "sign with. Generate one with --generate-key. Until then the audit "
                    "chain detects an edit in place and not a rewrite."
                )
            )
            raise SystemExit(1)

        tenants = Tenant.objects.all()
        if options["tenant"]:
            tenants = tenants.filter(id=options["tenant"])

        signed = 0
        for tenant in tenants:
            record = audit.write_checkpoint(tenant_id=tenant.id, private_pem=private_pem)
            if record is None:
                self.stdout.write(f"  {tenant.id}: no audit records yet, nothing to anchor")
                continue
            signed += 1
            self.stdout.write(
                f"  {tenant.id}: {record.chain_length} records anchored at {record.head_hash[:20]}…"
            )

        self.stdout.write(self.style.SUCCESS(f"{signed} chain(s) signed."))
