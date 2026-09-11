# hello-web — Project Map

## Framework / Runtime
Node 22 + Express, serving a static frontend.

## Key Directories
- `public/` — static frontend (HTML/CSS)
- `test/` — `node --test` suite
- `server.js` — the app entry point (also exports `createApp`/`start` for tests)

## Entry Points
- `server.js` — `npm start` / `node server.js`

## Conventions
Deliberately minimal — this is the deployment-target boilerplate, not a
real project. Replace `public/`, `server.js`, and this file once scaffolded
into an actual project.

## Test Framework
`node --test` (`npm test`).

## Available Agents
- frontend (suffix: frontend)
