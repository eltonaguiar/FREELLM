# FREELLM — Rotating-Fallback LLM Proxy

A local LiteLLM proxy on `http://localhost:4000/v1` that fronts **25+ verified LLM upstreams** behind one OpenAI-compatible endpoint with automatic rate-limit rotation.

## Security Model

**NO API KEYS are stored in this repository.** All keys are sourced from an external file at launch time.

| File | Location | In Git? |
|------|----------|---------|
| Keys file (`dbpasses.txt`) | `~/dbpasses.txt` (home directory, outside repo) | **NO** |
| Config (`litellm_config.yaml`) | This repo | Yes — uses `os.environ/ENV_VAR_NAME` references only |
| Launcher (`tools/start_litellm_proxy.sh`) | This repo | Yes — parses keys at runtime, never echoes them |
| Proxy log (`/tmp/litellm_proxy.log`) | `/tmp/` | No — gitignored |
| Cooldown state (`/tmp/litellm_cooldown_state.json`) | `/tmp/` | No — gitignored |

### Key Storage
- Place your API keys in `~/dbpasses.txt` using the label format documented below.
- The launcher reads labels → extracts values → exports as env vars → passes to LiteLLM child process.
- Keys never touch the config file, never appear in logs (masked by provider), and the keys file is gitignored at the filesystem level.

## Quick Start

```bash
# 1. Install LiteLLM
python3 -m venv .venv
.venv/bin/pip install 'litellm[proxy]'

# 2. Create your keys file (~15 lines per provider block)
#    See "Keys File Format" section below for template.
nano ~/dbpasses.txt

# 3. Start the proxy
bash tools/start_litellm_proxy.sh --background

# 4. Verify health
curl -s http://localhost:4000/health/readiness

# 5. Test a request
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer anything" \
  -H "Content-Type: application/json" \
  -d '{"model":"free-mode","messages":[{"role":"user","content":"1+1?"}],"max_tokens":50}'

# 6. Stop the proxy
pkill -f 'litellm.*litellm_config'
```

## Client Setup

Point any OpenAI-compatible client at the proxy:

```python
from openai import OpenAI
client = OpenAI(
    base_url="http://localhost:4000/v1",
    api_key="anything",   # placeholder — real auth is per-upstream
)
resp = client.chat.completions.create(
    model="hybrid-model",
    messages=[{"role": "user", "content": "Hello"}],
)
print(resp.choices[0].message.content)
```

### Roo / Kilo Code Configuration
- **Base URL**: `http://localhost:4000/v1`
- **Model**: `free-mode` (or `paid-mode`, `hybrid-model`)
- **API Key**: any non-empty string

## Virtual Model Groups

| Group | Cost | Description |
|-------|------|-------------|
| `free-mode` | $0 | 22 free-tier upstreams (Groq, NVIDIA, Gemini, GitHub Models, Fireworks, Together, Cerebras, etc.) |
| `free-mode-fast` | $0 | Top free-tier by TPS with tool-call support (Cerebras, Groq, Bluesmind) |
| `free-mode-tools` | $0 | Free subset that handles parallel `tool_use` cleanly |
| `free-mode-large` | $0 | Long-context fallback (Gemini 1M, OpenRouter Ring 262K, NVIDIA, Fireworks) |
| `paid-mode` | Per-token | Premium frontier (Anthropic Claude Haiku, DeepSeek, Moonshot Kimi, AIMLAPI paid) |
| `paid-mode-fast` | Per-token | Fastest paid providers (xAI Grok, Anthropic Haiku, DeepSeek) |
| `paid-mode-large` | Per-token | Long-context paid fallback |
| `hybrid-model` | $0 | Backward-compat alias for existing configs (= free-mode subset) |
| `hybrid-model-large` | $0 | Large-context variant of hybrid |

### Routing Strategy
- **Rotation**: `simple-shuffle` — picks random upstream per request
- **Retries**: 2 retries with per-error-type policies (0 for auth failures, 3 for rate limits)
- **Fallbacks**: Oversize prompts auto-route to `*-large` variants via context window detection
- **Cooldown**: Custom smart-cooldown logger classifies failures and records meaningful unban times

## Keys File Format (`~/dbpasses.txt`)

Labels must match exactly what's in `start_litellm_proxy.sh`. Each label appears on its own line; the key value is the next non-empty, non-header line.

```text
GROQ FREE KEY:
gsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

GOOGLE GEMINI API KEY:
AIzaSyxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

NVIDIA:
nvapi-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

**Rules:**
- Labels with trailing spaces will still match (trailing whitespace is trimmed)
- Pure-uppercase lines after a label are skipped (e.g., `GROK_SUPER` under `GROK:`)
- URL lines are skipped
- Colons-ending lines are treated as continuation labels, not values

## Tools

| Tool | Purpose |
|------|---------|
| `tools/verify_all_keys.py` | Concurrent health-check of every key against live endpoints |
| `tools/litellm_smart_cooldown.py` | Custom logger — classifies failures, writes cooldown state |
| `tools/vllmp_mode_status.py` | Dashboard showing group health + recent request counts |

```bash
# Health-check all keys (spins up 10 concurrent threads)
python3 tools/verify_all_keys.py

# View current proxy mode health
python3 tools/vllmp_mode_status.py

# Tail proxy log with 429/cooldown highlights
tail -f /tmp/litellm_proxy.log | grep -iE '429|RateLimit|cooldown'
```

## Skipped Upstreams

These providers were audited but are NOT active due to account restrictions:

| Provider | Reason |
|----------|--------|
| Cloudflare Workers AI | Daily 10k-neuron quota exhausted; auto-rejoins at UTC midnight |
| HuggingFace (3 tokens) | Monthly account-pool credits depleted; resets first of next month |
| xAI Grok | Key reports invalid per xAI; needs regeneration at console.x.ai |
| Inception (mercury) | Locked to accounts created before cutoff date |
| Ollama Cloud | SSH-ed25519 public key requiring JWT signing, not bearer auth |
| Qwen DashScope | Key invalid per provider; re-issue needed |
| Chutes | Account balance $0 |
| AIMLAPI (free) | ALL_TIME_LIMIT reached (paid key works, in paid-mode) |
| OpenAI direct | Quota exhausted; surfaces if topped up |

## Troubleshooting

### Stale shell env vars shadow file values
If a key was changed in `~/dbpasses.txt` but you get 403 errors, check for stale environment variables:
```bash
env | grep GROQ_API_KEY   # if set in shell, it shadows the file
unset GROQ_API_KEY        # clear the stale var
bash tools/start_litellm_proxy.sh -b    # restart — file values take precedence
```

### Cloudflare 403 error 1010
The Python default User-Agent is blocked by Cloudflare fronting. The launcher and health checker both send a Mozilla UA to avoid this.

### Proxy won't start
```bash
# Check port availability
lsof -i :4000
# Kill existing process
pkill -f 'litellm.*litellm_config'
# Verify venv
ls .venv/bin/litellm
# Fresh install
rm -rf .venv && python3 -m venv .venv && .venv/bin/pip install 'litellm[proxy]'
```

### Verify no keys leaked into git
```bash
git log -p --all -- '*.yaml' '*.sh' | grep -i 'api[_-]\?key.*[A-Za-z0-9_-]\{20,\}' | head
# If nothing prints, you're safe. Configs use os.environ/VAR_NAME syntax only.
```

## License

MIT
