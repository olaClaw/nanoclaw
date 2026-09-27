/**
 * One example response per endpoint, valid against its schema.
 *
 * Documentation and UI fixtures (the prototype and later the real screens can
 * render these without a host). All IDs and values are synthetic.
 */
const AGENT_MAIN = 'agt_4f1c9a2e7b3d4c5e8f9a0b1c2d3e4f50';
const AGENT_HELPER = 'agt_9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c41';
const REVISION_OLD = '117a271e0000000000000000000000000000beef';
const REVISION_NEW = '062c0896000000000000000000000000000cafe0';
const JOB = 'job_0123456789abcdef';

const updateJob = {
  id: JOB,
  kind: 'update',
  phase: 'refresh_images',
  outcome: 'succeeded',
  failure_category: null,
  rollback: null,
  release: { from_revision: REVISION_OLD, to_revision: REVISION_NEW, to_version: '2.4.0' },
  backup: null,
  phases: [
    { phase: 'preflight', at: '2026-01-15T12:00:00Z' },
    { phase: 'control_backup', at: '2026-01-15T12:00:41Z' },
    { phase: 'stop_host', at: '2026-01-15T12:00:41Z' },
    { phase: 'switch_release', at: '2026-01-15T12:00:44Z' },
    { phase: 'start_services', at: '2026-01-15T12:00:45Z' },
    { phase: 'wait_channels', at: '2026-01-15T12:01:20Z' },
    { phase: 'refresh_images', at: '2026-01-15T12:01:31Z' },
  ],
  started_at: '2026-01-15T12:00:00Z',
  updated_at: '2026-01-15T12:04:02Z',
  finished_at: '2026-01-15T12:04:02Z',
};

const sessionState = {
  expires_at: '2026-01-15T20:00:00Z',
  idle_expires_at: '2026-01-15T12:30:00Z',
  reauth_expires_at: null,
  csrf_token: 'Qm9ndXMtY3NyZi10b2tlbi1mb3ItZXhhbXBsZXMtb25',
};

const accepted = (kind: string) => ({ job: { id: JOB, kind } });

export const EXAMPLES: Readonly<Record<string, unknown>> = {
  health: { status: 'ok' },
  login: sessionState,
  session: sessionState,
  logout: null,
  setup_state: { needed: false },
  setup: sessionState,
  reauth: { ...sessionState, reauth_expires_at: '2026-01-15T12:05:00Z' },

  overview: {
    release: { version: '2.4.0', revision: REVISION_NEW },
    checked_at: '2026-01-15T12:10:00Z',
    services: [
      { name: 'nanoclaw', state: 'healthy' },
      { name: 'onecli', state: 'healthy' },
      { name: 'postgres', state: 'healthy' },
      { name: 'signal-cli', state: 'healthy' },
    ],
    channels: { connected: 3, total: 3 },
    llm: { state: 'reachable' },
    alerts: [{ code: 'backup_older_than_day', severity: 'warning' }],
  },
  agents: {
    items: [
      {
        id: AGENT_MAIN,
        label: 'Main assistant',
        provider: 'opencode',
        model: 'fixture-model-a',
        state: 'running',
        sessions: { active: 2, total: 3 },
      },
      {
        id: AGENT_HELPER,
        label: 'Helper',
        provider: null,
        model: null,
        state: 'stopped',
        sessions: { active: 1, total: 1 },
      },
    ],
    next_cursor: null,
  },
  agent: {
    id: AGENT_MAIN,
    label: 'Main assistant',
    provider: 'opencode',
    model: 'fixture-model-a',
    state: 'running',
    sessions: { active: 2, total: 3 },
    created_at: '2026-01-15T09:00:00.000Z',
    container: { state: 'running', image: { derived: true, current: true, release_revision: REVISION_NEW } },
    capabilities: { cli_scope: 'global', skills: 'selected', packages: 2, mcp_servers: 1, additional_mounts: 1 },
  },
  channels: {
    items: [
      { id: 'chn_1a2b3c4d5e6f708192a3b4c5d6e7f801', type: 'signal', state: 'connected', chats: 2 },
      { id: 'chn_2b3c4d5e6f708192a3b4c5d6e7f80112', type: 'telegram', state: 'connected', chats: 1 },
      { id: 'chn_3c4d5e6f708192a3b4c5d6e7f8011223', type: 'cli', state: 'connected', chats: 1 },
    ],
    next_cursor: null,
  },
  model_settings: {
    mode: 'mixed',
    provider: 'opencode',
    model: 'fixture-model-a',
    endpoint_status: { configured: true, state: 'reachable' },
    agents: { total: 2, matching: 1 },
  },
  sessions: {
    items: [
      {
        id: 'ses_aa11bb22cc33dd44ee55ff6600112233',
        agent: AGENT_MAIN,
        state: 'active',
        container: 'running',
        last_activity: '2026-01-15T10:17:00Z',
      },
      {
        id: 'ses_bb22cc33dd44ee55ff66001122334455',
        agent: AGENT_HELPER,
        state: 'active',
        container: 'stopped',
        last_activity: null,
      },
    ],
    next_cursor: 'c2Vzc2lvbnMtMg',
  },
  releases: {
    installed: { version: '2.4.0', revision: REVISION_NEW },
    candidate: null,
    last_update: updateJob,
  },
  backups: {
    items: [
      {
        id: 'bkp_0f1e2d3c4b5a69788796a5b4c3d2e1f0',
        created_at: '2026-01-15T11:38:56Z',
        release_revision: REVISION_OLD,
        size_bytes: 5_046_586_572,
        verification: 'verified',
        exportable: true,
        key_on_host: false,
      },
    ],
    next_cursor: null,
  },
  job: updateJob,

  restart_agent: { agent: AGENT_MAIN, restarted: 1 },
  channel_state: accepted('channel_state'),
  secret: { kind: 'llm_api_key', configured: true, rotated_at: '2026-01-15T12:20:00Z' },
  model_preflight: {
    preflight_id: 'pfl_fedcba9876543210',
    ready: false,
    leaves_lan: false,
    agents: [
      { id: AGENT_MAIN, ready: true, reason: null },
      { id: AGENT_HELPER, ready: false, reason: 'provider_not_installed' },
    ],
    sessions_to_restart: 2,
    expires_at: '2026-01-15T12:15:00Z',
  },
  model_apply: accepted('model_settings_apply'),
  update: accepted('update'),
  backup_create: accepted('backup_create'),
  backup_verify: accepted('backup_verify'),
  backup_key: { backup: 'bkp_0f1e2d3c4b5a69788796a5b4c3d2e1f0', key: '0123456789abcdef'.repeat(4) },
  backup_delete: { backup: 'bkp_0f1e2d3c4b5a69788796a5b4c3d2e1f0', deleted: true },
  backup_key_saved: { backup: 'bkp_0f1e2d3c4b5a69788796a5b4c3d2e1f0', key_on_host: false },
  backup_export: accepted('backup_export'),
  import_preflight: accepted('import_preflight'),
  import_apply: accepted('import_apply'),
};
