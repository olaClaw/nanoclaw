# Compose host preview

`compose.yaml` currently describes only the NanoClaw host. It is behind the
`core-preview` profile so a plain `docker compose up` does not start a partial
stack. It has **not** been deployed or tested against an operational gateway.
OneCLI/Postgres, the two read-only brokers, Signal, bootstrap and the
administrative panel still need Compose integration.

The image runs from `/srv/nanoclaw`, matching the absolute path on the Docker
daemon host. This matters because NanoClaw asks that daemon to bind-mount
session files into agent containers. Before a future deployment, create
`/srv/nanoclaw/{data,groups,store,config,templates}` on that host and give the
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
