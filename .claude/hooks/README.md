# Hooks

## `require-worktree.sh`

Enforces `CLAUDE.md`'s "Worktree precondition on the first write": a
PreToolUse hook that refuses an `Edit`/`Write`/`MultiEdit`/`NotebookEdit` call
targeting a tracked file in the repo's main checkout while that checkout is
on `dev` or `main`. See the script's own header comment for its exact
behavior and sample test invocations.

This script is not registered anywhere yet — an agent session must not edit
`.claude/settings.json` or `.claude/settings.local.json` itself. Register it
by hand, in your own interactive session, with `/update-config`, pasting the
following into project `.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          { "type": "command", "command": "bash \"$CLAUDE_PROJECT_DIR/.claude/hooks/require-worktree.sh\"" }
        ]
      }
    ]
  }
}
```

`.claude/settings.json` is not git-ignored (only `settings.local.json` is),
so this registration is committed and applies to every session on every
machine. Merges into `dev` are unaffected because they use git via Bash, not
Edit/Write; a conflict resolved by hand-editing in the main checkout will be
blocked, and that is intended.
