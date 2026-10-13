"""REQ-06 — a session is kept in full without an end signal: quiet on its
newest run's end record or after 35 minutes, active again on growth, and
each transition to quiet counted per harness version."""

import pytest
from django.test import Client

from archive.models import Session
from tests.support import Session as Src, completed_run, conv, cost_state, dequeue, enqueue, opening

pytestmark = pytest.mark.django_db


def counts(version='2.1.296'):
    return Client().get('/counts').json()['versions'].get(
        version, {'unknown': 0, 'unparseable': 0, 'quiet_end_record': 0, 'quiet_interval': 0})


def listing(cursor=None):
    return Client().get('/sessions', {'since': cursor} if cursor else {}).json()


def test_a_completed_run_goes_quiet_on_its_end_record_without_waiting(roots, copier):
    s = Src(roots[0])
    before = counts()
    s.append(completed_run())
    copier.sync_path(s.parts(s.transcript))
    session = Session.objects.get(pk=s.id)
    assert (session.status, session.quiet_by) == ('quiet', 'end_record')
    assert counts()['quiet_end_record'] == before['quiet_end_record'] + 1


def test_a_killed_run_stays_active_for_35_minutes_then_goes_quiet_by_interval(roots, copier, clock):
    s = Src(roots[0])
    before = counts()
    s.append(opening() + [conv('assistant')])
    copier.reconcile()
    clock.advance(minutes=34)
    copier.sweep_quiet()
    assert Session.objects.get(pk=s.id).status == 'active'
    clock.advance(minutes=2)
    copier.sweep_quiet()
    session = Session.objects.get(pk=s.id)
    assert (session.status, session.quiet_by) == ('quiet', 'interval')
    assert counts()['quiet_interval'] == before['quiet_interval'] + 1
    assert counts()['quiet_end_record'] == before['quiet_end_record']


def test_the_sweep_sees_growth_an_event_missed_before_calling_a_session_quiet(roots, copier, clock):
    s = Src(roots[0])
    s.append(opening())
    copier.reconcile()
    clock.advance(minutes=36)
    s.append([conv('assistant')])  # no event, no pass
    copier.sweep_quiet()
    assert Session.objects.get(pk=s.id).status == 'active'


def test_a_resume_returns_the_session_to_active_and_its_end_record_to_quiet(roots, copier):
    s = Src(roots[0])
    s.append(completed_run())
    copier.reconcile()
    before = counts()
    s.append([enqueue('-\nresume: carry on'), dequeue()])
    copier.reconcile()
    assert Session.objects.get(pk=s.id).status == 'active'
    s.append([conv('user'), conv('assistant'), cost_state()])
    copier.reconcile()
    session = Session.objects.get(pk=s.id)
    assert (session.status, session.quiet_by) == ('quiet', 'end_record')
    assert counts()['quiet_end_record'] == before['quiet_end_record'] + 1


def test_growth_without_a_prompt_after_an_end_record_leaves_the_session_quiet(roots, copier):
    s = Src(roots[0])
    s.append(completed_run())
    copier.reconcile()
    cursor = listing()['cursor']
    before = counts()
    s.append([conv('system', subtype='stop_hook_summary'), {'type': 'last-prompt', 'sessionId': 's'}])
    copier.reconcile()
    session = Session.objects.get(pk=s.id)
    assert (session.status, session.quiet_by) == ('quiet', 'end_record')
    assert session.files.get(path__endswith='.jsonl').bytes_read == s.transcript.stat().st_size
    assert s.id in listing(cursor)['sessions']
    assert counts() == before
    s.append([enqueue()])
    copier.reconcile()
    assert Session.objects.get(pk=s.id).status == 'active'


def test_an_end_record_in_a_subagent_transcript_ends_no_run(roots, copier):
    s = Src(roots[0])
    s.append(opening())
    agent = s.append([conv('user')], path=s.sidecar('subagents', 'agent-c.jsonl'))
    copier.reconcile()
    before = counts()
    s.append([cost_state()], path=agent)
    copier.reconcile()
    assert Session.objects.get(pk=s.id).status == 'active'
    assert counts() == before


def test_an_interval_quiet_counts_against_the_newest_runs_last_record_version(roots, copier, clock):
    s = Src(roots[0])
    s.append([enqueue(), dequeue(), conv('user', version='4.0.0'), {'type': 'atis-latch'}])
    copier.reconcile()
    clock.advance(minutes=36)
    copier.sweep_quiet()
    assert counts('4.0.0')['quiet_interval'] == 1
