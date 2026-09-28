# Compose core preview

`compose.yaml` describes NanoClaw, PostgreSQL, OneCLI, Signal, two read-only
brokers and a TCP-only egress proxy. They are behind the `core-preview` profile so a plain
`docker compose up` does not start a partial stack. The profile has been exercised
locally with synthetic state through Podman's Docker-compatible API and on a
dedicated Docker LXC with synthetic identities and a local model endpoint.
Real channel and calendar identities have not been migrated. The
administrative panel still needs Compose integration.

The image runs from `/srv/nanoclaw`, matching the absolute path on the Docker
daemon host. This matters because NanoClaw asks that daemon to bind-mount
session files into agent containers. Before a future deployment, create
`/srv/nanoclaw/{data,groups,store,config,templates,signal,signal-outbox}` on that host and give the
runtime user (UID 1000 in the current image) the required ownership. The
Compose file refuses to create missing bind sources. It mounts the local,
ignored `.env` file read-only into the image and mounts the Docker socket only
into the host service. Set `DOCKER_SOCKET_GID` in `.env` to the socket's group
ID; possession of the socket grants Docker daemon control.

`.env.example` is a template, not a working configuration. A release manifest
lists six images by immutable `@sha256:` reference: host, agent, brokers,
OneCLI, PostgreSQL and Signal. After image publication, run
`node scripts/compose-release.mjs create` with `--output` and one option for
each of those six image names, then install the generated file at
`/srv/nanoclaw/release.json`. The generator requires a clean checkout and
records its Git commit, tree and package version. Set the six image entries in
the private `.env` to exactly those manifest references. The host checks all
six local images and requires matching commit/tree labels on the three fork
images before startup; before creating an agent session, its Docker driver
also checks the agent revision label. Local images built from uncommitted
changes are only test artifacts, never release images.
The private GHCR publication procedure and its browser-side activation gate
are documented in [compose-ghcr-release.md](compose-ghcr-release.md).

For a **fresh** data directory, after pulling all six images and preparing
the bind sources, run `docker compose --env-file .env --profile core-preview run
--rm --no-deps nanoclaw node deploy/bootstrap.mjs`. This verifies the manifest
and image labels, then writes `data/upgrade-state.json` with the same commit
and tree. It refuses a nonempty or missing data directory and never overwrites
an existing marker. The host's startup gate rejects a missing or mismatched
marker; in image mode it no longer accepts a version-only fallback. This
bootstrap is not an updater or an import path for existing data.

The image healthcheck connects to the live `data/ncl.sock` Unix socket. It
detects a running CLI listener, but does not establish that the gateway,
channels or model endpoint are healthy. The profile includes service-level
health checks, network isolation and a bootstrap that records the installed
release before the host can start. These checks passed in the synthetic Podman
rehearsal described below and on the dedicated Docker LXC. An encrypted
backup, restore onto fresh volumes and automatic rollback were exercised
there with synthetic state. Do not run the preview profile on a production
installation without a separately reviewed cutover.

The `agent-egress` network has the fixed name `nanoclaw-egress` and is internal.
Compose creates it before NanoClaw starts. Agents can resolve the two brokers
there as `infomaniak-mail:18765` and `nextcloud-calendar:18766`. Broker
containers also join `broker-outbound` to reach IMAP and HTTPS CalDAV; neither
MCP port is published on the server. Agent MCP registrations must use those
service names instead of the previous Docker bridge address. They are plain
HTTP on the internal network, so the host lists them in
`NANOCLAW_MCP_PLAIN_HTTP_HOSTS`; MCP URLs to any other host still need HTTPS. Each broker
requires a bearer capability in the agent group's MCP headers; the capabilities
and upstream passwords stay in private runtime files and must never enter Git.

`NANOCLAW_BROKER_IMAGE` is built only from the three reviewed files in the
broker context. The Infomaniak configuration is the existing broker's `0600`
JSON file, with `bind` set to `0.0.0.0`, `port` to `18765`, and `download_dir`
equal to `INFOMANIAK_DOWNLOAD_DIR`. That download directory must be inside the
selected group's persistent directory, owned by UID 1000 and mounted at the
same absolute host path in the broker. The agent sees the corresponding group
directory at `/workspace/agent`. The broker's IMAP password is
available only inside its container. Its methods use read-only `INBOX` and
`BODY.PEEK` and expose no mail mutation tool.

