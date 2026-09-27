import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { getAllAgentGroups } from '../../db/agent-groups.js';
import { closeDb, getDb, initTestDb } from '../../db/connection.js';
import { runMigrations } from '../../db/migrations/index.js';
import { ENDPOINTS, PATH_PARAMS, agentList, errorResponse, job, loginRequest } from './api.js';
import { POLICIES, policyDrift } from './authorization.js';
import { CANARIES, findLeaks } from '../fixtures/canaries.js';
import { EXAMPLES } from './examples.js';
import { publicId, resolvePublicId } from './opaque-id.js';
import {
  array,
  fieldPaths,
  int,
  nullable,
  object,
  oneOf,
  str,
  timestamp,
  minuteTimestamp,
  validate,
} from './schema.js';
import { EXPECTED, SYNTHETIC, seedSyntheticInstall } from '../fixtures/synthetic-install.js';

/**
 * Field names that would mean a raw runtime value is being passed through.
 * A schema field with one of these names needs an explicit, reviewed reason
 * and a change to this test.
 */
const FORBIDDEN_FIELD =
  /(?:^|_)(?:platform|phone|email|folder|path|url|host|ip|address|token|secret|password|key|thread|display|sender|text|content|message|history|prompt|env|mount_path|headers|image_tag)(?:_|$)/;
/** Input-only request fields: they never appear in a response (checked below). */
const INPUT_FIELDS = new Set(['$.password', '$.value', '$.endpoint']);
/** The CSRF token is the session's own anti-forgery value, not a runtime secret. */
const RESPONSE_FIELDS = new Set(['$.csrf_token']);

describe('schema language', () => {
  const sample = object({
    name: str(8, /^[a-z]+$/),
    count: int(0, 3),
    tags: array(oneOf('a', 'b'), 2),
    at: nullable(timestamp),
  });

  it('accepts an exact match', () => {
    expect(validate(sample, { name: 'ok', count: 3, tags: ['a'], at: null })).toEqual([]);
  });

  it('rejects extra, missing and out-of-range fields without echoing values', () => {
    const errors = validate(sample, { name: 'TOO-LONG-VALUE', count: 4, tags: ['a', 'b', 'a'], extra: CANARIES.token });
    expect(errors).toEqual([
      '$: unexpected_field',
      '$.name: too_long',
      '$.count: integer_range',
      '$.tags: too_many_items',
      '$.at: missing',
    ]);
    expect(findLeaks(errors.join('\n'))).toEqual([]);
  });

  it('rejects control characters, non-integers and unrounded activity times', () => {
    expect(validate(str(20), 'line\nbreak')).toEqual(['$: not_printable']);
    expect(validate(str(20), 'bidi\u202eoverride')).toEqual(['$: not_printable']);
    expect(validate(int(0, 10), 1.5)).toEqual(['$: integer_range']);
    expect(validate(minuteTimestamp, '2026-01-15T10:17:42Z')).toEqual(['$: timestamp']);
    expect(validate(minuteTimestamp, '2026-01-15T10:17:00Z')).toEqual([]);
    expect(validate(timestamp, '2026-01-15 10:17:42')).toEqual(['$: timestamp']);
  });
});

