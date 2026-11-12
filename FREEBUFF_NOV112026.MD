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
