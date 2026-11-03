"""Withdrawn Tk regressions: preview cancellation and one voice-audio owner."""
import copy
import json
from pathlib import Path
import threading
import tkinter as tk
import unittest
from unittest.mock import patch

from agent_core.desktop_studio import RED
from tests import test_desktop_workbench as fixture


class DesktopPreviewLifecycleTests(unittest.TestCase):
    setUp = fixture.DesktopInteractionTests.setUp
    tearDown = fixture.DesktopInteractionTests.tearDown

    def test_replaced_queued_preview_does_not_erase_notice_or_report_an_error(self):
        started, release = threading.Event(), threading.Event()
        def hold_worker():
            started.set()
            release.wait(3)
        blocking = self.ui.preview_executor.submit(hold_worker)
        self.assertTrue(started.wait(1))
        self.ui._output_preview_path = 'queued-frame-only.mp4'
        try:
            with patch.object(self.ui, '_load_output_preview_frame', return_value=None):
                self.ui._request_output_preview_frame()
                old = self.ui._output_preview_future
                self.ui._request_output_preview_frame()
                self.assertTrue(old.cancelled())
                self.ui.notice('正在定位新画面')
                self.ui._drain()
                self.assertEqual(self.ui.notice_var.get(), '正在定位新画面')
                self.assertNotEqual(str(self.ui.notice_label.cget('foreground')), RED)
        finally:
            self.ui._stop_output_preview_stream()
            release.set()
            blocking.result(timeout=2)

    def _voice_root(self):
        root = Path(self.tmp.name) / 'voices'
        root.mkdir()
        (root/'catalog.json').write_text(json.dumps({'schema_version': 1,
            'defaults': {'ja-JP': 'jp'},
            'voices': [{'id': 'jp', 'locale': 'ja-JP', 'label': '日语样例'}]}), encoding='utf-8')
        self.backend.service.config = {'direct_media': {'local_voice_library': str(root)}}

    def test_voice_picker_is_singleton_and_releases_audio_and_grab_on_close(self):
        self._voice_root()
        with patch('winsound.PlaySound') as play:
            self.ui.open_voice_library()
            first = self.ui._voice_library_dialog
            self.ui.open_voice_library()
            self.assertIs(self.ui._voice_library_dialog, first)
            self.assertEqual(sum(isinstance(child, tk.Toplevel)
                for child in self.root.winfo_children()), 1)
            self.assertIs(self.root.grab_current(), first)
            play.assert_not_called()  # Opening alone never auditions.
            self.root.tk.call(first.protocol('WM_DELETE_WINDOW'))
            play.assert_called_once_with(None, 0)
            self.assertIsNone(self.root.grab_current())
            self.assertIsNone(self.ui._voice_library_dialog)
            self.ui.open_voice_library()
            self.assertIsNot(self.ui._voice_library_dialog, first)

    def test_shutdown_stops_voice_audition_and_cancels_tk_timers(self):
        self._voice_root()
        with patch('winsound.PlaySound') as play:
            self.ui.open_voice_library()
            self.ui.shutdown()
            play.assert_called_once_with(None, 0)
            self.assertFalse(self.ui.timers)
            self.assertTrue(self.backend.stop.is_set())

    def test_country_description_follows_saved_narration_mode_without_mutating_job(self):
        for mode, note in (('new_native', '生成新口播'),
                           ('preserve_source', '保留原声'), ('text_only', '不生成新口播')):
            with self.subTest(mode=mode):
                job = fixture.fixture_job(task_spec={'target_country': '日本', 'narration_mode': mode})
                saved = copy.deepcopy(job)
                self.snapshot['jobs'] = [job]
                self.ui.render_status(self.snapshot)
                self.ui.task_tree.selection_set(job['job_id'])
                self.ui.draw_job()
                detail = self.ui.detail.get('1.0', 'end')
                self.assertIn('目标国家：日本（'+note+'）', detail)
                self.assertNotIn('仅支持文案，不生成口播', detail)
                self.assertEqual(job, saved)


if __name__ == '__main__':
    unittest.main()
