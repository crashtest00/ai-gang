# Hello Desktop

Tauri 2 + React/Vite example for the AI Gang desktop lane.

- `npm run dev` runs the browser frontend used for fast iteration and web beta.
- `npm run tauri dev` runs the native shell on a machine with Tauri prerequisites.
- `npm run tauri build` creates unsigned native installers.
- `.github/workflows/build-desktop.yml` only runs when Jenkins dispatches an exact SHA.
- `.github/workflows/release-desktop.yml` only runs when Jenkins pushes a `vX.Y.Z` tag on production approval; it publishes the GitHub Release.

Install dependencies with `npm ci`. See `setup/DESKTOP_HANDBOOK_v1.md` for OS
prerequisites and release-flow boundaries.