describe('dashboard API contract', () => {
  it('has unique endpoint names and method+path pairs', () => {
    expect(new Set(ENDPOINTS.map((e) => e.name)).size).toBe(ENDPOINTS.length);
    expect(new Set(ENDPOINTS.map((e) => `${e.method} ${e.path}`)).size).toBe(ENDPOINTS.length);
  });

  it('uses only declared path parameters and versioned paths', () => {
    for (const endpoint of ENDPOINTS) {
      expect(endpoint.path).toMatch(/^\/api\/v1\/[a-z0-9/{}-]+$/);
      for (const [, param] of endpoint.path.matchAll(/\{([a-z]+)\}/g))
        expect(Object.keys(PATH_PARAMS)).toContain(param);
    }
  });

  it('gives GET and DELETE no request body, and every other method one (except streamed uploads)', () => {
    for (const endpoint of ENDPOINTS) {
      if (endpoint.method !== 'POST') expect(endpoint.request, endpoint.name).toBeNull();
      else if (endpoint.name !== 'import_preflight') expect(endpoint.request, endpoint.name).not.toBeNull();
    }
  });

  it('never names a field after a raw runtime value', () => {
    const offending: string[] = [];
    for (const endpoint of ENDPOINTS) {
      for (const schema of [endpoint.request, endpoint.response]) {
        if (!schema) continue;
        for (const path of fieldPaths(schema)) {
          const field = path.split('.').at(-1)!.replace('[]', '');
          if (
            FORBIDDEN_FIELD.test(field) &&
            !(schema === endpoint.request ? INPUT_FIELDS : RESPONSE_FIELDS).has(path)
          ) {
            offending.push(`${endpoint.name} ${path}`);
          }
        }
      }
    }
    expect(offending).toEqual([]);
  });

  it('keeps secret inputs out of every response', () => {
    for (const endpoint of ENDPOINTS) {
      if (!endpoint.response) continue;
      const paths = fieldPaths(endpoint.response).map((path) => path.split('.').at(-1));
      for (const field of ['password', 'value', 'endpoint']) expect(paths, endpoint.name).not.toContain(field);
    }
  });

  it('has a valid, leak-free example for every endpoint', () => {
    for (const endpoint of ENDPOINTS) {
      expect(Object.hasOwn(EXAMPLES, endpoint.name), endpoint.name).toBe(true);
      const example = EXAMPLES[endpoint.name];
      if (endpoint.response === null) expect(example, endpoint.name).toBeNull();
      else expect(validate(endpoint.response, example), endpoint.name).toEqual([]);
      expect(findLeaks(JSON.stringify(example) ?? ''), endpoint.name).toEqual([]);
    }
  });

  it('bounds every list', () => {
    expect(validate(agentList, { items: Array(201).fill(EXAMPLES.agent), next_cursor: null })).toContain(
      '$.items: too_many_items',
    );
  });

  it('accepts the release-update job state as a job', () => {
    const fromTool = {
      id: 'job_0123456789abcdef',
      kind: 'update',
      phase: 'rollback',
      outcome: 'rolled_back',
      failure_category: 'derived_image_not_refreshed',
      rollback: 'healthy',
      release: { from_revision: 'a'.repeat(40), to_revision: 'b'.repeat(40), to_version: '2.4.0' },
      phases: [{ phase: 'preflight', at: '2026-01-15T12:00:00Z' }],
      started_at: '2026-01-15T12:00:00Z',
      updated_at: '2026-01-15T12:05:00Z',
      finished_at: '2026-01-15T12:05:00Z',
    };
    expect(validate(job, fromTool)).toEqual([]);
    expect(validate(job, { ...fromTool, failure_category: 'Traceback: /srv/x' })).toEqual([
      '$.failure_category: pattern',
    ]);
  });

  it('shapes errors as a stable code and a request id only', () => {
    expect(validate(errorResponse, { error: { code: 'not_found', request_id: 'req_0123456789abcdef' } })).toEqual([]);
    expect(
      validate(errorResponse, { error: { code: 'not_found', request_id: 'req_0123456789abcdef', detail: 'x' } }),
    ).toEqual(['$.error: unexpected_field']);
  });

  it('bounds the password input', () => {
    expect(validate(loginRequest, { password: '' })).toEqual(['$.password: pattern']);
    expect(validate(loginRequest, { password: 'x'.repeat(1025) })).toEqual(['$.password: too_long']);
  });
});

describe('authorization matrix', () => {
  it('covers exactly the endpoints', () => {
    expect(policyDrift()).toEqual([]);
  });

  it('allows anonymous access only to health and login', () => {
    const anonymous = Object.entries(POLICIES)
      .filter(([, p]) => p.auth === 'anonymous')
      .map(([name]) => name);
    expect(anonymous.sort()).toEqual(['health', 'login']);
  });

  it('protects every state-changing request with Origin, CSRF (after login) and audit', () => {
    for (const endpoint of ENDPOINTS) {
      const policy = POLICIES[endpoint.name];
      if (endpoint.method === 'GET') {
        expect(policy.csrf || policy.origin || policy.audit || policy.lock !== null, endpoint.name).toBe(false);
        continue;
      }
      expect(policy.origin, endpoint.name).toBe(true);
      expect(policy.audit, endpoint.name).toBe(true);
      expect(policy.csrf, endpoint.name).toBe(endpoint.name !== 'login');
    }
  });

  it('requires re-authentication for secrets, channels, updates, backups, restores and the model switch', () => {
    for (const name of [
      'channel_state',
      'secret',
      'model_preflight',
      'model_apply',
      'update',
      'backup_create',
      'backup_export',
      'import_preflight',
      'import_apply',
    ]) {
      expect(POLICIES[name].auth, name).toBe('reauth');
    }
  });

  it('runs every writer-stopping operation under the lock shared with the CLI', () => {
    const locked = Object.entries(POLICIES)
      .filter(([, p]) => p.lock === 'maintenance')
      .map(([name]) => name);
    expect(locked.sort()).toEqual(['backup_create', 'import_apply', 'model_apply', 'update']);
  });

  it('asks for confirm: true exactly where the policy says so', () => {
    for (const endpoint of ENDPOINTS) {
      if (!endpoint.request) continue;
      const fields = fieldPaths(endpoint.request);
      expect(fields.includes('$.confirm'), endpoint.name).toBe(POLICIES[endpoint.name].confirm);
    }
  });
});

