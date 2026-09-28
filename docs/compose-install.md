# Installing NanoClaw with Docker Compose

A step-by-step guide from an empty Linux container to a running install with
the administrative panel over HTTPS, encrypted backups and updates from the
panel. It follows the order in which things depend on each other. The
reference behind each step (security model, file formats, failure modes) is
in [compose-preview.md](compose-preview.md) and the documents it links.

Moving an existing install instead? Use **export/import** between two Compose
installs ([compose-preview.md](compose-preview.md#moving-an-install-export-and-import)),
or the one-off importer for the old checkout-based install
([compose-legacy-import.md](compose-legacy-import.md)).

> **Status.** Steps 1–6 and 8–12 have been rehearsed on a test LXC container
> with synthetic identities, and run in production on an LXC. A VM or physical
> machine differs only in step 1. Step 7 (the first agent and its channels on
> a brand-new install) is the one part not yet rehearsed end to end: follow it
> carefully and report what differs.

This guide is for the Compose install, which the panel, backups and updates
need. The upstream checkout-based install (`nanoclaw.sh`, a systemd user
service, no panel) is described in the [README](../README.md).

Throughout, `192.0.2.10` stands for the container's LAN (or VPN) address and
`panel.example.org` for the panel's name. Never use `0.0.0.0`.

## 1. The machine

NanoClaw runs as a Docker Compose stack on one Linux machine. That machine
can be:

- **a Proxmox LXC container**: unprivileged, with *Nesting* and *keyctl*
  enabled (both are needed for Docker inside it). This is what the tested
  installs use;
- **a virtual machine** (Proxmox, VMware, a cloud VM on a private network) or
  **a physical computer**: nothing special to enable. Docker runs directly on
  its kernel.

Either way:

- Debian 12/13 or Ubuntu 22.04/24.04, with Docker Engine and the Compose
  plugin from Docker's own repository (`docker.com`). The Snap or distribution
  packages keep Docker's files elsewhere and are not tested. Rootless Docker
  and Podman are not supported for a real install, because NanoClaw asks the
  daemon to bind-mount absolute host paths into agent containers. The tested
  Compose version is 5.x.
- At least 4 GB RAM and 40 GB free disk. Backups stay on the same disk by
  default, so plan for about three times the size of `/srv/nanoclaw`.
- The model: a local OpenAI-compatible server on the LAN (vLLM, llama.cpp,
  …), or an account with Anthropic, OpenAI or ChatGPT.
- No port has to be opened to the internet. The panel is reached on the LAN
  or over a VPN such as Tailscale. On a VM or physical machine with a
  firewall, allow only port 443 on the LAN/VPN address (step 9), plus SSH
  for yourself.

**The service account.** The containers run as **UID 1000**, and the account
that owns the checkout must be that same UID: the host container reads the
release manifest the account owns.

- In a fresh LXC container, UID 1000 is usually free. Create the account:
  ```sh
  adduser --uid 1000 --disabled-password --gecos '' nanoclaw
  ```
- On a VM or physical machine, the installer often gave UID 1000 to the first
  person's account (check with `getent passwd 1000`). Prefer a machine used
  only for NanoClaw, where the first account you create at install time is
  `nanoclaw`. If UID 1000 is a person's everyday account, NanoClaw's state
  becomes theirs, so move that person to another UID before continuing, or
  use a dedicated VM instead.

Commands below prefixed with `sudo` run as root (from your own admin account
or a root shell); the rest run as `nanoclaw`.

## 2. The code and a release

Each release is a GitHub release named `compose-<first 8 characters of the
commit>`. Its asset `compose-release.json` pins all six images by digest.
Pick the newest one.

```sh
# as nanoclaw
git clone https://github.com/olaClaw/nanoclaw.git ~/nanoclaw
cd ~/nanoclaw
git switch --detach <the release's full commit>
curl -fsSLo /tmp/compose-release.json \
  https://github.com/olaClaw/nanoclaw/releases/download/compose-<rev8>/compose-release.json
```

The three NanoClaw images are **private** packages on GHCR. From an admin
account, log in
once with a GitHub token that can only read packages (`read:packages`), then
pull the six images from the manifest:

```sh
sudo docker login ghcr.io -u <github-user>        # paste the token when asked
for image in $(python3 -c 'import json;print(" ".join(json.load(open("/tmp/compose-release.json"))["images"].values()))'); do
  sudo docker pull -q "$image"
done
```

The token stays in root's Docker configuration. Never put it in `.env`,
Git or an image.

## 3. State directories and private files

Everything NanoClaw keeps lives in `/srv/nanoclaw`. Compose refuses to create
missing directories, so create them now:

```sh
sudo install -d -m 755 -o 1000 -g 1000 /srv/nanoclaw
sudo install -d -m 700 -o 1000 -g 1000 \
  /srv/nanoclaw/{data,groups,store,config,templates,signal,signal/attachments,signal-outbox,secrets}
sudo install -m 600 -o 1000 -g 1000 /tmp/compose-release.json /srv/nanoclaw/release.json
```

Private files, created under `/srv/nanoclaw/secrets` and outside Git:

- **OneCLI database password**: 64 hexadecimal characters, readable only by
  UID 1000:
  ```sh
  openssl rand -hex 32 | sudo install -m 400 -o 1000 -g 1000 /dev/stdin /srv/nanoclaw/secrets/onecli-db-password
  ```
- **Mail and calendar brokers**: the stack includes a read-only Infomaniak
  mail broker and a read-only Nextcloud calendar broker, each with a JSON
  configuration file (`0600`, owner UID 1000). Their fields are described in
  [compose-preview.md](compose-preview.md). Both files are required.

## 4. The private `.env`

Copy the template and fill it in. The file stays next to `compose.yaml`,
`0600`, owned by `nanoclaw`, and is never committed:

```sh
cp .env.example .env && chmod 600 .env
```

| Key | Value |
| --- | --- |
| `NANOCLAW_HOST_IMAGE`, `NANOCLAW_AGENT_IMAGE`, `NANOCLAW_BROKER_IMAGE`, `ONECLI_IMAGE`, `POSTGRES_IMAGE`, `SIGNAL_IMAGE` | exactly the six references from `release.json` |
| `NANOCLAW_INSTALL_ID` | a short name for this install, lowercase (`home`, `office`) |
| `DOCKER_SOCKET_GID` | the group of `/var/run/docker.sock`: `stat -c %g /var/run/docker.sock` |
| `ONECLI_DB_PASSWORD_FILE` | `/srv/nanoclaw/secrets/onecli-db-password` |
| `INFOMANIAK_BROKER_CONFIG_FILE`, `INFOMANIAK_DOWNLOAD_DIR`, `NEXTCLOUD_BROKER_CONFIG_FILE` | the broker files from step 3 |
| `DEFAULT_AGENT_PROVIDER` | `opencode` for a local LLM (the panel can change it later) |
| `OPENCODE_PROVIDER`, `OPENCODE_BASE_URL`, `OPENCODE_MODEL` | for a local LLM: `openai`, `http://LLM_HOST:8000/v1`, `openai/<model id>` |
| `TZ` | your time zone, e.g. `Europe/Rome` |
| `SIGNAL_ACCOUNT`, `TELEGRAM_BOT_TOKEN` | the channel identities, when you add those channels (step 7) |

The dashboard keys come in step 8.

## 5. First start

Check the configuration without printing it (it would show secrets). Then
record the installed release on the empty data directory, which works
**once**, and start the stack:

```sh
sudo docker compose --env-file .env --profile core-preview config --quiet
sudo docker compose --env-file .env --profile core-preview run --rm --no-deps nanoclaw node deploy/bootstrap.mjs
sudo docker compose --env-file .env --profile core-preview up -d --wait
sudo docker compose --env-file .env --profile core-preview ps
```

All services should be `running` and `healthy`: nanoclaw, postgres, onecli,
onecli-egress, signal-cli and the two brokers. The host refuses to start
when the release, the images and the marker disagree. That is deliberate:
fix the mismatch, never the check.

`ncl`, the admin command line, runs inside the host container. It is used
below as:

```sh
alias ncl='sudo docker compose --env-file .env --profile core-preview exec -T nanoclaw node dist/cli/client.js'
ncl groups list
```

## 6. Credentials for external services (OneCLI)

API keys never go into NanoClaw or `.env`. **OneCLI** holds them and injects
them into the agents' requests. Its console listens only on the container's
loopback address; open it through an SSH tunnel:

```sh
ssh -L 10254:127.0.0.1:10254 nanoclaw@192.0.2.10    # then browse http://127.0.0.1:10254
```

A local LLM needs no key. For Anthropic, OpenAI or ChatGPT, add the key
(or the ChatGPT sign-in) in OneCLI and grant it to the agents. The panel
then checks it for every agent before switching (step 11).

## 7. The first agent and its channels

> Not yet rehearsed end to end on a brand-new install (see the status note).

With `ncl`:

1. Create the agent group:
   ```sh
   ncl groups create --name "Assistant" --folder assistant
   ```
2. Create yourself as a user and make yourself the owner:
   ```sh
   ncl users create --id <channel>:<your handle> --kind <channel> --display-name "Your name"
   ncl roles grant --user-id <channel>:<your handle> --role owner
   ```
3. Register the chat and wire it to the agent:
   ```sh
   ncl messaging-groups create --channel-type <channel> --platform-id <chat id>
   ncl wirings create --messaging-group-id <id from step 3> --agent-group-id <id from step 1>
   ```
4. Channel identities go in `.env` (`TELEGRAM_BOT_TOKEN`; for Signal,
   `SIGNAL_ACCOUNT` plus an account registered or linked in the `signal-cli`
   service). Restart the host after changing them. **Never run two installs
   with the same Signal or Telegram identity**: both answer, and Signal's
   sessions break.

`ncl <resource> help` lists every field. The per-agent settings
(`ncl groups config update`) are described in the main
[CLAUDE.md](../CLAUDE.md).

## 8. The administrative panel

The panel is a separate service (Compose profile `dashboard`) that runs
with its own UID 61001 and no access to NanoClaw's data. Only the host's
admin socket connects the two.

1. Create its private state directory:
   ```sh
   sudo install -d -m 700 -o 61001 -g 61001 /srv/nanoclaw/dashboard
   ```
2. In `.env`, set the exact address the browser will use:
   ```sh
   NANOCLAW_DASHBOARD_ORIGIN=https://panel.example.org
   ```
3. Start it:
   ```sh
   sudo docker compose --env-file .env --profile core-preview --profile dashboard up -d --wait dashboard
   ```

### First administrator: the setup code

The first time, the panel shows a **setup page** instead of the login. It
creates the administrator only with a one-time code that can be read on the
server's console, so whoever reaches the page from the network cannot claim
the panel.

1. On the server, in the checkout, ask for the code:
   ```sh
   sudo docker compose --env-file .env --profile core-preview --profile dashboard \
     exec dashboard node dist/dashboard/web/admin-cli.js setup-code
   ```
   It prints four groups of four characters (for example `7K2Q-M9XD-4TNB-R8WC`)
   and the time it expires, 30 minutes later. Asking again before then prints
   the same code; after expiry you get a new one.
2. Open the panel in the browser (after step 9 at `https://panel.example.org`,
   or through the SSH tunnel on the loopback port while testing). Enter the
   code, dashes and case do not matter, and the password twice: at least 12
   characters. Store it in a password manager.
3. **Crea l'amministratore**. The code is spent, the setup page is gone for
   good, and you are logged in.

Wrong codes count against the same throttle as wrong passwords. Later, from
the console:
- `admin-cli.js set-password` replaces a lost password (asked twice, without
  echo) and logs every session out;
- `admin-cli.js revoke-sessions` logs everyone out;
- `admin-cli.js unlock` clears the login throttle.

There is no password reset by email.

## 9. HTTPS: the guided setup

`scripts/compose-https-setup.py` configures the `proxy` service (Nginx Proxy
Manager) through its API, so its admin UI never needs to be opened. Run it
as root on the server, from the checkout:

```sh
sudo python3 scripts/compose-https-setup.py --project-root ~nanoclaw/nanoclaw
```

It asks, in order:

1. **Mode**:
   - `letsencrypt`, when you own a domain. The certificate is obtained with
     a DNS challenge, so nothing has to be reachable from the internet.
   - `local`, without a domain. A certificate made on the server: the
     connection is encrypted, but each browser warns until you trust the
     certificate on that device.
   - `http`, no encryption at all, for a trusted LAN only. The script
     states the risk and asks you to type `HTTP`, and the panel shows a red
     warning on every page. Prefer `local`.
2. **The panel's name** (`panel.example.org`). For `letsencrypt`, first
   create a DNS record pointing the name at `192.0.2.10`, in your provider's
   DNS or a local DNS. The address is private: it only has to resolve for
   the devices that use the panel, VPN clients included.
3. **The address to publish 443 on**: `192.0.2.10` (the LAN or VPN address,
   never `0.0.0.0`).
4. **An email address** for the proxy's administrator (and Let's Encrypt).
5. For `letsencrypt`: **the DNS provider**, from the list the proxy image
   supports, and **its API token**. The token is typed hidden and stored
   only in the proxy's own database. Create it with the least rights that
   allow DNS changes on that one domain.

The script then:
- writes `NANOCLAW_DASHBOARD_ORIGIN` and `NANOCLAW_PROXY_BIND` in `.env`;
- recreates the panel and the proxy;
- creates the proxy's administrator, with a random password printed
  **once**: store it in the password manager;
- obtains or installs the certificate;
- creates the proxy host with Force SSL, HTTP/2, HSTS and no upload limit
  for export files;
- checks that the panel answers over HTTPS.

Running it again is safe: the certificate and the proxy host are reused and
updated. `--check` shows the plan and changes nothing. The proxy's own admin
UI stays on the server's loopback (`127.0.0.1:18081`, through an SSH tunnel)
for anything else.

## 10. The operations service (backups, updates, export/import)

Backups, updates and exports from the panel are run by a small root service
outside Docker, `scripts/compose-ops.py`. Install it from the same reviewed
checkout, following the numbered steps in
[compose-preview.md](compose-preview.md#operations-service-backups-updates):
- copy the scripts and check their SHA-256;
- create the `/var/lib/nanoclaw-ops` folders and links;
- install the service and the hourly timer that looks for new releases;
- recreate the dashboard.

On a test host with synthetic identities, put
`NANOCLAW_OPS_UPDATE_MODE=synthetic` in `/etc/default/nanoclaw-ops`.

## 11. The model

**Modello e provider** in the panel switches every agent at once. The
choices are a local LLM (address and model, checked with a one-token
answer), Claude, OpenAI or ChatGPT (credentials checked per agent through
OneCLI). The panel keeps its choice in `data/model-settings.json` and never
edits `.env`. A failed switch rolls back by itself. Details:
[compose-preview.md](compose-preview.md#changing-the-llm-and-model-from-the-panel).

## 12. Backups and updates

- **Backup**: in the panel, **Crea backup**. The services stop for a few
  minutes while the copy is made and verified. Then the panel shows the
  backup's **key once**:
  1. save it in the password manager;
  2. tick the box;
  3. confirm, and the key is deleted from the server.

  A backup without its key cannot be restored.
- **Updates**: new releases appear in **Aggiornamenti** within an hour.
  **Installa** takes a fresh backup, switches the release and checks
  channels and agent images. If anything fails, it returns to the previous
  release by itself. When a release changes the database, the panel says
  so: a failed update then also puts back the data from that backup, and
  anything received during the maintenance window is lost.
- The same backup and update tools run from the terminal:
  [compose-recovery.md](compose-recovery.md),
  [compose-release-update.md](compose-release-update.md).

## When something is wrong

- `sudo docker compose --env-file .env --profile core-preview ps`: which
  service is not healthy.
- `sudo docker compose --env-file .env --profile core-preview logs --tail 100 nanoclaw`:
  the host's log. The tools and the panel print fixed status codes, never
  values; the code usually names the cause, e.g. `release_marker_mismatch` or
  `backup_too_old`.
- The panel's **Panoramica** shows the channels and the model's reachability.
- Before anything risky, take a backup and save its key.
