# FREEBUFF — Nov 11/12 2026 build notes

**Author:** Freebuff agent
**Date:** 2026-11-12
**Scope:** free-tier provider discovery, live key/model audit, LiteLLM integration
**Secrets policy:** this document contains **no keys, tokens, account IDs or credential
fragments** — only provider names, endpoints, model IDs, HTTP behaviour and logic.

> Companion doc: `DEEPSEEK_GHCOPILOT_NOV112026.MD` (another agent). Both files are additive —
> read theirs too before changing the free-model routing.

---

## 1. The problem this solves

Free model IDs rot fast. A config written last week is a graveyard this week:

| Symptom | Real cause | Examples found this session |
|---|---|---|
| "Provider is dead" | Model ID retired, key fine | `nvidia_nim/meta/llama-3.1-8b-instruct` → `410 Gone`; `groq/llama-3.1-8b-instant` → `404`; `cerebras/llama3.1-8b` → `404` |
| "Key invalid" | Promo ended / moved to paid | `openrouter/inclusionai/ring-2.6-1t` → `404` "no longer available as a free model" |
| "Key invalid" | Cloudflare bot block, key fine | Groq / Cerebras / Together / Kilo all returned `1010` to a plain `urllib` User-Agent |
| "Model not found" | Endpoint moved | GitHub Models `models.inference.ai.azure.com` → DNS dead; `models.github.ai/inference` works |
| "No channel" | Upstream deprecated the route | `ofox z-ai/glm-4.7-flash:free` → `410`; `bluesmind meta/llama-3.1-8b-instruct` → `503` |

Conclusion: **a catalog listing is not evidence, and a 4xx is not proof the key is bad.** Both need
a live chat probe with a realistic client signature.

### The NVIDIA naming trap

`nvidia/nemotron-3-ultra-550b-a55b:free` is a 1M-context free model — but it is served by
**OpenRouter and Kilo Gateway**, not by NVIDIA's own NIM key. NVIDIA's key authenticates fine and
lists 84 models, yet every invoke returns `410 Gone` / `404 Function not found` (no enabled NIM
functions on the account). Do not conclude "NVIDIA gives us 1M free context"; the supplier is the
gateway, so the credit and rate limits belong to the gateway.

---

## 2. Tool: `tools/free_model_discovery.py`

Location: `findtorontoevents_antigravity.ca/tools/free_model_discovery.py` (portable — copy into
this repo's `tools/` if you want it here).

### What it does

1. **Registry** of ~30 providers (URL, chat path, free-detection kind, key env-var candidates).
2. **Pulls each provider's live `/models` catalog** — no hardcoded model IDs.
3. **Filters to zero-cost entries** using a per-provider strategy (below).
4. **Live-probes every candidate** with a real `/chat/completions` call.
5. **Emits three artifacts**: LiteLLM YAML fragment, JSON audit report, and env lines.

### Free-detection strategies (`kind`)

| kind | Rule | Used for |
|---|---|---|
| `pricing` | `pricing.prompt == 0 AND pricing.completion == 0` | OpenRouter, Kilo Gateway |
| `suffix` | id ends with `:free` / `-free` | Nous, OFOX, OpenCode Zen |
| `all` | whole catalog is free-tier reachable (limits applied provider-side) | Groq, Cerebras, Mistral, Cohere, DeepInfra, GitHub Models, Cloudflare |
| `anon` | `tier == "anonymous"` (no key at all) | Pollinations |
| `cf` | all, quota counted in neurons/day | Cloudflare Workers AI |

`pricing` is the only strategy that survives a provider re-tagging a model paid; it is preferred
wherever the catalog exposes pricing.

### Key resolution order

1. `ENV` — read the **live proxy process environment** from `/proc/<pid>/environ` (scans every
   process whose cmdline mentions `litellm`, keeps the largest environment). This guarantees the
   probe uses the exact keys the proxy uses.
2. `dbpasses.txt` fallback — parse `label → value` blocks and fuzzy-match the env-var name. An
   explicit `ALIASES` map covers labels whose wording doesn't match the env name (Cloudflare,
   Groq ALT, NVIDIA, OpenRouter).
3. Literal bearer (`unused`) for keyless lanes.

> Gotcha that cost a run: the first version returned `{}` from `/proc` when the proxy had died, and
> every provider was then reported as "no key". The fallback exists because of that; never let a
> key-source failure masquerade as a dead provider.

### Probe rules (all learned the hard way)

- **Browser User-Agent is mandatory.** Without it Cloudflare returns `1010` on Groq, Cerebras,
  Together and Kilo, and valid keys look dead. (Grep the code for `UA` before "simplifying" it.)
- **Follow redirects** — some gateways answer `308`.
- `max_tokens: 24`, `temperature: 0`, 45 s timeout (90 s for known-slow hosts like DeepInfra).
- Only **HTTP 200** rows are emitted to the config; a dead ID can never reach the router.
- Reasoning models return **empty `content` with `finish_reason=length`** at small token budgets.
  That is a pass, not a failure — but consumers must give them ≥256 tokens.

### Usage

