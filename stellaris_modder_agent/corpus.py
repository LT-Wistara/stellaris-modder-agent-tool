#!/usr/bin/env python3
"""Refresh the bundled CWT corpus from its upstream repository.

The tool ships a pinned snapshot of ``.cwt`` files so that it works with no
network at all.  This module is the only part of the project that talks to the
internet, and it only runs when you ask it to:

    python stellaris_modder_tool.py update-corpus --check                 # report drift, change nothing
    python stellaris_modder_tool.py update-corpus --apply                 # download and replace
    python stellaris_modder_tool.py update-corpus --apply --commit <sha>  # pin one exact commit
    python scripts/update_corpus.py --apply                        # the same, script form

Design notes:

* standard library only (``urllib`` + ``tarfile``), exactly like the rest of the
  tool, so updating needs no pip install;
* only ``config/**.cwt`` and the upstream ``LICENSE`` are taken from the
  archive -- upstream also publishes ``script-docs/`` and ``script-files/``,
  which are tens of megabytes of generated logs and are **not** part of the
  corpus;
* the download is extracted into a staging directory, every file is parsed and
  checked for a lossless roundtrip, and only then is it swapped in.  A failed or
  interrupted update leaves the previous corpus untouched;
* ``UPSTREAM.json`` is rewritten from the same data that produced the files, so
  the manifest can never disagree with what is on disk.

Nothing here runs at server start: the bundled corpus stays the source of truth
until you deliberately replace it.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'stellaris_modder_agent' / 'data'
CONFIG_DIR = DATA_DIR / 'config'
MANIFEST_PATH = DATA_DIR / 'UPSTREAM.json'
LICENSE_PATH = DATA_DIR / 'LICENSE.cwt'

DEFAULT_REPOSITORY = 'DragonKnightOfBreeze/cwtools-stellaris-config'
DEFAULT_BRANCH = 'master'

USER_AGENT = 'stellaris-modder-agent-tool corpus updater (+https://github.com/LT-Wistara/stellaris-modder-agent-tool)'
API_COMMIT = 'https://api.github.com/repos/{repository}/commits/{ref}'
API_COMPARE = 'https://api.github.com/repos/{repository}/compare/{base}...{head}'
ARCHIVE = 'https://codeload.github.com/{repository}/tar.gz/{ref}'

# Fallback channel. Networks differ in which GitHub hosts they allow -- corporate
# proxies and some national networks block the archive host while leaving the API
# and the raw file host reachable, and others do the opposite. So the updater
# tries the single-request tarball first and, if that host cannot be reached at
# all, fetches the same commit one file at a time through these two.
API_TREE = 'https://api.github.com/repos/{repository}/git/trees/{ref}?recursive=1'
RAW_FILE = 'https://raw.githubusercontent.com/{repository}/{ref}/{path}'
CONFIG_PREFIX = 'config/'

# The launch-time staleness check is deliberately cheap: two small API calls and
# no archive download. Its result is cached because start.py runs on every client
# session, and asking GitHub once per session would be rude to the API and slow
# for the user.
CHECK_CACHE_NAME = 'stellaris-modder-agent-tool-update-check.json'
CHECK_MAX_AGE = 24 * 60 * 60

# Retry policy for the per-file route: seconds before the first retry, doubled
# for each further attempt.
RETRY_PAUSE = 1.5
FILE_ATTEMPTS = 4
# Sweeps over the files that failed, before giving up. One stalled connection out
# of a hundred-odd must not discard the ones that already arrived.
FILE_ROUNDS = 3
# A CWT file is small, so its request gets a much shorter leash than an archive
# download: a stalled connection must not hold the update for two minutes.
FILE_TIMEOUT = 30
# Ceiling for the whole per-file route, so a bad network ends in a message rather
# than in a window that looks frozen.
FILE_BUDGET = 600
# How often the per-file route reports progress. Ten files is a few seconds even
# on a slow link, which is soon enough to look alive.
PROGRESS_EVERY = 10

CONFIG_MEMBER = re.compile(r'^[^/]+/config/(?P<relative>.+\.cwt)$')
LICENSE_MEMBER = re.compile(r'^[^/]+/LICENSE$')

# The corpus states its target game version in a comment inside config/aliases.cwt:
#     # no `AND = {...}` usages in vanilla files atm (game version 4.5), ...
# It is the only declared marker upstream publishes, so it is read rather than
# guessed, and a missing marker is reported instead of silently inventing a value.
VERSION_MARKER = re.compile(r'game\s+version\s+(\d+(?:\.\d+)+)', re.IGNORECASE)

VERSION_BASIS = ('Explicit game-version marker in config/aliases.cwt at the pinned '
                 'corpus commit.')
VERSION_NOTE = ('This is the target version declared by the bundled CWT snapshot. '
                'Individual files may retain older update markers, and CWT support is '
                'not a guarantee of complete game compatibility.')
SOURCE_BYTES = ('Downloaded as a codeload tar.gz at the pinned commit and extracted '
                'verbatim; not normalized by a Git checkout.')


class UpdateError(RuntimeError):
    """Anything that should stop the update before the corpus is touched."""


# region reading the current snapshot

def read_manifest(path=MANIFEST_PATH):
    """The manifest of what is currently bundled; ``{}`` when there is none."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text('utf-8'))
    except (OSError, ValueError) as error:
        raise UpdateError('Cannot read ' + str(path) + ': ' + str(error))
    return parsed if isinstance(parsed, dict) else {}


