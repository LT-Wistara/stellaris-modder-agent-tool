"""Locate the two data sources this server is allowed to read.

Only two sources exist by design:

1. the Stellaris installation (game data), and
2. the mod directory explicitly selected by the user (their own work in progress).

The launcher database, playsets and other installed mods are deliberately not
consulted: a modder validating their own mod only needs vanilla plus their own
files.  Everything here is read-only and best effort -- when detection fails the
caller keeps working with the bundled CWT corpus alone.

No third-party imports: ``winreg`` is optional and only used on Windows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re

GAME_ENV = ('STELLARIS_GAME_ROOT', 'STELLARIS_GAME_DIR')
MOD_ENV = ('STELLARIS_MOD_ROOT', 'STELLARIS_MOD_DIR')
GAME_APP_ID = '281990'
GAME_DIR_NAME = 'Stellaris'
MOD_DESCRIPTOR = 'descriptor.mod'
GAME_EXECUTABLES = ('stellaris.exe', 'stellaris', 'Stellaris.app')
DEFINES_MARKER = Path('common') / 'defines' / '00_defines.txt'
DYNAMIC_MARKER = Path('common') / 'economic_categories'

_VERSION = re.compile(r'v?(\d+\.\d+(?:\.\d+)?)')

# The launcher's own record of the installed build, written by the launcher into
# the game root.  ``rawVersion`` is the cleanest ("v4.5.0"); ``version`` mixes in
# the release codename and the build number ("Cygnus v4.5.0 (8697)"), so the
# number still has to be pulled out with ``_VERSION``.  Tried in this order.
LAUNCHER_SETTINGS = 'launcher-settings.json'
LAUNCHER_VERSION_KEYS = ('rawVersion', 'modsCompatibilityVersion', 'version')

# The banner the game writes into its own application log:
# ``[17:55:05][game_application.cpp:250]: Game Version: Cygnus v4.5.0``
GAME_VERSION_BANNER = re.compile(r'game\s+version\s*:?', re.I)

# ``logs/system.log`` opens with graphics and audio initialisation, so it is full
# of versions that are not the game's.  A loose match on the word "version" reads
# ``OpenGL Version: 4.6.0 NVIDIA 596.21`` as the installed build, which makes the
# reported game version track the graphics driver instead of the game.  Any line
# matching this is never a game version.
_VERSION_NOISE = re.compile(
    r'opengl|vulkan|direct3d|directx|\bdriver\b|shader|\bgpu\b|adapter|renderer|'
    r'multi-?sampl|nvidia|geforce|radeon|\bamd\b|intel', re.I)

# game.log first: it is the game application's own log, while system.log is the
# engine/graphics boot log that produced the false positives.
VERSION_LOG_FILES = ('game.log', 'system.log')
VERSION_LOG_SCAN_LINES = 80


@dataclass
class Step:
    """One discovery attempt, kept so failures stay actionable."""

    source: str
    path: str
    ok: bool
    detail: str = ''

    def as_dict(self):
        return {'source': self.source, 'path': self.path, 'ok': self.ok, 'detail': self.detail}


@dataclass
class Environment:
    game_root: Path | None = None
    mod_root: Path | None = None
    mod_roots: list[Path] = field(default_factory=list)
    userdata_root: Path | None = None
    game_version: str | None = None
    mod_name: str | None = None
    mod_supported_version: str | None = None
    dynamic_ready: bool = False
    steps: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def usable(self):
        return self.game_root is not None and self.dynamic_ready

    def entries(self):
        """Read the game, then every configured Mod in order."""
        result = []
        if self.game_root is not None:
            result.append(('game', self.game_root))
        roots = self.mod_roots or ([self.mod_root] if self.mod_root is not None else [])
        for number, root in enumerate(roots, 1):
            result.append(('mod' if number == 1 else f'mod-{number}', root))
        return result

    def as_dict(self):
        return {
            'game_root': str(self.game_root) if self.game_root else None,
            'mod_root': str(self.mod_root) if self.mod_root else None,
            'mod_roots': [str(root) for root in (self.mod_roots or ([self.mod_root] if self.mod_root else []))],
            'userdata_root': str(self.userdata_root) if self.userdata_root else None,
            'game_version': self.game_version,
            'mod_name': self.mod_name,
            'mod_supported_version': self.mod_supported_version,
            'dynamic_ready': self.dynamic_ready,
            'usable': self.usable,
            'steps': [step.as_dict() for step in self.steps],
            'warnings': list(self.warnings),
        }


def unquote_descriptor(text):
    """Parse ``descriptor.mod`` / ``.mod`` pseudo-script into a flat mapping."""
    result = {}
    for match in re.finditer(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"', text):
        result[match.group(1).casefold()] = match.group(2)
    return result


def read_descriptor(mod_root):
    if mod_root is None:
        return {}
    descriptor = Path(mod_root) / MOD_DESCRIPTOR
    if not descriptor.is_file():
        return {}
    try:
        return unquote_descriptor(descriptor.read_text('utf-8', errors='replace'))
    except OSError:
        return {}


def is_game_root(path):
    path = Path(path)
    if not (path / 'common').is_dir():
        return False, 'no common/ directory'
    if not any((path / name).exists() for name in GAME_EXECUTABLES):
        return False, 'no game executable (' + ', '.join(GAME_EXECUTABLES) + ')'
    if not (path / DEFINES_MARKER).is_file():
        return False, 'no ' + DEFINES_MARKER.as_posix()
    if not (path / DYNAMIC_MARKER).is_dir():
        return True, 'usable, but ' + DYNAMIC_MARKER.as_posix() + ' is missing (dynamic names stay unresolved)'
    return True, 'game data present'


def _steam_roots():
    """Steam installation directories, from the registry then from defaults."""
    candidates = []
    if os.name == 'nt':
        try:
            import winreg  # noqa: PLC0415 - Windows only, optional
        except ImportError:
            winreg = None
        if winreg is not None:
            lookups = (
                (winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam', 'SteamPath'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Valve\Steam', 'InstallPath'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Valve\Steam', 'InstallPath'),
            )
            for hive, key, value in lookups:
                try:
                    with winreg.OpenKey(hive, key) as handle:
                        candidates.append(Path(winreg.QueryValueEx(handle, value)[0]))
                except OSError:
                    continue
    home = Path.home()
    candidates.extend([
        Path('C:/Program Files (x86)/Steam'),
        Path('C:/Program Files/Steam'),
        home / '.steam' / 'steam',
        home / '.local' / 'share' / 'Steam',
        home / 'Library' / 'Application Support' / 'Steam',
    ])
    seen, result = set(), []
    for candidate in candidates:
        try:
            key = str(candidate).casefold()
        except OSError:
            continue
        if key not in seen and candidate.is_dir():
            seen.add(key)
            result.append(candidate)
    return result


def _library_roots(steam_root):
    """Steam libraries of one installation, read from ``libraryfolders.vdf``."""
    roots = [steam_root]
    manifest = steam_root / 'steamapps' / 'libraryfolders.vdf'
    if manifest.is_file():
        try:
            text = manifest.read_text('utf-8', errors='replace')
        except OSError:
            text = ''
        for match in re.finditer(r'"path"\s+"((?:[^"\\]|\\.)*)"', text):
            value = match.group(1).replace('\\\\', '\\')
            roots.append(Path(value))
    seen, result = set(), []
    for root in roots:
        key = str(root).casefold()
        if key not in seen and root.is_dir():
            seen.add(key)
            result.append(root)
    return result


def _game_candidates(explicit=None):
    """Yield ``(source, candidate)`` game directories, most reliable first."""
    if explicit:
        yield 'argument', Path(explicit)
    for name in GAME_ENV:
        value = os.environ.get(name)
        if value:
            yield 'env:' + name, Path(value)
    for steam_root in _steam_roots():
        for library in _library_roots(steam_root):
            apps = library / 'steamapps'
            yield 'steam-library', apps / 'common' / GAME_DIR_NAME
            manifest = apps / ('appmanifest_' + GAME_APP_ID + '.acf')
            if manifest.is_file():
                yield 'steam-manifest', apps / 'common' / GAME_DIR_NAME
    home = Path.home()
    for base in ('Documents/Paradox Interactive', '.local/share/Paradox Interactive',
                 'Library/Application Support/Paradox Interactive'):
        yield 'pdx-userdata', home / base / GAME_DIR_NAME


def detect_game_root(explicit=None, steps=None):
    steps = [] if steps is None else steps
    seen = set()
    for source, candidate in _game_candidates(explicit):
        try:
            key = str(candidate).casefold()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        if not candidate.is_dir():
            steps.append(Step(source, str(candidate), False, 'directory does not exist'))
            continue
        ok, detail = is_game_root(candidate)
        steps.append(Step(source, str(candidate), ok, detail))
        if ok:
            return candidate
    return None


def detect_userdata_root(mod_root, steps=None):
    """``Documents/Paradox Interactive/Stellaris``: only used for logs/version."""
    steps = [] if steps is None else steps
    candidates = []
    if mod_root is not None:
        # mod/<id> -> parent is mod/, its parent is the user data directory.
        candidates.append(Path(mod_root).parent.parent)
    home = Path.home()
    for base in ('Documents/Paradox Interactive', '.local/share/Paradox Interactive',
                 'Library/Application Support/Paradox Interactive'):
        candidates.append(home / base / GAME_DIR_NAME)
    for candidate in candidates:
        if candidate.is_dir() and (candidate / 'logs').is_dir():
            steps.append(Step('userdata', str(candidate), True, 'logs/ present'))
            return candidate
    for candidate in candidates:
        if candidate.is_dir():
            steps.append(Step('userdata', str(candidate), True, 'directory present, no logs/'))
            return candidate
    return None


def detect_mod_root(explicit=None, start=None, steps=None):
    """Use only a manually supplied mod directory; never infer one from placement."""
    steps = [] if steps is None else steps
    if explicit:
        candidate = Path(explicit)
        ok = (candidate / MOD_DESCRIPTOR).is_file()
        steps.append(Step('argument', str(candidate), ok, 'descriptor.mod present' if ok else 'explicit path accepted'))
        return candidate
    for name in MOD_ENV:
        value = os.environ.get(name)
        if value:
            candidate = Path(value)
            steps.append(Step('env:' + name, str(candidate), True, 'explicit mod root'))
            return candidate
    steps.append(Step('manual', '', False, 'mod root not configured'))
    return None


VERSION_MARKERS = ('version.txt', '.stellaris-version')


def inspect_mod_root(mod_root):
    """Report whether the configured mod root has a descriptor."""
    if mod_root is None:
        return {'usable': False, 'descriptor': False, 'root': None,
                'reason': 'Mod directory is not configured; mod scripts are not being read.',
                'hint': 'Enter the Mod directory in the GUI, pass --mod-root, or set ' + MOD_ENV[0] + '.'}
    root = Path(mod_root)
    if (root / MOD_DESCRIPTOR).is_file():
        return {'usable': True, 'descriptor': True, 'root': str(root),
                'reason': None, 'hint': None}
    return {'usable': True, 'descriptor': False, 'root': str(root),
            'reason': ('No descriptor.mod in the configured mod root ' + str(root) + '; it may not '
                       'be a mod folder, and mod-defined interfaces may be missed.'),
            'hint': 'Configure the folder that holds your descriptor.mod.'}


def _version_number(text):
    """First ``x.y`` / ``x.y.z`` in *text*, without a leading ``v``."""
    match = _VERSION.search(text if isinstance(text, str) else '')
    return match.group(1) if match else None


def _read_marker_version(game_root):
    """Explicit ``version.txt`` / ``.stellaris-version``, written by hand.

    A hosted copy of the game files has no launcher metadata and no logs, so
    whoever uploads the data can state the version once.  That statement is
    deliberate, so it wins over everything detected automatically.
    """
    for name in VERSION_MARKERS:
        marker = Path(game_root) / name
        if not marker.is_file():
            continue
        try:
            found = _version_number(marker.read_text('utf-8', errors='replace'))
        except OSError:
            continue
        if found:
            return found
    return None


def _read_launcher_version(game_root):
    """``launcher-settings.json``: the authoritative source on a normal install.

    The launcher writes the installed build here, which is exactly what this
    function needs and what the log scan below kept getting wrong.
    """
    settings = Path(game_root) / LAUNCHER_SETTINGS
    if not settings.is_file():
        return None
    try:
        parsed = json.loads(settings.read_text('utf-8', errors='replace'))
    except (OSError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    for key in LAUNCHER_VERSION_KEYS:
        found = _version_number(parsed.get(key))
        if found:
            return found
    return None


def _read_log_version(userdata_root):
    """Last resort: the banner the game writes into its own logs.

    Only a line that says ``Game Version`` or names Stellaris *and* is not a
    hardware/driver banner counts.  Returning ``None`` here is correct and much
    better than returning the OpenGL version: a missing version shows up as
    ``null``, while a wrong one silently fabricates corpus drift.
    """
    for name in VERSION_LOG_FILES:
        log = Path(userdata_root) / 'logs' / name
        if not log.is_file():
            continue
        try:
            with log.open('r', encoding='utf-8', errors='replace') as handle:
                for _ in range(VERSION_LOG_SCAN_LINES):
                    line = handle.readline()
                    if not line:
                        break
                    if _VERSION_NOISE.search(line):
                        continue
                    if not (GAME_VERSION_BANNER.search(line) or 'stellaris' in line.casefold()):
                        continue
                    found = _version_number(line)
                    if found:
                        return found
        except OSError:
            continue
    return None


def read_game_version(userdata_root, game_root=None):
    """Version of the installation, most trustworthy source first.

    1. an explicit ``version.txt`` / ``.stellaris-version`` marker,
    2. ``launcher-settings.json`` in the game root (normal installs),
    3. the game's own ``Game Version`` log banner.

    The log fallback is last and deliberately narrow.  It used to accept any line
    containing the substring ``version`` in the first 80 lines of
    ``logs/system.log``, which is graphics and audio initialisation -- so the
    reported game version was whatever the driver claimed, e.g. ``4.6.0`` from
    ``OpenGL Version: 4.6.0 NVIDIA 596.21`` on a Stellaris 4.5.0 install.
    """
    if game_root is not None:
        for reader in (_read_marker_version, _read_launcher_version):
            found = reader(game_root)
            if found:
                return found
    if userdata_root is None:
        return None
    return _read_log_version(userdata_root)


def detect_environment(game_root=None, mod_root=None, start=None):
    """Detect both sources once; the result is safe to cache for the process."""
    steps = []
    warnings = []
    env = Environment(steps=steps, warnings=warnings)
    configured = mod_root if isinstance(mod_root, (list, tuple)) else ([mod_root] if mod_root else [])
    if configured:
        seen = set()
        for value in configured:
            root = detect_mod_root(value, start=start, steps=steps)
            if root is not None and str(root).casefold() not in seen:
                env.mod_roots.append(root)
                seen.add(str(root).casefold())
    else:
        root = detect_mod_root(start=start, steps=steps)
        if root is not None:
            env.mod_roots.append(root)
    env.mod_root = env.mod_roots[0] if env.mod_roots else None
    if env.mod_root is not None:
        descriptor = read_descriptor(env.mod_root)
        env.mod_name = descriptor.get('name')
        env.mod_supported_version = descriptor.get('supported_version')
    env.game_root = detect_game_root(game_root, steps=steps)
    if env.game_root is None:
        warnings.append('Stellaris installation not found; game-generated names stay unresolved. '
                        'Pass --game-root or set ' + GAME_ENV[0] + '.')
    else:
        env.dynamic_ready = (env.game_root / DYNAMIC_MARKER).is_dir()
        if not env.dynamic_ready:
            warnings.append(DYNAMIC_MARKER.as_posix() + ' is missing in the detected game root; '
                            'economic-category modifiers cannot be generated.')
    env.userdata_root = detect_userdata_root(env.mod_root, steps=steps)
    env.game_version = read_game_version(env.userdata_root, env.game_root)
    return env
