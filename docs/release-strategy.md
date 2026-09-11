# Release Strategy

**Purpose**: Define how AI Gang moves software from agent development through human testing to production.

---

## Principles

- Environments are trust and distribution boundaries, not necessarily VMs.
- Git branches and runtime environments are separate concepts (see below).
- Development is private; beta is available to testers; production is human-gated.
- CI identifies every release candidate by commit SHA and records what was deployed.
- Server artifacts should be built once and promoted rather than rebuilt separately in each environment.
- A web beta is the default acceptance surface for every product lane — mobile, web, browser extension, desktop, and low/no-frontend — with native builds as supplementary validation, not a separate track per lane.
- Native builds supplement web testing; they are required where behavior depends on the operating system, packaging, signing, updates, or native APIs.

---

## Branches vs. Environments

`dev`, `beta`, and `prod` are git branches — where code lives. The Development VM, Beta environment, and Production VM/runtime are where that code *runs*. Normally these are separate machines with separate secrets, data, and exposure; Beta may instead run as a container on the Development VM in environments where a dedicated Beta VM hasn't been provisioned yet (see Beta, below, for what that trades away). The two are related but not interchangeable: a branch can update without anything redeploying, and an environment's running build is whatever was last deployed to it, not necessarily that branch's current tip.

- Agents work on feature branches from the development VM. A feature branch does not require a permanent checkout or a dedicated VM.
- A successful PR is merged into `dev`.
- Jenkins automatically promotes that merge to `beta` and deploys the result to the Beta VM — no human step, no Jira transition required.

---

## Environments

### Development

Each project has a private development VM. It hosts AI Gang, the source workspaces, agent containers, and development services. Agents implement Jira tickets on feature branches, run tests, and open pull requests from this VM.

The application itself is not publicly exposed from development. Only narrowly scoped integration endpoints, such as the authenticated Jira webhook, may be reachable through a tunnel. Redis, CI administration, development servers, workspaces, and SSH remain private or access-controlled.

### Beta

Beta is the automatic destination for code that has passed the development test and merge gates. It is exposed to authorized testers and uses separate secrets, data, domains, and external-service sandbox accounts. Beta continuously receives every successful change — there is no batching or approval step before something lands on beta.

**Container fallback on the Development VM**: where a dedicated Beta VM has not been provisioned yet, beta may instead run as a plain Docker container on the Development VM. This is an interim/test configuration, not the default — it forfeits the separate-machine isolation described above (secrets and network exposure are shared with Development instead of kept apart), so it should be replaced with a real Beta VM (see `beta-vm/README.md`) before real testers or real user data are involved. To keep that later move a non-event, the container should still listen on the same internal port (`8080`) and follow the same image-per-SHA convention `beta-vm/deploy/deploy.sh` uses, so nothing about the project's build changes when it's promoted to a real Beta VM — only where the container runs.

The form of beta depends on the product:

| Product | Primary beta surface | Additional validation |
|---|---|---|
| Web application | Web deployment on a beta VM, container, or hosting platform | Browser and integration tests |
| Server/API | Beta VM or managed runtime | API and consumer integration tests |
| Desktop (Tauri/Electron) | Web beta of the shared UI and application behavior | Periodic native builds for desktop-only behavior |
| Mobile app | Web beta when practical, plus an internal mobile build | EAS internal distribution, TestFlight, or Play internal testing |
| Browser extension | Web beta of shared UI/logic where applicable, plus a dev-mode extension build | Browser-specific manual load testing (permissions, content scripts) |
| Low/no-frontend | Beta deployment of the service to its declared beta destination (worker, cron job, CLI, headless service) | Consumer/integration tests; no UI surface required |

For Tauri and Electron, the web beta provides rapid review of layout, navigation, state, business rules, and server integration without requiring testers to install every iteration. The application should keep shared product logic behind interfaces so the web implementation can substitute for native capabilities where appropriate.

A web beta does not certify native-only behavior. Filesystem access, window management, system tray behavior, deep links, notifications, permissions, auto-update, installers, signing, and OS-specific integration must be tested in native builds before production. This validation is a human responsibility: the workflow does not include an automated native-validation gate. Moving a Release ticket to Done (see below) is the human's approval that any required native validation has been done.

Moving an individual Story to Done means the change is accepted on beta — full stop. It does not trigger production promotion.

### Production

Production changes only after explicit human approval, batched via a Jira Release ticket rather than per story (see Cutting and Promoting a Release). A production VM is required for hosted web applications, APIs, and other server components. It is not required solely because a project produces a desktop or mobile client.

Production promotion is one logical, user-visible operation initiated by the approved Release ticket. Underneath, Jenkins runs whichever target-specific commands the project declares:

