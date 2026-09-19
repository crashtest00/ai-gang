# ScrumMaster

The routing service between the work-item store and the agent containers. It
consumes canonical work-item events, decides what to dispatch and to whom,
publishes the dispatch onto the assigned agent's stream, and applies what the
agents send back — comments, subtasks, reassignments, outcomes — to the work
item they belong to. Every one of those paths runs over Redis Streams; the
service holds no database of its own.

Its two catalogs are read once, at startup:

- `config/agents.json` — the agents that exist, and the stream suffix each one
  listens on.
- `config/projects.json` — the projects, and which agents each may use. A
  project added here needs a restart before anything routes to it.

## Running the tests

```bash
npm test
```

The runner is `node --test --test-concurrency=1`: one file at a time,
deliberately. Several files share a real Redis instance and flush it between
cases, so running them concurrently makes them clear each other's state.

**A real Redis has to be listening first.** `test/streams.test.js`,
`test/idempotency.test.js`, `test/gateway.integration.test.js` and
`test/dispatch.integration.test.js` all talk to one at `localhost:16399`
rather than faking the transport, because what they are testing is the
transport's own behaviour: consumer groups, acknowledgement, redelivery,
retries and dead-lettering.

Nothing in this package provides that Redis. The only thing that does is the
test compose file next door, which is where the port number comes from:

```bash
# from the repository root, before running the tests
docker compose -f services/work-item-service/docker-compose.test.yml up --wait

cd services/scrummaster && npm test

# from the repository root, when you are done
docker compose -f services/work-item-service/docker-compose.test.yml down -v
```

`npm test` checks the port before it starts anything and stops with that
instruction if nothing answers. The check never starts the containers itself:
when they run, and whether their data is thrown away afterwards, stays the
caller's decision. Without it a bare `npm test` looked like it was working and
then hung — the Redis client retries a refused connection rather than failing,
so the first Redis-backed file waited forever with nothing on screen to say
what was missing.

To run a file that needs no Redis without that check, add `--ignore-scripts`.

The two overrides the test files honour, if Redis is somewhere else:
`REDIS_TEST_URL` (used by `test/streams.test.js` and
`test/idempotency.test.js`) and `REDIS_TEST_HOST`/`REDIS_TEST_PORT` (used by
the integration tests). The check dials whichever of those you have set.
