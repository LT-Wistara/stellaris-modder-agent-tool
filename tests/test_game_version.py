"""Game-version detection contract.

The version this server reports drives ``version_check``, whose whole job is to
tell a modder whether their corpus has drifted from the installed game.  A wrong
version therefore does not show up as an error -- it fabricates drift that does
not exist, or hides drift that does.

``logs/system.log`` opens with graphics and audio initialisation, so a loose
search for the substring "version" in its first lines reads
``OpenGL Version: 4.6.0 NVIDIA 596.21`` as the installed build.  On a real
Stellaris 4.5.0 install that produced "The corpus targets Stellaris 4.5 but 4.6.0
is installed", i.e. the reported game version tracked the *graphics driver*.

These tests pin the source order (explicit marker, then ``launcher-settings.json``,
then the game's own banner) and the noise rule.
"""
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stellaris_modder_agent import environment as env  # noqa: E402

DRIVER_BANNER = 'OpenGL Version: 4.6.0 NVIDIA 596.21'
GAME_BANNER = '[17:55:05][game_application.cpp:250]: Game Version: Cygnus v4.5.0'


class GameVersionCase(unittest.TestCase):
    """Each case builds its own game root and userdata root."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='stellaris-version-'))
        self.game = self.base / 'game'
        self.userdata = self.base / 'userdata'
        self.game.mkdir()
        (self.userdata / 'logs').mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    # region helpers

    def launcher_settings(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (self.game / env.LAUNCHER_SETTINGS).write_text(text, encoding='utf-8')

    def log(self, name, lines):
        (self.userdata / 'logs' / name).write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def read(self):
        return env.read_game_version(self.userdata, self.game)

    # endregion

    # region the regression

    def test_driver_banner_alone_reports_nothing(self):
        """The exact bug: system.log's driver line must not become the version."""
        self.log('system.log', ['[17:55:04][graphicssettings.cpp:785]: Using multisampling: 8',
                                DRIVER_BANNER])
        self.assertIsNone(self.read())

    def test_driver_banner_loses_to_the_game_banner(self):
        self.log('system.log', [DRIVER_BANNER])
        self.log('game.log', [GAME_BANNER])
        self.assertEqual(self.read(), '4.5.0')

    def test_driver_banner_loses_to_launcher_settings(self):
        self.launcher_settings({'rawVersion': 'v4.5.0'})
        self.log('system.log', [DRIVER_BANNER])
        self.assertEqual(self.read(), '4.5.0')

    # endregion

    # region source order

    def test_launcher_settings_raw_version_wins(self):
        self.launcher_settings({'gameId': 'stellaris', 'version': 'Cygnus v4.5.0 (8697)',
                                'modsCompatibilityVersion': '4.5', 'rawVersion': 'v4.5.0'})
        self.assertEqual(self.read(), '4.5.0')

    def test_launcher_settings_falls_back_to_compatibility_version(self):
        self.launcher_settings({'gameId': 'stellaris', 'modsCompatibilityVersion': '4.5'})
        self.assertEqual(self.read(), '4.5')

    def test_launcher_settings_falls_back_to_the_codename_string(self):
        """``version`` mixes a codename and a build number; only the number counts."""
        self.launcher_settings({'gameId': 'stellaris', 'version': 'Cygnus v4.5.0 (8697)'})
        self.assertEqual(self.read(), '4.5.0')

    def test_explicit_marker_still_wins_over_launcher_settings(self):
        """A hosted copy states its version by hand; that statement is deliberate."""
        (self.game / 'version.txt').write_text('4.4.1', encoding='utf-8')
        self.launcher_settings({'rawVersion': 'v4.5.0'})
        self.assertEqual(self.read(), '4.4.1')

    def test_launcher_settings_win_over_the_game_log(self):
        self.launcher_settings({'rawVersion': 'v4.3.2'})
        self.log('game.log', [GAME_BANNER])
        self.assertEqual(self.read(), '4.3.2')

    # endregion

    # region degraded inputs

    def test_unreadable_launcher_settings_fall_through_to_the_log(self):
        self.launcher_settings('{ this is not json')
        self.log('game.log', [GAME_BANNER])
        self.assertEqual(self.read(), '4.5.0')

    def test_launcher_settings_without_any_version_falls_through(self):
        self.launcher_settings({'gameId': 'stellaris'})
        self.assertIsNone(self.read())

    def test_launcher_settings_that_is_not_an_object_is_ignored(self):
        self.launcher_settings('[1, 2, 3]')
        self.log('game.log', [GAME_BANNER])
        self.assertEqual(self.read(), '4.5.0')

    def test_nothing_available_reports_none_not_a_guess(self):
        """A missing version shows up as null; a wrong one fabricates drift."""
        self.assertIsNone(self.read())

    def test_userdata_root_may_be_missing_entirely(self):
        self.launcher_settings({'rawVersion': 'v4.5.0'})
        self.assertEqual(env.read_game_version(None, self.game), '4.5.0')

    def test_plain_stellaris_line_without_the_banner_word_still_counts(self):
        self.log('game.log', ['[17:55:05][pdx.cpp:1]: Stellaris v4.5.0 (8697)'])
        self.assertEqual(self.read(), '4.5.0')

    # endregion

    # region this machine

    def test_whatever_is_installed_here_parses(self):
        """If a real install is present, detection must match its own record.

        Skipped on machines without the game.  The point is that the reported
        version agrees with ``launcher-settings.json`` -- the source that used to
        be ignored in favour of a graphics driver banner.
        """
        detected = env.detect_environment()
        if detected.game_root is None:
            self.skipTest('no Stellaris installation detected on this machine')
        version = env.read_game_version(detected.userdata_root, detected.game_root)
        if version is None:
            self.skipTest('this install exposes neither launcher metadata nor a version log')
        self.assertRegex(version, r'^\d+\.\d+')
        settings = detected.game_root / env.LAUNCHER_SETTINGS
        if settings.is_file():
            recorded = env._version_number(
                json.loads(settings.read_text('utf-8', errors='replace')).get('rawVersion'))
            if recorded:
                self.assertEqual(version, recorded)

    # endregion


if __name__ == '__main__':
    unittest.main()
