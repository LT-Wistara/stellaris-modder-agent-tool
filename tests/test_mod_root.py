"""Mod-root placement contract.

This tool reads only the mod it is *installed inside*: it walks up from its own
location to find the single directory that owns ``descriptor.mod``. Anywhere
else the mod's own scripts are not read, so the failure must be reported rather
than guessed around. These tests pin the rule and the warning for every layout a
user can realistically produce.
"""
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stellaris_agent import environment as env  # noqa: E402

DESCRIPTOR = 'name="layout test"\nsupported_version="v4.2.4"\n'


class ModRootCase(unittest.TestCase):
    """Builds each layout in its own temporary tree, then removes it."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix='stellaris-modroot-'))

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def resolve(self, start):
        return env.detect_mod_root(None, start=start)

    def test_descriptor_at_mod_root_is_detected(self):
        mod = self.base / 'mod' / '239854621'
        start = mod / 'stellaris-agent-tool' / 'stellaris_agent' / 'index.py'
        start.parent.mkdir(parents=True)
        (mod / 'descriptor.mod').write_text(DESCRIPTOR, encoding='utf-8')
        (mod / 'common').mkdir()
        root = self.resolve(start)
        self.assertEqual(root, mod)
        self.assertTrue(env.inspect_mod_root(root)['usable'])
        self.assertIsNone(env.inspect_mod_root(root)['reason'])

    def test_tool_nested_under_common_still_finds_the_mod(self):
        mod = self.base / 'mod' / '239854621'
        start = mod / 'common' / 'stellaris-agent-tool' / 'stellaris_agent' / 'index.py'
        start.parent.mkdir(parents=True)
        (mod / 'descriptor.mod').write_text(DESCRIPTOR, encoding='utf-8')
        (mod / 'events').mkdir()
        self.assertEqual(self.resolve(start), mod)

    def test_no_descriptor_anywhere_is_reported_not_guessed(self):
        """A stray common/ above the tool must not be mistaken for a mod."""
        mod = self.base / 'mod' / '239854621'
        start = mod / 'stellaris-agent-tool' / 'stellaris_agent' / 'index.py'
        start.parent.mkdir(parents=True)
        (mod / 'common').mkdir()
        (mod / 'events').mkdir()
        root = self.resolve(start)
        self.assertIsNone(root)
        health = env.inspect_mod_root(root)
        self.assertFalse(health['usable'])
        self.assertIn('descriptor.mod', health['reason'])
        self.assertIn(env.MOD_ENV[0], health['hint'])

    def test_unpacked_into_the_mod_container_is_reported(self):
        """The usual mistake: extracted into mod/ instead of the mod folder."""
        container = self.base / 'mod'
        start = container / 'stellaris-agent-tool' / 'stellaris_agent' / 'index.py'
        start.parent.mkdir(parents=True)
        (container / '239854621' / 'common').mkdir(parents=True)
        self.assertIsNone(self.resolve(start))
        self.assertIn('descriptor.mod', env.inspect_mod_root(None)['reason'])

    def test_explicit_environment_variable_wins(self):
        """A mod genuinely without descriptor.mod stays usable when told."""
        elsewhere = self.base / 'plain-mod'
        (elsewhere / 'common').mkdir(parents=True)
        steps = []
        root = env.detect_mod_root(str(elsewhere), steps=steps)
        self.assertEqual(root, elsewhere)
        self.assertTrue(env.inspect_mod_root(root)['usable'])
        self.assertFalse(env.inspect_mod_root(root)['descriptor'])

    def test_upstream_layout_on_this_checkout(self):
        """Whatever this copy is installed in, detection must not guess.

        Two layouts are legitimate here: the vendored one (a ``descriptor.mod``
        one level up, i.e. the tool sits inside the mod folder) and a loose copy
        with no descriptor.mod anywhere. The second must report "not detected"
        instead of silently claiming the surrounding folder is a mod.
        """
        start = ROOT / 'stellaris_agent' / 'index.py'
        root = self.resolve(start)
        above = ROOT.parent
        health = env.inspect_mod_root(root)
        if (above / env.MOD_DESCRIPTOR).is_file():
            self.assertEqual(root, above)
            self.assertTrue(health['descriptor'])
            self.assertIsNone(health['reason'])
        else:
            self.assertIsNone(root)
            self.assertFalse(health['usable'])
            self.assertIn('descriptor.mod', health['reason'])


if __name__ == '__main__':
    unittest.main()
