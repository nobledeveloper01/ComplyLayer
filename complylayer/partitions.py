"""Monthly partition maintenance for the decisions table.

A partitioned table with no partition for today's date still works — rows land
in the default partition — which is precisely why this needs a job and an alert
rather than good intentions. Everything keeps running while decisions quietly
pile into one unpartitioned heap, and the problem surfaces months later as a
query that used to be fast.

So: create partitions ahead of time, and treat any row in the default partition
as a signal that this stopped running.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from django.db import connection

TABLE = "complylayer_decision"

# Partition names are built from a module constant and a calendar month, never
# from anything a request supplies. Asserted rather than asserted-in-a-comment,
# because this file interpolates identifiers into DDL and Postgres has no
# placeholder for those.
_SAFE_NAME = re.compile(r"[a-z_][a-z0-9_]*\Z")


def _identifier(name: str) -> str:
    if not _SAFE_NAME.match(name):
        raise ValueError(f"unsafe partition identifier: {name!r}")
    return name


DEFAULT_PARTITION = f"{TABLE}_default"


@dataclass(frozen=True)
class Partition:
    name: str
    start: date
    end: date

    @property
    def label(self) -> str:
        return self.start.strftime("%Y-%m")


def _month_start(moment: date) -> date:
    return moment.replace(day=1)


def _next_month(moment: date) -> date:
    return (
        date(moment.year + 1, 1, 1)
        if moment.month == 12
        else date(moment.year, moment.month + 1, 1)
    )


def partitions_for(start: date, months: int) -> list[Partition]:
    """The partitions covering ``months`` calendar months from ``start``."""
    result = []
    current = _month_start(start)
    for _ in range(months):
        following = _next_month(current)
        result.append(Partition(name=f"{TABLE}_{current:%Y_%m}", start=current, end=following))
        current = following
    return result


def ensure_partitions(today: date, months_ahead: int = 3) -> list[str]:
    """Create any missing partitions. Returns the names actually created.

    Idempotent, so it is safe to run on every deploy as well as on a schedule —
    which is the point, because the failure mode is silent and the cost of
    running it too often is nothing.
    """
    created: list[str] = []
    with connection.cursor() as cursor:
        for partition in partitions_for(today, months_ahead + 1):
            cursor.execute(
                "SELECT to_regclass(%s) IS NOT NULL",
                [f"public.{partition.name}"],
            )
            if cursor.fetchone()[0]:
                continue
            cursor.execute(
                f'CREATE TABLE "{_identifier(partition.name)}" '
                f'PARTITION OF "{_identifier(TABLE)}" '
                f"FOR VALUES FROM ('{partition.start:%Y-%m-%d}') "
                f"TO ('{partition.end:%Y-%m-%d}')"
            )
            created.append(partition.name)
    return created


def rows_in_default_partition() -> int:
    """Anything here means partition maintenance has stopped. Alert on it."""
    with connection.cursor() as cursor:
        # nosec B608 - the identifier is a module constant checked by
        # _identifier(); Postgres has no placeholder for a table name.
        cursor.execute(f'SELECT count(*) FROM ONLY "{_identifier(DEFAULT_PARTITION)}"')  # nosec  # noqa: S608  # nosemgrep
        return cursor.fetchone()[0]


def existing_partitions() -> list[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_inherits i ON i.inhrelid = c.oid
            JOIN pg_class parent ON parent.oid = i.inhparent
            WHERE parent.relname = %s
            ORDER BY c.relname
            """,
            [TABLE],
        )
        return [row[0] for row in cursor.fetchall()]
