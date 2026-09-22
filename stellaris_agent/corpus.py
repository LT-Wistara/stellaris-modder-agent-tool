#!/usr/bin/env python3
"""Refresh the bundled CWT corpus from its upstream repository.

The tool ships a pinned snapshot of ``.cwt`` files so that it works with no
network at all.  This module is the only part of the project that talks to the
internet, and it only runs when you ask it to:

    python stellaris_tool.py update-corpus --check                 # report drift, change nothing
    python stellaris_tool.py update-corpus --apply                 # download and replace
    python stellaris_tool.py update-corpus --apply --commit <sha>  # pin one exact commit
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
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'stellaris_agent' / 'data'
CONFIG_DIR = DATA_DIR / 'config'
MANIFEST_PATH = DATA_DIR / 'UPSTREAM.json'
LICENSE_PATH = DATA_DIR / 'LICENSE.cwt'

DEFAULT_REPOSITORY = 'DragonKnightOfBreeze/cwtools-stellaris-config'
DEFAULT_BRANCH = 'master'

USER_AGENT = 'stellaris-agent-tool corpus updater (+https://github.com/LT-Wistara/stellaris-agent-tool)'
API_COMMIT = 'https://api.github.com/repos/{repository}/commits/{ref}'
API_COMPARE = 'https://api.github.com/repos/{repository}/compare/{base}...{head}'
ARCHIVE = 'https://codeload.github.com/{repository}/tar.gz/{ref}'

# The launch-time staleness check is deliberately cheap: two small API calls and
# no archive download. Its result is cached because start.py runs on every client
# session, and asking GitHub once per session would be rude to the API and slow
# for the user.
CHECK_CACHE_NAME = 'stellaris-agent-tool-update-check.json'
CHECK_MAX_AGE = 24 * 60 * 60

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

def _get(url, accept=None, timeout=120):
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT,
                                                   **({'Accept': accept} if accept else {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        raise UpdateError('Upstream returned HTTP ' + str(error.code) + ' for ' + url)
    except urllib.error.URLError as error:
        raise UpdateError('Cannot reach ' + url + ': ' + str(error.reason)
                          + '\nCheck the network, or keep the bundled corpus as it is.')


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
        '       运行  python stellaris_tool.py update-corpus --check  查看差异，',
        '       或    python stellaris_tool.py update-corpus --apply  下载并替换。',
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
        blob = download_archive(repository, commit, timeout=args.timeout)
        incoming, license_bytes, _ = read_archive(blob)
    except UpdateError as error:
        print(str(error), file=sys.stderr)
        return 1

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
