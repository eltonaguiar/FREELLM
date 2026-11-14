"""Free-tier hygiene for LiteLLM routers (experimental, opt-in).

This module provides :class:`FreeTierRouter`, a subclass of ``litellm.Router``
that adds two "free-tier hygiene" behaviours on top of the stock router:

1. ``Daily quota-budget pre-filter + provider-scope circuit breaker``
   Before a provider is used, its per-provider daily budget counter is checked.
   If the provider is over-budget -- or has been temporarily "parked" because a
   recent call returned 401/403 -- that provider is skipped and routing proceeds
   to the next fallback deployment, without burning more quota on a broken
   provider.

2. ``Empty-200-as-failure failover``
   If a provider returns HTTP 200 but the payload is empty (blank content, or a
   response that only contains reasoning with no usable content), and the model
   is tagged as free-tier (``model_info["free_tier"] == True``), the response is
   treated as a failure and routing falls through to the next deployment instead
   of surfacing a blank answer. Streaming requests (``stream=True``) skip the
   empty-check entirely.

Design guarantees
-----------------
* **Append-only / non-breaking.** When the feature is disabled the class behaves
  exactly like the stock ``litellm.Router``: every override simply delegates to
  ``super()`` untouched.
* **Opt-in.** Activation happens only through explicit constructor kwargs or
  environment flags (see below). No global ``litellm`` state is modified; all
  hygiene state lives on the router instance (``self._ft_engine``).
* **Self-contained.** The hygiene engine (``FreeTierHygieneEngine``) is a plain
  Python class with an injectable clock, so it is unit-testable without any
  network or LiteLLM machinery.

Activation
----------
Constructor kwargs (they win over environment variables)::

    from litellm_free_tier_router import FreeTierRouter

    router = FreeTierRouter(
        model_list=[...],
        enable_free_tier_hygiene=True,          # master switch (default: off)
        free_tier_budget={
            # per-provider daily request budget (scope keyed by provider)
            "openai": {"daily_limit": 1000},
            "groq": {"daily_limit": 5000},
        },
        free_tier_cooldown_seconds=60,          # how long a 401/403 parks a provider
        free_tier_empty_content_is_failure=True, # empty-200 failover toggle
    )

Environment flags::

    LITELLM_FREE_TIER_HYGIENE=1|true|yes        # enable (if no explicit kwarg)
    LITELLM_FREE_TIER_BUDGET='{"openai":0}'    # JSON dict, same shape as above
    LITELLM_FREE_TIER_COOLDOWN_SECONDS=60

Per-model opt-in: mark a deployment as free-tier with ``model_info``::

    {"model_name": "my-model", "litellm_params": {...},
     "model_info": {"free_tier": True}}

Empty-content detection applies only to deployments carrying that tag; the quota
filter and circuit breaker apply to every provider in the router when the
feature is enabled.
"""

from __future__ import annotations

import datetime as _datetime
import json as _json
import logging as _logging
import os as _os
import time as _time
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

import litellm as _litellm

from litellm import Router

_logger = _logging.getLogger("litellm_free_tier_router")

# Status codes that mean "this provider scope is broken / unauthorized" and cause
# the entire provider scope to be parked briefly.
_PROVIDER_BREAKER_STATUS_CODES = frozenset({401, 403})

# Environment flags.
_ENV_ENABLE = "LITELLM_FREE_TIER_HYGIENE"
_ENV_BUDGET = "LITELLM_FREE_TIER_BUDGET"
_ENV_COOLDOWN = "LITELLM_FREE_TIER_COOLDOWN_SECONDS"


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class FreeTierError(Exception):
    """Base error for free-tier hygiene."""


class EmptyContentError(FreeTierError):
    """Raised/logged when a free-tier provider returns an empty 200 response."""


class FreeTierNoAvailableDeploymentError(FreeTierError):
    """All candidate deployments were skipped by hygiene (quota/breaker/empty)."""

    def __init__(self, model: str) -> None:
        self.model = model
        super().__init__(
            f"No deployment available for model {model!r} after free-tier "
            "hygiene filtering (all providers over-budget, parked, or empty)."
        )


# --------------------------------------------------------------------------- #
# Pure helpers (deployment/response inspection)
# --------------------------------------------------------------------------- #
def provider_scope(deployment: Dict[str, Any]) -> str:
    """Return a stable provider-scope key for a router deployment dict.

    Precedence: ``model_info["provider"]``, then ``litellm_params["api_base"]``,
    then the provider prefix of the upstream model name, then ``"unknown"``.
    """
    model_info = deployment.get("model_info") or {}
    provider = model_info.get("provider")
    if provider:
        return str(provider)
    params = deployment.get("litellm_params") or {}
    api_base = params.get("api_base")
    if api_base:
        return str(api_base)
    model = params.get("model") or ""
    if isinstance(model, str) and "/" in model:
        return model.split("/", 1)[0]
    return "unknown"


