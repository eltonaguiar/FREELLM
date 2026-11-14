# Free-Tier Hygiene — `FreeTierRouter`

> **Status: EXPERIMENTAL · opt-in · default OFF.**
> This feature changes nothing unless explicitly enabled.

## What it does

`tools/litellm_free_tier_router.py` defines `FreeTierRouter`, a subclass of
`litellm.Router`. When enabled it adds three free-tier-hygiene behaviours on top
of the stock router. When disabled, every override simply delegates to the stock
`Router` — behaviour is identical to stock LiteLLM.

### 1. Daily quota-budget pre-filter
Before a provider is used, its per-provider daily budget counter is checked. If
the provider is over-budget, routing proceeds to the next fallback deployment
*without burning quota* on an exhausted provider.

### 2. Provider-scope circuit breaker
If a call to a provider returns `401`/`403`, the whole provider scope is
"parked" for a cooldown (default `60s`). While parked, that provider is skipped.
This prevents hammering a broken/unauthorized provider.

### 3. Empty-200-as-failure failover
For deployments tagged `model_info.free_tier: true`, an HTTP `200` whose payload
is empty (blank content, or reasoning-only) is treated as a failure, and routing
falls through to the next candidate instead of surfacing a blank answer.
Streaming requests (`stream=True`) skip this check.

## Enabling

The deployment entry point is the launcher `tools/start_litellm_proxy.sh`.
Enable with either of:

```bash
bash tools/start_litellm_proxy.sh --background --free-tier-hygiene
LITELLM_FREE_TIER_HYGIENE=1 bash tools/start_litellm_proxy.sh -b
```

How it wires in (why there is no config `custom_router` key):

- The pinned LiteLLM version's proxy builds its router internally via
  `litellm.Router.from_config` and exposes **no** `custom_router` config hook.
- When enabled, the launcher prepends `tools/free_tier_bootstrap` to
  `PYTHONPATH`. Python auto-imports that directory's `sitecustomize.py`, which —
  only because `LITELLM_FREE_TIER_HYGIENE` is set — swaps `litellm.Router` for
  `FreeTierRouter` before the proxy imports it.
- Because the proxy constructs the router through `Router.from_config` (which
  bypasses `__init__`), the router lazily initializes its hygiene state on first
  use from the environment flags.
- When disabled, the bootstrap directory is never on the path, nothing is
  patched, and the stock `litellm.Router` is used unchanged.

### Confirming on vs off
The launcher prints a clear line at startup:

```
[litellm-proxy] free-tier hygiene: ENABLED (experimental; FreeTierRouter)   # on
[litellm-proxy] free-tier hygiene: OFF (default; stock router)               # off
```

## Tuning (optional)

Only read when the feature is on:

| Env var | Meaning | Default |
| --- | --- | --- |
| `LITELLM_FREE_TIER_HYGIENE` | Master switch (`1`/`true`/`yes`/`on`) | off |
| `LITELLM_FREE_TIER_BUDGET` | JSON dict `{"<scope>": {"daily_limit": N}}` | none |
| `LITELLM_FREE_TIER_COOLDOWN_SECONDS` | `401`/`403` park duration | `60` |

Per-model opt-in: set `model_info.free_tier: true` on a deployment (in the
**original** `litellm_config.yaml` `model_list`, or in the config the operator
edits) for the empty-200 failover to apply to that deployment. Quota and
breaker apply to every provider once the feature is on.

`litellm_config.yaml` contains an inert documented `litellm_settings.free_tier_hygiene`
block. It is never read by the proxy (there is no native `custom_router` hook);
it exists so the wiring is discoverable in the config and to make the
default-off guarantee explicit. The launcher flag/env var is the real switch.

## Tests

```bash
# from the repo root, with the litellm venv active
python -m pytest tests/ -q
```

- `tests/test_free_tier_router.py` — 18 unit tests (engine, quota, breaker,
  empty-200 failover, env flags, disabled-identical-to-stock).
- `tests/test_free_tier_hygiene_integration.py` — integration-style tests:
  disabled router matches stock; the actual `from_config` proxy construction
  path initializes hygiene when the flag is on and stays stock when off; the
  launcher script is syntactically valid and wires the flag; the config block
  is inert and loads through LiteLLM's own `ConfigYAML` model.

## Non-breaking guarantees

- Default startup is unchanged: the feature is opt-in.
- No `model_list` entry in `litellm_config.yaml` is modified; only a new inert
  block is appended under `litellm_settings`.
- With hygiene off, the router delegates to stock and creates no hygiene state.
- The sitecustomize bootstrap only exists on `PYTHONPATH` when the flag is on.