def local_files(config_dir=CONFIG_DIR):
    """Current snapshot as ``{relative posix path: bytes}``."""
    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        return {}
    result = {}
    for path in sorted(config_dir.rglob('*.cwt')):
        if path.is_file():
            result[path.relative_to(config_dir).as_posix()] = path.read_bytes()
    return result


# endregion

# region talking to upstream

def _get(url, accept=None, timeout=120, attempts=1):
    """One GET, wrapped so callers only ever see :class:`UpdateError`.

    ``attempts`` retries transport failures and 5xx replies with a growing pause.
    The per-file route asks for retries because a hundred-odd sequential requests
    will eventually meet a flaky moment, and one dropped connection must not
    throw away an otherwise good update.

    The bare ``OSError`` clause is not redundant. urllib only wraps the *sending*
    half of a request in ``URLError``; a timeout that happens while reading the
    status line or the body arrives as a plain ``TimeoutError`` from inside
    ``getresponse()``, and an updater that only catches ``URLError`` dies with a
    traceback on exactly the networks it was written to cope with.
    """
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                                                   **({'Accept': accept} if accept else {})})
    pause = RETRY_PAUSE
    for attempt in range(1, max(1, attempts) + 1):
        last = attempt >= max(1, attempts)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if not last and error.code >= 500:
                time.sleep(pause)
                pause *= 2
                continue
            raise UpdateError('Upstream returned HTTP ' + str(error.code) + ' for ' + url)
        except (urllib.error.URLError, OSError) as error:
            if not last:
                time.sleep(pause)
                pause *= 2
                continue
            reason = getattr(error, 'reason', None) or (type(error).__name__ + ': ' + str(error))
            raise UpdateError('Cannot reach ' + url + ': ' + str(reason)
                              + '\nCheck the network, or keep the bundled corpus as it is.')
    raise UpdateError('Cannot reach ' + url)


def upstream_commit(repository=DEFAULT_REPOSITORY, ref=DEFAULT_BRANCH, timeout=30):
    """``(sha, committed_at)`` for a branch name or an explicit commit."""
    payload = json.loads(_get(API_COMMIT.format(repository=repository, ref=ref),
                              accept='application/vnd.github+json', timeout=timeout))
    sha = payload.get('sha')
    if not sha:
        raise UpdateError('Upstream did not report a commit for ' + ref)
    return sha, (payload.get('commit') or {}).get('author', {}).get('date')


def download_archive(repository=DEFAULT_REPOSITORY, ref=DEFAULT_BRANCH, timeout=120):
    """The tar.gz of one commit, as bytes."""
    return _get(ARCHIVE.format(repository=repository, ref=ref), timeout=timeout)


