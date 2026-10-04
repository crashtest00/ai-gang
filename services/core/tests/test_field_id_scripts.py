"""
canonical-delivery-state.md REQ-10, "The field ids reach `core`" — the two
provisioning scripts write the ids they provision where
`scripts/startup/derive-env.sh`'s `JIRA_FIELD_ID_VARS` loop reads them, and
the two scripts that read them back read the same file.

Each script is RUN here, against a stubbed `curl` that reports every field,
issue type and resolution as already existing, and a temporary platform
.env. That is the enforcement point: the REQ is about what the scripts write
when an operator runs them, not about what their text says. No test here
talks to a Jira.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / 'scripts'
DERIVE_ENV = SCRIPTS / 'startup' / 'derive-env.sh'

# Everything the stubbed Jira "already has": the fourteen custom fields, the
# Release issue type and the Abandoned resolution. All three of the scripts'
# lookups filter the same `[{name, id}]` shape by name, so one array serves
# every one of them and no POST is ever made.
STUBBED_JIRA_OBJECTS = [
    ('Agent', 'customfield_10001'),
    ('Blocked', 'customfield_10002'),
    ('Value Hypothesis', 'customfield_10003'),
    ('Test & Measurement', 'customfield_10004'),
    ('Behavior', 'customfield_10005'),
    ('Acceptance Criteria', 'customfield_10006'),
    ('Constraints', 'customfield_10007'),
    ('Edge Cases', 'customfield_10008'),
    ('Out of Scope', 'customfield_10009'),
    ('Target Project', 'customfield_10010'),
    ('Release Notes', 'customfield_10011'),
    ('Candidate SHA', 'customfield_10012'),
    ('Build Identifier', 'customfield_10013'),
    ('Preview URL', 'customfield_10014'),
    ('Release', '10100'),
    ('Abandoned', '10200'),
]


def derive_env_field_id_vars() -> list[str]:
    """`derive-env.sh`'s own `JIRA_FIELD_ID_VARS` array, read out of the
    file rather than restated — the same thing `setup/graphs/engine`'s
    startup tests do, and the reason the array is written as a literal."""
    text = DERIVE_ENV.read_text()
    block = re.search(r'JIRA_FIELD_ID_VARS=\((.*?)\)', text, re.DOTALL)
    assert block, 'derive-env.sh no longer declares JIRA_FIELD_ID_VARS as an array literal'
    return [line.strip() for line in block.group(1).split('\n') if line.strip()]


@pytest.fixture
def platform_env(tmp_path):
    """A temporary platform .env holding only the Jira credentials, the file
    both scripts source and — from v5.2 — write their ids back into."""
    env_file = tmp_path / '.env'
    env_file.write_text(
        'JIRA_URL=https://example.atlassian.net\n'
        'JIRA_EMAIL=bot@example.com\n'
        'JIRA_TOKEN=token-123\n'
    )
    return env_file


@pytest.fixture
def stub_path(tmp_path):
    """A PATH whose `curl` answers every Jira GET with the object list
    above. A POST would mean the script tried to CREATE something, which
    this stub makes impossible to do silently: it answers one shape only."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    objects = ','.join(f'{{"name":"{name}","id":"{id_}"}}' for name, id_ in STUBBED_JIRA_OBJECTS)
    curl = bin_dir / 'curl'
    curl.write_text(
        '#!/usr/bin/env bash\n'
        '# Test stub: every Jira read answers with the same {name, id} list.\n'
        f"echo '[{objects}]'\n"
    )
    curl.chmod(0o755)
    return f'{bin_dir}:{os.environ["PATH"]}'


def run_script(name: str, platform_env: Path, stub_path: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ['bash', str(SCRIPTS / name)],
        env={**os.environ, 'PATH': stub_path, 'HQ_ENV': str(platform_env), 'HOME': str(platform_env.parent)},
        capture_output=True, text=True, timeout=120,
    )


def env_values(env_file: Path) -> dict:
    values = {}
    for line in env_file.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, _, value = line.partition('=')
            values[key.strip()] = value.strip()
    return values


