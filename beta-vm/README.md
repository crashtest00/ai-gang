# Beta VM

Config-as-code for the Beta VM described in `docs/release-strategy.md` and
the release-workflow design. This is a separate machine from the
Development VM (which runs `ai-gang`, ScrumMaster, Redis, Jenkins, and agent
containers) — separate secrets, separate data, and it's the only environment
that runs release-candidate preview containers.

Provisioning an actual Beta VM (the droplet/instance itself, DNS, firewall)
is a Cloud Engineer / operator bootstrap concern, not something this repo's
scripts do — see release-workflow.md's Dependencies section. What lives
here is what runs *on* that VM once it exists.

**Container topology is operator-defined.** Dev, beta, and prod can run as
containers on this one VM, be split across several, or each get its own
machine — the choice is the operator's, based on their own infrastructure
and risk tolerance. Co-locating environments means they share secrets and
network exposure; splitting beta onto a dedicated VM like this one keeps
that boundary. Provision a dedicated Beta VM when that isolation matters —
e.g. real testers, real user data, or any environment shared with other
customers' infrastructure.

## Layout

- `traefik/` — reverse proxy that routes both `rc-<sha>.<PREVIEW_DOMAIN>`
  preview requests and `<project>.<BETA_DOMAIN>` long-lived-app requests to
  the right container, picking up routing rules from Docker labels the
  moment a container starts (no config edits, no restarts per release or per
  deploy). The Cloudflare Tunnel runs on the **Dev VM**, not here — its
  ingress for the preview and beta-app wildcards points at Traefik's port on
  this VM over the network (`BETA_VM_HOST`), not over loopback — see
  `scripts/setup-cloudflare-tunnel.sh`. That means this VM's firewall, not
  Traefik's bind address, is what keeps that port from being open to the
  internet (step 6 below).
- `deploy/` — the restricted SSH deploy path Jenkins uses to reach this VM.
  Jenkins never gets a shell or direct Docker API access here; every command
  it can run is named explicitly in `forced-command.sh`.

## One-time setup on the Beta VM

Steps 1, 2, 4, 5, and 6 below are automated by
[`scripts/setup-beta-vm.sh`](../scripts/setup-beta-vm.sh), run from the Dev
VM — see `docs/ClaudeInstructions.md` Phase 2.5. It only needs SSH access to
this VM as an existing sudo-capable user; it never requires logging into
this VM by hand. Steps 3 and 7 remain manual/per-project. The steps
themselves, for reference or manual fallback:

1. Create an unprivileged `beta-deploy` system account with rootless Docker
   configured for it (`dockerd-rootless-setuptool.sh install` as that user).
2. Copy `deploy/` to e.g. `/opt/beta-deploy/` and make its scripts owned by
   and executable by `beta-deploy` only. Alongside them, create
   `/opt/beta-deploy/env` (owned by `beta-deploy`, mode `600`) with:
   ```
   PREVIEW_DOMAIN=preview.yourdomain.com
   BETA_DOMAIN=beta.yourdomain.com
   ```
   (bare domains, no leading `*.` — matches what `setup-cloudflare-tunnel.sh`
   writes to `~/ai-gang/.env` on the Dev VM). `forced-command.sh` sources
   this before dispatching to `deploy.sh`/`preview-deploy.sh`, since an SSH
   forced-command session has no login shell to pick it up from otherwise.
3. Add a read-only, repo-scoped GitHub deploy key for `beta-deploy` (checkout
   only — this account never pushes). **Not automated** — this is per-project
   (done during each repo's Phase 3 setup), not part of the one-time VM
   bootstrap.
4. On the Jenkins side, generate a dedicated SSH keypair (not the general
   `github-token` credential) and add its public half to
   `~beta-deploy/.ssh/authorized_keys` using the `command=` forced-command
   pattern in `deploy/authorized_keys.example` — this is what stops the key
   from ever being usable as an unrestricted shell.
5. `cd traefik && docker compose up -d` (as `beta-deploy`, under its rootless
   Docker context). Requires the `beta` Docker network to already exist
   (`docker network create beta`) — Traefik's compose file declares it
   `external: true`, so `compose up` never creates it itself.
6. Firewall Traefik's published port (`8181` by default) to accept
   connections only from the Dev VM — e.g. with `ufw`:
   ```
   ufw allow from <Dev VM's LAN/VPC IP> to any port 8181 proto tcp
   ```
   Put the Dev and Beta VMs on a shared LAN or cloud VPC first if they
   aren't already (same reachability this VM's SSH deploy path from Jenkins
   already assumes). Without this rule, Traefik — and therefore every
   preview and every project's Beta app — is open to the whole internet the
   moment the port is published.
7. Supply Beta secrets to containers at deploy time (env vars passed to
   `docker run`/`docker compose` by `deploy.sh`), never baked into the image
   at build time. **Not automated** — a runtime concern, not part of VM
   bootstrap.

## Why forced-command SSH instead of a Docker API port

Exposing the Docker API (even over TLS) to Jenkins would let a compromised
Jenkins credential run arbitrary containers on this VM. A forced SSH command
scoped to five named operations (`deploy`, `preview-deploy`,
`preview-teardown`, `preview-teardown-by-issue`) with regex-validated
arguments is a much smaller blast radius, and it's what
release-workflow.md's Beta VM remote-deploy mechanism dependency specifies.
