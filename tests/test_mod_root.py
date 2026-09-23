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


if __name__ == '__main__':
    unittest.main()
