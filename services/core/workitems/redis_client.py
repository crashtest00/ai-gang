"""
Redis connection factory. Deliberately a small, separate module rather than
a cross-service require (mirrors the old Node service's own src/redis.js
module comment): a Redis client constructor has no cross-service
correctness requirement to preserve, unlike the Streams wire protocol
(streams.py) or catalog-backed assignment validation (registry.py/
assignment.py), so each service simply needs its own connection to the
same Redis instance/URL.
"""

from __future__ import annotations

import redis
from django.conf import settings


def new_client() -> 'redis.Redis':
    """Build a new Redis client. Each Streams consumer keeps its own
    connection (see streams.Consumer) rather than sharing one across
    blocking reads — decode_responses=True so every value handed back to
    application code is `str`, matching the Node client's default string
    behavior."""
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


_default_client = None


def get_client() -> 'redis.Redis':
    global _default_client
    if _default_client is None:
        _default_client = new_client()
    return _default_client
