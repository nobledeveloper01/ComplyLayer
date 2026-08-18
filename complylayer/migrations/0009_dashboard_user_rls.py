"""The eighth tenant-scoped table, which had no policy at all.

`complylayer_dashboarduser` carries a tenant and was not in migration 0005's
list. Nobody removes a table from that list; the mistake is adding a model with
a tenant on it and not thinking about row level security, and that is what
happened in phase 6. The table holds `totp_secret`, so a query that forgot its
tenant filter would return every tenant's second factors.

Found by a test that compares the models carrying a tenant against the migration
list, rather than by anybody reading the list —
`tests/test_rls_every_table.py::TestNothingScopedEscapedTheList`. That test is
the durable part of this fix: the next model to carry a tenant fails CI until
somebody decides about it.

**Same bootstrap problem as the API key table, same shape of answer.** Sign-in
looks a profile up by Django auth user before any tenant is known, so the tenant
policy alone would make the dashboard unreachable exactly as it made the API
unauthenticatable (0008). Resolution gets its own narrow policy, active only
inside a function that sets a flag for the length of one call and returns one
row by user id, which is unique.
"""

from django.db import migrations

TABLE = "complylayer_dashboarduser"

ENABLE = f"""
ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY;

CREATE POLICY {TABLE}_tenant_isolation ON {TABLE}
    USING (tenant_id = current_setting('complylayer.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('complylayer.tenant_id', true));

-- Active only inside complylayer_resolve_dashboard_user(). Ordinary queries
-- never set the flag, so they stay scoped by the policy above.
CREATE POLICY {TABLE}_resolution ON {TABLE}
    FOR SELECT
    USING (current_setting('complylayer.resolving_user', true) = 'on');

-- Returns the pk and the tenant, which is all the caller needs to open a proper
-- tenant scope and then query normally. Deliberately not the whole row: the
-- narrow door stays narrow, and `totp_secret` is not something to hand out
-- through an exemption.
CREATE FUNCTION complylayer_resolve_dashboard_user(candidate_user_id integer)
RETURNS TABLE (id bigint, tenant_id text)
LANGUAGE sql
STABLE
SET complylayer.resolving_user = 'on'
SET search_path = pg_catalog, public
AS $$
    SELECT id::bigint, tenant_id::text
    FROM public.{TABLE}
    WHERE user_id = candidate_user_id
$$;

REVOKE ALL ON FUNCTION complylayer_resolve_dashboard_user(integer) FROM PUBLIC;
"""

GRANT = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'complylayer_app') THEN
        GRANT EXECUTE ON FUNCTION complylayer_resolve_dashboard_user(integer)
            TO complylayer_app;
    END IF;
END
$$;
"""

REVERSE = f"""
DROP FUNCTION IF EXISTS complylayer_resolve_dashboard_user(integer);
DROP POLICY IF EXISTS {TABLE}_resolution ON {TABLE};
DROP POLICY IF EXISTS {TABLE}_tenant_isolation ON {TABLE};
ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY;
"""


class Migration(migrations.Migration):
    dependencies = [("complylayer", "0008_api_key_resolution")]

    operations = [
        migrations.RunSQL(sql=ENABLE, reverse_sql=REVERSE),
        migrations.RunSQL(sql=GRANT, reverse_sql=migrations.RunSQL.noop),
    ]