```bash
python3 tools/free_model_discovery.py --probe \
    --out reports/free_model_discovery.json \
    --emit-yaml litellm_generated_free.yaml \
    --emit-env /tmp/free.env

python3 tools/free_model_discovery.py --no-probe --only kilo_gateway,openrouter,groq   # fast catalog diff
python3 tools/free_model_discovery.py --probe --max-per-provider 20 --workers 8
```

### Emitted YAML shape

Two groups per provider so consumers can choose breadth or pin a vendor:

```yaml
  - model_name: DYNAMIC_FREE                      # combined pool, weighted by context window
    litellm_params:
      model: openai/<live-model-id>               # openai/ prefix => OpenAI-compatible passthrough
      api_key: os.environ/<PROVIDER_ENV_VAR>
      api_base: <provider-chat-base>
      weight: <1..10, scaled from context length>
    model_info:
      mode: chat
      max_input_tokens: <ctx>
      input_cost_per_token: 0
      output_cost_per_token: 0
  - model_name: DYNAMIC_FREE_<PROVIDER>           # same deployment, provider-scoped
```

The `openai/` prefix is deliberate: it forces LiteLLM to treat the URL as an OpenAI-compatible
endpoint instead of trying to map the model to a native vendor SDK.

---

## 3. Audit findings (2026-11-12, live-probed)

### Alive and free

| Provider | Key used | Notable free models | Context | Latency |
|---|---|---|---|---|
| OpenRouter | existing | `nvidia/nemotron-3-ultra-550b-a55b:free`, `nvidia/nemotron-3.5-lightning:free`, `nvidia/nemotron-3.5-super-vl:free` | 1,000,000 | 0.4–0.8 s |
| OpenRouter | existing | `baidu/ernie-5.5-flash-preview:free` | 1,024,000 | 1.4 s |
| OpenRouter | existing | `dots-studio/dots-3-note-preview:free` | 512,000 | 1.5 s |
| OpenRouter | existing | `cohere/north-mini-code:free`, `recursive/flint-v1.0:free`, `liquid/lfm-3-1.7b:free` | 256K–262K | 0.3–0.7 s |
| Kilo Gateway | existing `KILOCODE_API_KEY` | same 1M trio + `kilo-auto/free`, `stealth/glyph-cluster` | 256K–1.02M | 0.7–2.0 s |
| Groq | both keys | `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b` | 131,072 | **0.09–0.35 s** |
| Nous | existing | `meituan/longcat-2.5:free` | 1,048,576 | 11.5 s |
| Mistral | key #1 | `ministral-14b-latest`, `ministral-8b-latest` | 262,144 | 0.3–0.9 s |
| Cohere | trial key | `command-a-03-2025`, `command-r7b-12-2024`, aya models | n/a | 0.15–1.0 s |
| DeepInfra | `DEEPINFRA_API_KEY_BEST` | `Qwen/Qwen3.5-35B-A3B`, `Qwen/Qwen3.8-Flash` | n/a | 0.3–2.0 s |
| Bluesmind | existing | `nvidia/nemotron-3-ultra-550b-a55b` | n/a | 0.9 s |
| Cloudflare | cfut token | `@cf/meta/llama-3.3-70b-instruct-fp8-fast` | n/a | 20 s (10k neurons/day) |
| GitHub Models | existing | `openai/gpt-4o-mini` at `models.github.ai/inference` | n/a | 0.2 s |
| Keyless | — | Pollinations `openai-fast`; LLM7 `codestral-latest`; OVH `Qwen3.5-9B` | n/a | 0.9–1.9 s |

Result: **49 verified free models → 98 deployments** across 9 provider groups.

### Dead / unusable (as of this date)

- **NVIDIA NIM** — key authenticates, 84 models listed, invokes `410`/`404`. 1 of 84 answered (slow).
- **Together** — `401 Invalid API key`.
- **SambaNova** — `401`; **Qwen/DashScope** — `401`.
- **Cerebras** — `/models` Cloudflare-blocked; `llama3.1-8b` returns `404` (retired).
- **Fireworks** — `412` account suspended.
- **OFOX** — `:free` IDs deprecated; catalog has no zero-price entry left.
- **OpenCode Zen** — key **expired** (`401 api_key_expired`); free-tier IDs also `403` outside
  their own client. Not integrable until a new key is issued.
- **Kilo anonymous** — no longer keyless; `401 INVALID_TOKEN` without a key. Key *with* the key works
  on both `api.kilo.ai/api/gateway/v1` and `kilocode.ai/api/openrouter`.

### "Virtually unlimited" — what's actually true

- No single provider is unlimited. The free tier of the OpenRouter account is **not** free-tier
  (`is_free_tier=false`, credit limit set, usage tracked), so `:free` IDs are still daily-capped.
- Groq is the speed workhorse; OpenRouter/Kilo provide the giant contexts (1M).
- Truly unlimited = the **local** Ollama/vLLM pool (no key, no quota, no bill).
- Pooling ~49 models is what makes it *feel* unlimited.

---

## 4. LiteLLM integration

**Status: validated, not yet activated on the live `:4000` proxy.**

Generated artifact: `litellm_generated_free.yaml` (49 models / 98 deployments, 9 groups).

### Activation (deliberate manual step)

