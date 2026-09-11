# Branching

Work on a short-lived branch cut from `dev`, never directly on `dev`, `beta`, or `prod`:

- `feat/<topic>`, `fix/<topic>`, `docs/<topic>`, `chore/<topic>`, `test/<topic>`
- One topic per branch. Delete it after merging into `dev`.

Full rules (merge policy, worktrees, versioning, commit conventions): [docs/git-strategy.md](docs/git-strategy.md).

# Pull requests

Merging a topic branch into `dev` (see Branching above) follows one of two paths, chosen by
concurrency. Count `.claude/worktrees/*` entries besides the one being merged and the
main-branch mirror (`.claude/worktrees/main`):

- **Solo** (that count is 0): merge directly with `git merge --squash`, as before.
- **Concurrent** (that count is 1 or more): open a PR instead of merging directly. PRs route the
  merge through GitHub's serialized server-side merge, avoiding the shared-main-checkout race
  where a concurrent `git checkout` can silently clobber another session's staged squash output.

Delete the branch once it's merged, either way — removing its worktree first, since git refuses to
delete a branch that a worktree still has checked out.

**A worktree's life ends when its PR opens.** On the concurrent path the session that owns the
branch and the session that merges it are no longer the same, so the owner cannot be relied on to
say when it is finished with the worktree — and cleanup needs to happen without asking.
Opening the PR is that signal: from that moment the author stops editing the worktree, and once
the PR lands, the branch and worktree are cleaned up with the five steps below, without asking
whether the author is still live. Any session may run them on any merged PR, because every
destructive step is gated on git's own refusal or on `<headRefOid>`, the commit GitHub merged
(`gh pr view <n> --json headRefOid`). Whoever merges runs them. A merge in the GitHub UI has no
agent merger, so the human tells the author session, or any session if the author is gone, and
that session runs them. Any session may also run them on leftovers it finds. A refusal at any
step means something real is at risk: stop and report it, and never force past it.

1. `gh pr merge <n> --squash`, without `--delete-branch`. Branch cleanup is steps 2–5. (Already
   done if the PR was merged in the GitHub UI.)
