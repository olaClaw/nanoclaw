/** Stage exact, reviewed Compose build inputs without sending the checkout to Docker. */
import { createHash } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { auditSource } from './privacy-source-gate.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const manifestPath = path.join(root, 'deploy', 'context-files.json');
const marker = 'nanoclaw-compose-context/v1\n';
const fixed = {
  host: [
    '.npmrc',
    'package.json',
    'pnpm-lock.yaml',
    'pnpm-workspace.yaml',
    'tsconfig.json',
    'container/CLAUDE.md',
    'deploy/host.Dockerfile',
    'deploy/healthcheck.mjs',
  ],
  agent: ['deploy/agent.Dockerfile'],
};

function walkFiles(base) {
  const files = [];
  function visit(dir) {
    for (const item of fs.readdirSync(dir, { withFileTypes: true })) {
      const absolute = path.join(dir, item.name);
      if (item.isSymbolicLink()) throw new Error('Symlink in release source');
      if (item.isDirectory()) visit(absolute);
      else if (item.isFile()) files.push(path.relative(root, absolute).split(path.sep).join('/'));
      else throw new Error('Special file in release source');
    }
  }
  visit(path.join(root, base));
  return files;
}

function productionFiles(base) {
  return walkFiles(base).filter((relative) => {
    const local = relative.slice(base.length + 1);
    if (/(^|\/)(?:fixtures|__fixtures__|test|tests)\//.test(local)) return false;
    const name = path.posix.basename(relative);
    if (/(?:^|[.-])(?:test|spec|fixture)(?:[.-]|$)/.test(name) || name.endsWith('-vectors.json')) return false;
    if (!/\.(?:ts|md|json)$/.test(name)) throw new Error('Unexpected source file type');
    return true;
  });
}

function discovered(kind) {
  const skills = walkFiles('container/skills');
  if (skills.some((relative) => !relative.endsWith('.md'))) throw new Error('Unexpected skill file type');
  if (kind === 'host') {
    const instructions = walkFiles('container/agent-runner/src/mcp-tools').filter((relative) =>
      relative.endsWith('.instructions.md'),
    );
    return [...new Set([...fixed.host, ...productionFiles('src'), ...skills, ...instructions])].sort();
  }
  if (kind === 'agent') {
    return [...new Set([...fixed.agent, ...productionFiles('container/agent-runner/src'), ...skills])].sort();
  }
  throw new Error('Unknown context');
}

function safeSource(relative) {
  if (
    !relative ||
    relative.startsWith('/') ||
    relative.split('/').some((part) => !part || part === '.' || part === '..')
  ) {
    throw new Error('Unsafe manifest path');
  }
  const absolute = path.join(root, relative);
  let current = root;
  for (const part of relative.split('/')) {
    current = path.join(current, part);
    const stat = fs.lstatSync(current);
    if (stat.isSymbolicLink()) throw new Error('Symlink in manifest path');
  }
  if (!fs.statSync(absolute).isFile()) throw new Error('Non-file manifest path');
  return absolute;
}

export function verifyContextManifest() {
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  if (manifest.schema !== 'nanoclaw-compose-context/v1') throw new Error('Unknown context manifest');
  for (const kind of ['host', 'agent']) {
    const expected = discovered(kind);
    const listed = manifest[kind];
    if (
      !Array.isArray(listed) ||
      listed.length !== expected.length ||
      listed.some((relative, index) => relative !== expected[index])
    ) {
      throw new Error('Release source manifest needs review');
    }
    for (const relative of listed) safeSource(relative);
  }
  return manifest;
}

function sha256(bytes) {
  return createHash('sha256').update(bytes).digest('hex');
}

export function stageContext(kind, manifest = verifyContextManifest()) {
  if (kind !== 'host' && kind !== 'agent') throw new Error('Unknown context');
  const parent = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-compose-'));
  const context = path.join(parent, kind);
  try {
    fs.writeFileSync(path.join(parent, '.owner'), marker, { mode: 0o600 });
    fs.mkdirSync(context);
    for (const relative of manifest[kind]) {
      const source = safeSource(relative);
      const destination = path.join(context, relative.endsWith('.Dockerfile') ? 'Dockerfile' : relative);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      const bytes = fs.readFileSync(source);
      fs.writeFileSync(destination, bytes, { mode: 0o644 });
      if (sha256(fs.readFileSync(destination)) !== sha256(bytes)) throw new Error('Context copy mismatch');
    }
    return context;
  } catch {
    fs.rmSync(parent, { recursive: true, force: true });
    throw new Error('Context staging failed');
  }
}

export function cleanupContext(context) {
  const absolute = path.resolve(context);
  const parent = path.dirname(absolute);
  if (
    !['host', 'agent'].includes(path.basename(absolute)) ||
    path.dirname(parent) !== os.tmpdir() ||
    !path.basename(parent).startsWith('nanoclaw-compose-') ||
    fs.readFileSync(path.join(parent, '.owner'), 'utf8') !== marker
  ) {
    throw new Error('Refusing to remove an unowned context');
  }
  fs.rmSync(parent, { recursive: true });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const [command, argument] = process.argv.slice(2);
    if (command === '--cleanup' && argument) {
      cleanupContext(argument);
    } else if ((command === '--stage' && ['host', 'agent'].includes(argument)) || command === '--check') {
      const manifest = verifyContextManifest();
      const audit = await auditSource();
      if (!audit.passed) throw new Error('Source privacy gate blocked');
      if (command === '--stage') {
        process.stdout.write(stageContext(argument, manifest) + '\n');
      } else {
        process.stdout.write(
          'compose contexts: pass (' + manifest.host.length + ' host, ' + manifest.agent.length + ' agent files)\n',
        );
      }
    } else {
      throw new Error('Usage');
    }
  } catch {
    process.stderr.write('compose context: blocked; inspect manifest and source gate\n');
    process.exitCode = 1;
  }
}
