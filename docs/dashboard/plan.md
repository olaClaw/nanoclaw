# NanoClaw administrative dashboard — shared implementation plan

Status: design agreed, implementation not started. Owner: Claude Code (the
operator moved all dashboard work to Claude Code on 2026-09-27; Codex no
longer works on it). Update **Current state**, **Next step** and the
**Progress log** after each dashboard intervention. This document does not
authorize a production deploy, credential access, image publication, or
changes to the migration/recovery controllers without a separate review.

Companion documents in this directory:

- [design.md](design.md) — confirmed decisions, security boundaries,
  authentication, screens, acceptance criteria.
- [api-contract.md](api-contract.md) — read-only and mutating endpoints, field
  allowlists, backup/export/import flow.
- [backlog-and-tests.md](backlog-and-tests.md) — epics D0–D9, threat model,
  test matrix, milestones.
- [threat-model.md](threat-model.md) — assets, actors, boundaries, threats
  T1–T20 with their controls and tests.
- [prototype/index.html](prototype/index.html) — static UI prototype with
  placeholder values only; not wired to any API.

## Current state

- The legacy-to-Compose migration is complete and production runs on the
  Compose stack. This document is not a health report; check the operational
  state before any production-facing step.
- `scripts/compose-release-update.py` supports a production mode and records a
  persistent job state (`--status`, interrupted-run guard) that the dashboard
  update screen will read.
- **D0 done:** the API contract lives in `src/dashboard/contract/` (strict
  schemas, endpoint list, authorization matrix, HMAC public IDs, one example
  per endpoint) with synthetic fixtures and canaries in
  `src/dashboard/fixtures/`, and the threat model in
  [threat-model.md](threat-model.md). No dashboard service, host boundary or
  authentication exists yet.
- **D1 done (read-only):** the host boundary in `src/dashboard/host/` serves
  overview, agents, agent detail, channels, sessions and model settings over a
  Unix socket (`NANOCLAW_DASHBOARD_ADMIN_SOCKET`, off by default). It
  re-validates every request and response against the contract, resolves
  public IDs server-side and answers `not_implemented` for operations that
  need the root-side operations service (releases, backups, jobs,
  mutations).

## Next step

D2: the dashboard service itself, a separate web process that talks to the host
only through the admin socket. It holds password login (hash and rate limit),
server-side sessions, CSRF and Origin checks and the reauth window, and
forwards authorized calls to the boundary. Then add it to Compose with the
socket directory as its only mount, and rehearse on the test LXC.

## Agreed product behavior

- One administrator, password authentication in v1; no second factor for now.
  Access is restricted to LAN/VPN and HTTPS. Nginx Proxy Manager (NPM) is the
  planned reverse proxy in the same Compose deployment, with a dedicated
  private runtime subdomain. NPM admin access must not be broadly published.
  Pin its image by digest; keep DNS credentials and certificate state outside
  Git, images, build contexts and CI artifacts. DNS-01 does not require an
  exposed port 80. Verify the real LAN/VPN bind and firewall before deploy.
- The panel must cover agents, channels, configuration, write-only secrets,
  redacted diagnostics/logs, backup create/export/import, and updates. Updates
  must be launchable from either web or CLI through the **same** persistent
  job/lock and recovery path.
- The local LLM endpoint and model may be edited in the panel. This replaces
  the earlier `.env`-only decision. Values stay in private runtime state and
  are never baked into an image or repository. The web service requests a
  named host operation; it does not edit `.env` directly or accept arbitrary
  variable names or filesystem paths.
- Provider/model selection is **global for the installation**. Applying it
  updates both the default for new agents and every existing agent, including
  agents with per-group provider/model overrides. There is no per-agent
  selection in v1. Merely changing `DEFAULT_AGENT_PROVIDER` is insufficient:
  the existing group settings must be migrated as part of the job. Save a
  configuration snapshot first; preflight every agent, model, endpoint,
  credential and gateway permission; show affected sessions and the controlled
  restart plan; refuse the whole change if any agent is unready. On failure,
  restore all prior settings and verify health. A remote provider requires a
  clear warning that prompts/context for **all** agents may leave the LAN.
- Claude, ChatGPT via a supported login path, and OpenAI API are distinct
  authentication/billing modes. Do not treat an OpenAI API key as a ChatGPT
  subscription. Verify the supported provider paths in the current code
  before implementing each option.
- Backup export/import is part of the requested v1. Export a verified,
  authenticated encrypted bundle with a fresh high-entropy key shown once and
  stored separately by the operator. Import stages in quarantine, verifies
  integrity/compatibility/space, backs up the target, and offers a rehearsal
  with identities/tasks disabled before a definitive move with the source
  stopped. Never overwrite live state directly from an upload.

## Security boundary

- `dashboard` gets **no Docker socket, `data/ncl.sock`, database mount, broker
  files or OneCLI secrets**. The existing `ncl` socket treats callers as host
  operators and must not be exposed to the browser or dashboard container.
- Use a narrow host-side API of named, typed, server-authorized operations;
  no arbitrary CLI/shell/Docker passthrough. Explicit response schemas must
  exclude raw database rows, chat/history, paths, private endpoints, IPs,
  tokens and raw logs. Sensitive values are write-only.
- Bootstrap the admin password interactively on the host. Store only a salted
  adaptive hash (Argon2id), not a password in `.env`, arguments or logs.
  Require rate limits, revocable server-side sessions, secure/HttpOnly/
  SameSite cookies, CSRF and Origin checks, re-authentication for dangerous
  actions, and a local-console reset that revokes all sessions.
