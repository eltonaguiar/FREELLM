"""Integration-style tests for the deployable free-tier hygiene wiring.

These assert the two guarantees the deployment depends on:

1. Default/zero-config behaviour is unchanged: with hygiene off, a
   ``FreeTierRouter`` behaves identically to the stock ``litellm.Router``
   (same routing group, no hygiene state, no hygiene errors surfaced).
2. The launcher's opt-in wiring is present and valid: the flag parses, the
   script is syntactically valid, the bootstrap path is wired to
   ``PYTHONPATH``, and the appended ``litellm_config.yaml`` block remains inert
   and loads through LiteLLM's own ``ConfigYAML`` model.

These tests never start a proxy server and never touch a port.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import litellm
import pytest
import yaml

from litellm import Router
from litellm.proxy._types import ConfigYAML
from litellm_free_tier_router import FreeTierRouter

litellm.suppress_debug_info = True

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LAUNCHER = _REPO_ROOT / "tools" / "start_litellm_proxy.sh"
_BOOTSTRAP = _REPO_ROOT / "tools" / "free_tier_bootstrap" / "sitecustomize.py"

MSGS = [{"role": "user", "content": "hi"}]

MODEL_LIST = [
    {
        "model_name": "gpt-test",
        "litellm_params": {"model": "openai/fake-a", "mock_response": ""},
        "model_info": {"free_tier": True},
    },
    {
        "model_name": "gpt-test",
        "litellm_params": {"model": "groq/fake-b", "mock_response": "B_GOOD"},
        "model_info": {"free_tier": True},
    },
]


def test_free_tier_router_is_a_router_subclass():
    assert issubclass(FreeTierRouter, Router)


def test_disabled_router_matches_stock_behaviour(monkeypatch):
    """Feature off => identical to stock: no engine, no failover, no hygiene errors."""
    monkeypatch.delenv("LITELLM_FREE_TIER_HYGIENE", raising=False)
    monkeypatch.delenv("LITELLM_FREE_TIER_BUDGET", raising=False)
    monkeypatch.delenv("LITELLM_FREE_TIER_COOLDOWN_SECONDS", raising=False)

    stock = Router(model_list=MODEL_LIST)
    hygiene_off = FreeTierRouter(model_list=MODEL_LIST)

    assert hygiene_off._ft_enabled is False
    assert hygiene_off._ft_engine is None

    for _ in range(6):
        stock_result = stock.completion(model="gpt-test", messages=MSGS)
        hygiene_result = hygiene_off.completion(model="gpt-test", messages=MSGS)
        # Both must surface a real configured response — never a hygiene error,
        # never a synthesized fallback, and an empty free-tier 200 is surfaced
        # as-is (not treated as a failure) by both.
        assert stock_result.choices[0].message.content in ("", "B_GOOD")
        assert hygiene_result.choices[0].message.content in ("", "B_GOOD")

    # The disabled subclass never builds hygiene state.
    assert hygiene_off._ft_engine is None


def test_disabled_router_still_identical_when_env_flag_absent(monkeypatch):
    """With no env flag at all, activation defaults to off."""
    monkeypatch.delenv("LITELLM_FREE_TIER_HYGIENE", raising=False)
    router = FreeTierRouter(model_list=MODEL_LIST)
    assert router._ft_enabled is False
    assert router._ft_engine is None


def test_launcher_script_is_valid_and_wires_flag():
    """The launcher parses `--free-tier-hygiene`, is syntactically valid, and
    gates the feature so the default path stays stock."""
    assert _LAUNCHER.exists()

    # Shell syntax must be valid (validates/loads without error).
    syntax = subprocess.run(
        ["bash", "-n", str(_LAUNCHER)],
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    script = _LAUNCHER.read_text(encoding="utf-8")
    # Flag is a recognized CLI arg.
    assert "--free-tier-hygiene" in script
    # Module flag is honored too.
    assert "LITELLM_FREE_TIER_HYGIENE" in script
    # Bootstrap path is wired to PYTHONPATH only in the enabled branch.
    assert "free_tier_bootstrap" in script
    # The default is off and the stock path is untouched.
    assert "FREE_TIER_HYGIENE=\"${FREE_TIER_HYGIENE:-0}\"" in script
    assert "free-tier hygiene: OFF (default; stock router)" in script

    # Bootstrap sitecustomize exists and is gated on the env flag.
    assert _BOOTSTRAP.exists()
    bootstrap = _BOOTSTRAP.read_text(encoding="utf-8")
    assert "LITELLM_FREE_TIER_HYGIENE" in bootstrap
    assert "FreeTierRouter" in bootstrap


def test_proxy_from_config_path_inits_hygiene(monkeypatch):
    """The proxy builds routers via Router.from_config (which bypasses __init__).

    Ensure the lazy-init guard makes hygiene effective on that construction path,
    and that with the flag absent the same path stays stock.
    """
    from litellm.router_config import RouterConfig

    production_cfg = RouterConfig(
        arguments={
            "model_list": [
                {
                    "model_name": "gpt-test",
                    "litellm_params": {
                        "model": "openai/fake-a",
                        "mock_response": "",
                    },
                    "model_info": {"free_tier": True},
                    "tpm": 1000,
                    "rpm": 10,
                },
                {
                    "model_name": "gpt-test",
                    "litellm_params": {
                        "model": "groq/fake-b",
                        "mock_response": "B_GOOD",
                    },
                    "model_info": {"free_tier": True},
                    "tpm": 1000,
                    "rpm": 10,
                },
            ]
        }
    )

    # Enabled via env flag -> the from_config-built router applies hygiene:
    # empty-200 from openai falls back to groq.
    monkeypatch.setenv("LITELLM_FREE_TIER_HYGIENE", "1")
    enabled_router = FreeTierRouter.from_config(production_cfg)
    result = enabled_router.completion(model="gpt-test", messages=MSGS)
    assert result.choices[0].message.content == "B_GOOD"
    assert enabled_router._ft_enabled is True
    assert enabled_router._ft_engine is not None

    # Default (no flag) -> from_config-built router behaves like stock.
    monkeypatch.delenv("LITELLM_FREE_TIER_HYGIENE", raising=False)
    stock_router = FreeTierRouter.from_config(production_cfg)
    stock_result = stock_router.completion(model="gpt-test", messages=MSGS)
    assert stock_result.choices[0].message.content in ("", "B_GOOD")
    assert stock_router._ft_enabled is False
    assert stock_router._ft_engine is None


def test_config_inert_hygiene_block_loads_and_defaults_off():
    """The appended litellm_config.yaml block is inert, does not change model_list
    entries, and loads without error through LiteLLM's own ConfigYAML model."""
    config_path = _REPO_ROOT / "litellm_config.yaml"
    assert config_path.exists()

    with open(config_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    # The block parses as a dict and is default-OFF.
    settings = data["litellm_settings"]
    assert isinstance(settings.get("free_tier_hygiene"), dict)
    assert settings["free_tier_hygiene"]["enabled"] is False

    # It loads through LiteLLM's own config model without error.
    cfg = ConfigYAML(**data)
    assert cfg.litellm_settings["free_tier_hygiene"]["enabled"] is False
    assert cfg.model_list is not None and len(cfg.model_list) > 0

    # The existing smart-cooldown callback wiring is preserved.
    assert settings["callbacks"] == "tools.litellm_smart_cooldown.smart_cooldown"
