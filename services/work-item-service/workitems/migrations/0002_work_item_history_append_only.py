# canonical-work-item-schema.md REQ-05 / internal-work-item-service.md REQ-07:
# "This table has no UPDATE/DELETE grant in the persistence layer... enforced
# there, not merely by convention." A trigger enforces this at the database
# level regardless of which role connects (PGUSER is env-configurable, so a
# role-specific REVOKE would not be portable across environments the way this
# is) and regardless of caller — including a future code path in store.py
# that might otherwise try to mutate a history row by mistake.

from django.db import migrations

CREATE_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION reject_work_item_history_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'work_item_history is append-only: % is not permitted', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER work_item_history_append_only
    BEFORE UPDATE OR DELETE ON work_item_history
    FOR EACH ROW EXECUTE FUNCTION reject_work_item_history_mutation();
"""

DROP_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS work_item_history_append_only ON work_item_history;
DROP FUNCTION IF EXISTS reject_work_item_history_mutation();
"""


class Migration(migrations.Migration):

    dependencies = [
        ('workitems', '0001_initial'),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_TRIGGER_SQL, reverse_sql=DROP_TRIGGER_SQL),
    ]