def read_archive(blob):
    """``(files, license_bytes, top_level)`` from a GitHub tar.gz.

    ``script-docs/`` and ``script-files/`` are ignored on purpose: they are large
    generated dumps and deliberately not part of the shipped corpus.
    """
    files = {}
    license_bytes = None
    top = None
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode='r:gz') as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                top = top or member.name.split('/')[0]
                match = CONFIG_MEMBER.match(member.name)
                if match:
                    handle = archive.extractfile(member)
                    files[match.group('relative')] = handle.read() if handle else b''
                    continue
                if LICENSE_MEMBER.match(member.name):
                    handle = archive.extractfile(member)
                    license_bytes = handle.read() if handle else None
    except tarfile.TarError as error:
        raise UpdateError('The downloaded archive is not a readable tar.gz: ' + str(error))
    if not files:
        raise UpdateError('The archive contains no config/**/*.cwt files; refusing to '
                          'replace the corpus with it.')
    return files, license_bytes, top


def detect_version(files):
    """Target Stellaris version declared by the snapshot, or ``None``."""
    text = files.get('aliases.cwt')
    if text is None:
        for relative, blob in files.items():
            if relative.endswith('aliases.cwt'):
                text = blob
                break
    if text is None:
        return None
    match = VERSION_MARKER.search(text.decode('utf-8', 'replace'))
    return match.group(1) if match else None


def tree_paths(repository, ref, timeout=30):
    """Relative ``config/**/*.cwt`` paths at one commit, from the API.

    Refuses a truncated tree rather than returning part of one: a partial corpus
    that still parses would be far worse than a failed update, because nothing
    downstream could tell it apart from a complete one.
    """
    payload = json.loads(_get(API_TREE.format(repository=repository, ref=ref),
                              accept='application/vnd.github+json', timeout=timeout,
                              attempts=3))
    if payload.get('truncated'):
        raise UpdateError('Upstream reported a truncated file list for ' + ref
                          + '; refusing to install a partial corpus.')
    tree = payload.get('tree')
    if not isinstance(tree, list):
        raise UpdateError('Upstream did not return a file list for ' + ref)
    return sorted(entry['path'][len(CONFIG_PREFIX):] for entry in tree
                  if entry.get('type') == 'blob'
                  and entry.get('path', '').startswith(CONFIG_PREFIX)
                  and entry.get('path', '').endswith('.cwt'))


