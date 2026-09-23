#!/usr/bin/env python3
"""Build the distributable portable archive (the "lazy" bundle).

Everything is taken from this directory; nothing is downloaded and no index is
generated. Run it from anywhere:

    python scripts/build_release.py

The archive is written next to this project directory (the repository root)
and contains exactly one top-level folder, so extracting it never scatters
files:

    <output-dir>/stellaris-modder-agent-tool-<version>-portable.zip
        stellaris-modder-agent-tool/
            使用说明.txt     <- read this first (double-click guide)
            start.cmd        <- double-click: MCP server on 8765 + client snippets
            start.py         <- the launcher itself (checks, index, serve, snippets)
            ...

Machine-specific and build-time files are never packed: ``client-config/``
(it holds absolute paths of the machine that ran ``scripts/configure.py``),
``.git/``, caches, virtual environments and this script's own output.
"""
from pathlib import Path
import argparse
import platform
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT.parent
TOP = 'stellaris-modder-agent-tool'

EXCLUDED_DIRS = {'__pycache__', '.git', '.venv', 'venv', 'build', 'dist', 'Releases', '.idea', '.vscode'}
EXCLUDED_DIRS_BY_PATH = {'client-config'}
EXCLUDED_SUFFIXES = {'.pyc', '.pyo'}
EXCLUDED_BY_PATH = {'scripts/' + Path(__file__).name, 'gui-settings.json', 'gui-settings.tmp'}

# A usable bundle must contain these; a silent mistake here would ship a
# launcher that cannot find its entry point.
REQUIRED = ('start.py', 'start.cmd', 'stellaris_modder_tool.py', '使用说明.txt',
            'stellaris_modder_agent/data/UPSTREAM.json')


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
        if any(part.startswith('.venv-') for part in parts) or path.suffix == '.log':
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


def build_windows(staged=False):
    """Build an onedir EXE; the adjacent corpus remains updateable across runs."""
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise SystemExit('Windows x64 Python is required for this release.')
    release = ROOT / 'Releases'
    work = ROOT / 'build' / 'windows'
    dist = ROOT / 'build' / 'windows-dist' if staged else release
    release.mkdir(exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--distpath', str(dist), '--workpath', str(work),
        str(ROOT / 'scripts' / 'windows.spec'),
    ], check=True, cwd=ROOT)
    return package_windows(dist / 'StellarisModderAgent')


def package_windows(folder=None):
    """Refresh release documentation and archive an already-built executable."""
    from importlib.metadata import distribution
    release = ROOT / 'Releases'
    name = 'StellarisModderAgent'
    folder = Path(folder) if folder is not None else release / name
    if not (folder / (name + '.exe')).is_file():
        raise SystemExit('Build the Windows EXE before packaging it.')
    for filename in ('README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT / filename, folder / filename)
    shutil.copy2(ROOT / 'docs' / 'WINDOWS_RELEASE.md', folder / '使用说明.txt')
    shutil.copy2(Path(sys.base_prefix) / 'LICENSE.txt', folder / 'LICENSE-PYTHON.txt')
    pyinstaller = distribution('pyinstaller')
    for item in pyinstaller.files or ():
        if str(item).endswith('/licenses/COPYING.txt'):
            shutil.copy2(pyinstaller.locate_file(item), folder / 'LICENSE-PYINSTALLER.txt')
            break
    for package in ('customtkinter', 'pillow', 'darkdetect', 'packaging'):
        installed = distribution(package)
        for item in installed.files or ():
            if '.dist-info/' in str(item) and ('license' in str(item).lower() or str(item).endswith('COPYING')) and installed.locate_file(item).is_file():
                name_part = str(item).split('.dist-info/', 1)[1].replace('/', '-')
                shutil.copy2(installed.locate_file(item), folder / f'LICENSE-{package}-{name_part}')
    docs = folder / 'docs'
    docs.mkdir(exist_ok=True)
    for filename in ('RELEASE_REVIEW.md', 'GUI_RELEASE.md', 'WINDOWS_RELEASE.md', 'TEST_REPORT.txt', 'TEST_REPORT.json',
                     'FUZZY_SEARCH_REPORT.md'):
        source = ROOT / 'docs' / filename
        if source.exists():
            shutil.copy2(source, docs / filename)
    output = release / f'{TOP}-{project_version()}-windows-x64.zip'
    write_archive(output, [(path, (Path(name) / path.relative_to(folder)).as_posix())
                           for path in sorted(folder.rglob('*')) if path.is_file()
                           and path.name not in ('gui-settings.json', 'gui-settings.tmp') and path.suffix != '.log'])
    with zipfile.ZipFile(output) as archive:
        if archive.testzip():
            raise SystemExit('Release archive verification failed')
    print(str(output))
    print(f'{output.stat().st_size:,} bytes; launcher: {folder / (name + ".exe")}')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--windows', action='store_true', help='build a standalone Windows x64 EXE zip')
    parser.add_argument('--windows-staged', action='store_true', help='build the Windows zip while an older EXE is running')
    args = parser.parse_args(argv)
    if args.windows or args.windows_staged:
        return build_windows(staged=args.windows_staged)
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
