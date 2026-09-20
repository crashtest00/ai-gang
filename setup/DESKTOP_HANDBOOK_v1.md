# Desktop App Handbook (v1)

This handbook is the source of truth for Electron and Tauri projects in AI Gang.
It supplements `DEVOPS_HANDBOOK_v1.md`; the Jira/Jenkins release workflow remains
authoritative for release state and promotion.

## Build strategy

Desktop projects keep their UI and shared logic runnable as an ordinary web app.
`npm run dev` is the fast local loop and the built web frontend is deployed to beta
for normal acceptance. Native builds validate OS integration and packaging. They
run when a change affects native behavior, a Release ticket explicitly requests
them, or the production gate requires them.

Native builds run on GitHub-hosted `windows-latest`, `macos-latest`, and
`ubuntu-22.04` runners. Each runner checks out the exact SHA supplied by Jenkins,
installs locked dependencies, builds an unsigned package, and uploads its artifact.
Unsigned v1 outputs are `.exe` (Electron or Tauri NSIS), `.dmg`, and `.AppImage`.
Code signing, notarization, and store publishing are out of scope.

## Release-flow contract

`.github/workflows/build-desktop.yml` has one trigger: `workflow_dispatch`, with a
required `sha` input. It must never add `pull_request`, `push`, or tag triggers.
Jenkins dispatches it as a stage of the same Release ticket used for the web
preview, during the release-candidate job's "Trigger native validation build"
stage — via `jenkins/scripts/trigger-native-build.sh`, which detects a desktop
project by the presence of this workflow file itself, so no separate per-project
config is needed. Agents and humans do not run the workflow or create release
tags. The Jenkins integration and artifact-link Jira comment are defined by
the release-flow tooling shared with the rest of the release pipeline and should be
changed with that flow, not inside an application repository.

The workflow verifies that checkout resolved to the requested SHA before building.
A failure on any matrix OS fails the run; successful legs remain downloadable for
diagnosis but do not constitute a valid cross-platform build. This validation is
supplementary to the web beta preview — its failure is reported on the Release
ticket but never blocks cutting the candidate.

`.github/workflows/release-desktop.yml` is the production counterpart: its only
trigger is `push: tags: ['v*']`. Jenkins is the only thing that ever pushes that
tag, from the production-promote job's "Tag and publish desktop release" stage
(`jenkins/scripts/tag-desktop-release.sh`), when a Release ticket for a desktop
project moves to Done — never a human or agent running `git tag` directly. Unlike
the native-build stage, a failure to tag and push fails the production-promote
job itself, since the tag is the actual desktop release artifact, not a
supplementary check. Once pushed, the workflow matrix-builds the same three
platforms and publishes them as a GitHub Release attached to that tag —
this is the "stable release" GitHub Releases distribution mentioned in the V1
scope; it produces no code signing, notarization, or store submission.

## Tauri template

Required package scripts:

```json
{
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "tauri": "tauri"
  },
  "devDependencies": {
    "@tauri-apps/cli": "^2.0.0"
  }
}
```

`src-tauri/tauri.conf.json` should set `build.beforeDevCommand` to `npm run dev`,
`build.beforeBuildCommand` to `npm run build`, and bundle targets to
`["nsis", "dmg", "appimage"]`. The Rust crate uses `tauri-build` and `tauri` v2.
Linux runners need WebKitGTK, AppIndicator, librsvg, patchelf, and build tools; the
shared workflow installs them.

Tauri updater configuration uses the v2 updater plugin and a signed update
manifest. Because v1 artifacts are unsigned and signing infrastructure is a
non-goal, ship the updater disabled by default. Projects may configure endpoints
and a public key only after signing is available; an unreachable feed is reported
as an update-check failure and must not prevent the installed app from starting.

## Electron template

Expose a deterministic packaging script and keep platform targets in
`electron-builder.yml`:

```json
{
  "scripts": {
    "build": "vite build",
    "build:desktop": "npm run build && electron-builder"
  }
}
```

```yaml
appId: com.example.app
files: [dist/**, electron/**]
win: { target: nsis }
mac: { target: dmg }
linux: { target: AppImage }
publish: null
```

Use `electron-updater` only with an explicitly configured HTTPS provider. Disable
automatic download until the feed and signing policy exist. Network errors should
be surfaced as non-fatal update status, never as an application startup failure.
The workflow calls `npm run build:desktop` when `src-tauri/` is absent.

## Troubleshooting

- One OS fails: inspect that matrix leg for native dependency or packaging-target
  differences; do not rerun only the successful platforms as a release substitute.
- Linux WebKit or linker errors: confirm the workflow's apt packages are intact.
- No artifacts: confirm bundle targets and output paths have not been customized;
  update artifact globs alongside any intentional output change.
- Update server unreachable: verify the endpoint separately, keep startup working,
  and report the Release ticket blocked if update validation is a stated gate.
- SHA mismatch: stop. Jenkins must dispatch a full commit SHA that exists in the
  repository; never silently build the default branch instead.
- Native build dispatched but never found: `trigger-native-build.sh` polls for
  up to a minute for the run to appear before giving up and reporting
  `STATUS=unknown` on the Release ticket — this doesn't fail the release
  candidate; check the repo's Actions tab directly if it recurs.
- Tag push fails on production approval: `tag-desktop-release.sh` fails the
  `production-promote` job (reported via the same failure Jira comment as any
  other promotion failure) rather than silently skipping the release — check
  that `GITHUB_TOKEN` still has Contents: Read and write and that the computed
  `vX.Y.Z` doesn't already exist on the remote.
