#!/usr/bin/env python3
"""Absolute-path entry point: independent of working directory."""
from stellaris_modder_agent.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
