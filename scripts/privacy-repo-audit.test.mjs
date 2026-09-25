import assert from 'node:assert/strict';
import { test } from 'node:test';

import { scanContent } from './privacy-repo-audit.mjs';

test('classifies source candidates without returning their values', () => {
  const privateAddress = [10, 21, 34, 55].join('.');
  const credential = `aoc_${'X'.repeat(32)}`;
  const content = Buffer.from(`server=${privateAddress}\nauth=${credential}\n`);
  const categories = scanContent(content);
  assert.ok(categories.includes('rfc1918-ipv4-review'));
  assert.ok(categories.includes('credential-shape'));
  assert.ok(!JSON.stringify(categories).includes(privateAddress));
  assert.ok(!JSON.stringify(categories).includes(credential));
});

test('distinguishes the generic Docker bridge from other private IPv4 candidates', () => {
  const bridge = [172, 17, 0, 1].join('.');
  assert.deepEqual(scanContent(Buffer.from(bridge)), ['default-docker-bridge-ipv4']);
  assert.deepEqual(scanContent(Buffer.from([192, 168, 44, 7].join('.'))), ['rfc1918-ipv4-review']);
});

test('separates reserved examples from email addresses needing review', () => {
  assert.deepEqual(scanContent(Buffer.from('demo@example.org')), ['example-email']);
  assert.deepEqual(scanContent(Buffer.from('demo@host.stage.test')), ['example-email']);
  assert.deepEqual(scanContent(Buffer.from(['person', 'real-domain.invalidtld'].join('@'))), ['email-review']);
});

test('distinguishes documentation IPv4 ranges from other addresses', () => {
  for (const octets of [[192, 0, 2, 8], [198, 51, 100, 8], [203, 0, 113, 8]]) {
    const address = octets.join('.');
    assert.deepEqual(scanContent(Buffer.from(address)), ['documentation-ipv4']);
  }
  assert.deepEqual(scanContent(Buffer.from([8, 8, 8, 8].join('.'))), ['other-ipv4-review']);
});

test('flags Unix and Windows home paths without reporting the account segment', () => {
  const segment = 'accountfixture';
  const unix = `/home/${segment}/settings.json`;
  const windows = `C:\\Users\\${segment}\\settings.json`;
  for (const sample of [unix, windows]) {
    const categories = scanContent(Buffer.from(sample));
    assert.deepEqual(categories, ['home-path-review']);
    assert.ok(!JSON.stringify(categories).includes(segment));
  }
});

test('marks binary files for review without decoding their contents', () => {
  assert.deepEqual(scanContent(Buffer.from([1, 0, 2])), ['binary-review']);
});
