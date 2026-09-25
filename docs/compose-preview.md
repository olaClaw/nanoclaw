# Compose core preview

`compose.yaml` describes NanoClaw, PostgreSQL, OneCLI, Signal and a TCP-only egress
proxy. They are behind the `core-preview` profile so a plain
`docker compose up` does not start a partial stack. It has **not** been deployed
or tested against an operational gateway. The two read-only brokers,
bootstrap and the administrative panel still need Compose integration.

The image runs from `/srv/nanoclaw`, matching the absolute path on the Docker
daemon host. This matters because NanoClaw asks that daemon to bind-mount
session files into agent containers. Before a future deployment, create
`/srv/nanoclaw/{data,groups,store,config,templates,signal,signal-outbox}` on that host and give the
runtime user (UID 1000 in the current image) the required ownership. The
Compose file refuses to create missing bind sources. It mounts the local,
ignored `.env` file read-only into the image and mounts the Docker socket only
into the host service. Set `DOCKER_SOCKET_GID` in `.env` to the socket's group
ID; possession of the socket grants Docker daemon control.

`.env.example` is a template, not a working configuration. Replace its image
references with images from the same reviewed commit before deployment. The
host image embeds its revision; before creating a new agent session, the Docker
driver checks that the agent image has the same revision label. Images with a
missing or different label are refused. Local images built from uncommitted
changes are only test artifacts, never release images.

The image healthcheck connects to the live `data/ncl.sock` Unix socket. It
detects a running CLI listener, but does not establish that the gateway,
channels or model endpoint are healthy. The final stack needs service-level
health checks, network isolation and a bootstrap that records the installed
release before the host can start. Until those are implemented and tested,
validate the file with synthetic values and do not run the preview profile on
the installation.

OneCLI and PostgreSQL use separate persistent volumes. PostgreSQL only joins
the internal `db` network; OneCLI joins `db` and `gateway`. The NanoClaw host
joins `gateway` and forces its agent sessions onto a separate internal egress
network. Only the `onecli-egress` proxy is attached to that agent network at
spawn, and it forwards TCP to OneCLI's gateway port; the agent cannot reach
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
`SIGNAL_IMAGE` remains an unusable placeholder until a specific image digest
is reviewed. Starting a second daemon with a copied live account would also
be unsafe; use a test identity for the first runtime check.

`ONECLI_DB_PASSWORD_FILE` must point to an operator-owned file outside Git
containing exactly 64 hexadecimal characters (for example, the output of
`openssl rand -hex 32`). Compose grants that file only to PostgreSQL and
OneCLI. The latter builds its required `DATABASE_URL` inside the container;
the password does not enter Compose interpolation or image metadata. Never
print the resolved Compose configuration with real secrets or capture it in CI.

The `ONECLI_IMAGE` example is deliberately unusable. The fork currently pins
OneCLI 1.41.0, while the upstream [changelog](https://github.com/onecli/onecli/blob/main/CHANGELOG.md)
reports a credential-injection host-enforcement fix in 1.42.0. Review a newer
version and its compatibility with this fork before selecting an image for a
deployment. The PostgreSQL image must also be pinned to an immutable digest
for the release. Static validation with `.env.example` is not a runtime test.
