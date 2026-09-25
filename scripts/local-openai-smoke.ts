import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

import { readEnvFile } from '../src/env.js';
import { getDefaultContainerImage } from '../src/install-slug.js';
import { discoverLocalModelIds, validateModel } from './opencode-model-config.js';

const MARKER = 'NANOCLAW_LOCAL_TOOL_OK';

export interface SmokeArguments {
  runtime: 'docker' | 'podman';
  baseUrl?: string;
  model?: string;
  image?: string;
}

export interface SmokeDependencies {
  readConfig: () => Record<string, string | undefined>;
  discoverModels: (baseUrl: string) => Promise<string[]>;
  defaultImage: () => string;
  runContainer: (runtime: string, args: string[]) => { status: number | null; stdout: string; stderr: string };
}

export function parseSmokeArguments(args: string[]): SmokeArguments {
  const parsed: SmokeArguments = {
    runtime: process.env.CONTAINER_RUNTIME === 'podman' ? 'podman' : 'docker',
  };
  for (let i = 0; i < args.length; i++) {
    const value = args[i + 1];
    if (args[i] === '--runtime' && (value === 'docker' || value === 'podman')) {
      parsed.runtime = value;
      i++;
    } else if (args[i] === '--base-url' && value && !value.startsWith('--')) {
      parsed.baseUrl = value;
      i++;
    } else if (args[i] === '--model' && value && !value.startsWith('--')) {
      parsed.model = value;
      i++;
    } else if (args[i] === '--image' && value && !value.startsWith('--')) {
      parsed.image = value;
      i++;
    } else {
      throw new Error(
        'Usage: local-openai-smoke.ts [--runtime docker|podman] [--base-url URL] [--model model-id] [--image image]',
      );
    }
  }
  return parsed;
}

export function buildOpenCodeSmokeConfig(baseUrl: string, model: string): string {
  return JSON.stringify({
    enabled_providers: ['openai'],
    model: `openai/${model}`,
    provider: {
      openai: {
        npm: '@ai-sdk/openai-compatible',
        options: { apiKey: 'placeholder', baseURL: baseUrl },
        models: { [model]: { id: model, name: model, tool_call: true } },
      },
    },
  });
}

export function verifySmokeOutput(output: string): void {
  const events = output
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line) as Record<string, unknown>;
      } catch {
        return undefined;
      }
    })
    .filter((event): event is Record<string, unknown> => event !== undefined);
  const toolWorked = events.some((event) => {
    if (event.type !== 'tool_use' || !event.part || typeof event.part !== 'object') return false;
    const state = (event.part as Record<string, unknown>).state;
    return Boolean(state && typeof state === 'object' && (state as Record<string, unknown>).output === MARKER);
  });
  const replyWorked = events.some((event) => {
    if (event.type !== 'text' || !event.part || typeof event.part !== 'object') return false;
    return (event.part as Record<string, unknown>).text === MARKER;
  });
  if (!toolWorked || !replyWorked) {
    throw new Error(`Smoke test did not observe both the ${MARKER} tool result and final reply.`);
  }
}

function defaultDependencies(): SmokeDependencies {
  return {
    readConfig: () => readEnvFile(['OPENCODE_BASE_URL', 'OPENCODE_MODEL']),
    discoverModels: (baseUrl) => discoverLocalModelIds(baseUrl),
    defaultImage: () => getDefaultContainerImage(process.cwd()),
    runContainer: (runtime, args) => {
      const result = spawnSync(runtime, args, {
        cwd: process.cwd(),
        encoding: 'utf8',
        timeout: 120_000,
        maxBuffer: 8 * 1024 * 1024,
      });
      if (result.error) throw result.error;
      return { status: result.status, stdout: result.stdout ?? '', stderr: result.stderr ?? '' };
    },
  };
}

function normalizeModel(model: string): string {
  return model.startsWith('openai/') ? model.slice('openai/'.length) : model;
}

export async function runLocalOpenAiSmoke(
  args: SmokeArguments,
  dependencies: SmokeDependencies = defaultDependencies(),
): Promise<{ runtime: string; image: string; baseUrl: string; model: string }> {
  const saved = dependencies.readConfig();
  const baseUrl = args.baseUrl ?? saved.OPENCODE_BASE_URL;
  if (!baseUrl || baseUrl === 'native') throw new Error('Provide a local OpenAI-compatible --base-url including /v1.');
  const url = new URL(baseUrl);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('The base URL must be http(s) without embedded credentials.');
  }

  const discovered = [...new Set(await dependencies.discoverModels(baseUrl))].sort((a, b) => a.localeCompare(b));
  const requested = args.model ? normalizeModel(args.model) : undefined;
  let model: string;
  if (requested) {
    if (!discovered.includes(requested)) {
      throw new Error(`Model ${requested} is not exposed by the endpoint. Available: ${discovered.join(', ')}`);
    }
    model = requested;
  } else if (discovered.length === 1) {
    model = discovered[0];
  } else if (!discovered.length) {
    throw new Error('The endpoint returned no model IDs.');
  } else {
    throw new Error(
      `The endpoint exposes multiple models; choose one with --model. Available: ${discovered.join(', ')}`,
    );
  }
  const invalid = validateModel(`openai/${model}`, 'openai');
  if (invalid) throw new Error(invalid);

  const image = args.image ?? dependencies.defaultImage();
  const config = buildOpenCodeSmokeConfig(baseUrl, model);
  const prompt = `Use the bash tool to run printf ${MARKER}, then reply with exactly the tool output.`;
  const result = dependencies.runContainer(args.runtime, [
    'run',
    '--rm',
    '-e',
    `OPENCODE_CONFIG_CONTENT=${config}`,
    '--entrypoint',
    'opencode',
    image,
    'run',
    '--pure',
    '--auto',
    '--model',
    `openai/${model}`,
    '--format',
    'json',
    prompt,
  ]);
  if (result.status !== 0) {
    throw new Error(`The ${args.runtime} smoke container failed: ${result.stderr.trim() || `exit ${result.status}`}`);
  }
  verifySmokeOutput(result.stdout);
  return { runtime: args.runtime, image, baseUrl, model: `openai/${model}` };
}

async function main(): Promise<void> {
  const result = await runLocalOpenAiSmoke(parseSmokeArguments(process.argv.slice(2)));
  console.log(`Smoke test passed with ${result.runtime}, ${result.image}, and ${result.model} at ${result.baseUrl}.`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  main().catch((error: unknown) => {
    console.error(error instanceof Error ? error.message : 'Local OpenAI smoke test failed.');
    process.exitCode = 1;
  });
}
