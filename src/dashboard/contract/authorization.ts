/**
 * Authorization matrix: one policy per endpoint in ./api.ts.
 *
 * The dashboard has a single administrator, so authorization is about *how
 * strongly* a request must be authenticated, not *who* may call it:
 *
 * - `anonymous`: only the health probe, the login itself and first-run setup
 *   (which needs a one-time code read on the server console).
 * - `session`: a valid, unexpired server-side session cookie.
 * - `reauth`: a session whose password was re-entered within the reauth window
 *   (design.md: secrets, channel changes, updates, backups, restores).
 *
 * Every state-changing request also needs an allowed `Origin`, and all of them
 * except login need the CSRF token. Operations that stop writers or replace
 * state share one `maintenance` lock with the CLI tools, so web and terminal
 * can never run two of them at once. The contract tests enforce these rules.
 */
import { ENDPOINTS } from './api.js';

export type AuthLevel = 'anonymous' | 'session' | 'reauth';
export type RateLimitClass = 'login' | 'read' | 'mutation' | 'probe';

export interface EndpointPolicy {
  readonly auth: AuthLevel;
  /** `Origin` must be the dashboard's own origin. */
  readonly origin: boolean;
  /** `X-CSRF-Token` must match the session's token. */
  readonly csrf: boolean;
  /** The request body must carry `confirm: true`. */
  readonly confirm: boolean;
  readonly rateLimit: RateLimitClass;
  /** Recorded in the audit log: operation, outcome and correlation ID only. */
  readonly audit: boolean;
  /** Shared with the CLI tools (release update, backup, import). */
  readonly lock: 'maintenance' | null;
}

const read: EndpointPolicy = {
  auth: 'session',
  origin: false,
  csrf: false,
  confirm: false,
  rateLimit: 'read',
  audit: false,
  lock: null,
};
const mutation = (changes: Partial<EndpointPolicy>): EndpointPolicy => ({
  auth: 'session',
  origin: true,
  csrf: true,
  confirm: true,
  rateLimit: 'mutation',
  audit: true,
  lock: null,
  ...changes,
});
const dangerous = (changes: Partial<EndpointPolicy> = {}) => mutation({ auth: 'reauth', ...changes });

export const POLICIES: Readonly<Record<string, EndpointPolicy>> = {
  health: { ...read, auth: 'anonymous', rateLimit: 'probe' },
  login: mutation({ auth: 'anonymous', csrf: false, confirm: false, rateLimit: 'login' }),
  setup_state: { ...read, auth: 'anonymous', rateLimit: 'probe' },
  setup: mutation({ auth: 'anonymous', csrf: false, confirm: false, rateLimit: 'login' }),
  session: read,
  logout: mutation({ confirm: false }),
  reauth: mutation({ confirm: false, rateLimit: 'login' }),

  overview: read,
  agents: read,
  agent: read,
  channels: read,
  model_settings: read,
  sessions: read,
  releases: read,
  backups: read,
  job: read,

  restart_agent: mutation({}),
  channel_state: dangerous(),
  secret: dangerous({ confirm: false }),
  model_preflight: dangerous({ confirm: false }),
  model_apply: dangerous({ lock: 'maintenance' }),
  update: dangerous({ lock: 'maintenance' }),
  backup_create: dangerous({ lock: 'maintenance' }),
  backup_verify: mutation({ confirm: false }),
  backup_key: dangerous(),
  backup_key_saved: dangerous(),
  backup_delete: dangerous({ lock: 'maintenance' }),
  backup_export: dangerous(),
  import_preflight: dangerous({ confirm: false }),
  import_apply: dangerous({ lock: 'maintenance' }),
};

export function policyFor(name: string): EndpointPolicy {
  const policy = POLICIES[name];
  if (!policy) throw new Error(`no policy for dashboard endpoint: ${name}`);
  return policy;
}

/** Endpoints without a policy, or policies without an endpoint; empty when consistent. */
export function policyDrift(): string[] {
  const names = new Set(ENDPOINTS.map((item) => item.name));
  return [
    ...[...names].filter((name) => !POLICIES[name]).map((name) => `missing_policy:${name}`),
    ...Object.keys(POLICIES)
      .filter((name) => !names.has(name))
      .map((name) => `orphan_policy:${name}`),
  ];
}
