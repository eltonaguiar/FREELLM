# LiteLLM Free-Model Proxy — Architecture & Operating Notes

Snapshot of the local proxy that fronts free LLM providers behind one
OpenAI-compatible endpoint. No credentials are stored here — every deployment
references `os.environ/<NAME>`, with real values kept outside the repo.

---

## 1. Naming schema

Group names encode **routing behaviour**, because `_CHAT` (one model) and
`_CODE` (one model plus a fallback) used to look identical while behaving
differently.

| Suffix | Meaning | Mechanism |
|---|---|---|
| *(none)* | Exactly one model | single deployment |
| `_2ROTATE` | N peer models, one picked per request | `simple-shuffle`, weighted by `rpm` |
| `_1FALLBACK` | Reached **only** when its primary errors | `router_settings.fallbacks` |

**Counts are deliberately omitted on dynamically-discovered groups** (e.g.
`ULTRA_FREE_OPENROUTER`), because the free catalogue changes daily and the count
would rot. Member counts are always available from `/model/info`.

Rotation and fallback are **different mechanisms, not different counts**:
rotate picks a peer at random; fallback ranks primary above secondary and only
descends on failure.

---

## 2. Model discovery (`tools/litellm_dynamic_models.py`)

Hardcoded model IDs rot. `nvidia_nim/meta/llama-3.1-8b-instruct` was retired and
returned HTTP 410 for months while the config still listed it. So free models
are re-resolved from each gateway's live catalogue at every boot.

One `ProviderSpec` per gateway:

| Provider | Catalogue | Notes |
|---|---|---|
| `kilo` | `ai-gateway.kilo.ai` | anonymous works |
| `opencode` | `opencode.ai/zen/v1` | free tier is **app-gated** (see §5) |
| `gemini` | per-model × per-key fan-out | 4 key envs |
| `openrouter` | `openrouter.ai/api/v1` | pricing-based free detection |
| `pins` | per-provider | single verified model → dedicated alias |

### The five admission gates

A catalogue entry becomes a live deployment only if it passes **all** of:

1. **Pricing must be exactly `0`.**
   Negative prices must be **rejected**. OpenRouter encodes *"this is a router,
   bill the underlying model"* as `-1`:

   ```
   openrouter/auto     pricing {prompt: "-1", completion: "-1"}
   nvidia/switchyard   pricing {prompt: "-1", completion: "-1"}
   ```

   Testing `float(raw) > 0` classifies every router as **free**, which routes
   traffic to **paid** models and spends real credit. Only an exact zero is free.

2. **Output must be text — all of it.**
   `google/lyria-3-pro-preview` is a *music* model priced at $0 and declares
   `output_modalities: ["text","audio"]`. Accepting anything that merely
   *contains* "text" lets music models into a chat pool. Require every output
   modality to be text.

3. **Minimum context.** Models below a floor (default 100 000) are not useful as
   general fallbacks.

4. **A real completion.** A `200` from `/models` does not mean inference works.
   Probe with a genuine request before enrolling.

5. **Reasoning-aware probing.** Reasoning models return `200` with **empty
   content** when `max_tokens` is too low, because the budget is consumed
   thinking. Probe at 64, then retry at 800. `finish_reason == "length"` with no
   content means *retry bigger*, not *dead*.

### Other safeguards

- **Browser User-Agent required.** Cloudflare-fronted gateways (Groq, Cerebras,
  Together, Kilo) answer the default `python-requests` agent with `403` /
  error `1010`. This alone makes healthy providers look dead.
- **Last-good cache.** A failed refresh reuses the previous verified list rather
  than wiping the group.
- **Shuffle-metric alignment.** `simple-shuffle` reads its weight metric from
  `healthy_deployments[0]` **only**, then scores siblings with
  `.get(metric, 0)`. A group whose first member declares `rpm` and whose others
  declare `weight` silently leaves most members **never selected** (observed:
  38 of 45 routes unpickable). Every member of a group must carry the same
  metric.

---

## 3. Cooldown & quota classification (`tools/litellm_smart_cooldown.py`)

Failures are classified so a daily exhaustion parks the upstream until its real
reset, instead of retrying every 65 s all day.

| Category | Trigger | Cooldown |
|---|---|---|
| `google_daily_quota` | RPD exhausted | until Pacific midnight |
| `google_per_min_quota` | TPM exceeded | parsed `retry in Ns`, clamped 60–900 |
| `google_quota` | other Google quota | until Pacific midnight |
| `daily_quota_cf` | Cloudflare neurons/RPS | until UTC midnight |
| `monthly_quota_hf` | HuggingFace 402 | until next month |
| `dead_key` | 401 / invalid key / balance | 24 h |
| `rate_limit` | 429 | 300 s |
| `server_error` | 5xx | 60 s |
| `bad_request` | 400 / context length | 0 (let the router handle it) |

