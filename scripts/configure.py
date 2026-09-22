#!/usr/bin/env python3
"""Generate MCP client snippets; never edit user/global configs.

Normally you do not need this file: ``start.py`` prints the same snippets while
it runs the server. It stays for scripted setups and for clients that want a
snippet on disk.

Two flavours are written for every supported client format:

* ``mcp.json`` / ``codex.toml`` pin this computer's interpreter path
  (``sys.executable``) and run ``start.py serve`` directly.
* ``portable-mcp.json`` / ``portable-codex.toml`` launch ``start.cmd``, which
  finds a working Python 3 on whatever computer it ends up on, so the snippet
  survives moving the folder or copying it to another machine.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def identifiers(root=None):
    """Absolute paths of the entries this tool exposes.

    Kept separate from ``main`` so the values can be tested without writing
    files. ``root`` defaults to the directory containing this script's parent.
    """
    base = Path(root).resolve() if root is not None else ROOT
    return {'entry': str(base / 'stellaris_modder_tool.py'),
            'start': str(base / 'start.py'),
            'launcher': str(base / 'start.cmd')}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if '--print-only' in argv:
        # Print the portable snippet instead of writing files: a client only
        # needs one block, and tests use this without touching the disk.
        print(json.dumps({'mcpServers': {'stellaris': {'command': identifiers()['launcher'],
                                                       'args': ['serve']}}},
                         ensure_ascii=False, indent=2))
        return 0
    if sys.version_info < (3, 9):
        raise SystemExit('Python 3.9 or newer is required.')
    base = ROOT
    if '--root' in argv:
        index = argv.index('--root')
        if index + 1 >= len(argv):
            raise SystemExit('--root needs a directory argument')
        base = Path(argv[index + 1]).resolve()
        if not (base / 'start.py').is_file():
            raise SystemExit('no start.py in ' + str(base))
    from stellaris_modder_agent.index import Database
    db = Database()
    out = base / 'client-config'
    out.mkdir(exist_ok=True)
    command = str(Path(sys.executable).resolve())
    start = str(base / 'start.py')
    launcher = str(base / 'start.cmd')

    def mcp_json(program, arguments):
        return json.dumps({'mcpServers': {'stellaris': {'command': program, 'args': arguments}}},
                          ensure_ascii=False, indent=2) + '\n'

    def codex_toml(program, arguments):
        # Forward slashes are valid Windows paths and avoid TOML escaping mistakes.
        return ('[mcp_servers.stellaris]\ncommand = '
                + json.dumps(program.replace('\\', '/'), ensure_ascii=False)
                + '\nargs = ' + json.dumps([a.replace('\\', '/') for a in arguments], ensure_ascii=False)
                + '\nstartup_timeout_sec = 60\n')

    # Local snippets: pinned interpreter, launched straight into stdio mode.
    (out / 'mcp.json').write_text(mcp_json(command, [start, 'serve']), encoding='utf-8')
    (out / 'codex.toml').write_text(codex_toml(command, [start, 'serve']), encoding='utf-8')

    # Portable snippets: the launcher finds Python on the target computer.
    (out / 'portable-mcp.json').write_text(mcp_json(launcher, ['serve']), encoding='utf-8')
    (out / 'portable-codex.toml').write_text(codex_toml(launcher, ['serve']), encoding='utf-8')

    names = ('mcp.json', 'codex.toml', 'portable-mcp.json', 'portable-codex.toml')
    missing = [name for name in names if not (out / name).is_file()]
    absent = [name for name in ('start.py', 'start.cmd') if not (base / name).is_file()]
    if missing or absent:
        raise SystemExit('Could not write snippets; missing: ' + ', '.join(missing + absent))
    print('Ready: Python ' + sys.version.split()[0] + ', ' + str(db.stats['PARSED']) + ' bundled CWT files.')
    print('Client config snippets: ' + str(out))
    print('  mcp.json / codex.toml               -> stdio via start.py, pinned to this interpreter')
    print('  portable-mcp.json / portable-*.toml -> stdio via start.cmd, works after moving the folder')
    print('Tip: running start.py already prints both snippets, including the HTTP form.')
    print('Copy the appropriate snippet into your MCP client settings. Existing settings were not modified.')


if __name__ == '__main__':
    main()
