# NanoClaw dashboard — functional and API contract, draft v0

Status: D0 contract defined in code; no endpoint implemented. Deployment and
security decisions: [design.md](design.md); threats:
[threat-model.md](threat-model.md).

**Normative source:** `src/dashboard/contract/` (`api.ts` for schemas and
endpoints, `authorization.ts` for the per-endpoint policy, `examples.ts` for
one valid example response per endpoint). The tables below summarize it; if
they disagree, the code and its tests win. D0 settled these points:

- Public IDs (`agt_`, `chn_`, `ses_`, `bkp_` + 32 hex) are HMAC-derived from
  the internal ID with a per-install key; internal IDs never leave the host.
- Every job, including release updates, uses one shape and is read at
  `GET /api/v1/jobs/{job}`; it mirrors the release-update job state.
- Authentication endpoints: `POST`/`GET`/`DELETE /api/v1/session` and
  `POST /api/v1/session/reauth`; `GET /api/v1/health` is the anonymous probe.
- Activity times are rounded to the minute; all strings are bounded and
  printable-only; lists hold at most 200 items with a cursor.
- Export/import payloads stay `draft` until D5 settles the key delivery and
  upload framing.
- The dashboard reaches the host only through a Unix socket in its own
  directory (`NANOCLAW_DASHBOARD_ADMIN_SOCKET`), never a network port.
  Session endpoints and `health` are served by the dashboard itself; the
  host boundary refuses them. Lists accept only `?cursor=`; everything else is
  `invalid_query`.

## Observations from the current code

- `src/cli/socket-server.ts` treats every caller of `data/ncl.sock` as
  `caller: host`. The socket must not be mounted into the web container.
- `ncl status` includes `project_root` and runtime data the browser does not
  need.
- `messaging-groups` rows include `platform_id` (possibly a phone number,
  email or chat ID); `sessions history` contains conversation text.
- `groups config` includes MCP configuration, mounts and other operational
  details. None of these raw responses may become an HTTP response.
- The `ncl` registry already offers resources for groups, sessions, messaging
  groups, wirings and tasks. Reuse the application logic where possible, but
  behind explicit projections and authorization in the new administrative
  boundary.

## API boundary

The browser talks only to the `dashboard` backend over HTTPS through NPM. The
backend talks to a *narrow* host-side administrative interface that exposes
named operations with versioned response schemas. No endpoint accepts an
`ncl` command name, a shell line, an arbitrary path or free-form JSON to be
forwarded to the runtime. The dashboard service does not mount the Docker
socket, `ncl.sock`, the central database, the group directories or secret
files.

Every HTTP response is `Cache-Control: no-store`; public errors carry a
stable code and a random `request_id`, never a stack, path, URL, token or
external ID. Every list has size and pagination limits. Application logs
record only category, outcome and correlation ID, never request bodies,
cookies, query strings or responses. Automated tests with canary values check
that none of this leaks.

## First slice: read-only

| Screen | Proposed endpoint | Allowed fields | Explicitly excluded |
| --- | --- | --- | --- |
| Overview | `GET /api/v1/overview` | current release, summarized host/gateway/broker/channel state, check time | PIDs, paths, URLs/IPs, `.env` configuration, raw exceptions |
| Agents | `GET /api/v1/agents` | opaque internal ID, private label, provider, state, session count | folder/path, prompt, memory, MCP, mounts, credentials |
| Agent detail | `GET /api/v1/agents/{id}` | the above, container state, image release, allowed capabilities | history, environment variables, Docker output, sensitive hashes |
| Channels | `GET /api/v1/channels` | type, opaque instance, connected/disconnected, chat count | tokens, accounts, phone numbers, emails, `platform_id` |
| Provider/model | `GET /api/v1/model-settings` | local/external mode, active provider and model for the install, redacted endpoint state, number of agents affected | full URL/IP, API keys, cookies/OAuth, gateway secrets |
| Sessions | `GET /api/v1/sessions` | opaque internal ID, agent, state, last event (rounded) | texts, thread IDs, sender names/IDs |
| Updates | `GET /api/v1/releases` | installed/verified candidate release, preflight outcome, job state | private registry URLs, credentials, shell output |
| Backups | `GET /api/v1/backups` | opaque ID, date, release, size, verification, transferability | local path, key, raw manifest, personal names/IDs |

