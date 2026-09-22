#!/usr/bin/env python3
"""Script-form entry point for the corpus updater.

The implementation lives in :mod:`stellaris_agent.corpus` so that the same code
is reachable from the CLI (``python stellaris_tool.py update-corpus``) and works
from an installed package.  This file exists only so the operation is also
discoverable next to the other maintenance scripts.

    python scripts/update_corpus.py --check
    python scripts/update_corpus.py --apply
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stellaris_agent.corpus import main  # noqa: E402

if __name__ == '__main__':
    raise SystemExit(main())