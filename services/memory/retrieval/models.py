"""The query log (Knowledge Retrieval REQ-03).

Every retrieval leaves one row: the question, the scope declaration the
request made (or none), the passages returned with the scope each carried,
and whether the index reported possible incompleteness. The row holds the
result exactly as it was returned, so what an agent was told is recoverable
after the fact (PRD §7.7).

The log is not corpus content (REQ-02). It is a table of Memory's own that
the indexer never reads: ``index`` builds units only from the seven source
classes and the platform corpus, and ``QueryLog`` is none of them. Retrieval
reads ``index_unit`` alone, so a log entry cannot be returned as a passage.
"""

from django.db import models


class QueryLog(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    question = models.TextField()
    # What the request declared: 'project', 'platform', or null for none.
    scope_declared = models.CharField(max_length=16, null=True, blank=True)
    # The scopes actually searched.
    scopes_searched = models.JSONField()
    # limit, session weight and cap as applied.
    parameters = models.JSONField()
    # The whole response body, as returned.
    response = models.JSONField()
    # REQ-03: whether the index reported possible incompleteness.
    index_incomplete = models.BooleanField()

    class Meta:
        db_table = 'retrieval_query_log'
        constraints = [
            models.CheckConstraint(
                check=models.Q(scope_declared__isnull=True) | models.Q(scope_declared__in=('project', 'platform')),
                name='retrieval_query_log_scope_valid'),
        ]
