# DEEPSEEK_GHCOPILOT_NOV112026

**Session:** 2026-11-12 · **Author:** GitHub Copilot (DeepSeek V4.1 Flash)
**Scope:** repair + harden the local LiteLLM proxy on `http://localhost:4000/v1`, and make free-provider discovery self-maintaining.

> **No credentials appear in this document.** Every citation is a provider name, an HTTP status, or a masked fingerprint. Keys live only in `~/dbpasses.txt` (chmod 600, outside any repo) and are referenced from config as `os.environ/<NAME>`.

---

## 1. What was actually broken

Three independent defects, all silent — the proxy kept returning 200s while quietly burning retries.

### 1.1 Gemini free-tier quota was cooled for 65 seconds instead of until reset

`~144` `RESOURCE_EXHAUSTED` errors in one day across four Gemini models. The upstream message is explicit:

```
429 RESOURCE_EXHAUSTED
Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_input_token_count
limit: 250000, model: gemini-2.5-flash, description: ... per minute limit exceeded
"You exceeded your current quota, please check your plan and billing details."
```

Two compounding faults:

1. **Classification.** `tools/litellm_smart_cooldown.py` had branches for Cloudflare daily quota and HuggingFace monthly 402, but nothing for Google. Every Gemini 429 fell through to the generic `rate_limit → 300s`.
2. **Enforcement (the real bug).** The module was *observability only*: it wrote `/tmp/litellm_cooldown_state.json` and never applied anything. The router enforced its static `cooldown_time: 65`. So a model whose **daily** allowance was exhausted came back into rotation every 65 seconds and failed again, all day.

Observed free-tier limits on the affected project (Google AI Studio → Rate limits, 1-day window): several models capped at **RPD 20/day**, `gemini-2.5-flash` at 32/20, `gemini-2.5-flash-lite` at 28/20. A 300-second (let alone 65-second) cooldown can never be correct against a per-day limit.

### 1.2 NVIDIA was contributing nothing — every deployment returned HTTP 410

Both NVIDIA NIM keys authenticated fine and listed **84 models**. The config pinned one model:

```
The model 'meta/llama-3.1-8b-instruct' has reached its end of life
on 2026-08-26T09:00:00Z and is no longer available.
```

That model was wired into **5 deployments** (`BAD_LEGACY_MIXED_F_MODE`, `BAD_LEGACY_MIXED_F_MODE_LARGE`, `ULTRA_HYBRID_MODEL`, `ULTRA_HYBRID_MODEL_LARGE`, plus the ALT-key twin). All 5 were dead. NVIDIA is one of the most generous free tiers available; it had been silently disabled since **August 2026**.

A model-list check would **not** have caught this — the catalog still listed the model after inference began returning 410. Only a real completion proves a deployment works.

### 1.3 Working free providers were never wired at all

A sweep of all 46 credentials found **13 providers currently serving inference**, including several with free tiers absent from the config. The most notable misses:

| Provider | Why it mattered |
|---|---|
| **Kilo AI Gateway** | Serves **11 `:free` models**, and the catalog needs **no API key at all** (anonymous ≈200 req/h). Was not in the config. |
| **Qwen / DashScope** | **101 models** usable on the free key. Was invisible because the credential parser read the neighbouring `QWEN_API_URL` line as the key, producing 401. |
| **Nous Research** | 3 `:free` models verified (`meituan/longcat-2.5:free` etc.); the config only used one legacy entry in a `BAD_*` group. |
| **OVHcloud AI Endpoints** | Keyless anonymous tier, 9 free models — undiscovered. |
| **Pollinations** | Keyless anonymous tier (`text.pollinations.ai/openai`) — undiscovered. |

---

## 2. What was built

Four pieces, designed so the same "does it *actually* generate?" gate applies everywhere.

### 2.1 `tools/litellm_smart_cooldown.py` — classification **and** enforcement

**Google-aware classification** (`_classify`):

