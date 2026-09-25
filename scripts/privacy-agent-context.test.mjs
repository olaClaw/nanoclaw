import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterEach, test } from 'node:test';

import { checkAgentContext, cleanupAgentContext, stageAgentContext } from './privacy-agent-context.mjs';

const source = path.resolve('container');
const scratch = [];

function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-privacy-context-'));
  scratch.push(root);
  fs.mkdirSync(path.join(root, 'agent-runner'));
  for (const relative of [
    'Dockerfile',
    '.dockerignore',
    'agent-runner/package.json',
    'agent-runner/bun.lock',
    'cli-tools.json',
    'install-cli-tools.sh',
    'entrypoint.sh',
  ]) {
    fs.copyFileSync(path.join(source, relative), path.join(root, relative));
  }
  return root;
}

afterEach(() => {
  for (const root of scratch.splice(0)) fs.rmSync(root, { recursive: true, force: true });
});

test('reviewed context passes without admitting unrelated local files to COPY', () => {
  const root = fixture();
  fs.writeFileSync(path.join(root, 'untracked-secret.env'), 'not part of the context');
  assert.deepEqual(checkAgentContext(root), []);
});

test('fails closed if ignore rules or COPY sources change', () => {
  const root = fixture();
  fs.appendFileSync(path.join(root, '.dockerignore'), '!untracked-secret.env\n');
  fs.appendFileSync(path.join(root, 'Dockerfile'), '\nCOPY untracked-secret.env /tmp/\n');
  const issues = checkAgentContext(root);
  assert.ok(issues.some((issue) => issue.includes('.dockerignore')));
  assert.ok(issues.some((issue) => issue.includes('COPY source outside allowlist')));
});

test('rejects credential-shaped content without echoing its value', () => {
  const root = fixture();
  const sample = 'aoc_' + 'X'.repeat(32);
  fs.appendFileSync(path.join(root, 'entrypoint.sh'), `\n# ${sample}\n`);
  const issues = checkAgentContext(root);
  assert.ok(issues.some((issue) => issue.includes('credential-shaped value')));
  assert.ok(!issues.join('\n').includes(sample));
});

test('rejects symlinked build inputs', () => {
  const root = fixture();
  fs.unlinkSync(path.join(root, 'entrypoint.sh'));
  fs.symlinkSync(path.join(source, 'entrypoint.sh'), path.join(root, 'entrypoint.sh'));
  assert.ok(checkAgentContext(root).some((issue) => issue.includes('not a regular file')));
});

test('rejects a symlinked parent before reading its files', () => {
  const root = fixture();
  fs.renameSync(path.join(root, 'agent-runner'), path.join(root, 'saved-runner'));
  fs.symlinkSync(path.join(root, 'saved-runner'), path.join(root, 'agent-runner'));
  assert.ok(checkAgentContext(root).some((issue) => issue.includes('agent-runner')));
});

test('stages only approved inputs and removes the generated context', () => {
  const root = fixture();
  fs.writeFileSync(path.join(root, 'untracked-secret.env'), 'must never be copied');
  const staged = stageAgentContext(root);
  try {
    assert.deepEqual(checkAgentContext(staged), []);
    assert.ok(!fs.existsSync(path.join(staged, 'untracked-secret.env')));
    assert.deepEqual(fs.readdirSync(staged).sort(), [
      '.dockerignore',
      '.nanoclaw-generated-context',
      'Dockerfile',
      'agent-runner',
      'cli-tools.json',
      'entrypoint.sh',
      'install-cli-tools.sh',
    ]);
    assert.deepEqual(fs.readdirSync(path.join(staged, 'agent-runner')).sort(), ['bun.lock', 'package.json']);
  } finally {
    cleanupAgentContext(staged);
  }
  assert.ok(!fs.existsSync(staged));
});

test('refuses to remove a directory it did not generate', () => {
  const root = fixture();
  assert.throws(() => cleanupAgentContext(root), /Refusing to remove/);
  assert.ok(fs.existsSync(root));
});

test('build script passes only its staged context to a fake builder', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-build-fixture-'));
  scratch.push(root);
  const container = path.join(root, 'container');
  fs.mkdirSync(path.join(container, 'agent-runner'), { recursive: true });
  fs.mkdirSync(path.join(root, 'scripts'));
  fs.mkdirSync(path.join(root, 'setup', 'lib'), { recursive: true });
  for (const relative of [
    'container/build.sh',
    'container/Dockerfile',
    'container/.dockerignore',
    'container/agent-runner/package.json',
    'container/agent-runner/bun.lock',
    'container/cli-tools.json',
    'container/install-cli-tools.sh',
    'container/entrypoint.sh',
    'scripts/privacy-agent-context.mjs',
    'setup/lib/install-slug.sh',
  ]) {
    fs.copyFileSync(path.resolve(relative), path.join(root, relative));
  }
  fs.writeFileSync(path.join(container, 'untracked-secret.env'), 'must not reach the builder');
  fs.writeFileSync(path.join(root, '.env'), 'NANOCLAW_HARDENED_IMAGE=false\nTEST_ONLY_SECRET=synthetic\n');
  const fakeBuilder = path.join(root, 'fake-builder.sh');
  fs.writeFileSync(
    fakeBuilder,
    '#!/bin/sh\nfor argument do last="$argument"; done\nif [ -e "$last/untracked-secret.env" ] || [ -e "$last/.env" ]; then exit 7; fi\nprintf "%s\\n" "$last" > "$NANOCLAW_TEST_CONTEXT_PATH"\n',
    { mode: 0o700 },
  );
  const pathRecord = path.join(root, 'context-path');
  const status = await new Promise((resolve, reject) => {
    const child = spawn('bash', [path.join(container, 'build.sh'), 'build'], {
      cwd: root,
      env: { ...process.env, CONTAINER_RUNTIME: fakeBuilder, NANOCLAW_TEST_CONTEXT_PATH: pathRecord },
      stdio: 'ignore',
    });
    child.on('error', reject);
    child.on('close', resolve);
  });
  assert.equal(status, 0);
  const passedContext = fs.readFileSync(pathRecord, 'utf8').trim();
  assert.notEqual(passedContext, container);
  assert.ok(path.basename(passedContext).startsWith('nanoclaw-agent-context-'));
  assert.ok(!fs.existsSync(passedContext), 'the staged context must be removed after the build');
  assert.ok(fs.existsSync(path.join(container, 'untracked-secret.env')));
  assert.ok(fs.existsSync(path.join(root, '.env')));
});
