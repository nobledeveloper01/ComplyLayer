"""Row Level Security: the third isolation layer (§8.1, D4).

Layers one and two — a key resolving to one tenant, and a query layer that scopes
every read — are application code. This one is not. If a query ever reaches the
database without a tenant filter, RLS returns nothing rather than everything.
That is the difference between a bug that leaks one tenant's rules to another and
a bug that returns an empty list.

Two ways to get this wrong, both closed here.

**`FORCE ROW LEVEL SECURITY`, not plain `ENABLE`.** A table's owner bypasses RLS
unless it is forced. D4 says the application role should not own any table, and
that is right for production — but relying on remembering it for every future
table is a control that holds until the day it doesn't. Forcing it means the
policy applies to everyone, owner included, so the deployment topology cannot
silently disable the protection.

**`SET LOCAL`, never `SET`.** The tenant is a transaction-scoped setting. A
session-scoped one on a pooled connection leaks one tenant's context into the
next request that borrows that connection, which is precisely the failure RLS was
added to prevent.
"""

from django.db import migrations

TENANT_SCOPED_TABLES = [
    "complylayer_rule",
    "complylayer_rulesetversion",
    "complylayer_decision",
    "complylayer_idempotency",
    "complylayer_auditrecord",
    "complylayer_namedlist",
    "complylayer_apikey",
]


def _enable(table: str) -> str:
    return f"""
ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {table} FORCE ROW LEVEL SECURITY;

-- current_setting(..., true) returns NULL rather than raising when the setting
-- is absent, so a connection that never set a tenant sees nothing at all. That
-- is the safe direction: a forgotten SET LOCAL produces an empty result, not an
-- unscoped one.
CREATE POLICY {table}_tenant_isolation ON {table}
    USING (tenant_id = current_setting('complylayer.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('complylayer.tenant_id', true));
"""


def _disable(table: str) -> str:
    return f"""
DROP POLICY IF EXISTS {table}_tenant_isolation ON {table};
ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
"""


class Migration(migrations.Migration):
    dependencies = [("complylayer", "0004_apikey")]

    operations = [
        migrations.RunSQL(sql=_enable(table), reverse_sql=_disable(table))
        for table in TENANT_SCOPED_TABLES
    ]
