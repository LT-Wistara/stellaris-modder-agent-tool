"""Mod data is read only from a directory the user explicitly configures."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from stellaris_modder_agent import environment as env  # noqa: E402
from stellaris_modder_agent.index import Database  # noqa: E402


class ModRootCase(unittest.TestCase):
    def test_placement_does_not_select_mod_even_with_descriptor(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            mod = Path(temp)
            (mod / 'descriptor.mod').write_text('name="example"', encoding='utf-8')
            tool = mod / 'tool' / 'environment.py'
            tool.parent.mkdir()
            steps = []
            self.assertIsNone(env.detect_mod_root(start=tool, steps=steps))
            self.assertEqual(steps[-1].source, 'manual')
            self.assertIn('--mod-root', env.inspect_mod_root(None)['hint'])

    def test_explicit_path_is_used_with_or_without_descriptor(self):
        with tempfile.TemporaryDirectory() as temp:
            mod = Path(temp)
            self.assertEqual(env.detect_mod_root(str(mod)), mod)
            self.assertTrue(env.inspect_mod_root(mod)['usable'])
            self.assertFalse(env.inspect_mod_root(mod)['descriptor'])
            (mod / 'descriptor.mod').write_text('name="example"', encoding='utf-8')
            self.assertTrue(env.inspect_mod_root(mod)['descriptor'])

    def test_environment_variable_is_manual_configuration(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
                os.environ, {'STELLARIS_MOD_ROOT': temp}, clear=True):
            self.assertEqual(env.detect_mod_root(), Path(temp))

    def test_multiple_mod_directories_are_both_indexed(self):
        with tempfile.TemporaryDirectory() as temp:
            roots = [Path(temp) / 'first', Path(temp) / 'second']
            for root, name in zip(roots, ('first_mod_trigger', 'second_mod_trigger')):
                folder = root / 'common' / 'scripted_triggers'
                folder.mkdir(parents=True)
                (folder / 'shared_filename.txt').write_text(name + ' = { always = yes }', encoding='utf-8')
            with patch.object(env, 'detect_game_root', return_value=None):
                environment = env.detect_environment(mod_root=list(map(str, roots)))
            self.assertEqual(environment.mod_roots, roots)
            database = Database(game_data=True, environment=environment, watch=False)
            for name in ('first_mod_trigger', 'second_mod_trigger'):
                self.assertEqual(database.search(name, type='trigger')['status'], 'CONFIRMED_GAME_DATA')
            entries = database.dynamic.index.stats['entries']
            self.assertEqual(entries, [('mod', str(roots[0])), ('mod-2', str(roots[1]))])


if __name__ == '__main__':
    unittest.main()
