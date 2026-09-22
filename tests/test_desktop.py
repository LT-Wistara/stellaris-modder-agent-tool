import json
from pathlib import Path
import queue
import socket
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.request

from stellaris_modder_agent.desktop_runtime import (
    Settings, ProcessTask, load_settings, save_settings, client_config, backend_command, update_status)


class DesktopSettingsCase(unittest.TestCase):
    def test_settings_roundtrip_with_unicode_paths(self):
        with tempfile.TemporaryDirectory(prefix='gui-') as temp:
            folder = Path(temp) / '中文 Mod'
            folder.mkdir()
            path = Path(temp) / 'settings.json'
            expected = Settings(port=12345, mod_root=str(folder), watch=False)
            save_settings(path, expected)
            self.assertEqual(load_settings(path), (expected, None))
            self.assertFalse(path.with_suffix('.tmp').exists())

    def test_malformed_settings_recover(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'settings.json'
            for content in ('broken', '[]', '{"port":true}', '{"port":70000}', '{"watch":"yes"}'):
                path.write_text(content, encoding='utf-8')
                actual, warning = load_settings(path)
                self.assertEqual(actual, Settings())
                self.assertTrue(warning)

    def test_missing_settings_use_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(load_settings(Path(temp) / 'missing.json'), (Settings(), None))

    def test_invalid_port_rejected(self):
        for value in (0, -1, 65536, True, '123'):
            with self.assertRaises(ValueError):
                Settings(port=value).validate()

    def test_missing_path_retained_but_start_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings(mod_root=str(Path(temp) / 'missing'))
            path = Path(temp) / 'settings.json'
            path.write_text(json.dumps({'mod_root': settings.mod_root}), encoding='utf-8')
            self.assertEqual(load_settings(path)[0], settings)
            with self.assertRaises(ValueError):
                settings.validate()

    def test_stdio_config_preserves_flags_and_spaces(self):
        settings = Settings(mod_root='D:\\中文 Mod', game_data=False, watch=False)
        result = json.loads(client_config(settings, 'stdio', ['D:\\My Tool\\server.exe']))
        self.assertEqual(result['mcpServers']['stellaris']['args'],
                         ['serve', '--mod-root', 'D:\\中文 Mod', '--no-game-data', '--no-watch'])

    def test_http_configuration_uses_selected_port(self):
        result = json.loads(client_config(Settings(port=9123), 'HTTP'))
        self.assertEqual(result['mcpServers']['stellaris']['url'], 'http://127.0.0.1:9123/mcp')

    def test_codex_config_parses_as_toml(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest('tomllib requires Python 3.11+')
        result = tomllib.loads(client_config(Settings(), 'Codex', ['D:\\Tool\\server.exe']))
        self.assertEqual(result['mcp_servers']['stellaris']['args'], ['serve'])

    def test_frozen_backend_is_console_helper(self):
        import sys
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', 'D:/GUI/app.exe'):
            self.assertEqual(Path(backend_command()[0]).name, 'StellarisModderAgent-server.exe')

    def test_failed_update_check_reports_failure(self):
        from stellaris_modder_agent import corpus
        with patch.object(corpus, 'upstream_status', return_value=None), patch('builtins.print'):
            self.assertEqual(update_status(), 1)


class DesktopProcessCase(unittest.TestCase):
    def wait_event(self, events, target, seconds=25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            kind, event, value = events.get(timeout=max(0.1, deadline - time.monotonic()))
            if event == target:
                return value
        self.fail('No event: ' + target)

    def test_service_start_handshake_stop_releases_port(self):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            port = probe.getsockname()[1]
        events = queue.Queue()
        task = ProcessTask(events, 'service')
        try:
            task.start(['--http', '--no-game-data', '--no-update-check', '--port', str(port)])
            self.wait_event(events, 'ready')
            request = urllib.request.Request(f'http://127.0.0.1:{port}/mcp',
                data=json.dumps({'jsonrpc':'2.0', 'id':1, 'method':'initialize'}).encode(),
                headers={'Content-Type':'application/json'})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=5) as response:
                self.assertEqual(json.load(response)['result']['serverInfo']['name'], 'stellaris-modder-agent-tool')
            with self.assertRaises(RuntimeError):
                task.start([])
        finally:
            task.stop()
            self.wait_event(events, 'done')
            task.thread.join(timeout=5)
        self.assertFalse(task.active)
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(('127.0.0.1', port)), 0)

    def test_occupied_port_never_reports_ready(self):
        events = queue.Queue()
        task = ProcessTask(events, 'service')
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            probe.listen()
            try:
                task.start(['--http', '--no-game-data', '--no-update-check', '--port', str(probe.getsockname()[1])])
                code, stopped, output = self.wait_event(events, 'done')
                self.assertNotEqual(code, 0)
                self.assertNotIn('Server is running.', output)
            finally:
                task.stop()
                task.thread.join(timeout=5)