The Nextcloud configuration is a separate `0600` JSON file owned by UID 1000,
with `calendar_url`, `username`, `app_password` and `broker_token` fields.
`calendar_url` must be an HTTPS calendar collection URL ending in `/`; the
broker verifies TLS and sends only a bounded CalDAV `REPORT` request. Its
single MCP tool, `nextcloud_list_events`, accepts timezone-qualified `start`
and `end` timestamps for a range of at most 93 days. It limits the response
to 50 events, 16 KiB per event and 2 MiB total. The bearer capability controls
access to this read-only broker, but it does not make the upstream Nextcloud
credential read-only. Use an account with the narrowest calendar access
available and keep the credential out of agent containers.

OneCLI and PostgreSQL use separate persistent volumes. NanoClaw's OneCLI SDK
uses OneCLI's web API on port 10254; only the agent-facing proxy forwards to
the gateway on port 10255. PostgreSQL only joins
the internal `db` network; OneCLI joins `db` and `gateway`. The NanoClaw host
joins `gateway` and forces its agent sessions onto a separate internal egress
network. The `onecli-egress` proxy joins that agent network through Compose
as `host.docker.internal`, the hostname configured for the agent's HTTP proxy,
and forwards TCP to OneCLI's gateway port; the agent cannot reach
OneCLI's dashboard through that proxy. The OneCLI dashboard is bound only to
the server's loopback address for this preview. No Postgres or gateway port is
published on the server.

Signal runs on a separate `channels` network shared only with NanoClaw. Its
TCP JSON-RPC port is not published on the server or exposed to agents. The
official image's `--config` path maps to `/srv/nanoclaw/signal`, which NanoClaw
accesses only through a read-only mount of its `attachments/` subdirectory;
the host container does not mount Signal's account keys. Create that
subdirectory before starting Compose. Outbound attachments are
created in `/srv/nanoclaw/signal-outbox` with mode 0600; Signal sees that
directory read-only and NanoClaw removes each file after sending. Both
containers use UID 1000, so both bind sources must be owned by that UID.
`SIGNAL_IMAGE` in `.env.example` selects the official signal-cli 0.14.8 image
by an immutable digest. Its `--version` command was checked locally without an
account. Signal's `/tmp` tmpfs is mounted `exec` (still `nosuid,nodev`)
because signal-cli loads its native libsignal library from there; with
Docker's default `noexec` the daemon exits at startup and restarts in a loop.
A TCP healthcheck on the JSON-RPC port makes such a loop show as `unhealthy`.
Starting a second daemon with a copied live account would also be unsafe; use
a test identity for the first runtime check.

`ONECLI_DB_PASSWORD_FILE` must point to an operator-owned file outside Git
containing exactly 64 hexadecimal characters (for example, the output of
`openssl rand -hex 32`). Compose grants that file only to PostgreSQL and
OneCLI. The latter builds its required `DATABASE_URL` inside the container;
the password does not enter Compose interpolation or image metadata. Never
print the resolved Compose configuration with real secrets or capture it in CI.
OneCLI runs as UID 1000 and must be able to read the mounted file. On the
target Docker host, give the file that UID as owner and mode `0400`, then check
readability with a disposable secret before loading real credentials. A rootless
Podman bind mount may map the host UID differently: a synthetic `0600` file
was not readable in the local test, while a readable test file started the
service. Do not broaden permissions on a real password to work around a UID
mapping mismatch.