def is_free_tier(deployment: Dict[str, Any]) -> bool:
    """True if the deployment opts into free-tier handling."""
    model_info = deployment.get("model_info") or {}
    return bool(model_info.get("free_tier"))


def _first_choice_content(response: Any) -> Any:
    try:
        choices = response.choices
        if not choices:
            return ""
        message = choices[0].message
        return getattr(message, "content", None)
    except Exception:
        return None


def should_treat_as_empty(response: Any) -> bool:
    """True if the response is an empty-200 (blank content, or reasoning-only).

    Streaming wrappers are never inspected here; callers must guard on
    ``stream=True`` before calling this.
    """
    if response is None:
        return True
    # A dict payload (e.g. some pass-through formats) is treated as empty if it
    # has no usable "content".
    if isinstance(response, dict):
        content = response.get("content", "")
        return content is None or str(content).strip() == ""
    content = _first_choice_content(response)
    if content is None:
        return True
    if isinstance(content, str):
        return content.strip() == ""
    # Non-string content (e.g. a list of content parts) is non-empty.
    return False


def exception_status(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status from an exception (e.g. AuthenticationError)."""
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if code is None:
        code = getattr(exc, "status_code", None)
    if code is None:
        # httpx-style responses sometimes expose .status_code only on the
        # exception itself.
        code = getattr(getattr(exc, "status", None), "status_code", None)
    return int(code) if isinstance(code, int) else None


# --------------------------------------------------------------------------- #
# Free-tier hygiene engine (pure state; injectable clock)
# --------------------------------------------------------------------------- #
class FreeTierHygieneEngine:
    """Tracks per-provider daily budgets and 401/403 park state.

    The engine is deliberately independent of LiteLLM. It operates on provider
    *scope strings* (see :func:`provider_scope`) and takes a callable ``clock``
    (defaults to ``time.time``) so tests can drive time deterministically.
    """

    def __init__(
        self,
        budget: Optional[Dict[str, Any]] = None,
        cooldown_seconds: float = 60.0,
        clock: Optional[Callable[[], float]] = None,
        empty_is_failure: bool = True,
    ) -> None:
        self.cooldown_seconds = float(cooldown_seconds)
        self.now: Callable[[], float] = clock or _time.time
        self.empty_is_failure = bool(empty_is_failure)

        # scope -> {"limit": int | None, "day": str, "used": int}
        self._budgets: Dict[str, Dict[str, Any]] = {}
        for scope, spec in (budget or {}).items():
            if isinstance(spec, dict):
                limit = spec.get("daily_limit", spec.get("limit"))
            else:
                limit = spec
            self._budgets[str(scope)] = {
                "limit": int(limit) if limit is not None else None,
                "day": self._day(),
                "used": 0,
            }

        # scope -> parked_until (monotonic/clock timestamp)
        self._parked: Dict[str, float] = {}

    # -- internal --------------------------------------------------------- #
    def _day(self) -> str:
        return _datetime.date.fromtimestamp(self.now()).isoformat()

    def _check_rollover(self, scope: str) -> None:
        entry = self._budgets.get(scope)
        if entry is None:
            return
        today = self._day()
        if entry["day"] != today:
            entry["day"] = today
            entry["used"] = 0

    # -- queries ---------------------------------------------------------- #
    def available(self, scope: str) -> bool:
        """True if the provider scope is not over-budget and not parked."""
        self._check_rollover(scope)
        entry = self._budgets.get(scope)
        if entry is not None and entry["limit"] is not None:
            if entry["used"] >= entry["limit"]:
                return False
        parked_until = self._parked.get(scope)
        if parked_until is not None and self.now() < parked_until:
            return False
        return True

    def budget_used(self, scope: str) -> int:
        self._check_rollover(scope)
        entry = self._budgets.get(scope)
        return entry["used"] if entry else 0

    def budget_limit(self, scope: str) -> Optional[int]:
        self._check_rollover(scope)
        entry = self._budgets.get(scope)
        return entry["limit"] if entry else None

    def parked_until(self, scope: str) -> Optional[float]:
        return self._parked.get(scope)

    def is_parked(self, scope: str) -> bool:
        until = self._parked.get(scope)
        return until is not None and self.now() < until

    # -- mutations -------------------------------------------------------- #
    def consume(self, scope: str, units: int = 1) -> None:
        """Record budget usage for a provider scope."""
        self._check_rollover(scope)
        entry = self._budgets.get(scope)
        if entry is not None:
            entry["used"] += int(units)

    def park(
        self, scope: str, cooldown_seconds: Optional[float] = None
    ) -> float:
        """Park a provider scope until ``now + cooldown``. Returns the time."""
        cd = (
            float(cooldown_seconds)
            if cooldown_seconds is not None
            else self.cooldown_seconds
        )
        until = self.now() + cd
        self._parked[scope] = until
        return until

    def unpark(self, scope: str) -> None:
        self._parked.pop(scope, None)


# --------------------------------------------------------------------------- #
# Router subclass
# --------------------------------------------------------------------------- #
class FreeTierRouter(Router):
    """A ``litellm.Router`` with opt-in free-tier hygiene.

    When ``enable_free_tier_hygiene`` is falsy (the default) every override
    delegates to the stock ``Router`` and behaviour is identical to stock.
    """

    def __init__(
        self,
        *,
        enable_free_tier_hygiene: Optional[bool] = None,
        free_tier_budget: Optional[Dict[str, Any]] = None,
        free_tier_cooldown_seconds: float = 60.0,
        free_tier_empty_content_is_failure: bool = True,
        free_tier_clock: Optional[Callable[[], float]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._init_hygiene(
            enable=enable_free_tier_hygiene,
            budget=free_tier_budget,
            cooldown=free_tier_cooldown_seconds,
            empty=free_tier_empty_content_is_failure,
            clock=free_tier_clock,
        )

    def _init_hygiene(
        self,
        *,
        enable: Optional[bool] = None,
        budget: Optional[Dict[str, Any]] = None,
        cooldown: float = 60.0,
        empty: bool = True,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        """Resolve the master switch and (re)build the hygiene engine.

        ``enable=None`` (the default here, and the only path available through
        ``from_config``) means "read the environment flag". An explicit bool wins.
        When disabled, every override delegates to stock and no hygiene state exists.
        """
        # Resolve the master switch: explicit kwarg wins, else env flag.
        if enable is not None:
            enabled = bool(enable)
        else:
            enabled = _os.environ.get(_ENV_ENABLE, "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )

        # LiteLLM's Router is a facade over a generated core class
        # (``FreeTierRouterCore``). Method calls (``completion``,
        # ``acompletion``) are dispatched with ``self`` bound to the core, so the
        # hygiene state must live on the core object — both for the normal
        # ``__init__`` path and for the proxy's ``from_config`` construction path.
        try:
            target = self._core
        except AttributeError:
            target = self

        object.__setattr__(target, "_ft_enabled", enabled)
        object.__setattr__(target, "_ft_engine", None)

        if not enabled:
            return

        if budget is None:
            raw_budget = _os.environ.get(_ENV_BUDGET, "").strip()
            if raw_budget:
                try:
                    budget = _json.loads(raw_budget)
                except ValueError:
                    budget = None

        if cooldown == 60.0:
            raw_cd = _os.environ.get(_ENV_COOLDOWN, "").strip()
            if raw_cd:
                try:
                    cooldown = float(raw_cd)
                except ValueError:
                    cooldown = 60.0

        object.__setattr__(
            target,
            "_ft_engine",
            FreeTierHygieneEngine(
                budget=budget,
                cooldown_seconds=cooldown,
                clock=clock,
                empty_is_failure=empty,
            ),
        )

    def _ensure_hygiene_state(self) -> None:
        """Ensure hygiene state exists, lazily for the proxy's construction path.

        LiteLLM's proxy builds its router exclusively through
        ``litellm.Router.from_config``. The stock ``from_config`` constructs the
        instance via ``cls.__new__`` + ``_attach``, bypassing ``__init__``, so
        hygiene state would never exist when the custom router is registered on the
        proxy. This guard initializes it on first actual use, from environment flags
        alone (the proxy path cannot pass constructor kwargs). Direct
        ``FreeTierRouter(...)`` construction already initializes in ``__init__``,
        so this is a no-op there.
        """
        if hasattr(self, "_ft_enabled"):
            return
        self._init_hygiene()

    # ------------------------------------------------------------------ #
    # Candidate ordering
    # ------------------------------------------------------------------ #
    def _hygiene_candidates(self, model: str) -> List[Dict[str, Any]]:
        """Deployments in the routing group for ``model``, in fallback order.

        Respects the ``order`` field when present; otherwise insertion order.
        """
        ordered: List[tuple] = []
        for idx, dep in enumerate(self.model_list or []):
            if dep.get("model_name") == model:
                ordered.append((dep.get("order", idx), idx, dep))
        ordered.sort(key=lambda item: (item[0], item[1]))
        return [dep for _, _, dep in ordered]

    def _build_call_kwargs(
        self,
        deployment: Dict[str, Any],
        messages: Sequence[Dict[str, str]],
        kwargs: Dict[str, Any],
    ) -> Dict[str, Any]:
        params = dict(deployment.get("litellm_params") or {})
        call = dict(params)
        call["messages"] = messages
        for key, value in kwargs.items():
            if key in ("model", "messages", "specific_deployment"):
                continue
            call[key] = value
        return call

    # ------------------------------------------------------------------ #
    # Enabled completion loops
    # ------------------------------------------------------------------ #
    def _ft_completion(self, model: str, messages, **kwargs) -> Any:
        assert self._ft_engine is not None
        engine = self._ft_engine
        candidates = self._hygiene_candidates(model)
        if not candidates:
            return super().completion(model, messages, **kwargs)

        stream = bool(kwargs.get("stream"))
        last_exception: Optional[BaseException] = None

        for deployment in candidates:
            scope = provider_scope(deployment)
            if not engine.available(scope):
                _logger.debug(
                    "free-tier hygiene: skipping provider scope %r (budget/breaker)",
                    scope,
                )
                continue

            call_kwargs = self._build_call_kwargs(deployment, messages, kwargs)
            try:
                response = _litellm.completion(**call_kwargs)
            except Exception as exc:  # noqa: BLE001 - any provider failure
                last_exception = exc
                status = exception_status(exc)
                if status in _PROVIDER_BREAKER_STATUS_CODES:
                    until = engine.park(scope)
                    _logger.warning(
                        "free-tier hygiene: provider scope %r returned %s -> "
                        "parked until %.1f",
                        scope,
                        status,
                        until,
                    )
                continue

            engine.consume(scope)

            if stream:
                # Streaming: skip empty-check entirely, surface as-is.
                return response

            if (
                engine.empty_is_failure
                and is_free_tier(deployment)
                and should_treat_as_empty(response)
            ):
                _logger.warning(
                    "free-tier hygiene: empty 200 from %r (%s) -> falling back "
                    "to next deployment",
                    scope,
                    deployment.get("model_name"),
                )
                empty_error = EmptyContentError(
                    f"Empty 200 response from provider scope {scope!r}"
                )
                _logger.debug("empty-content failure: %r", empty_error)
                continue

            return response

        if last_exception is not None:
            raise last_exception
        raise FreeTierNoAvailableDeploymentError(model=model)

    async def _ft_acompletion(self, model: str, messages, **kwargs) -> Any:
        assert self._ft_engine is not None
        engine = self._ft_engine
        candidates = self._hygiene_candidates(model)
        if not candidates:
            return await super().acompletion(model, messages, **kwargs)

        stream = bool(kwargs.get("stream"))
        last_exception: Optional[BaseException] = None

        for deployment in candidates:
            scope = provider_scope(deployment)
            if not engine.available(scope):
                _logger.debug(
                    "free-tier hygiene: skipping provider scope %r (budget/breaker)",
                    scope,
                )
                continue

            call_kwargs = self._build_call_kwargs(deployment, messages, kwargs)
            try:
                response = await _litellm.acompletion(**call_kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exception = exc
                status = exception_status(exc)
                if status in _PROVIDER_BREAKER_STATUS_CODES:
                    until = engine.park(scope)
                    _logger.warning(
                        "free-tier hygiene: provider scope %r returned %s -> "
                        "parked until %.1f",
                        scope,
                        status,
                        until,
                    )
                continue

            engine.consume(scope)

            if stream:
                return response

            if (
                engine.empty_is_failure
                and is_free_tier(deployment)
                and should_treat_as_empty(response)
            ):
                _logger.warning(
                    "free-tier hygiene: empty 200 from %r (%s) -> falling back "
                    "to next deployment",
                    scope,
                    deployment.get("model_name"),
                )
                continue

            return response

        if last_exception is not None:
            raise last_exception
        raise FreeTierNoAvailableDeploymentError(model=model)

    # ------------------------------------------------------------------ #
    # Public overrides (feature-off => identical to stock)
    # ------------------------------------------------------------------ #
    def completion(self, model: str, messages, **kwargs) -> Any:
        self._ensure_hygiene_state()
        if not self._ft_enabled:
            return super().completion(model, messages, **kwargs)
        return self._ft_completion(model, messages, **kwargs)

    async def acompletion(self, model: str, messages, **kwargs) -> Any:
        self._ensure_hygiene_state()
        if not self._ft_enabled:
            return await super().acompletion(model, messages, **kwargs)
        return await self._ft_acompletion(model, messages, **kwargs)
