#!/usr/bin/env python3
"""Build the distributable portable archive (the "lazy" bundle).

Everything is taken from this directory; nothing is downloaded and no index is
generated. Run it from anywhere:

    python scripts/build_release.py

The archive is written next to this project directory (the repository root)
and contains exactly one top-level folder, so extracting it never scatters
files:

    <output-dir>/stellaris-agent-tool-<version>-portable.zip
        stellaris-agent-tool/
            使用说明.txt     <- read this first (double-click guide)
            start.cmd        <- double-click: MCP server on 8765 + client snippets
            start.py         <- the launcher itself (checks, index, serve, snippets)
            ...

Machine-specific and build-time files are never packed: ``client-config/``
(it holds absolute paths of the machine that ran ``scripts/configure.py``),
``.git/``, caches, virtual environments and this script's own output.
"""
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT.parent
TOP = 'stellaris-agent-tool'

EXCLUDED_DIRS = {'__pycache__', '.git', '.venv', 'venv', 'build', 'dist', '.idea', '.vscode'}
EXCLUDED_DIRS_BY_PATH = {'client-config'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo'}
EXCLUDED_BY_PATH = {'scripts/' + Path(__file__).name}

# A usable bundle must contain these; a silent mistake here would ship a
# launcher that cannot find its entry point.
REQUIRED = ('start.py', 'start.cmd', 'stellaris_tool.py', '使用说明.txt',
            'stellaris_agent/data/UPSTREAM.json')


def project_version():
    """Version from pyproject.toml, so the archive name never goes stale."""
    match = re.search(r'^version\s*=\s*"([^"]+)"',
                      (ROOT / 'pyproject.toml').read_text('utf-8'), re.MULTILINE)
    return match.group(1) if match else '0.0.0'


def included_files():
    """Every regular file of the bundle, as (absolute path, archive name)."""
    selected = []
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        parts = relative.parts
        if EXCLUDED_DIRS.intersection(parts):
            continue
        if EXCLUDED_DIRS_BY_PATH.intersection(parts):
            continue
        if path.suffix in EXCLUDED_SUFFIXES:
            continue
        if relative.as_posix() in EXCLUDED_BY_PATH:
            continue
        if any(part.endswith('.egg-info') for part in parts):
            continue
        selected.append((path, TOP + '/' + relative.as_posix()))
    return selected


def write_archive(output, files):
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, archive_name in files:
            info = zipfile.ZipInfo.from_file(path, archive_name)
            # Fixed timestamps keep rebuilds byte-reproducible.
            info.date_time = (1980, 1, 1, 0, 0, 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(path, 'rb') as handle:
                archive.writestr(info, handle.read())


def main():
    files = included_files()
    names = {archive_name[len(TOP) + 1:] for _, archive_name in files}
    missing = [name for name in REQUIRED if name not in names]
    if missing:
        raise SystemExit('Refusing to build: required files are missing: ' + ', '.join(missing))

    version = project_version()
    output = OUTPUT_DIR / f'{TOP}-{version}-portable.zip'
    write_archive(output, files)

    with zipfile.ZipFile(output) as archive:
        packed = archive.namelist()
        damaged = archive.testzip()
    if damaged:
        raise SystemExit('Archive damaged at ' + damaged)
    print(str(output))
    print(f'{len(packed)} entries; {output.stat().st_size:,} bytes; version {version}')
    print('launcher: ' + TOP + '/start.cmd  (double-click: MCP server on http://127.0.0.1:8765/mcp)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