**Enforcement** patches **both** `Router.async_get_healthy_deployments` (primary
pick path) *and* `Router._async_get_healthy_deployments` (retries/scheduler).
Patching only the underscore variant does nothing. The filter is fail-open: it
never returns zero candidates for a group.

---

## 4. Cloudflare Workers AI neuron budget

Workers AI reports the **exact** neuron cost of every request:

```
response header   cf-ai-neurons: 1.86
response body     usage.neurons: 1.8592603206634521
```

The free plan is 10 000 neurons/day, reset at 00:00 UTC, **per account** — not
per key. Adding tokens to the same account does not add quota.

So spend is **ledgered from real values**, not estimated, and the Cloudflare
deployments are dropped from routing at a soft cap (default 95% of budget,
keeping headroom for in-flight requests). Parking *before* exhaustion gives
callers clean failover instead of a 429 storm.

Env: `CF_DAILY_NEURON_BUDGET` (0 disables), `CF_NEURON_RESERVE`,
`LITELLM_CF_NEURON_STATE`.

---

## 5. Hard-won traps — catalog ≠ access

Every one of these caused a real misdiagnosis. They generalise.

| Trap | Symptom | Truth |
|---|---|---|
| **GitHub Models zombie** | `200`, `content-type: text/plain`, body `OK` | Retired placeholder, not inference. Worst failure mode: a *silent fake success*. |
| **OpenCode free tier** | lists ~11 `*-free` models, then `429 FreeUsageLimitError`, `Retry-After: 76170` | App-gated. External clients get 403/429 even with a valid key. Paid models went 402→200 after top-up while free stayed 429→429. |
| **NVIDIA retired model** | `/models` still lists it | HTTP 410/404 on inference. Only a real completion catches this. |
| **Negative pricing** | `-1` reads as "not > 0" ⇒ "free" | Router that bills the underlying **paid** model. |
| **Zero-priced non-chat** | `pricing 0/0` | Music/audio models. Check output modalities. |
| **Huge catalogue, no service** | 22 555 models listed | Served none. Catalogue size ≠ capacity. |
| **Trial credit** | works briefly | Dies when credit runs out. Ongoing free tier ≠ trial. |
| **Reasoning models** | empty content at low `max_tokens` | Budget consumed thinking. Use ≥1024 for callers, ≥800 for probes. |
| **`/v1/models` vs upstream ids** | grep finds "no model X" | `/v1/models` returns **aliases**, never upstream model ids. Use `/model/info` to see loaded deployments. |
| **Ollama context cap** | config says 262 144, serving 16 384 | `OLLAMA_CONTEXT_LENGTH` is a server-wide default. `model_info.max_input_tokens` is metadata only and never reaches Ollama. Pin `num_ctx` **per deployment**. |

---

## 6. Ollama vs vLLM (measured, GB10-class unified memory)

| | Ollama | vLLM |
|---|---|---|
| Single-stream | **74–89 tok/s** | 8–10 tok/s |
| Concurrency ×4 | serialises (queues) | **54.7 tok/s aggregate** |
| Weights | Q4_K_M GGUF | bf16 only |
| Reasoning | native `thinking` field | needs `--reasoning-parser` |

The single-stream gap is **quantisation, not the engine**: the box is
memory-bandwidth-bound, and Q4 reads roughly a third of the bytes per token that
bf16 does. vLLM wins decisively on batching, which is why it backs a
concurrency-specific alias rather than the chat default.

vLLM caveats seen here: ModelOpt NVFP4 checkpoints can fail on scale-tensor
naming (`KeyError: layers.0.mlp.experts.w2_input_scale`), and CUDA-graph capture
needs a current driver — on an older driver the engine is stuck far below its
published throughput.

---

## 7. Operating commands

```bash
# start (regenerates the runtime config from the static one + live discovery)
bash tools/start_litellm_proxy.sh --background

# inspect what is ACTUALLY loaded (aliases → deployments)
curl -s localhost:4000/model/info -H "Authorization: Bearer <key>"

# cooldown / neuron state
python3 -m tools.litellm_smart_cooldown status
python3 -m tools.litellm_smart_cooldown test     # classifier self-test

# discovery dry-run for one provider
python3 tools/litellm_dynamic_models.py --dry-run --providers openrouter

# probe every deployment in a config for dead entries
python3 tools/litellm_dynamic_models.py --verify-static --base litellm_config.yaml
```

**Config-path gotcha:** the merged runtime config must live in the repo root, not
`/tmp`. LiteLLM resolves `callbacks: tools.x.y` relative to the **config file's
directory**, so a `/tmp` config makes the callback import fail and the proxy
exits.
