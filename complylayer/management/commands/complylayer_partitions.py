"""Create the decision partitions for the months ahead.

Safe to run repeatedly. Run it on a schedule and on every deploy: the failure
mode is silent, and running it too often costs nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from django.core.management.base import BaseCommand

from complylayer import partitions


class Command(BaseCommand):
    help = "Create missing monthly partitions on the decisions table."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--months-ahead",
            type=int,
            default=3,
            help="How far ahead to create partitions (default: 3).",
        )

    def handle(self, *args, **options) -> None:
        created = partitions.ensure_partitions(datetime.now(UTC).date(), options["months_ahead"])

        for name in created:
            self.stdout.write(f"created {name}")
        if not created:
            self.stdout.write("all partitions already present")

        stranded = partitions.rows_in_default_partition()
        if stranded:
            self.stdout.write(
                self.style.WARNING(
                    f"{stranded} row(s) are in the default partition. That means this "
                    "command stopped running at some point — those decisions are in one "
                    "unpartitioned heap and should be moved."
                )
            )