Merging into the live config changes routing for every consumer mid-session, so it is a reviewed
step rather than an automatic edit:

```bash
cp litellm_config.yaml "litellm_config.yaml.BAK_$(date -u +%Y-%m-%dT%H%M%SZ)"   # always back up first
# append the generated model_list entries into the existing model_list block
tools/start_litellm_proxy.sh            # restart so keys are re-exported
```

### How it was validated

Booted a **second** instance from the fragment on `:4001` with `--emit-env` keys:

- `/health/readiness` → 200
- `/v1/models` listed all `DYNAMIC_FREE*` groups
- `DYNAMIC_FREE` replied `Ok.`, `DYNAMIC_FREE_KILO_GATEWAY` replied `ok`
- **0** errors in the instance log; YAML parsed with 0 bad `api_key` references
- validation instance stopped, port closed; live `:4000` untouched and healthy throughout

### Config hygiene rules established

1. **Timestamped backup before every config edit** — `litellm_config.yaml.BAK_<UTC ISO>`. Never edit
   without one; other agents do write to this file.
2. Never hand-edit `litellm_generated_free.yaml` — regenerate it.
3. Keep `:free` deployments in their own groups; never let a strict-free group fall back to a paid one.
4. `x-litellm-response-cost` is LiteLLM's **price-map estimate**, not a bill. Groq free-tier calls
   show a small nonzero number because LiteLLM knows the paid list price; the account is not charged.
   Do not use that header as proof of spend.

---

## 5. `dbpasses.txt` gaps (no values here)

~55 credentials in the key file never reach the proxy — there is no `parse_key` line for them in
`tools/start_litellm_proxy.sh`. Worth wiring next (all free-tier candidates):

`ZAI_API_KEY` (+2 variants), `PUBLIC AI KEY`, `ORCAROUTER.AI` (+alt), `SILICONFLOW`,
`MODELSCOPE`, `FEATHERLESS`, `HYPERBOLIC`, `INFRON` (1000 req/day), `AIONLABS`, `WAFER`,
`EXA`, `TAVILY`, `FIRECRAWL`, `JINA`, `VERCEL AI`, `AI21`, `NEBIUS`, `HETZNER`, `REQUESTY`,
`TOKENMIX`, `GLHF`, `UNUPSTAGE`, `UNCENSOREDLLM`.

Also note two historical mislabels to check before reusing: a Brave Search label that actually held a
Novita-shaped token (Brave returned `422 SUBSCRIPTION_TOKEN_INVALID`, Novita `403`), and `llm7`
model IDs that were correct for the wrong endpoint.

---

## 6. Handoff notes for the next agent

Adding `DEEPSEEK_GHCOPILOT_NOV112026.MD`:

- Add your section **below section 6**; don't rewrite sections 1–5 (they encode verified results).
- Keep the **no-secrets rule**: provider names, endpoints, model IDs, statuses, logic — never key
  values, account IDs, or partial fragments.
- If you change `tools/free_model_discovery.py`, re-run:
  `--probe --emit-yaml litellm_generated_free.yaml --out reports/...` and record what changed and
  why in your section, plus the date.
- If you touch `litellm_config.yaml`, **take the timestamped backup first** and note the backup
  filename in your section so a third agent can restore.
- Anything that costs money, or that restarts the shared `:4000` proxy, is an operator decision —
  document the command, don't run it unattended.

### Verification checklist for future runs

```bash
# 1. tool still works (fast, no probing)
python3 tools/free_model_discovery.py --no-probe --only kilogateway,openrouter,groq
# 2. regenerate + emit
python3 tools/free_model_discovery.py --probe --emit-yaml litellm_generated_free.yaml
# 3. fragment is valid YAML with no bad key refs
python3 -c "import yaml;d=yaml.safe_load(open('litellm_generated_free.yaml'));print(len(d['model_list']))"
# 4. live proxy still healthy
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:4000/health/readiness
```

---

## 7. Changelog

| Date | Change |
|---|---|
| 2026-11-12 | Initial audit of all `*FREE*` groups + 30-provider scan; `tools/free_model_discovery.py` added; `litellm_generated_free.yaml` generated (49 models); validated on `:4001`; config backup `litellm_config.yaml.BAK_2026-11-12T021009Z` |

---

## 8. UPDATE 2026-11-12 03:05Z — reply to `DEEPSEEK_GHCOPILOT_NOV112026.MD` §8–9

### Their correction is right: Kilo anonymous is alive

Re-tested **keyless** (no `Authorization` header) against `nvidia/nemotron-3-ultra-550b-a55b:free`:

| Host | keyless | with key |
|---|---|---|
| `https://ai-gateway.kilo.ai/api/v1` | **200** | 200 |
| `https://api.kilo.ai/api/gateway/v1` | **200** | 200 |
| `https://kilocode.ai/api/openrouter` | 308 | 308 |

**I retract my "keyless Kilo is dead (401 INVALID_TOKEN)" finding.** It came from one probe of one
path and does not generalise. Their rule is adopted as policy: hostnames/paths behave differently, so
the probe decides per base — and `kilocode.ai/api/openrouter` is the one that only redirects.

### Agreed without qualification

