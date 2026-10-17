"""The extractor — artifact-text-extraction.md REQ-05 and REQ-06.

A process of its own, in its own image (``Dockerfile`` here), separate from
``memory-extraction``, the Memory process that dispatches work to it. It
parses untrusted, attacker-influenced files, so it holds nothing worth
reaching: no database connection, no Redis connection, no listening socket
and, in the deployment, no network at all (``network_mode: none`` in
``services/memory/docker-compose.yml``). Its only interface is a spool
directory on a volume it shares with the dispatcher (``spool.py``).

Each file is parsed in a child process of its own (``child.py``) under a
wall-clock time limit and kernel resource limits, so a file that crashes
the parser kills only that child: the extractor records the crash as the
file's result and goes on, and the dispatcher is never touched.
"""
