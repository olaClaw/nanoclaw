/**
 * Synthetic install for dashboard tests: real migrations, fake data.
 *
 * Seeds the central DB through the normal DB layer with two agents, three
 * channels, several chats, sessions and a user. Every value the API must
 * never return carries a canary (./canaries.ts). The dashboard projections in
 * later epics run against this seed; their tests check the counts below and
 * that no canary reaches a response, log or audit record.
 *
 * Call after `initTestDb()`.
 */
import { createAgentGroup } from '../../db/agent-groups.js';
import { createContainerConfig } from '../../db/container-configs.js';
import { createMessagingGroup, createMessagingGroupAgent } from '../../db/messaging-groups.js';
import { createSession } from '../../db/sessions.js';
import { createUser } from '../../modules/permissions/db/users.js';
import { CANARIES } from './canaries.js';

const CREATED = '2026-01-15T09:00:00.000Z';

export const SYNTHETIC = {
  agents: {
    main: { id: CANARIES.internal_agent_id, label: 'Main assistant', provider: 'opencode', model: 'fixture-model-a' },
    helper: { id: 'ag-fixture-helper', label: 'Helper', provider: null, model: null },
  },
  messagingGroups: [
    { id: CANARIES.internal_messaging_group_id, channel: 'signal', platform: CANARIES.phone, name: CANARIES.chat_name },
    { id: 'mg-fixture-signal-2', channel: 'signal', platform: CANARIES.platform_id, name: null },
    {
      id: 'mg-fixture-telegram',
      channel: 'telegram',
      platform: `tg:${CANARIES.platform_id}`,
      name: CANARIES.person_name,
    },
    { id: 'mg-fixture-cli', channel: 'cli', platform: 'cli:local', name: null },
  ],
  sessions: [
    {
      id: CANARIES.internal_session_id,
      agent: 'main',
      mg: 0,
      thread: CANARIES.thread_id,
      status: 'active',
      container: 'running',
    },
    { id: 'sess-fixture-2', agent: 'main', mg: 2, thread: null, status: 'active', container: 'idle' },
    { id: 'sess-fixture-3', agent: 'main', mg: 3, thread: null, status: 'closed', container: 'stopped' },
    { id: 'sess-fixture-4', agent: 'helper', mg: 1, thread: null, status: 'active', container: 'stopped' },
  ],
} as const;

/** What a correct projection of the seed reports; derived from SYNTHETIC. */
export const EXPECTED = {
  agents: 2,
  channels: { signal: 2, telegram: 1, cli: 1 },
  sessions: { total: 4, active: 3, main: { active: 2, total: 3 }, helper: { active: 1, total: 1 } },
} as const;

export async function seedSyntheticInstall(): Promise<void> {
  const { main, helper } = SYNTHETIC.agents;
  await createAgentGroup({
    id: main.id,
    name: main.label,
    folder: CANARIES.folder,
    agent_provider: null,
    created_at: CREATED,
  });
  await createAgentGroup({
    id: helper.id,
    name: helper.label,
    folder: 'fixture-helper',
    agent_provider: null,
    created_at: CREATED,
  });

  await createContainerConfig({
    agent_group_id: main.id,
    provider: main.provider,
    model: main.model,
    effort: null,
    image_tag: CANARIES.image_tag,
    assistant_name: CANARIES.person_name,
    max_messages_per_prompt: null,
    skills: JSON.stringify(['fixture-skill']),
    mcp_servers: JSON.stringify({
      mail: { type: 'http', url: CANARIES.mcp_url, headers: { Authorization: `Bearer ${CANARIES.token}` } },
    }),
    packages_apt: JSON.stringify(['fixture-apt-package']),
    packages_npm: JSON.stringify(['fixture-npm-package']),
    additional_mounts: JSON.stringify([{ hostPath: CANARIES.mount_path, containerPath: 'private', readonly: true }]),
    cli_scope: 'global',
    timezone: null,
    speed: null,
    updated_at: CREATED,
  });
  await createContainerConfig({
    agent_group_id: helper.id,
    provider: helper.provider,
    model: helper.model,
    effort: null,
    image_tag: null,
    assistant_name: null,
    max_messages_per_prompt: null,
    skills: '"all"',
    mcp_servers: '{}',
    packages_apt: '[]',
    packages_npm: '[]',
    additional_mounts: '[]',
    cli_scope: 'group',
    timezone: null,
    speed: null,
    updated_at: CREATED,
  });

  for (const group of SYNTHETIC.messagingGroups) {
    await createMessagingGroup({
      id: group.id,
      channel_type: group.channel,
      platform_id: group.platform,
      name: group.name,
      is_group: group.name ? 1 : 0,
      unknown_sender_policy: 'strict',
      created_at: CREATED,
    });
  }
  for (const [index, group] of SYNTHETIC.messagingGroups.entries()) {
    await createMessagingGroupAgent({
      id: `mga-fixture-${index}`,
      messaging_group_id: group.id,
      agent_group_id: index === 1 ? helper.id : main.id,
      engage_mode: 'pattern',
      engage_pattern: '.',
      sender_scope: 'all',
      ignored_message_policy: 'drop',
      session_mode: 'shared',
      priority: 0,
      created_at: CREATED,
    });
  }

  for (const session of SYNTHETIC.sessions) {
    await createSession({
      id: session.id,
      agent_group_id: SYNTHETIC.agents[session.agent].id,
      messaging_group_id: SYNTHETIC.messagingGroups[session.mg].id,
      thread_id: session.thread,
      agent_provider: null,
      status: session.status,
      container_status: session.container,
      last_active: '2026-01-15T10:17:42.512Z',
      created_at: CREATED,
    });
  }

  await createUser({
    id: `phone:${CANARIES.phone}`,
    kind: 'phone',
    display_name: CANARIES.person_name,
    created_at: CREATED,
  });
  await createUser({ id: `email:${CANARIES.email}`, kind: 'email', display_name: null, created_at: CREATED });
}
