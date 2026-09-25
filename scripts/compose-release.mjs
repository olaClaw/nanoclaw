/** Create or validate a digest-pinned Compose release manifest after image publication. */
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { imageKeys, readReleaseManifest, validateReleaseManifest } from '../deploy/release-manifest.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function argumentsFrom(argv) {
  const options = {};
  for (let i = 0; i < argv.length; i += 2) {
    const flag = argv[i];
    if (!flag?.startsWith('--') || !argv[i + 1] || flag.slice(2) in options) throw new Error('Invalid options');
    options[flag.slice(2)] = argv[i + 1];
  }
  return options;
}

export function createReleaseManifest(images, projectRoot = root) {
  const dirty = execFileSync('git', ['status', '--porcelain=v1'], {
    cwd: projectRoot,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'ignore'],
  });
  if (dirty) throw new Error('Release source checkout must be clean');
  const git = (rev) =>
    execFileSync('git', ['rev-parse', '--verify', rev], {
      cwd: projectRoot,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
  const pkg = JSON.parse(fs.readFileSync(path.join(projectRoot, 'package.json'), 'utf8'));
  return validateReleaseManifest({
    schema: 'nanoclaw-compose-release/v1',
    version: pkg.version,
    revision: git('HEAD'),
    tree: git('HEAD^{tree}'),
    images,
  });
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  try {
    const [, , command, ...argv] = process.argv;
    const options = argumentsFrom(argv);
    if (command === 'check' && Object.keys(options).length === 1 && options.file) {
      readReleaseManifest(options.file);
      process.stdout.write('Release manifest valid.\n');
    } else if (
      command === 'create' &&
      options.output &&
      JSON.stringify(Object.keys(options).sort()) === JSON.stringify(['output', ...imageKeys].sort())
    ) {
      const images = Object.fromEntries(imageKeys.map((key) => [key, options[key]]));
      const manifest = createReleaseManifest(images);
      fs.writeFileSync(options.output, JSON.stringify(manifest, null, 2) + '\n', { flag: 'wx', mode: 0o644 });
      process.stdout.write('Release manifest created.\n');
    } else {
      throw new Error('Usage');
    }
  } catch {
    process.stderr.write('Release manifest command blocked.\n');
    process.exitCode = 1;
  }
}
