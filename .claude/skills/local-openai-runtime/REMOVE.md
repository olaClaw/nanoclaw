# Remove local OpenAI runtime synchronization

Delete the two skill-owned installed files:

- `scripts/local-openai-sync.ts`
- `scripts/local-openai-sync.test.ts`
- `scripts/local-openai-smoke.ts`
- `scripts/local-openai-smoke.test.ts`

The OpenCode provider is independent; keep it or remove it through
`/add-opencode`'s `REMOVE.md`.

If no other local endpoint needs them, remove the endpoint IP from the NanoClaw
service's `NO_PROXY` and `no_proxy` values and restart the service. Preserve
unrelated proxy exclusions.

Remove `OPENCODE_PROVIDER`, `OPENCODE_MODEL`, `OPENCODE_SMALL_MODEL`, and
`OPENCODE_BASE_URL` from `.env` only if OpenCode will no longer use them.
Preserve unrelated settings and user data.
