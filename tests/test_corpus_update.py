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
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stellaris_agent import corpus  # noqa: E402

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


class LaunchFlagCase(unittest.TestCase):
    """The hint is a convenience, so it must be switchable off."""

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
