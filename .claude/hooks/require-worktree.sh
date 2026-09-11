#!/usr/bin/env bash
# require-worktree.sh — PreToolUse hook (Edit|Write|MultiEdit|NotebookEdit).
#
# Blocks a write to a tracked file in the repo's main checkout, per
# CLAUDE.md's "Worktree precondition on the first write" rule — a linked
# worktree is the default requirement, not just a rule for `dev`/`beta`/`prod`.
# `dev`/`beta`/`prod` are always blocked, no exceptions. Any other branch in the
# main checkout is blocked too, unless MAIN_CHECKOUT_MARKER (below) exists —
# an explicit, deliberate opt-in for the one legitimate reason to write
# directly in the main checkout: so its files show up in a human's file
# navigator (which only ever looks at the main checkout, never a worktree
# under .claude/worktrees/). Create the marker yourself when that's why
# you're here; remove it once you're done, so the next write goes back to
# requiring a worktree by default. Reads the tool call's JSON on stdin and
# looks at `tool_input.file_path`.
#
# It also blocks a write inside a linked worktree whose branch already has an
# open or merged PR, per CLAUDE.md's "a worktree's life ends when its PR opens"
# rule: that worktree and branch are removed once the PR lands, so work added
# afterwards is deleted with it. Merged is included because that is
# when removal is imminent — an earlier revision stopped at open, and so
# stopped protecting at exactly the wrong moment. Opt back in per worktree with
# PR_EDIT_MARKER (`<worktree>/.claude/pr-edit-allowed`) when you are
# deliberately resuming the branch, e.g. to act on review feedback. The marker
# is honored only while the PR is open, so a merge revokes it.
#
# This hook does NOT gate Bash (git merge/commit/etc.) — a prior revision
# briefly added a Bash check for `git merge --squash` in the main checkout,
# but that check couldn't distinguish the sanctioned merge-into-dev step
# (which necessarily runs here, since dev has no other checkout) from the
# unsafe case, and blocked the former outright. It was reverted. The actual
# hazard is concurrent sessions racing ref-mutating git commands in the one
# shared main checkout directory; fixing that needs cross-session mutual
# exclusion, not a static per-call text match.
#
# The same Bash gap applies to the open-PR check: a write made with `sed`, a
# heredoc, or a python one-liner never reaches this hook. It catches the
# common case, not every case — the rule itself is the agent's to keep.
#
# Exit codes: 0 = allow, 2 = block (Claude Code refuses the tool call and
# shows the agent the stderr message).
#
# Sample invocations and their expected exit codes (run from the repo root;
# adjust paths to real tracked files in your checkout):
#
#   # (a) file under the main checkout, main checkout is on dev/beta/prod -> 2
#   echo '{"tool_input":{"file_path":"'"$PWD"'/CLAUDE.md"}}' \
#     | .claude/hooks/require-worktree.sh; echo "exit=$?"
#
#   # (b) file under a linked worktree -> 0
#   echo '{"tool_input":{"file_path":"'"$PWD"'/.claude/worktrees/some-worktree/CLAUDE.md"}}' \
#     | .claude/hooks/require-worktree.sh; echo "exit=$?"
#
#   # (c) file outside any git repository -> 0
#   echo '{"tool_input":{"file_path":"/tmp/x"}}' \
#     | .claude/hooks/require-worktree.sh; echo "exit=$?"
#
#   # (d) file under the main checkout, on a work branch, no opt-in -> 2
#   echo '{"tool_input":{"file_path":"'"$PWD"'/CLAUDE.md"}}' \
#     | .claude/hooks/require-worktree.sh; echo "exit=$?"
#
#   # (e) same as (d), but with the opt-in marker present -> 0
#   touch .claude/main-checkout-allowed
#   echo '{"tool_input":{"file_path":"'"$PWD"'/CLAUDE.md"}}' \
#     | .claude/hooks/require-worktree.sh; echo "exit=$?"
#   rm .claude/main-checkout-allowed
#
#   # (f) file in a linked worktree whose branch has an open PR -> 2
#   WT=.claude/worktrees/<worktree-with-an-open-pr>
#   echo '{"tool_input":{"file_path":"'"$PWD/$WT"'/CLAUDE.md"}}' \
#     | .claude/hooks/require-worktree.sh; echo "exit=$?"
#
#   # (g) same as (f), but with that worktree's opt-in marker present -> 0
#   touch "$WT/.claude/pr-edit-allowed"
#   echo '{"tool_input":{"file_path":"'"$PWD/$WT"'/CLAUDE.md"}}' \
#     | .claude/hooks/require-worktree.sh; echo "exit=$?"
#   rm "$WT/.claude/pr-edit-allowed"
#
#   # (h) same as (g), but that PR has since merged -> 2 (the marker no longer
#   #     counts once the PR is merged)

