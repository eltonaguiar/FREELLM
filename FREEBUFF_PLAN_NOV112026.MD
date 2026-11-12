# FREEBUFF_PLAN_NOV112026 — free-pool plan and status

**Author:** Freebuff agent · **Date:** 2026-11-12
**Companion docs:** `FREEBUFF_NOV112026.MD` (audit + findings), `DEEPSEEK_GHCOPILOT_NOV112026.MD` (Copilot/DeepSeek)
**No credentials appear in this file.** Provider names, endpoints, model IDs, HTTP statuses and logic only.

---

## 0. Where this stands

| Area | State |
|---|---|
| Retired / dead model IDs in `litellm_config.yaml` | **FIXED** — 23 deployments re-pointed at models that returned a real completion |
| Dead GitHub Models endpoint | **FIXED** — `models.inference.ai.azure.com` → `models.github.ai/inference` |
| Credentials present in `dbpasses.txt` but never exported | **FIXED** — 22 `parse_key` lines added, all resolve non-empty |
| Live-catalog free discovery (multi-source) | **BUILT** — `tools/free_model_discovery.py`, 49 verified models |
| Rotation without proxy restart (hot sync) | **NOT BUILT** — design below |
| Last-good cache per source | **NOT BUILT** — this is the most important gap |
| Gemini per-model × per-key pooling | **DONE by the Copilot agent** — `ULTRA_FREE_GEMINI`, 36 deployments (12 models × 3 keys), all carrying `rpm: 5` |

---

## 1. Step 0 — credential inventory (masked)

Rule of record: **grep `dbpasses.txt` before declaring anything unconfigured.** Multiple agents have
called a provider "missing" while the key sat in the file under a label that didn't match the env var.

What the inventory produced today:

- `dbpasses.txt` holds **476 lines / ~106 parseable label→value pairs**; the running proxy exported
  **223 env vars**.
- **22 credentials** were in the file with no `parse_key` line: `SILICONFLOW`, `PUBLICAI`,
  `HYPERBOLIC`, `MODELSCOPE`, `FEATHERLESS`, `ORCAROUTER` ×2, `REQUESTY`, `ZAI` ×3, `WAFER`,
  `VERCEL`, `INFRON` ×2, `UPSTAGE` ×2, `EXA` ×2, `FIRECRAWL`, `AIONLABS`, `OPENCODE` alt.
  All 22 now export non-empty (verified by a harness that prints **names only**).
- Two label traps, both fixed:
  1. **`parse_key()` trims whitespace**, so a label with a trailing space in the file
     (`PUBLIC AI KEY `, `MODELSCOPE API KEY : `) must be passed **without** it. Passing the
     untrimmed string silently resolves to empty — the provider then looks missing.
  2. `parse_key()` compares **whole lines**, so a new label can never shadow `OPENAI_KEY`; but
     ordering still matters when two labels map to the same env var (later wins).

Masked inventory format to use in any future report: `provider | label | last-4 | resolves?` —
never the value, never a prefix.

---

## 2. Step 1 — discovery from live catalogs (implemented)

`tools/free_model_discovery.py` — 30-provider registry, catalog-first, no hardcoded model IDs.

| Source | Catalog endpoint | Free filter | Measured today |
|---|---|---|---|
| OpenRouter | `/api/v1/models` | `pricing.prompt == 0 AND pricing.completion == 0` | 462 catalog → 16 zero-priced → **11–16 alive** |
| Kilo Gateway | `/api/gateway/v1/models` | same pricing rule | 383 catalog → 14 free → **11–14 alive** |
| Groq | `/openai/v1/models` | whole catalog free-tier | 11 catalog → **7–11 alive** |
| Nous | `/v1/models` | `:free` suffix | 395 catalog → 3 free → **1 alive** (`longcat-2.5:free`, 1M ctx) |
| NVIDIA NIM | `/v1/models` | whole catalog | 84 catalog → **2 alive** (`llama-3.2-90b-vision`, `diffusiongemma`) |
| Mistral / Cohere / DeepInfra / Bluesmind | `/v1/models` | whole catalog | 4 / 10 / 6 / 4 alive |
| GitHub Models | `models.github.ai` | whole catalog | alive, 200 on smoke call |
| Pollinations / LLM7 / OVH | keyless | `tier: anonymous` | alive |
| Gemini | `models?key=` | `generateContent` supported | see §4 |

Rejected counts are recorded per source in the JSON report (`reports/free_model_discovery_*.json`),
with the reason: `404 model does not exist`, `410 end of life`, `402 no funds`, `401 invalid key`,
`429 FreeUsageLimit`, `403 external client blocked`.

**Cerebras / Fireworks / Together / SambaNova / Qwen-DashScope** returned no admitted models today:
first two 404 on every probed ID (Cerebras catalog is also Cloudflare-blocked), Together/SambaNova 401,
Qwen 401 with the currently exported key. They are reported **unverified** with the exact error —
not silently dropped and not filled in from memory.

