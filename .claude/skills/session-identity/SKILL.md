---
name: session-identity
description: Use when the user asks this agent's name, session name/ID, or "who are you" — look it up with ListAgents and report it, instead of saying the agent has no name.
---

# Session identity

Every session has a name. When the user asks for it — "what's your name", "what session is
this", "who am I talking to" — don't say there isn't one. Look it up:

1. Call `ListAgents`. Its result opens with a line naming this session, e.g.:
   `This session is ai-gang-e3 [f57894] — the name other sessions use to message it...`
2. Report that name and id directly: "This session's name is `ai-gang-e3` (id `f57894`)."

**If the user wants the durable session UUID instead of the short display name** (e.g. to trace a
PR back to this session, or to `claude --resume` it later): `ListAgents` does not return it. Find
it from this session's own scratchpad path or environment block (it embeds the UUID), or as the
most recently modified `*.jsonl` file under `~/.claude/projects/<project>/`. Tell the user
explicitly that this UUID and the `ListAgents` display name are two different identifiers: the
UUID is durable, the display name is ephemeral and stops resolving once the session ends.
