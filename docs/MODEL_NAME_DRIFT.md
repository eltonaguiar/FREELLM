# Model-name drift: why consumers break when the proxy's groups change

**Incident (2026-11-16).** The Antigravity CLI/IDE/App were routed at this proxy with a
38-name `AGY_LLM_GATEWAY_MODELS` list. The proxy on `:4000` was then restarted from a
*different* checkout whose config uses the `free-mode` / `paid-mode` / `hybrid-model`
schema. Result: 18 of the 38 names no longer existed, and every request for them returned
a deterministic `400 Invalid model name passed in model=<X>`. The three surfaces looked
broken; the config was simply out of step.

## The contract this repo should keep

1. **A published name is an API.** Anything documented as served (the `ULTRA_*` family,
   `free-mode*`, `paid-mode*`, `hybrid-model`) should keep resolving across config
   edits. Renaming a group is a breaking change for every saved config that pins it.
2. **When a rename is unavoidable, alias the old name** instead of deleting it:

   ```yaml
   router_settings:
     model_group_alias:
       ULTRA_FREE_CLOUD_LARGE: free-mode-large
       ULTRA_FREE_CLOUD_FAST: free-mode-fast
     fallbacks:
       # an alias group needs its own fallback target, or the router reports
       # "no fallback model group was found for it" and gives up
       - ULTRA_FREE_CLOUD_LARGE: ["free-mode-fast", "free-mode-large"]
   ```

   `model_group_alias` accepts both shapes — `alias: target` and
   `alias: {model: target, hidden: true}` — and is resolved from
   `router_settings` on every proxy request.

3. **Consumers should re-read `/v1/models`** rather than pin a copy. Where a consumer
   must pin (env files, editor settings), reconcile it with the live list after any
   config change.

## Hardening applied here

| Change | Why |
|---|---|
| `retry_policy.AuthenticationErrorRetries: 0` → `1` | With a pool of ~96 keys, one revoked key was enough to fail the whole group instantly: auth errors were never retried, so `simple-shuffle` never got to pick a healthy deployment. One retry lets the router rotate past a dead key. |
| Docs: the alias + fallback contract above | Renames silently broke every pinned consumer; aliasing makes a rename backward-compatible. |

Related: `AGLINUXPATCH` ships `tools/agy_patch_kit.py --action 7`, which reconciles a
pinned consumer list against `/v1/models` (live-tests each name, enforces the CLI's
32-entry cap, rewrites the env and `admin_settings.json`). That is the repair path for a
consumer that pins names; this file is the prevention path for the proxy that serves them.

## Verifying a rename is safe

```bash
# names the proxy will accept right now
curl -s localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  | python3 -c 'import json,sys; print("\n".join(m["id"] for m in json.load(sys.stdin)["data"]))'

# an old name must still resolve after the rename
curl -s localhost:4000/v1/chat/completions -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -d '{"model":"ULTRA_FREE_CLOUD_LARGE","messages":[{"role":"user","content":"ping"}],"max_tokens":4}'
```

A `400 Invalid model name` means the alias is missing; an upstream `401/404/503` under a
resolved name means the *key/model behind it* is dead, which is a pool-health issue
(`tools/verify_all_keys.py`, `tools/free_provider_audit.py`), not a naming one.
