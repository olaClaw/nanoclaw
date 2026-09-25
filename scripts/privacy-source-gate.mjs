/** Block newly introduced private source values, including blobs removed in later commits. */
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

export const ACCEPTED_UPSTREAM = 'c313d061b0263dfbb1967ab64e4d7524c09a71a0';
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const maxBytes = 32 * 1024 * 1024;
const safeHomeNames = new Set([
  'me',
  'node',
  'user',
  'username',
  'test',
  'alice',
  'bob',
  'example',
  'demo',
  'fake',
  'agent',
]);
const reservedEmailDomain = /^(?:example\.(?:com|net|org)|(?:[^.]+\.)+(?:example|test|invalid|localhost))$/i;

async function git(root, ...args) {
  return new Promise((resolve, reject) => {
    const child = spawn('git', args, { cwd: root, stdio: ['ignore', 'pipe', 'ignore'] });
    const chunks = [];
    let size = 0;
    let settled = false;
    const fail = () => {
      if (settled) return;
      settled = true;
      reject(new Error('Git operation failed'));
    };
    child.stdout.on('data', (chunk) => {
      size += chunk.length;
      if (size > 128 * 1024 * 1024) {
        child.kill();
        fail();
      } else chunks.push(chunk);
    });
    child.on('error', fail);
    child.on('close', (code) => {
      if (settled) return;
      settled = true;
      if (code === 0) resolve(Buffer.concat(chunks));
      else reject(new Error('Git operation failed'));
    });
  });
}

function paths(bytes) {
  return bytes.toString('utf8').split('\0').filter(Boolean);
}

function addCandidate(found, kind, value) {
  found.add(kind + '\0' + value);
}