def _duration(seconds):
    """``45s`` / ``2m30s`` -- short enough for a progress line."""
    seconds = int(max(0, seconds))
    if seconds < 60:
        return '%ds' % seconds
    return '%dm%02ds' % (seconds // 60, seconds % 60)


class ProgressBar:
    """One line that redraws in place, or plain lines when not on a terminal.

    A minute of silence is indistinguishable from a hang, and the per-file route
    is exactly that: one request per CWT, so the caller has to show movement from
    the very first file. On a terminal this draws a bar with a rate and a
    remaining-time estimate; when the output is captured (a log, a pipe, a CI
    run) in-place redraws would be garbage, so it degrades to one line every
    ``PROGRESS_EVERY`` files instead.
    """

    WIDTH = 24
    MAX_DETAIL = 34
    PAD = 96

    def __init__(self, total, stream=None):
        self.total = max(1, int(total))
        self.stream = stream if stream is not None else sys.stdout
        self.live = bool(getattr(self.stream, 'isatty', lambda: False)())
        self.started = time.monotonic()
        self.drawn = False

    def _write(self, text):
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            # A closed or redirected stream must not turn into an update failure.
            self.live = False

    def update(self, done, total=None, detail=''):
        total = int(total or self.total)
        if not self.live:
            if done % PROGRESS_EVERY and done != total:
                return
            self._write('  %d/%d files\n' % (done, total))
            return
        elapsed = time.monotonic() - self.started
        rate = done / elapsed if elapsed > 0 else 0.0
        detail = detail if len(detail) <= self.MAX_DETAIL else '...' + detail[-(self.MAX_DETAIL - 3):]
        filled = int(self.WIDTH * done / total)
        tail = ''
        if rate and done < total:
            tail = '  ~%s left' % _duration((total - done) / rate)
        line = '  [%s%s] %3d%%  %d/%d  %-*s%s' % (
            '#' * filled, '-' * (self.WIDTH - filled), 100 * done // total, done, total,
            self.MAX_DETAIL, detail, tail)
        # Pad so a previously longer line leaves nothing behind.
        self._write('\r' + line[:self.PAD].ljust(self.PAD))
        self.drawn = True

    def close(self):
        if self.live and self.drawn:
            self._write('\n')
        self.drawn = False


def download_files(repository, ref, paths, timeout=FILE_TIMEOUT, progress=None,
                   budget=FILE_BUDGET, rounds=FILE_ROUNDS):
    """``(files, license_bytes)`` fetched one request per file.

    Slower than the archive -- about one round trip per CWT -- but it only needs
    the API and raw file hosts, so it works on networks that block the archive
    host. Bytes are exactly what upstream serves, same as the tarball route.

    Four guards, because a hundred-odd sequential requests over an unsteady link
    is a different problem from one big download:

    * each request gets its own short timeout -- these are small files, and the
      generous archive timeout is the wrong budget for one of them;
    * ``rounds`` sweeps the *remaining* files again, so one stalled connection
      does not throw away a hundred files that already arrived. A partial corpus
      can never be installed, so without this a single bad moment is fatal;
    * ``budget`` caps the whole route, so it ends in a message instead of running
      for as long as the network feels like;
    * ``progress`` is called *before* each request, not after, so the line names
      the file being fetched right now even if that fetch is the one that stalls.
    """
    started = time.monotonic()
    files = {}
    failures = {}
    pending = list(paths)
    for _ in range(max(1, rounds)):
        if not pending:
            break
        remaining = []
        for relative in pending:
            if time.monotonic() - started > budget:
                raise UpdateError('Stopped after %d of %d files: the per-file route has been '
                                  'running for more than %d seconds. The corpus is unchanged.'
                                  % (len(files), len(paths), budget))
            if progress is not None:
                progress(len(files), len(paths), relative)
            try:
                files[relative] = _get(RAW_FILE.format(repository=repository, ref=ref,
                                                       path=CONFIG_PREFIX + relative),
                                       timeout=min(timeout, FILE_TIMEOUT),
                                       attempts=FILE_ATTEMPTS)
                failures.pop(relative, None)
            except UpdateError as error:
                failures[relative] = str(error).splitlines()[0]
                remaining.append(relative)
        pending = remaining
    if pending:
        detail = '\n'.join('    ' + name + ': ' + failures.get(name, '')
                           for name in sorted(pending)[:3])
        raise UpdateError('Could not fetch %d of %d files after %d rounds; the corpus is '
                          'unchanged.\n  first failures:\n%s'
                          % (len(pending), len(paths), max(1, rounds), detail))
    if progress is not None:
        progress(len(files), len(paths), '')
    try:
        license_bytes = _get(RAW_FILE.format(repository=repository, ref=ref, path='LICENSE'),
                             timeout=min(timeout, FILE_TIMEOUT), attempts=FILE_ATTEMPTS)
    except UpdateError:
        # The licence is attribution, not corpus: its absence must not stop an
        # otherwise good update, and install() keeps the previous file.
        license_bytes = None
    return files, license_bytes


# endregion

# region comparing and installing

def normalised(blob):
    """Content with line endings unified.

    The snapshot on disk is CRLF because it was checked out through
    ``core.autocrlf`` at some point, while upstream stores LF.  Comparing raw
    bytes would report all 173 files as changed on every run and bury the files
    that really did change.
    """
    return blob.replace(b'\r\n', b'\n')


def line_endings(files):
    """``'crlf'``, ``'lf'``, ``'mixed'`` or ``'none'`` across a set of files."""
    crlf = lf = 0
    for blob in files.values():
        crlf += blob.count(b'\r\n')
        lf += blob.count(b'\n') - blob.count(b'\r\n')
    if crlf and lf:
        return 'mixed'
    if crlf:
        return 'crlf'
    return 'lf' if lf else 'none'


def compare(current, incoming):
    """``(added, removed, changed, unchanged)`` as sorted path lists."""
    added = sorted(set(incoming) - set(current))
    removed = sorted(set(current) - set(incoming))
    common = set(current) & set(incoming)
    changed = sorted(name for name in common
                     if normalised(current[name]) != normalised(incoming[name]))
    unchanged = sorted(common - set(changed))
    return added, removed, changed, unchanged


def verify_corpus(files):
    """Parse every file and prove the parser reproduces its bytes.

    Runs against the tool's own parser, so a corpus that would make the server
    report parse failures is rejected before it replaces a working snapshot.
    """
    from .parser import parse  # noqa: PLC0415 - keep the import off the --check path

    failures = []
    for relative in sorted(files):
        text = files[relative].decode('utf-8', 'replace')
        try:
            document = parse(text, relative)
        except Exception as error:  # noqa: BLE001 - any parser failure disqualifies the file
            failures.append(relative + ': ' + str(error))
            continue
        if document.roundtrip() != text:
            failures.append(relative + ': lossless roundtrip failed')
    if failures:
        raise UpdateError('The downloaded corpus did not survive verification:\n  '
                          + '\n  '.join(failures[:10]))


def build_manifest(files, *, repository, branch, commit, committed_at, license_bytes):
    """The ``UPSTREAM.json`` payload describing exactly these files."""
    version = detect_version(files)
    now = datetime.now(timezone.utc)
    manifest = {
        'repository': 'https://github.com/' + repository,
        'branch': branch,
        'stellaris_version': version,
        'stellaris_version_basis': VERSION_BASIS if version else
                                   'No game-version marker found in config/aliases.cwt.',
        'stellaris_version_note': VERSION_NOTE,
        'commit_sha': commit,
        'packaged_at_utc': now.isoformat(),
        'packaged_date': now.date().isoformat(),
        'license': 'MIT',
        'license_file': 'LICENSE.cwt',
        'attribution': ('Copyright (c) 2018 tboby; CWTools Stellaris config contributors; '
                        'DragonKnightOfBreeze fork.'),
        'file_count': len(files),
        'source_bytes': SOURCE_BYTES,
        'files': sorted(files),
    }
    if committed_at:
        manifest['committed_at_utc'] = committed_at
    return manifest


def install(files, license_bytes, manifest, *, config_dir=CONFIG_DIR,
            license_path=LICENSE_PATH, manifest_path=MANIFEST_PATH):
    """Swap a verified snapshot into place, rolling back if anything fails.

    Staging lives next to the target so the final rename stays on one filesystem
    and is therefore atomic; the previous corpus is kept until the new one is
    fully in place.
    """
    config_dir = Path(config_dir)
    license_path = Path(license_path)
    manifest_path = Path(manifest_path)
    parent = config_dir.parent
    parent.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix='.staging-corpus-', dir=str(parent)))
    backup = parent / (config_dir.name + '.previous')
    old_license = license_path.read_bytes() if license_path.is_file() else None
    old_manifest = manifest_path.read_bytes() if manifest_path.is_file() else None

    try:
        for relative, blob in files.items():
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)

        if backup.exists():
            shutil.rmtree(backup)
        if config_dir.exists():
            config_dir.rename(backup)
        staging.rename(config_dir)

        if license_bytes is not None:
            license_path.write_bytes(license_bytes)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                                 encoding='utf-8')
    except BaseException:
        # Either the staging directory is still staged, or it has already been
        # renamed into place -- the second case must be undone too, or a failed
        # update would leave a half corpus behind and the backup orphaned.
        shutil.rmtree(staging, ignore_errors=True)
        if backup.exists():
            shutil.rmtree(config_dir, ignore_errors=True)
            backup.rename(config_dir)
        if old_license is not None:
            license_path.write_bytes(old_license)
        if old_manifest is not None:
            manifest_path.write_bytes(old_manifest)
        raise

    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)


