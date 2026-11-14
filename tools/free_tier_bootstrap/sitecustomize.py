"""Auto-register ``FreeTierRouter`` as ``litellm.Router`` (opt-in, default off).

LiteLLM's proxy builds its router internally via ``litellm.Router.from_config``
and does not expose a ``custom_router`` hook in this version. The launcher
(``tools/start_litellm_proxy.sh``) honors ``--free-tier-hygiene`` /
``LITELLM_FREE_TIER_HYGIENE=1`` by prepending ONLY this directory to
``PYTHONPATH``. Python auto-imports ``sitecustomize`` at interpreter startup, so
this file runs before the proxy imports ``litellm`` and swaps the ``Router``
symbol for our ``FreeTierRouter`` subclass.

If the flag is not set, this directory is never on the path, this file never
runs, and routing is 100% unchanged (stock ``litellm.Router``).
"""

import os

_ACTIVE = (
    os.environ.get("LITELLM_FREE_TIER_HYGIENE", "").strip().lower()
    in ("1", "true", "yes", "on")
)

if _ACTIVE:
    import sys
    from pathlib import Path

    # This file lives at <repo>/tools/free_tier_bootstrap/sitecustomize.py
    _REPO_ROOT = Path(__file__).resolve().parent.parent.parent
    _TOOLS_DIR = _REPO_ROOT / "tools"
    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))

    import litellm

    from litellm_free_tier_router import FreeTierRouter

    # Register the subclass as the router the proxy will build via from_config
    # (FreeTierRouter.from_config re-initializes hygiene state, so the proxy's
    # ``cls.__new__`` construction path still works).
    litellm.Router = FreeTierRouter
