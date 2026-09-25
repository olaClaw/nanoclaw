---
name: local-openai-runtime
description: Keep NanoClaw's OpenCode defaults synchronized with a keyless local OpenAI-compatible endpoint whose served model changes over time.
---

# Local OpenAI-compatible runtime

Use this after `/add-opencode` when llama.cpp, DwarfStar4, vLLM, Halogen, or
another OpenAI-compatible runtime is exposed at one stable URL while the served
model ID changes.

This skill deliberately adds no provider and no proxy. OpenCode remains the
provider maintained by upstream NanoClaw. The helper only discovers the current
model through `GET /models`, updates both OpenCode model defaults, and optionally
restarts selected groups.

## Install

Require the OpenCode provider payload to be installed first. Copy the two files
from this skill into the matching project paths, overwriting older copies from
this skill when reapplying it:

- `scripts/local-openai-sync.ts` to `scripts/local-openai-sync.ts`
- `scripts/local-openai-sync.test.ts` to `scripts/local-openai-sync.test.ts`
- `scripts/local-openai-smoke.ts` to `scripts/local-openai-smoke.ts`
- `scripts/local-openai-smoke.test.ts` to `scripts/local-openai-smoke.test.ts`

Run the integration test and the host build:

```bash
pnpm exec vitest run scripts/local-openai-sync.test.ts scripts/local-openai-smoke.test.ts
pnpm run build
```

## Configure once

Configure OpenCode with its official setup flow:

```bash
pnpm exec tsx setup/index.ts --step provider-auth opencode
```

Choose `Local or self-hosted`, enter the endpoint including `/v1`, confirm that
it works without an API key, and select the discovered model. For a server
reached through a Tailscale exit node by its LAN address, the URL is typically
`http://<server-lan-ip>:8000/v1`.

Direct LAN access requires open container egress. Keep
`NANOCLAW_EGRESS_LOCKDOWN=false`; an internal-only container network cannot
route to the remote LAN. If OneCLI supplies `HTTP_PROXY` or `HTTPS_PROXY`, add
the endpoint IP to both `NO_PROXY` and `no_proxy` in the NanoClaw service
environment, together with `127.0.0.1,localhost`, then restart the host service.
Do not put credentials in these settings.

Select OpenCode for each intended group and restart it:

```bash
ncl groups config update --id <group-id> --provider opencode
ncl groups restart --id <group-id>
```

Do not set a per-group model override unless it is intentional. A group model
override wins over the synchronized installation default.

## After changing runtime or model

When the endpoint exposes exactly one model, synchronize and restart one or more
groups in one command:

```bash
pnpm exec tsx scripts/local-openai-sync.ts --group <group-id>
```

Repeat `--group` for additional groups. If `/models` exposes more than one ID,
select one explicitly:

```bash
pnpm exec tsx scripts/local-openai-sync.ts --model <model-id> --group <group-id>
```

The helper refuses ambiguous catalogs, exported environment variables that
would override `.env`, ChatGPT auth mode, non-OpenAI backends, native endpoints,
and enabled egress lockdown. It never guesses a model ID.

## Podman or Docker smoke test

The full NanoClaw setup and OneCLI lifecycle remain Docker-first. The agent
image itself can be built with Podman and the local OpenAI path can be tested
without installing or reconfiguring the host service:

```bash
CONTAINER_RUNTIME=podman ./container/build.sh build
pnpm exec tsx scripts/local-openai-smoke.ts --runtime podman
```

The smoke test discovers the single model from the configured endpoint, starts
an ephemeral container, asks the model to execute a harmless `printf` through
the Bash tool, and requires both the tool result and final reply. Pass
`--base-url`, `--model`, or `--image` to override the `.env` and derived image
defaults. It places no credentials in the container.

## Upgrade order

Use `/update-nanoclaw`, not a raw pull. Let `/update-skills` refresh the official
OpenCode payload, then reapply this skill and run its test. Because the helper
only consumes upstream's public scripts and adds files, upstream refreshes do
not overwrite it.

## Troubleshooting

From the NanoClaw host, verify the route before changing configuration:

```bash
curl -fsS http://<server-lan-ip>:8000/v1/models
```

If that fails while the exit node is selected, check routing and overlapping
private subnets. If host discovery works but the agent cannot connect, verify
container egress and the service's `NO_PROXY`/`no_proxy` values. If several
models are returned, pass the exact ID with `--model`. Podman support here is a
provider smoke-test path, not a replacement for NanoClaw's Docker-based setup,
OneCLI gateway, service management, or egress-lockdown lifecycle.

To remove the helper, follow [REMOVE.md](REMOVE.md).
