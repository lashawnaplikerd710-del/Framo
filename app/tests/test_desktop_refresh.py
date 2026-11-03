"""Refresh must preserve responsiveness without hiding state changes."""
from unittest.mock import patch
from tests.test_desktop_workbench import DesktopInteractionTests, fixture_job


import unittest
class RefreshRegressionTests(unittest.TestCase):
    setUp = DesktopInteractionTests.setUp
    tearDown = DesktopInteractionTests.tearDown

    def test_unchanged_refresh_does_not_rebuild_widgets(self):
        self.ui.render_status(self.snapshot)
        with patch.object(self.ui,'render_job_groups') as groups, \
             patch.object(self.ui.detail,'delete') as detail, \
             patch.object(self.ui.checks_text,'delete') as checks, \
             patch.object(self.ui,'refresh_export_page') as exports:
            self.ui.render_status(self.snapshot)
            groups.assert_not_called(); detail.assert_not_called()
            checks.assert_not_called(); exports.assert_not_called()

    def test_in_place_event_change_updates_detail_without_rebuilding_list(self):
        job=fixture_job(state='FAILED',publication=None)
        self.snapshot['jobs']=[job];self.ui.render_status(self.snapshot)
        self.root.update_idletasks()
        self.ui.render_status(self.snapshot)
        with patch.object(self.ui,'render_job_groups') as groups:
            job['events'].append({'type':'progress','payload':{'message':'新进度已写入'}})
            self.ui.render_status(self.snapshot)
            groups.assert_not_called()
        self.assertIn('新进度已写入',self.ui.detail.get('1.0','end'))
        job['state']='WAITING_FOR_INPUT'
        self.ui.render_status(self.snapshot)
        self.assertIn('需要处理',self.ui.task_state_var.get())

    def test_fast_navigation_keeps_only_latest_destination(self):
        for name in ('tasks','settings','export','create','tasks'):
            self.ui.show(name)
        self.root.update()
        self.assertEqual(self.ui.current_view,'tasks')
        self.assertTrue(self.ui.nav_buttons['tasks'].selected)
        self.assertFalse(self.ui.nav_buttons['create'].selected)

    def test_completed_callback_queue_yields_to_input(self):
        from concurrent.futures import Future
        from unittest.mock import Mock
        callback=Mock()
        for _ in range(2):
            done=Future(); done.set_result('ready')
            self.ui.inbox.put(('preview',done,callback))
        with patch('agent_core.desktop_workbench.time.perf_counter',side_effect=[0,.009]), \
             patch.object(self.ui,'later') as later:
            self.ui._drain()
        self.assertEqual(callback.call_count,1)
        self.assertEqual(self.ui.inbox.qsize(),1)
        self.assertEqual(later.call_args.args[0],1)
