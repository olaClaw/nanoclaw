/** One-time marker for a fresh, verified Compose installation. */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { verifyRuntimeRelease } from './release-manifest.mjs';

export function bootstrapComposeInstall({ dataDir = path.resolve('data'), verify = verifyRuntimeRelease } = {}) {
  const manifest = verify();
  const info = fs.lstatSync(dataDir);
  if (!info.isDirectory() || info.isSymbolicLink() || fs.readdirSync(dataDir).length !== 0) {
    throw new Error('Compose bootstrap requires an empty, real data directory');
  }
  const marker = path.join(dataDir, 'upgrade-state.json');
  const temporary = path.join(dataDir, `.upgrade-state.${process.pid}.tmp`);
  const state = {
    version: manifest.version,
    commit: manifest.revision,
    tree: manifest.tree,
    updatedAt: new Date().toISOString(),
    via: 'compose-bootstrap',
  };
  let fd;
  try {
    fd = fs.openSync(temporary, 'wx', 0o600);
    fs.writeFileSync(fd, JSON.stringify(state, null, 2) + '\n');
    fs.fsyncSync(fd);
    fs.closeSync(fd);
    fd = undefined;
    fs.renameSync(temporary, marker);
  } catch (error) {
    if (fd !== undefined) fs.closeSync(fd);
    fs.rmSync(temporary, { force: true });
    throw error;
  }
  return state;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    bootstrapComposeInstall();
    process.stdout.write('Compose bootstrap marker created.\n');
  } catch {
    process.stderr.write('Compose bootstrap blocked; no install marker created.\n');
    process.exitCode = 1;
  }
}
