# `docs/ClaudeInstructions.md` Migration Status

Tracks, per phase, whether it remains as-is under `docs/ClaudeInstructions.md`
(the initialization agent follows it directly, unchanged) or has
been converted to a named graph. This record lists every phase, with
exactly one of the two states ("as-is"/"converted: `<graph_id>`") — the
Cloudflare phase is the first to show "converted."

Conversion proceeds one **conditional** phase at a time and does
not require converting non-conditional phases. "As-is" is a valid interim
state for a phase not yet reached in the conversion order — it is not a
permanent exemption for a conditional phase. A phase marked "not
conditional" is not required to convert at all, though it MAY be later if a
future finding gives it real branch points.

| Phase | Conditional? | Status |
| --- | --- | --- |
| Phase 0: Project Type Discovery | No — a fixed question set, not a branch on environment state | as-is |
| **Phase 1.0/1.1: Jira Instance Setup** | **Yes**, as of `escalation` — see "Reclassified this pass" below | **converted: `jira-instance-setup`** (`setup/graphs/jira-instance-setup.graph.yaml`) |
| Phase 2, Prerequisites/Docker | No — linear steps; see "Reclassified this pass" below for why the VM/container-topology decision doesn't land here either | as-is |
| **Phase 2.0: Cloudflare Tunnel** | **Yes** — the four branch points this feature's pilot covers | **converted: `cloudflare-setup`** (`setup/graphs/cloudflare-setup.graph.yaml`) |
| Phase 2.1: Redis | No — linear steps | as-is |
| Phase 2.2: ScrumMaster | No — linear steps | as-is |
| Phase 2.3: Jenkins | Yes — plugin/credential-type conditions exist in prose, not yet graphed | as-is |
| Phase 2.4: Security | No — linear steps (branch-protection settings are declarative, not conditional on detected state) | as-is |
| **Phase 2.5: Beta VM Bootstrap** | **Yes**, as of `escalation` — Beta's own-VM-vs.-container-on-Dev fork; see "Reclassified this pass" below | as-is, not yet converted |
| Phase 3.0: Create Project Repo | No — linear steps | as-is |
| Phase 3.1: Project Initialisation (deployment-target boilerplate selection) | Yes — target-deployment selection is a real branch point | converted: `deployment-target-boilerplate` (`setup/graphs/deployment-target-boilerplate.graph.yaml`) — covers only the boilerplate-selection branch point within this phase; the rest of 3.1 (Jira project creation, branch protection) remains as-is |
| Phase 3.2: Container Setup (base-image family) | Yes — which Dockerfile template/base image family determines user-management syntax | converted: `base-image-family` (`setup/graphs/base-image-family.graph.yaml`) — covers only the base-image-family branch point; the rest of 3.2 (copying and customizing the Dockerfile, `docker compose build/up`) remains as-is |
| Phase 3.3: Project Map (CLAUDE.md) | No — a fill-in-the-blank template | as-is |
| Phase 3.4: Verify Claude Code and Git Access | No — linear steps | as-is |
| Phase 3.5: Start the Redis Subscriber | No — linear steps (now also automated at container start via each Dockerfile template's ENTRYPOINT) | as-is |
| Phase 3.6: Jenkins Pipeline | Yes — per-deployment-target pipeline step selection (web static vs. SSR/container, mobile, MCP) exists in prose, not yet graphed | as-is |
| Phase 3.7: Release Promotion | No — the Release-ticket flow is Jenkins/ScrumMaster-automated, not a human-followed branch | as-is |
| Phase 4: End-to-End Test | No — a fixed validation checklist | as-is |
| Continuing Support (all subsections) | No — reference material, not a setup procedure | as-is |

## Reclassified this pass — the `escalation` node kind

Before `escalation` existed, this record's only branching mechanism was
`decision`, which strictly tests real, observable environment
state — never a human's declared preference. Phase 1.0/1.1 and "Phase 2,
Prerequisites/Docker" were marked not-conditional on that basis, in the same
category as Phase 0 ("a fixed question set, not a branch on environment
state"). `escalation` closes that gap for a human-resolved fork,
so both rows were reassessed against the actual phase text in
`docs/ClaudeInstructions.md`, rather than mechanically flipped.

**Phase 1.0/1.1 (Jira Instance Setup) — reclassified Yes, converted.**
The product owner named the Jira-vs-local per-project mode switch as a
candidate. Read literally, that decision does not belong here
at all: selecting Jira mode during a project's own initialization is
explicitly barred — a project must complete initialization in local mode
and connect Jira, if at all, as a separate, later operation — so no
per-project mode switch is ever reached while walking Phase 0-4.
`docs/ClaudeInstructions.md` confirms this independently: nowhere in the
document does "Jira mode" or "local mode" appear, and Phase 0's question set
doesn't ask about Jira at all. Forcing that project-level switch into
Phase 1 as an escalation node would misrepresent both documents.

What Phase 1 actually contains, on its own terms, is different and *is* a
real, currently-unrepresented fork: its own header scopes it explicitly —
"these steps are scoped to the Jira instance, not the project" — and the
guide's "Phases 1 and 2 have skip conditions; always check them" implies a
skip decision beyond 1.0/1.1's already-idempotent internal steps ("skip any
step already done for this Jira instance," the "linear steps... but that's
not the same as branching on detected state" already noted here). The
document never asks whether this AI Gang deployment wants Jira integration
at all before doing that setup — it's simply assumed. Given the product
owner's own framing that "Jira isn't required," whether to do Phase 1's
instance-level setup at all (create the service account, create the custom
fields) is exactly a human's declared preference with no observable
environment state to probe — unrepresentable before the `escalation` node
kind existed, cleanly representable now.

This is deliberately **not the same decision** as the per-project
mode switch, and the new graph's header comment says so to avoid the two
being conflated later: this one is instance-scoped (done once per Jira
instance, gating whether Phase 1 runs at all), the per-project one is
project-scoped (always local at init; Jira connects later, separately, per
project).
Converted to `setup/graphs/jira-instance-setup.graph.yaml`: an
`escalation` entry node asking that instance-level yes/no question, a
`skipped` terminal on "no," and the existing linear 1.0/1.1 steps unchanged
on "yes" (their own "linear once you're doing them at all" character,
noted in the original classification, still holds — only the fork ahead of
them was previously unrepresentable).

**"Phase 2, Prerequisites/Docker" — reclassified: still No, not converted.**
The product owner's other candidate, VM/container topology, does not live
in this row. Re-reading the actual text: "Prerequisites" here is Cloudflare
credential-gathering, and "Docker" is a single linear
`./scripts/install-docker.sh` invocation — neither contains a topology
choice. That decision lives at Phase 2.5 instead (below); Phase 0's "Derive
the container topology" is a same-named but unrelated concept — the
per-repo agent-container split (`AGENT_CHANNEL_SUFFIX`), not which VM an
environment runs on — and stays correctly classified No on its own terms.
"Phase 2, Prerequisites/Docker" itself remains linear/non-conditional.

**Phase 2.5 (Beta VM Bootstrap) — reclassified Yes, not converted this
pass.** An earlier draft of this reclassification called the product
owner's VM/container-topology candidate too open-ended to enumerate as
`escalation` branches — "any combination of VMs + containers" reads as
unbounded. That was wrong: pressed on it, the actual decision decomposes
into a small, bounded structure. The Dev VM has no choice — it's always the
one persistent VM the platform runs on. Beta is a genuine two-way fork
(its own VM, or a container on the Dev VM). Prod, once its deploy path
exists at all, is a three-way fork gated on Beta's answer (its own VM, a
container on the Dev VM, or — only reachable if Beta chose its own VM — a
container on the Beta VM). That's cleanly `escalation`-shaped, not open
combinatorics.

`docs/ClaudeInstructions.md`'s Phase 2.5 has been amended to state Beta's
fork explicitly (previously it hardcoded a dedicated Beta VM
unconditionally, so there was nothing here to reclassify until that text
changed) and to describe Prod's three-way, Beta-gated fork as the shape
that section will take once a Prod deploy path is built. Phase 2.5 is
reclassified Yes on that basis. It is **not converted to a graph this
pass** — this pass's one-conditional-phase-at-a-time budget went to Phase 1
above, and only Beta's half of the topology decision has real content to
route to today (its own-VM path is the existing `setup-beta-vm.sh`
procedure; the container-on-Dev path has no script yet, an honestly
disclosed gap, not a hidden one). Prod's third of the fork has no phase to
attach to at all until its deploy path is specified. Converting Phase 2.5
is a follow-on candidate for whichever pass picks up the next
conditional phase.

## Cross-cutting branch-point classes (not tied to one numbered phase)

These generalize beyond a single phase:

| Branch-point class | Applies wherever | Status |
| --- | --- | --- |
| Base-image family (Alpine/BusyBox vs. Debian/Ubuntu user-management syntax) | Any Dockerfile customization step (currently Phase 3.2; also relevant to any future Docker Templates/ addition) | converted: `base-image-family` |
| Git branch-tracking / stale-ref state | Any step that assumes a local checkout's tracking ref still matches the remote default branch (Phase 0's platform-repo clone, Phase 3.0/3.1's new project repo, and the Frontend/Backend Agent's own `git checkout main && git pull` step per `setup/frontend-agent.md:49`/`setup/backend-agent.md:49`) | converted: `branch-tracking-stale-ref` (`setup/graphs/branch-tracking-stale-ref.graph.yaml`) — the graph exists and is walkable; it is not yet wired into every one of those call sites as a mandatory pre-step, which would be a separate follow-on change to those scripts/docs, not a graph-schema change |

## Not yet converted, and why

- **Phase 2.3 (Jenkins) and Phase 3.6 (Jenkins Pipeline / deployment-target
  pipeline steps)** remain as-is. They contain real conditional prose
  (credential-type gotchas, per-deployment-target build steps) that would
  benefit from graph form, but `jenkins/` is owned by a sibling V2
  workstream (Jenkins workspace/cache retention) per this feature's
  collision-avoidance boundary — converting Jenkins-owned phases is left for
  that workstream or a later pass, not attempted here.