# endregion

# region reporting

def report(current, incoming, manifest, *, repository, branch, commit, committed_at,
           blocked=False):
    added, removed, changed, unchanged = compare(current, incoming)
    lines = []
    local_commit = manifest.get('commit_sha')
    lines.append('local commit  : ' + (local_commit or '(no manifest)'))
    lines.append('upstream      : ' + repository + '@' + branch)
    lines.append('upstream head : ' + commit + ('  ' + committed_at if committed_at else ''))
    lines.append('target version: ' + str(detect_version(incoming) or '(no marker)'))
    lines.append('files         : ' + str(len(current)) + ' local, '
                 + str(len(incoming)) + ' upstream')
    lines.append('  added       : ' + (', '.join(added) if added else '(none)'))
    lines.append('  removed     : ' + (', '.join(removed) if removed else '(none)'))
    lines.append('  changed     : ' + (', '.join(changed) if changed else '(none)'))
    lines.append('  unchanged   : ' + str(len(unchanged)))
    local_endings, remote_endings = line_endings(current), line_endings(incoming)
    lines.append('line endings  : local ' + local_endings + ', upstream ' + remote_endings
                 + '  (compared after normalising, so it is not counted as drift)')
    if local_endings != 'none' and local_endings != remote_endings:
        lines.append('  -> --apply writes the upstream bytes verbatim, so the snapshot on '
                     'disk becomes ' + remote_endings + '. Keep .gitattributes in place or '
                     'Git will convert it back on checkout.')
    if blocked:
        lines.append('')
        lines.append('--check is read-only: nothing was written. Re-run with --apply to '
                     'install this snapshot.')
    elif local_commit == commit and not (added or removed or changed):
        lines.append('')
        lines.append('Already up to date with ' + commit[:10] + '; nothing to do.')
    return '\n'.join(lines), (added, removed, changed, unchanged)