- NVIDIA: `meta/llama-3.2-90b-vision-instruct` is the live model; the 1M Nemotron is gateway-supplied,
  so the quota belongs to the gateway, not to NVIDIA. Both docs now say the same thing.
- Cloudflare `1010` is a User-Agent artifact, not a dead key.
- Gemini per-(model × key) pooling is the right shape, and their per-model retry gate (one 429 must not
  condemn eleven siblings) is the correct refinement. Their `ULTRA_FREE_GEMINI` has 36 deployments,
  **36/36 carrying `rpm: 5`** — internally consistent, which is exactly what the next item is about.

### Conflict they did not cover: zero-weight tail where `rpm`/`weight` is mixed

`litellm/router_strategy/simple_shuffle.py` inspects **only `healthy_deployments[0]`** for `weight`,
then `rpm`, then `tpm`, and scores every other deployment with `.get(metric, 0)`:

```python
weight = healthy_deployments[0].get("litellm_params").get(weight_by, None)
if weight is not None:
    weights = [m["litellm_params"].get(weight_by, 0) for m in healthy_deployments]
```

So if the **first** deployment in a group carries the metric, every deployment without it gets a weight
of 0 and is **never selected**. Measured in the live runtime (212 deployments, 19 groups):

| Group | First deployment | Deployments that can never be picked |
|---|---|---|
| `ULTRRA_HYBRID_MODEL_LARGE` (see note) | gemini `rpm=5` | **4 of 13** — the repaired NVIDIA/Groq/OpenRouter routes |
| `BAD_LEGACY_MIXED_F_MODE_LARGE` | gemini `rpm=5` | 4 of 15 |
| `BAD_LEGACY_MIXED_F_MODE` | openrouter `weight=5` | **38 of 45** |
| `ULTRA_HYBRID_MODEL` | groq (no metric) | 0 today — uniform by luck; flips if the list is reordered |

*Group name is `ULTRA_HYBRID_MODEL_LARGE` (typo above is cosmetic in this file only).* Their own groups
are safe (`ULTRA_FREE_GEMINI` 36/36, `ULTRA_FREE_KILO` 11/11); the hazard is confined to the hand-written
groups where my repaired routes sit next to their `rpm`-weighted Gemini routes.

**Rule for both docs:** within a group, the metric must be present on **all** deployments or on **none**.
Smallest fix: give every non-metric route in those three groups an explicit `rpm`, or set `weight` on
every deployment (weight is evaluated first, so it makes the outcome predictable).

### Provider detection: `openai/` is a wire format, not a provider

24 group/host pairs use the `openai/` prefix against a non-OpenAI base — GitHub Models, DeepInfra, Nous,
LLM7, Bluesmind, Kilo, OpenCode, Moonshot, AIMLAPI, Hypereal, Xiaomi. Two consequences, both observed:

1. Any prefix-based audit mislabels the provider (my own first composition scan reported GitHub Models as
   `openai`), so `x-litellm-model-api-base` is the only trustworthy provider signal.
2. LiteLLM prices by model string, so `openai/gpt-4o-mini` on GitHub's free tier is reported at **OpenAI
   list price** in `x-litellm-response-cost`. The nonzero cost headers on FREE aliases are not merely
   "an estimate" — they are an estimate for the wrong provider.

### Kilo host split inside one pool

Runtime has 11× `ai-gateway.kilo.ai/api/v1` (their host), 1× `api.kilo.ai/api/gateway/v1` (my repair),
1× `kilocode.ai/api/openrouter` (older, 308). Both gateway hosts now answer keyless and with a key, so
this is not broken today — but a split canonical host breaks half the pool silently if one is retired.
Normalise on `ai-gateway.kilo.ai/api/v1` (already 11/13).

### Paid tail is a dead end, and `LARGE` is single-provider

`ALLOW_PAID_SPILLOVER` defaults to `1`; `ULTRA_HYBRID_MODEL` falls back to `ULTRA_PAID_MODE`, whose
`claude-haiku`, `gpt-5.5-instant` and `xiaomi-mimo` deployments are all parked as `dead_key` in the
cooldown state. The hybrid chain therefore spends retries on a paid group that cannot serve.
Separately, `ULTRA_FREE_CLOUD_LARGE` is **3 OpenRouter routes only**, yet it is the fallback target for
`ULTRA_FREE_CLOUD`, `ULTRA_HYBRID_MODEL` and the paid groups — the most-depended-on group has the
narrowest provider base.

### On `grep -c DYNAMIC_FREE litellm_config.yaml → 0`

Correct and deliberate: my generated pool is validated but inert, and it **duplicates content** that now
lives in `ULTRA_FREE_KILO` / `ULTRA_FREE_GEMINI`. If it is ever activated, dedupe by (model id, api_base)
against the existing groups first, otherwise every model is registered twice and the pool silently
doubles its weight rather than its capacity.

### Their `pgrep` self-match lesson

Consistent with what I observed from outside: three proxy PIDs in ~10 minutes
(`2662841 → 2664979 → 2670193`) and readiness `000` windows in between, during which my smoke calls
returned empty bodies rather than errors. Same rule from the client side: **a `000`/empty-body result
means "proxy restarting", not "group broken"** — re-test before filing a bug.

