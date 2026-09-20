'use strict';

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { detectBaseImageFamily } = require('../lib/checks/base-image-family');

const DOCKER_TEMPLATES_DIR = path.join(__dirname, '..', '..', '..', '..', 'Docker Templates');

function readTemplate(name) {
  return fs.readFileSync(path.join(DOCKER_TEMPLATES_DIR, name), 'utf8');
}

test('detectBaseImageFamily: "alpine-busybox" for node:22-alpine (Dockerfile-node.template)', () => {
  const content = readTemplate('Dockerfile-node.template');
  assert.equal(detectBaseImageFamily(content), 'alpine-busybox');
});

test('detectBaseImageFamily: "debian-ubuntu" for python:3.11-slim (Dockerfile-python.template)', () => {
  const content = readTemplate('Dockerfile-python.template');
  assert.equal(detectBaseImageFamily(content), 'debian-ubuntu');
});

test('detectBaseImageFamily: "debian-ubuntu" for the checked-in Dockerfile-tauri.template', () => {
  const content = readTemplate('Dockerfile-tauri.template');
  assert.equal(detectBaseImageFamily(content), 'debian-ubuntu');
});

test('detectBaseImageFamily: "unrecognized" for an unknown/unusual base image', () => {
  assert.equal(detectBaseImageFamily('FROM some-vendor/proprietary-image:9.9\nRUN echo hi\n'), 'unrecognized');
});

test('detectBaseImageFamily: "unrecognized" when there is no FROM line at all', () => {
  assert.equal(detectBaseImageFamily('# just a comment\n'), 'unrecognized');
});

test('detectBaseImageFamily: "probe-error" for non-string input', () => {
  assert.equal(detectBaseImageFamily(undefined), 'probe-error');
  assert.equal(detectBaseImageFamily(null), 'probe-error');
});

test('detectBaseImageFamily: bare "alpine" and "busybox" repository names are recognized', () => {
  assert.equal(detectBaseImageFamily('FROM alpine:3.19\n'), 'alpine-busybox');
  assert.equal(detectBaseImageFamily('FROM busybox:latest\n'), 'alpine-busybox');
});

test('detectBaseImageFamily: bare "debian" and "ubuntu" repository names are recognized', () => {
  assert.equal(detectBaseImageFamily('FROM debian:12\n'), 'debian-ubuntu');
  assert.equal(detectBaseImageFamily('FROM ubuntu:22.04\n'), 'debian-ubuntu');
});