set -u

input="$(cat)"

# --- Parse tool_input.file_path -------------------------------------------
if command -v jq >/dev/null 2>&1; then
  file_path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty' 2>/dev/null)"
  parse_status=$?
else
  file_path="$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
print((data.get("tool_input") or {}).get("file_path") or "")
' 2>/dev/null)"
  parse_status=$?
fi

if [ "$parse_status" -ne 0 ]; then
  echo "require-worktree.sh: could not parse PreToolUse JSON on stdin (need jq or python3); blocking the write to be safe." >&2
  exit 2
fi

# No file_path (e.g. a tool call with no file target): allow.
if [ -z "$file_path" ]; then
  exit 0
fi

# --- Find the nearest existing ancestor directory --------------------------
# A brand-new file's own directory (or several levels of it) may not exist
# yet; walk up until we find a directory git can actually be run against.
dir="$(dirname -- "$file_path")"
while [ ! -d "$dir" ] && [ "$dir" != "/" ] && [ "$dir" != "." ]; do
  dir="$(dirname -- "$dir")"
done
if [ ! -d "$dir" ]; then
  # Nowhere sensible to resolve to; not our concern (e.g. relative path with
  # no existing ancestor). Allow rather than block on a path we can't check.
  exit 0
fi

# --- Not inside any git repository: allow -----------------------------------
git_dir="$(git -C "$dir" rev-parse --git-dir 2>/dev/null)"
if [ -z "$git_dir" ]; then
  exit 0
fi

git_common_dir="$(git -C "$dir" rev-parse --git-common-dir 2>/dev/null)"

abs_git_dir="$(cd "$dir" 2>/dev/null && cd "$git_dir" 2>/dev/null && pwd -P)"
abs_common_dir="$(cd "$dir" 2>/dev/null && cd "$git_common_dir" 2>/dev/null && pwd -P)"

