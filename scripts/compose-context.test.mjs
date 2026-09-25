import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { cleanupContext, stageContext, verifyContextManifest } from './compose-context.mjs';

function filesUnder(root) {
  const result = [];
  function visit(dir) {
    for (const item of fs.readdirSync(dir, { withFileTypes: true })) {
      const absolute = path.join(dir, item.name);
      if (item.isDirectory()) visit(absolute);
      else result.push(path.relative(root, absolute).split(path.sep).join('/'));
    }
  }
  visit(root);
  return result.sort();
}

test('stages exactly the reviewed host and agent inputs', () => {
  const manifest = verifyContextManifest();
  for (const kind of ['host', 'agent']) {
    const context = stageContext(kind, manifest);
    try {
      const expected = manifest[kind]
        .map((relative) => (relative.endsWith('.Dockerfile') ? 'Dockerfile' : relative))
        .sort();
      assert.deepEqual(filesUnder(context), expected);
      assert.ok(fs.readFileSync(path.join(context, 'Dockerfile'), 'utf8').includes('FROM '));
      assert.equal(
        expected.some((relative) => /(?:^|\/)(?:data|groups|store|fixtures|__fixtures__)\//.test(relative)),
        false,
      );
      assert.equal(
        expected.some((relative) => /(?:^|[.-])(?:test|spec|fixture)(?:[.-]|$)/.test(path.posix.basename(relative))),
        false,
      );
    } finally {
      cleanupContext(context);
    }
    assert.equal(fs.existsSync(context), false);
  }
});

test('refuses to remove any directory it did not create', () => {
  assert.throws(() => cleanupContext('/tmp/host'));
});
