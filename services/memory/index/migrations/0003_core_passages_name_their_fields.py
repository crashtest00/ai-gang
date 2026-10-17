"""Knowledge Retrieval REQ-04 (v6.1 pass 2, row 22).

A Core record's unit now names the fields of Core's read its text came from
(``reference.fields``, ``index.render``), and the four Core classes' CHECK
constraints require it.

Units indexed before this have no ``fields``, and the constraint cannot be
added over them, so this migration first removes them, with what the index
recorded about their sources and failures, and drops the four Core
enumerations' cursors. The indexer then reads those classes again from no
cursor, as a rebuild does (Corpus Indexing REQ-03): the index is derived,
so nothing is lost, and until the read reaches its end each class reports
"not indexed yet" (REQ-01, REQ-05) rather than an empty result.
"""

from django.db import migrations, models

CORE_CLASSES = ('work_item', 'work_item_comment', 'delivery_artifact', 'a2a_message')
CORE_CURSORS = ('core:work_items', 'core:comments', 'core:delivery_artifacts', 'core:a2a_tasks')


def drop_core_units(apps, schema_editor):
    for model in ('IndexUnit', 'IndexSource', 'IndexFailure'):
        apps.get_model('index', model).objects.filter(source_class__in=CORE_CLASSES).delete()
    apps.get_model('index', 'IndexCursor').objects.filter(name__in=CORE_CURSORS).delete()


def _keys(*keys):
    return models.Q(reference__has_keys=list(keys))


# Written out here, not imported from index.models, so this migration keeps
# meaning what it meant if the model changes later.
FIELDS = _keys('fields') & models.Q(reference__fields__0__path__isnull=False)
CHECKS = {
    'index_unit_reference_work_item': ('work_item', ('workItemId', 'section')),
    'index_unit_reference_comment': ('work_item_comment', ('commentId', 'workItemId')),
    'index_unit_reference_delivery': ('delivery_artifact', ('deliveryArtifactId', 'workItemId')),
    'index_unit_reference_a2a': ('a2a_message', ('taskId', 'messageId')),
}


class Migration(migrations.Migration):

    dependencies = [
        ('index', '0002_search_words_and_note_archive_path'),
    ]

    operations = [migrations.RunPython(drop_core_units, migrations.RunPython.noop)]
    for name, (source_class, keys) in CHECKS.items():
        operations += [
            migrations.RemoveConstraint(model_name='indexunit', name=name),
            migrations.AddConstraint(
                model_name='indexunit',
                constraint=models.CheckConstraint(
                    check=~models.Q(source_class=source_class) | (_keys(*keys, 'offset', 'end') & FIELDS),
                    name=name)),
        ]
