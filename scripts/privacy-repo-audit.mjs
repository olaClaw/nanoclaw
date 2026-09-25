/** Redacted inventory of source surfaces; never prints matched content. */
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const reservedEmailDomain = /^(?:example\.(?:com|net|org)|(?:[^.]+\.)+(?:example|test|invalid|localhost))$/i;

async function git(...args) {
  return new Promise((resolve, reject) => {
    const child = spawn('git', args, { cwd: root, stdio: ['ignore', 'pipe', 'ignore'] });
    const chunks = [];
    let size = 0;
    child.stdout.on('data', (chunk) => {
      size += chunk.length;
      if (size > 128 * 1024 * 1024) {
        child.kill();
        reject(new Error('Git output exceeds audit limit'));
      } else chunks.push(chunk);
    });
    child.on('error', () => reject(new Error('Git inventory unavailable')));
    child.on('close', (code) => {
      if (code === 0) resolve(Buffer.concat(chunks));
      else reject(new Error('Git inventory unavailable'));
    });
  });
}

function nulPaths(buffer) {
  return buffer.toString('utf8').split('\0').filter(Boolean);
}

function privateUrlCategories(text) {
  const categories = new Set();
  for (const match of text.matchAll(/https?:\/\/[^\s'"`<>]+/g)) {
    try {
      const host = new URL(match[0]).hostname.toLowerCase();
      if (host === 'localhost' || host === 'host.docker.internal') categories.add('generic-local-url');
      else if (/\.(?:lan|local|internal)$/.test(host)) categories.add('named-private-url-review');
    } catch {
      // Not a complete URL; leave it to the other candidate checks.
    }
  }
  return categories;
}

export function scanContent(bytes) {
  if (bytes.includes(0)) return ['binary-review'];
  const text = bytes.toString('utf8');
  const categories = new Set();
  for (const match of text.matchAll(/(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)/g)) {
    const octets = match[0].split('.').map(Number);
    if (!octets.every((value) => value <= 255)) continue;
    if (
      octets[0] === 10 ||
      (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
      (octets[0] === 192 && octets[1] === 168)
    ) {
      if (octets[0] === 172 && octets[1] === 17 && octets[2] === 0 && octets[3] === 1) {
        categories.add('default-docker-bridge-ipv4');
      } else {
        categories.add('rfc1918-ipv4-review');
      }
    } else if (octets[0] === 127 || octets[0] === 0) {
      categories.add('loopback-or-unspecified-ipv4');
    } else if (
      (octets[0] === 192 && octets[1] === 0 && octets[2] === 2) ||
      (octets[0] === 198 && octets[1] === 51 && octets[2] === 100) ||
      (octets[0] === 203 && octets[1] === 0 && octets[2] === 113)
    ) {
      categories.add('documentation-ipv4');
    } else {
      categories.add('other-ipv4-review');
    }
  }
  for (const match of text.matchAll(/\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b/g)) {
    categories.add(reservedEmailDomain.test(match[1]) ? 'example-email' : 'email-review');
  }
  if (/(?:\/(?:home|Users)\/|[A-Za-z]:\\Users\\)[A-Za-z0-9._-]+/i.test(text)) {
    categories.add('home-path-review');
  }
  for (const category of privateUrlCategories(text)) categories.add(category);
  if (
    /aoc_[A-Za-z0-9_-]{16,}|[0-9]{6,}:[A-Za-z0-9_-]{20,}|(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{16,}|BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY/.test(
      text,
    )
  ) {
    categories.add('credential-shape');
  }
  if (/(?:password|passwd|api[_-]?key|secret|token)\s*[:=]\s*['"]?[A-Za-z0-9_+./-]{12,}/i.test(text)) {
    categories.add('credential-assignment');
  }
  return [...categories].sort();
}

function safeArea(relative) {
  const first = relative.split('/')[0];
  return ['.claude', '.github', 'container', 'docs', 'scripts', 'setup', 'src', 'templates'].includes(first)
    ? first
    : 'other';
}

function addFinding(report, scope, relative, categories) {
  if (!categories.length) return;
  report.findings.push({
    scope,
    area: safeArea(relative),
    pathId: createHash('sha256').update(relative).digest('hex').slice(0, 12),
    categories,
  });
}

function readWorking(relative) {
  const absolute = path.join(root, relative);
  const stat = fs.lstatSync(absolute, { throwIfNoEntry: false });
  if (!stat) return { categories: ['missing-review'] };
  if (stat.isSymbolicLink()) return { categories: ['symlink-review'] };
  if (!stat.isFile()) return { categories: ['special-file-review'] };
  if (stat.size > 32 * 1024 * 1024) return { categories: ['large-file-review'] };
  return { categories: scanContent(fs.readFileSync(absolute)) };
}

export async function auditRepo() {
  const [headRaw, trackedRaw, untrackedRaw, stagedRaw] = await Promise.all([
    git('ls-tree', '-r', '--name-only', '-z', 'HEAD'),
    git('ls-files', '-z', '--cached'),
    git('ls-files', '-z', '--others', '--exclude-standard'),
    git('diff', '--cached', '--name-only', '-z'),
  ]);
  const head = nulPaths(headRaw);
  const tracked = nulPaths(trackedRaw);
  const untracked = nulPaths(untrackedRaw);
  const staged = nulPaths(stagedRaw);
  const trackedSet = new Set(tracked);
  const report = {
    schema: 'nanoclaw-privacy-source-audit/v1',
    head: (await git('rev-parse', '--short', 'HEAD')).toString('utf8').trim(),
    totals: { head: head.length, tracked: tracked.length, untracked: untracked.length, staged: staged.length },
    findings: [],
  };

  for (const relative of head) {
    addFinding(report, 'head', relative, scanContent(await git('show', `HEAD:${relative}`)));
  }
  for (const relative of [...tracked, ...untracked]) {
    addFinding(
      report,
      trackedSet.has(relative) ? 'worktree-tracked' : 'worktree-untracked',
      relative,
      readWorking(relative).categories,
    );
  }
  for (const relative of staged) {
    try {
      const bytes = await git('show', `:${relative}`);
      addFinding(report, 'index-staged', relative, scanContent(bytes));
    } catch {
      addFinding(report, 'index-staged', relative, ['deleted-or-unreadable-review']);
    }
  }
  const counts = {};
  for (const finding of report.findings) {
    for (const category of finding.categories) counts[category] = (counts[category] ?? 0) + 1;
  }
  report.categories = counts;
  return report;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const report = await auditRepo();
    if (process.argv.includes('--summary')) {
      const byScope = {};
      for (const finding of report.findings) {
        const counts = (byScope[finding.scope] ??= {});
        for (const category of finding.categories) counts[category] = (counts[category] ?? 0) + 1;
      }
      process.stdout.write(
        `${JSON.stringify({ schema: report.schema, head: report.head, totals: report.totals, byScope }, null, 2)}\n`,
      );
    } else {
      process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    }
  } catch {
    process.stderr.write('privacy audit: inventory failed without exposing source values\n');
    process.exitCode = 1;
  }
}
