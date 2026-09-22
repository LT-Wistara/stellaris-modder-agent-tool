"""Frozen entry: double-click/serve uses the launcher; `cli` exposes diagnostics."""
import sys
from pathlib import Path

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import start
from stellaris_modder_agent.cli import main as cli_main


if __name__ == '__main__':
    start.configure_console()
    if len(sys.argv) > 1 and sys.argv[1] == 'gui-check':
        from stellaris_modder_agent.desktop_runtime import update_status
        raise SystemExit(update_status())
    if len(sys.argv) > 1 and sys.argv[1] == 'cli':
        raise SystemExit(cli_main(sys.argv[2:]))
    raise SystemExit(start.main())
