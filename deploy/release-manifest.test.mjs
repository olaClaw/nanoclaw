import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { bootstrapComposeInstall } from './bootstrap.mjs';
import { imageKeys, readReleaseManifest, validateReleaseManifest, verifyRuntimeRelease } from './release-manifest.mjs';

const revision = 'a'.repeat(40);
const tree = 'b'.repeat(40);
const digest = 'c'.repeat(64);
const images = Object.fromEntries(imageKeys.map((key) => [key, `registry.example/${key}@sha256:${digest}`]));
const manifest = { schema: 'nanoclaw-compose-release/v1', version: '2.4.0', revision, tree, images };

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-release-test-'));
  fs.writeFileSync(path.join(root, 'package.json'), JSON.stringify({ version: manifest.version }));
  const file = path.join(root, 'release.json');
  fs.writeFileSync(file, JSON.stringify(manifest));
  const env = {
    NANOCLAW_RELEASE_MANIFEST: file,
    NANOCLAW_SOURCE_REVISION: revision,
    NANOCLAW_SOURCE_TREE: tree,
    NANOCLAW_AGENT_ASSETS_IN_IMAGE: 'true',
    NANOCLAW_HOST_IMAGE: images.host,
    NANOCLAW_AGENT_IMAGE: images.agent,
    NANOCLAW_BROKER_IMAGE: images.brokers,
    ONECLI_IMAGE: images.onecli,
    POSTGRES_IMAGE: images.postgres,
    SIGNAL_IMAGE: images.signal,
  };
  const inspect = (ref) =>
    ref === images.host || ref === images.agent || ref === images.brokers
      ? { 'org.opencontainers.image.revision': revision, 'org.olaclaw.source.tree': tree }
      : null;
  return { root, file, env, inspect };
}

test('release requires exact schema and digest-pinned images', () => {
  assert.equal(validateReleaseManifest(manifest), manifest);
  assert.throws(() => validateReleaseManifest({ ...manifest, password: 'fixture' }));
  assert.throws(() =>
    validateReleaseManifest({ ...manifest, images: { ...images, host: 'registry.example/host:latest' } }),
  );
  assert.throws(() =>
    validateReleaseManifest({ ...manifest, images: { ...images, host: `example.invalid/host@sha256:${digest}` } }),
  );
});

test('runtime release rejects changed refs, tree and fork image labels', () => {
  const state = fixture();
  try {
    assert.deepEqual(
      verifyRuntimeRelease({ env: state.env, projectRoot: state.root, inspect: state.inspect }),
      manifest,
    );
    assert.throws(() =>
      verifyRuntimeRelease({
        env: { ...state.env, NANOCLAW_AGENT_IMAGE: images.host },
        projectRoot: state.root,
        inspect: state.inspect,
      }),
    );
    assert.throws(() =>
      verifyRuntimeRelease({
        env: { ...state.env, NANOCLAW_SOURCE_TREE: 'd'.repeat(40) },
        projectRoot: state.root,
        inspect: state.inspect,
      }),
    );
    assert.throws(() =>
      verifyRuntimeRelease({
        env: state.env,
        projectRoot: state.root,
        inspect: (ref) =>
          ref === images.agent ? { 'org.opencontainers.image.revision': revision } : state.inspect(ref),
      }),
    );
  } finally {
    fs.rmSync(state.root, { recursive: true, force: true });
  }
});

test('bootstrap writes a marker only for an empty directory after release verification', () => {
  const state = fixture();
  const dataDir = path.join(state.root, 'data');
  fs.mkdirSync(dataDir);
  try {
    const verify = () => verifyRuntimeRelease({ env: state.env, projectRoot: state.root, inspect: state.inspect });
    const marker = bootstrapComposeInstall({ dataDir, verify });
    assert.equal(marker.commit, revision);
    assert.equal(marker.tree, tree);
    assert.equal(marker.via, 'compose-bootstrap');
    assert.equal(JSON.parse(fs.readFileSync(path.join(dataDir, 'upgrade-state.json'), 'utf8')).commit, revision);
    assert.throws(() => bootstrapComposeInstall({ dataDir, verify }));
  } finally {
    fs.rmSync(state.root, { recursive: true, force: true });
  }
});

test('bootstrap leaves fresh data untouched when a release check fails', () => {
  const state = fixture();
  const dataDir = path.join(state.root, 'data');
  fs.mkdirSync(dataDir);
  try {
    assert.throws(() =>
      bootstrapComposeInstall({
        dataDir,
        verify: () => {
          throw new Error('image mismatch');
        },
      }),
    );
    assert.deepEqual(fs.readdirSync(dataDir), []);
    fs.writeFileSync(path.join(dataDir, 'v2.db'), 'fixture');
    assert.throws(() => bootstrapComposeInstall({ dataDir, verify: () => manifest }));
    assert.deepEqual(fs.readdirSync(dataDir), ['v2.db']);
  } finally {
    fs.rmSync(state.root, { recursive: true, force: true });
  }
});