Agent labels are private data visible *only* to the authenticated
administrator, never in logs, artifacts or anonymous responses. If the final
privacy plan requires omitting them in the browser too, use local labels
derived from an opaque ID.

Neither `sessions history` nor raw log reading belongs in the first slice.
Later diagnostics use structured events redacted at the source.

## Second slice: named operations

| Operation | Proposed endpoint | Mandatory guard |
| --- | --- | --- |
| Restart agent | `POST /api/v1/agents/{id}/restart` | valid session, CSRF, confirmation, concurrency limit, asynchronous result |
| Enable/disable channel | `POST /api/v1/channels/{id}/state` | re-authentication, state validation, confirmation |
| Change provider/model | `POST /api/v1/model-settings/preflight`, `POST /api/v1/model-settings/apply` | re-authentication, endpoint and model validation, preflight of every agent, restart plan, configuration snapshot and verified global rollback |
| Create/replace/revoke secret | `POST /api/v1/secrets/{kind}` | re-authentication, value input-only, never readable, audit without the value |
| Start update | `POST /api/v1/updates` | re-authentication, verified release, lock shared with the CLI, job ID |
| Job status (updates included) | `GET /api/v1/jobs/{job}` | redacted steps/outcomes only; limited polling |
| Create/verify backup | `POST /api/v1/backups`, `POST /api/v1/backups/{id}/verify` | re-authentication, lock shared with update/import, asynchronous job, verification before offering export |
| Export backup | `POST /api/v1/backups/{id}/export` | re-authentication, encrypted and authenticated bundle, streamed transfer, key outside the bundle |
| Import backup | `POST /api/v1/imports/preflight`, `POST /api/v1/imports/{job_id}/apply` | upload/stage in quarantine, verification, compatibility, target backup and strong confirmation before mutating |

Deleting agents or chats, sending messages and running scripts are **not**
part of the first mutating version. The provider/model change is required,
but it must be a job distinct from free-form `.env` writes: the browser cannot
send arbitrary key names or paths.

In the current code the default provider is applied to new groups, while a
group's provider and model can be changed individually with
`ncl groups config update`; a group model override wins over the default. For
the requested single choice the job must update both the default for new
agents and **every existing group**, including individual settings; changing
only the default is not enough. Before mutating, save a snapshot of the
previous configurations. The preflight lists every agent, checks endpoint,
model, credentials and gateway permissions for each, and presents the stop/
restart plan for sessions. If any agent is not ready, nothing is applied; an
error while applying triggers a rollback of the whole change.
`OPENCODE_BASE_URL` is shared and stays private configuration. No per-agent
selection in the first version.

Implementation (D3a, local path): the preflight answer adds a global `reason`
(the endpoint, model or provider problem, or null) and `available_models`
(the IDs the endpoint lists that fit the model pattern, at most 50). A
preflight is kept in host memory for ten minutes, is single-use and is refused
at apply if the set of agent groups changed since (`preflight_stale`). The
apply job is a host job: `GET /api/v1/jobs/{job}` goes to the operations
service first and to the host when that answers `not_found`. Its phases are
`snapshot`, `write_settings`, `update_agents`, `restart_agents`, `verify`,
`done` (or `rollback`). Operator notes: [../compose-preview.md](../compose-preview.md).

Backup import belongs in the requested release, but only as a controlled
migration job with preflight and recovery, not as an upload that immediately
overwrites state. Never infer web authorization from the mere existence of an
`ncl` command.

## Local backup, export and import between instances