# endregion

def _defaults(manifest):
    """Where to look for updates when the command line says nothing.

    The manifest records the repository the snapshot actually came from, so a
    fork or a mirror keeps updating from itself instead of from this default.
    """
    repository, branch = DEFAULT_REPOSITORY, manifest.get('branch') or DEFAULT_BRANCH
    match = re.match(r'https?://github\.com/(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$',
                     manifest.get('repository') or '')
    if match:
        repository = match.group('slug')
    return repository, branch


# region staleness check

def cache_path():
    """Where the last check is remembered, shared by every launch of the tool."""
    return Path(tempfile.gettempdir()) / CHECK_CACHE_NAME


def upstream_status(manifest=None, repository=None, branch=None, timeout=8):
    """How far the bundled snapshot is behind upstream, or ``None``.

    Two small API calls -- no archive download -- because this runs on the launch
    path.  It returns ``None`` instead of raising when GitHub is unreachable: the
    tool works offline by design, and a failed check is not an error worth
    showing anyone.
    """
    manifest = read_manifest() if manifest is None else manifest
    if repository is None:
        repository, default_branch = _defaults(manifest)
        branch = branch or default_branch
    branch = branch or DEFAULT_BRANCH
    local = manifest.get('commit_sha')
    try:
        head, committed_at = upstream_commit(repository, branch, timeout=timeout)
    except UpdateError:
        return None

    behind = 0
    if local and local != head:
        behind = None
        try:
            comparison = json.loads(_get(
                API_COMPARE.format(repository=repository, base=local, head=head),
                accept='application/vnd.github+json', timeout=timeout))
            behind = comparison.get('ahead_by')
        except (UpdateError, ValueError):
            # The head already differs, so "some" is known even when the exact
            # count is not; that is enough for a hint.
            behind = None
    return {'repository': repository, 'branch': branch, 'local': local, 'upstream': head,
            'committed_at': committed_at, 'behind': behind,
            'checked_at': datetime.now(timezone.utc).isoformat()}


def read_status_cache(path=None, max_age=CHECK_MAX_AGE):
    """The cached check, or ``None`` when it is missing, unreadable or expired."""
    path = Path(path) if path is not None else cache_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if max_age is not None:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(str(payload['checked_at']))).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None
        if age > max_age:
            return None
    return payload


def write_status_cache(status, path=None):
    """Best effort: a read-only temp directory must not break a launch."""
    path = Path(path) if path is not None else cache_path()
    try:
        path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except OSError:
        pass


def cached_status(path=None, max_age=CHECK_MAX_AGE, **kwargs):
    """The check from cache, refreshed only once the cache has expired."""
    cached = read_status_cache(path, max_age)
    if cached is not None:
        return cached
    status = upstream_status(**kwargs)
    if status is not None:
        write_status_cache(status, path)
    return status