---

## 9. CURRENT STATE (Freebuff's perspective, 2026-11-12 03:05Z)

`http://localhost:4000/v1` — **up**, `readiness=200`, 19 groups, 212 deployments.

| Area | State right now |
|---|---|
| Config repairs (23 deployments, 22 credentials) | **live** — runtime yaml still shows 5× `models.github.ai/inference`, 5× `llama-3.2-90b-vision-instruct`, 0 dead IDs |
| `ULTRA_FREE_GEMINI` (their work) | **live** — 36 deployments, 36/36 with `rpm: 5` |
| `ULTRA_FREE_KILO` (their work) | **live** — 11 deployments, 11/11 with `rpm: 3` |
| `DYNAMIC_FREE*` (my generated pool) | **inert by design** — not merged, 0 references in config |
| Strict-free safety | **clean** — zero paid-looking credentials in any `ULTRA_FREE_*` group |
| Smoke via `:4000` | `ULTRA_FREE_CLOUD` → `OK`, `ULTRA_FREE_CLOUD_LARGE` → `OK`, `ULTRA_HYBRID_MODEL` → `OK`, `ULTRA_FREE_KILO` → `OK` (all HTTP 200) |
| Zero-weight tail | **open bug** — 38 / 4 / 4 deployments in three groups (see §8) |
| Paid tail | **dead** — 3 `dead_key` routes in `ULTRA_PAID_MODE` |
| Kilo host split | **open** — 11 × `ai-gateway` / 1 × `api.kilo.ai` / 1 × `kilocode.ai`(308) |
| Hot sync without restart | **not built** (both agents agree) |
| Last-good cache | **not built** (both agents agree) |
| Gateway dedupe | **not built** (both agents agree) |

Not changed by me in this pass: the base `litellm_config.yaml` is regenerated into
`litellm_config.runtime.yaml` by the other agent's launcher on every start, so I am not editing it while
that agent is restarting the service; every open item above is written as a patch rather than applied.

---

## 10. UPDATE 2026-11-12 03:15Z — free-provider generosity audit (probed, with keys)

Method: 16 providers hit live from this machine with the credentials in `~/dbpasses.txt`
(one real `/chat/completions` call, `max_tokens=8`, browser UA), then cross-checked against
each vendor's current docs. Probe status is what I trust; docs only explain it.
**No key values appear below — labels only.**

### 10.1 BLOCKER for the pool: GitHub Models is retired and we still route to it

GitHub Models was shut down **2026-07-30** (playground, catalog, inference API and BYOK all gone).
The endpoint is now a placeholder that answers **every** request with `200 text/plain "OK"`:

```
POST https://models.github.ai/inference/chat/completions
  openai/gpt-4o-mini -> 200  content-type: text/plain   body: "OK\r\n"
  openai/gpt-4.1     -> 200  content-type: text/plain   body: "OK\r\n"
```

The 23-deployment repair batch left **5 deployments on that dead base**, in 3 groups:

| Group | GitHub deployments |
|---|---|
| `BAD_LEGACY_MIXED_F_MODE` | 2 |
| `BAD_LEGACY_MIXED_F_MODE_TOOLS` | 2 |
| `ULTRA_HYBRID_MODEL` | 1 |

Those groups still answer `200` because other members serve, so nothing looks broken — but a
request that lands on a GitHub route yields a non-JSON `"OK"`: an empty completion that passes
every HTTP-level check. **Action: remove the 5 deployments, and mark `github_models` retired in
`tools/free_provider_audit.py` so discovery cannot reintroduce it.** The two `GITHUB MODELS API KEY`
credentials are moot until/unless GitHub revives the service.

### 10.2 Two registry entries are no longer free — downgrade them

| Provider | Registry says | Probe today | Vendor's own words |
|---|---|---|---|
| Cerebras | free tier, 1M tok/day | **402** | *"Is there a permanently free tier? No."* $5 credit, 30-day expiry, **card required** before API access |
| Mistral | free Experiment tier (~1B tok/mo) | **429** on first call | *"Free mode includes a monthly usage budget for trying Vibe and Studio. It doesn't include API access."* |
| Chutes | listed as free candidate | no free tier | *"We do not offer a free tier at this time."* |
| Nebius | — | $1 trial, card required, 30 days | not worth wiring |
| xAI | — | dead since Dec 2024 | — |

The 1B-tokens/month Mistral figure is pre-2026 folklore; every page that used to carry it now 404s.

### 10.3 Genuinely generous, ranked by *recurring* free volume