# --- Linked worktree: allow, unless its branch already has an open PR -------
# CLAUDE.md § Pull requests: a worktree's life ends when its PR opens. Once
# the PR lands, whoever runs the five cleanup steps removes the branch and
# worktree without asking whether the author is still live. Editing here
# means editing something scheduled for deletion.
if [ -n "$abs_git_dir" ] && [ -n "$abs_common_dir" ] && [ "$abs_git_dir" != "$abs_common_dir" ]; then
  wt_root="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)"
  wt_branch="$(git -C "$dir" branch --show-current 2>/dev/null)"

  # The per-worktree opt-in marker is deliberately NOT checked here. It is
  # consulted only once the PR's state is known, and only for an OPEN PR (see
  # the case below), so a merge revokes it. Per-worktree by design, so one
  # session's opt-in never widens another's.

  # Detached HEAD, or no gh to ask: nothing to check. Fail open.
  if [ -z "$wt_branch" ] || ! command -v gh >/dev/null 2>&1; then
    exit 0
  fi

  # `gh pr list` costs a network round trip (~0.4s), which is too much to pay
  # on every single edit, so the answer is cached under the common git dir
  # (shared by every worktree, never part of any working tree) for 60s. The
  # cost of that staleness is bounded and one-directional: for up to a minute
  # after you open a PR, edits in its worktree still pass. The same window
  # applies after a merge: a cached OPEN keeps honoring the opt-in marker for
  # up to a minute after the PR lands. Writes made under the marker pay this
  # lookup too, since the marker only counts once the state is known; the
  # cache absorbs most of that cost.
  cache_dir="$abs_common_dir/pr-open-cache"
  cache_file="$cache_dir/$(printf '%s' "$wt_branch" | tr '/' '_')"
  state=""
  if [ -f "$cache_file" ]; then
    now="$(date +%s)"
    mtime="$(stat -c %Y "$cache_file" 2>/dev/null || echo 0)"
    if [ $((now - mtime)) -lt 60 ]; then
      state="$(cat "$cache_file" 2>/dev/null)"
    fi
  fi

  if [ -z "$state" ]; then
    # Hard timeout: this runs before every edit in every session, so a hung
    # network call must never become a stalled repo. Timing out is a failure,
    # and failures fail open.
    gh_run="gh"
    if command -v timeout >/dev/null 2>&1; then gh_run="timeout 5 gh"; fi
    # OPEN or MERGED, never CLOSED: a closed-unmerged PR hands nothing over for
    # cleanup, so the worktree is still its owner's. OPEN wins if both.
    if pr_ref="$($gh_run pr list --head "$wt_branch" --state all --json number,state,headRefOid \
        -q '(map(select(.state=="OPEN")) + map(select(.state=="MERGED"))) | first // empty | "\(.state):\(.number):\(.headRefOid)"' 2>/dev/null)"; then
      if [ -n "$pr_ref" ]; then state="$pr_ref"; else state="none"; fi
      mkdir -p "$cache_dir" 2>/dev/null && printf '%s' "$state" >"$cache_file" 2>/dev/null
    else
      # gh unauthenticated, offline, no remote, not a GitHub repo: fail open,
      # and don't cache a result we didn't actually get.
      state="none"
    fi
  fi

  pr_state="${state%%:*}"; pr_rest="${state#*:}"
  pr_num="${pr_rest%%:*}"; pr_head="${pr_rest#*:}"
  case "$pr_state" in
    OPEN)
      # The only place the opt-in marker is honored. MERGED below ignores it.
      if [ -n "$wt_root" ] && [ -e "$wt_root/.claude/pr-edit-allowed" ]; then
        exit 0
      fi
      echo "require-worktree.sh: refusing to write '$file_path' — PR #$pr_num is already open for branch '$wt_branch', so this worktree is finished (CLAUDE.md § Pull requests). This worktree and branch are removed once the PR lands, including anything you add now. Put follow-up work on a new branch in a new worktree. If you are deliberately resuming this branch (review feedback, say), stay in this worktree and create '$wt_root/.claude/pr-edit-allowed', removing it when done. It counts only while the PR is open: once the PR merges, writes here are refused again." >&2
      exit 2
      ;;
    MERGED)
      # Branch names get reused: an earlier docs/foo merges, a later topic
      # recreates docs/foo from dev. Squash-merging keeps the old PR's head
      # commit out of dev, so only the branch that PR actually came from
      # contains it. A missing object makes --is-ancestor fail, which allows
      # the write — the same fail-open rule as everywhere else here.
      if git -C "$dir" merge-base --is-ancestor "$pr_head" HEAD 2>/dev/null; then
        echo "require-worktree.sh: refusing to write '$file_path' — PR #$pr_num for branch '$wt_branch' has already merged, so this worktree is finished and due for removal (CLAUDE.md § Pull requests). Anything added here sits on a merged branch and is deleted with it. Put new work on a new branch in a new worktree." >&2
        exit 2
      fi
      ;;
  esac

  exit 0
fi

# --- Main checkout: always block on dev/beta/prod ---------------------------
branch="$(git -C "$dir" branch --show-current 2>/dev/null)"

if [ "$branch" = "dev" ] || [ "$branch" = "beta" ] || [ "$branch" = "prod" ]; then
  echo "require-worktree.sh: refusing to write '$file_path' — the main checkout is on branch '$branch'. Create a branch and worktree under .claude/worktrees/ first (see docs/git-strategy.md), then edit there." >&2
  exit 2
fi

# --- Main checkout on any other branch: block unless explicitly opted in ----
# Requiring a worktree is the default even here — the main checkout may be
# mid-edit by another agent/session, and a stray write can get silently
# swept into an unrelated commit there. The one legitimate exception is
# deliberately keeping a change visible in a human's file navigator, which
# only shows the main checkout. Signal that intent by creating the marker.
repo_root="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)"
marker="$repo_root/.claude/main-checkout-allowed"

if [ -n "$repo_root" ] && [ -e "$marker" ]; then
  exit 0
fi

echo "require-worktree.sh: refusing to write '$file_path' — this is the main checkout (branch '$branch'), not a linked worktree. Create a branch and worktree under .claude/worktrees/ first (see docs/git-strategy.md), then edit there. If you deliberately need this change visible in the main checkout (e.g. for a human's file navigator), create '$repo_root/.claude/main-checkout-allowed' and remove it when done." >&2
exit 2
