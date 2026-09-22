"""Offline, evidence-first Stellaris CWT tools."""
from pathlib import Path
import re

# Single source of truth: the release version declared in pyproject.toml. Reading it here
# keeps the MCP handshake, the CLI and the packaging metadata from drifting apart. The
# distribution runs from a plain source checkout, so the file is read rather than looked
# up through installed-package metadata.
_PYPROJECT = Path(__file__).resolve().parents[1] / 'pyproject.toml'
_MATCH = re.search(r'^version\s*=\s*"([^"]+)"', _PYPROJECT.read_text('utf-8'), re.M) \
    if _PYPROJECT.exists() else None
__version__ = _MATCH.group(1) if _MATCH else '0.0.0'
