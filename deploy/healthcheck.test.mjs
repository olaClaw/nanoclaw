import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { test } from 'node:test';

const script = new URL('./healthcheck.mjs', import.meta.url);

async function check(cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [script.pathname], { cwd, stdio: 'ignore' });
    child.once('error', reject);
    child.once('exit', (code) => resolve(code));
  });
}

test('healthcheck rejects a missing listener', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-health-'));
  try {
    assert.equal(await check(root), 1);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('healthcheck accepts a live CLI socket', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nanoclaw-health-'));
  const data = path.join(root, 'data');
  fs.mkdirSync(data);
  const server = net.createServer((socket) => socket.end());
  try {
    await new Promise((resolve) => server.listen(path.join(data, 'ncl.sock'), resolve));
    assert.equal(await check(root), 0);
  } finally {
    await new Promise((resolve) => server.close(resolve));
    fs.rmSync(root, { recursive: true, force: true });
  }
});
