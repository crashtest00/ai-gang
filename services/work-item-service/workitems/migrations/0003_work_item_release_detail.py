# canonical-release-workflow.md REQ-01 — the `work_item_release_detail`
# table, following 0001_initial's WorkItemStoryDetail pattern exactly.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('workitems', '0002_work_item_history_append_only'),
    ]

    operations = [
        migrations.CreateModel(
            name='WorkItemReleaseDetail',
            fields=[
                ('work_item', models.OneToOneField(
                    db_column='work_item_id', on_delete=django.db.models.deletion.CASCADE,
                    primary_key=True, related_name='release_detail', serialize=False, to='workitems.workitem',
                )),
                ('release_notes', models.TextField(blank=True, null=True)),
                ('candidate_sha', models.TextField(blank=True, null=True)),
                ('build_identifier', models.TextField(blank=True, null=True)),
                ('preview_url', models.TextField(blank=True, null=True)),
            ],
            options={
                'db_table': 'work_item_release_detail',
            },
        ),
    ]