def update_hint(status):
    """A short bilingual hint, or ``None`` when there is nothing to report."""
    if not status or status.get('behind') == 0:
        return None
    count = status.get('behind')
    local = str(status.get('local') or '?')[:10]
    head = str(status.get('upstream') or '?')[:10]
    if count:
        english = 'The bundled CWT corpus is %d commit(s) behind upstream (%s -> %s).' % (
            count, local, head)
        chinese = '内置语料落后上游 %d 个提交（%s -> %s）。' % (count, local, head)
    else:
        english = 'The bundled CWT corpus is behind upstream (%s -> %s).' % (local, head)
        chinese = '内置语料落后上游（%s -> %s）。' % (local, head)
    return '\n'.join([
        '[提示] ' + chinese,
        '       运行  python stellaris_modder_tool.py update-corpus --check  查看差异，',
        '       或    python stellaris_modder_tool.py update-corpus --apply  下载并替换。',
        '       ' + english,
        '       Nothing changes until you run it: the tool stays offline otherwise.',
    ])


# endregion


def main(argv=None):
    manifest = read_manifest()
    default_repository, default_branch = _defaults(manifest)

    parser = argparse.ArgumentParser(
        description='Refresh the bundled CWT corpus from its upstream repository')
    parser.add_argument('--check', action='store_true',
                        help='report the difference and change nothing (default)')
    parser.add_argument('--apply', action='store_true',
                        help='download, verify and install the upstream snapshot')
    parser.add_argument('--repository', default=None,
                        help='upstream repository as owner/name (default %(default)s)'
                             % {'default': default_repository})
    parser.add_argument('--branch', default=None,
                        help='branch to follow when --commit is not given (default '
                             + default_branch + ')')
    parser.add_argument('--commit', default=None,
                        help='pin one exact commit instead of the branch head')
    parser.add_argument('--timeout', type=int, default=120,
                        help='download timeout in seconds (default %(default)s)')
    args = parser.parse_args(argv)

    repository = args.repository or default_repository
    ref = args.commit or args.branch or default_branch
    current = local_files()

    try:
        if args.commit:
            commit, committed_at = args.commit, None
        else:
            commit, committed_at = upstream_commit(repository, ref, timeout=min(args.timeout, 30))
    except UpdateError as error:
        print(str(error), file=sys.stderr)
        return 1

    incoming = license_bytes = None
    try:
        incoming, license_bytes, _ = read_archive(
            download_archive(repository, commit, timeout=args.timeout))
    except UpdateError as archive_error:
        # A blocked archive host is a network fact, not a dead end: the API and
        # raw file hosts serve the same commit, just one request per file.
        print('  archive host unavailable; fetching file by file instead')
        print('  (' + str(archive_error).splitlines()[0] + ')')
        try:
            print('  listing the commit ...', flush=True)
            paths = tree_paths(repository, commit, timeout=min(args.timeout, 30))
        except UpdateError as error:
            print(str(error), file=sys.stderr)
            return 1
        print('  %d files, one request each:' % len(paths), flush=True)
        bar = ProgressBar(len(paths))
        try:
            incoming, license_bytes = download_files(repository, commit, paths,
                                                     timeout=args.timeout, progress=bar.update)
        except UpdateError as error:
            print(str(error), file=sys.stderr)
            return 1
        finally:
            # Also on Ctrl+C: leave the bar on its own line, never half drawn.
            bar.close()

    summary, _ = report(current, incoming, manifest, repository=repository, branch=ref,
                        commit=commit, committed_at=committed_at, blocked=not args.apply)
    print(summary)

    if not args.apply:
        return 0

    try:
        verify_corpus(incoming)
        build = build_manifest(incoming, repository=repository, branch=ref, commit=commit,
                               committed_at=committed_at, license_bytes=license_bytes)
        install(incoming, license_bytes, build)
    except UpdateError as error:
        print('\n' + str(error), file=sys.stderr)
        return 1

    print('\nInstalled ' + str(len(incoming)) + ' files from ' + commit[:10]
          + '; UPSTREAM.json rewritten.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
