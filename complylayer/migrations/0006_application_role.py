"""A non-superuser application role, so Row Level Security actually applies.

Migration 0005 added the policies. This one makes them mean something.

Postgres exempts SUPERUSER and BYPASSRLS roles from every policy,
unconditionally — `FORCE ROW LEVEL SECURITY` does not change that. So a
deployment connecting as the role `docker-compose` creates by default has
policies that are configured, visible in `\\d`, and never consulted. Tenant
isolation then rests entirely on application code, which is exactly the single
point of failure §8.1's three layers exist to avoid.

The role is created here rather than in a runbook because a security control
that depends on somebody reading a runbook is a security control that is off.
`complylayer_doctor` checks the connecting role for the same reason.

**This migration does not change who the application connects as.** It creates
the role and grants it what it needs; pointing `COMPLYLAYER_DB_USER` at it is a
deployment decision, and doing it silently inside a migration would change how
an existing deployment authenticates without anybody asking for it.
"""

from django.db import migrations

APP_ROLE = "complylayer_app"

# nosec B608 - APP_ROLE is a module constant, not input, and Postgres has no
# placeholder for a role name in DDL.
CREATE_ROLE = f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
        -- NOSUPERUSER and NOBYPASSRLS are the whole point. NOCREATEROLE and
        -- NOCREATEDB because an application does not need them.
        CREATE ROLE {APP_ROLE} NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE LOGIN;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO {APP_ROLE};
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE};
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE};

-- The audit table is the exception, and the reason §8.3 can say immutability is
-- enforced rather than promised: INSERT and SELECT only. The trigger refuses an
-- UPDATE from anyone; this means the application cannot even ask.
REVOKE UPDATE, DELETE, TRUNCATE ON complylayer_auditrecord FROM {APP_ROLE};

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE};
"""

DROP_ROLE = f"""  # nosec B608 - see above
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE};
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE};
REVOKE USAGE ON SCHEMA public FROM {APP_ROLE};
"""


class Migration(migrations.Migration):
    dependencies = [("complylayer", "0005_row_level_security")]

    operations = [migrations.RunSQL(sql=CREATE_ROLE, reverse_sql=DROP_ROLE)]
