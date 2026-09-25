# Remove Infomaniak Mail read-only access

This procedure is idempotent. Replace `<group-id>` and `<group-folder>` with
the selected group's exact values.

## 1. Remove the MCP registration

```bash
ncl groups config remove-mcp-server \
  --id <group-id> \
  --name infomaniak_mail_readonly
ncl groups restart --id <group-id>
```

## 2. Stop and remove the host broker

```bash
systemctl --user disable --now nanoclaw-infomaniak-mail.service 2>/dev/null || true
rm -f ~/.config/systemd/user/nanoclaw-infomaniak-mail.service
systemctl --user daemon-reload

rm -f ~/.config/nanoclaw/infomaniak-mail-readonly.json
rm -f ~/.local/lib/nanoclaw/infomaniak-mail-readonly/broker.py
rmdir ~/.local/lib/nanoclaw/infomaniak-mail-readonly 2>/dev/null || true
```

The removed configuration contains the mailbox/device password and local
broker capability. File deletion prevents further use from this server.

## 3. Revoke at Infomaniak

Delete the dedicated mailbox/device password in Infomaniak Mail. This is the
authoritative revocation step and invalidates the credential even if a backup
copy exists elsewhere.

Downloaded attachments under
`groups/<group-folder>/downloads/infomaniak/` are ordinary user data. Review and
remove them separately if no longer needed.
