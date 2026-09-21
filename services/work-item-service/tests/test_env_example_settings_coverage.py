"""
V4 audit Pass 2 row 26 — the missing direction of `.env.example`
completeness.

The engine's own test
(`setup/graphs/engine/test/startup-docs.test.js`, "every variable the
.env.example declares is one settings.py reads...") only enforces that
direction: every NAME `.env.example` declares has a real reader. Nothing
enforced the reverse, so `PROJECTS_REPO_SUBDIR` (settings.py:178) shipped
without a line in `.env.example` documenting it (V4 audit Pass 2 row 26).

This asserts the missing direction from this side of the fence instead,
since `setup/` belongs to a concurrent docs track this pass does not own:
every environment variable `workitemservice/settings.py` itself reads via
`os.environ.get(...)`, `os.getenv(...)` or `os.environ[...]` (single or
double quotes) is declared somewhere in `.env.example` (commented out or
not) — so a setting can never be added to `settings.py` again without
`.env.example` growing to document it.

The one documented exception in `.env.example`'s own header — the Jira
custom-field ids, read directly via `os.environ` by
`workitems/jira_interpret.py` and `workitems/webhook_consumer.py` rather
than by `settings.py` — does not need restating here: this test only
reads `settings.py`, so those names never appear in `read_names` to begin
with.
"""

from __future__ import annotations

import re
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parent.parent


def test_every_setting_settings_py_reads_from_the_environment_is_declared_in_env_example():
    settings_source = (SERVICE_ROOT / 'workitemservice' / 'settings.py').read_text()
    env_example = (SERVICE_ROOT / '.env.example').read_text()

    read_names = set(re.findall(
        r"(?:os\.environ\.get\(|os\.getenv\(|os\.environ\[)\s*['\"]([A-Z][A-Z0-9_]*)['\"]",
        settings_source,
    ))
    assert read_names, (
        'expected settings.py to read at least one environment variable via '
        'os.environ.get(...), os.getenv(...) or os.environ[...]'
    )

    declared_names = set(re.findall(r'^#?\s*([A-Z][A-Z0-9_]*)=', env_example, re.MULTILINE))

    missing = sorted(read_names - declared_names)
    assert not missing, (
        f'settings.py reads {missing} from the environment but .env.example does not declare '
        f'{"it" if len(missing) == 1 else "them"}'
    )
