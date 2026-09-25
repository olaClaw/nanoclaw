# Compose core preview

`compose.yaml` describes NanoClaw, PostgreSQL, OneCLI, Signal, two read-only
brokers and a TCP-only egress proxy. They are behind the `core-preview` profile so a plain
`docker compose up` does not start a partial stack. It has **not** been deployed
or tested against an operational gateway or calendar. The bootstrap gate is
implemented but not yet exercised on a complete stack. The administrative
panel still needs Compose integration.

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
channels or model endpoint are healthy. The final stack needs service-level
health checks, network isolation and a bootstrap that records the installed
release before the host can start. Although the static gates are implemented,
the complete stack and its recovery path have not been tested. Validate the
file with synthetic values and do not run the preview profile on the
installation.

The `agent-egress` network has the fixed name `nanoclaw-egress` and is internal.
Compose creates it before NanoClaw starts. Agents can resolve the two brokers
there as `infomaniak-mail:18765` and `nextcloud-calendar:18766`. Broker
containers also join `broker-outbound` to reach IMAP and HTTPS CalDAV; neither
MCP port is published on the server. Agent MCP registrations must use those
service names instead of the previous Docker bridge address. Each broker
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

OneCLI and PostgreSQL use separate persistent volumes. PostgreSQL only joins
the internal `db` network; OneCLI joins `db` and `gateway`. The NanoClaw host
joins `gateway` and forces its agent sessions onto a separate internal egress
network. The `onecli-egress` proxy joins that agent network through Compose
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
account. Starting a second daemon with a copied live account would also
be unsafe; use a test identity for the first runtime check.

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

For the future backup procedure, quiesce NanoClaw and OneCLI, keep PostgreSQL
running long enough to take a custom-format database dump, and copy the entire
OneCLI `/app/data` volume in the same maintenance window. Preserve the
encryption key with mode `0600` inside a private backup directory. Include
NanoClaw's `data`, `groups`, `store`, configuration, templates, Signal state,
private secrets and the release manifest in the same recovery set. Restore
into fresh volumes and directories, verify ownership and permissions, start
PostgreSQL and OneCLI at the recorded image digests, then verify an existing
agent through the SDK before starting NanoClaw or Signal. This sequence is a
design backed by the isolated OneCLI test; the complete stack's backup and
restore still need an end-to-end rehearsal with test identities.