---

## 3. Step 2 — admission gates (implemented, one gap)

1. **Smoke call is the gate.** `max_tokens: 24`, `temperature: 0`, browser UA, redirects followed,
   45 s (90 s for known-slow hosts). Only HTTP 200 enters the config. `/models` is never evidence —
   NVIDIA still listed `meta/llama-3.1-8b-instruct` for months while it returned 410.
2. **Context recorded** from catalog; below-32k models must go to a `_SMALL` group, never `_LARGE`.
3. **Dedupe** identical upstream models across gateways — preference order: direct provider → Kilo →
   OpenCode.
4. **GAP — last-good cache.** Not implemented. Today a failed refresh can shrink the pool. Fix:
   write `reports/free_model_pool_cache.json` keyed by source and only overwrite a source's list when
   the new list is non-empty **and** the run succeeded.

Two probe artefacts worth keeping in the code:
- Cloudflare returns `1010` to a plain `urllib` UA on Groq/Cerebras/Together/Kilo — valid keys look dead.
- Reasoning models answer `200` with empty `content` and `finish_reason=length` at low token budgets.
  That is a pass; consumers must give them ≥256 tokens.

---

## 4. Gemini structure (agreed with both other docs, not yet built)

Free-tier quota is **per model per day**. A hand-guessed fallback chain burns the first model's daily
allowance, then reacts only after failures. Correct structure: **one group, one deployment per
(model × key)** — 3 keys × N models — so `simple-shuffle` spreads load and a 429 cools only the one
deployment that hit its cap, while siblings keep serving.

Implementation notes for whoever builds it:
- Register each pair with a distinct `model_name`-internal id but the same group name.
- Cooldown must honour the period: per-day → until Pacific midnight; per-minute → honour the
  `retry in Ns` hint, clamped 60–900 s. Cooling a daily limit for 300 s is a busy loop.
- Do **not** rebuild the old static chain.

---

## 5. Step 3 — rotation without restart (design)

Current path (restart-regenerated YAML) stays as bootstrap/fallback. Add hot sync:

1. `store_model_in_db: true`; provider catalog lives in the DB, not only in YAML.
2. Sync tool diffs discovered pool vs `GET /model/info`, applies delta via `POST /model/new`,
   `PATCH /model/{id}/update`, delete endpoint.
3. Triggers: at proxy start, on a 30–60 min timer, and on demand (`--sync`).
4. **Group membership only** — never touch group names or the fallback graph. Strict-free groups must
   never gain a paid deployment: an ordered preference list (direct → Kilo → OpenCode) and a
   post-sync audit that re-reads `/model/info` and **fails loudly** if any `ULTRA_FREE_*` group holds a
   route whose catalog price is nonzero.
5. Fail open, never strand a group: if filtering empties a candidate list, return the unfiltered list so
   the real error surfaces instead of a hard "no deployments".

Caveat for the DB path: a runtime config must live in the **repo root** so `callbacks:
tools.litellm_smart_cooldown…` resolves relative to the config file, not the CWD.

---

## 6. What was actually fixed today (evidence)

Backups: `.config_backups/litellm_config.yaml.2026-11-12T0223*.bak` and
`.config_backups/start_litellm_proxy.sh.2026-11-12T0223*.bak`.

23 deployments repaired, each replacement verified by a direct smoke call **before** editing:

| Old | New | Old status |
|---|---|---|
| `nvidia_nim/meta/llama-3.1-8b-instruct` ×5 | `nvidia_nim/meta/llama-3.2-90b-vision-instruct` | 410 EOL |
| `groq/llama-3.1-8b-instant` ×5 | `groq/openai/gpt-oss-20b` | 404 |
| `cerebras/llama3.1-8b` | `groq/openai/gpt-oss-20b` | 404 |
| `models.inference.ai.azure.com` ×5 | `models.github.ai/inference` | DNS dead |
| `fireworks kimi-k2p5` ×4 | `groq/openai/gpt-oss-20b` | 404 |
| `nous deepseek/deepseek-v4-flash:free` ×2 | `nous meituan/longcat-2.5:free` (1M) | 404 |
| `openrouter inclusionai/ring-2.6-1t` ×4 | `openrouter nvidia/nemotron-3-ultra-550b-a55b:free` (1M) | now paid |
| `ofox z-ai/glm-4.7-flash:free` | `groq/openai/gpt-oss-20b` | 410 |
| `kilocode nemotron-3-super-120b-a12b:free` | `nemotron-3-ultra-550b-a55b:free` @ `api.kilo.ai/api/gateway/v1` | 404 |

Smoke evidence on a **second** instance (`:4002`, live `:4000` untouched):

