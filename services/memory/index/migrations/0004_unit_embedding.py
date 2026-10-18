"""Hybrid retrieval (v6.1 follow-up track 5): every unit carries its text's
embedding from the local model (``index.embedding``), and the database
refuses a unit without one.

Units indexed before this have no embedding, and the constraint cannot be
added over them, so this migration first empties the index as a rebuild
does (Corpus Indexing REQ-03): every unit, what the index recorded about its
sources, its failures, cursors, specification links and state. The indexer
then reads every class again from no cursor, embedding each unit as it
writes it. The index is derived, so nothing is lost, and until each read
reaches its end the class reports "not indexed yet" (REQ-01, REQ-05) rather
than an empty result.
"""

from django.db import migrations, models
from django.db.models.functions import Length, Mod
from django.db.models.lookups import Exact, GreaterThan

# Written out here, not imported from index.embedding, so this migration
# keeps meaning what it meant if the model changes later: 768 float32s.
VECTOR_BYTES = 768 * 4


def empty_the_index(apps, schema_editor):
    for model in ('IndexUnit', 'IndexSource', 'IndexFailure', 'IndexCursor', 'IndexState', 'SpecificationLink'):
        apps.get_model('index', model).objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('index', '0003_core_passages_name_their_fields'),
    ]

    operations = [
        migrations.RunPython(empty_the_index, migrations.RunPython.noop),
        migrations.AddField(
            model_name='indexunit',
            name='embedding',
            field=models.BinaryField(default=b''),
            preserve_default=False,
        ),
        migrations.AddConstraint(
            model_name='indexunit',
            constraint=models.CheckConstraint(
                check=models.Q(GreaterThan(Length('embedding'), 0))
                & models.Q(Exact(Mod(Length('embedding'), VECTOR_BYTES), 0)),
                name='index_unit_embedding_whole_vectors'),
        ),
    ]