2. First confirm the branch still belongs to this PR: `gh pr list --head <branch> --state open`
   returns nothing, and, where they exist, the local branch head and the worktree's HEAD equal
   `<headRefOid>`. If either check fails, the name was reused or work was added after the merge:
   stop. Then `git worktree remove <path>` (skip if there's no worktree), never `--force`. It
   refuses a dirty tree: stop. This comes before step 3 because git refuses to delete a branch
   that a worktree still has checked out.
3. `git branch -d <branch>` (skip if there's no local branch). If it refuses, `git branch -D` is
   allowed only when the branch's local head equals `<headRefOid>`, which proves nothing exists
   beyond what was merged. Otherwise stop.
4. If `git ls-remote --heads origin <branch>` returns nothing, skip this step. Otherwise run
   `git push --force-with-lease=refs/heads/<branch>:<headRefOid> origin --delete <branch>`. A
   rejection means a commit landed on the branch after the merge: stop.
5. `git fetch --prune`. It must come last: pruning earlier removes the tracking ref that step 3's
   `-d` checks against, and `-d` then refuses every squash-merged branch.

**Steps 2–5 are one command: `.claude/scripts/pr-cleanup.sh <n>`**, run from the repo root in
exactly that form. `.claude/settings.json` allowlists it, so it runs without an approval prompt.
The same steps typed by hand do not, because the auto-mode classifier refuses each destructive
command whose exact target the user did not name. The user's "merge <n>" is the only
authorization the whole sequence needs: run step 1, then the script, in the same turn, without
asking again. For a PR merged in the GitHub UI, the script alone. It refuses a PR that is not yet
merged, so it can never merge anything itself. The list above is the spec it implements: change
both together.

**Then tell the author.** Whoever ran the cleanup, unless it is the author, messages the author
session named in the PR's identity trailer. The message gives the PR number and merge commit; says
the worktree is gone and must not be used again, including by any shell still sitting in it; and
reports what was cleaned up, or the step that stopped and why. The author acts only on a stop:
uncommitted changes at step 2 are the author's to resolve, because only the author knows whether
they matter. Session names change when a session restarts, so if the trailer's name doesn't
resolve in `ListAgents`, skip the message and report any stop to the human instead. Confirm the
outcome by checking the branch, worktree and remote branch, not by trusting a reply; send reports
can be wrong.

- Do not open a PR until the work on that branch is complete. A PR is a hand-off, not a checkpoint.
- After opening a PR, treat that worktree as read-only. Follow-up work — anything the PR did not
  already contain — goes on a **new branch in a new worktree**, even when it is a direct
  continuation.
- To resume work on an open PR's branch (review feedback, say), stay in its existing worktree
  and create `.claude/pr-edit-allowed` there, removing it when done. The marker counts only while
  the PR is open. Once the PR merges, the hook refuses writes there again, within about a minute,
  and that refusal is how you learn the PR has merged and the worktree is due for cleanup.

A PreToolUse hook (`.claude/hooks/require-worktree.sh`) refuses `Edit`/`Write` in a worktree whose
branch has an open or merged PR; its `.claude/pr-edit-allowed` opt-in works for an open PR only.
It does not gate `Bash`, so a write made through `sed`, a heredoc, or a script still lands — the
rule is yours to keep, and the hook only catches the common case.

Either way, the resulting commit ends with an identity trailer:

Session ID: <sessionId>
Session Name: <sessionName>

- `sessionId` — the full session UUID, found in the session's transcript path under
  `~/.claude/projects/**/<sessionId>.jsonl`. This is the durable lookup key.
- `sessionName` — the short display name `ListAgents` reports for the session (e.g. `ai-gang-e3`)
  — not the bracketed ref shown alongside it (e.g. `[f57894]`), which is a separate identifier
  this trailer doesn't use. This is ephemeral: it stops resolving once the session ends, so treat
  it as a convenience label alongside `sessionId`, not a substitute for it.

`sessionId` and `sessionName` come from unrelated sources — the local `~/.claude/projects`
transcript files and `ListAgents`' own session registry, respectively — and neither can be
derived from the other. Don't go looking for one by searching for the other; capture both
directly from the session that's producing the commit.

This trailer is attribution-only: it identifies which session produced the change, but does not
currently guarantee `claude --resume <sessionId>` will reopen it for every producer.

# Worktree precondition on the first write

Before the first Edit or Write to any tracked file, confirm the file is inside a linked worktree,
not the main checkout — regardless of what branch the main checkout happens to be on. `dev`,
`beta`, and `prod` are always blocked in the main checkout, with no exception. Any other branch in the main
checkout is blocked too by default: it may be mid-edit by another agent or session, and a stray
write there can get silently swept into an unrelated commit. If it is not in a linked worktree,
create the branch and worktree first, then edit there. This applies to a session that started as a
discussion and later begins editing; the rule triggers on the write, not on how the session started.

The one deliberate exception is keeping a change visible in a human's file navigator, which only
ever shows the main checkout, never a worktree under `.claude/worktrees/`. Signal that intent by
creating `.claude/main-checkout-allowed` (untracked) before writing there, and remove it once done,
so the next write goes back to requiring a worktree by default.

A PreToolUse hook (`.claude/hooks/require-worktree.sh`) enforces this and will refuse the write.

# Act on authorization in the same turn

When the user authorizes an action ("you can merge", "go ahead", "proceed"), perform it in that
same turn and report the resulting state (commit hash, branch, whether pushed) in the same reply.
Never acknowledge an authorization and defer the action to a later turn.

# End with the next step

Every turn ends with a `**Next Step:**` line naming who acts and what they do. Take the first
branch that applies:

1. Decisions were presented — point at them (`You answer decisions 1-2 above`). Don't restate the
   questions.
2. Something is unresolved — state the question.
3. A PR is open — `You merge PR #<n>`.
4. Otherwise — the concrete action you intend to take next, phrased as an action with an owner.
   "Here's what I found" is not a next step.

The line is always present, even when it's obvious. It matters most in long context, which is
exactly when an "it's obvious here" exception gets taken.

# Bounded verification runs

Wrap every background test or verification command in `timeout`, with a bound of roughly three
times the last known duration of that suite (default 15 minutes when unknown). Before describing a
still-running background command as normal, compare its elapsed time against the prior runs of the
same command in this session; if it has exceeded them, investigate instead of waiting.
# Citing Docs & Formatting

- **Quote, don't reference.** When a point depends on what a doc actually says, quote the relevant lines verbatim (with file:line) instead of summarizing or pointing at the file.
- **Break up text.** Use headers, bullets, and short lines (1-2 sentences each) instead of dense prose paragraphs, even in short exploratory answers — a bullet wrapping a paragraph doesn't count. Prefer tables for comparisons.

# Status Reporting & Decisions

When reporting status on multiple independent items (branches, PRs, files, review findings) where some are blocked and some aren't:

1. **Triage first.** Partition into "ready, no caveats" vs. "needs a decision" before narrating any detail.
2. **Explain each blocker causally.** Name the actual mechanism (why/how it breaks), not just that a problem might exist.
3. **Surface secondary issues found as a side effect as their own item.** Don't silently fold a newly-noticed problem into the primary fix.
4. **Present every real decision as a discrete, labeled unit.** Confirmation-of-a-plan vs. genuine either/or, scoped to exactly what it affects, with concrete consequence-bearing options — not open-ended "what do you want to do?" This applies to any single decision point in a conversation, not only multi-item status reports — don't bury a decision in a closing prose question or under a lean/recommendation paragraph.
5. **Quote the material text when it helps** (see "Citing Docs & Formatting" above) — not a hard requirement when a plain description is enough.
6. **Use this exact shape by default:** a table (or other compact structure) laying out the findings — one row per item, terse — followed by a separate, clearly-headed "Decisions" section physically apart from the table, numbering only the genuine decision points (1, 2, 3...). Within each numbered decision, if the real options are few and enumerable, present them as a nested numbered sub-list — indented under the decision as a true Markdown ordered list (each decision's sub-list restarts at 1), not flush-left options that only read as sub-items — rather than open-ended prose. Do not use lettered sub-options (a, b, c): they are not native Markdown list syntax, so agents keep typing them as plain indented text that renders flush-left instead of as a real sublist. Prefer AskUserQuestion for this when the options are genuinely mutually exclusive and only the user can choose.
