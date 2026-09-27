"""
The minimum canonical status vocabulary.
Direct port of the Node service's src/statusVocabulary.js. Fixed names,
never stored in the database, never reconfigurable — the minimum
ten statuses remain available under their fixed names regardless of
project configuration.
"""

MINIMUM_STATUSES = (
    'proposed',
    'ready',
    'waiting-on-dependency',
    'assigned',
    'in-progress',
    'needs-clarification',
    'in-review',
    'done',
    'cancelled',
    'failed',
)

MINIMUM_STATUS_SET = set(MINIMUM_STATUSES)

# Terminal-ish statuses the parent-rollup policy keys off of.
TERMINAL_STATUSES = ('done', 'cancelled', 'failed')


def is_minimum_status(status: str) -> bool:
    return status in MINIMUM_STATUS_SET


def validate_status(status, custom_statuses):
    """`custom_statuses`: iterable of objects/dicts with `status` and
    `baseline_status` attributes/keys (already loaded for this project) —
    this function does no I/O of its own so it can be unit tested without a
    database. Returns {'ok': True, 'baseline': ...} or {'ok': False,
    'reason': ...}."""
    if not isinstance(status, str) or len(status) == 0:
        return {'ok': False, 'reason': 'status must be a non-empty string'}
    if is_minimum_status(status):
        return {'ok': True, 'baseline': status}

    custom = None
    for c in custom_statuses or []:
        c_status = c['status'] if isinstance(c, dict) else c.status
        if c_status == status:
            custom = c
            break
    if custom is None:
        return {
            'ok': False,
            'reason': f'"{status}" is not one of the minimum canonical statuses and is not declared as a custom status for this project',
        }
    baseline_status = custom['baseline_status'] if isinstance(custom, dict) else custom.baseline_status
    if not is_minimum_status(baseline_status):
        return {
            'ok': False,
            'reason': f'custom status "{status}" declares an invalid baseline_status "{baseline_status}" — must be one of the minimum ten',
        }
    return {'ok': True, 'baseline': baseline_status}


def baseline_of(status, custom_statuses):
    """Resolve a status to its baseline (itself, if already a minimum-set
    status) — used by parent-rollup and dispatch-eligibility logic."""
    result = validate_status(status, custom_statuses)
    return result['baseline'] if result['ok'] else None
