'use strict';

// Detection logic for the base-image-family branch-point class:
// does a Dockerfile's base image use
// Alpine/BusyBox (adduser/deluser) or Debian/Ubuntu (useradd) user-management
// syntax? Pure text parsing — no shell-out, no external call — so this
// check can never itself fail to execute, but the decision node still
// declares a "probe-error" outcome for contract uniformity and
// as a safety net if `dockerfileContent` isn't a string.
//
// Returns one of: "alpine-busybox" | "debian-ubuntu" | "unrecognized" | "probe-error"

const ALPINE_FAMILY = /^(alpine|.*-alpine\b|.*:alpine\b)/i;
const BUSYBOX_FAMILY = /^busybox\b/i;

// Debian/Ubuntu-derived tags this repo's templates and common bases use:
// "bookworm", "bullseye", "buster" (Debian codenames), "slim" (Debian-slim
// variant), "jammy"/"focal"/"noble" (Ubuntu codenames), or a bare
// "debian"/"ubuntu" repository name.
const DEBIAN_UBUNTU_FAMILY = /(debian|ubuntu|bookworm|bullseye|buster|jammy|focal|noble|-slim\b|:slim\b)/i;

function detectBaseImageFamily(dockerfileContent) {
  try {
    if (typeof dockerfileContent !== 'string') return 'probe-error';
    const fromLine = dockerfileContent
      .split('\n')
      .map((l) => l.trim())
      .find((l) => /^FROM\s+/i.test(l));
    if (!fromLine) return 'unrecognized';

    // `FROM <image>[:tag] [AS name]` — take the image[:tag] token.
    const image = fromLine.replace(/^FROM\s+/i, '').split(/\s+/)[0];
    if (!image) return 'unrecognized';

    if (ALPINE_FAMILY.test(image) || BUSYBOX_FAMILY.test(image)) return 'alpine-busybox';
    if (DEBIAN_UBUNTU_FAMILY.test(image)) return 'debian-ubuntu';
    return 'unrecognized';
  } catch {
    return 'probe-error';
  }
}

module.exports = { detectBaseImageFamily };
