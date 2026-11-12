"""
LiteLLM smart-cooldown custom logger.

Classifies per-deployment failures into a category and computes a meaningful
unban time, instead of the static `cooldown_time: 300` seconds in the router.

Categories
----------
  daily_quota   (CF, some HF) → cool until next UTC midnight
  monthly_quota (HF account-pool 402) → cool until UTC midnight on 1st of next month
  dead_key      (401 with "invalid"/"out of funds") → cool 24h (operator action needed)
  rate_limit    (transient 429 without quota markers) → cool 300s
  server_error  (5xx) → cool 60s

Writes a state file at /tmp/litellm_cooldown_state.json so the human
operator can see WHY a provider is parked (which the LiteLLM internal
state alone never tells you).

How LiteLLM picks this up
-------------------------
litellm_settings:
  callbacks: tools.litellm_smart_cooldown.smart_cooldown

Register the *instance* (smart_cooldown) — LiteLLM treats it as a
CustomLogger and calls async_log_failure_event() on every upstream failure.

Reading the state file:
  jq . /tmp/litellm_cooldown_state.json
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re as _re
from pathlib import Path
from typing import Any

from litellm.integrations.custom_logger import CustomLogger  # type: ignore

_STATE_PATH = Path(os.environ.get("LITELLM_COOLDOWN_STATE", "/tmp/litellm_cooldown_state.json"))
_ENFORCE = os.environ.get("LITELLM_SMART_COOLDOWN_ENFORCE", "1") not in ("0", "false", "False", "")


def _now_utc() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _seconds_until_next_utc_midnight() -> int:
    now = _now_utc()
    tomorrow = (now + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(60, int((tomorrow - now).total_seconds()))


def _seconds_until_next_month_start() -> int:
    now = _now_utc()
    if now.month == 12:
        nxt = now.replace(year=now.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = now.replace(month=now.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return max(3600, int((nxt - now).total_seconds()))


def _seconds_until_next_google_reset() -> int:
    """Google AI Studio free-tier RPD quotas reset at midnight Pacific Time."""
    now = _now_utc()
    pacific = _dt.timezone(_dt.timedelta(hours=-8))  # PST; +/-1h error is harmless
    local = now.astimezone(pacific)
    tomorrow = (local + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(300, int((tomorrow - local).total_seconds()))


def _parse_retry_hint(msg: str) -> int | None:
    """Google returns `Please retry in 23.713777494s` — honour it (capped 60..900s)."""
    m = _re.search(r"retry in ([0-9]+(?:\.[0-9]+)?)s", msg)
    if not m:
        return None
    try:
        return max(60, min(900, int(float(m.group(1))) + 5))
    except Exception:
        return None


_GOOGLE_DAILY_MARKERS = (
    "per day",
    "requests per day",
    "generate_requests_per_day",
    "free_tier_requests",
    "daily limit",
    "day limit exceeded",
    "current quota, please check your plan and billing",
    "you have reached a rate limit",
)
_GOOGLE_PER_MIN_MARKERS = (
    "per minute",
    "input_token_count",
    "generate_content_free_tier_input_token_count",
    "tokens per minute",
    "generate_requests_per_minute",
)


def _classify(exc: BaseException | None, api_base: str | None) -> tuple[str, int]:
    """Return (category, cooldown_seconds)."""
    msg = (str(exc) if exc else "").lower()
    base = (api_base or "").lower()

    # --- Google / Gemini free-tier quota -------------------------------------
    # A free-tier project that has exhausted its DAILY quota (RPD) cannot serve
    # again until the Pacific-midnight reset. Cooling it for the generic 300s
    # made the router re-pick it all day (144 RESOURCE_EXHAUSTED in the log on
    # 2026-11-11). Distinguish RPD (long) from TPM (short) explicitly.
    is_google = "generativelanguage.googleapis.com" in base or "googleapis.com" in base or "vertex" in base
    google_msg = ("resource_exhausted" in msg) or ("generate_content" in msg) or ("quota exceeded for metric" in msg)
    if (is_google and ("429" in msg or "quota" in msg or "resource_exhausted" in msg)) or google_msg:
        if any(k in msg for k in _GOOGLE_DAILY_MARKERS):
            return "google_daily_quota", _seconds_until_next_google_reset()
        if any(k in msg for k in _GOOGLE_PER_MIN_MARKERS):
            hint = _parse_retry_hint(msg)
            return "google_per_min_quota", hint or 120
        return "google_quota", _seconds_until_next_google_reset()

    # Cloudflare daily neuron quota
    if "cloudflare.com" in base and ("429" in msg or "rate" in msg or "quota" in msg or "neuron" in msg):
        return "daily_quota_cf", _seconds_until_next_utc_midnight()

    # HuggingFace monthly account-pool 402
    if "huggingface" in base or "hf.co" in base:
        if "402" in msg or "payment required" in msg or "monthly" in msg or "credit" in msg:
            return "monthly_quota_hf", _seconds_until_next_month_start()
        if "429" in msg:
            return "rate_limit", 300

    # Dead-key / insufficient-funds signals (Nous balance=0, OFOX invalid key,
    # Hypereal "Insufficient credits", DeepInfra primary 401, etc.)
    if (
        "401" in msg
        or "unauthorized" in msg
        or "invalid api key" in msg
        or "invalid or expired api key" in msg
        or "out of funds" in msg
        or "insufficient credits" in msg
        or "insufficient balance" in msg
        or "balance" in msg
        or "top up" in msg
    ):
        return "dead_key", 24 * 3600

    # Generic rate limit
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return "rate_limit", 300

    # 5xx
    if "500" in msg or "502" in msg or "503" in msg or "504" in msg or "internal server" in msg:
        return "server_error", 60

    # Context-window / bad-request → don't cool, return 0 (let router handle context fallback)
    if "400" in msg or "context" in msg or "max_tokens" in msg or "input validation" in msg:
        return "bad_request", 0

    return "other", 120


def _load_state() -> dict[str, Any]:
    if not _STATE_PATH.exists():
        return {"version": 1, "cooled": {}}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {"version": 1, "cooled": {}}


def _save_state(state: dict[str, Any]) -> None:
    try:
        _STATE_PATH.write_text(json.dumps(state, indent=2, default=str))
    except Exception:
        pass


def _norm_host(api_base: str | None, model: str | None) -> str:
    """Collapse every Google api_base to one token so cooldowns don't fragment.

    Historically `_record` keyed on the raw api_base, which produced BOTH
    `gemini-2.5-flash @ ?` and `gemini-2.5-flash @ https://generativelanguage...`
    for the same exhausted upstream — so nothing could match it reliably.
    """
    base = (api_base or "").strip().lower()
    mdl = (model or "").strip().lower()
    if "googleapis.com" in base or "vertex" in base or mdl.startswith("gemini/") or mdl.startswith("vertex_ai/"):
        return "googleapis.com"
    if not base:
        return "?"
    m = _re.match(r"^[a-z]+://([^/]+)", base)
    return m.group(1) if m else base


def _deployment_key(model: str | None, api_base: str | None) -> str:
    """Stable identity used both when recording and when filtering routing."""
    return f"{(model or '?').strip()}@{_norm_host(api_base, model)}"


def _key_from_raw(raw: str) -> str:
    """Accept both the new `model@host` keys and legacy `model @ api_base` keys."""
    s = str(raw)
    if " @ " in s:
        model, _, base = s.partition(" @ ")
        return _deployment_key(model, base)
    return s


def _active_cooldowns() -> dict[str, int]:
    """{deployment_key: seconds_remaining} for cooldowns still in force."""
    state = _load_state()
    out: dict[str, int] = {}
    now = _now_utc()
    for raw, entry in (state.get("cooled") or {}).items():
        if not isinstance(entry, dict):
            continue
        until_s = entry.get("cool_until_utc")
        if not until_s:
            continue
        try:
            until = _dt.datetime.fromisoformat(str(until_s).replace("Z", "+00:00"))
        except Exception:
            continue
        if until.tzinfo is None:
            until = until.replace(tzinfo=_dt.timezone.utc)
        rem = int((until - now).total_seconds())
        if rem <= 0:
            continue
        out[_key_from_raw(raw)] = rem
    return out


def _filter_cooled(deployments):
    """Drop deployments whose upstream is inside a smart-cooldown window."""
    try:
        if not deployments:
            return deployments
        active = _active_cooldowns()
        if not active:
            return deployments
        kept = []
        for dp in deployments:
            lp = (dp or {}).get("litellm_params") or {}
            key = _deployment_key(lp.get("model"), lp.get("api_base"))
            if key in active:
                continue
            kept.append(dp)
        # Fail-open: never strand a whole group with zero candidates.
        return kept or deployments
    except Exception:
        return deployments


def _record(api_base: str | None, model: str | None, category: str, cooldown_s: int, exc: str) -> None:
    state = _load_state()
    cooled: dict[str, Any] = state.setdefault("cooled", {})
    key = _deployment_key(model, api_base)
    until = _now_utc() + _dt.timedelta(seconds=cooldown_s)
    entry = cooled.get(key, {"hits": 0})
    entry.update({
        "category": category,
        "cooldown_seconds": cooldown_s,
        "cool_until_utc": until.isoformat(),
        "last_error": exc[:300],
        "last_seen_utc": _now_utc().isoformat(),
        "hits": int(entry.get("hits", 0)) + 1,
    })
    cooled[key] = entry
    _save_state(state)


_PATCH_INSTALLED = False


def install_router_patch() -> bool:
    """Make the recorded cooldown actually bite.

    Before this, `smart_cooldown` was observability-only: it wrote a state file
    while the router enforced the static `cooldown_time: 65`. A Gemini model
    whose DAILY quota was exhausted therefore came back into rotation every
    65s and failed again indefinitely (144 RESOURCE_EXHAUSTED on 2026-11-11).

    Both Router entry points must be patched — `async_get_healthy_deployments`
    is the primary pick path, `_async_get_healthy_deployments` is used by
    retries/scheduler. Patching only one silently does nothing.
    """
    global _PATCH_INSTALLED
    if _PATCH_INSTALLED:
        return True
    if not _ENFORCE:
        return False
    try:
        from litellm.router import Router  # type: ignore

        if getattr(Router, "_smart_cooldown_patched", False):
            _PATCH_INSTALLED = True
            return True

        _orig_public = Router.async_get_healthy_deployments
        _orig_private = Router._async_get_healthy_deployments

        async def async_get_healthy_deployments(self, *args, **kwargs):  # noqa: ANN001
            result = await _orig_public(self, *args, **kwargs)
            return _filter_cooled(result)

        async def _async_get_healthy_deployments(self, *args, **kwargs):  # noqa: ANN001
            result = await _orig_private(self, *args, **kwargs)
            if isinstance(result, tuple) and len(result) == 2:
                return _filter_cooled(result[0]), result[1]
            return _filter_cooled(result)

        Router.async_get_healthy_deployments = async_get_healthy_deployments
        Router._async_get_healthy_deployments = _async_get_healthy_deployments
        Router._smart_cooldown_patched = True  # type: ignore[attr-defined]
        _PATCH_INSTALLED = True
        return True
    except Exception:
        return False


def cool(model: str, hours: float = 12.0, reason: str = "manual operator park") -> None:
    """Park a model immediately (operator action)."""
    _record(None, model, "manual_park", int(hours * 3600), reason)


def clear(model: str | None = None) -> None:
    state = _load_state()
    cooled = state.setdefault("cooled", {})
    if model is None:
        cooled.clear()
    else:
        target = _deployment_key(model, None)
        for k in list(cooled):
            if _key_from_raw(k) == target or k.partition(" @ ")[0] == model:
                cooled.pop(k, None)
    _save_state(state)


def status() -> None:
    active = _active_cooldowns()
    now = _now_utc()
    state = _load_state().get("cooled") or {}
    cats = {}
    for raw, entry in state.items():
        if isinstance(entry, dict):
            cats[_key_from_raw(raw)] = str(entry.get("category", ""))
    print(f"{'deployment':58} {'remaining':>10}  category")
    print("-" * 100)
    if not active:
        print("(nothing cooled)")
    for key, rem in sorted(active.items(), key=lambda kv: -kv[1]):
        print(f"{key[:58]:58} {rem // 60:>7}m{rem % 60:02d}s  {cats.get(key, '')}")
    print(f"\nas of {now.isoformat()}  (state: {_STATE_PATH})")


class _SmartCooldown(CustomLogger):
    """LiteLLM custom logger — observes upstream failures and tags them.

    NOTE: this records the *recommended* cooldown to the state file for the
    operator. LiteLLM's router still owns the live cooldown via cooldown_time.
    To enforce the dynamic value, set litellm_settings.cooldown_time to the
    longest of the categories (24h) and rely on `_record` for visibility.
    A full enforcement variant would require monkey-patching router internals
    which is brittle across LiteLLM versions.
    """

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: D401
        try:
            litellm_params = kwargs.get("litellm_params") or {}
            api_base = litellm_params.get("api_base") or kwargs.get("api_base")
            model = kwargs.get("model")
            exc = kwargs.get("exception")
            category, cooldown_s = _classify(exc, api_base)
            _record(api_base, model, category, cooldown_s, str(exc) if exc else "")
        except Exception:
            pass

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):  # sync variant
        try:
            litellm_params = kwargs.get("litellm_params") or {}
            api_base = litellm_params.get("api_base") or kwargs.get("api_base")
            model = kwargs.get("model")
            exc = kwargs.get("exception")
            category, cooldown_s = _classify(exc, api_base)
            _record(api_base, model, category, cooldown_s, str(exc) if exc else "")
        except Exception:
            pass


smart_cooldown = _SmartCooldown()

# Apply enforcement as early as possible (proxy imports this module at boot).
install_router_patch()


if __name__ == "__main__":  # pragma: no cover - operator CLI
    import argparse

    ap = argparse.ArgumentParser(description="LiteLLM smart cooldown inspector / operator tool")
    ap.add_argument("action", nargs="?", default="status", choices=["status", "cool", "clear", "test"])
    ap.add_argument("--model", default=None, help="deployment model string, e.g. gemini/gemini-2.5-flash")
    ap.add_argument("--hours", type=float, default=12.0)
    ap.add_argument("--reason", default="manual operator park")
    a = ap.parse_args()

    if a.action == "status":
        status()
    elif a.action == "cool":
        if not a.model:
            ap.error("--model is required for 'cool'")
        cool(a.model, a.hours, a.reason)
        print(f"parked {a.model} for {a.hours}h")
        status()
    elif a.action == "clear":
        clear(a.model)
        print("cleared" + (f" {a.model}" if a.model else " all"))
        status()
    elif a.action == "test":
        samples = {
            "google_rpd": (
                "litellm.RateLimitError: vertex_ai_betaException - b'{\"error\": {\"code\": 429, \"message\": "
                "\"You exceeded your current quota, please check your plan and billing details.\", "
                "\"status\": \"RESOURCE_EXHAUSTED\"}}'",
                "https://generativelanguage.googleapis.com/v1beta",
            ),
            "google_tpm": (
                "429 RESOURCE_EXHAUSTED Quota exceeded for metric: generativelanguage.googleapis.com/"
                "generate_content_free_tier_input_token_count, limit: 250000, model: gemini-2.5-flash, "
                "description: Generate content input tokens per minute limit exceeded. Please retry in 23.7s.",
                "https://generativelanguage.googleapis.com/v1beta",
            ),
            "plain_429": ("429 Too Many Requests", "https://api.groq.com/openai/v1"),
        }
        for name, (msg, base) in samples.items():
            cat, secs = _classify(Exception(msg), base)
            print(f"{name:12} -> {cat:24} {secs}s ({secs // 60}m)")
