"""Launcher and frozen-release regressions."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import start
from stellaris_modder_agent import environment
from stellaris_modder_agent.http_server import create_http_server

ROOT = Path(__file__).resolve().parents[1]


class ReleaseCase(unittest.TestCase):
    def test_print_only_exits_when_stdin_is_piped(self):
        result = subprocess.run([sys.executable, str(ROOT / 'start.py'), '--print-only',
                                 '--no-game-data'], input='', capture_output=True,
                                encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mcpServers', result.stdout)

    def test_explicit_roots_are_forwarded(self):
        args = ['serve', '--mod-root', 'my-mod', '--game-root', 'my-game']
        with patch.object(start, 'detect_environment', return_value=(None, None)) as detect, \
                patch.object(start, 'build_database', return_value=object()), \
                patch('stellaris_modder_agent.server.serve'):
            self.assertEqual(start.run(args), 0)
        detect.assert_called_once_with(True, 'my-game', 'my-mod')

    def test_frozen_config_calls_exe_without_script(self):
        output = io.StringIO()
        with patch.object(sys, 'frozen', True, create=True), contextlib.redirect_stdout(output):
            start.print_snippets('http://127.0.0.1:8765/mcp')
        self.assertIn('serve', output.getvalue())
        self.assertNotIn('start.py', output.getvalue())

    def test_printed_stdio_config_keeps_manual_mod_root(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            start.print_snippets('http://127.0.0.1:8765/mcp', ['--mod-root', 'D:/My Mod'])
        self.assertIn('"--mod-root", "D:/My Mod"', output.getvalue())

    def test_frozen_mod_placement_is_not_used(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            mod = Path(temp)
            (mod / 'descriptor.mod').write_text('name="test"', encoding='utf-8')
            exe = mod / 'tool' / 'StellarisModderAgent.exe'
            with patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, 'executable', str(exe)):
                self.assertIsNone(environment.detect_mod_root())
                self.assertEqual(environment.detect_mod_root(str(mod)), mod)

    def test_stdio_start_failure_keeps_stdout_clean(self):
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(start, 'build_database', side_effect=ValueError('bad corpus')), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(start.main(['serve', '--no-game-data']), 1)
        self.assertEqual(output.getvalue(), '')
        self.assertIn('bad corpus', errors.getvalue())

    def test_port_probe_checks_server_identity_and_full_response(self):
        for name, expected in [('stellaris-modder-agent-tool', True), ('other-server', False)]:
            payload = {'result': {'instructions': 'x' * 500, 'serverInfo': {'name': name}}}
            with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(payload).encode())):
                self.assertEqual(start.port_in_use('127.0.0.1', 8765)[0], expected)
        with patch('urllib.request.urlopen', side_effect=urllib.error.HTTPError(
                'http://localhost', 403, 'Forbidden', {}, None)):
            self.assertFalse(start.port_in_use('127.0.0.1', 8765)[0])

    def test_ipv6_loopback_can_bind_and_url_is_valid(self):
        import socket
        if not socket.has_ipv6:
            self.skipTest('IPv6 unavailable')
        server = create_http_server(object(), host='::1', port=0)
        try:
            self.assertEqual(server.address_family, socket.AF_INET6)
            self.assertEqual(start.http_url('::1', server.server_port),
                             f'http://[::1]:{server.server_port}/mcp')
        finally:
            server.server_close()

    def test_cli_text_mode_searches_mod_content(self):
        with tempfile.TemporaryDirectory() as temp:
            mod = Path(temp)
            (mod / 'events').mkdir()
            (mod / 'events' / 'demo.txt').write_text('# release_text_needle\n', encoding='utf-8')
            result = subprocess.run([sys.executable, str(ROOT / 'stellaris_modder_tool.py'),
                                     '--mod-root', temp, 'search', 'release_text_needle',
                                     '--mode', 'text'], capture_output=True, encoding='utf-8', timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('release_text_needle', result.stdout)
            self.assertIn('events/demo.txt', result.stdout)


if __name__ == '__main__':
    unittest.main()
