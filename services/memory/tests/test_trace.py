"""REQ-07 — pull requests from `pr-link` records and commits from git's
`[<branch> <hash>]` lines, with the squash rule applied within one
transcript file, in the session's transcript and each subagent transcript.
Each test builds the session REQ-07's Acceptance describes, as records."""

import pytest

from archive.models import Session, SessionCommit, SessionPullRequest
from tests.support import Session as Src, bash, completed_run, conv, opening, pr_link, result, tool_call

pytestmark = pytest.mark.django_db


def commits(session_id):
    return [(c.hash, c.role) for c in SessionCommit.objects.filter(session_id=session_id).order_by('id')]


def test_first_session_commits_and_opens_a_pull_request(roots, copier):
    s = Src(roots[0])
    s.append(opening() + [
        bash('t1', 'git checkout -b feature/x && git commit -am "Add x"'),
        result('t1', "Switched to a new branch 'feature/x'\n[feature/x 1a2b3c4] Add x\n 1 file changed\n"),
        bash('t2', 'git push -u origin feature/x && gh pr create --base main --fill'),
        result('t2', 'https://github.com/org/repo/pull/7\n'),
        pr_link(7),
        pr_link(7),  # repeated: de-duplicated
    ] + completed_run()[3:])
    copier.reconcile()
    assert commits(s.id) == [('1a2b3c4', 'authored')]
    prs = list(SessionPullRequest.objects.filter(session_id=s.id).values_list('repository', 'number', 'url'))
    assert prs == [('org/repo', 7, 'https://github.com/org/repo/pull/7')]


def test_second_session_views_and_merges_on_github_and_lists_nothing(roots, copier):
    s = Src(roots[0])
    s.append(opening() + [
        bash('t1', 'gh pr view 7'), result('t1', 'title: Add x\nstate: OPEN\n'),
        bash('t2', 'gh pr merge 7 --squash'), result('t2', '✓ Squashed and merged pull request #7 (Add x)\n'),
    ] + completed_run()[3:])
    copier.reconcile()
    assert commits(s.id) == []
    assert not SessionPullRequest.objects.filter(session_id=s.id).exists()


def test_third_session_squash_commit_is_merged_and_the_next_commit_authored(roots, copier):
    s = Src(roots[0])
    s.append(opening() + [
        bash('t1', 'git merge --squash feature/y'), result('t1', 'Squash commit -- not updating HEAD\n'),
        bash('t2', 'git commit -m "Squash y"'), result('t2', '[main 5d6e7f8] Squash y\n 2 files changed\n'),
        bash('t3', 'git commit --allow-empty -m "Note"'), result('t3', '[main 9a8b7c6] Note\n'),
    ] + completed_run()[3:])
    copier.reconcile()
    assert commits(s.id) == [('5d6e7f8', 'merged'), ('9a8b7c6', 'authored')]


def test_a_squash_and_its_commit_in_one_command_is_merged(roots, copier):
    s = Src(roots[0])
    s.append(opening() + [
        bash('t1', 'git merge --squash feature/z && git commit -m "z" && git commit --allow-empty -m "after"'),
        result('t1', 'Squash commit -- not updating HEAD\n[main (root-commit) abcdef1] z\n[main 1234567] after\n'),
    ])
    copier.reconcile()
    assert commits(s.id) == [('abcdef1', 'merged'), ('1234567', 'authored')]


def test_fourth_session_counts_the_subagents_commits_once_each(roots, copier):
    s = Src(roots[0])
    lines = '[main c0ffee1] Ordinary\n[main d00d0d2] Squash w\n'
    # The parent squashes, starts a subagent, reads the subagent's file, and
    # receives its reply quoting both lines.
    s.append(opening() + [
        bash('p1', 'git merge --squash feature/v'), result('p1', 'Squash commit -- not updating HEAD\n'),
        tool_call('p2', 'Agent', {'prompt': 'commit, squash w, write the lines to /tmp/lines.txt'}),
        result('p2', 'The report was delivered as a message.'),
        {'type': 'queue-operation', 'operation': 'enqueue', 'content': f'<agent-message from="sub">{lines}</agent-message>'},
        tool_call('p3', 'Read', {'file_path': '/tmp/lines.txt'}),
        result('p3', lines),
        result('p2b', lines, as_list=True),
    ])
    # The subagent's own transcript: an ordinary commit, then a squash and
    # its commit, then it writes the two lines to the file.
    s.append([
        bash('a1', 'git commit -am "Ordinary"'), result('a1', '[main c0ffee1] Ordinary\n'),
        bash('a2', 'git merge --squash feature/w'), result('a2', 'Squash commit -- not updating HEAD\n'),
        bash('a3', 'git commit -m "Squash w"'), result('a3', '[main d00d0d2] Squash w\n', as_list=True),
        bash('a4', f"printf '%s' '{lines}' > /tmp/lines.txt"), result('a4', ''),
    ], path=s.sidecar('subagents', 'agent-sub.jsonl'))
    s.append(completed_run()[3:])
    copier.reconcile()
    assert commits(s.id) == [('c0ffee1', 'authored'), ('d00d0d2', 'merged')]


def test_a_hash_line_from_a_command_that_is_not_git_does_not_count(roots, copier):
    s = Src(roots[0])
    s.append(opening() + [bash('t1', 'cat /tmp/lines.txt'), result('t1', '[main c0ffee1] Ordinary\n')])
    copier.reconcile()
    assert commits(s.id) == []


def test_resolving_a_shipped_commit_by_hash_prefix_and_by_pull_request(roots, copier):
    first = Src(roots[0])
    first.append(opening() + [
        bash('t1', 'git commit -m "Add x"'), result('t1', '[feature/x 1a2b3c4] Add x\n'), pr_link(7)])
    copier.reconcile()
    shipped_full_hash = '1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d'
    by_hash = {c.session_id for c in SessionCommit.objects.all() if shipped_full_hash.startswith(c.hash)}
    by_pr = set(SessionPullRequest.objects.filter(repository='org/repo', number=7).values_list('session_id', flat=True))
    assert by_hash == by_pr == {first.id}
    assert Session.objects.filter(pk=first.id).exists()
