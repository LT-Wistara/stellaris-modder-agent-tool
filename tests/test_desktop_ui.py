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
        self.app.events.put(('service', 'done', (0, True, '')))
        self.app.poll()
        self.assertFalse(self.app.running)
        self.assertEqual(self.app.start_button.cget('state'), 'normal')

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
        self.assertEqual(self.app.start_button.cget('state'), 'normal')

    def test_check_result_distinguishes_current_and_new(self):
        self.app.job_action = 'check'
        for remote, expected in [('abc', '最新'), ('def', '发现')]:
            self.app.events.put(('job', 'done', (0, False, json.dumps({'local':'abc', 'upstream':remote}))))
            self.app.poll()
            self.assertIn(expected, self.app.update_message.cget('text'))

    def test_install_cannot_be_interrupted_by_closing_window(self):
        from unittest.mock import PropertyMock
        from stellaris_modder_agent.desktop_runtime import ProcessTask
        self.app.job_action = 'update'
        with patch.object(ProcessTask, 'active', new_callable=PropertyMock, return_value=True), \
                patch('stellaris_modder_agent.desktop.messagebox.showinfo') as notice, \
                patch.object(self.app.job, 'stop') as stop:
            self.app.close()
        notice.assert_called_once()
        stop.assert_not_called()
        self.assertFalse(self.app.closing)
