import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

import test_task_service
from agent_core.resource_policy import ResourcePolicyBlocked
from agent_core.task_service import task_spec
from agent_core.task_store import atomic_json


class NativeReviewTaskServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_task_service.TaskServiceTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service=self.fixture.service;root=self.fixture.root
        self.service.config['native_resources']={'mode':'official_cli_v1','allow_isolated_host':True}
        for name in ('draft_root','user_data_dir'):
            path=root/name;path.mkdir();self.service.config[name]=str(path)
        media=root/'media.mp4';media.write_bytes(b'media')
        aid=self.service.register_asset(media)
        self.job=self.service.create_job(task_spec('本地待复核草稿',[aid]))
        self.folder=self.service.store.job_dir(self.job)
        self.draft=self.folder/'draft';self.draft.mkdir();(self.draft/'draft_content.json').write_bytes(b'draft')
        self.service.store.put_artifact(self.job,'packaged_draft','revision_001',self.draft/'draft_content.json',verified=True)
        atomic_json(self.folder/'delivery.json',{'draft_readback_passed':True,'jianying_native_preview_checked':True,
            'draft_path':str(self.draft),'editable_layers_verified':True,'subtitle_mode':'not_requested'})
        self.service.store.put_artifact(self.job,'delivery','revision_001',self.folder/'delivery.json',verified=True)
        self.native=self.folder/'native/packaging-report.json';atomic_json(self.native,{'fixture':True})
        self.packaging=self.folder/'packaging.json'
        self.adapter=Mock();self.service.executor_factory=Mock()
        self.service.store.set_state(self.job,'READY_FOR_REVIEW',message='fixture')

    def put_packaging(self,schema):
        atomic_json(self.packaging,{'schema':schema,'native_report':str(self.native)})
        self.service.store.put_artifact(self.job,'packaging_report','revision_001',self.packaging,verified=True)

    def test_native_profile_does_not_unlock_legacy_generated_resources(self):
        self.put_packaging('legacy-generated-packaging')
        with self.assertRaises(ResourcePolicyBlocked):self.service.publish_job(self.job)
        self.service.executor_factory.assert_not_called()

    def test_registration_uses_verified_native_adapter_and_keeps_review_pending(self):
        self.put_packaging('jianying-native-task-packaging-v1')
        def register(packaging,root,user_data,name,output):
            self.assertEqual(packaging,self.native.parent)
            result={'status':'native_draft_registered','destination':str(root/name),
                'self_contained_dependencies':True,'registration':{'index_readback_equal':True,'existing_entries_preserved':True}}
            atomic_json(output/'registration-report.json',result)
            return result
        self.adapter.register_review.side_effect=register
        with patch('agent_core.task_service.JianyingAdapter',return_value=self.adapter), \
                patch('agent_core.native_packaging.validate_completed',return_value={
                    'draft':str(self.draft),'source_preserved':True}) as check:
            result=self.service.publish_job(self.job)
        check.assert_called_once_with(self.native.parent)
        self.adapter.publish.assert_not_called()
        self.assertTrue(result['homepage_index_registered'])
        self.assertTrue(result['self_contained_dependencies'])
        self.assertTrue(result['local_review_only'])
        self.assertFalse(result['full_quality_accepted'])
        self.assertFalse(result['final_export_created'])

    def test_new_narration_registration_uses_its_own_verified_draft(self):
        self.put_packaging('jianying-native-task-packaging-v1')
        voice=self.folder/'voice-revision';voice.mkdir()
        delivery=json.loads((self.folder/'delivery.json').read_text())
        delivery['new_narration_revision']=str(voice)
        atomic_json(voice/'delivery.json',delivery)
        self.service.store.put_artifact(self.job,'delivery','revision_001_voice',voice/'delivery.json',verified=True)
        self.service.store.put_artifact(self.job,'packaged_draft','revision_001_voice',self.draft/'draft_content.json',verified=True)
        self.service.store.put_artifact(self.job,'packaging_report','revision_001_voice',self.packaging,verified=True)
        def register(config,output,name):
            result={'status':'native_draft_registered','destination':str(self.fixture.root/'draft_root'/name),
                'self_contained_dependencies':True,'registration':{'index_readback_equal':True,'existing_entries_preserved':True}}
            atomic_json(output/'publication/registration-report.json',result);return result
        with patch('agent_core.native_packaging.validate_completed',return_value={'draft':'older-base','source_preserved':True}), \
                patch('agent_core.native_narration.validate_revision',return_value={'draft':str(self.draft)}), \
                patch('agent_core.native_narration.register_revision',side_effect=register) as publish:
            result=self.service.publish_job(self.job)
        self.assertTrue(result['homepage_index_registered']);publish.assert_called_once()


if __name__=='__main__':unittest.main()
