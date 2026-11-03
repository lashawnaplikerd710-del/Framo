import copy
import json
import unittest
from pathlib import Path
import test_task_service
from agent_core.evidence_reuse import rerun_with_evidence
from agent_core.task_service import task_spec
from agent_core.task_store import atomic_json,digest


class EvidenceReuseTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_task_service.TaskServiceTests();self.fixture.setUp()
        self.service=self.fixture.service
        path=self.fixture.root/'source.mp4';path.write_bytes(b'original')
        a=self.service.register_asset(path)
        second=copy.deepcopy(self.service.store.get_asset(a));second['asset_id']='asset_'+'b'*24
        b=self.service.store.put_asset(second)
        self.old=self.service.create_job(task_spec('完整带货视频',[a,b],'product_ad'))
        self.old_dir=self.service.store.job_dir(self.old)
        self.frame=self.old_dir/'sources'/a/'frame.png';self.frame.parent.mkdir(parents=True)
        self.frame.write_bytes(b'unchanged native pixels')
        evidence=self.frame.parent/'frame-evidence.json'
        atomic_json(evidence,{'schema_version':'1.0','asset_id':a,'frames':[{'path':str(self.frame),'sha256':digest(self.frame)}]})
        self.service.store.put_artifact(self.old,'frame_evidence_'+a,'revision_001',evidence)
        self.service.store.set_state(self.old,'FAILED')

    def tearDown(self):self.fixture.tearDown()

    def test_rerun_rebases_verified_evidence_without_mutating_old_job(self):
        before=self.service.get_job(self.old)
        new=rerun_with_evidence(self.service,self.old)
        self.assertNotEqual(new,self.old)
        self.assertEqual(self.service.get_job(self.old),before)
        artifact=self.service.list_artifacts(new)[0]
        frame=json.loads(Path(artifact['path']).read_text(encoding='utf-8'))['frames'][0]
        self.assertTrue(Path(frame['path']).is_relative_to(self.service.store.job_dir(new)))
        self.assertEqual(digest(Path(frame['path'])),digest(self.frame))
        self.assertFalse(json.loads((self.service.store.job_dir(new)/'evidence-reuse.json').read_text(encoding='utf-8'))['edit_decisions_reused'])

    def test_changed_pixels_or_running_job_cannot_reuse(self):
        self.frame.write_bytes(b'different')
        with self.assertRaisesRegex(ValueError,'frame evidence changed'):rerun_with_evidence(self.service,self.old)
        self.service.store.set_state(self.old,'PLANNING')
        with self.assertRaisesRegex(ValueError,'Stop'):rerun_with_evidence(self.service,self.old)
