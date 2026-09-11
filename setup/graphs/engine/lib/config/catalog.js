'use strict';

// The shipped target/stack compatibility catalog: which deployment targets
// (project.type) have a real, buildable boilerplate today, and which stack
// profile identifiers (project.stack) are supported for each.
//
// This mirrors the existing, already-shipped support surface rather than
// inventing new targets or stacks:
//   - "web" is the only deployment target with a checked-in, independently
//     runnable and deployable boilerplate (templates/web/), and the only
//     target scripts/init-repo.sh and
//     setup/graphs/deployment-target-boilerplate.graph.yaml's
//     "select-target-deployment" decision node treat as supported — every
//     other CLI-recognized --deployment value (mobile, desktop, extension,
//     mcp, other) reaches that graph's "unsupported" remediation branch
//     instead. "desktop" has partial, explicitly experimental content
//     (templates/desktop/ workflow files only, no scaffolded app) that the
//     graph itself excludes from its acceptance bar, so it is left out of
//     this catalog too — a recognized CLI target name alone does not
//     establish implementation support.
//   - "node-express" is the one stack profile templates/web/ ships:
//     Node 22 + Express (templates/web/package.json's "express" dependency,
//     templates/web/server.js, templates/web/Dockerfile).
//
// Adding a new supported target or stack means adding real, buildable
// content first (a templates/<target>/ boilerplate, a corresponding branch
// in deployment-target-boilerplate.graph.yaml) and then adding its
// identifier here — not the other way around.

const TARGET_STACK_CATALOG = Object.freeze({
  web: Object.freeze({
    description:
      'Node.js + Express web app scaffolded from templates/web/ (scripts/init-repo.sh --deployment web).',
    stacks: Object.freeze({
      'node-express': Object.freeze({
        description: 'Node 22 + Express (templates/web/package.json dependency: express).',
      }),
    }),
  }),
});

function listSupportedTargets() {
  return Object.keys(TARGET_STACK_CATALOG);
}

function listSupportedStacks(target) {
  const entry = TARGET_STACK_CATALOG[target];
  return entry ? Object.keys(entry.stacks) : [];
}

function isSupportedTarget(target) {
  return Object.prototype.hasOwnProperty.call(TARGET_STACK_CATALOG, target);
}

function isSupportedStack(target, stack) {
  return listSupportedStacks(target).includes(stack);
}

module.exports = {
  TARGET_STACK_CATALOG,
  listSupportedTargets,
  listSupportedStacks,
  isSupportedTarget,
  isSupportedStack,
};