| Category | Trigger | Cooldown |
|---|---|---|
| `google_daily_quota` | `per day`, `requests per day`, `generate_requests_per_day`, `free_tier_requests`, `current quota … billing` | until **midnight Pacific** (Google's documented free-tier reset) |
| `google_per_min_quota` | `per minute`, `input_token_count`, `generate_content_free_tier_input_token_count` | honours the upstream `Please retry in Ns` hint, clamped 60–900s |
| `google_quota` | other Google 429/quota | until next Pacific midnight (conservative) |
| `daily_quota_cf` | Cloudflare neuron quota | next UTC midnight |
| `monthly_quota_hf` | HF 402 / account-pool | 1st of next month |
| `dead_key` | 401 / invalid key / insufficient credits / balance | 24h |
| `rate_limit` | plain 429 | 300s |
| `server_error` | 5xx | 60s |
| `bad_request` | 400 / context / max_tokens | 0 (let the router do context fallback) |

**Enforcement via `install_router_patch()`.** Both Router entry points are wrapped, because patching only one silently does nothing:

```python
Router.async_get_healthy_deployments    # primary pick path
Router._async_get_healthy_deployments   # retries / scheduler
```

The wrapper runs the original method, then drops deployments whose `model@host` key is still inside a cooldown window. Guard rails:

- **Fail-open** — any exception returns the original list untouched.
- **Never strands a group** — if filtering would empty the candidate list, the original list is returned so the error surfaces normally rather than as a hard "no deployments".
- Applied at import time *and* re-attempted lazily from the logger, so a late-constructed router is still covered.
- Disable with `LITELLM_SMART_COOLDOWN_ENFORCE=0`.

**Stable cooldown keys.** Old keys were `f"{model} @ {api_base}"`, which produced *both* `gemini-2.5-flash @ ?` and `gemini-2.5-flash @ https://generativelanguage…` for the same upstream — so nothing could ever match. Keys are now `model@host`, with all Google bases collapsed to `googleapis.com`, and `_key_from_raw()` accepts both formats so existing state files stay readable.

**Operator CLI:**

```bash
python3 -m tools.litellm_smart_cooldown status
python3 -m tools.litellm_smart_cooldown cool --model gemini/gemini-2.5-flash --hours 12
python3 -m tools.litellm_smart_cooldown clear --model gemini/gemini-2.5-flash
python3 -m tools.litellm_smart_cooldown test      # classifier self-test on real error strings
```

`test` is the regression gate — it asserts the real Google payloads classify as daily vs per-minute:

```
google_rpd   -> google_daily_quota       21865s (364m)
google_tpm   -> google_per_min_quota        60s (1m)
plain_429    -> rate_limit                 300s (5m)
```

### 2.2 `tools/litellm_dynamic_models.py` — discover, **prove**, merge

Hardcoded model IDs rot. Gateway catalogs do not. This runs on **every proxy start**.

```
list models  →  keep free  →  PROVE inference  →  merge into config
```

`ProviderSpec` is the registry; adding a gateway is one line:

```python
PROVIDERS: dict[str, ProviderSpec] = {
    "kilo":     ProviderSpec("kilo", KILO_BASE, "KILOCODE_API_KEY",
                             "ULTRA_FREE_KILO", "KILO_RPM", needs_key=False),
    "opencode": ProviderSpec("opencode", OPENCODE_BASES[0], "OPENCODE_API_KEY",
                             "ULTRA_FREE_OPENCODE", "OPENCODE_RPM", needs_key=True),
}
```

**The probe is the whole point.** A `200` from `/models` proves nothing. `_probe_generation()` sends one real 8-token completion and the provider is enrolled only if it succeeds (up to `probe_candidates` models are tried, so one cold or briefly-throttled model does not condemn a healthy gateway).

This single gate catches all three real-world failure modes found in this session:

| Failure | Signal | Example |
|---|---|---|
| Free tier gated to first-party app | `429 FreeUsageLimitError` | OpenCode lists 11 `*-free` models, refuses every external client |
| Model retired upstream | `410 Gone` | NVIDIA `meta/llama-3.1-8b-instruct` |
| Account alive but unfunded | `402` / `NOT_ENOUGH_BALANCE` | Cerebras, Chutes, Wafer, Venice |

Generated entries carry the two fields that silently break routing if omitted:

- **`rpm: N`** — `simple-shuffle` is *weighted by rpm*. An entry without it gets weight 1 and is effectively never picked.
- **`model_info.{max_input_tokens, max_output_tokens}`** — otherwise reported as `null` and context-window fallbacks misfire.

Both are populated from catalog metadata (`context_length`, `max_completion_tokens`).

**Fallback wiring** — discovered groups are appended to the strict-free pools, never to paid:

```
ULTRA_FREE_CLOUD      -> [ULTRA_FREE_CLOUD_LARGE, ULTRA_FREE_KILO]
ULTRA_FREE_CLOUD_FAST -> [ULTRA_FREE_CLOUD, ULTRA_FREE_CLOUD_LARGE, ULTRA_FREE_KILO]
```

**On-demand static audit** — the same probe applied to hand-written entries:

```bash
python3 tools/litellm_dynamic_models.py --verify-static --base litellm_config.yaml \
        --only nvidia_nim --workers 4 --timeout 25
```

Result on the day this was written — this is what would have caught 1.2 in August:

```
verified 5 deployments: 0 OK, 5 DEAD

  DEAD  BAD_LEGACY_MIXED_F_MODE        nvidia_nim/meta/llama-3.1-8b-instruct  Error code: 410
  DEAD  BAD_LEGACY_MIXED_F_MODE        nvidia_nim/meta/llama-3.1-8b-instruct  Error code: 410
  DEAD  BAD_LEGACY_MIXED_F_MODE_LARGE  nvidia_nim/meta/llama-3.1-8b-instruct  Error code: 410
  DEAD  ULTRA_HYBRID_MODEL             nvidia_nim/meta/llama-3.1-8b-instruct  Error code: 410
  DEAD  ULTRA_HYBRID_MODEL_LARGE       nvidia_nim/meta/llama-3.1-8b-instruct  Error code: 410
```

### 2.3 `tools/free_provider_audit.py` — sweep **every** credential

Registry-driven audit of all 46 providers in `~/dbpasses.txt`. Four checks per provider: **credential → catalog → free models → real inference**.

```bash
python3 tools/free_provider_audit.py                    # markdown table
python3 tools/free_provider_audit.py --json             # machine readable
python3 tools/free_provider_audit.py --only gemini,nvidia
python3 tools/free_provider_audit.py --out reports/free_provider_audit.md
```

**Two bugs in the first implementation that this exercise exposed — both worth keeping in mind:**

1. **Credential parser read URL lines as keys.** Labels are followed by annotation lines like `ZAI_API_URL: https://…` and `QWEN_API_URL: https://…`. A naive "next non-empty line" parser returns the URL, producing a 401 and a *false* verdict of "broken key". The parser now rejects any value containing `://`, any inline `NAME: value` shape, and anything under 8 chars. Fixing this alone turned Qwen from "401 dead key" into **101 usable models**.
2. **Probe candidate selection mattered more than the probe.** Probing the first catalog entry produced nonsense: `adept/fuyu-8b` (NVIDIA) and `nvr-tts-de-de` (OVH) both 404, making healthy accounts look dead. Candidates are now ranked by chat-likelihood (explicit `:free` first, then `flash`/`instruct`/`chat`/size hints), non-chat families (`embed|rerank|tts|asr|whisper|moderat|guard|safety|dall-e|flux|ocr`) are pushed to the back, and up to 4 are tried. Gemini went from "MODEL GONE" to **USABLE, 63 models** on this change alone.

**Measured result — 2026-11-12, 46 providers:**

`13 USABLE` · `3 RATE LIMITED` · `17 NO FUNDS/AUTH` · `13 other` (HTTP 4xx / unreachable / not checked)

| Provider | Status | Catalog | Notes |
|---|---|---:|---|
| `gemini` | USABLE | 63 | free-tier RPD is tiny (some models 20/day) |
| `kilo` | USABLE | 383 | **11 `:free` models, no key required** |
| `nous` | USABLE | 395 | 3 `:free` models verified |
| `qwen_dashscope` | USABLE | 101 | free key, previously masked by parser bug |
| `nvidia` | USABLE | 84 | keys fine; config pinned a retired model |
| `ovh_ai` | USABLE | 21 | **keyless** anonymous tier |
| `mistral` | USABLE | 55 | |
| `groq` | USABLE | 11 | |
| `publicai` | USABLE | 10 | |
| `inception` | USABLE | 4 | `mercury-2/3` |
| `deepseek` | USABLE | 2 | |
| `pollinations` | USABLE | 1 | **keyless** |
| `xai` | USABLE | 13 | |
| `opencode` | RATE LIMITED | 46 | 11 `*-free` listed, all `FreeUsageLimitError` for external clients |
| `zai` | RATE LIMITED | 12 | `Insufficient balance or …` |
| `openai` | RATE LIMITED | 96 | `You have no credits remaining` |
| `cerebras` / `chutes` / `wafer` / `venice` / `hyperbolic` / `novita` / `requesty` | NO FUNDS/AUTH | — | `402` / `NOT_ENOUGH_BALANCE` |
| `sambanova` | NO FUNDS/AUTH | 5 | catalog OK, inference rejects the key |
| `openrouter` | NO FUNDS/AUTH | 462 | 14 `:free` visible but `403` on use |
| `sambanova`, `featherless`, `llm7`, `modelscope`, `upstage`, `aimlapi` | NO FUNDS/AUTH | — | key- or plan-level blocks |

The distinction that matters: **catalog size is not capacity.** `featherless` listed 22,555 models and served none; `aimlapi` listed 980 and returned 403.

### 2.4 Launcher wiring — `tools/start_litellm_proxy.sh`

Discovery runs after keys are exported and before launch, and fails open:

```bash
LITELLM_DYNAMIC_MODELS=1            # 0 disables
LITELLM_DYNAMIC_PROVIDERS=kilo,opencode
```

**Critical gotcha, cost ~30 minutes:** the merged config **must live in the repo root**, not `/tmp`. LiteLLM resolves `callbacks: tools.litellm_smart_cooldown.smart_cooldown` relative to the **config file's directory**. With the config in `/tmp` the import fails and the proxy exits with:

```
ImportError: Could not import smart_cooldown from tools.litellm_smart_cooldown
```

The runtime config is therefore written to `litellm_config.runtime.yaml` (gitignored) and `PYTHONPATH` is set to the repo root as a second line of defence.

**Credential repair.** The `OPENCODE_API_KEY` label held a dead key (`api_key_expired`). The live OpenCode Zen credential is filed under a different label, so an extra `parse_key` line maps it to `OPENCODE_API_KEY`. Safe because `parse_key()` compares **whole lines**, so it cannot collide with the plain `OPENAI_KEY` label — an ordering hazard worth remembering.

---

## 3. Verification

```
$ curl -s http://localhost:4000/health/readiness      → 200

$ curl -s http://localhost:4000/v1/models | jq -r '.[].id' | grep KILO
ULTRA_FREE_KILO

$ curl -s -X POST http://localhost:4000/v1/chat/completions \
    -d '{"model":"ULTRA_FREE_KILO","messages":[{"role":"user","content":"Reply with exactly: ARCHIVE-OK"}],"max_tokens":200}'
content: 'ARCHIVE-OK'    finish_reason: stop
x-litellm-model-api-base: https://ai-gateway.kilo.ai/api/v1
```

Group composition at runtime — 11 deployments, `rpm: 3` each (weighted so they are actually selected):

```
openai/kilo-auto/free                            rpm=3  ctx=256000
openai/nvidia/nemotron-3.5-super-vl:free         rpm=3  ctx=1000000
openai/baidu/ernie-5.5-flash-preview:free        rpm=3  ctx=1024000
openai/thinkingmachines/inkling-2-small:free     rpm=3  ctx=1048576
openai/liquid/lfm-3-1.7b:free                    rpm=3  ctx=262144
openai/dots-studio/dots-3-note-preview:free      rpm=3  ctx=512000
openai/nvidia/nemotron-3.5-lightning:free        rpm=3  ctx=1000000
openai/cohere/north-mini-code:free               rpm=3  ctx=256000
openai/nvidia/nemotron-3-ultra-550b-a55b:free    rpm=3  ctx=1000000
openai/nvidia/nemotron-3-nano-omni-30b-a3b…      rpm=3  ctx=256000
openai/openrouter/free                           rpm=3  ctx=200000
```

---

## 4. Design rules extracted (apply these to any future provider)

1. **Never trust `/models`.** A retired model stays listed (NVIDIA 410 for months). Only a completion proves a deployment.
2. **Free ≠ listed.** `:free` in the catalog can still be `429 FreeUsageLimitError` for external clients (OpenCode). Probe before enrolling.
3. **Distinguish quota *periods*.** A per-minute window needs ~60s; a per-day window needs "until reset". Cooling a daily limit for 300s is a busy-loop.
4. **Make cooldowns enforceable, or they are decoration.** A logger that only writes a state file changes nothing; the router's static `cooldown_time` wins. Patch both healthy-deployment methods.
5. **`rpm` is routing weight.** Omitting it from a `simple-shuffle` group means the deployment is never picked.
6. **Parse credentials strictly.** Reject `://`, inline `NAME: value`, and short strings — otherwise a URL silently becomes your API key and a working provider is declared dead.
7. **Rank probe candidates.** Alphabetical-first is adversarial: it surfaces TTS, embedding and retired models.
8. **Config path determines callback import.** Custom callbacks resolve relative to the config file, not the CWD.
9. **Fail open, never strand a group.** Filtering that empties a candidate list should return the unfiltered list.
10. **Back up before editing.** `litellm_config.yaml`, the launcher and the cooldown module are copied to `.config_backups/<name>.<UTC>.bak` before each change, with deployment counts recorded, because other agents write to this tree concurrently.

---

## 5. Follow-ups

- [ ] Replace the 5 dead NVIDIA deployments with a verified live model (84 available; `meta/llama-3.2-90b-vision-instruct` and `openai/gpt-oss-20b` both returned 200).
- [ ] Add `qwen_dashscope` (101 models) as a first-class provider group.
- [ ] Move the verified `nous` `:free` models out of `BAD_LEGACY_*` into a strict-free group.
- [ ] Add `ovh_ai` and `pollinations` (both keyless) to `ULTRA_FREE_KILO`-style strict-free pools.
- [ ] Run `free_provider_audit.py` on a schedule and diff — new free catalogs appear frequently.
- [ ] Consider `--verify-static` in CI to fail the build when a configured deployment stops generating.

---

## 6. File map

| File | Purpose |
|---|---|
| `tools/litellm_smart_cooldown.py` | Failure classification + Router enforcement patch + operator CLI |
| `tools/litellm_dynamic_models.py` | Gateway free-model discovery + generation probe + static-config verification |
| `tools/free_provider_audit.py` | Registry-driven audit of every credential for usable free inference |
| `tools/start_litellm_proxy.sh` | Key export, discovery hook, launch |
| `litellm_config.yaml` | Group definitions (secrets referenced as `os.environ/<NAME>`) |
| `litellm_config.runtime.yaml` | Generated per start (gitignored) |
| `.config_backups/` | Timestamped pre-edit snapshots |

## 7. Reproducing

```bash
# 1. sanity-check the classifier
python3 -m tools.litellm_smart_cooldown test

# 2. what does discovery see right now?
python3 tools/litellm_dynamic_models.py --dry-run

# 3. which of our static deployments are dead?
python3 tools/litellm_dynamic_models.py --verify-static --base litellm_config.yaml

# 4. which credentials actually serve free inference?
python3 tools/free_provider_audit.py --workers 10

# 5. restart with discovery enabled
bash tools/start_litellm_proxy.sh --background
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:4000/health/readiness
```
