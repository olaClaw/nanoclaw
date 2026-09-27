# NanoClaw dashboard — backlog and tests, draft v0

Status: planning. References: [design.md](design.md) and
[api-contract.md](api-contract.md).

## Coordination rules

- Dashboard work happens on a dedicated branch/worktree from a stable
  revision. Any change to the backup/import/update controllers needs a joint
  review and recovery tests; never create a second controller hidden in the
  web service.
- All fixtures use reserved example names and domains, non-reusable fake
  credentials and canary files; no data from the real server.

## Epics and proposed order

| ID | Goal | Verifiable delivery | Depends on |
| --- | --- | --- | --- |
| D0 | Contract and threats | API schemas, authorization, threat model, synthetic fixtures | No runtime change |
| D1 | Narrow host boundary | Typed operations, validated input, safe projections, no `ncl` pass-through | D0 |
| D2 | Administrator login | Local bootstrap, password hash, rate limit, sessions, logout/reset | D0 |
| D8 | HTTPS ingress | NPM pinned image, DNS-01, 443 on LAN/VPN only, NPM admin not public | D0; before real browser access |
| D3 | Read-only panel | Overview, agents, channels, backup/release state with redacted data | D1, D2 |
| D3a | Model configuration | Preflight of local endpoint/remote provider and of every agent, write-only credentials, global apply to existing and future agents, coordinated restart and rollback | D1–D3, provider/gateway ready |
| D4 | Local backup | Asynchronous job, lock, preflight, backup, verification, progress, restart | Stable recovery, D1–D3 |
| D5 | Portable export/import | Encrypted bundle, upload in quarantine, preflight, target backup, clone/migration | D4, stable import controller |
| D6 | Administrative operations | Agent restart and write-only secrets, confirmations, audit | D1–D3 |
| D7 | Updates | Button and CLI on the same job/lock, health gate and recovery | Stable update controller, D4 |
| D9 | Rehearsal and release | End-to-end tests on the test host with synthetic data; privacy audit of repo/images/logs | D0–D8 |

D1/D2 and D8 may overlap, but D5 must not precede the rehearsal of the
underlying recovery. "First complete version" means D0–D9: no `ready` label
while import/export is missing.

## Essential threat model

| Threat | Boundary | Required control |
| --- | --- | --- |
| Password guessing | Browser → dashboard | Persistent rate limit, uniform errors, audit without passwords |
| Session theft/fixation | Browser → dashboard | HTTPS, protected cookie, ID rotation, timeout, revocation, CSRF/Origin |
| Escalation from web to host root | Dashboard → host API | Named endpoints, allowlist, no Docker socket/`ncl.sock`, no arbitrary shell |
| Exfiltration through responses/logs | Host → dashboard/browser | Explicit output schemas, redaction at the source, canary tests |
| Malicious backup | Upload → quarantine → import | Quota, authenticity check, path/symlink checks, no extraction into live state |
| Two live identities | Import → channels/tasks | Inactive clone by default; migration only after the source is stopped |
| Partial state after an error | Controller → services/volumes | Target backup, journal, lock, health gate, tested rollback |
| Lost export key | Operator → other host | Random key shown once, separate from the bundle; new export from the source if available |
| NPM/DNS exposure | Network → proxy | 443 on LAN/VPN only, admin port not public, DNS token outside images |

## Minimum test matrix

| Area | Positive case | Negative/failure case | Required result |
| --- | --- | --- | --- |
| Auth | Correct password, login/logout | Brute force, stolen/revoked cookie, CSRF, reset | No unauthorized mutation |
| Read API | State and lists from fixtures | CLI output containing paths, URLs, phone numbers, tokens, chat text | Canaries absent from HTTP and logs |
| Mutating API | Single agent restart | Missing/hostile ID, repeated request | Validation, deduplication, audit |
| Model/provider | Valid local endpoint, change applied to all existing agents and to the default for new ones | SSRF URL/wrong credential, unauthorized agent, failed restart, provider not installed | Preflight blocks everything if one agent is not ready; no partial change; global rollback; no secret/URL in logs |
| Backup | Verified backup, services restarted | Full disk, failed DB dump, interrupted process | Consistent state, redacted error, recovery possible |
| Export | Encrypted multi-GB download, one-time key saved separately | Browser disconnects, incomplete download, lost key | No plaintext, checksum verification, expiring link; new export if needed |
| Import preflight | Valid compatible bundle | Tampered bundle, wrong version, zip/tar bomb, path traversal, hostile symlink | Live state untouched, upload removed/expired |
| Import apply | Inactive clone and controlled migration | Populated target, source still running, failed health gate | Explicit confirmation, target backup, rollback |
| Updates | Web and CLI see the same job | Two concurrent starts; panel goes down mid-way | A single job, recovery CLI available |
| Proxy | HTTPS access from LAN/VPN | Access from a disallowed network, port 81 exposed | Unexpected connections refused |
| Release privacy | Build with synthetic fixtures only | Canary in context, layer or log | Publish blocked |

## Verification milestones

1. **Design review:** schemas and threats approved, no raw CLI response over
   HTTP; D0–D9 tickets estimated.
2. **Safe vertical slice:** login + `/overview` with synthetic data and tests
   for anonymous access and CSRF; no Docker access in the dashboard container.
3. **Operations and backup:** jobs with persistent state and CLI fallback;
   interruption tests before and after the writers stop.
4. **Portability:** export from instance A, import into B, verify history and
   configuration without activating credentials on B; then a definitive
   cutover rehearsal with test identities.
5. **Release:** audited build, rehearsal on the test host, documented
   backup/import/recovery test; only then add the dashboard to the Compose
   profile.

## Open questions

- Should the browser export download the large bundle directly or prepare it
  in a private directory for SFTP transfer? The first version may offer both
  if the protocol is the same.
- The one-time random portable key is confirmed. Before D5, define and verify
  the UX for copying it into the password manager without putting it in logs,
  URLs, browser storage or the download next to the bundle.
- Scheduled automatic backups are out of the initial scope; confirm whether to
  keep them out after the manual backup lands.

## Backlog added after the first deployment

- **First-password setup code for new installs** (with the installation
  guide; the operator agreed on 2026-09-27): on first start with no
  administrator, the service generates a one-time code shown only in the
  server console/log; a web setup page creates the administrator only with
  that code, which expires on use or after a few minutes. The console command
  stays the recovery path.
- **Guided HTTPS setup for new installs** (requested by the operator on
  2026-09-27): a local console script that asks for the panel hostname, the
  DNS provider and its token (hidden input, never echoed, logged or passed as
  arguments) and configures the proxy through its API: initial administrator
  with a random password shown once, DNS-01 certificate, proxy host to
  `dashboard:8080` with Force SSL, HTTP/2 and HSTS. The proxy admin UI never
  needs to be opened or tunnelled. Rehearsed by hand through the API on the
  test LXC (self-signed certificate); needs the real DNS-01 path, idempotent
  reruns and a dry-run mode. Goes with the installation guide and the
  first-password setup code.
- **Release updates recreate the dashboard** (done): `compose-release-update.py`
  recreates `dashboard` on the new host image when the profile is in use, and
  on the old one during a rollback. `proxy` image bumps still need a manual
  pull and recreate.
