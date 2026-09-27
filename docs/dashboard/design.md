# NanoClaw administrative dashboard — design

Status: design; no implementation yet. Companion documents:
[api-contract.md](api-contract.md) (functional/API contract) and
[backlog-and-tests.md](backlog-and-tests.md) (epics, threat model, tests).
The shared plan with current state and next step is [plan.md](plan.md).

## Confirmed decisions

- Access only from the LAN or a VPN, over HTTPS terminated by Nginx Proxy
  Manager (NPM), running as a service of the Compose stack.
- The panel uses a dedicated subdomain of the operator's existing domain,
  following the operator's other Nginx-fronted services. The real subdomain
  name lives only in private runtime configuration.
- On the Compose host this will be the first NPM; there is no existing proxy
  to share the HTTPS port with. Still verify the target's ports before deploy.
- **Password** authentication, no second factor in the first version.
- A single administrator account in the first version; no account shared with
  other operators.
- Management of agents, channels, configuration, secrets, logs and updates.
- Updated decision (requested by the operator): the panel may change the local
  LLM address and model and select an external provider, keeping the values in
  private runtime configuration only. The earlier "`.env` only" limitation no
  longer applies. The web backend gets no direct access to the file: a typed
  host operation validates, saves atomically, verifies and applies, with a
  rollback path.
- One provider/model configuration is active for the whole install: after
  confirmation it is applied automatically to **all** existing agents and
  becomes the default for new ones. There is no per-agent selection in the
  panel. The code also keeps per-group settings, so the job must update all of
  them, not only the global default, and keep a snapshot of the previous
  settings for rollback.
- Switching to an external provider requires write-only credentials in the
  gateway, a per-agent check of credentials and permissions beforehand, a
  warning that requests and context of all agents may leave the local network,
  and explicit confirmation. If any agent is not ready, the job does not start.
- The local OpenCode address is shared today. The preflight must show the
  whole set of agents, the affected sessions and the restart plan; the change
  must never silently produce a mix of old and new provider/model.
- Updates can be started from both the panel and the terminal through the
  same job, lock and recovery path.
- Backup can be started from the panel; encrypted export and controlled import
  on another instance, with a rehearsal mode that does not activate channel
  identities.
- Every portable export uses a random key separate from the bundle, shown
  once so the operator can save it in a password manager; it is not the
  administrator password and cannot be read back from the panel.
- No personal data, secret, credential, private endpoint or IP address in the
  repository, images, build logs or CI artifacts.

## Security boundaries

- The `dashboard` service does not mount the Docker socket, the NanoClaw
  database, `data/ncl.sock`, broker files or OneCLI secrets. The current CLI
  socket treats every caller as a host operator: it is not suitable as a
  direct web API.
- A narrow administrative interface, separate from the CLI socket, exposes
  only typed operations authorized on the server side. Responses use explicit
  schemas that exclude sensitive fields; no pass-through of database rows,
  CLI output or raw logs.
- Update actions reach the privileged controller through a typed request; the
  panel cannot run arbitrary shell or Docker commands.
- Publish the web port only on the intended LAN/VPN interface; do not assume
  that an internal Docker network or a password replaces TLS and a firewall.
- Only NPM publishes HTTPS towards the LAN/VPN; `dashboard` stays on an
  internal Compose network without a host port. With DNS-01, do not publish
  port 80 just to obtain the certificate. The NPM admin port (81) stays on
  loopback, reached through an SSH tunnel, or on an explicitly protected
  administrative HTTPS surface; never a blanket `81:81`.
- NPM keeps its configuration, accounts, certificates, private keys and DNS
  credentials on private persistent volumes/paths, outside Git, build
  contexts and images. Include this data in the encrypted backup and test its
  restore. For DNS-01 use a provider token with the least privileges the
  automation needs, and never show it in logs or in the NanoClaw panel. The
  certificate must be trusted by the browsers in use: never disable TLS
  verification.
- Pin the NPM image by digest in the release manifest and verify its
  provenance; no `:latest` in production. If a proxy already uses ports
  80/443 on the same host, add the new proxy host there instead of starting a
  second, conflicting one.
- No secret value is ever readable from the panel. Create, replace and revoke
  are separate operations with confirmation and server-side checks.

## Authentication and recovery

- Bootstrap the administrator with an interactive local command, not through
  an open web endpoint. The password never goes through shell arguments,
  `.env`, logs or images. Store only an adaptive hash with a unique salt
  (Argon2id).
- Limit and slow down login attempts; identical errors for wrong credentials.
  Provide a local unlock procedure.
- Revocable server-side sessions, random IDs, idle timeout and maximum
  lifetime. `Secure`, `HttpOnly`, `SameSite=Strict` cookies; CSRF protection
  on every state-changing request, Origin check and security headers. No
  tokens in `localStorage`.
- Password recovery only from the server console by the operator, revoking
  all sessions. No reset by email or security questions in the first version.
- Confirm the current password for secret rotation, channel changes and for
  starting an update or restore. The action log holds only the operation
  type, outcome and a non-sensitive internal identifier.

## Screens and progressive capabilities

1. **Sign-in and overview:** login/logout, current release, service health,
   summarized state of gateway, brokers, channels and LLM; no private URLs.
2. **Agents and channels:** list, redacted details and the allowed operations,
   with confirmation for stop, restart, wiring and deletion.
3. **Configuration and secrets:** a single provider/model/local-endpoint
   choice for all agents, with an impact summary; server-validated forms;
   sensitive credentials input-only, never shown after saving; status and
   last rotation visible without exposing the value.
4. **Logs and diagnostics:** structured events, limited search, redaction at
   the source, volume and retention limits; no raw log download.
5. **Updates and backups:** verified release, preflight, on-demand backup,
   portable export/import, progress, health gate and recovery instructions;
   lock shared with the CLI. Details in [api-contract.md](api-contract.md).

## Work sequence without conflicts

1. Settle the operation contract, authorization model, wireframes and threats
   without changing Compose or recovery code.
2. On a dedicated dashboard branch/worktree from a stable revision: implement
   login, sessions and a read-only API with synthetic data.
3. Add administrative operations one at a time, each with authorization,
   input validation and response-privacy tests.
4. Connect the already tested update controller; test web/CLI concurrency and
   an unavailable panel.
5. Rehearse on the test host before importing real identities or credentials.

## Minimum acceptance criteria

- Abusive login attempts are limited; logout and reset revoke sessions.
- Requests without a valid session or CSRF token cannot change state.
- The dashboard container has no Docker socket, `ncl.sock` or secret files
  and is not reachable outside the LAN/VPN.
- APIs never return secrets, private addresses, chat history or raw logs.
- A simultaneous web and CLI request cannot start two updates.
- Backup/import/update never mutate state concurrently; import requires a
  preflight and a verified backup of the target, never an implicit overwrite.
- The recovery CLI works with the dashboard and the NanoClaw host stopped.
- Privacy gates on source, build contexts, images and artifacts before push.

## Decisions to confirm before code

- The bind of the HTTPS port on the LAN/VPN interface of the Compose host
  (value only in private runtime configuration), and that no router forward
  exposes it to the Internet. Never record the real subdomain in Git or logs.
- Which mutating agent/channel operations belong in the first release besides
  viewing and restart.
