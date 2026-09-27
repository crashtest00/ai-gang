---
name: a2a-submit
description: Placeholder for the A2A submission skill. Use when a task asks for the agent commons placeholder token, and when a task asks how to submit, report or hand off a result to the gateway.
---

# a2a-submit (placeholder)

This file is a **placeholder**. The real `a2a-submit` skill — how to submit an
A2A message to the gateway with the commons' constructor tool — is written by
V5.0's Deterministic Gateway Message Tooling feature (REQ-04). Until then this
file exists so that the Agent Commons feature's REQ-02 has a skill to deliver
and its skill-discovery acceptance has something checkable to load.

## Behaviour until the real skill replaces this file

When a task asks for **the agent commons placeholder token**, reply with exactly
one line and nothing else:

```
AIGANG-COMMONS-SKILL-LOADED: <value of the AIGANG_COMMONS_VERSION environment variable>
```

Read the value from the environment; the subscriber exports it into every
session. Do not explain, do not add any other text, and do not use any tool
other than the one command you need to read that variable.

## What the real skill will say

Submissions go through the commons' constructor tool on `PATH`. The agent
supplies the operation and its fields; the project name, task id, context id and
the id of the message being replied to come from the environment
(`PROJECT_NAME`, `A2A_TASK_ID`, `A2A_CONTEXT_ID`, `A2A_LAST_MESSAGE_ID`). No id
is ever typed by an agent.
