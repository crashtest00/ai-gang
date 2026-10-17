"""Knowledge Retrieval REQ-04 / Corpus Indexing REQ-02 (pass 1, rows 21
and 23).

``search`` gains a search-only second reading of a delivery artifact's text
(``index.models.SEARCH``), so the words inside its URL are found without being
written into the unit's text. PostgreSQL cannot alter a generated column's
expression, so the column and its GIN index are dropped and added again; the
column is generated, so every existing row is recomputed.

A `memory/` note unit's reference must name its archived copy
(``archivePath``): that is the path its passage resolves through.
"""

import django.contrib.postgres.indexes
import django.contrib.postgres.search
from django.db import migrations, models

# Written out here, not imported from index.models, so this migration keeps
# meaning what it meant if the model's expression changes later.
SEARCH = django.contrib.postgres.search.SearchVector(
    'text',
    models.Case(
        models.When(source_class='delivery_artifact',
                    then=models.Func(models.F('text'), models.Value('[^[:alnum:]]+'), models.Value(' '),
                                     models.Value('g'), function='regexp_replace', output_field=models.TextField())),
        default=models.Value(''), output_field=models.TextField()),
    config='english')


class Migration(migrations.Migration):

    dependencies = [
        ('index', '0001_initial'),
    ]

    operations = [
        migrations.RemoveIndex(model_name='indexunit', name='idx_index_unit_search'),
        migrations.RemoveField(model_name='indexunit', name='search'),
        migrations.AddField(
            model_name='indexunit',
            name='search',
            field=models.GeneratedField(db_persist=True, expression=SEARCH,
                                        output_field=django.contrib.postgres.search.SearchVectorField()),
        ),
        migrations.AddIndex(
            model_name='indexunit',
            index=django.contrib.postgres.indexes.GinIndex(fields=['search'], name='idx_index_unit_search'),
        ),
        migrations.RemoveConstraint(model_name='indexunit', name='index_unit_reference_session'),
        migrations.AddConstraint(
            model_name='indexunit',
            constraint=models.CheckConstraint(
                check=(~models.Q(source_class='session')
                       | (models.Q(reference__has_keys=['sessionId', 'file', 'turn', 'lines', 'lineStart', 'lineEnd',
                                                        'offset', 'end'])
                          & (~models.Q(reference__has_key='parentSessionId')
                             | models.Q(reference__has_key='toolUseId')))
                       | models.Q(reference__has_keys=['project', 'path', 'archivePath', 'digest', 'lineStart',
                                                       'lineEnd', 'offset', 'end'])),
                name='index_unit_reference_session'),
        ),
    ]
