"""UI state tests use application objects directly, with no desktop input."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

try:
    from stellaris_modder_agent.desktop import Desktop
except ImportError:
    Desktop = None


@unittest.skipIf(Desktop is None, 'Install requirements-gui.txt to test the desktop')
class DesktopStateCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = Desktop(Path(cls.temp.name) / 'settings.json')
        cls.app.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.app.destroy()
        cls.temp.cleanup()

    def test_ready_and_stop_states(self):
        self.app.events.put(('service', 'ready', None))
        self.app.poll()
        self.assertTrue(self.app.running)
        self.assertIn('运行中', self.app.status_label.cget('text'))
        self.assertEqual(self.app.update_button.cget('state'), 'disabled')
        self.assertEqual(self.app.service_button.cget('text'), '停止服务')
        self.app.events.put(('service', 'done', (0, True, '')))
        self.app.poll()
        self.assertFalse(self.app.running)
        self.assertEqual(self.app.service_button.cget('text'), '启动服务')
        self.assertEqual(self.app.service_button.cget('state'), 'normal')

    def test_update_success_refreshes_corpus(self):
        self.app.job_action = 'update'
        self.app.events.put(('job', 'done', (0, False, 'installed')))
        with patch.object(self.app, 'refresh_corpus') as refresh:
            self.app.poll()
        refresh.assert_called_once()
        self.assertIn('更新完成', self.app.update_message.cget('text'))

    def test_update_failure_restores_controls(self):
        self.app.job_action = 'update'
        self.app.events.put(('job', 'done', (1, False, 'network failed')))
        self.app.poll()
        self.assertIn('未完成', self.app.update_message.cget('text'))
        self.assertEqual(self.app.service_button.cget('state'), 'normal')

    def test_check_result_distinguishes_current_and_new(self):
        self.app.job_action = 'check'
        for remote, expected in [('abc', '最新'), ('def', '发现')]:
            self.app.events.put(('job', 'done', (0, False, json.dumps({'local':'abc', 'upstream':remote}))))
            self.app.poll()
            self.assertIn(expected, self.app.update_message.cget('text'))
            self.assertEqual(self.app.update_button.cget('text'),
                             '检查更新' if remote == 'abc' else '更新语料')

    def test_update_progress_uses_reported_fraction(self):
        self.app.events.put(('job', 'log', '@@GUI_PROGRESS@@' + json.dumps(
            {'fraction': 0.45, 'label': '已下载 10/20 个文件'})))
        self.app.poll()
        self.assertAlmostEqual(self.app.progress.get(), 0.45)
        self.assertIn('10/20', self.app.progress_label.cget('text'))

    def test_hidden_pages_are_unmapped(self):
        self.app.show_page('about')
        self.assertEqual(self.app.pages['overview'].winfo_manager(), '')
        self.assertEqual(self.app.pages['about'].winfo_manager(), 'grid')
        self.app.show_page('overview')

    def test_starting_button_spins_and_restores(self):
        from stellaris_modder_agent.desktop import SPINNER
        self.app._render_service_button('starting', False)
        self.assertIn(self.app.service_button.cget('text'), SPINNER)
        self.assertEqual(self.app.service_button.cget('state'), 'disabled')
        self.assertEqual(self.app.service_button._canvas.cget('cursor'), 'watch')
        self.assertEqual(self.app.service_button._text_label.cget('cursor'), 'watch')
        self.assertFalse(hasattr(self.app, 'service_progress'))
        self.app._render_service_button('idle', False)
        self.assertEqual(self.app.service_button.cget('text'), '启动服务')

    def test_copy_http_button_uses_saved_port(self):
        from stellaris_modder_agent.desktop_runtime import Settings

        original = self.app.settings
        try:
            self.app.settings = Settings(port=9123)
            with patch.object(self.app, 'copy') as copy:
                self.app.copy_http_button.invoke()
            config = json.loads(copy.call_args.args[0])
            self.assertEqual(config['mcpServers']['stellaris']['url'], 'http://127.0.0.1:9123/mcp')
        finally:
            self.app.settings = original

    def test_mod_sources_are_saved_and_removable(self):
        from stellaris_modder_agent.desktop_runtime import load_settings

        with tempfile.TemporaryDirectory() as temp:
            roots = [Path(temp) / 'mod-a', Path(temp) / 'mod-b']
            for root in roots:
                root.mkdir()
                self.app.mod.set(str(root))
                self.assertTrue(self.app.add_mod_root())
            self.assertEqual(load_settings(self.app.settings_path)[0].mod_roots, list(map(str, roots)))
            self.app.show_page('data')
            self.app.update_idletasks()
            tile = self.app._source_tiles[0]
            title = next(child for child in tile.winfo_children() if child.winfo_manager() == 'place')
            self.assertLessEqual(title.winfo_y() + title.winfo_height(), tile.winfo_height())
            self.assertEqual(self.app._source_tiles[0].grid_info()['row'],
                             self.app._source_tiles[1].grid_info()['row'])
            self.app._show_source_remove(self.app._source_tiles[0], self.app._source_delete_buttons[0], str(roots[0]))
            self.assertEqual(self.app._source_delete_buttons[0].winfo_manager(), 'place')
            self.assertTrue(self.app.remove_mod_root(str(roots[0])))
            self.assertEqual(load_settings(self.app.settings_path)[0].mod_roots, [str(roots[1])])
            self.assertTrue(self.app.remove_mod_root(str(roots[1])))
            self.app.show_page('overview')

    def test_browse_adds_multiple_mod_sources_together(self):
        from stellaris_modder_agent.desktop_runtime import load_settings

        with tempfile.TemporaryDirectory() as temp:
            roots = [Path(temp) / 'mod-a', Path(temp) / 'mod-b']
            for root in roots:
                root.mkdir()
            with patch('stellaris_modder_agent.desktop.select_folders', return_value=list(map(str, roots))):
                self.app.browse_mod()
            self.assertEqual(load_settings(self.app.settings_path)[0].mod_roots, list(map(str, roots)))
            for root in roots:
                self.app.remove_mod_root(str(root))

    def test_force_exit_choice_closes_update_dialog(self):
        self.app._show_exit_dialog()
        self.assertTrue(self.app._exit_dialog.winfo_exists())
        with patch.object(self.app, '_finish_close') as finish:
            self.app._force_close()
        finish.assert_called_once()
        self.assertFalse(self.app._exit_dialog.winfo_exists())

    def test_close_during_update_offers_exit_choice(self):
        from unittest.mock import PropertyMock
        from stellaris_modder_agent.desktop_runtime import ProcessTask
        self.app.job_action = 'update'
        with patch.object(ProcessTask, 'active', new_callable=PropertyMock, return_value=True), \
                patch.object(self.app, '_show_exit_dialog') as notice, \
                patch.object(self.app.job, 'stop') as stop:
            self.app.close()
        notice.assert_called_once()
        stop.assert_not_called()
        self.assertFalse(self.app.closing)
