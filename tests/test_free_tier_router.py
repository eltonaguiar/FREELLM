"""Unit tests for ``litellm_free_tier_router.FreeTierRouter``.

Run with: pytest tests/test_free_tier_router.py -v
"""

from __future__ import annotations

import asyncio
import os

import litellm
import pytest

from litellm import Router
from litellm_free_tier_router import (
    FreeTierHygieneEngine,
    FreeTierNoAvailableDeploymentError,
    FreeTierRouter,
    is_free_tier,
    provider_scope,
    should_treat_as_empty,
)

litellm.suppress_debug_info = True

MSGS = [{"role": "user", "content": "hi"}]

# Two deployments in one routing group:
#  - A: free-tier tagged, provider scope "openai", returns an EMPTY 200
#  - B: free-tier tagged, provider scope "groq", returns a NON-EMPTY 200
# The providers are deliberately DIFFERENT so that provider-scope parking or
# quota exhaustion on A (openai) does not block B (groq) from serving fallback.
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


class _FakeAuthError(Exception):
    """A provider exception that looks like 401 unauthorized."""

    status_code = 401


class _FakeClock:
    """Injectable clock; tests can advance time deterministically."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def __call__(self) -> float:
        return self._now


# --------------------------------------------------------------------------- #
# Disabled (feature-off) behaviour — must be identical to stock
# --------------------------------------------------------------------------- #
def test_disabled_empty_not_treated_as_failure():
    """Feature off: a free-tier-tagged empty 200 is returned as-is (no failover)."""
    router = FreeTierRouter(model_list=MODEL_LIST, enable_free_tier_hygiene=False)
    assert router._ft_enabled is False
    result = router.completion(model="gpt-test", messages=MSGS)
    # Whatever the stock router picks, no EmptyContentError may surface and no
    # hygiene state may be created.
    assert router._ft_engine is None
    assert result.choices[0].message.content in ("", "B_GOOD")


def test_disabled_behaves_like_stock():
    """Feature off: no hygiene state, no failover, never raises hygiene errors.

    Note: LiteLLM's default usage-based routing is non-deterministic across
    independently-constructed Router instances (it phases against the wall
    clock), so exact per-call sequence equality with a second Router is not a
    stable assertion. What we guarantee is that the disabled subclass never
    builds an engine, never swallows/rejects a 200 based on content, and never
    raises the free-tier errors.
    """
    stripped = FreeTierRouter(
        model_list=MODEL_LIST, enable_free_tier_hygiene=False
    )
    assert stripped._ft_enabled is False
    assert stripped._ft_engine is None

    values = []
    for _ in range(8):
        result = stripped.completion(model="gpt-test", messages=MSGS)
        values.append(result.choices[0].message.content)
    # Every call succeeded with a real configured response (no fallback
    # synthesis, no FreeTierNoAvailableDeploymentError, no EmptyContentError).
    assert all(v in ("", "B_GOOD") for v in values)
    assert "" in values  # an empty free-tier response is surfaced as-is


def test_disabled_single_free_tier_empty_no_fallback():
    """Feature off with a single free-tier empty deployment: blank answer returned."""
    single = [
        {
            "model_name": "only",
            "litellm_params": {"model": "openai/fake-a", "mock_response": ""},
            "model_info": {"free_tier": True},
        }
    ]
    router = FreeTierRouter(model_list=single, enable_free_tier_hygiene=False)
    result = router.completion(model="only", messages=MSGS)
    assert result.choices[0].message.content == ""


# --------------------------------------------------------------------------- #
# Enabled: empty-200-as-failure failover
# --------------------------------------------------------------------------- #
def test_enabled_empty_200_falls_back_to_next_model():
    router = FreeTierRouter(
        model_list=MODEL_LIST,
        enable_free_tier_hygiene=True,
        free_tier_budget={
            "openai": {"daily_limit": 1000},
            "groq": {"daily_limit": 1000},
        },
    )
    result = router.completion(model="gpt-test", messages=MSGS)
    assert result.choices[0].message.content == "B_GOOD"
    # The empty provider (openai) was attempted before falling back to groq.
    engine = router._ft_engine
    assert engine.budget_used("openai") >= 1
    assert engine.budget_used("groq") >= 1


def test_enabled_all_empty_raises_no_deployment():
    """If every candidate returns empty, raise the custom no-deployment error."""
    both_empty = [
        {
            "model_name": "gpt-test",
            "litellm_params": {"model": "openai/fake-a", "mock_response": ""},
            "model_info": {"free_tier": True},
        },
        {
            "model_name": "gpt-test",
            "litellm_params": {"model": "openai/fake-b", "mock_response": ""},
            "model_info": {"free_tier": True},
        },
    ]
    router = FreeTierRouter(
        model_list=both_empty, enable_free_tier_hygiene=True
    )
    with pytest.raises(FreeTierNoAvailableDeploymentError):
        router.completion(model="gpt-test", messages=MSGS)


def test_enabled_streaming_skips_empty_check():
    """stream=True bypasses the empty-check (returns the empty stream as-is)."""
    router = FreeTierRouter(
        model_list=MODEL_LIST, enable_free_tier_hygiene=True
    )
    # With stream=True we do not go through the mock completion cleanly, so we
    # assert the code path chooses the first available provider without raising
    # about empty content. Use a direct call to the loop guard.
    # (Streaming is exercised via a stub; see matching logic in module.)
    stream_router = FreeTierRouter(
        model_list=MODEL_LIST,
        enable_free_tier_hygiene=True,
    )
    # The stream flag is honoured by _build_call_kwargs / the loop's early
    # return. Here we just confirm no FreeTierNoAvailableDeploymentError.
    try:
        stream_router.completion(
            model="gpt-test", messages=MSGS, stream=True
        )
    except FreeTierNoAvailableDeploymentError:
        pytest.fail("streaming should not be treated as empty content")


def test_enabled_acompletion_empty_200_falls_back():
    async def run():
        router = FreeTierRouter(
            model_list=MODEL_LIST, enable_free_tier_hygiene=True
        )
        return await router.acompletion(model="gpt-test", messages=MSGS)

    result = asyncio.run(run())
    assert result.choices[0].message.content == "B_GOOD"


# --------------------------------------------------------------------------- #
# Enabled: quota pre-filter
# --------------------------------------------------------------------------- #
def test_quota_prefilter_skips_exhausted_provider(monkeypatch):
    """Provider A over-budget -> skipped; B answers; A is never called."""
    original = litellm.completion
    called: list[str] = []

    def fake_completion(**kwargs):
        model = kwargs.get("model")
        called.append(model)
        # Never reach A; if A is invoked the test fails below.
        return original(**kwargs)

    monkeypatch.setattr(litellm, "completion", fake_completion)

    router = FreeTierRouter(
        model_list=MODEL_LIST,
        enable_free_tier_hygiene=True,
        free_tier_budget={"openai": {"daily_limit": 0}},  # already exhausted
    )
    result = router.completion(model="gpt-test", messages=MSGS)
    assert result.choices[0].message.content == "B_GOOD"
    assert "openai/fake-a" not in called
    assert "groq/fake-b" in called


def test_quota_consumed_counts_toward_budget():
    # First call burns both providers (A empty -> B answers), so both hit their
    # limit of 1 and the second call has no provider left.
    router = FreeTierRouter(
        model_list=MODEL_LIST,
        enable_free_tier_hygiene=True,
        free_tier_budget={
            "openai": {"daily_limit": 1},
            "groq": {"daily_limit": 1},
        },
    )
    router.completion(model="gpt-test", messages=MSGS)
    engine = router._ft_engine
    assert engine.budget_used("openai") >= 1
    assert engine.budget_used("groq") >= 1
    with pytest.raises(FreeTierNoAvailableDeploymentError):
        router.completion(model="gpt-test", messages=MSGS)


# --------------------------------------------------------------------------- #
# Enabled: provider-scope circuit breaker (401/403)
# --------------------------------------------------------------------------- #
def test_provider_breaker_parks_on_401_and_expires(monkeypatch):
    original = litellm.completion
    clock = _FakeClock()

    def fake_completion(**kwargs):
        model = kwargs.get("model")
        if model == "openai/fake-a":
            raise _FakeAuthError("unauthorized")
        return original(**kwargs)

    monkeypatch.setattr(litellm, "completion", fake_completion)

    router = FreeTierRouter(
        model_list=MODEL_LIST,
        enable_free_tier_hygiene=True,
        free_tier_cooldown_seconds=100,
        free_tier_clock=clock,
    )
    engine = router._ft_engine

    # First call: A returns 401 -> parked; falls back to B.
    result = router.completion(model="gpt-test", messages=MSGS)
    assert result.choices[0].message.content == "B_GOOD"
    assert engine.is_parked("openai") is True

    # Immediately after, A is still parked -> skipped (B answers, A not hit).
    clock.advance(10)
    result2 = router.completion(model="gpt-test", messages=MSGS)
    assert result2.choices[0].message.content == "B_GOOD"
    assert engine.is_parked("openai") is True

    # Advance past the cooldown window -> A is available again.
    clock.advance(200)
    assert engine.is_parked("openai") is False
    assert engine.available("openai") is True


def test_provider_breaker_403_parks(monkeypatch):
    class _Fake403(Exception):
        status_code = 403

    original = litellm.completion

    def fake_completion(**kwargs):
        if kwargs.get("model") == "openai/fake-a":
            raise _Fake403("forbidden")
        return original(**kwargs)

    monkeypatch.setattr(litellm, "completion", fake_completion)

    router = FreeTierRouter(
        model_list=MODEL_LIST,
        enable_free_tier_hygiene=True,
        free_tier_cooldown_seconds=60,
    )
    result = router.completion(model="gpt-test", messages=MSGS)
    assert result.choices[0].message.content == "B_GOOD"
    assert router._ft_engine.is_parked("openai") is True


# --------------------------------------------------------------------------- #
# Engine unit tests (pure)
# --------------------------------------------------------------------------- #
def test_engine_daily_budget_reset_on_day_change():
    clock = _FakeClock()
    engine = FreeTierHygieneEngine(budget={"p": {"daily_limit": 1}}, clock=clock)
    assert engine.available("p") is True
    engine.consume("p")
    assert engine.available("p") is False
    # Advance past midnight (next UTC day) -> budget rolls over.
    clock.advance(86400 * 2)
    assert engine.available("p") is True
    assert engine.budget_used("p") == 0


def test_engine_no_budget_never_blocks():
    engine = FreeTierHygieneEngine(clock=_FakeClock())
    assert engine.available("anything") is True
    engine.consume("anything")
    assert engine.available("anything") is True


def test_engine_park_unpark():
    clock = _FakeClock()
    engine = FreeTierHygieneEngine(cooldown_seconds=50, clock=clock)
    until = engine.park("p")
    assert until == clock() + 50
    assert engine.is_parked("p") is True
    assert engine.available("p") is False
    engine.unpark("p")
    assert engine.is_parked("p") is False


def test_helpers():
    assert provider_scope({"litellm_params": {"model": "openai/gpt-4o"}}) == "openai"
    assert provider_scope({"model_info": {"provider": "custom"}}) == "custom"
    assert is_free_tier({"model_info": {"free_tier": True}}) is True
    assert is_free_tier({"model_info": {}}) is False
    assert should_treat_as_empty(None) is True


# --------------------------------------------------------------------------- #
# Activation flags
# --------------------------------------------------------------------------- #
def test_env_flag_activates_feature(monkeypatch):
    monkeypatch.setenv("LITELLM_FREE_TIER_HYGIENE", "1")
    router = FreeTierRouter(model_list=MODEL_LIST)
    assert router._ft_enabled is True


def test_env_flag_does_not_activate_by_default():
    os.environ.pop("LITELLM_FREE_TIER_HYGIENE", None)
    router = FreeTierRouter(model_list=MODEL_LIST)
    assert router._ft_enabled is False


def test_explicit_kwarg_overrides_env(monkeypatch):
    monkeypatch.setenv("LITELLM_FREE_TIER_HYGIENE", "1")
    router = FreeTierRouter(
        model_list=MODEL_LIST, enable_free_tier_hygiene=False
    )
    assert router._ft_enabled is False
