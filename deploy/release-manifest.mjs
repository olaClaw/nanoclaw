/** Validate an immutable Compose release before bootstrap or host startup. */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';

export const imageKeys = ['host', 'agent', 'brokers', 'onecli', 'postgres', 'signal'];
const forkImageKeys = ['host', 'agent', 'brokers'];
const envKeys = {
  host: 'NANOCLAW_HOST_IMAGE',
  agent: 'NANOCLAW_AGENT_IMAGE',
  brokers: 'NANOCLAW_BROKER_IMAGE',
  onecli: 'ONECLI_IMAGE',
  postgres: 'POSTGRES_IMAGE',
  signal: 'SIGNAL_IMAGE',
};
const sha = /^[0-9a-f]{40}$/;
const pinnedImage = /^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$/;

function exactKeys(record, expected) {
  return (
    record &&
    typeof record === 'object' &&
    !Array.isArray(record) &&
    JSON.stringify(Object.keys(record).sort()) === JSON.stringify([...expected].sort())
  );
}

export function validateReleaseManifest(manifest) {
  if (
    !exactKeys(manifest, ['schema', 'version', 'revision', 'tree', 'images']) ||
    manifest.schema !== 'nanoclaw-compose-release/v1' ||
    typeof manifest.version !== 'string' ||
    !/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(manifest.version) ||
    !sha.test(manifest.revision) ||
    !sha.test(manifest.tree) ||
    !exactKeys(manifest.images, imageKeys)
  ) {
    throw new Error('Release manifest is invalid');
  }
  for (const key of imageKeys) {
    const ref = manifest.images[key];
    if (typeof ref !== 'string' || !pinnedImage.test(ref) || ref.startsWith('example.invalid/')) {
      throw new Error('Release manifest has an unpinned image');
    }
  }
  return manifest;
}

export function readReleaseManifest(filename) {
  const info = fs.lstatSync(filename);
  if (!info.isFile() || info.isSymbolicLink() || info.size > 16 * 1024) {
    throw new Error('Release manifest must be a small regular file');
  }
  return validateReleaseManifest(JSON.parse(fs.readFileSync(filename, 'utf8')));
}

export function inspectImageLabels(ref) {
  const output = execFileSync('docker', ['image', 'inspect', '--format', '{{json .Config.Labels}}', ref], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'ignore'],
    timeout: 15_000,
    maxBuffer: 128 * 1024,
  });
  return JSON.parse(output);
}

export function verifyRuntimeRelease({
  env = process.env,
  manifestPath = env.NANOCLAW_RELEASE_MANIFEST,
  projectRoot = process.cwd(),
  inspect = inspectImageLabels,
} = {}) {
  if (!manifestPath) throw new Error('Release manifest path is missing');
  const manifest = readReleaseManifest(manifestPath);
  const pkg = JSON.parse(fs.readFileSync(path.join(projectRoot, 'package.json'), 'utf8'));
  if (
    manifest.version !== pkg.version ||
    manifest.revision !== env.NANOCLAW_SOURCE_REVISION ||
    manifest.tree !== env.NANOCLAW_SOURCE_TREE ||
    env.NANOCLAW_AGENT_ASSETS_IN_IMAGE !== 'true'
  ) {
    throw new Error('Release identity does not match the running host image');
  }
  for (const key of imageKeys) {
    if (env[envKeys[key]] !== manifest.images[key]) throw new Error('Compose image differs from release manifest');
    const labels = inspect(manifest.images[key]);
    if (
      forkImageKeys.includes(key) &&
      (!labels ||
        typeof labels !== 'object' ||
        labels['org.opencontainers.image.revision'] !== manifest.revision ||
        labels['org.olaclaw.source.tree'] !== manifest.tree)
    ) {
      throw new Error('Release image source identity does not match');
    }
  }
  return manifest;
}