```
readiness=200
ULTRA_HYBRID_MODEL         content='FIXED'  base=https://api.groq.com/openai/v1
ULTRA_HYBRID_MODEL_LARGE   content='FIXED'
BAD_LEGACY_MIXED_F_MODE    content='FIXED'
```

Earlier, the generated dynamic fragment was validated the same way on `:4001`:
`DYNAMIC_FREE` → `Ok.`, `DYNAMIC_FREE_KILO_GATEWAY` → `ok`, 0 YAML/key errors.

---

## 7. Cross-check against the other two docs

**Agreements.** `/models` proves nothing — only a completion does. Retired IDs caused most "dead
provider" verdicts. Gemini needs per-(model × key) pooling, not a chain. Cooldown must honour the
quota *period*, and enforcement (not just logging) is required. `rpm` is routing weight in
`simple-shuffle`. Credential parsing is the historical failure point. Never put a paid route in a
strict-free group.

**Discrepancies to re-test (do not resolve by assumption):**

| Point | Copilot/DeepSeek doc | This doc |
|---|---|---|
| Kilo anonymous access | "no key required, ~200 req/h" | `401 INVALID_TOKEN` today; **with** the key it returns 200 on both `api.kilo.ai/api/gateway/v1` and `kilocode.ai/api/openrouter` |
| OpenRouter `:free` | "403 on use, NO FUNDS/AUTH" | 11–16 `:free` models returned **200** repeatedly (nemotron 1M, ernie, dots, lfm) |
| OpenCode | `429 FreeUsageLimitError` for external clients | key itself returns `401 api_key_expired`; both describe an external-client block, different stage |
| NVIDIA | 5 dead deployments, follow-up suggests `llama-3.2-90b-vision` | confirmed 200 → **that follow-up is now DONE**; only 2 of 84 models answer on this key |
| Cerebras | `402 NOT_ENOUGH_BALANCE` | probed ID returns `404 model does not exist`; catalog blocked by Cloudflare, so key state is **unverified** |

**Duplication to resolve.** Three overlapping discovery implementations exist or are proposed:
`tools/litellm_dynamic_models.py` (Kilo/OpenCode + static verify), `tools/free_model_discovery.py`
(30-source registry), `tools/free_provider_audit.py` (credential sweep). Recommendation: keep
`free_provider_audit.py` as the credential/report layer, `litellm_dynamic_models.py` as the
merge+probe layer, and fold the 30-source registry into it as the `PROVIDERS` table — one pipeline,
three entry points (`--audit`, `--sync`, `--verify-static`).

---

## 8. Order of work

1. **Last-good cache** per source (highest value, small): prevents a bad refresh from shrinking the pool.
2. **Hot sync** (`store_model_in_db`, `/model/new` + `PATCH` + delete, 30–60 min timer) with the
   strict-free post-sync audit.
3. **Gemini (model × key)** group, replacing any static chain.
4. **Consolidate** the three discovery scripts into one provider registry.
5. **CI gate:** `--verify-static` on `litellm_config.yaml` fails the build when a configured deployment
   stops generating; weekly `--audit` diff to catch new/removed free catalogs.
6. **Probe the 22 newly wired keys** for usable free inference and enrol the winners (ZAI, SiliconFlow,
   Infron, OrcaRouter, Requesty are the most promising).

## 9. Safety rules (all docs agree; keep them)

- Timestamped backup **before** any config edit — other agents write to this tree concurrently.
- No key values in code, logs, commits, chat output, or a public repo. `os.environ/<NAME>` only.
- Never add a paid route to a strict-free group.
- Never restart the shared `:4000` proxy unattended; document the command and let the operator run it.
- If catalog and smoke call disagree, believe the smoke call; if a source cannot be verified, report it
  as unverified with the exact error.

## 10. Live collaboration notes

See `FREEBUFF_NOV112026.MD` §8–9 for the reply to the Copilot agent's audit, the
zero-weight-tail bug, provider-prefix detection limits, the Kilo host split and the current live state
(19 groups / 212 deployments).

## 11. Free-provider generosity audit (2026-11-12)

Live-probed ranking of 16 providers, the GitHub Models retirement (5 zombie deployments),
the Cerebras/Mistral downgrades, the rotten-key repair queue, and why AMD Token Factory is
excluded (China-IP only). See `FREEBUFF_NOV112026.MD` section 10.

## 12. Verification commands

```bash
python3 tools/free_model_discovery.py --no-probe --only kilo_gateway,openrouter,groq
python3 tools/free_model_discovery.py --probe --emit-yaml litellm_generated_free.yaml
python3 -c "import yaml;d=yaml.safe_load(open('litellm_generated_free.yaml'));print(len(d['model_list']))"
bash -n tools/start_litellm_proxy.sh
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:4000/health/readiness
```
