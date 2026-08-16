"""Sending analytics reads to the replica.

§11.1: backtests, reports and dashboard queries never touch the database serving
decisions. A 30-day replay is the heaviest read in the product, and running it on
the primary is how a compliance officer testing a rule causes a latency incident
for the customer's transactions.

The router is deliberately explicit rather than clever. It does not try to guess
which reads are analytical; a caller says so by using the `replica` alias
directly, and this router only makes sure writes never go there. Guessing would
eventually send a decision's read to a replica lagging behind its own write.
"""

from __future__ import annotations

REPLICA = "replica"


class ReadReplicaRouter:
    """Writes always go to the primary. Reads go where the caller asked."""

    def db_for_read(self, model, **hints):
        # No guessing. A caller wanting the replica uses `.using("replica")`,
        # which Django honours ahead of the router.
        return None

    def db_for_write(self, model, **hints):
        return "default"

    def allow_relation(self, obj1, obj2, **hints) -> bool:
        """The replica holds the same data, so relations across aliases are fine."""
        return True

    def allow_migrate(self, db, app_label, model_name=None, **hints) -> bool:
        """Never migrate the replica. It is a copy, and writing schema to it
        either fails or, worse, succeeds and diverges."""
        return db == "default"