- Privacy gates cover source/diff/history, minimal build contexts, image
  layers/metadata, logs and CI artifacts. Fixtures and tests use only
  synthetic identities, reserved example domains and canary secrets.

## Delivery order and checks

1. API schemas, authorization matrix, threat model and synthetic fixtures.
2. Narrow host boundary; password login/session/reset; read-only overview.
3. Agent/channel views and typed safe operations; redacted diagnostics.
4. Global provider/model preflight and apply with coordinated restart,
   complete rollback and tests for partial failure.
5. Backup job and verified portable export/import; test rehearsal and
   controlled cutover between synthetic instances.
6. Web update button attached to the already-tested CLI release job/lock.
7. NPM HTTPS/LAN-VPN integration, privacy audit and end-to-end LXC rehearsal.
8. Publish a user-facing installation and configuration guide, verified on a
   clean LXC/VM with a fresh install and on a separate test instance for
   migration. Do not publish untested commands or private runtime values.

Do not call the dashboard ready until backup import/export, update recovery,
the global provider switch and the privacy/security gates have all been
tested. The first safe vertical slice is read-only, not a production deploy.

## Public installation and configuration guide

Publish a detailed guide in this repository for operators who have not seen
our development notes. It is a release deliverable for the **whole Compose
stack**, not just the dashboard. Maintain it alongside releases when commands,
environment keys, provider paths or screens change. Its examples must use
reserved domains and placeholders, never real IP addresses, credentials,
account identifiers, chat data or private service names.

The guide must cover:

1. Supported host prerequisites, Docker/Compose versions and LXC/VM caveats;
   storage, permissions, networking, DNS and LAN/VPN-only exposure.
2. Obtaining a pinned release/manifest, verifying provenance and preparing
   persistent directories and a private `.env`/secret files without putting
   any secret in Git, image layers, shell history or command-line arguments.
3. First start and health checks for NanoClaw, OneCLI, database, brokers and
   Signal; administrator password bootstrap; HTTPS/NPM setup and firewall.
4. Configuring local LLM endpoint/model or a supported external provider,
   OneCLI authentication, agents, Telegram/Signal and optional mail/calendar
   integrations. State clearly that dashboard provider selection applies to
   **all** agents, existing and future, with preflight and restart.
5. Migration procedures, separated by source and destination: a fresh
   installation; import from a legacy NanoClaw installation; and transfer
   between two Compose installations. For each supported path document
   compatibility requirements, what state is included (agents, complete
   history, memory, configuration, secrets, gateway and channel identities),
   what must be reconfigured, preflight and encrypted backup, key custody,
   transfer, isolated rehearsal with identities/tasks disabled, comparison of
   counts and functional checks, definitive cutover with the source stopped,
   verification of the new instance, and rollback. Explicitly prohibit two
   instances from using the same live Telegram/Signal identities at once.
   State when migration is unsupported or requires operator intervention.
6. Daily operations: logs with redaction, service status, backup, verification,
   encrypted export/import, recovery, and release updates from both CLI and
   dashboard.
7. A troubleshooting table with symptoms, safe diagnostic commands, expected
   redacted results, and when to stop rather than bypass a failed gate.
8. Clean-install **and migration** acceptance checklists plus a privacy
   checklist. Validate every documented command against the released images
   on disposable instances before publication; rehearse each supported
   migration path rather than publishing an untested recipe.

The existing `compose-preview`, recovery, import and release-update documents
are technical references, not substitutes for this end-to-end guide. During
implementation, choose its final filename and link it prominently from the
repository README.

## Coordination and open decisions

- Integrate with the reviewed release-update controller and its job state;
  never create a second update controller in the web service. Changing
  backup/import/update scripts requires recovery tests and a rehearsal.
- Choose whether browser export streams large bundles directly or prepares a
  private SFTP transfer. Neither choice may put the encryption key beside the
  bundle or in browser storage/logs.
- Define the safe interruption point for active agent sessions during a
  global provider switch and the user-facing confirmation.
- Confirm the actual LAN/VPN bind and absence of public routing before NPM
  or dashboard deployment. Keep real domain, endpoint and credentials only
  in private runtime configuration.

## Progress log

| Date | Result | Next |
| --- | --- | --- |
| 2026-09-27 | Consolidated dashboard decisions and implementation boundary in the new fork; no runtime, server, image or credential changed. | Complete post-migration checks, then begin the read-only security vertical slice on a separate branch. |
| 2026-09-27 | Added a public, release-verified installation/configuration guide as a whole-stack deliverable, including tested legacy-to-Compose and Compose-to-Compose migration procedures; no guide commands or runtime behavior changed. | Write and rehearse each supported guide path when the dashboard and installation flow stabilize. |
| 2026-09-27 | Moved the dashboard plan, design, API contract, backlog/tests and static prototype into `docs/dashboard/`; owner is now Claude Code. No runtime, server, image or credential changed. | Persistent job state for the release-update controller, then D0–D2. |
| 2026-09-27 | D0: contract in `src/dashboard/contract/` (dependency-free strict schema language, 25 endpoints with request/response schemas, authorization matrix, HMAC public IDs, examples), synthetic install and canaries in `src/dashboard/fixtures/`, threat model T1–T20. 25 contract tests. No runtime change. | D1/D2: host boundary and login. |
| 2026-09-27 | D1: host boundary over a Unix socket (HTTP framing, 64 KiB bodies, 15 s timeouts, `no-store`), read-only projections, per-install public-ID key in `data/dashboard/`, off unless `NANOCLAW_DASHBOARD_ADMIN_SOCKET` is set. 17 new tests (canaries, tampering, framing, key file). | D2: dashboard service and login. |
