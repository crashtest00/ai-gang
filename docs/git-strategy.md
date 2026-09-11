# Git Strategy

**Purpose**: Define how the AI Gang repository is branched, promoted, versioned, and tagged.

This repository uses the same `dev` / `beta` / `prod` shape that `docs/release-strategy.md` and
`setup/DEVOPS_HANDBOOK_v1.md` describe for the product repositories AI Gang manages.

---

## Branches

| Branch | Holds | How changes arrive |
|---|---|---|
| `dev` | Integrated, unreleased work | Squash merges of work branches |
| `beta` | The release candidate under validation | A human-approved `dev → beta` pull request |
| `prod` | Released code. Release tags live here | A human-approved `beta → prod` pull request, for a revision whose validation passed |

- All three are permanent. Nothing is pushed to `beta` or `prod` directly.
- Pipeline credentials cannot approve or bypass either promotion.
- Clone `prod` to run a released AI Gang. Clone `dev` to work on it.

### Work branches

All day-to-day work happens on a short-lived branch cut from `dev`.

| Prefix | Use |
|---|---|
| `feat/<topic>` | New capability in code |
| `fix/<topic>` | Defect fix |
| `docs/<topic>` | Documentation only |
| `chore/<topic>` | Tooling, cleanup, dependency updates, non-functional changes |
| `test/<topic>` | Test cases and test tooling |

- One topic per branch. A branch that picks up a second purpose is split.
- A branch that lives longer than a few days is rebased on `dev` before merging.
- Branches are squash-merged into `dev`, directly or through a pull request (see `CLAUDE.md`
  § Pull requests), and deleted once merged.

---

## Promotion

1. A human decides to promote. An agent may prepare the `dev → beta` pull request.
2. A human reviews and approves it. The merge into `beta` triggers automated validation.
3. When validation passes, a separate `beta → prod` pull request is opened for that revision.
   A human approves and merges it.
4. The release is tagged on `prod`.

```text
feat/x    ●──●──●╮
fix/y          ●─┼─●╮
                  ▼  ▼
dev       ──●──●──●──●──●──●──●──●──▶
                     │           │
                     ▼ PR + human approval
beta      ───────────●───────────●──▶   (merge triggers validation)
                     │
                     ▼ PR + human approval, validation passed
prod      ───────────●──────────────▶
                  (tag v3.0.0)
```

---

## Versions

- Semantic Versioning. Annotated tags `v<major>.<minor>.<patch>`, for example `v3.0.0`.
- Tags go on `prod`, at the merge commit of the `beta → prod` pull request, and are pushed to
  `origin`.
- A tag is never moved. A mistaken release gets a new patch tag.

---

## Commits and merges

- Conventional Commits, matching the branch prefixes: `feat(scope): …`, `fix(scope): …`,
  `docs: …`, `chore: …`, `test: …`. The subject says what changed; the body says why when the
  diff does not make it obvious.
- Work branch → `dev`: squash. Promotions (`dev → beta`, `beta → prod`): merge commits, so each
  promoted batch stays one traceable point.
- Pushed merge commits are never rewritten. Only unmerged work branches are rebased or
  force-pushed.

---

## Worktrees

Parallel streams of work get parallel worktrees rather than stashes or context switches.

- Create them with `git worktree add` under `.claude/worktrees/`, which is git-ignored.
- One branch per worktree. Remove a finished worktree with `git worktree remove`, then
  `git worktree prune`.

---

## Remote hygiene

- `origin` carries `dev`, `beta`, `prod`, every tag, and any work branch under review.
- Merged work branches are deleted from `origin`. `git fetch --prune` is the normal fetch.
- Secrets never enter history. `.env` and credential files are git-ignored; templates
  (`.env.template`) are committed.
