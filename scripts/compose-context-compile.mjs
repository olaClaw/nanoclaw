/** Compile only the reviewed production source lists, using locally installed dependencies. */
import { spawnSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { cleanupContext, stageContext, verifyContextManifest } from './compose-context.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const compiler = path.join(root, 'node_modules', '.bin', 'tsc');

function compile(context, config) {
  const result = spawnSync(compiler, ['-p', config, '--noEmit'], {
    cwd: context,
    stdio: ['ignore', 'ignore', 'ignore'],
  });
  if (result.error || result.status !== 0) throw new Error('Restricted source typecheck failed');
}

export function checkCompiledContexts() {
  const manifest = verifyContextManifest();
  const host = stageContext('host', manifest);
  try {
    fs.symlinkSync(path.join(root, 'node_modules'), path.join(host, 'node_modules'), 'dir');
    compile(host, path.join(host, 'tsconfig.json'));
  } finally {
    cleanupContext(host);
  }
  const agent = stageContext('agent', manifest);
  try {
    const runner = path.join(agent, 'container', 'agent-runner');
    fs.copyFileSync(path.join(root, 'container', 'agent-runner', 'package.json'), path.join(runner, 'package.json'));
    fs.copyFileSync(path.join(root, 'container', 'agent-runner', 'tsconfig.json'), path.join(runner, 'tsconfig.json'));
    fs.symlinkSync(
      path.join(root, 'container', 'agent-runner', 'node_modules'),
      path.join(runner, 'node_modules'),
      'dir',
    );
    compile(agent, path.join(runner, 'tsconfig.json'));
  } finally {
    cleanupContext(agent);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    checkCompiledContexts();
    process.stdout.write('restricted Compose sources: typecheck passed\n');
  } catch {
    process.stderr.write('restricted Compose sources: typecheck failed without source output\n');
    process.exitCode = 1;
  }
}
