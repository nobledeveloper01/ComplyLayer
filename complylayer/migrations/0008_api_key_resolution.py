"""The one lookup that cannot be scoped by tenant: the one that finds the tenant.

Migration 0005 put `complylayer_apikey` behind the same tenant policy as every
other table. That is wrong in a way nothing caught, because the whole product is
developed and tested as a superuser, and superusers skip every policy.

Under `complylayer_app` — the role migration 0006 creates and
`complylayer_doctor` tells operators to deploy with — the policy is consulted,
`current_setting('complylayer.tenant_id', true)` is NULL because no tenant is
known yet, and the lookup returns zero rows. Every request 401s. The two
controls are mutually exclusive: a deployment can have row level security or it
can have authentication, and until now the only working configuration was the
one where layer three does nothing.

Proven rather than reasoned about: `SELECT count(*) FROM complylayer_apikey
WHERE prefix = ...` as `complylayer_app` returns 0 where the same query as the
superuser returns 1.

**The fix is a second policy, not an exemption.** Dropping the table out of RLS
would work and would give up the protection for every *other* query that touches
it — a management endpoint listing keys without a tenant filter would silently
list everyone's. Instead the resolution path gets its own narrow door:

- a `FOR SELECT` policy that applies only while `complylayer.resolving_key` is
  set, and
- a function that sets it for the duration of one call, by prefix, which is
  unique.

Policies are OR'd, so ordinary queries still see only their own tenant's rows.
The function is `SET`-scoped rather than `SECURITY DEFINER`: `FORCE ROW LEVEL
SECURITY` subjects even the table owner to policies, so definer rights would not
have helped, and this avoids handing out an owner-privileged function to work
around it.
"""

from django.db import migrations

RESOLVER = """
-- Applies only inside complylayer_resolve_api_key(), which sets the flag for
-- the duration of one call and no longer. Ordinary queries never set it, so
-- they stay scoped by the tenant policy added in 0005.
CREATE POLICY complylayer_apikey_resolution ON complylayer_apikey
    FOR SELECT
    USING (current_setting('complylayer.resolving_key', true) = 'on');

-- STABLE, and one row by a unique column. The SET clause also stops the planner
-- inlining the body, which is what makes the flag actually apply.
CREATE FUNCTION complylayer_resolve_api_key(candidate_prefix text)
RETURNS TABLE (
    id text,
    tenant_id text,
    hashed_secret text,
    environment text,
    role text,
    revoked_at timestamptz
)
LANGUAGE sql
STABLE
SET complylayer.resolving_key = 'on'
SET search_path = pg_catalog, public
AS $$
    SELECT id::text, tenant_id::text, hashed_secret::text,
           environment::text, role::text, revoked_at
    FROM public.complylayer_apikey
    WHERE prefix = candidate_prefix
$$;

REVOKE ALL ON FUNCTION complylayer_resolve_api_key(text) FROM PUBLIC;
"""

# Granted separately so the migration still applies on a database where the role
# was never created — 0006 deliberately does not force a deployment onto it.
GRANT = """
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'complylayer_app') THEN
        GRANT EXECUTE ON FUNCTION complylayer_resolve_api_key(text) TO complylayer_app;
    END IF;
END
$$;
"""

REVERSE = """
DROP FUNCTION IF EXISTS complylayer_resolve_api_key(text);
DROP POLICY IF EXISTS complylayer_apikey_resolution ON complylayer_apikey;
"""


class Migration(migrations.Migration):
    dependencies = [("complylayer", "0007_dashboarduser")]

    operations = [
        migrations.RunSQL(sql=RESOLVER, reverse_sql=REVERSE),
        migrations.RunSQL(sql=GRANT, reverse_sql=migrations.RunSQL.noop),
    ]
