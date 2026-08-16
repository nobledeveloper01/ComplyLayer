"""Setting the tenant for the life of one transaction.

`SET LOCAL` rather than `SET`, and inside an explicit atomic block, because the
setting has to die with the transaction. On a pooled connection a session-scoped
setting outlives the request that made it and is inherited by whoever borrows the
connection next — which is the exact cross-tenant read RLS exists to stop.

pgbouncer runs in transaction mode for the same reason. Session mode plus
`SET LOCAL` is fine; session mode plus one stray session-scoped `SET` is not, and
transaction mode removes the possibility rather than relying on nobody making
that mistake.
"""

from __future__ import annotations

from contextlib import contextmanager

from django.db import connection, transaction

SETTING = "complylayer.tenant_id"


@contextmanager
def tenant_scope(tenant_id: str):
    """Run a block with RLS scoped to one tenant.

    The setting is cleared on the way out as well as set on the way in. `SET
    LOCAL` is scoped to the *outermost* transaction, and `transaction.atomic()`
    inside an existing transaction opens a savepoint rather than a transaction —
    so releasing that savepoint does not restore the previous value. Nested
    scopes are ordinary (a request-level atomic, a service-level one), so
    clearing explicitly is what makes the scope actually scoped.
    """
    previous = current_tenant()
    with transaction.atomic():
        _set(tenant_id)
        try:
            yield
        finally:
            _set(previous or "")


def _set(tenant_id: str) -> None:
    with connection.cursor() as cursor:
        # set_config with a bind parameter, because SET LOCAL would need the
        # value interpolated into the statement.
        cursor.execute("SELECT set_config(%s, %s, true)", [SETTING, tenant_id])


def current_tenant() -> str | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting(%s, true)", [SETTING])
        value = cursor.fetchone()[0]
    return value or None
