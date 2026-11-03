import threading
import time
import unittest
from unittest.mock import patch
from tests import test_desktop_workbench


class CreateNavigationTests(unittest.TestCase):
    setUp = test_desktop_workbench.DesktopInteractionTests.setUp
    tearDown = test_desktop_workbench.DesktopInteractionTests.tearDown

    def pump_until(self, predicate, timeout=2):
        deadline = time.monotonic()+timeout
        while not predicate() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_slow_history_cannot_block_create_and_old_snapshot_cannot_erase_new_job(self):
        started, release, created = threading.Event(), threading.Event(), threading.Event()
        calls = []
        def status():
            calls.append(1)
            if len(calls) == 1:
                started.set(); release.wait(4)
            return self.snapshot
        self.backend.status.side_effect = status
        def create(payload):
            created.set()
            return {'job_id':'job_new'}
        self.backend.create_job.side_effect = create
        try:
            self.ui.refresh()
            self.assertTrue(started.wait(1))
            self.ui.create_job()
            self.assertTrue(created.wait(1), 'Creation queued behind slow status')
            self.pump_until(lambda:self.ui.current_view == 'tasks')
            self.pump_until(lambda:'job_new' in self.ui.jobs)
            self.assertFalse(release.is_set())
            # The in-flight pre-creation snapshot must be discarded.
            release.set()
            self.pump_until(lambda:len(calls) > 1)
        finally:
            release.set()

    def test_navigation_does_not_repaint_outgoing_video(self):
        with patch.object(self.ui, 'paint_preview') as paint:
            self.ui.show('tasks')
            paint.assert_not_called()

    def test_poll_during_animation_coalesces_without_rebuilding(self):
        self.ui.show('tasks')
        with patch.object(self.ui, 'render_job_groups') as groups, \
             patch.object(self.ui, 'draw_job') as detail:
            self.ui.render_status(self.snapshot, visible_only=True)
            groups.assert_not_called(); detail.assert_not_called()

    def test_hidden_task_and_export_panels_do_not_render_on_poll(self):
        self.ui._page_transition_until = 0
        with patch.object(self.ui, 'render_job_groups') as groups, \
             patch.object(self.ui, 'draw_job') as detail, \
             patch.object(self.ui, 'refresh_export_page') as exports:
            self.ui.render_status(self.snapshot, visible_only=True)
            groups.assert_not_called(); detail.assert_not_called(); exports.assert_not_called()

    def test_unchanged_button_state_does_not_redraw(self):
        button = self.ui.nav_buttons['create']
        with patch.object(button, '_draw') as draw:
            button.state(['!disabled'])
            draw.assert_not_called()
            button.state(['disabled'])
            self.assertEqual(draw.call_count, 1)

    def test_created_page_is_raised_before_asset_cleanup(self):
        with patch.object(self.ui, 'draw_assets') as cleanup:
            self.ui._created({'job_id':'job_new'})
            self.assertEqual(self.ui.current_view, 'tasks')
            cleanup.assert_not_called()
            self.pump_until(lambda:cleanup.called)

    def test_creation_finishing_on_tasks_still_shows_confirmed_job(self):
        self.ui.show('tasks')
        self.pump_until(lambda:time.perf_counter() >= self.ui._page_transition_until)
        self.ui._created({'job_id':'job_new'})
        self.pump_until(lambda:'job_new' in self.ui.jobs)
        self.assertEqual(self.ui.task_tree.selection(), ('job_new',))