export function candidates(bytes) {
  const found = new Set();
  if (bytes.length > maxBytes || bytes.includes(0)) return { found, unreadable: true };
  let source;
  try {
    source = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  } catch {
    return { found, unreadable: true };
  }
  for (const match of source.matchAll(/(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)/g)) {
    const parts = match[0].split('.').map(Number);
    if (parts.some((part) => part > 255)) continue;
    if (
      parts[0] === 10 ||
      (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31) ||
      (parts[0] === 192 && parts[1] === 168)
    )
      addCandidate(found, 'private-ipv4', match[0]);
  }
  for (const match of source.matchAll(/\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b/g)) {
    if (!reservedEmailDomain.test(match[1]) && !match[1].endsWith('.noreply.github.com')) {
      addCandidate(found, 'personal-email', match[0].toLowerCase());
    }
  }
  for (const match of source.matchAll(
    /aoc_[A-Za-z0-9_-]{16,}|[0-9]{6,}:[A-Za-z0-9_-]{20,}|(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}|BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY/g,
  )) {
    addCandidate(found, 'credential-shape', match[0]);
  }
  for (const match of source.matchAll(/https?:\/\/[^\s'"<>]+/g)) {
    try {
      const host = new URL(match[0]).hostname.toLowerCase();
      if (/\.(?:lan|local|internal)$/.test(host) && host !== 'host.docker.internal') {
        addCandidate(found, 'private-url', host);
      }
    } catch {
      // Incomplete URL; other patterns still apply.
    }
  }
  for (const match of source.matchAll(/(?:\/(?:home|Users)\/|[A-Za-z]:\\Users\\)([A-Za-z0-9._-]+)/gi)) {
    if (!safeHomeNames.has(match[1].toLowerCase())) addCandidate(found, 'home-path', match[0]);
  }
  return { found, unreadable: false };
}

function forbiddenPath(relative) {
  const parts = relative.toLowerCase().split('/');
  const name = parts.at(-1);
  if (name === '.env.example') return false;
  return (
    parts.some((part) => ['data', 'groups', 'backups', '.ssh'].includes(part)) ||
    /^\.env(?:\.|$)/.test(name) ||
    /\.(?:pem|key|p12|pfx)$/.test(name)
  );
}

function count(counts, kind) {
  counts[kind] = (counts[kind] ?? 0) + 1;
}

function checkContent(bytes, baseline, counts) {
  const result = candidates(bytes);
  if (result.unreadable) {
    count(counts, 'binary-or-large-file');
    return;
  }
  for (const item of result.found) {
    if (!baseline.has(item)) count(counts, item.split('\0', 1)[0]);
  }
}

export async function auditSource({ root = repoRoot, base = ACCEPTED_UPSTREAM } = {}) {
  await git(root, 'rev-parse', '--verify', base + '^{commit}');
  await git(root, 'merge-base', '--is-ancestor', base, 'HEAD');
  const baselinePaths = paths(await git(root, 'ls-tree', '-r', '--name-only', '-z', base));
  const baselinePathSet = new Set(baselinePaths);
  const baseline = new Set();
  for (const relative of baselinePaths) {
    const result = candidates(await git(root, 'show', base + ':' + relative));
    for (const item of result.found) baseline.add(item);
  }

  const counts = {};
  const commits = (await git(root, 'rev-list', base + '..HEAD')).toString('utf8').trim().split('\n').filter(Boolean);
  for (const commit of commits) {
    checkContent(await git(root, 'show', '-s', '--format=%B', commit), baseline, counts);
    const identities = await git(root, 'show', '-s', '--format=%ae%n%ce', commit);
    if ([...candidates(identities).found].some((item) => item.startsWith('personal-email\0'))) {
      count(counts, 'commit-email');
    }
    for (const entry of paths(await git(root, 'ls-tree', '-r', '-z', commit))) {
      const separator = entry.indexOf('\t');
      if (separator < 0) throw new Error('Invalid Git tree');
      const relative = entry.slice(separator + 1);
      const mode = entry.slice(0, separator).split(' ', 1)[0];
      if (!baselinePathSet.has(relative) && forbiddenPath(relative)) count(counts, 'runtime-or-secret-path');
      if (mode === '120000' && !baselinePathSet.has(relative)) count(counts, 'symlink');
    }
  }
  const objects = (await git(root, 'rev-list', '--objects', base + '..HEAD'))
    .toString('utf8')
    .trim()
    .split('\n')
    .filter(Boolean);
  for (const line of objects) {
    const oid = line.split(' ', 1)[0];
    if ((await git(root, 'cat-file', '-t', oid)).toString('utf8').trim() === 'blob') {
      checkContent(await git(root, 'cat-file', 'blob', oid), baseline, counts);
    }
  }

  const changed = new Set([
    ...paths(await git(root, 'diff', '--name-only', '-z', base)),
    ...paths(await git(root, 'ls-files', '--others', '--exclude-standard', '-z')),
  ]);
  for (const relative of changed) {
    const absolute = path.resolve(root, relative);
    if (!absolute.startsWith(path.resolve(root) + path.sep)) throw new Error('Invalid source path');
    const stat = fs.lstatSync(absolute, { throwIfNoEntry: false });
    if (!stat) continue;
    if (forbiddenPath(relative)) count(counts, 'runtime-or-secret-path');
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size > maxBytes) {
      count(counts, 'binary-or-large-file');
    } else {
      checkContent(fs.readFileSync(absolute), baseline, counts);
    }
  }
  const stagedDeleted = new Set(
    paths(await git(root, 'diff', '--cached', '--diff-filter=D', '--name-only', '-z', base)),
  );
  for (const relative of paths(await git(root, 'diff', '--cached', '--name-only', '-z', base))) {
    if (stagedDeleted.has(relative)) continue;
    if (forbiddenPath(relative)) count(counts, 'runtime-or-secret-path');
    try {
      checkContent(await git(root, 'show', ':' + relative), baseline, counts);
    } catch {
      throw new Error('Staged source unavailable');
    }
  }
  return {
    passed: Object.keys(counts).length === 0,
    counts,
    baseline: base,
    scannedCommits: commits.length,
    scannedBlobs: objects.length,
  };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const report = await auditSource();
    process.stdout.write('privacy source gate: ' + (report.passed ? 'pass' : 'blocked') + '\n');
    process.stdout.write(JSON.stringify({ categories: report.counts, commits: report.scannedCommits }) + '\n');
    if (!report.passed) process.exitCode = 1;
  } catch {
    process.stderr.write('privacy source gate: unavailable; check Git history and source access\n');
    process.exitCode = 1;
  }
}
