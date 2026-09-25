---
name: add-infomaniak-mail-readonly
description: Add read-only Infomaniak mailbox access, including bodies and safe attachment downloads, without exposing the IMAP device password to NanoClaw agent containers.
---

# Add Infomaniak Mail (read only)

Install a small host-side broker that connects to Infomaniak using IMAP over
TLS and exposes five read-only MCP tools to one selected NanoClaw group:

- `infomaniak_list_messages`
- `infomaniak_search_messages`
- `infomaniak_get_message`
- `infomaniak_list_attachments`
- `infomaniak_download_attachment`

The broker always selects `INBOX` with `readonly=True` and uses `BODY.PEEK`, so
reading does not mark messages as seen. It contains no SMTP client and exposes
no send, delete, move, flag, or mailbox-management operation.

Infomaniak device-mail passwords can authenticate both IMAP and SMTP. Keep that
password outside the agent container: the broker reads it from a host-only
`0600` file, while the container receives only a random bearer capability for
the local read-only broker. The broker binds to the Docker bridge rather than
the LAN interface.

## Pre-flight

Select exactly one agent group and record its ID and folder:

```bash
ncl groups list
```

Discover the Docker bridge address locally without printing it. A fresh broker
defaults to loopback; to reach it from the agent container, pass the bridge
address explicitly when configuring it:

```bash
BROKER_BIND="$(ip -4 -o addr show dev docker0 | awk '{split($4, address, "/"); print address[1]; exit}')"
test -n "$BROKER_BIND"
```

If Docker uses a different bridge, identify its address on the server and set
`BROKER_BIND` locally. Never commit that value or paste it into a build log.

Create a dedicated password for this mailbox/device in Infomaniak Mail. Do not
use the Infomaniak Manager account password, do not paste the password in chat,
and do not place it in `.env`, MCP configuration, or Git.

## Coexistence with Outlook

Do not remove an existing `outlook_readonly` registration. This integration
uses the distinct name `infomaniak_mail_readonly`, so both can remain installed
and be enabled independently. A failed or incomplete Outlook authorization does
not prevent the Infomaniak broker from working.

## Install the host broker

From the NanoClaw project root, run the installer carried by this skill:

```bash
python3 "${CLAUDE_SKILL_DIR}/install_service.py" \
  --project-root "$PWD" \
  --group-folder <group-folder>
```

It copies executable code to
`~/.local/lib/nanoclaw/infomaniak-mail-readonly/`, creates the selected group's
attachment directory, and writes a hardened systemd user unit. It does not
collect or copy credentials.

Configure credentials interactively on the host. The password prompt uses
`getpass`, so the value is not echoed or stored in shell history:

```bash
python3 ~/.local/lib/nanoclaw/infomaniak-mail-readonly/broker.py configure \
  --config ~/.config/nanoclaw/infomaniak-mail-readonly.json \
  --download-dir "$PWD/groups/<group-folder>/downloads/infomaniak" \
  --bind "$BROKER_BIND"
```

The configure command verifies an IMAP TLS login before writing anything. It
fixes the server to `mail.infomaniak.com:993`, writes the credential file with
mode `0600`, and generates a separate random broker capability.

Start the broker and verify both host and container reachability:

```bash
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now nanoclaw-infomaniak-mail.service
curl -fsS "http://${BROKER_BIND}:18765/health"
docker run --rm --add-host=host.docker.internal:host-gateway \
  curlimages/curl:8.12.1 -fsS http://host.docker.internal:18765/health
unset BROKER_BIND
```

Both health calls must return `{"ok": true}`. Stop if the service binds to a
LAN address or if the container probe fails.

## Register the MCP endpoint

Load the local bearer header into a temporary shell variable without printing
it, then register the broker for the selected group:

```bash
BROKER_HEADERS=$(python3 \
  ~/.local/lib/nanoclaw/infomaniak-mail-readonly/broker.py client-headers \
  --config ~/.config/nanoclaw/infomaniak-mail-readonly.json)

ncl groups config add-mcp-server \
  --id <group-id> \
  --name infomaniak_mail_readonly \
  --url http://host.docker.internal:18765/mcp \
  --headers "$BROKER_HEADERS"

unset BROKER_HEADERS

ncl groups restart \
  --id <group-id> \
  --message "Infomaniak Mail read-only is installed. List two recent INBOX messages and report whether the tool succeeds."
```

The capability stored in local NanoClaw runtime state can call only the
host-local read-only broker; it is not the IMAP password and is useless away
from this Docker host. Never print or commit it.

## Verify

Run the safety tests before deployment:

```bash
python3 -m unittest \
  "${CLAUDE_SKILL_DIR}/test_infomaniak_mail_broker.py"
```

Ask the selected agent to list unread messages, read one body, list its
attachments, and download a small non-executable attachment. Confirm in
Infomaniak Mail that the message remains unread. Attachments are capped at 10
MiB, saved with mode `0600`, and labeled as untrusted content.

## Upgrade order

Use `/update-nanoclaw`, not a raw pull. Re-run the installer to refresh the
broker code and unit, run the tests, restart
`nanoclaw-infomaniak-mail.service`, and restart the selected group. No upstream
source file or shared agent image is modified.

## Troubleshooting

- Authentication failure: generate a fresh mailbox/device password in
  Infomaniak and rerun `configure`; do not use the Manager login password.
- Broker service failure: inspect
  `journalctl --user -u nanoclaw-infomaniak-mail.service -n 80 --no-pager`.
- An older generated unit that exits with `status=218/CAPABILITIES` inside an
  unprivileged LXC must be regenerated with the current installer and restarted.
- Container health failure: confirm `NANOCLAW_EGRESS_LOCKDOWN=false`, Docker's
  `host-gateway` mapping, and the actual `docker0` address.
- MCP tool absent: inspect the selected group's configuration and restart it.
- Search failure with non-ASCII text: search a narrower ASCII sender or subject
  term; server IMAP search capabilities can vary.

To uninstall the integration or revoke its device password, follow
[REMOVE.md](REMOVE.md).
