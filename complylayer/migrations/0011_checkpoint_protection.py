"""The checkpoint table gets the same protections as the records it anchors.

Two of them, and both would have been caught by an existing test rather than by
anybody remembering:

**Row level security.** `AuditCheckpoint` carries a tenant, and
`tests/test_rls_every_table.py::TestNothingScopedEscapedTheList` compares the
models carrying one against the policy list. Adding a tenant-scoped model
without a policy fails that test, which is exactly the direction the mistake
gets made — `complylayer_dashboarduser` sat unscoped for two phases.

**Append-only.** A checkpoint that can be rewritten proves whatever the last
writer wanted. The whole point of signing the chain head is that an attacker
with write access cannot forge it; leaving the signature itself editable would
hand back what the signature was protecting — they would simply replace the
checkpoint with one over their rewritten chain.

The signature would not verify afterwards, because they still lack the key. But
`UPDATE` is refused anyway: defence that costs one trigger is defence worth
having, and an auditor should see a refusal rather than a mismatch.
"""

from django.db import migrations

TABLE = "complylayer_auditcheckpoint"

PROTECT = f"""
ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY;

CREATE POLICY {TABLE}_tenant_isolation ON {TABLE}
    USING (tenant_id = current_setting('complylayer.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('complylayer.tenant_id', true));

CREATE OR REPLACE FUNCTION complylayer_checkpoint_is_append_only()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        '{TABLE} is append-only: % on checkpoint % was refused. A checkpoint '
        'that can be rewritten proves whatever the last writer wanted.',
        TG_OP, COALESCE(OLD.id::text, '?')
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER complylayer_checkpoint_append_only
    BEFORE UPDATE OR DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION complylayer_checkpoint_is_append_only();

-- TRUNCATE bypasses row triggers, same as on the audit table.
CREATE TRIGGER complylayer_checkpoint_no_truncate
    BEFORE TRUNCATE ON {TABLE}
    FOR EACH STATEMENT EXECUTE FUNCTION complylayer_checkpoint_is_append_only();
"""

UNPROTECT = f"""
DROP TRIGGER IF EXISTS complylayer_checkpoint_no_truncate ON {TABLE};
DROP TRIGGER IF EXISTS complylayer_checkpoint_append_only ON {TABLE};
DROP FUNCTION IF EXISTS complylayer_checkpoint_is_append_only();
DROP POLICY IF EXISTS {TABLE}_tenant_isolation ON {TABLE};
ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY;
ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY;
"""

GRANT = f"""
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'complylayer_app') THEN
        GRANT SELECT, INSERT ON {TABLE} TO complylayer_app;
        -- No UPDATE, no DELETE. The trigger refuses them anyway; this means the
        -- application cannot even ask.
        REVOKE UPDATE, DELETE, TRUNCATE ON {TABLE} FROM complylayer_app;
    END IF;
END
$$;
"""


class Migration(migrations.Migration):
    dependencies = [("complylayer", "0010_audit_checkpoint")]

    operations = [
        migrations.RunSQL(sql=PROTECT, reverse_sql=UNPROTECT),
        migrations.RunSQL(sql=GRANT, reverse_sql=migrations.RunSQL.noop),
    ]
