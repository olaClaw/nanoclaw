# NanoClaw dashboard — threat model (D0)

Status: D0 deliverable. It extends the short table in
[backlog-and-tests.md](backlog-and-tests.md). Every threat names its control
and the test that proves it: the contract tests exist now
(`src/dashboard/contract/contract.test.ts`); later epics add the rest.

## Assets

| Asset | Why it matters | Where it lives |
| --- | --- | --- |
| Conversation content, memory, agent files | Private data of the operator and their contacts | Session DBs, group folders |
| Identities: phone numbers, chat IDs, names | Personal data; enough to identify people | `messaging_groups`, `users`, sessions |
| Channel credentials (Signal account, Telegram token) | Impersonating the assistant on real chats | Private runtime config, Signal state |
| Provider/API credentials, OneCLI vault | Spending, data exfiltration through the model provider | OneCLI, private runtime config |
| Private endpoints and addresses | Network mapping of the operator's LAN | Private runtime config |
| Backups and their keys | A backup plus its key is the whole install | Private backup storage, password manager |
| Admin password hash and sessions | Full control of the dashboard | Dashboard private state |
| Host control (Docker, `ncl.sock`) | Root-equivalent on the Compose host | Host only; never in the dashboard |

## Actors

- **Administrator**: the single legitimate user, on the LAN/VPN.
- **LAN device**: anything else on the same network (compromised IoT, guest).
- **Web attacker**: a site the administrator visits (CSRF, clickjacking).
- **Malicious bundle**: a crafted backup or import file.
- **Compromised dashboard process**: a bug or dependency in the web service.
- **Agent**: an agent container, which must gain nothing from the dashboard.

## Trust boundaries

1. Browser → NPM (TLS, LAN/VPN only) → `dashboard` service.
2. `dashboard` → host administrative boundary: named operations only (D1).
3. Host boundary → runtime: DB layer, Docker, release/backup/import tools.
4. Upload → quarantine → import controller.

The dashboard process is treated as **untrusted by the host**: the host
boundary re-validates every request and every response against the contract,
so a compromised web process cannot turn one endpoint into another or read
fields the contract does not allow.

## Threats, controls and tests

| ID | Threat | Control | Proof |
| --- | --- | --- | --- |
| T1 | Password guessing from the LAN | Argon2id, persistent rate limit (`login` class), uniform errors, audit without passwords | D2 tests: brute force, uniform error |
| T2 | Session theft or fixation | Server-side sessions, rotation at login, `Secure`/`HttpOnly`/`SameSite=Strict`, idle and absolute timeouts, logout and reset revoke all | D2 tests |
| T3 | CSRF / login CSRF | `Origin` check on every non-GET; CSRF token on every non-GET except login; `SameSite=Strict` | Contract: `protects every state-changing request…`; D2 request tests |
| T4 | Anonymous access | Only `health` and `login` are anonymous | Contract: `allows anonymous access only to health and login` |
| T5 | A stolen but idle session changing credentials or state | Re-authentication window for secrets, channels, model switch, updates, backups, restores | Contract: `requires re-authentication…` |
| T6 | Leak of personal or private data in responses | Exact response schemas (no extra fields, bounded strings and lists), host-side validation before sending, field-name review | Contract: schema language, `never names a field after a raw runtime value`, examples leak-free; D1/D3 canary tests on the synthetic install |
| T7 | Leak through identifiers | Public IDs are HMAC-derived with a per-install key; internal IDs (possibly built from chat or folder names in migrated installs) never leave the host | Contract: `public ids` tests |
| T8 | Leak through errors and logs | Errors are a code plus a random request ID; validation errors carry paths, never values; logs record category, outcome and correlation ID only | Contract: error shape, `rejects … without echoing values`; D1 log canary tests |
| T9 | Secrets read back | Secrets, passwords and endpoints are input-only; no response schema contains them | Contract: `keeps secret inputs out of every response` |
| T10 | Web → host escalation | No Docker socket, `ncl.sock`, DB or secret mount in `dashboard`; host boundary exposes named operations with typed inputs; no pass-through of commands, paths or free JSON | D1 tests; Compose config test (no such mounts) |
| T11 | IDOR / path tampering | Path parameters validated against their public-ID pattern, then resolved server-side; unknown IDs give `not_found` | Contract: declared path params; D1 tests |
| T12 | Two concurrent state changes (web + CLI) | Writer-stopping operations share the CLI's `maintenance` lock; the update controller refuses a busy lock and blocks after an interrupted run | Contract: lock set; `compose-release-update.test.py` job-state tests |
| T13 | Malicious backup or import bundle | Quarantine outside the web root, quota and size limits, authenticity before extraction, path/symlink checks, target backup, rehearsal mode by default | D5 tests (tar/zip bomb, traversal, tampering) |
| T14 | Two live channel identities after an import | Rehearsal imports disable identities and pause tasks; migration only with the source stopped | D5 tests; existing import tool tests |
| T15 | SSRF through the model endpoint | Endpoint accepted only in the preflight request, validated against allowed schemes and networks, never fetched by the browser | D3a tests |
| T16 | Remote provider silently receiving all agents' context | Preflight reports `leaves_lan`; explicit confirmation; one global setting, no silent mix | D3a tests |
| T17 | Clickjacking and content injection | `frame-ancestors 'none'`, strict CSP, no inline scripts in the real UI, printable-only strings in every response | D2/D3 header tests; schema `not_printable` |
| T18 | Browser caching of private data | `Cache-Control: no-store` on every API response | D1 tests |
| T19 | Exposure beyond LAN/VPN | NPM publishes 443 on the LAN/VPN interface only; NPM admin never broadly published; DNS-01 without port 80 | D8 checks on the target host |
| T20 | Private data in images, CI artifacts or the repo | Synthetic fixtures with reserved values only; privacy source gate; image audit | Privacy gate in CI; fixtures in `src/dashboard/fixtures/` |

## Out of scope for v1

- Multiple administrators and roles (single account by decision).
- A second factor (decided against for v1; revisit if exposure changes).
- Protection against an attacker with root on the Compose host.

## Residual risks

- The agent label is private and shown to the administrator; it is bounded
  and printable-only but can still contain a personal name chosen by the
  operator.
- The single password is the only factor: its strength and the LAN/VPN
  boundary carry the whole authentication.