- Web/server: reuse and deploy the already-built, SHA-pinned server image.
- Desktop: build, sign, and publish desktop artifacts.
- Mobile: select and submit the appropriate build to the app stores.
- Browser extension: package and publish the extension.
- Low/no-frontend: run the project's declared deployment/publication command.

Store submission and public release may remain separate approvals. Uploading a build for store review must not imply automatic public rollout unless the project explicitly enables that policy.

---

## Release Flow

### Per-story acceptance (continuous)

```text
Jira Story
    ↓
Private development VM — agent works on a feature branch
    ↓
Pull request into `dev` — automated tests
    ↓
Tests pass → Jenkins auto-merges to `dev`, promotes to `beta`,
              and deploys `beta` automatically to the Beta VM
    ↓
Beta VM URL, build identifier, and commit SHA posted to the ticket
    ↓
Tester reviews on the Beta VM → moves Story to Done
    ("accepted on beta" — does not trigger production promotion)
```

### Cutting and promoting a release (batched, deliberate)

```text
Human creates a Jira Release ticket
    ↓
Jenkins verifies `beta`'s queue is clean
    (no story whose commits are on `beta` is still awaiting acceptance)
    ↓ clean
Jenkins pins `beta`'s exact HEAD by creating `release/<sha>`
    ↓
Jenkins immediately opens a frozen `release/<sha>` → `prod` PR
    ↓
Jenkins creates a private preview and posts the preview link,
    SHA, and build identifier back to the Release ticket
    ↓
`beta` may keep advancing — the release candidate does not change
    ↓
Human opens the preview, reviews the exact candidate → moves
    Release ticket to Done
    ↓
Done authorizes Jenkins to merge the frozen PR and perform
    production promotion — no manual Jenkins or GitHub action required
```

---

## Artifact and Branch Behavior

- Hosted previews and production deployments reuse the already-built, SHA-pinned artifact rather than rebuilding it.
- Other targets (desktop, mobile, browser extension, low/no-frontend) operate against that same pinned commit using their own project-specific build/publish commands.
- Git branch movement (e.g., merging `release/<sha>` into `prod`) and artifact deployment are separate but coordinated actions — Jenkins performs both within the same job so what's git history and what's actually running never diverge.
- Once `release/<sha>` is cut, later changes on `beta` must not enter that frozen release; only the pinned commit ships.
- Merge method affects SHA continuity: a fast-forward merge preserves the exact candidate SHA as `prod`'s tip. A merge method that creates a new commit (e.g., squash) preserves identical approved content but produces a new SHA — `prod`'s tip is then not literally equal to the candidate SHA, and the deployed artifact remains identified by the original candidate SHA it was built and previewed from.

---

## Desktop Release Policy

Desktop projects use two complementary validation paths:

1. **Web beta on normal changes** — automatically deploy after CI succeeds so testers can review product behavior quickly.
2. **Native beta build at defined gates** — build when a change touches native code or capabilities, before a production release, or whenever a tester explicitly requests native validation.

Every production desktop release requires native validation on each supported operating system. CI should use platform-appropriate runners for packaging, signing, and notarization. The production build must be traceable to the same accepted commit as the web beta, even when native packaging creates a separate artifact.

Native validation is a human responsibility, not an automated gate: the workflow does not verify it directly. Moving the Release ticket to Done is the human's attestation that required native validation has been completed.

Projects may choose to create native beta builds on every merge. This is a project-level cost and speed policy, not a platform requirement.

---

## Promotion Gates

| Transition | Required gate |
|---|---|
| Feature branch → `dev` | Automated tests pass and the PR merges |
| `dev` → `beta` | Automatic on merge; no additional gate |
| Story → Done | Tester accepts the change on beta; does not trigger promotion |
| `beta` → `release/<sha>` | `beta`'s queue is clean — no story awaiting acceptance |
| `release/<sha>` → `prod` | Release ticket moved to Done (human approval); `prod` remains protected, and Jenkins is permitted to merge only this frozen PR, not arbitrary PRs |
| Store submission → Public release | Human approval unless explicitly configured otherwise |

Production credentials must not be available to development agents. CI receives them only inside the protected production workflow after its approval gate.

---

## Project Configuration

Each project declares:

- Product targets: web, server, desktop, mobile, browser extension, low/no-frontend, or a combination
- Beta and production destinations
- Build, test, packaging, and target-specific deployment/publication commands for each declared lane
- Web compatibility strategy for desktop or mobile UI
- Changes that require a native beta build
- Supported operating systems and mobile platforms
- Branch protection and required checks on `prod`, including that Jenkins may merge only the frozen `release/<sha>` → `prod` PR after the Release ticket reaches Done
- Human approvers
- Store submission and public-release policy
- Rollback procedure and artifact retention period

This configuration lets a full-stack project use a development VM, beta and production backend VMs, a web beta, and native distribution channels without treating every target as another VM.
