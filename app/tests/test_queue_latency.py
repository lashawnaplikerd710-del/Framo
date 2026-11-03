import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from agent_core.task_store import TaskStore, atomic_json
from agent_core.workbench import Workbench
from agent_core.batch_model import ResourceSlots


class QueueLatencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TaskStore(Path(self.tmp.name)/'tasks')
        self.app = Workbench.__new__(Workbench)
        self.app.service = Mock(store=self.store)
        self.app.mutex = threading.RLock()
        self.app.busy_action = None
        self.app.worker_leases = {}
        self.app.resource_slots = ResourceSlots({'model':1})
        self.app.live_worker = Mock(return_value=False)

    def test_queue_does_not_inspect_completed_artifacts(self):
        job = self.store.put_job({'instruction':'test','workflow':'custom'})
        self.store.set_state(job, 'COMPLETED')
        with patch.object(self.store,'list_artifacts_safe',side_effect=AssertionError('historical audit')):
            rows = self.app.jobs(all_jobs=True)
        self.assertEqual(rows[0]['job_id'],job)
        self.assertEqual(rows[0]['state'],'COMPLETED')

    def test_maintenance_does_not_hold_creation_mutex(self):
        entered, release = threading.Event(), threading.Event()
        self.app._auto_finalize_batches = Mock(side_effect=lambda rows:(entered.set(),release.wait(3)))
        worker = threading.Thread(target=self.app.scheduler_tick)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertTrue(self.app.mutex.acquire(timeout=.2))
            self.app.mutex.release()
        finally:
            release.set();worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_artifact_bulk_read_still_rejects_changed_bytes(self):
        job=self.store.put_job({})
        for i in range(12):
            p=self.store.job_dir(job)/f'{i}.json'
            atomic_json(p,{'value':i})
            self.store.put_artifact(job,'test',str(i),p)
        with patch.object(self.store,'_connect',wraps=self.store._connect) as connect:
            self.assertEqual(len(self.store.list_artifacts(job)),12)
            self.assertEqual(connect.call_count,2)
        atomic_json(self.store.job_dir(job)/'0.json',{'value':'changed'})
        with self.assertRaises(ValueError):self.store.list_artifacts(job)
        valid, invalid=self.store.list_artifacts_safe(job)
        self.assertEqual(len(valid),11);self.assertEqual(len(invalid),1)

    def test_batch_display_is_read_only_and_uses_live_child_state(self):
        job=self.store.put_job({})
        batch=self.store.put_batch({'outputs':[], 'gap_us':3_000_000})
        self.store.add_batch_child(batch,'output_01',job,1)
        self.store.set_state(job,'COMPLETED')
        rows=self.app.batches()
        self.assertEqual(rows[0]['counts']['success'],1)
        self.assertEqual(rows[0]['children'][0]['state'],'COMPLETED')
        self.app.service.list_batches.assert_not_called()
        self.app.service.finalize_batch.assert_not_called()
        self.assertFalse((self.store.batch_dir(batch)/'batch-report.json').exists())


if __name__=='__main__':unittest.main()
