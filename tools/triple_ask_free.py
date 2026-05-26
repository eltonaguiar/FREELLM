#!/usr/bin/env python3
"""
Ask multiple free LiteLLM aliases and print each reply with upstream metadata.

Usage:
  __TRIPLEASKFREE -q "your question"
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse


DEFAULT_BASE_URL = "http://localhost:4000/v1"
DEFAULT_ALIASES = [
    "free-mode",
    "free-mode-fast",
    "free-mode-tools",
    "free-mode-large",
]


@dataclass
class AskResult:
    alias: str
    status: int
    finish_reason: str | None
    upstream_model: str | None
    returned_model: str | None
    upstream_api_base: str | None
    upstream_provider: str | None
    upstream_host: str | None
    reply: str
    error: str | None


def infer_provider(api_base: str | None) -> tuple[str | None, str | None]:
    if not api_base:
        return None, None

    try:
        host = (urlparse(api_base).hostname or "").lower()
    except Exception:
        host = ""

    if not host:
        return None, None

    mapping = [
        ("api.groq.com", "Groq"),
        ("api.cerebras.ai", "Cerebras"),
        ("integrate.api.nvidia.com", "NVIDIA NIM"),
        ("generativelanguage.googleapis.com", "Google Gemini"),
        ("models.inference.ai.azure.com", "GitHub Models (Azure)"),
        ("api.deepinfra.com", "DeepInfra"),
        ("inference-api.nousresearch.com", "Nous Research"),
        ("api.mistral.ai", "Mistral"),
        ("api.orcarouter.ai", "Orcarouter"),
        ("api.x.ai", "xAI"),
        ("dashscope-intl.aliyuncs.com", "Alibaba Model Studio (DashScope Intl)"),
        ("token-plan.ap-southeast-1.maas.aliyuncs.com", "Alibaba Model Studio (Token Plan SG)"),
        ("api.together.xyz", "Together AI"),
        ("api.fireworks.ai", "Fireworks"),
        ("api.aimlapi.com", "AIMLAPI"),
        ("api.hypereal.cloud", "Hypereal"),
        ("api.bluesminds.com", "Bluesmind"),
        ("kilocode.ai", "Kilocode"),
        ("opencode.ai", "Opencode"),
        ("api.ofox.ai", "OFOX"),
        ("api.llm7.io", "LLM7"),
    ]

    for needle, provider in mapping:
        if needle in host:
            return provider, host

    return "Unknown", host


def post_chat(base_url: str, api_key: str, model_alias: str, question: str, timeout: int, max_tokens: int) -> AskResult:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model_alias,
        "messages": [{"role": "user", "content": question}],
        "temperature": 0.3,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    headers: dict[str, str] = {}
    status = 0
    raw = ""

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            headers = {k.lower(): v for k, v in resp.headers.items()}
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
        raw = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return AskResult(
            alias=model_alias,
            status=0,
            finish_reason=None,
            upstream_model=None,
            returned_model=None,
            upstream_api_base=None,
            upstream_provider=None,
            upstream_host=None,
            reply="",
            error=f"Connection failed: {exc}",
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {"_raw": raw}

    choice0 = ((data.get("choices") or [{}])[0] if isinstance(data, dict) else {})
    message = (choice0.get("message") or {}) if isinstance(choice0, dict) else {}
    reply = message.get("content") if isinstance(message, dict) else ""
    if reply is None:
        reply = ""
    elif not isinstance(reply, str):
        reply = str(reply)

    upstream_api_base = headers.get("x-litellm-model-api-base")
    provider, host = infer_provider(upstream_api_base)

    upstream_model = headers.get("x-litellm-model") or headers.get("x-litellm-model-id")
    returned_model = data.get("model") if isinstance(data, dict) else None

    error = None
    if status != 200:
        if isinstance(data, dict) and data.get("error") is not None:
            error = str(data.get("error"))
        else:
            error = f"HTTP {status}"

    return AskResult(
        alias=model_alias,
        status=status,
        finish_reason=choice0.get("finish_reason") if isinstance(choice0, dict) else None,
        upstream_model=upstream_model,
        returned_model=returned_model,
        upstream_api_base=upstream_api_base,
        upstream_provider=provider,
        upstream_host=host,
        reply=reply,
        error=error,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask 3 free models and print upstream model info.")
    parser.add_argument("-q", "--query", required=True, help="Question to ask")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="LiteLLM base URL")
    parser.add_argument("--api-key", default="anything", help="Proxy API key placeholder")
    parser.add_argument(
        "--aliases",
        default=",".join(DEFAULT_ALIASES),
        help="Comma-separated alias candidates (will pick first 3 distinct upstream models)",
    )
    parser.add_argument("--target", type=int, default=3, help="Target number of distinct upstream model replies")
    parser.add_argument("--timeout", type=int, default=60, help="Timeout seconds per request")
    parser.add_argument("--max-tokens", type=int, default=700, help="Max tokens per response")
    parser.add_argument(
        "--empty-retries",
        type=int,
        default=1,
        help="Retries per alias when reply is empty and finish_reason=length",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow empty 200 responses to count toward target (default: skip empties)",
    )
    return parser.parse_args()


def print_one(index: int, result: AskResult) -> None:
    print(f"[{index}] alias: {result.alias}")
    print(f"    upstream_model:   {result.upstream_model or 'unknown'}")
    print(f"    returned_model:   {result.returned_model or 'unknown'}")
    print(f"    upstream_provider:{result.upstream_provider or 'unknown'}")
    print(f"    upstream_host:    {result.upstream_host or 'unknown'}")
    print(f"    http_status:      {result.status}")
    print(f"    finish_reason:    {result.finish_reason or 'unknown'}")
    if result.error:
        print(f"    error:            {result.error}")
    print("    reply:")
    body = result.reply.strip() or "(empty reply)"
    for line in body.splitlines() or [body]:
        print(f"      {line}")
    print()


def main() -> int:
    args = parse_args()
    aliases = [x.strip() for x in args.aliases.split(",") if x.strip()]
    if not aliases:
        print("No aliases provided.")
        return 1

    target = max(1, args.target)
    selected: list[AskResult] = []
    failures: list[AskResult] = []
    seen_upstream: set[str] = set()
    skipped_empty: list[str] = []
    skipped_duplicate_upstream: list[str] = []
    empty_retry_success: list[str] = []

    for alias in aliases:
        result = post_chat(
            base_url=args.base_url,
            api_key=args.api_key,
            model_alias=alias,
            question=args.query,
            timeout=args.timeout,
            max_tokens=args.max_tokens,
        )

        if result.status != 200:
            failures.append(result)
            continue

        if not args.allow_empty and not result.reply.strip():
            recovered = False
            retry_budget = max(0, args.empty_retries)
            if result.finish_reason == "length" and retry_budget > 0:
                for _ in range(retry_budget):
                    retry_max_tokens = min(max(args.max_tokens * 4, args.max_tokens + 400), 4096)
                    retried = post_chat(
                        base_url=args.base_url,
                        api_key=args.api_key,
                        model_alias=alias,
                        question=args.query,
                        timeout=args.timeout,
                        max_tokens=retry_max_tokens,
                    )
                    if retried.status == 200 and retried.reply.strip():
                        result = retried
                        empty_retry_success.append(alias)
                        recovered = True
                        break
            if not recovered:
                skipped_empty.append(alias)
                continue

        upstream_key = result.upstream_model or result.returned_model or f"alias:{alias}"
        if upstream_key in seen_upstream:
            skipped_duplicate_upstream.append(alias)
            continue

        seen_upstream.add(upstream_key)
        selected.append(result)
        if len(selected) >= target:
            break

    if not selected:
        print("No successful results returned.")
        if failures:
            print("failed_aliases:")
            for fail in failures:
                err = (fail.error or "unknown error").replace("\n", " ")
                print(f"  - {fail.alias}: status={fail.status} error={err[:220]}")
        return 1

    print(f"query: {args.query}")
    print(f"target_distinct_upstream_models: {target}")
    print(f"aliases_scanned: {', '.join(aliases)}")
    print()

    for i, item in enumerate(selected, start=1):
        print_one(i, item)

    if skipped_empty:
        print(f"skipped_empty_aliases: {', '.join(skipped_empty)}")
    if empty_retry_success:
        print(f"empty_recovery_aliases: {', '.join(empty_retry_success)}")
    if skipped_duplicate_upstream:
        print(f"skipped_duplicate_upstream_aliases: {', '.join(skipped_duplicate_upstream)}")
    if failures:
        print("failed_aliases:")
        for fail in failures:
            err = (fail.error or "unknown error").replace("\n", " ")
            print(f"  - {fail.alias}: status={fail.status} error={err[:220]}")

    distinct = len({(x.upstream_model or x.returned_model or x.alias) for x in selected})
    if distinct < target:
        print(
            f"Warning: only {distinct} distinct upstream model(s) were found; "
            f"requested {target}. Consider expanding --aliases."
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
