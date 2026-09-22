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
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest

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


if __name__ == '__main__':
    unittest.main()