The fork pins OneCLI 1.43.3 and `.env.example` selects its official multi-architecture
image by an immutable digest. The upstream [changelog](https://github.com/onecli/onecli/blob/main/CHANGELOG.md)
reports a credential-injection host-enforcement fix in 1.42.0; 1.43.3 also
contains the later shared-host injection fix. The image's entrypoint uses
`tini`, which the Compose password wrapper explicitly starts. These checks
establish the image version and startup contract, not SDK or database migration
compatibility. Verify the gateway against a synthetic database before release.
The example pins the official PostgreSQL 18.6 Alpine image by its immutable
multi-architecture digest. Recheck all three external digests when preparing
the release; pinning prevents silent updates, including security fixes.
Static validation with `.env.example` is not a runtime test.

An integrated local Podman rehearsal used a temporary Compose copy with bind
sources under a private temporary directory, synthetic broker configuration,
an unused model endpoint and no linked Signal account. The three audited fork
images had matching commit/tree labels, and all six images were pinned in a
temporary release manifest. Bootstrap accepted an empty data directory,
created the `0600` marker and rejected a second bootstrap on the same data.
PostgreSQL, OneCLI, both brokers, the egress proxy and NanoClaw reached their
health checks. From the agent network, the proxy and brokers were reachable;
the database, OneCLI dashboard and Signal were not. The installed OneCLI SDK
created and reread a synthetic agent through the host's configured API URL.
A host restart retained the database and marker and passed the release and
socket health checks again. This exercise revealed that the pinned OneCLI
image exposes no healthcheck metadata to Podman, so Compose now declares one
explicitly, and that the SDK needs port 10254 rather than the gateway's 10255.
Podman's Docker-compatible API rejected the host's cleanup filter for the
Docker `dead` state; startup continued, but this filter still needs a target
Docker check. No real message delivery, authenticated CalDAV request, model
inference, agent session or full-stack backup/restore was exercised.

An isolated local runtime test used the pinned OneCLI and PostgreSQL images,
new volumes, an internal network and synthetic credentials. OneCLI 1.43.3
applied its migrations, created 41 public tables and returned healthy
`/v1/health` and gateway `/healthz` responses. The fork's installed
`@onecli-sh/sdk` created a synthetic agent, treated a repeat `ensureAgent`
as already existing and fetched a usable container configuration without
printing its tokens or certificate. A custom-format `pg_dump` restored into a
second database with 41 tables, 82 migration records and two agents. A copy
of `/app/data`, including the encryption key and gateway CA, was mounted into
a second OneCLI container; the same agent and configuration remained usable.
The Compose password-file wrapper was also exercised on that restored state,
with `tini` verified as PID 1. All test containers, volumes, network and
temporary files were removed afterward.

For the operator-run encrypted backup, offline verification and extraction
procedure, see [compose-recovery.md](compose-recovery.md). The dedicated LXC
rehearsal restored the complete synthetic stack onto fresh volumes and then
rolled back automatically with successful agent prompts on both sides. The
backup tool deliberately stops at offline staging; the separate
`compose-rehearsal.py` performs a synthetic-only restore and rollback. A
production cutover and real channel migration still need their own review.

## Administrative dashboard (opt-in profile)

The `dashboard` service is off unless Compose runs with `--profile dashboard`
as well as `core-preview`. It uses the host image with another entry point
(`dist/dashboard/web/main.js`) and UID 61001 (outside the range given to
people), and mounts only two things: its
private state directory `/srv/nanoclaw/dashboard` (administrator credential,
login throttle, audit log) and the `dashboard-admin` named volume that holds
the host's admin socket. It has no Docker socket, `ncl.sock` or NanoClaw data.
The host always serves the socket on that volume; without the dashboard
nothing reaches it. The volume is not part of any backup; the state directory
is, as part of `/srv/nanoclaw`. Design and security model:
[dashboard/plan.md](dashboard/plan.md).

To enable it on a Compose host:

1. Create the state directory: `install -d -m 700 -o 61001 -g 61001 /srv/nanoclaw/dashboard`
   (an install that used the earlier UID 1001: `chown -R 61001:61001 /srv/nanoclaw/dashboard`,
   then recreate the service).
2. Set `NANOCLAW_DASHBOARD_ORIGIN` in the private `.env` to the exact https
   origin of the panel (the service refuses to start without it).
3. Start it: `docker compose --env-file .env --profile core-preview --profile dashboard up -d --wait dashboard`.
4. Create the administrator. On a new install the panel opens on a
   **first-run setup page** instead of the login:
   1. On the server, from the Compose checkout, run
      `docker compose --env-file .env --profile core-preview --profile dashboard exec dashboard node dist/dashboard/web/admin-cli.js setup-code`.
      It prints a one-time code (four groups of four characters) and the
      time it expires, 30 minutes later. Running it again within that time
      prints the same code; after expiry it prints a new one.
   2. In the browser, enter that code and the password twice (at least 12
      characters; store it in a password manager). The code works once: the
      page then logs you in and disappears for good.

   Wrong codes count against the same throttle as wrong passwords. Anyone who
   reaches the page without console access cannot create the administrator.
   Without the browser, the console command `admin-cli.js set-password` (asks
   twice, no echo) sets or replaces the password directly; it also revokes
   every session. The same tool offers `revoke-sessions` and `unlock` (clears
   the login throttle). A lost password is replaced with `set-password`; there
   is no reset by email.

The dashboard itself listens only on the server's loopback address
(`NANOCLAW_DASHBOARD_LOOPBACK_PORT`, default 18080), for API checks through an
SSH tunnel. Browsers use HTTPS through the `proxy` service of the same profile.

### Operations service (backups, updates)

The dashboard's backup and update screens are served by a small root-side
service, `scripts/compose-ops.py`, which runs on the Compose host outside
Docker. It shows the installed and candidate release, update jobs and
backups, and runs backups started from the panel: `compose-recovery.py
backup --apply` under the release-update lock, so a backup and an update never
overlap, whichever side starts them. After a backup the panel shows its key
once; when the operator confirms it is saved in a password manager, the
service shreds it from the server. Backups whose key is still on the server
are flagged in the list. A backup can also be deleted from the panel (archive
and any key left on the server); the last remaining one cannot. Release updates work the same way. Every published release attaches its
digest-pinned manifest to a GitHub release `compose-<revision>`; the hourly
`nanoclaw-ops-candidates` timer (the only operations unit with network access)
fetches the newest ones, keeps those whose revision descends from the
installed one, and fetches that revision into the checkout as its owner. The
panel offers the newest as the candidate; installing it pulls the three fork
images, takes a fresh encrypted backup under the release-update lock and runs
`compose-release-update.py` in production mode, whose checks and automatic
rollback apply unchanged. A candidate that changes the database schema is
flagged, and its confirmation warns that a failed update restores NanoClaw's
data from the backup just taken, losing what arrived during the maintenance
window ([compose-release-update.md](compose-release-update.md)). The dashboard restarts during the update; the
result stays on the Updates screen and the backup's key on the Backups
screen. The service answers on a Unix socket in
`/var/lib/nanoclaw-ops/sock/` that only the dashboard's group (61001) can
open; the dashboard mounts that directory read-only. Without it the screens
say the service is not active and the CLI tools keep working as before.

1. Copy `scripts/compose-ops.py`, `scripts/compose-recovery.py`,
   `scripts/compose-release-update.py`, `scripts/compose-portable.py`,
   `scripts/compose-import-legacy.py` and `scripts/legacy-snapshot.py` from a
   reviewed checkout to `/usr/local/lib/nanoclaw/` (root-owned, `0644`) and
   check their SHA-256 against that checkout.
2. `install -d -m 750 -o root -g 61001 /var/lib/nanoclaw-ops/sock`,
   `install -d -m 700 /var/lib/nanoclaw-ops/candidates /var/lib/nanoclaw-ops/jobs` and
   `install -d -m 700 /var/lib/nanoclaw-ops/portable /var/lib/nanoclaw-ops/portable/{exports,export-keys,imports,work}`
   (`work` must be on the same filesystem as `/srv/nanoclaw`).
   Link `/var/lib/nanoclaw-ops/{project,backups,keys,release-backups}` to the
   Compose checkout, the backup root, the key root and the release-update
   control root.
3. Install `deploy/ops/nanoclaw-ops.service`,
   `deploy/ops/nanoclaw-ops-candidates.service` and
   `deploy/ops/nanoclaw-ops-candidates.timer` in `/etc/systemd/system/`. On a
   test host with synthetic identities put `NANOCLAW_OPS_UPDATE_MODE=synthetic`
   in `/etc/default/nanoclaw-ops` (the default is `production`). Then
   `systemctl daemon-reload && systemctl enable --now nanoclaw-ops nanoclaw-ops-candidates.timer`.
4. Recreate the `dashboard` service so it sees the socket.

### Moving an install: export and import

**Esporta e importa** moves NanoClaw to another Compose host, or makes a
test copy of it. Behind it is `scripts/compose-portable.py`, which also runs
from the terminal.

**Export.** In **Backup**, pick a verified backup and press **Esporta**. If
its key is no longer on the server (it was saved and shredded), type it in.
The operations service turns the backup into one file under a **new random
key**, shown once like a backup key. Save that key in the password manager,
apart from the file.

The file holds everything needed for continuity: data, agent folders, Signal
state, OneCLI with its credentials, and the channel and broker configuration.
It leaves out what belongs to the machine: the proxy (certificates, DNS
token), the dashboard's administrator, the panel's model settings and the
public-ID key.

Take the file with **Scarica** in the browser, or over SFTP from
`/var/lib/nanoclaw-ops/portable/exports/`.

**Import** on the target host (itself installed and running, with its own
backup set up):

1. Upload the file in **Esporta e importa**, or copy it over SFTP into
   `/var/lib/nanoclaw-ops/portable/imports/` (as root, `0600`). It then shows
   up in the list.
2. **Verifica** with the export key and a mode. This checks, without
   touching anything:
   - that the file is authentic;
   - that the target runs the same release or a newer one (older data is
     migrated at start);
   - whether the target is empty or already has agents and chats.
3. **Applica**:
   - the operations service first makes a fresh encrypted backup of the
     target, whose key the panel then offers to save;
   - it then stops the NanoClaw services (not the panel), replaces the state
     and starts them again;
   - if anything fails, it puts the target back from that backup by itself.

   Replacing a target that already has data needs its own confirmation.

Modes:

- **Copia di prova** (default): Signal's account state and the Signal and
  Telegram identities are not copied (the target's own are removed too);
  pending tasks are paused and pending chat is closed. The copy never
  talks to real contacts.
- **Migrazione definitiva**: queues, tasks, Signal state and identities come
  along. Only after the source is stopped for good: two installs with the
  same identities answer twice and can break Signal's sessions. The panel
  asks you to confirm it.

The target keeps its own images, install ID, LLM endpoint, proxy, dashboard
and paths. Only model, timezone and gateway settings (and the identities in
a migration) come from the file.

Files are transferred without size limits through the proxy: the guided
HTTPS setup configures this. Rerun it once on an existing proxy.

From the terminal:
- `compose-portable.py export --backup-dir … --backup-key … --export-root … --key-root …`
- `compose-portable.py import --project-root … --state-root … --bundle … --key-file … --work-root … --mode rehearsal|migration`,
  plus `--target-backup-dir/--target-backup-key` and `--apply` to change
  anything;
- `--rollback-txn` undoes an import.

### Changing the LLM and model from the panel

The **Modello e provider** screen changes the provider and model for
**every** agent at once, and for agents created later; there is no per-agent
choice. The choices are:

- **LLM locale**: OpenCode against an OpenAI-compatible server (vLLM,
  llama.cpp, …) on the LAN or VPN. Details below.
- **Claude** (Claude Agent SDK, Anthropic API key), **OpenAI** (OpenCode,
  API key billed per use) and **ChatGPT** (OpenCode, the subscription
  sign-in). These send the requests and context of **all** agents out of the
  LAN; the panel says so and asks for an explicit acknowledgement.

For an external provider the credential stays in OneCLI. Add it there and
grant it to the agents first, from the OneCLI console or with the
`provider-auth` setup step. The panel never sees it.

**Verifica** then checks every agent group with one read-only request through
OneCLI, exactly as the agent would make it:
- Anthropic and OpenAI: `GET /v1/models`, which also confirms the model is
  offered;
- ChatGPT: an authenticated account read, since there is no model list.

If any agent is not ready, nothing changes, and the panel shows why:
- *manca la credenziale*: the provider answered 401/403;
- *OneCLI non permette*: the gateway refused the agent;
- *OneCLI non risponde*: the gateway is unreachable.

NanoClaw's pinned OneCLI version does not refresh the ChatGPT sign-in by
itself: when it expires, sign in again (see the OpenCode skill), and the
check will say so until then.

For the local LLM:

1. Enter the endpoint (for example `http://LLM_HOST:8000/v1`) and the model ID
   exactly as the server lists it, then **Verifica**. The host checks the
   address (http(s), no credentials or query; it must resolve only to a
   private LAN/VPN address, never loopback, link-local, a single-label service
   name or one of the stack's own networks), lists the server's models (they
   become suggestions for the field) and asks the model for a one-token
   answer. It never follows redirects and sends no credentials, so an
   endpoint that needs an API key is refused for now.
2. The result lists the agents and how many active sessions will restart.
   **Applica a tutti gli agenti** (after the password again) starts the job.

The panel does not edit `.env`. Its choice lives in
`/srv/nanoclaw/data/model-settings.json` (host-owned, `0600`, included in
every backup) and wins over the `.env` values it replaces: the default
provider and model for new groups (`DEFAULT_AGENT_PROVIDER`,
`NANOCLAW_DEFAULT_MODEL`) and, for the OpenCode choices, `OPENCODE_PROVIDER`,
`OPENCODE_BASE_URL` (`native` for OpenAI and ChatGPT), `OPENCODE_AUTH_MODE`,
`OPENCODE_MODEL` and `OPENCODE_SMALL_MODEL` (which follows the main model). When the server reports the model's context window
(vLLM `max_model_len`) it replaces `OPENCODE_MODEL_CONTEXT_LIMIT`, and an
output limit that no longer fits is dropped. Model-specific settings such as
`OPENCODE_MODEL_INPUT_MODALITIES` stay in `.env`: check them when the new
model accepts different inputs. To go back to `.env` alone, stop the host,
remove the file and start it again; the per-group rows keep the model the
panel wrote, so change them with `ncl groups config update` if needed.

The job first writes a journal (`model-settings.journal.json` next to the
settings) with the previous settings and every group's provider and model,
then writes the settings, updates every group row (creating missing ones) and
any session pinned to another provider in one transaction, restarts every
agent and checks the rows and the endpoint again. If a step fails it restores
the journal exactly, restarts the agents again and reports *rolled back*. If
the host stops mid-way, the next start restores the journal before any agent
runs and marks the job *interrupted*. While the journal exists the operations
service refuses backups, backup deletion and updates. If even the rollback
fails the job says *rollback failed* and the journal stays: restarting the
host retries the restore; check the host log (`Model settings rollback`) if
it keeps failing. The job record is `model-settings.job.json` and never holds
the endpoint.

### HTTPS proxy

`proxy` is Nginx Proxy Manager 2.16.0, pinned by digest in `compose.yaml`. It
publishes 443 only on `NANOCLAW_PROXY_BIND`, the server's LAN/VPN address
(default loopback, never `0.0.0.0`), and its admin UI only on loopback
(`NANOCLAW_PROXY_ADMIN_PORT`, default 18081). Port 80 is not published:
certificates are issued with the DNS-01 challenge. Its configuration,
certificates and DNS-provider token live in `/srv/nanoclaw/proxy`, inside the
encrypted backup (the backup accepts the certificate store's own relative
links under `proxy/letsencrypt/`, nothing else).

**Guided setup (recommended).** After step 1 below, run as root on the
Compose host:

```sh
python3 scripts/compose-https-setup.py --project-root /path/to/checkout
```

It asks for the panel's hostname, the LAN/VPN address to publish 443 on, an
email address and the certificate mode, then does everything through the
proxy's API: it writes the two `.env` keys, recreates `dashboard` and
`proxy`, creates the proxy administrator on a fresh proxy (random password,
printed once: store it in a password manager), obtains the certificate,
creates the proxy host and checks the panel over HTTPS. Modes:

- `letsencrypt`: DNS-01 with one of the DNS providers the pinned proxy image
  ships (the script lists them). It asks for the provider's credentials with
  hidden input; they are stored only in the proxy's own database. Create the
  provider token with the least rights that allow DNS changes.
- `local`: for installs without a domain. The script generates a certificate
  for the hostname on the host; the connection is encrypted, but browsers
  warn until the certificate is trusted on each device.
- `http`: **no encryption**, for a trusted LAN/VPN only, when even a local
  certificate is not an option. Prefer `local`. The script shows the risk
  and asks you to type `HTTP` to confirm. It then:
  - stops the proxy;
  - publishes the dashboard directly on the given address
    (`NANOCLAW_DASHBOARD_HTTP_BIND`, port `NANOCLAW_DASHBOARD_LOOPBACK_PORT`,
    default 18080);
  - sets `NANOCLAW_DASHBOARD_INSECURE_HTTP=true` and the matching `http://`
    origin.

  In this mode:
  - passwords, backup keys and data cross the network in clear;
  - the session cookie drops `Secure` and the `__Host-` prefix (browsers
    refuse both on http) but keeps HttpOnly, SameSite=Strict, the Origin and
    CSRF checks;
  - HSTS is not sent;
  - every page shows a red "Connessione non cifrata" banner.

  Running the script again in an https mode switches all of this off. The
  service refuses an http origin without the opt-in, and the opt-in with an
  https origin.

`--check` prints the plan and changes nothing; running it again reuses the
certificate and updates the proxy host. The admin UI is still available on
loopback for anything else. Manual setup:

1. `install -d -m 700 /srv/nanoclaw/proxy /srv/nanoclaw/proxy/data /srv/nanoclaw/proxy/letsencrypt`.
2. Set `NANOCLAW_PROXY_BIND` (and the panel's `NANOCLAW_DASHBOARD_ORIGIN`) in
   the private `.env`, then start it with both profiles as above.
3. Open the admin UI through an SSH tunnel (`ssh -L 18081:127.0.0.1:18081 …`),
   create the administrator, and add a proxy host: the panel's hostname,
   forward to `http://dashboard:8080`, a Let's Encrypt certificate with the
   DNS challenge of your DNS provider (enter its API token only there, with the
   least rights that allow DNS changes), "Force SSL" and HTTP/2 on.
4. Make the hostname resolve to the LAN address for every client that should
   reach the panel (local DNS; remote VPN clients need that DNS too). No
   router forward may point at the published port.