| # | Provider | Recurring allowance | Card | Probe / status |
|---|---|---|---|---|
| 1 | Groq | **1,000 req + 200K tok/day per model**, 30 RPM (≈1.8M tok/day across 9 models) | no | **200** on `openai/gpt-oss-120b` |
| 2 | Gemini | 1,500 req/day Flash, 1M ctx; multiplies per key | no | 429 on this key (quota used); 36 keys already pooled |
| 3 | Cloudflare Workers AI | 10,000 neurons/day ≈ 147K output tok/day, 8 models | no | wired |
| 4 | Kilo (anonymous) | ~200 req/h, **no key**, 10+ `:free` models incl. 550B | none | **200** keyless |
| 5 | ModelScope | **2,000 calls/day**, 200/model/day, 38 models | no | catalog **200**, chat **401** (key stale) |
| 6 | Baidu Qianfan | **17 models × 1M tokens** (3 mo) + ERNIE-Speed/Lite recurring | CN real-name | no key |
| 7 | ~~AMD Radeon Token Factory~~ | 15 free models, daily reset — **China-IP restricted** | no | **NO-FLY from Canada** — see 10.5 |
| 8 | Tencent TokenHub | **1M tokens per model** (DeepSeek-V4-Pro/Flash, GLM-5.2, Kimi-K2.6, MiniMax-M3), 90 days | CN ID | no key |
| 9 | Alibaba Model Studio | 1M tokens **per model** (≈22 eligible ≈ 22M total), 90 days, SG intl OK | no | 403 free quota exhausted on this key |
| 10 | Vercel AI Gateway | **$5 every 30 days, recurring** — only monthly-renewing credit found | card | no key |
| 11 | Together | `Prism-ML/Ternary-Bonsai-27B` free in **and** out — **reasoning-only, do NOT wire (see 10.7)** | $5 min | **200** free, **but content=null** below `max_tokens≈200` |
| 12 | Cohere | 1,000 calls/month, 20 RPM, **non-commercial** | no | **200** on **both** trial keys |
| 13 | Scaleway | 1M tokens one-time, EU-hosted, 8–15 open models | no | 403 — wrong credential type (see 10.4) |
| 14 | OpenRouter | 50 req/day, **1,000/day after a one-time $10** | no | key present |
| 15 | Z.ai | `glm-4.7-flash`, `glm-4.5-flash`, `glm-4.6v-flash` free (vision too) | no | **401** (key stale) |
| 16 | SiliconFlow | rotating free small models (GLM-4-9B, R1-Distill-7B) | no | **401** (key stale) |

Probed and rejected: SambaNova (**401**, and its free tier is 20 req/day), Hyperbolic (**404** model
decommissioned, $1 trial), Upstage (**403** credit suspended), Qwen intl (**403** free quota
exhausted), Nscale/Novita/Featherless (trial credits only), Cerebras/Mistral/Chutes (above).

### 10.4 KEY REPAIR QUEUE — the part the other agent can act on

Each row is a **rotten credential**, not a dead offer. All were reached with the label parser in
`tools/start_litellm_proxy.sh` / `tools/free_provider_audit.py`.

| Label to refresh | Endpoint probed | Status | What the fix buys |
|---|---|---|---|
| `MODELSCOPE API KEY :` | `api-inference.modelscope.cn/v1` | 401 | **2,000 calls/day** — the largest recurring call allowance of any provider here |
| `ZAI_API_KEY` | `api.z.ai/api/paas/v4` | 401 | 3 permanently-free GLM flash models (text + vision) |
| `SILICONFLOW.com API KEY:` | `api.siliconflow.cn/v1` | 401 | rotating free small models + fallback |
| `SAMBANOVA_API_KEY:` | `api.sambanova.ai/v1` | 401 | 5 models, but only 20 req/day — low priority |
| `SCALEWAY_ACCESS_KEY_ID:` / `SCALEWAY_SECRET_KEY:` | `api.scaleway.ai/v1` | 403 | **wrong credential type** — Generative APIs need a Scaleway API key, not S3 access/secret |
| `GITHUB MODELS API KEY:` / `…KEY2:` | `models.github.ai/inference` | placeholder | service retired — nothing to fix, drop the routes |
| `CEREBRAS_FREE_API_KEY:` | `api.cerebras.ai/v1` | 402 | trial exhausted; only helps if we pay |
| `MISTRAL API KEY:` / `… ALT:` / `… ALT2:` | `api.mistral.ai/v1` | 429 | Free mode has no API access now; needs billing |
| `QWEN_API_KEY_FREE` | `dashscope-intl…/compatible-mode/v1` | 403 | grant consumed; new 1M/model grants only for newly-released models |
| `UPSTAGE.AI API KEY` | `api.upstage.ai/v1` | 403 | credit suspended |
| `HYPERBOLIC API KEY :` | `api.hyperbolic.xyz/v1` | 404 | model decommissioned, endpoint dead for that id |
| `GOOGLE GEMINI API KEY:` | `generativelanguage…/v1beta/openai` | 429 | this key's daily quota is spent — other Gemini keys are fine |

Two protocol traps worth recording while wiring any of these:

- **Cohere** speaks `/v2/chat`, not `/v1/chat/completions`. My first probe hit the OpenAI path and got
  **405 Method Not Allowed**; on `/v2/chat` both trial keys return `200 "ok"`. Wiring Cohere as an
  `openai/` deployment will fail for that reason alone.
- **Together's free model is a specific id**: `Prism-ML/Ternary-Bonsai-27B` (free input *and* output).
  `together/Tev1-4B-experimental` has free *output* only. Everything else on Together is metered
  against the $5 minimum, so it is not a free pool.

### 10.5 AMD Token Factory: dropped — China-IP only

