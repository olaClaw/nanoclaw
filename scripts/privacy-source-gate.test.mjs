import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { auditSource, candidates } from './privacy-source-gate.mjs';

async function git(root, ...args) {
  return new Promise((resolve, reject) => {
    const child = spawn('git', args, { cwd: root, stdio: ['ignore', 'pipe', 'ignore'] });
    const chunks = [];
    child.stdout.on('data', (chunk) => chunks.push(chunk));
    child.on('error', reject);
    child.on('close', (code) => {
      if (code === 0) resolve(Buffer.concat(chunks).toString('utf8').trim());
      else reject(new Error('Fixture Git operation failed'));
    });
  });
}

async function commit(root) {
  await git(root, 'add', '-A');
  await git(root, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture');
  return git(root, 'rev-parse', 'HEAD');
}

async function fixture(run) {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'privacy-gate-'));
  try {
    await git(root, 'init', '-q');
    await fs.writeFile(path.join(root, 'upstream.txt'), 'public' + '@' + 'upstream.org\n');
    const base = await commit(root);
    await run(root, base);
  } finally {
    await fs.rm(root, { recursive: true, force: true });
  }
}

test('accepts an already public upstream value copied into the draft', async () => {
  await fixture(async (root, base) => {
    await fs.writeFile(path.join(root, 'new.txt'), 'public' + '@' + 'upstream.org\n');
    const result = await auditSource({ root, base });
    assert.equal(result.passed, true);
  });
});

test('blocks sensitive content retained only in an older custom commit', async () => {
  await fixture(async (root, base) => {
    const privateAddress = [10, 42, 1, 8].join('.');
    const personalEmail = ['fixture-person', 'synthetic.org'].join('@');
    const token = 'aoc_' + 'A'.repeat(24);
    await fs.writeFile(path.join(root, 'draft.txt'), [privateAddress, personalEmail, token].join('\n'));
    await commit(root);
    await fs.writeFile(path.join(root, 'draft.txt'), 'clean\n');
    await commit(root);
    const result = await auditSource({ root, base });
    assert.equal(result.passed, false);
    assert.ok(result.counts['private-ipv4'] > 0);
    assert.ok(result.counts['personal-email'] > 0);
    assert.ok(result.counts['credential-shape'] > 0);
    assert.equal(JSON.stringify(result).includes(privateAddress), false);
    assert.equal(JSON.stringify(result).includes(token), false);
  });
});

test('checks staged content even when the working file has been cleaned', async () => {
  await fixture(async (root, base) => {
    const value = [192, 168, 4, 2].join('.');
    await fs.writeFile(path.join(root, 'draft.txt'), value);
    await git(root, 'add', 'draft.txt');
    await fs.writeFile(path.join(root, 'draft.txt'), 'clean\n');
    const result = await auditSource({ root, base });
    assert.equal(result.passed, false);
    assert.ok(result.counts['private-ipv4'] > 0);
  });
});

test('blocks runtime paths retained only in an older custom commit', async () => {
  await fixture(async (root, base) => {
    await fs.writeFile(path.join(root, '.env'), 'PLACEHOLDER=fixture\n');
    await commit(root);
    await fs.unlink(path.join(root, '.env'));
    await commit(root);
    const result = await auditSource({ root, base });
    assert.equal(result.passed, false);
    assert.ok(result.counts['runtime-or-secret-path'] > 0);
  });
});

test('blocks private commit messages and personal commit email metadata', async () => {
  await fixture(async (root, base) => {
    const address = [10, 33, 2, 5].join('.');
    const email = ['fixture-person', 'synthetic.org'].join('@');
    await fs.writeFile(path.join(root, 'draft.txt'), 'clean\n');
    await git(root, 'add', '-A');
    await git(root, '-c', 'user.name=Fixture', '-c', 'user.email=' + email, 'commit', '-qm', 'fixture ' + address);
    const result = await auditSource({ root, base });
    assert.equal(result.passed, false);
    assert.ok(result.counts['private-ipv4'] > 0);
    assert.ok(result.counts['commit-email'] > 0);
    assert.equal(JSON.stringify(result).includes(address), false);
    assert.equal(JSON.stringify(result).includes(email), false);
  });
});

test('accepts the public GitHub committer address used for PR merge previews', async () => {
  await fixture(async (root, base) => {
    await fs.writeFile(path.join(root, 'draft.txt'), 'clean\n');
    await git(root, 'add', '-A');
    await git(root, '-c', 'user.name=GitHub', '-c', 'user.email=noreply@github.com', 'commit', '-qm', 'fixture');
    const result = await auditSource({ root, base });
    assert.equal(result.passed, true);
  });
});

test('fails closed when the accepted baseline is unavailable', async () => {
  await fixture(async (root) => {
    await assert.rejects(auditSource({ root, base: '0'.repeat(40) }));
  });
});

test('recognizes only non-generic local home paths', () => {
  const generic = candidates(Buffer.from('/home/me /home/node'));
  const specific = candidates(Buffer.from('/home/' + 'private-user'));
  assert.equal(generic.found.size, 0);
  assert.equal([...specific.found][0].startsWith('home-path'), true);
});
