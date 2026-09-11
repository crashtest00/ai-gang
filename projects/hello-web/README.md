# Hello Web

The checked-in, running reference instance of AI Gang's web
deployment-target boilerplate, paralleling `projects/hello-desktop/`'s role
for the desktop target.

This top-level `Dockerfile`/`docker-compose.yml` is the generic AI Gang
agent dev container (mounts `./src` as `/workspace`, same as every other
project under `projects/`) — it is not the app itself. The actual boilerplate
app — the thing `scripts/init-repo.sh --deployment web` scaffolds into a new
project — lives in `src/` and is a copy of `templates/web/`. See
`src/README.md` for how to run, test, and deploy it directly.

- `docker compose up -d` starts the agent dev container (requires the
  platform's `ai-gang` network and `.env`, same as any other project — see
  `docs/ClaudeInstructions.md` Phase 3).
- `cd src && npm install && npm start` runs the boilerplate app itself,
  independent of the agent container.
