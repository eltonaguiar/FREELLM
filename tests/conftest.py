"""Pytest bootstrap: make repo-local modules importable.

The free-tier router lives at ``tools/litellm_free_tier_router.py`` (not a
package). ``test_free_tier_router.py`` imports it by bare name, so the repo's
``tools/`` directory is placed on ``sys.path`` here. This keeps the tests
self-contained — they run from the repo regardless of how/where it is checked
out — with no dependency on any worktree or absolute path.
"""

from pathlib import Path
import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent
_TOOLS_DIR = _REPO_ROOT / "tools"

if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
