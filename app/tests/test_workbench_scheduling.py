"""Isolated persistent-queue tests; no media, credentials or native host used."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_core.task_service import TaskService
from agent_core.task_store import TaskStore, atomic_json, digest
from agent_core.workbench import Workbench


class SerialSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = dict(gateway_url='', install_dir='', draft_root='D:/drafts',
                             user_data_dir='D:/userdata')
        self.store = TaskStore(Path(self.tmp.name) / 'tasks')
        self.service = Mock(store=self.store)
        self.service.get_job.side_effect = self.store.get_job
        self.service.list_artifacts.side_effect = self.store.list_artifacts
        self.service.read_events.side_effect = self.store.read_events
        self.service.list_batches.return_value = []
        self.service.retry_batch_child.side_effect = lambda batch_id, output_id: (
            TaskService.retry_batch_child(self.service, batch_id, output_id))
        self.running = set()
        self.started = []
        self.app = self.new_app()
        launcher = patch('agent_core.background.launch', side_effect=self.launch)
        self.launcher = launcher.start()
        self.addCleanup(launcher.stop)
        editors = patch('agent_core.workbench.editor_pids', return_value=[])
        editors.start()
        self.addCleanup(editors.stop)

    def new_app(self):
        with patch('agent_core.workbench.default_settings', return_value=self.settings):
            app = Workbench(self.tmp.name)
        app.service = self.service
        app.live_worker = lambda job_id: job_id in self.running
        return app

    def job(self, state='CREATED', *, batch_id=None, ordinal=1):
        job_id = self.store.put_job({'instruction': '顺序执行测试', 'workflow': 'custom'})
        if batch_id:
            self.store.add_batch_child(batch_id, f'output_{ordinal:02d}', job_id, ordinal)
        if state != 'CREATED':
            self.store.set_state(job_id, state)
        return job_id

    def batch(self, count):
        batch_id = self.store.put_batch({'outputs': [], 'gap_us': 3_000_000})
        return [self.job(batch_id=batch_id, ordinal=i) for i in range(1, count + 1)]

    def launch(self, _config, _root, job_id):
        self.assertFalse(self.running, 'A second editing job must never overlap')
        self.running.add(job_id)
        self.started.append(job_id)
        self.store.set_state(job_id, 'ANALYZING')

    def finish(self, job_id, state='COMPLETED'):
        self.running.remove(job_id)
        self.store.set_state(job_id, state)

    def test_singles_before_two_fifo_batches_with_fifteen_sequential_children(self):
        first_batch = self.batch(15)
        first_single = self.job()
        second_batch = self.batch(3)
        second_single = self.job()
        expected = [first_single, second_single, *first_batch, *second_batch]
        for job_id in expected:
            before = len(self.started)
            self.app.scheduler_tick()
            self.assertEqual(self.started, expected[:before + 1])
            self.assertEqual(self.running, {job_id})
            self.app.scheduler_tick()
            self.assertEqual(len(self.started), before + 1)
            self.assertEqual(self.app.resource_slots.snapshot()['used']['model'], 1)
            self.finish(job_id)
        self.app.scheduler_tick()
        self.assertEqual(self.app.resource_slots.snapshot()['used']['model'], 0)

    def test_new_single_waits_for_running_batch_child_then_goes_first(self):
        children = self.batch(3)
        self.app.scheduler_tick()
        single = self.job()
        self.app.scheduler_tick()
        self.assertEqual(self.started, children[:1])
        self.finish(children[0])
        self.app.scheduler_tick()
        self.assertEqual(self.started, [children[0], single])
        self.finish(single)
        self.app.scheduler_tick()
        self.assertEqual(self.started[-1], children[1])

    def test_restart_drains_legacy_parallel_workers_without_cancelling_them(self):
        active = [self.job('ANALYZING'), self.job('BUILDING_DRAFT')]
        self.running.update(active)
        queued = self.job()
        for job_id in active:
            self.app.scheduler_tick()
            self.launcher.assert_not_called()
            self.assertFalse(self.store.get_job(job_id)['cancel_requested'])
            self.finish(job_id)
        self.app.scheduler_tick()
        self.assertEqual(self.started, [queued])

    def test_manual_resume_is_durable_and_does_not_jump_ahead_of_singles(self):
        children = self.batch(2)
        self.store.set_state(children[0], 'FAILED', error='original failure')
        checkpoint = self.store.job_dir(children[0]) / 'checkpoint.json'
        atomic_json(checkpoint, {'revision': 1})
        artifact = self.store.put_artifact(children[0], 'snapshot', 'v1', checkpoint, verified=True)
        before = digest(checkpoint)
        single = self.job()
        self.app.busy_action = 'existing operation'
        result = self.app.action({'action': 'resume', 'job_id': children[0]})
        self.assertTrue(result['queued'])
        self.launcher.assert_not_called()
        self.assertEqual(digest(checkpoint), before)
        self.assertEqual(self.store.get_artifact(artifact['artifact_id'])['sha256'], before)
        recovery = [e for e in self.store.read_events(children[0]) if e['stage'] == 'RECOVERY'][-1]
        self.assertEqual(recovery['payload']['previous_error'], 'original failure')
        self.app = self.new_app()
        self.app.scheduler_tick()
        self.assertEqual(self.started, [single])
        self.finish(single)
        self.app.scheduler_tick()
        self.assertEqual(self.started[-1], children[0])

    def test_live_or_ready_jobs_cannot_be_requeued_and_duplicate_queue_is_idempotent(self):
        active = self.job('ANALYZING')
        self.running.add(active)
        with self.assertRaises(ValueError):
            self.app.action({'action': 'resume', 'job_id': active})
        ready = self.job('READY_FOR_REVIEW')
        with self.assertRaises(ValueError):
            self.app.action({'action': 'resume', 'job_id': ready})
        failed = self.job('FAILED')
        self.app.action({'action': 'resume', 'job_id': failed})
        events = self.store.read_events(failed)
        self.app.action({'action': 'resume', 'job_id': failed})
        self.assertEqual(self.store.read_events(failed), events)
        self.launcher.assert_not_called()

    def test_failed_waiting_and_cancelled_items_do_not_block_remaining_batch(self):
        for state in ('FAILED', 'WAITING_FOR_INPUT', 'BLOCKED_RESOURCES', 'CANCELLED'):
            self.job(state)
        cancelled = self.job()
        self.store.request_cancel(cancelled)
        children = self.batch(3)
        self.store.set_state(children[0], 'FAILED')
        self.app.scheduler_tick()
        self.assertEqual(self.store.get_job(cancelled)['state'], 'CANCELLED')
        self.assertEqual(self.started, [children[1]])
        result = self.app.action({'action': 'retry_batch_child',
            'batch_id': self.store.batch_for_job(children[0])['batch_id'], 'output_id': 'output_01'})
        self.assertEqual(result['state'], 'CREATED')
        self.app.scheduler_tick()
        self.assertEqual(self.started, [children[1]])
        self.finish(children[1])
        self.app.scheduler_tick()
        self.assertEqual(self.started[-1], children[0])

    def test_worker_launch_failure_releases_slot_and_allows_next_item(self):
        bad = self.job()
        good = self.job()
        def launch(config, root, job_id):
            if job_id == bad:
                raise RuntimeError('launch failed')
            return self.launch(config, root, job_id)
        self.launcher.side_effect = launch
        self.app.scheduler_tick()
        self.assertEqual(self.store.get_job(bad)['state'], 'WAITING_FOR_INPUT')
        self.assertEqual(self.started, [good])

    def test_automatic_recovery_shares_priority_with_new_jobs(self):
        batch_child = self.batch(1)[0]
        self.store.set_state(batch_child, 'ANALYZING')
        single = self.job()
        with patch.object(self.app, '_recoverable_jobs', return_value=[{'job_id': batch_child}]):
            self.app.scheduler_tick()
            self.assertEqual(self.started, [single])
            self.finish(single)
            self.app.scheduler_tick()
        self.assertEqual(self.started, [single, batch_child])
        self.assertTrue(any(e['stage'] == 'RECOVERY' for e in self.store.read_events(batch_child)))

    def test_scheduler_checks_jobs_older_than_display_limit(self):
        oldest = self.job()
        for _ in range(100):
            self.job('COMPLETED')
        self.batch(2)
        self.assertNotIn(oldest, [j['job_id'] for j in self.app.jobs()])
        self.app.scheduler_tick()
        self.assertEqual(self.started, [oldest])
        self.app.scheduler_tick()
        self.assertEqual(self.started, [oldest])

    def test_batch_ordinal_order_overrides_child_creation_order(self):
        batch_id = self.store.put_batch({'outputs': []})
        last = self.job(batch_id=batch_id, ordinal=2)
        first = self.job(batch_id=batch_id, ordinal=1)
        self.app.scheduler_tick()
        self.assertEqual(self.started, [first])
        self.finish(first)
        self.app.scheduler_tick()
        self.assertEqual(self.started, [first, last])


if __name__ == '__main__':
    unittest.main()
