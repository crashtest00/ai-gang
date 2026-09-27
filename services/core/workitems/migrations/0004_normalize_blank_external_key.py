# Data migration: an existing row with external_key = '' (rather than
# NULL) predates the fix that stops the admin, and any other writer, from
# ever storing '' there again (see WorkItem.save() / WorkItemAdminForm in
# workitems/admin.py). Left as '' it would still collide with the next
# work item saved with a blank external key, since a unique constraint
# treats '' as an ordinary value but always lets NULL through.

from django.db import migrations


def normalize_blank_external_key(apps, schema_editor):
    WorkItem = apps.get_model('workitems', 'WorkItem')
    WorkItem.objects.filter(external_key='').update(external_key=None)


def noop_reverse(apps, schema_editor):
    # '' and NULL were never meaningfully distinct in this column — there
    # is nothing a reverse migration should restore.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('workitems', '0003_work_item_release_detail'),
    ]

    operations = [
        migrations.RunPython(normalize_blank_external_key, noop_reverse),
    ]