describe('public ids', () => {
  const key = Buffer.alloc(32, 7);
  const other = Buffer.alloc(32, 8);

  it('are stable, kind-scoped, key-scoped and reveal nothing of the internal id', () => {
    const id = publicId('agent', CANARIES.internal_agent_id, key);
    expect(id).toMatch(/^agt_[0-9a-f]{32}$/);
    expect(publicId('agent', CANARIES.internal_agent_id, key)).toBe(id);
    expect(publicId('session', CANARIES.internal_agent_id, key).slice(4)).not.toBe(id.slice(4));
    expect(publicId('agent', CANARIES.internal_agent_id, other)).not.toBe(id);
    expect(findLeaks(id)).toEqual([]);
  });

  it('resolve back only to the right internal id', () => {
    const ids = ['ag-a', 'ag-b', CANARIES.internal_agent_id];
    expect(resolvePublicId('agent', publicId('agent', 'ag-b', key), ids, key)).toBe('ag-b');
    expect(resolvePublicId('agent', publicId('agent', 'ag-b', other), ids, key)).toBeNull();
    expect(resolvePublicId('agent', 'agt_' + '0'.repeat(32), ids, key)).toBeNull();
  });

  it('refuse a short key', () => {
    expect(() => publicId('agent', 'ag-a', Buffer.alloc(16))).toThrow('too short');
  });
});

describe('synthetic install', () => {
  beforeEach(async () => {
    await runMigrations(await initTestDb());
    await seedSyntheticInstall();
  });
  afterEach(async () => {
    await closeDb();
  });

  it('seeds through the real migrations with the expected shape', async () => {
    expect((await getAllAgentGroups()).length).toBe(EXPECTED.agents);
    const channels = await getDb().all<{ channel_type: string; n: number }>(
      'SELECT channel_type, COUNT(*) AS n FROM messaging_groups GROUP BY channel_type',
    );
    expect(Object.fromEntries(channels.map((row) => [row.channel_type, row.n]))).toEqual(EXPECTED.channels);
    const sessions = await getDb().all<{ status: string }>('SELECT status FROM sessions');
    expect(sessions.length).toBe(EXPECTED.sessions.total);
    expect(sessions.filter((row) => row.status === 'active').length).toBe(EXPECTED.sessions.active);
    expect(SYNTHETIC.sessions.length).toBe(EXPECTED.sessions.total);
  });

  it('plants a canary in every sensitive column, so a raw dump is caught', async () => {
    const dump = JSON.stringify({
      groups: await getDb().all('SELECT * FROM agent_groups'),
      configs: await getDb().all('SELECT * FROM container_configs'),
      chats: await getDb().all('SELECT * FROM messaging_groups'),
      sessions: await getDb().all('SELECT * FROM sessions'),
      users: await getDb().all('SELECT * FROM users'),
    });
    const leaks = findLeaks(dump);
    for (const name of [
      'platform_id',
      'phone',
      'email',
      'person_name',
      'chat_name',
      'thread_id',
      'folder',
      'mount_path',
      'mcp_url',
      'token',
      'image_tag',
      'internal_agent_id',
      'internal_session_id',
      'internal_messaging_group_id',
    ]) {
      expect(leaks, name).toContain(`canary:${name}`);
    }
  });
});

describe('leak detector', () => {
  it('catches addresses, emails, URLs and absolute paths whatever their value', () => {
    expect(findLeaks('{"a":"203.0.113.9"}')).toEqual(['shape:ipv4']);
    expect(findLeaks('someone@example.org')).toEqual(['shape:email']);
    expect(findLeaks('see https://example.org/x')).toEqual(['shape:url']);
    expect(findLeaks('"/srv/nanoclaw/state"')).toEqual(['shape:absolute_path']);
    expect(findLeaks('{"version":"2.4.0","at":"2026-01-15T12:00:00Z"}')).toEqual([]);
  });
});
