#!/usr/bin/env python3
"""Discover free models from gateway catalogs and merge them into a LiteLLM config.

Why this exists
---------------
The static `litellm_config.yaml` hardcodes model IDs. When an upstream retires or
renames a model the proxy keeps routing to it and every request 410s/404s (e.g.
`nvidia_nim/meta/llama-3.1-8b-instruct` was retired 2026-08-26). Gateway
catalogs, by contrast, are always current — so we re-read them on every proxy
start instead of trusting a frozen list.

Providers
---------
  kilo      https://ai-gateway.kilo.ai/api/v1/models   — NO AUTH REQUIRED.
            (anonymous: ~200 req/hour per IP; with KILOCODE_API_KEY: higher)
  opencode  https://opencode.ai/zen/v1/models          — needs OPENCODE_API_KEY.
            Free-tier models are 403 when called outside the app, and an expired
            key reports api_key_expired; both cases are skipped, never fatal.

Output
------
A merged copy of the base config with extra `model_list` entries appended to
dedicated groups (`ULTRA_FREE_KILO`, `ULTRA_FREE_OPENCODE`) which are then
registered as fallbacks of the strict-free cloud groups.

Usage
-----
  python3 tools/litellm_dynamic_models.py --base litellm_config.yaml \
          --out /tmp/litellm_config.dynamic.yaml
  python3 tools/litellm_dynamic_models.py --dry-run     # print what it found

Exit codes
----------
  0  at least one provider contributed models; merged config written
  1  nothing discovered (caller should keep using the static config)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover
    print("[dynamic-models] requests not available", file=sys.stderr)
    raise SystemExit(1)

try:
    import yaml
except Exception:  # pragma: no cover
    print("[dynamic-models] PyYAML not available", file=sys.stderr)
    raise SystemExit(1)

KILO_BASE = "https://ai-gateway.kilo.ai/api/v1"
OPENCODE_BASES = ("https://opencode.ai/zen/v1", "https://opencode.ai/inference/openai/v1")

# Groups that should be able to fall back onto freshly discovered free models.
FALLBACK_TARGETS = ("ULTRA_FREE_CLOUD", "ULTRA_FREE_CLOUD_FAST")

DEFAULT_OUT_TOKENS = 8192
HTTP_TIMEOUT = 20


def _log(msg: str) -> None:
    print(f"[dynamic-models] {msg}", file=sys.stderr)


def _pricing_is_free(entry: dict[str, Any]) -> bool:
    """A model is free only if the catalog says every price component is 0/absent."""
    pricing = entry.get("pricing") or {}
    if not isinstance(pricing, dict):
        return False
    for key in ("prompt", "completion"):
        raw = pricing.get(key)
        if raw in (None, "", "0", 0, "0.0", 0.0):
            continue
        try:
            if float(raw) > 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _extract_ids(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("data", "models", "results"):
            if isinstance(payload.get(key), list):
                return [x for x in payload[key] if isinstance(x, dict)]
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


@dataclass(frozen=True)
class ProviderSpec:
    """One OpenAI-compatible gateway we can discover free models from."""

    name: str
    base: str
    key_env: str | None      # None => anonymous works
    group: str               # LiteLLM model_name to write the entries under
    rpm_env: str
    needs_key: bool
    probe_candidates: int = 2


PROVIDERS: dict[str, ProviderSpec] = {
    "kilo": ProviderSpec("kilo", KILO_BASE, "KILOCODE_API_KEY", "ULTRA_FREE_KILO", "KILO_RPM",
                         needs_key=False),
    "opencode": ProviderSpec("opencode", OPENCODE_BASES[0], "OPENCODE_API_KEY", "ULTRA_FREE_OPENCODE",
                             "OPENCODE_RPM", needs_key=True),
}


def _auth_headers(spec: ProviderSpec) -> tuple[dict[str, str], str | None]:
    key = os.environ.get(spec.key_env) if spec.key_env else None
    return ({"Authorization": f"Bearer {key}"} if key else {}), key


def _is_free_id(mid: str) -> bool:
    m = mid.lower()
    return ":free" in m or m.endswith("/free") or m.endswith("-free") or m.startswith("free")


def _list_models(spec: ProviderSpec) -> list[dict[str, Any]]:
    headers, _ = _auth_headers(spec)
    try:
        r = requests.get(f"{spec.base}/models", headers=headers, timeout=HTTP_TIMEOUT)
    except Exception as exc:
        _log(f"{spec.name}: list failed ({str(exc)[:70]})")
        return []
    if r.status_code != 200:
        try:
            detail = str(r.json().get("error") or "")[:90]
        except Exception:
            detail = r.text[:90]
        _log(f"{spec.name}: list HTTP {r.status_code} {detail}")
        return []
    return _extract_ids(r.json())


def _probe_generation(spec: ProviderSpec, model_id: str) -> tuple[bool, str]:
    """One cheap completion — proof that the model actually serves inference.

    A 200 from /models does NOT mean inference works. Three distinct traps this
    catches:
      * OpenCode lists ~11 `*-free` models, then 429 FreeUsageLimitError for any
        external client (free tier is gated to the official app).
      * A gateway can list a model that is retired upstream (NVIDIA returned
        HTTP 410 Gone for months for `meta/llama-3.1-8b-instruct`).
      * An account can be alive but unfunded (402 / NOT_ENOUGH_BALANCE).
    """
    headers, key = _auth_headers(spec)
    headers["Content-Type"] = "application/json"
    try:
        r = requests.post(
            f"{spec.base}/chat/completions",
            json={"model": model_id, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 8},
            headers=headers,
            timeout=60,
        )
    except Exception as exc:
        return False, f"probe error: {str(exc)[:60]}"
    if r.status_code == 200:
        return True, "ok"
    return False, f"HTTP {r.status_code} {r.text[:110].replace(chr(10), ' ')}"


def discover(spec: ProviderSpec, probe: bool = True) -> tuple[list[dict[str, Any]], str]:
    """List -> keep free -> PROVE inference. Returns (free_models, status)."""
    _, key = _auth_headers(spec)
    if spec.needs_key and not key:
        _log(f"{spec.name}: no {spec.key_env} set — skipped")
        return [], "no key"

    entries = _list_models(spec)
    free = [
        e for e in entries
        if e.get("id") and _is_free_id(str(e["id"])) and _pricing_is_free(e)
    ]
    _log(f"{spec.name}: {len(entries)} listed, {len(free)} free")
    if not free:
        return [], "no free models"
    if not probe:
        return free, "unprobed"

    # Try more than one candidate: the first may be cold-starting or briefly
    # rate-limited while another free model on the same gateway serves fine.
    for cand in free[: max(1, spec.probe_candidates)]:
        ok, detail = _probe_generation(spec, str(cand["id"]))
        if ok:
            _log(f"{spec.name}: generation verified via {cand['id']} ({len(free)} enrolled)")
            return free, "verified"
        _log(f"{spec.name}: probe {cand['id']} failed ({detail})")
    _log(f"{spec.name}: no free model could generate — provider skipped")
    return [], "probe failed"


def _entry(model_name: str, real_id: str, api_base: str, key_env: str, rpm: int,
           ctx: int | None, out_tokens: int | None, supports_tools: bool) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": f"openai/{real_id}",
        "api_base": api_base,
        "api_key": f"os.environ/{key_env}",
        # simple-shuffle is WEIGHTED BY rpm — an entry with no rpm is effectively
        # never picked, so this field is mandatory for these to appear in rotation.
        "rpm": rpm,
    }
    info: dict[str, Any] = {
        "max_input_tokens": ctx or 128000,
        "max_output_tokens": out_tokens or DEFAULT_OUT_TOKENS,
        "supports_function_calling": bool(supports_tools),
    }
    return {"model_name": model_name, "litellm_params": params, "model_info": info}


def build_entries(spec: ProviderSpec, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rpm = int(os.environ.get(spec.rpm_env, "3"))
    out = []
    for e in entries:
        mid = str(e["id"])
        top = e.get("top_provider") or {}
        ctx = e.get("context_length") or top.get("context_length")
        max_out = e.get("max_completion_tokens") or top.get("max_completion_tokens")
        params_supported = e.get("supported_parameters") or []
        supports_tools = any(str(p).lower() in ("tools", "tool_choice", "function_calling")
                             for p in params_supported)
        env = spec.key_env or ""
        out.append(_entry(spec.group, mid, spec.base, env, rpm, ctx, max_out, supports_tools))
    return out


def merge(base_path: Path, kilo_entries: list[dict[str, Any]], oc_entries: list[dict[str, Any]]) -> dict[str, Any]:
    cfg = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("model_list", [])

    added: list[str] = []
    if kilo_entries:
        cfg["model_list"].extend(kilo_entries)
        added.append("ULTRA_FREE_KILO")
    if oc_entries:
        cfg["model_list"].extend(oc_entries)
        added.append("ULTRA_FREE_OPENCODE")

    if added:
        fb = cfg.setdefault("router_settings", {}).setdefault("fallbacks", [])
        for target in FALLBACK_TARGETS:
            for item in fb:
                if isinstance(item, dict) and target in item:
                    current = item[target] or []
                    for group in added:
                        if group not in current:
                            current.append(group)
                    item[target] = current
    return cfg


def verify_static(base_path: Path, workers: int = 8, timeout: int = 30,
                  only: str | None = None) -> int:
    """Probe every deployment in a static LiteLLM config and report dead ones.

    This is the same "does it actually generate?" gate as `discover()`, applied
    to the hand-written entries. It catches the failure mode that motivated this
    tool: `nvidia_nim/meta/llama-3.1-8b-instruct` sat in the config returning
    HTTP 410 Gone long after NVIDIA retired it on 2026-08-26.

    Uses litellm.completion directly so provider→base-URL mapping matches exactly
    what the proxy does at runtime.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    import litellm

    cfg = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    deployments = [
        m for m in cfg.get("model_list", [])
        if isinstance(m, dict) and m.get("litellm_params", {}).get("model")
    ]
    if only:
        deployments = [d for d in deployments if only in str(d["litellm_params"]["model"])]
    if not deployments:
        _log("verify-static: no deployments matched")
        return 1

    def check(dep: dict[str, Any]) -> tuple[str, str, str]:
        p = dict(dep["litellm_params"])
        group = dep.get("model_name", "?")
        real = p.pop("model")
        key = p.get("api_key")
        if isinstance(key, str) and key.startswith("os.environ/"):
            p["api_key"] = os.environ.get(key.split("/", 1)[1], "")
        p.setdefault("timeout", timeout)
        try:
            litellm.completion(
                model=real,
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=8,
                **{k: v for k, v in p.items() if k != "rpm"},
            )
            return group, real, "OK"
        except Exception as exc:
            msg = str(exc).replace("\n", " ")
            return group, real, msg[:150]

    dead, alive = [], 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(check, d) for d in deployments]
        for fut in as_completed(futures):
            group, real, status = fut.result()
            if status == "OK":
                alive += 1
            else:
                dead.append((group, real, status))

    print(f"\nverified {len(deployments)} deployments: {alive} OK, {len(dead)} DEAD\n")
    for group, real, status in sorted(dead):
        print(f"  DEAD  {group:28} {real:52} {status}")
    if dead:
        print("\nSuggested action: comment out the DEAD entries in the config, or move "
              "the provider behind a live gateway so discovery can replace it.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge live gateway free models into a LiteLLM config")
    ap.add_argument("--base", default="litellm_config.yaml")
    ap.add_argument("--out", default="/tmp/litellm_config.dynamic.yaml")
    ap.add_argument("--providers", default="kilo,opencode")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-probe", action="store_true",
                    help="skip the OpenCode generation probe (may enrol non-working free models)")
    ap.add_argument("--summary", default="/tmp/litellm_dynamic_models.json")
    ap.add_argument("--verify-static", action="store_true",
                    help="probe every deployment in --base and report dead ones instead of merging")
    ap.add_argument("--only", default=None, help="with --verify-static: substring filter on model id")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    if args.verify_static:
        return verify_static(Path(args.base), workers=args.workers,
                             timeout=args.timeout, only=args.only)

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    kilo_entries: list[dict[str, Any]] = []
    oc_entries: list[dict[str, Any]] = []

    for pname in providers:
        spec = PROVIDERS.get(pname)
        if spec is None:
            _log(f"unknown provider '{pname}' — known: {', '.join(PROVIDERS)}")
            continue
        models, status = discover(spec, probe=not args.no_probe)
        if not models:
            _log(f"{pname}: nothing enrolled ({status})")
            continue
        built = build_entries(spec, models)
        if pname == "kilo":
            kilo_entries = built
        elif pname == "opencode":
            oc_entries = built
        else:
            kilo_entries = kilo_entries + built

    total = len(kilo_entries) + len(oc_entries)
    if total == 0:
        _log("no free models discovered — leaving config untouched")
        return 1

    summary = [e["litellm_params"]["model"].replace("openai/", "") for e in (kilo_entries + oc_entries)]
    if args.dry_run:
        print(json.dumps(summary, indent=2))
        return 0

    base = Path(args.base)
    if not base.exists():
        _log(f"base config not found: {base}")
        return 1

    cfg = merge(base, kilo_entries, oc_entries)
    out = Path(args.out)
    out.write_text(
        "# AUTO-GENERATED by tools/litellm_dynamic_models.py — do not edit.\n"
        f"# base: {base}   free models: {total}\n"
        + yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )

    try:
        Path(args.summary).write_text(json.dumps({
            "generated_utc": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc).isoformat(),
            "count": total,
            "groups": {
                "ULTRA_FREE_KILO": len(kilo_entries),
                "ULTRA_FREE_OPENCODE": len(oc_entries),
            },
            "models": summary,
        }, indent=2), encoding="utf-8")
    except Exception:
        pass

    _log(f"wrote {out} (+{total} free models)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
