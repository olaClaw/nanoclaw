import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { readEnvFile } from '../src/env.js';
import { upsertEnvVars } from '../setup/set-env.js';
import { discoverLocalModelIds, validateModel } from './opencode-model-config.js';

const CONFIG_KEYS = [
  'OPENCODE_PROVIDER',
  'OPENCODE_MODEL',
  'OPENCODE_SMALL_MODEL',
  'OPENCODE_BASE_URL',
  'OPENCODE_AUTH_MODE',
  'NANOCLAW_EGRESS_LOCKDOWN',
] as const;

export interface SyncArguments {
  model?: string;
  groups: string[];
}

export interface SyncResult {
  baseUrl: string;
  model: string;
  changed: boolean;
  restartedGroups: string[];
}

export interface SyncDependencies {
  readConfig: () => Record<string, string | undefined>;
  discoverModels: (baseUrl: string) => Promise<string[]>;
  saveModels: (model: string) => void;
  restartGroup: (groupId: string) => void;
  environment: NodeJS.ProcessEnv;
}

export function parseSyncArguments(args: string[]): SyncArguments {
  const result: SyncArguments = { groups: [] };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--model' && args[i + 1] && !args[i + 1].startsWith('--')) {
      if (result.model) throw new Error('--model may be provided only once.');
      result.model = args[++i];
    } else if (args[i] === '--group' && args[i + 1] && !args[i + 1].startsWith('--')) {
      result.groups.push(args[++i]);
    } else {
      throw new Error('Usage: local-openai-sync.ts [--model model-id] [--group group-id]...');
    }
  }
  result.groups = [...new Set(result.groups)];
  return result;
}

function defaultDependencies(): SyncDependencies {
  return {
    readConfig: () => readEnvFile([...CONFIG_KEYS]),
    discoverModels: (baseUrl) => discoverLocalModelIds(baseUrl),
    saveModels: (model) =>
      void upsertEnvVars({
        OPENCODE_MODEL: `openai/${model}`,
        OPENCODE_SMALL_MODEL: `openai/${model}`,
      }),
    restartGroup: (groupId) => {
      const command = path.join(process.cwd(), 'bin', 'ncl');
      const result = spawnSync(command, ['groups', 'restart', '--id', groupId], {
        cwd: process.cwd(),
        stdio: 'inherit',
      });
      if (result.error) throw result.error;
      if (result.status !== 0) throw new Error(`Could not restart group ${groupId}.`);
    },
    environment: process.env,
  };
}

function configuredValue(
  key: (typeof CONFIG_KEYS)[number],
  saved: Record<string, string | undefined>,
  environment: NodeJS.ProcessEnv,
): string | undefined {
  return environment[key] ?? saved[key];
}

function normalizeRequestedModel(model: string | undefined): string | undefined {
  if (!model) return undefined;
  return model.startsWith('openai/') ? model.slice('openai/'.length) : model;
}

export async function syncLocalOpenAi(
  args: SyncArguments,
  dependencies: SyncDependencies = defaultDependencies(),
): Promise<SyncResult> {
  const saved = dependencies.readConfig();
  const provider = configuredValue('OPENCODE_PROVIDER', saved, dependencies.environment);
  if (provider !== 'openai') {
    throw new Error('OpenCode must first be configured with the openai backend for a local endpoint.');
  }

  const authMode = configuredValue('OPENCODE_AUTH_MODE', saved, dependencies.environment);
  if (authMode === 'chatgpt') {
    throw new Error('OpenCode is still in ChatGPT auth mode. Reconfigure it as Local or self-hosted first.');
  }

  const baseUrl = configuredValue('OPENCODE_BASE_URL', saved, dependencies.environment);
  if (!baseUrl || baseUrl === 'native') {
    throw new Error('OPENCODE_BASE_URL must be an OpenAI-compatible local endpoint including /v1.');
  }
  let endpoint: URL;
  try {
    endpoint = new URL(baseUrl);
  } catch {
    throw new Error('OPENCODE_BASE_URL is not a valid absolute URL.');
  }
  if (!['http:', 'https:'].includes(endpoint.protocol) || endpoint.username || endpoint.password) {
    throw new Error('OPENCODE_BASE_URL must be an http(s) URL without embedded credentials.');
  }

  const lockdown = configuredValue('NANOCLAW_EGRESS_LOCKDOWN', saved, dependencies.environment)?.toLowerCase();
  if (lockdown === 'true') {
    throw new Error('NANOCLAW_EGRESS_LOCKDOWN=true prevents direct access to the remote LAN endpoint.');
  }

  const models = [...new Set(await dependencies.discoverModels(baseUrl))].sort((a, b) => a.localeCompare(b));
  if (!models.length) throw new Error('The endpoint returned no model IDs.');

  const requested = normalizeRequestedModel(args.model);
  let model: string;
  if (requested) {
    if (!models.includes(requested)) {
      throw new Error(`Model ${requested} is not exposed by the endpoint. Available: ${models.join(', ')}`);
    }
    model = requested;
  } else if (models.length === 1) {
    model = models[0];
  } else {
    throw new Error(`The endpoint exposes multiple models; choose one with --model. Available: ${models.join(', ')}`);
  }

  const fullModel = `openai/${model}`;
  const invalid = validateModel(fullModel, 'openai');
  if (invalid) throw new Error(invalid);
  for (const key of ['OPENCODE_MODEL', 'OPENCODE_SMALL_MODEL'] as const) {
    if (dependencies.environment[key] !== undefined && dependencies.environment[key] !== fullModel) {
      throw new Error(`Exported ${key} overrides .env; unset it before synchronizing.`);
    }
  }
  const changed = saved.OPENCODE_MODEL !== fullModel || saved.OPENCODE_SMALL_MODEL !== fullModel;
  dependencies.saveModels(model);
  for (const group of args.groups) dependencies.restartGroup(group);
  return { baseUrl, model: fullModel, changed, restartedGroups: args.groups };
}

async function main(): Promise<void> {
  const result = await syncLocalOpenAi(parseSyncArguments(process.argv.slice(2)));
  console.log(`${result.changed ? 'Synchronized' : 'Confirmed'} ${result.model} from ${result.baseUrl}.`);
  if (result.restartedGroups.length) console.log(`Restarted groups: ${result.restartedGroups.join(', ')}.`);
  else console.log('No groups restarted; pass --group <group-id> to apply the model immediately.');
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : 'Local model synchronization failed.');
    process.exitCode = 1;
  });
}
