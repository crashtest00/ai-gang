# hello-desktop — Project Map

## Framework / Runtime
React 19 + Vite 7 web frontend; Tauri 2 + Rust native shell.

## Key Directories
- `src/` — shared React UI, runnable in browser and native shell
- `src-tauri/` — Rust entry point, Tauri capabilities, and bundle configuration
- `.github/workflows/` — Jenkins-dispatched native build workflow

## Entry Points
- `index.html` and `src/main.jsx` — web application
- `src-tauri/src/main.rs` — native application

## Conventions
Keep shared UI browser-compatible. Isolate native API calls behind adapters.

## Test Framework
No unit-test framework configured. `npm run build` is the web build gate.

## Available Agents
- frontend (suffix: frontend)
- devops (suffix: devops)
