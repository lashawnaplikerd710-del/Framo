import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from agent_core.task_store import TaskStore, atomic_json
from agent_core.workbench import Workbench


class AutoRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = {'gateway_url':'','install_dir':'','draft_root':'D:/drafts','user_data_dir':'D:/userdata'}
        with patch('agent_core.workbench.default_settings', return_value=self.settings):
            self.app = Workbench(self.tmp.name)
        self.store = TaskStore(Path(self.tmp.name)/'tasks')
        self.service = Mock(store=self.store)
        self.service.get_job.side_effect = self.store.get_job
        self.service.list_artifacts.side_effect = self.store.list_artifacts
        self.service.list_artifacts_safe.side_effect = self.store.list_artifacts_safe
        self.service.read_events.side_effect = self.store.read_events
        self.service.publish_job.side_effect = self.published
        self.app.service = self.service
        self.closed = patch('agent_core.workbench.editor_pids', return_value=[])
        self.editors = self.closed.start()
        self.addCleanup(self.closed.stop)

    def tearDown(self):
        self.finish()
        self.tmp.cleanup()

    def finish(self):
        if self.app.registration_thread:
            self.app.registration_thread.join(4)
            self.assertFalse(self.app.registration_thread.is_alive())

    def job(self, state='READY_FOR_REVIEW'):
        job_id = self.store.put_job({'instruction':'自动登记测试','workflow':'custom'})
        self.store.set_state(job_id, state, message='fixture')
        return job_id

    def published(self, job_id):
        summary = {'registered_draft_path':'D:/drafts/'+job_id,'homepage_index_registered':True,
                   'full_quality_accepted':False,'final_export_created':False}
        file = self.store.job_dir(job_id)/'publication/summary.json'
        atomic_json(file, summary)
        self.store.put_artifact(job_id, 'publication_summary', 'revision_001', file, verified=True)
        return summary

    def test_ready_automatically_publishes_once_without_changing_editing_state(self):
        job = self.job()
        self.app.scheduler_tick(); self.finish()
        self.service.publish_job.assert_called_once_with(job)
        self.app.scheduler_tick(); self.finish()
        self.service.publish_job.assert_called_once()
        current = self.app.job(job)
        self.assertEqual(current['state'], 'READY_FOR_REVIEW')
        self.assertFalse(current['publication']['full_quality_accepted'])
        self.assertFalse(current['publication']['final_export_created'])

    def test_editor_wait_is_durable_deduplicated_and_resumes_after_restart(self):
        job = self.job(); self.editors.return_value = [100]
        self.app.scheduler_tick()
        count = len(self.store.read_events(job))
        self.app.scheduler_tick()
        self.assertEqual(len(self.store.read_events(job)), count)
        self.assertEqual(self.app.job(job)['registration']['status'], 'waiting_editor')
        self.service.publish_job.assert_not_called()
        with patch('agent_core.workbench.default_settings', return_value=self.settings):
            self.app = Workbench(self.tmp.name)
        self.app.service = self.service
        self.editors.return_value = []
        self.app.scheduler_tick(); self.finish()
        self.service.publish_job.assert_called_once_with(job)

    def test_failed_unchecked_cancelled_and_already_published_jobs_are_skipped(self):
        for state in ('FAILED','BUILDING_DRAFT','CHECKING','CANCELLED','COMPLETED'):
            self.job(state)
        registered = self.job(); self.published(registered)
        self.app.scheduler_tick(); self.finish()
        self.service.publish_job.assert_not_called()
        self.editors.assert_not_called()

    def test_waits_for_other_workers_and_drains_before_launching_more(self):
        ready = self.job(); running = self.job('BUILDING_DRAFT'); self.job('CREATED')
        with patch.object(self.app, 'live_worker', side_effect=lambda j: j==running), \
                patch('agent_core.background.launch') as launch:
            self.app.scheduler_tick()
            self.assertEqual(self.app.job(ready)['registration']['status'], 'waiting_workers')
            launch.assert_not_called(); self.service.publish_job.assert_not_called()
        self.app.scheduler_tick(); self.finish()
        self.service.publish_job.assert_called_once_with(ready)

    def test_registration_failure_is_visible_persistent_and_requires_manual_retry(self):
        job = self.job()
        self.service.publish_job.side_effect = ValueError('draft fingerprint changed')
        self.app.scheduler_tick(); self.finish()
        self.assertEqual(self.app.job(job)['registration']['status'], 'failed')
        self.assertIn(self.app.safe_error('draft fingerprint changed'),
                      self.app.job(job)['registration']['message'])
        self.assertEqual(self.store.get_job(job)['state'], 'READY_FOR_REVIEW')
        self.app.scheduler_tick(); self.service.publish_job.assert_called_once()
        self.service.publish_job.side_effect = self.published
        self.app.action({'job_id':job,'action':'publish'}); self.finish()
        self.assertEqual(self.service.publish_job.call_count, 2)
        self.assertIsNotNone(self.app.job(job)['publication'])

    def test_manual_request_while_editor_open_queues_without_failing_task(self):
        job = self.job(); self.editors.return_value = [100]
        result = self.app.action({'job_id':job,'action':'publish'})
        self.assertTrue(result['waiting_for_editor'])
        self.service.publish_job.assert_not_called()
        self.editors.return_value = []
        self.app.scheduler_tick(); self.finish()
        self.service.publish_job.assert_called_once_with(job)

    def test_start_race_with_editor_retries_after_exit(self):
        job = self.job()
        self.service.publish_job.side_effect = RuntimeError('Save and exit Jianying before publishing a registered draft')
        self.app.scheduler_tick(); self.finish()
        self.assertEqual(self.app.job(job)['registration']['status'], 'waiting_editor')
        self.service.publish_job.side_effect = self.published
        self.app.scheduler_tick(); self.finish()
        self.assertIsNotNone(self.app.job(job)['publication'])

    def test_process_inspection_failure_does_not_write_homepage(self):
        job = self.job(); self.editors.side_effect = RuntimeError('inspection unavailable')
        self.app.scheduler_tick()
        self.service.publish_job.assert_not_called()
        self.assertEqual(self.app.job(job)['registration']['status'], 'failed')

    def test_automatic_and_manual_registration_cannot_overlap(self):
        first = self.job(); second = self.job()
        entered = threading.Event(); release = threading.Event()
        def held(job_id):
            entered.set()
            if not release.wait(4): raise RuntimeError('test timed out')
            return self.published(job_id)
        self.service.publish_job.side_effect = held
        try:
            self.app.scheduler_tick(); self.assertTrue(entered.wait(2))
            self.app.scheduler_tick()
            with self.assertRaisesRegex(ValueError, '正在执行'):
                self.app.action({'job_id':second,'action':'publish'})
            self.service.publish_job.assert_called_once_with(first)
        finally:
            release.set(); self.finish()
        self.app.scheduler_tick(); self.finish()
        self.assertEqual(self.service.publish_job.call_count, 2)

    def test_interrupted_registering_checkpoint_is_reconciled_without_new_job(self):
        job = self.job()
        atomic_json(self.store.job_dir(job)/'auto-registration.json',
                    {'status':'registering','message':'interrupted'})
        self.app.scheduler_tick(); self.finish()
        self.service.publish_job.assert_called_once_with(job)
        self.assertEqual(len(self.app.jobs()), 1)


if __name__=='__main__': unittest.main()