The existing local backup (`scripts/compose-recovery.py`) creates an
encrypted archive and PostgreSQL dump, a separate key and a verifiable
manifest; it stops the writers to get a consistent copy.
`scripts/compose-import-legacy.py` imports a snapshot of a checkout-based
install. **Neither is today a general portable web import/export.** Build a
new versioned contract on top of the verifications already proven, without
exposing the root controller or user-chosen paths to the browser.

Required flow:

1. **Create backup:** job with a persistent lock, space/service preflight,
   consistent writer pause, encryption, integrity check, service restart;
   shows only progress and outcome. The backup stays on private storage.
2. **Export:** from an already verified backup, produce one or more encrypted
   downloadable files with a version/compatibility manifest. For every export
   generate a random high-entropy key, distinct from the login password, shown
   **once** so the administrator saves it in a password manager. The key is
   never included in the package, URLs, logs, web server temporary files or
   the download name. If it is lost there is no read-back: a new export from
   the source instance is needed, if still available. Settle the
   cryptographic format after review; do not improvise a new scheme.
3. **Import:** the browser uploads/transfers the encrypted bundle to a private
   quarantine area with quota, size limits and expiry. Before extracting:
   authenticate manifest and content, check version, release, schema and
   paths/symlinks; no write to live state during the preflight.
4. **Before Apply:** create and verify a backup of the target, show a redacted
   plan of the replacements, require the current password and explicit
   confirmation. The privileged controller stops the writers, applies the
   migration, runs the health gate and keeps the rollback path. If it fails,
   the source is not considered migrated.
5. **Verify:** rehearse on an isolated instance with synthetic data, then
   check compatibility between releases and test recovery. No automatic
   import into an already populated instance without an explicit choice to
   replace it and a backup.

Import modes:

- **Copy/rehearsal** (default): tasks paused and channel identities disabled,
  to avoid double replies or two uses of the same credentials. The data stays
  private; it does not become anonymous because of this.
- **Definitive migration:** identities are activated only after the source
  instance has been stopped and the operator confirms the move. Variables
  specific to the new machine (LLM, networks, hostnames, paths) are
  reconfigured locally, not assumed valid from the bundle.

A portable export covers the data and application configuration NanoClaw
needs for continuity, including OneCLI and channel state where authorized.
NPM/certificate state and the DNS token are infrastructure of the target:
they stay in the single host's disaster-recovery backup but must not be
activated automatically on another instance. Imported credentials are
visible only to the intended services, never in an HTTP response.

For multi-GB archives use streaming with suitable timeouts/limits and
`no-store` downloads; no permanent download URLs. For uploads, enforce quota,
quarantine outside the web root, server-generated names and full validation
before any extraction or mutation.

## Minimal text wireframe

```text
┌─ NanoClaw ─ release/state ─────────────────────── Admin · Sign out ┐
│ Overview │ Agents │ Channels │ Backups │ Updates                   │
├────────────────────────────────────────────────────────────────────┤
│ Host / gateway / broker / channel state                            │
│ Redacted alerts with a diagnostic action, no sensitive values      │
│ Current and verified candidate release                             │
└────────────────────────────────────────────────────────────────────┘
```

Every long operation shows a job ID, phase, progress and outcome. If the panel
goes down during an update, the job continues and the recovery CLI stays
usable. No embedded web terminal.

## Criteria to start coding

1. Migration and recovery work integrated; dashboard branch from a stable
   base.
2. Response schemas and operations approved, including error cases.
3. Security tests designed before the handlers: anonymous access, CSRF, IDOR,
   hostile input, job conflicts, browser cache, privacy canaries.
4. Rehearsal with synthetic data and test identities, never with the
   production agent's data.

## Remaining decisions

- Verify the LAN/VPN bind of the HTTPS port and the absence of public
  forwarding.
- Which channel operations are really needed in the first release; enabling/
  disabling may require a host restart.
- Which channel states/credentials belong in the portable bundle. The
  one-time random key is a confirmed decision.
- Whether the first web interface must support direct download of very large
  archives or should prefer an export to a private directory transferred by
  the operator over SFTP.
- The safe point to restart active sessions during the global switch, showing
  the administrator which ones will be interrupted.
