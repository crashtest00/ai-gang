"""REQ-05: the extractor is a separate process, out of reach, holding no
connection, and a file that crashes it does not terminate the dispatcher.
Also the packaging that makes it so (docker-compose.yml, the extractor's
image) and the container list operators read (gate 4).

Not here, because they need a deployment: the egress capture across one
file per format and the reachability check from a client network. Those
run live on the disposable test environment (the build brief, §2).
"""

import os
import re
from pathlib import Path

import pytest
from django.db import connection

from extraction.models import ExtractionRecord
from tests.extraction_support import (  # noqa: F401 - fixtures
    MEMORY_DIR, core, heavy_html, process_env, redis_events, spool_dir, start_extractor, start_worker, stop,
    wait_for,
)

pytestmark = pytest.mark.django_db(transaction=True)
REPO_ROOT = MEMORY_DIR.parent.parent
COMPOSE = (MEMORY_DIR / 'docker-compose.yml').read_text()


def _service(name):
    match = re.search(rf'^  {re.escape(name)}:\n((?:    .*\n|\n)*)', COMPOSE, re.M)
    assert match, f'docker-compose.yml has no {name} service'
    return match.group(1)


def test_a_file_that_crashes_the_extractor_does_not_terminate_the_dispatching_worker(
        core, spool_dir, redis_events, settings):
    settings.MEMORY_EXTRACTION_SWEEP_SECONDS = 3600
    extractor = start_extractor(spool_dir, '--cpu-limit-seconds', '1')
    worker = start_worker(process_env(settings, connection.settings_dict['NAME']))
    try:
        from extraction import jobs
        crasher = core.upload(heavy_html(), 'crash.html')
        jobs.enqueue(crasher, 'created')
        assert wait_for(lambda: ExtractionRecord.objects.filter(artifact_id=crasher).exists(), 60)
        assert worker.poll() is None and extractor.poll() is None  # both still running
        after = core.upload(b'Extracted after the crash.\n', 'after.txt')
        jobs.enqueue(after, 'created')
        assert wait_for(lambda: ExtractionRecord.objects.filter(artifact_id=after).exists(), 60)
    finally:
        stop(worker)
        stop(extractor)
    assert ExtractionRecord.objects.get(artifact_id=crasher).failure_reason.startswith('extractor crashed')
    assert ExtractionRecord.objects.get(artifact_id=after).text == 'Extracted after the crash.\n'


def test_the_running_extractor_holds_no_socket_of_any_kind(spool_dir):
    extractor = start_extractor(spool_dir)
    try:
        assert wait_for(lambda: (spool_dir / 'requests').exists(), 10)
        targets = []
        for fd in os.listdir(f'/proc/{extractor.pid}/fd'):
            try:
                targets.append(os.readlink(f'/proc/{extractor.pid}/fd/{fd}'))
            except OSError:
                continue  # closed between the listing and the read
    finally:
        stop(extractor)
    assert targets, 'expected to read the extractor process\'s descriptors'
    assert not [t for t in targets if t.startswith('socket:')], targets


def test_the_extractor_code_imports_no_network_database_or_django_module():
    forbidden = re.compile(r'^\s*(?:import|from)\s+(socket|http|urllib|requests|redis|psycopg2|django|ssl|ftplib|'
                           r'smtplib|asyncio)\b', re.M)
    for source in (MEMORY_DIR / 'extractor').glob('*.py'):
        assert not forbidden.findall(source.read_text()), source.name


def test_compose_gives_the_extractor_no_network_no_port_and_no_credential():
    extractor = _service('memory-extractor')
    assert re.search(r'^    network_mode: none$', extractor, re.M)
    for key in ('networks:', 'ports:', 'expose:', 'env_file:', 'environment:'):
        assert key not in extractor
    assert 'build: ./extractor' in extractor
    assert '- memory-extraction-spool:/spool' in extractor


def test_compose_runs_the_dispatcher_from_memorys_image_sharing_only_the_spool():
    dispatcher = _service('memory-extraction')
    assert 'command: python manage.py run_extraction' in dispatcher
    assert 'memory-extraction-spool:/var/lib/aigang/memory/extraction-spool' in dispatcher
    assert 'MEMORY_EXTRACTION_SPOOL=/var/lib/aigang/memory/extraction-spool' in dispatcher
    assert 'ports:' not in dispatcher


def test_compose_caps_the_indexer_at_two_threads_and_two_cpus_and_leaves_the_api_uncapped():
    indexer = _service('memory-indexer')
    assert re.search(r'^      - MEMORY_EMBED_THREADS=2$', indexer, re.M)
    assert re.search(r'^    cpus: "2"$', indexer, re.M)
    api = _service('memory-api')
    assert 'MEMORY_EMBED_THREADS' not in api
    assert 'cpus:' not in api


def test_the_extractor_image_carries_its_parser_and_nothing_that_reaches_out():
    dockerfile = (MEMORY_DIR / 'extractor' / 'Dockerfile').read_text()
    requirements = (MEMORY_DIR / 'extractor' / 'requirements.txt').read_text()
    assert 'pip install --no-cache-dir -r requirements.txt' in dockerfile  # at build time (I-08)
    assert 'EXPOSE' not in dockerfile
    packages = [ln.split('>=')[0].split('==')[0].strip() for ln in requirements.splitlines()
                if ln.strip() and not ln.startswith('#')]
    assert packages == ['pypdf']


def test_the_suite_and_the_image_pin_the_same_parser_version():
    def pin(path):
        return [ln for ln in path.read_text().splitlines() if ln.startswith('pypdf')]
    assert pin(MEMORY_DIR / 'requirements.txt') == pin(MEMORY_DIR / 'extractor' / 'requirements.txt') != []


def test_the_operator_docs_list_every_memory_container_compose_defines():
    services = re.findall(r'^  ([a-z][a-z0-9-]*):\n', COMPOSE.split('\nnetworks:')[0], re.M)
    guide = (REPO_ROOT / 'docs' / 'ClaudeInstructions.md').read_text()
    section = guide[guide.index('### 2.6 Memory'):]
    section = section[:section.index('\n---')]
    listed = re.search(r'Memory runs (\w+) containers', section)
    assert listed and listed.group(1) == {6: 'six', 7: 'seven'}.get(len(services), str(len(services)))
    for service in services:
        assert f'`{service}`' in section, service
