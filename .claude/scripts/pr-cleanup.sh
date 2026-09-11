#!/usr/bin/env bash
# pr-cleanup.sh — post-merge cleanup for one PR: steps 2–5 of CLAUDE.md § Pull requests.
#
# Usage, from the repo root, in exactly this form (it is what .claude/settings.json allowlists):
#   .claude/scripts/pr-cleanup.sh <pr-number>
#
# It never merges: the PR must already be MERGED. Step 1 (`gh pr merge <n> --squash`) stays a
# separate command, because merging is the user's call. It never forces: every destructive step is
# gated on git's own refusal or on <headRefOid>, the commit GitHub merged, and any refusal stops
# the run with exit 1. Re-running is safe; a step whose target is already gone is skipped.
#
# Why a script: the auto-mode classifier judges each destructive git command on its own and
# refuses one whose exact target the user did not name, so the steps typed by hand stall on an
# approval prompt. One allowlisted script skips the classifier and keeps every guard.

set -euo pipefail

stop() { echo "STOP (step $1): $2" >&2; exit 1; }
say()  { echo "step $1: $2"; }

if [[ $# -ne 1 || ! $1 =~ ^[0-9]+$ ]]; then
  echo "usage: .claude/scripts/pr-cleanup.sh <pr-number>" >&2
  exit 2
fi
n=$1

# Run from the main checkout: git will not remove the worktree the caller is standing in.
cd "$(git worktree list --porcelain | sed -n '1s/^worktree //p')"

pr=$(gh pr view "$n" --json state,isCrossRepository,headRefName,headRefOid \
       --jq '[.state, .isCrossRepository, .headRefName, .headRefOid] | @tsv') \
  || stop 1 "cannot read PR #$n"
IFS=$'\t' read -r state cross branch oid <<<"$pr"

[[ $state == MERGED ]] || stop 1 "PR #$n is $state, not MERGED; merging it is the user's call"
[[ $cross == false ]] || stop 1 "PR #$n comes from a fork; its branch is not ours to delete"
case $branch in dev|main) stop 1 "PR #$n's head is $branch, which is never deleted" ;; esac
say 1 "PR #$n merged $branch at $oid"

# Step 2: the branch still belongs to this PR, then remove its worktree.
open=$(gh pr list --head "$branch" --state open --json number --jq length) \
  || stop 2 "cannot list open PRs for $branch"
[[ $open == 0 ]] || stop 2 "an open PR reuses the name $branch"

local_head=$(git rev-parse -q --verify "refs/heads/$branch" || true)
if [[ -n $local_head && $local_head != "$oid" ]]; then
  stop 2 "local $branch is at $local_head, not the merged $oid; work was added after the merge"
fi

wt_path=$(git worktree list --porcelain | awk -v ref="refs/heads/$branch" '
  /^worktree / { path = substr($0, 10) }
  $0 == "branch " ref { print path }')
if [[ -n $wt_path ]]; then
  git worktree remove "$wt_path" \
    || stop 2 "git refused to remove $wt_path (modified, untracked files, or locked)"
  say 2 "removed worktree $wt_path"
else
  say 2 "no worktree has $branch checked out"
fi

# Step 3: local branch. -D only because step 2 proved its head is exactly what was merged.
if [[ -n $local_head ]]; then
  if git branch -d "$branch" >/dev/null 2>&1; then
    say 3 "deleted local $branch"
  else
    git branch -D "$branch" >/dev/null || stop 3 "git refused to delete local $branch"
    say 3 "deleted local $branch (-D: its head equals the merged $oid)"
  fi
else
  say 3 "no local $branch"
fi

# Step 4: remote branch, leased to the merged commit.
remote=$(git ls-remote --heads origin "refs/heads/$branch") || stop 4 "cannot reach origin"
if [[ -n $remote ]]; then
  git push --force-with-lease="refs/heads/$branch:$oid" origin --delete "$branch" \
    || stop 4 "origin rejected the delete; a commit landed on $branch after the merge"
  say 4 "deleted origin/$branch"
else
  say 4 "origin has no $branch"
fi

# Step 5: last, so step 3's -d could still see the tracking ref.
git fetch --prune --quiet || stop 5 "git fetch --prune failed"
say 5 "fetched and pruned"

echo "PR #$n cleanup complete."
