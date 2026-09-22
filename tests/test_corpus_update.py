"""Corpus updater contract.

The bundled CWT snapshot is what every answer is built on, so replacing it is the
one operation in this project that can quietly make the whole tool wrong.  These
tests pin the properties that matter:

* only ``config/**.cwt`` and the upstream ``LICENSE`` are taken from a release
  archive -- upstream also ships tens of megabytes of generated ``script-docs``
  that must never end up in the snapshot;
* a user's corpus is CRLF on Windows (``core.autocrlf``) while upstream stores
  LF, so line endings must not be reported as drift or every file would look
  changed on every run;
* the swap is all-or-nothing: a failure halfway through must leave the previous
  corpus exactly as it was.

Everything here runs offline against synthetic archives.
"""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stellaris_modder_agent import corpus  # noqa: E402

TOP = 'cwtools-stellaris-config-abc1234'
VALID = 'alias[name:name] = localisation\n'
ALIASES = '# no `AND = {...}` usages at atm (game version 4.5), so this is enough\n' + VALID


def member(tar, name, blob):
    info = tarfile.TarInfo(name)
    info.size = len(blob)
    tar.addfile(info, io.BytesIO(blob))


def archive(files, license_bytes=b'MIT License\n', extra=None, top=TOP):
    """A GitHub-shaped tar.gz: one top-level directory holding config/."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        for relative, blob in files.items():
            member(tar, top + '/config/' + relative, blob)
        if license_bytes is not None:
            member(tar, top + '/LICENSE', license_bytes)
        for name, blob in (extra or {}).items():
            member(tar, top + '/' + name, blob)
    return buffer.getvalue()


class ArchiveCase(unittest.TestCase):
    def test_only_the_corpus_and_the_licence_are_taken(self):
        blob = archive({'aliases.cwt': ALIASES.encode(), 'common/buildings.cwt': VALID.encode()},
                       extra={'script-docs/v4.5.0/modifiers.log': b'x' * 5000,
                              'script-files/00_defines.txt': b'y',
                              'README.md': b'# nope'})
        files, license_bytes, top = corpus.read_archive(blob)
        self.assertEqual(sorted(files), ['aliases.cwt', 'common/buildings.cwt'])
        self.assertEqual(license_bytes, b'MIT License\n')
        self.assertEqual(top, TOP)

    def test_an_archive_without_a_corpus_is_refused(self):
        """Better to keep the old snapshot than to install an empty one."""
        blob = archive({}, extra={'script-docs/a.log': b'x'})
        with self.assertRaises(corpus.UpdateError) as caught:
            corpus.read_archive(blob)
        self.assertIn('no config/**/*.cwt', str(caught.exception))

    def test_a_broken_download_is_refused(self):
        with self.assertRaises(corpus.UpdateError) as caught:
            corpus.read_archive(b'this is not a tar.gz')
        self.assertIn('tar.gz', str(caught.exception))


class FallbackCase(unittest.TestCase):
    """A blocked archive host must not make updating impossible.

    Networks differ in which GitHub hosts they allow, so the updater tries the
    single-request tarball first and falls back to one request per file. Neither
    route may ever install a partial corpus.
    """

    def served(self, payload):
        def fake_get(url, accept=None, timeout=120, attempts=1):
            for path, blob in payload.items():
                if url.endswith('/' + path):
                    return blob
            raise corpus.UpdateError('404 for ' + url)
        return fake_get

    def test_tree_paths_keeps_only_the_corpus(self):
        payload = {'truncated': False, 'tree': [
            {'type': 'blob', 'path': 'config/aliases.cwt'},
            {'type': 'blob', 'path': 'config/common/buildings.cwt'},
            {'type': 'blob', 'path': 'config/README.md'},
            {'type': 'tree', 'path': 'config/common'},
            {'type': 'blob', 'path': 'script-docs/v4.5.0/modifiers.log'},
            {'type': 'blob', 'path': 'LICENSE'},
        ]}
        with patch.object(corpus, '_get', lambda *a, **k: json.dumps(payload).encode()):
            self.assertEqual(corpus.tree_paths('a/b', 'sha'),
                             ['aliases.cwt', 'common/buildings.cwt'])

    def test_a_truncated_tree_is_refused(self):
        """Part of a corpus parses fine, so nothing downstream could spot it."""
        payload = {'truncated': True, 'tree': [{'type': 'blob', 'path': 'config/a.cwt'}]}
        with patch.object(corpus, '_get', lambda *a, **k: json.dumps(payload).encode()):
            with self.assertRaises(corpus.UpdateError) as caught:
                corpus.tree_paths('a/b', 'sha')
        self.assertIn('partial corpus', str(caught.exception))

    def test_download_files_fetches_every_path_and_the_licence(self):
        payload = {'config/a.cwt': b'A', 'config/common/b.cwt': b'B', 'LICENSE': b'MIT'}
        progress = []
        with patch.object(corpus, '_get', self.served(payload)):
            files, licence = corpus.download_files('a/b', 'sha', ['a.cwt', 'common/b.cwt'],
                                                   progress=lambda d, t, name: progress.append((d, t)))
        self.assertEqual(files, {'a.cwt': b'A', 'common/b.cwt': b'B'})
        self.assertEqual(licence, b'MIT')
        self.assertEqual(progress, [(1, 2), (2, 2), (2, 2)],
                         'one report as each file lands, then one for the finish')

    def test_a_missing_licence_does_not_stop_the_download(self):
        """Attribution is not the corpus; install() keeps the previous file."""
        payload = {'config/a.cwt': b'X'}
        with patch.object(corpus, '_get', self.served(payload)):
            files, licence = corpus.download_files('a/b', 'sha', ['a.cwt'])
        self.assertEqual(files, {'a.cwt': b'X'})
        self.assertIsNone(licence)

    def test_main_uses_the_fallback_when_the_archive_host_fails(self):
        head = 'b' * 40
        payload = {'aliases.cwt': b'foo = bar\n'}
        captured = io.StringIO()
        with patch.object(corpus, 'upstream_commit', lambda *a, **k: (head, None)), \
                patch.object(corpus, 'download_archive',
                             side_effect=corpus.UpdateError('archive host unreachable')), \
                patch.object(corpus, 'tree_paths', lambda *a, **k: ['aliases.cwt']), \
                patch.object(corpus, 'download_files', lambda *a, **k: (payload, b'MIT')), \
                patch.object(corpus, 'read_manifest',
                             lambda *a, **k: {'repository': 'https://github.com/a/b'}), \
                patch.object(corpus, 'local_files', lambda *a, **k: {}), \
                patch.object(corpus, 'read_status_cache', lambda *a, **k: None), \
                contextlib.redirect_stdout(captured):
            code = corpus.main(['--check'])
        self.assertEqual(code, 0)
        self.assertIn('archive host unavailable', captured.getvalue())
        self.assertIn('0 local, 1 upstream', captured.getvalue())
        self.assertIn('added       : aliases.cwt', captured.getvalue())

    def test_main_reports_a_total_failure_without_installing(self):
        captured = io.StringIO()
        errors = io.StringIO()
        with patch.object(corpus, 'upstream_commit', lambda *a, **k: ('b' * 40, None)), \
                patch.object(corpus, 'download_archive',
                             side_effect=corpus.UpdateError('archive host unreachable')), \
                patch.object(corpus, 'tree_paths',
                             side_effect=corpus.UpdateError('api host unreachable')), \
                patch.object(corpus, 'read_manifest',
                             lambda *a, **k: {'repository': 'https://github.com/a/b'}), \
                patch.object(corpus, 'local_files', lambda *a, **k: {}), \
                patch.object(corpus, 'install') as installed, \
                contextlib.redirect_stdout(captured), contextlib.redirect_stderr(errors):
            code = corpus.main(['--apply'])
        self.assertEqual(code, 1)
        self.assertIn('api host unreachable', errors.getvalue())
        installed.assert_not_called()


class FakeResponse:
    def __init__(self, blob):
        self.blob = blob

    def read(self):
        return self.blob

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TransportCase(unittest.TestCase):
    """A flaky network must end in a clean message, never a traceback.

    urllib wraps only the *sending* half of a request in ``URLError``: a timeout
    while reading the status line or the body escapes from ``getresponse()`` as a
    bare ``TimeoutError``. Catching only ``URLError`` killed the updater with a
    traceback partway through a 175-file fetch, on exactly the unsteady networks
    the per-file route exists for.
    """

    def get(self, side_effect, attempts=1):
        with patch.object(corpus.urllib.request, 'urlopen', side_effect=side_effect), \
                patch.object(corpus.time, 'sleep', lambda seconds: None):
            return corpus._get('https://example.invalid/x', timeout=1, attempts=attempts)

    def test_a_read_timeout_is_reported_not_raised_raw(self):
        with self.assertRaises(corpus.UpdateError) as caught:
            self.get(TimeoutError('read timed out'))
        self.assertIn('Cannot reach', str(caught.exception))

    def test_a_connection_error_is_reported(self):
        with self.assertRaises(corpus.UpdateError) as caught:
            self.get(corpus.urllib.error.URLError('name resolution failed'))
        self.assertIn('Cannot reach', str(caught.exception))

    def test_a_server_error_is_reported(self):
        error = corpus.urllib.error.HTTPError('https://example.invalid/x', 404, 'Not Found', {}, None)
        with self.assertRaises(corpus.UpdateError) as caught:
            self.get(error)
        self.assertIn('HTTP 404', str(caught.exception))

    def test_a_transient_failure_is_retried(self):
        calls = []

        def flaky(url, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError('read timed out')
            return FakeResponse(b'payload')

        with patch.object(corpus.urllib.request, 'urlopen', flaky), \
                patch.object(corpus.time, 'sleep', lambda seconds: None):
            self.assertEqual(corpus._get('https://example.invalid/x', attempts=4), b'payload')
        self.assertEqual(len(calls), 3)

    def test_retries_are_bounded(self):
        calls = []

        def always_fails(url, timeout=None):
            calls.append(1)
            raise TimeoutError('read timed out')

        with self.assertRaises(corpus.UpdateError):
            self.get(always_fails, attempts=3)
        self.assertEqual(len(calls), 3, 'a broken host must not be retried forever')

    def test_the_per_file_route_asks_for_retries(self):
        """One dropped connection out of 175 must not discard the whole update."""
        seen = {}

        def fake_get(url, accept=None, timeout=120, attempts=1):
            seen[url.rsplit('/', 1)[-1]] = attempts
            return b'x'

        with patch.object(corpus, '_get', fake_get):
            corpus.download_files('a/b', 'sha', ['a.cwt'])
        self.assertGreater(seen['a.cwt'], 1)


class FakeStream(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


class ProgressBarCase(unittest.TestCase):
    """A silent minute reads as a hang, so progress must show from the first file.

    The per-file route is a hundred-odd sequential requests; whoever answers the
    y/N prompt is staring at the window the whole time.
    """

    def bar(self, total, tty):
        return corpus.ProgressBar(total, FakeStream(tty))

    def test_a_terminal_gets_one_line_that_redraws(self):
        stream = FakeStream(True)
        bar = corpus.ProgressBar(4, stream)
        for done in range(1, 5):
            bar.update(done, 4, 'common/buildings.cwt')
        bar.close()
        text = stream.getvalue()
        self.assertEqual(text.count('\r'), 4, 'every update redraws the same line')
        self.assertIn('100%', text)
        self.assertIn('4/4', text)
        self.assertIn('common/buildings.cwt', text)
        self.assertTrue(text.endswith('\n'), 'close() must finish the line')

    def test_it_reports_how_much_is_left(self):
        stream = FakeStream(True)
        with patch.object(corpus.time, 'monotonic', side_effect=[0.0, 1.0]):
            bar = corpus.ProgressBar(200, stream)
            bar.update(1, 200, 'a.cwt')
        self.assertIn('left', stream.getvalue())

    def test_a_captured_stream_gets_plain_lines_instead(self):
        """In-place redraws would be garbage in a log or a pipe."""
        stream = FakeStream(False)
        bar = corpus.ProgressBar(25, stream)
        for done in range(1, 26):
            bar.update(done, 25, 'x.cwt')
        bar.close()
        text = stream.getvalue()
        self.assertNotIn('\r', text)
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(len(lines), 3, 'every %d files, plus the last'
                         % corpus.PROGRESS_EVERY)
        self.assertIn('25/25', lines[-1])

    def test_a_broken_stream_does_not_fail_the_update(self):
        class Broken:
            def isatty(self):
                return True

            def write(self, text):
                raise OSError('pipe closed')

            def flush(self):
                raise OSError('pipe closed')

        bar = corpus.ProgressBar(3, Broken())
        bar.update(1, 3, 'a.cwt')
        bar.close()

    def test_a_long_filename_cannot_wrap_the_line(self):
        stream = FakeStream(True)
        bar = corpus.ProgressBar(2, stream)
        bar.update(1, 2, 'common/' + 'x' * 300 + '.cwt')
        line = stream.getvalue().split('\r')[-1]
        self.assertLessEqual(len(line), corpus.ProgressBar.PAD)


class BudgetCase(unittest.TestCase):
    """A bad network must end in a message, not in an indefinitely busy window."""

    def _get(self, url, accept=None, timeout=120, attempts=1):
        return b'x'

    def test_the_route_gives_up_at_its_budget(self):
        clock = iter([0.0, 1000.0])
        with patch.object(corpus, '_get', self._get), \
                patch.object(corpus.time, 'monotonic', lambda: next(clock, 1000.0)):
            with self.assertRaises(corpus.UpdateError) as caught:
                corpus.download_files('a/b', 'sha', ['a.cwt', 'b.cwt'], budget=10, workers=1)
        message = str(caught.exception)
        self.assertIn('Stopped after 1 of 2 files', message)
        self.assertIn('unchanged', message)


class ParallelCase(unittest.TestCase):
    """Overlap the requests; the cost here is latency, not bandwidth.

    Measured against the real host: a 4 KB file and a 200 KB file cost about the
    same, and the TCP+TLS handshake is under a tenth of either. So the speedup
    comes from having several requests in flight, not from moving bytes faster.
    """

    def test_requests_overlap_and_stay_within_the_limit(self):
        lock = threading.Lock()
        active = []
        peak = []

        def slow(url, accept=None, timeout=120, attempts=1):
            with lock:
                active.append(1)
                peak.append(len(active))
            time.sleep(0.05)
            with lock:
                active.pop()
            return b'x'

        paths = ['f%02d.cwt' % index for index in range(12)]
        with patch.object(corpus, '_get', slow):
            files, _ = corpus.download_files('a/b', 'sha', paths, workers=4)
        self.assertEqual(len(files), 12)
        self.assertGreater(max(peak), 1, 'requests must overlap')
        self.assertLessEqual(max(peak), 4, 'and stay inside the worker limit')

    def test_one_worker_still_works(self):
        with patch.object(corpus, '_get', lambda *a, **k: b'x'):
            files, _ = corpus.download_files('a/b', 'sha', ['a.cwt', 'b.cwt'], workers=1)
        self.assertEqual(sorted(files), ['a.cwt', 'b.cwt'])


class IncrementalCase(unittest.TestCase):
    """Fetch what changed, not the whole snapshot.

    A release moves a handful of files while the snapshot holds a hundred and
    seventy; every file not fetched is a request not paid for.
    """

    def compare(self, entries, cap=False):
        payload = {'files': entries}
        with patch.object(corpus, '_get', lambda *a, **k: json.dumps(payload).encode()):
            return corpus.changed_paths('a/b', 'base', 'head')

    def test_only_corpus_files_are_listed(self):
        changed, removed, complete = self.compare([
            {'filename': 'config/effects.cwt', 'status': 'modified'},
            {'filename': 'config/common/new.cwt', 'status': 'added'},
            {'filename': 'config/gone.cwt', 'status': 'removed'},
            {'filename': 'config/notes.md', 'status': 'modified'},
            {'filename': 'script-docs/v4.5.0/effects.log', 'status': 'added'},
        ])
        self.assertEqual(changed, ['common/new.cwt', 'effects.cwt'])
        self.assertEqual(removed, ['gone.cwt'])
        self.assertTrue(complete)

    def test_a_capped_file_list_is_not_trusted(self):
        """A truncated list read as complete would silently miss files."""
        entries = [{'filename': 'config/f%03d.cwt' % i, 'status': 'modified'}
                   for i in range(corpus.COMPARE_FILE_CAP)]
        with patch.object(corpus, '_get',
                          lambda *a, **k: json.dumps({'files': entries}).encode()):
            _, _, complete = corpus.changed_paths('a/b', 'base', 'head')
        self.assertFalse(complete)

    def test_a_missing_file_list_is_not_trusted(self):
        with patch.object(corpus, '_get', lambda *a, **k: json.dumps({}).encode()):
            self.assertEqual(corpus.changed_paths('a/b', 'base', 'head'), ([], [], False))

    def test_a_matching_tree_can_be_diffed(self):
        manifest = {'commit_sha': 'a' * 40, 'files': ['aliases.cwt', 'effects.cwt']}
        current = {'aliases.cwt': b'x', 'effects.cwt': b'y'}
        self.assertEqual(corpus.incremental_base(current, manifest), 'a' * 40)

    def test_a_hand_edited_tree_cannot_be_diffed(self):
        """If the files do not match the manifest, there is no baseline to trust."""
        manifest = {'commit_sha': 'a' * 40, 'files': ['aliases.cwt', 'effects.cwt']}
        self.assertIsNone(corpus.incremental_base({'aliases.cwt': b'x'}, manifest))
        self.assertIsNone(corpus.incremental_base(
            {'aliases.cwt': b'x', 'effects.cwt': b'y', 'extra.cwt': b'z'}, manifest))

    def test_no_manifest_means_a_full_download(self):
        self.assertIsNone(corpus.incremental_base({}, {}))
        self.assertIsNone(corpus.incremental_base({'a.cwt': b'x'},
                                                  {'commit_sha': 'a' * 40}))

    def test_changed_files_are_merged_over_the_local_snapshot(self):
        with patch.object(corpus, 'upstream_commit', lambda *a, **k: ('b' * 40, None)), \
                patch.object(corpus, 'changed_paths',
                             lambda *a, **k: (['effects.cwt', 'brand_new.cwt'],
                                              ['gone.cwt'], True)), \
                patch.object(corpus, 'download_files',
                             lambda *a, **k: ({'effects.cwt': b'new',
                                               'brand_new.cwt': b'new'}, None)), \
                patch.object(corpus, 'read_manifest',
                             lambda *a, **k: {'repository': 'https://github.com/a/b',
                                              'commit_sha': 'a' * 40,
                                              'files': ['aliases.cwt', 'effects.cwt',
                                                        'gone.cwt']}), \
                patch.object(corpus, 'local_files',
                             lambda *a, **k: {'aliases.cwt': b'keep', 'effects.cwt': b'old',
                                              'gone.cwt': b'x'}), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            code = corpus.main(['--check'])
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn('1 added, 1 updated, 1 removed, 1 untouched', text)
        self.assertIn('added       : brand_new.cwt', text)
        self.assertIn('changed     : effects.cwt', text)
        self.assertIn('removed     : gone.cwt', text)
        self.assertNotIn('aliases.cwt', text.split('changed     :')[1].split('\n')[0])

    def test_repeated_progress_lines_are_not_repeated(self):
        """The closing report would otherwise print the same count twice."""
        stream = FakeStream(False)
        bar = corpus.ProgressBar(2, stream)
        bar.update(2, 2, 'done')
        bar.update(2, 2, '')
        bar.close()
        self.assertEqual(len([line for line in stream.getvalue().splitlines() if line.strip()]), 1)

    def test_full_forces_every_file(self):
        with patch.object(corpus, 'changed_paths') as diff, \
                patch.object(corpus, 'upstream_commit', lambda *a, **k: ('b' * 40, None)), \
                patch.object(corpus, 'read_manifest',
                             lambda *a, **k: {'repository': 'https://github.com/a/b',
                                              'commit_sha': 'a' * 40,
                                              'files': ['aliases.cwt']}), \
                patch.object(corpus, 'local_files',
                             lambda *a, **k: {'aliases.cwt': b'x'}), \
                patch.object(corpus, 'read_archive',
                             lambda blob: ({'aliases.cwt': b'x'}, None, 'top')), \
                patch.object(corpus, 'download_archive', lambda *a, **k: b''), \
                contextlib.redirect_stdout(io.StringIO()):
            corpus.main(['--check', '--full'])
        diff.assert_not_called()


class RepeatedRoundCase(unittest.TestCase):
    """One stalled connection must not discard the files that already arrived.

    A partial corpus can never be installed, so without another sweep over the
    stragglers a single bad moment late in the run is fatal.
    """

    def test_a_file_that_fails_a_round_is_picked_up_in_the_next(self):
        seen = []

        def flaky(url, accept=None, timeout=120, attempts=1):
            name = url.rsplit('/', 1)[-1]
            seen.append(name)
            if name == 'b.cwt' and seen.count('b.cwt') < 2:
                raise corpus.UpdateError('read timed out')
            return b'x'

        with patch.object(corpus, '_get', flaky):
            files, _ = corpus.download_files('a/b', 'sha', ['a.cwt', 'b.cwt'], rounds=3)
        self.assertEqual(sorted(files), ['a.cwt', 'b.cwt'])
        self.assertEqual(seen.count('a.cwt'), 1, 'a file that arrived is not fetched twice')
        self.assertEqual(seen.count('b.cwt'), 2, 'the straggler is swept again')

    def test_giving_up_names_the_files_it_could_not_get(self):
        with patch.object(corpus, '_get',
                          side_effect=corpus.UpdateError('read timed out')):
            with self.assertRaises(corpus.UpdateError) as caught:
                corpus.download_files('a/b', 'sha', ['a.cwt', 'b.cwt'], rounds=2)
        message = str(caught.exception)
        self.assertIn('Could not fetch 2 of 2 files after 2 rounds', message)
        self.assertIn('unchanged', message)
        self.assertIn('a.cwt', message)


class VersionMarkerCase(unittest.TestCase):
    def test_marker_is_read_from_aliases(self):
        files, _, _ = corpus.read_archive(archive({'aliases.cwt': ALIASES.encode()}))
        self.assertEqual(corpus.detect_version(files), '4.5')

    def test_no_marker_reports_none_instead_of_guessing(self):
        files, _, _ = corpus.read_archive(archive({'aliases.cwt': VALID.encode()}))
        self.assertIsNone(corpus.detect_version(files))
        manifest = corpus.build_manifest(files, repository='a/b', branch='master',
                                         commit='c' * 40, committed_at=None,
                                         license_bytes=None)
        self.assertIsNone(manifest['stellaris_version'])
        self.assertIn('No game-version marker', manifest['stellaris_version_basis'])


class ComparisonCase(unittest.TestCase):
    def test_line_endings_are_not_drift(self):
        """The real bug: autocrlf made all 173 files look changed on every run."""
        current = {'a.cwt': b'one\ntwo\n'}
        incoming = {'a.cwt': b'one\r\ntwo\r\n'}
        added, removed, changed, unchanged = corpus.compare(current, incoming)
        self.assertEqual((added, removed, changed), ([], [], []))
        self.assertEqual(unchanged, ['a.cwt'])

    def test_real_content_change_is_still_reported(self):
        added, removed, changed, unchanged = corpus.compare(
            {'a.cwt': b'one\n', 'gone.cwt': b'x\n'},
            {'a.cwt': b'one more\n', 'new.cwt': b'y\n'})
        self.assertEqual(added, ['new.cwt'])
        self.assertEqual(removed, ['gone.cwt'])
        self.assertEqual(changed, ['a.cwt'])
        self.assertEqual(unchanged, [])

    def test_line_endings_are_classified(self):
        self.assertEqual(corpus.line_endings({'a': b'x\r\ny\r\n'}), 'crlf')
        self.assertEqual(corpus.line_endings({'a': b'x\ny\n'}), 'lf')
        self.assertEqual(corpus.line_endings({'a': b'x\r\ny\n'}), 'mixed')
        self.assertEqual(corpus.line_endings({}), 'none')


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='stellaris-corpus-'))
        self.config = self.base / 'config'
        self.license = self.base / 'LICENSE.cwt'
        self.manifest = self.base / 'UPSTREAM.json'
        (self.config / 'common').mkdir(parents=True)
        (self.config / 'aliases.cwt').write_bytes(b'old\n')
        (self.config / 'common' / 'stale.cwt').write_bytes(b'delete me\n')
        self.license.write_bytes(b'old licence\n')
        self.manifest.write_text(json.dumps({'commit_sha': 'old'}), encoding='utf-8')

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def install(self, files, manifest):
        corpus.install(files, b'new licence\n', manifest, config_dir=self.config,
                       license_path=self.license, manifest_path=self.manifest)

    def test_install_replaces_the_whole_snapshot(self):
        self.install({'aliases.cwt': b'new\n', 'common/fresh.cwt': b'added\n'},
                     {'commit_sha': 'new', 'files': ['aliases.cwt']})
        self.assertEqual((self.config / 'aliases.cwt').read_bytes(), b'new\n')
        self.assertEqual((self.config / 'common' / 'fresh.cwt').read_bytes(), b'added\n')
        self.assertFalse((self.config / 'common' / 'stale.cwt').exists(),
                         'files that left upstream must not survive the swap')
        self.assertEqual(self.license.read_bytes(), b'new licence\n')
        self.assertEqual(json.loads(self.manifest.read_text('utf-8'))['commit_sha'], 'new')

    def test_a_failure_midway_restores_everything(self):
        """An un-serialisable manifest stands in for any failure after the rename."""
        with self.assertRaises(TypeError):
            self.install({'aliases.cwt': b'new\n'}, {'commit_sha': {'not', 'json'}})
        self.assertEqual((self.config / 'aliases.cwt').read_bytes(), b'old\n')
        self.assertEqual((self.config / 'common' / 'stale.cwt').read_bytes(), b'delete me\n')
        self.assertEqual(self.license.read_bytes(), b'old licence\n')
        self.assertEqual(json.loads(self.manifest.read_text('utf-8'))['commit_sha'], 'old')
        leftovers = [p.name for p in self.base.iterdir() if p.name.startswith('.staging')
                     or p.name.endswith('.previous')]
        self.assertEqual(leftovers, [], 'staging and backup directories must be cleaned up')


class VerificationCase(unittest.TestCase):
    def test_a_parseable_corpus_passes(self):
        corpus.verify_corpus({'aliases.cwt': ALIASES.encode(), 'common/x.cwt': VALID.encode()})

    def test_an_unparseable_file_stops_the_update(self):
        with self.assertRaises(corpus.UpdateError) as caught:
            corpus.verify_corpus({'broken.cwt': b'thing = {\n'})
        self.assertIn('broken.cwt', str(caught.exception))


class DefaultsCase(unittest.TestCase):
    def test_repository_comes_from_the_manifest(self):
        """A fork or mirror keeps updating from itself, not from the default."""
        repository, branch = corpus._defaults(
            {'repository': 'https://github.com/someone/cwtools-stellaris-config',
             'branch': 'stable'})
        self.assertEqual(repository, 'someone/cwtools-stellaris-config')
        self.assertEqual(branch, 'stable')

    def test_missing_manifest_falls_back_to_the_project_default(self):
        repository, branch = corpus._defaults({})
        self.assertEqual(repository, corpus.DEFAULT_REPOSITORY)
        self.assertEqual(branch, corpus.DEFAULT_BRANCH)


class StalenessCase(unittest.TestCase):
    """The launch-time hint: cheap, cached, and never fatal.

    ``start.py`` runs on every client session, so this check must not download an
    archive, must not ask GitHub more than about once a day, and must treat an
    unreachable upstream as "nothing to say" rather than as an error.
    """

    LOCAL = 'a' * 40
    HEAD = 'b' * 40

    def status(self, local=None, head=None, ahead_by=3, commit_raises=False, compare_raises=False):
        local = self.LOCAL if local is None else local
        head = self.HEAD if head is None else head

        def fake_commit(repository, ref, timeout=30):
            if commit_raises:
                raise corpus.UpdateError('offline')
            return head, '2026-01-01T00:00:00Z'

        def fake_get(url, accept=None, timeout=120):
            if compare_raises:
                raise corpus.UpdateError('compare unavailable')
            return json.dumps({'ahead_by': ahead_by}).encode()

        manifest = {'repository': 'https://github.com/a/b', 'commit_sha': local}
        with patch.object(corpus, 'upstream_commit', fake_commit), \
                patch.object(corpus, '_get', fake_get):
            return corpus.upstream_status(manifest)

    def test_unreachable_upstream_reports_nothing(self):
        """Offline is the normal case, not a failure."""
        self.assertIsNone(self.status(commit_raises=True))

    def test_up_to_date_is_zero(self):
        self.assertEqual(self.status(local=self.HEAD)['behind'], 0)

    def test_behind_count_comes_from_the_compare_api(self):
        status = self.status(ahead_by=12)
        self.assertEqual(status['behind'], 12)
        self.assertEqual(status['local'], self.LOCAL)
        self.assertEqual(status['upstream'], self.HEAD)

    def test_an_unavailable_compare_still_reports_being_behind(self):
        """Knowing "some" beats reporting "current" when the count is unknown."""
        status = self.status(compare_raises=True)
        self.assertIsNone(status['behind'])
        self.assertIsNotNone(corpus.update_hint(status))


class StatusCacheCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='stellaris-check-'))
        self.cache = self.base / 'check.json'

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_missing_cache_reads_as_none(self):
        self.assertIsNone(corpus.read_status_cache(self.cache))

    def test_unreadable_cache_reads_as_none(self):
        self.cache.write_text('{ not json', encoding='utf-8')
        self.assertIsNone(corpus.read_status_cache(self.cache))

    def test_expired_cache_reads_as_none(self):
        corpus.write_status_cache({'checked_at': '2020-01-01T00:00:00+00:00', 'behind': 3},
                                  self.cache)
        self.assertIsNone(corpus.read_status_cache(self.cache, max_age=60))

    def test_fresh_cache_is_returned(self):
        corpus.write_status_cache({'checked_at': '2030-01-01T00:00:00+00:00', 'behind': 3},
                                  self.cache)
        self.assertEqual(corpus.read_status_cache(self.cache, max_age=60)['behind'], 3)

    def test_a_second_call_does_not_ask_github_again(self):
        calls = []

        def fake_status(**kwargs):
            calls.append(kwargs)
            return {'local': 'a', 'upstream': 'b', 'behind': 5,
                    'checked_at': '2030-01-01T00:00:00+00:00'}

        with patch.object(corpus, 'upstream_status', fake_status):
            first = corpus.cached_status(self.cache, max_age=60)
            second = corpus.cached_status(self.cache, max_age=60)
        self.assertEqual(len(calls), 1, 'a fresh cache must not trigger another request')
        self.assertEqual(first['behind'], second['behind'])

    def test_an_unwritable_cache_is_not_fatal(self):
        corpus.write_status_cache({'checked_at': 'x'}, self.base / 'no' / 'such' / 'dir.json')


class HintCase(unittest.TestCase):
    def test_nothing_to_say_when_current(self):
        self.assertIsNone(corpus.update_hint({'behind': 0}))
        self.assertIsNone(corpus.update_hint(None))

    def test_counts_are_shown(self):
        hint = corpus.update_hint({'behind': 12, 'local': 'a' * 40, 'upstream': 'b' * 40})
        self.assertIn('12', hint)
        self.assertIn('update-corpus', hint)

    def test_it_says_nothing_changes_by_itself(self):
        hint = corpus.update_hint({'behind': 1, 'local': 'a' * 40, 'upstream': 'b' * 40})
        self.assertIn('stays offline', hint)


class YesNoCase(unittest.TestCase):
    """Only a clear yes may change anything on disk."""

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        import start  # noqa: PLC0415
        self.start = start
        self.addCleanup(patch.stopall)
        patch.object(start, 'say', lambda text='': None).start()

    def answer(self, text):
        with patch.object(self.start, 'input_ready', lambda timeout: True), \
                patch('builtins.input', lambda prompt='': text):
            return self.start.ask_yes_no('? ')

    def test_yes_in_every_spelling(self):
        for text in ('y', 'Y', 'yes', 'YES', ' y ', '是'):
            with self.subTest(answer=text):
                self.assertTrue(self.answer(text))

    def test_anything_else_is_no(self):
        for text in ('', 'n', 'no', 'nope', 'q', 'yes please'):
            with self.subTest(answer=text):
                self.assertFalse(self.answer(text))

    def test_a_closed_stdin_is_no_not_a_crash(self):
        with patch.object(self.start, 'input_ready', lambda timeout: True):
            with patch('builtins.input', side_effect=EOFError):
                self.assertFalse(self.start.ask_yes_no('? '))
            with patch('builtins.input', side_effect=KeyboardInterrupt):
                self.assertFalse(self.start.ask_yes_no('? '))

    def test_silence_is_no_and_never_asks(self):
        """A hidden or service console has a tty stdin and nobody to type in it.

        Waiting on ``isatty()`` alone is what hung an unattended launch on the
        question forever; the prompt has to expire.
        """
        with patch.object(self.start, 'input_ready', lambda timeout: False), \
                patch('builtins.input', side_effect=AssertionError('must not be asked')):
            self.assertFalse(self.start.ask_yes_no('? ', timeout=0.01))


class ConfirmUpdateCase(unittest.TestCase):
    """The interactive offer that runs before the server starts.

    Two properties matter: saying no (or saying nothing) must leave the corpus
    alone, and no failure anywhere in here may stop the launch -- the window is
    the only feedback a double-clicking user gets.
    """

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        import start  # noqa: PLC0415
        self.start = start
        self.said = []
        self.addCleanup(patch.stopall)
        patch.object(start, 'say', lambda text='': self.said.append(text)).start()

    def status(self, behind):
        return {'local': 'a' * 40, 'upstream': 'b' * 40, 'behind': behind,
                'checked_at': '2030-01-01T00:00:00+00:00'}

    def confirm(self, *, behind=12, answer='y', apply_code=0, check_error=None,
                cache=None):
        from stellaris_modder_agent import corpus
        args = type('Args', (), {'update_timeout': 5.0})()

        def cached_status(**kwargs):
            if check_error is not None:
                raise check_error
            return self.status(behind)

        applications = []
        with patch.object(corpus, 'cached_status', cached_status), \
                patch.object(corpus, 'read_status_cache', lambda *a, **k: None), \
                patch.object(corpus, 'main',
                             lambda argv: (applications.append(argv), apply_code)[1]), \
                patch.object(corpus, 'cache_path', lambda: cache or Path(os.devnull)), \
                patch.object(self.start, 'input_ready', lambda timeout: True), \
                patch('builtins.input', lambda prompt='': answer):
            self.start.confirm_corpus_update(args)
        return applications

    def output(self):
        return '\n'.join(self.said)

    def test_a_current_corpus_asks_nothing(self):
        self.assertEqual(self.confirm(behind=0), [])
        self.assertNotIn('Update now?', self.output())

    def test_yes_installs_the_update(self):
        self.assertEqual(self.confirm(answer='y'), [['--apply']])
        self.assertIn('Corpus updated', self.output())

    def test_no_leaves_the_corpus_alone(self):
        self.assertEqual(self.confirm(answer='n'), [])
        self.assertIn('Skipped', self.output())

    def test_pressing_enter_leaves_the_corpus_alone(self):
        self.assertEqual(self.confirm(answer=''), [])

    def test_a_failed_update_still_starts_the_server(self):
        self.assertEqual(self.confirm(answer='y', apply_code=1), [['--apply']])
        self.assertIn('did not finish', self.output())
        self.assertNotIn('Corpus updated', self.output())

    def test_an_unreachable_check_says_nothing_about_updates(self):
        """Offline is the normal case: no prompt, no claim, no exception.

        The one line that does appear is the "checking" note, which has to be
        printed before the wait -- a silent five seconds reads as a hang too.
        """
        applications = self.confirm(check_error=corpus.UpdateError('offline'))
        self.assertEqual(applications, [])
        self.assertNotIn('Update now?', self.output())
        self.assertNotIn('behind', self.output())
        self.assertIn('Checking for corpus updates', self.output())

    def test_a_cached_verdict_is_dropped_after_a_successful_update(self):
        """Otherwise the next launch offers the update that already happened."""
        from stellaris_modder_agent import corpus
        cache = Path(tempfile.mkdtemp(prefix='stellaris-cache-')) / 'check.json'
        self.addCleanup(shutil.rmtree, cache.parent, ignore_errors=True)
        corpus.write_status_cache(self.status(12), cache)
        self.assertTrue(cache.is_file())
        self.confirm(answer='y', cache=cache)
        self.assertFalse(cache.is_file())

    def test_the_cache_survives_a_skipped_update(self):
        from stellaris_modder_agent import corpus
        cache = Path(tempfile.mkdtemp(prefix='stellaris-cache-')) / 'check.json'
        self.addCleanup(shutil.rmtree, cache.parent, ignore_errors=True)
        corpus.write_status_cache(self.status(12), cache)
        self.confirm(answer='n', cache=cache)
        self.assertTrue(cache.is_file(), 'a declined offer should not cost a re-check')


class LaunchFlagCase(unittest.TestCase):
    """The check is a convenience, so it must be switchable off."""

    def setUp(self):
        sys.path.insert(0, str(ROOT))
        import start  # noqa: PLC0415 - the launcher is a script, not a package module
        self.start = start
        self.addCleanup(lambda: os.environ.pop('STELLARIS_UPDATE_CHECK', None))

    def test_default_is_on(self):
        os.environ.pop('STELLARIS_UPDATE_CHECK', None)
        self.assertTrue(self.start.update_check_enabled(type('A', (), {'no_update_check': False})()))

    def test_flag_turns_it_off(self):
        os.environ.pop('STELLARIS_UPDATE_CHECK', None)
        self.assertFalse(self.start.update_check_enabled(type('A', (), {'no_update_check': True})()))

    def test_environment_turns_it_off(self):
        os.environ['STELLARIS_UPDATE_CHECK'] = '0'
        self.assertFalse(self.start.update_check_enabled(type('A', (), {'no_update_check': False})()))


if __name__ == '__main__':
    unittest.main()
