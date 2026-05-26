#!/usr/bin/env python3
"""
Check whether Hugging Face models are usable through the HF Router path used by FREELLM.

For each model ID, reports:
- hub_exists: whether https://huggingface.co/api/models/<id> exists
- router_callable: whether HF Router exposes an ID that maps to this model
- probe_status: whether a small chat prompt succeeds via HF Router
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0 Safari/537.36"
KEYS_FILE = Path.home() / "dbpasses.txt"


def parse_key(label: str) -> str:
    if not KEYS_FILE.exists():
        return ""

    lines = KEYS_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    found = False
    for raw in lines:
        line = raw.rstrip()
        if line == label:
            found = True
            continue

        if found and line.strip():
            cand = line.strip()
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", cand):
                continue
            if cand.endswith(":"):
                continue
            if cand.startswith("http://") or cand.startswith("https://"):
                continue
            if cand.startswith("==") or cand.startswith("--"):
                continue
            return cand

    return ""


def http_get_json(url: str, headers: dict[str, str], timeout: int = 45) -> tuple[int, Any]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return resp.getcode(), data


def http_post_json(url: str, headers: dict[str, str], payload: dict[str, Any], timeout: int = 90) -> tuple[int, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return resp.getcode(), data


def fetch_router_catalog(hf_key: str) -> list[str]:
    code, data = http_get_json(
        "https://router.huggingface.co/v1/models",
        headers={
            "Authorization": f"Bearer {hf_key}",
            "User-Agent": UA,
            "Accept": "application/json",
        },
    )
    if code != 200:
        return []
    return [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]


def possible_router_ids(model_id: str, router_ids: list[str]) -> list[str]:
    hits: list[str] = []

    for rid in router_ids:
        if rid == model_id:
            hits.append(rid)
            continue

        base = rid
        if base.startswith("openai/"):
            base = base[len("openai/") :]
        base = base.split(":", 1)[0]
        if base == model_id:
            hits.append(rid)

    return hits


def check_one(model_id: str, hf_key: str, router_ids: list[str]) -> dict[str, str]:
    out = {
        "model": model_id,
        "hub_exists": "no",
        "router_callable": "no",
        "probe_status": "n/a",
        "probe_note": "",
    }

    # 1) Hub existence
    try:
        code, _ = http_get_json(
            f"https://huggingface.co/api/models/{model_id}",
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=30,
        )
        out["hub_exists"] = "yes" if code == 200 else f"http-{code}"
    except urllib.error.HTTPError as exc:
        out["hub_exists"] = f"http-{exc.code}"
    except Exception as exc:
        out["hub_exists"] = f"error-{type(exc).__name__}"

    # 2) Router callable IDs
    candidates = possible_router_ids(model_id, router_ids)
    if candidates:
        out["router_callable"] = "yes"
    else:
        out["router_callable"] = "no"
        return out

    # 3) Live probe via HF router chat
    probe_model = candidates[0]
    payload = {
        "model": probe_model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    try:
        code, body = http_post_json(
            "https://router.huggingface.co/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {hf_key}",
                "Content-Type": "application/json",
                "User-Agent": UA,
            },
            payload=payload,
        )
        out["probe_status"] = str(code)
        choice = (body.get("choices") or [{}])[0] if isinstance(body, dict) else {}
        msg = (choice.get("message") or {}) if isinstance(choice, dict) else {}
        text = msg.get("content") if isinstance(msg, dict) else ""
        out["probe_note"] = (text or "").strip()[:80]
    except urllib.error.HTTPError as exc:
        out["probe_status"] = str(exc.code)
        out["probe_note"] = exc.read().decode("utf-8", errors="replace").replace("\n", " ")[:120]
    except Exception as exc:
        out["probe_status"] = f"error-{type(exc).__name__}"
        out["probe_note"] = str(exc)[:120]

    return out


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Check if HF model IDs are callable via HF router.")
    p.add_argument("models", nargs="*", help="HF model IDs to check")
    p.add_argument("--file", help="Path to file with one model ID per line")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    models: list[str] = []
    if args.file:
        p = Path(args.file)
        if not p.exists():
            print(f"File not found: {p}")
            return 1
        models.extend([x.strip() for x in p.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()])

    models.extend(args.models)
    models = [m for m in models if m]
    if not models:
        print("Provide model IDs as args or --file.")
        return 2

    hf_key = (
        parse_key("HUGGING_FACE_TOKEN ALT(TRIED FINE GRAIN AND SETTING BUNCH OF CHECKBOXES):")
        or parse_key("HUGGING_FACE_TOKEN")
        or parse_key("HUGGINF_FACE TOKEN ALT2(READ):")
    )
    if not hf_key:
        print("No Hugging Face key found in ~/dbpasses.txt")
        return 1

    router_ids = fetch_router_catalog(hf_key)
    if not router_ids:
        print("Unable to fetch HF router model catalog (or empty catalog).")
        return 1

    print(f"router_catalog_size: {len(router_ids)}")
    for model in models:
        result = check_one(model, hf_key, router_ids)
        print(f"MODEL {result['model']}")
        print(f"  hub_exists:      {result['hub_exists']}")
        print(f"  router_callable: {result['router_callable']}")
        print(f"  probe_status:    {result['probe_status']}")
        note = result["probe_note"]
        if note:
            print(f"  probe_note:      {note}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