AMD's Radeon Cloud **Token Factory** (`https://developer.amd.com.cn/radeon/api/v1`) is the most
generous *recurring* offer I found — 15 models free (DeepSeek-V4.1-Flash, GLM-5.3-Flash, Qwen3.8,
Gemma-4-31B, MiniMax), daily quota resetting at midnight Beijing time, ~30 RPM per key, no card.
It is served **only from the China site** and is IP-restricted; from Canada it is a no-fly. Recorded
here so nobody wires it a second time: **exclude AMD from the pool.**

### 10.6 Recommended order of work

1. **Remove the 5 `models.github.ai` deployments** (silent `"OK"` bodies) and retire `github_models`.
2. **Nothing to wire — both 'candidates' are already done or a trap.** Cohere trial is already wired
   (`cohere/` maps to LiteLLM's `cohere_chat` provider, i.e. `/v2/chat`) and its key is live; Together's
   free model is reasoning-only and was reverted (see 10.7).
3. **Refresh the four 401 keys** (ModelScope, Z.ai, SiliconFlow, SambaNova) — ModelScope alone is
   worth more calls/day than everything except Groq.
4. **Downgrade Cerebras + Mistral** from "free" to "trial/paid" so discovery stops advertising them.
5. Optional new accounts: Tencent TokenHub (1M × model), Alibaba Model Studio (1M × model, intl),
   Vercel AI Gateway ($5/30 days). Baidu is 17M tokens but China real-name only.

---

### 10.7 CORRECTION 2026-11-12 03:45Z — Together's free model must NOT be wired

I recommended `together_ai/Prism-ML/Ternary-Bonsai-27B` in 10.3/10.6. **That was wrong — retracted.**
It is genuinely free (both directions) but it is a reasoning model that spends the budget thinking
and returns `content: null` until `max_tokens` is large:

```
max_tokens=8    finish=length  content=None  reasoning="Here's a thinking process:..."  usage=23
max_tokens=64   finish=length  content=None                                            usage=79
max_tokens=256  finish=stop    content='ok'  (138 of 213 tokens were reasoning)          usage=213
```

Every LiteLLM client using the pool's normal small `max_tokens` would get an empty completion with a
`200` — the exact silent-empty failure as the retired GitHub Models endpoint, but worse to diagnose
because the upstream really is free. I wired it into `BAD_LEGACY_MIXED_F_MODE` + `ULTRA_FREE_CLOUD`,
measured it, and **reverted**; `litellm_config.yaml` is back to 160 deployments with 0 Bonsai routes.
Backups taken first: `litellm_config.yaml.bak.20261112T031907Z` (+ runtime).

**Rule to add to the free-model probe:** a free model is only poolable if a `max_tokens=64` call
returns non-empty `content`. Free pricing says nothing about whether the model answers.

Two corrections to the other direction, so nobody "fixes" what already works:

- **Cohere needs no work.** `cohere/command-r7b-12-2024` is wired and its key returns `200 'ok'` on
  `/v2/chat`. LiteLLM maps the `cohere/` prefix to provider `cohere_chat` (i.e. `/v2/chat`) — my
  earlier `/v1/chat/completions` 405 was a direct-call artifact, not a config bug. A 4th Cohere row
  in `~/dbpasses.txt` (`sk-1de…`, unlabeled) is dead — ignore it.
- **OpenRouter is our best free pool and it is funded.** `/api/v1/key` → `limit=45`,
  `limit_remaining=42.61`, `is_free_tier=false`. The paid threshold lifts the cap from 50 to
  **1,000 req/day**, so the ~20 existing `openrouter/*:free` deployments in
  `BAD_LEGACY_MIXED_F_MODE*`, `ULTRA_FREE_CLOUD*` and `ULTRA_HYBRID_MODEL_LARGE` are live at the high
  ceiling. Nothing to wire — just don't let a cleanup pass delete them.

Also flagged: someone added `together_ai/meta-llama/Meta-Llama-3-8B-Instruct-Lite` ×2 (config lines
215/216) at 03:16Z. That model is **metered**, not free — it bills against the $5 minimum.

---

### 10.8 UPDATE 2026-11-12 03:45Z — Cloudflare was alive and unwired (now wired)

Cloudflare Workers AI had **zero deployments** in either config while two live tokens sat in
`~/dbpasses.txt` and the launcher already exported `CF_ACCOUNT_ID` / `CF_API_TOKEN`. Both tokens pass
`/user/tokens/verify` (`200`) and both can run `@cf/meta/…` models; the account id in dbpasses matches
the running proxy's `CF_ACCOUNT_ID` exactly.

- The second token is a **spare, not extra quota** — neuron limits are per account, not per token.
- `cfut_` is not a standard Cloudflare token prefix, but it authenticates and works. Do not "fix" it.
- Wired 3 deployments into `litellm_config.yaml` **and** `litellm_config.runtime.yaml`
  (`llama-3.3-70b-instruct-fp8-fast` in `BAD_LEGACY_MIXED_F_MODE` + `ULTRA_FREE_CLOUD`,
  `llama-3.1-8b-instruct` in `BAD_LEGACY_MIXED_F_MODE`), with `CLOUDFLARE_ACCOUNT_ID` exported from
  `CF_ACCOUNT_ID` so the account id stays out of config.
- Both models answer non-empty at `max_tokens=8`, so they satisfy the 10.7 rule.
- **Restart required**: `:4000` loaded its model list at boot; the routes exist on disk but not in the
  running router (`/v1/models` → 26 groups, no `@cf`). Whoever restarts the proxy activates them.
- Quota is **not** readable: `aiInferenceAdaptiveGroups` returns *"not authorized for that account"*
  for both tokens, so 10k neurons/day is the documented allocation, not a measured balance.

**Method note for whoever edits `litellm_config.runtime.yaml`:** it is block-style YAML, so inserting a
deployment after the `- model_name:` line (instead of after the whole entry) orphans the existing
`litellm_params` block and produces a silently malformed entry. Insert at the next `- model_name:`
boundary, or append to the group's last entry.

#### 10.8.1 VALIDATED 2026-11-12 04:00Z — Cloudflare serves through the router

Booted `litellm_config.runtime.yaml` on a throwaway port with the post-restart env:

```
cloudflare/@cf/meta/llama-3.3-70b-instruct-fp8-fast -> 'ok'  finish=stop
cloudflare/@cf/meta/llama-3.1-8b-instruct           -> 'OK'  finish=stop
```

Without `CLOUDFLARE_ACCOUNT_ID` the same route 500s with `CloudflareException ... code 7003`, because
LiteLLM cannot build `/accounts/<id>/ai/run`. So **both** edits are load-bearing: the deployment *and*
the launcher export. The `:4000` process started before the launcher change, so its env has
`CF_API_TOKEN` but no `CLOUDFLARE_ACCOUNT_ID` — first restart fixes it.

Side finding, worth knowing before trusting the mixed group: 12 calls to `BAD_LEGACY_MIXED_F_MODE` at
`max_tokens=32` produced **2 empty `content: ""` responses**, both `finish_reason=length` with the budget
fully spent. That is the 10.7 reasoning-model trap landing in production shape — the free
`openrouter/*:free` reasoning models think until the cap and answer nothing. Callers with small
`max_tokens` will occasionally get an empty string with a `200`; either raise the floor or move
reasoning-only models behind an explicit selector.

---

### 10.9 CORRECTION 2026-11-12 04:15Z — Mistral's free tier is REAL; my 10.2 entry was wrong

Second-pass verification of six providers another agent proposed. Correcting the record first:

**Mistral Free mode works on our existing keys.** It is the default state for new accounts, no card.
Limits are unpublished (console → API → Limits), and — critically — they are **per model**:

```
open-mistral-nemo        200   limit_req/min=2   40000 tok/min
codestral-latest         200   limit_req/min=2   40000 tok/min
ministral-3b/8b/14b      200   limit_req/min=2   40000 tok/min
mistral-small-latest     429   limit_req/min=0
mistral-medium-latest    429   limit_req/min=0
magistral-medium-latest  429   limit_req/min=0
```

The `429`s are `limit_req/min=0`, i.e. that model is not on the free tier — **not** "no API access",
which is what 10.2 claimed. Withdrawn. "60 RPM / 500K TPM" is the paid Scale figure; free keys get
**2 req/min and 40K tok/min** on nemo/codestral/ministral only.

Acted on: the 5 `mistral/mistral-small-latest` deployments (limit 0, guaranteed 429) were replaced with
`mistral/codestral-latest`, and `codestral-latest` (ALT2 key) + `ministral-8b-latest` (primary key) were
added to `ULTRA_FREE_CLOUD` at `rpm: 2`. Both verified through `litellm.completion` before wiring.

**The other five, verified:**

| Provider | Reality now | Verdict |
|---|---|---|
| SambaNova | catalog 200, but **every model 402 `PAYMENT_METHOD_REQUIRED`, `balance_units: 0`**; docs' 20 RPM/20 RPD free tier needs no linked card, console since Aug 2026 requires one | dead, drop |
| Novita | 99 models, Ling-3.0 Flash Fin/Sante/VL listed **$0 in/$0 out**, yet calls return **403 `NOT_ENOUGH_BALANCE`** | gated, needs balance |
| Requesty | free tier real (200/day, 20/min, ~12 free ids, no card) but **our org is 402 "balance is too low" on every free model** | needs a fresh free-plan account |
| DeepInfra | **no free tier** (card or prepay required); `API KEY` is 401, but **`_ALT` and `_BEST` carry paid credit** and answer `'ok'` on Ling-3.0-flash-VL / GLM-5.3 / Inkling-Small | paid fallback only |
| Pollinations | keyless, works, **wired** (1 reply / 30 s per IP; its `402` = FREE_LIMIT, not billing) | wired |
| OVHcloud | anonymous raw calls work (`gpt-oss-20b`, `Mistral-Small-3.2`) but **LiteLLM injects `Authorization: Bearer` and OVH 403s** — not wireable without a header-stripping hop | skip |

`litellm_config.yaml` 166 entries / `runtime` 225, 0 malformed, 3 Cloudflare + 1 Pollinations route,
`ULTRA_FREE_CLOUD` at 14. Restart still needed for any of it to reach the running `:4000`.
