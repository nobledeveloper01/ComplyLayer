"""Make the audit trail append-only at the database, not in the application.

§8.3 says immutability is enforced rather than promised. An ORM that never calls
`save()` on an audit record is a promise; a trigger that raises on UPDATE is
enforcement. The distinction matters most in the case you cannot test for in CI:
somebody at a psql prompt during an incident, with every good intention, tidying
a record that looks wrong.

The trigger fires regardless of role — superuser included — so the failure is
loud and immediate rather than silent and permanent.

**One operational consequence, discovered by it breaking a test.** Anything that
empties the database with TRUNCATE now fails on this table: `manage.py flush`,
pytest-django's `transaction=True` teardown, and any restore script that clears
before loading. That is the trigger working, not a defect — but it is a surprise
the first time somebody meets it, so: to reset a *non-production* database, run
`ALTER TABLE complylayer_auditrecord DISABLE TRIGGER USER` around the flush. If
anybody reaches for that on production, the trail is already gone as far as an
auditor is concerned. Grants are applied
separately, at deploy time, because the application role's name is a deployment
concern rather than a schema one; `complylayer_doctor` checks both.
"""

from django.db import migrations

CREATE_TRIGGER = """
CREATE OR REPLACE FUNCTION complylayer_audit_is_append_only()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'complylayer_auditrecord is append-only: % on audit record % was refused. '
        'Corrections are appended with a compensating record carrying "corrects", '
        'never applied to the original.',
        TG_OP, COALESCE(OLD.id, '?')
        USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER complylayer_audit_append_only
    BEFORE UPDATE OR DELETE ON complylayer_auditrecord
    FOR EACH ROW EXECUTE FUNCTION complylayer_audit_is_append_only();

-- TRUNCATE bypasses row-level triggers entirely, so it needs its own statement
-- trigger. Without this, one command empties the entire evidence trail.
CREATE TRIGGER complylayer_audit_no_truncate
    BEFORE TRUNCATE ON complylayer_auditrecord
    FOR EACH STATEMENT EXECUTE FUNCTION complylayer_audit_is_append_only();
"""

DROP_TRIGGER = """
DROP TRIGGER IF EXISTS complylayer_audit_no_truncate ON complylayer_auditrecord;
DROP TRIGGER IF EXISTS complylayer_audit_append_only ON complylayer_auditrecord;
DROP FUNCTION IF EXISTS complylayer_audit_is_append_only();
"""


class Migration(migrations.Migration):
    dependencies = [("complylayer", "0001_initial")]

    operations = [migrations.RunSQL(sql=CREATE_TRIGGER, reverse_sql=DROP_TRIGGER)]