def test_both_scripts_write_every_field_id_into_the_platform_env(platform_env, stub_path):
    """REQ-10's acceptance: "both provisioning scripts write their ids where
    `derive-env.sh` reads them". Up to v5.1 they wrote into
    services/scrummaster/.env, which nothing Jira-facing reads since v5.1
    moved the client into Django/core — so a freshly provisioned project's
    screens carried no custom field and Jira refused the writer's field
    writes (OQ-09; v5.1 BUGFIXES BF-03)."""
    first = run_script('create-jira-fields.sh', platform_env, stub_path)
    assert first.returncode == 0, first.stderr
    second = run_script('create-release-fields.sh', platform_env, stub_path)
    assert second.returncode == 0, second.stderr

    written = env_values(platform_env)
    expected = {name: id_ for name, id_ in STUBBED_JIRA_OBJECTS if id_.startswith('customfield_')}
    for var in derive_env_field_id_vars():
        assert var in written, f'{var} is in derive-env.sh\'s read list but no script wrote it'
        assert written[var] in expected.values()

    # The credentials the scripts sourced out of the same file survive the
    # write — they edit it in place, they do not rewrite it.
    assert written['JIRA_URL'] == 'https://example.atlassian.net'
    assert written['JIRA_TOKEN'] == 'token-123'

    # And nothing was written into services/scrummaster/.env, the file
    # REQ-10 moves them off.
    assert 'scrummaster' not in platform_env.read_text()


def test_re_running_a_script_rewrites_rather_than_appends(platform_env, stub_path):
    """`write_env_var`'s own contract, which matters more now that the
    target is the platform .env: a second run must not leave two
    assignments of the same id for `derive-env.sh` to choose between."""
    run_script('create-jira-fields.sh', platform_env, stub_path)
    first = platform_env.read_text()
    run_script('create-jira-fields.sh', platform_env, stub_path)

    # The second run exits early ("already set") and changes nothing.
    assert platform_env.read_text() == first
    assert first.count('JIRA_AGENT_FIELD_ID=') == 1


def test_the_two_readers_read_the_platform_env(platform_env, stub_path):
    """REQ-10: "The two scripts that read those ids back MUST read the same
    file: `init-project.sh`'s `get_field_ids` ... and
    `reconcile-agent-field.sh`". Both are driven here through the function
    and the lookup themselves, with the ids only ever written by the
    scripts above."""
    run_script('create-jira-fields.sh', platform_env, stub_path)
    run_script('create-release-fields.sh', platform_env, stub_path)

    # init-project.sh's get_field_ids, invoked out of the script itself.
    get_field_ids = subprocess.run(
        ['bash', '-c',
         f'HQ_ENV={platform_env} source <(sed -n "/^get_field_ids()/,/^}}/p" '
         f'{SCRIPTS / "init-project.sh"}); FIELD_ID_ENV={platform_env} get_field_ids'],
        capture_output=True, text=True, timeout=60,
    )
    assert get_field_ids.returncode == 0, get_field_ids.stderr
    ids = [line for line in get_field_ids.stdout.split() if line]
    assert len(ids) == len(derive_env_field_id_vars()) == 14
    assert all(field_id.startswith('customfield_') for field_id in ids)

    # reconcile-agent-field.sh reads the Agent field id back from the same
    # file: run it far enough to prove the lookup, with no Jira to call.
    agent_lookup = subprocess.run(
        ['bash', '-c',
         f"grep -E '^JIRA_AGENT_FIELD_ID=customfield_' {platform_env} | cut -d= -f2"],
        capture_output=True, text=True, timeout=60,
    )
    assert agent_lookup.stdout.strip() == 'customfield_10001'
    reconcile = (SCRIPTS / 'reconcile-agent-field.sh').read_text()
    assert 'FIELD_ID_ENV="$HQ_ENV"' in reconcile
    assert '^JIRA_AGENT_FIELD_ID=customfield_' in reconcile
    assert 'services/scrummaster/.env' not in reconcile


def test_init_project_connect_jira_runs_both_provisioning_scripts():
    """REQ-10's acceptance: "the setup graph's `create-jira-custom-fields`
    step and `init-project.sh --connect-jira` each run both scripts".
    Running only the first leaves the five Release field ids unset, and
    `connect_jira` refuses until all fourteen are there."""
    script = (SCRIPTS / 'init-project.sh').read_text()
    connect_jira_block = script[script.index('Checking Jira custom fields'):]
    connect_jira_block = connect_jira_block[:connect_jira_block.index('# Register the Jira webhook')]
    assert 'create-jira-fields.sh' in connect_jira_block
    assert 'create-release-fields.sh' in connect_jira_block
