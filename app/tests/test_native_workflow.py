import copy
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

import test_task_service
import test_native_resource_planner
from agent_core.native_workflow import preflight, package_draft, earlier_failed_downloads
from agent_core.task_service import task_spec
from agent_core.task_store import atomic_json, digest
from agent_core.native_packaging import PackagingFailure
from agent_core.local_executor import ToolFailed


class NativeWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.fixture=test_task_service.TaskServiceTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.service=self.fixture.service
        self.service.config['native_resources']={'mode':'official_cli_v1','allow_isolated_host':True}
        source=self.fixture.root/'source.mp4';source.write_bytes(b'media')
        asset=self.service.register_asset(source)
        self.task=task_spec('仅本地验收',[asset]);self.task['resource_use']='commercial'
        self.job=self.service.create_job(self.task);self.folder=self.service.store.job_dir(self.job)
        data=test_native_resource_planner.ResourcePlannerTests();data.setUp()
        self.catalog=data.catalog;self.snapshot=data.snapshot;self.selection=data.result

    def test_configured_preflight_uses_real_candidate_stage_and_cache_is_hash_checked(self):
        with patch('agent_core.native_workflow.collect_candidates',return_value=self.catalog) as collect:
            first=preflight(self.service,self.job,self.task)
            self.assertEqual(preflight(self.service,self.job,self.task),first)
            self.assertEqual(collect.call_count,1)
            self.assertEqual(collect.call_args.kwargs['purpose'],'commercial')
            (self.folder/'official-catalog.json').write_text('{}')
            with self.assertRaisesRegex(ValueError,'changed'):preflight(self.service,self.job,self.task)

    def test_typed_stage_reuses_model_plan_and_rejects_changed_draft(self):
        self.exercise_stage()

    def test_failed_download_automatically_replans_and_resume_skips_rejected_choice(self):
        self.exercise_stage(recover=True)

    def test_later_revision_excludes_verified_failures_without_retroactive_cache_changes(self):
        receipt=self.folder/'n001/resources/music-123/provenance.json'
        atomic_json(receipt,{'status':'failed','reason':'native_resource_command_failed:-100202'})
        row={'kind':'music','resource_id':'123','reason':'official_download_failed',
             'receipt':str(receipt),'sha256':digest(receipt)}
        for revision in ('revision_001','revision_003'):
            report=self.folder/revision/'recovery.json';atomic_json(report,{'excluded':[row]})
            self.service.store.put_artifact(self.job,'native_resource_recovery',revision,report,verified=True)
        self.assertEqual(earlier_failed_downloads(self.service,self.job,'revision_001'),[])
        self.assertEqual(earlier_failed_downloads(self.service,self.job,'revision_002'),[row])
        # Several revisions may report the same failed resource; offer it zero times.
        self.assertEqual(earlier_failed_downloads(self.service,self.job,'revision_004'),[row])
        atomic_json(receipt,{'status':'changed'})
        with self.assertRaisesRegex(PackagingFailure,'evidence_changed'):
            earlier_failed_downloads(self.service,self.job,'revision_002')

    def exercise_stage(self,recover=False):
        draft=self.folder/'base';draft.mkdir();(draft/'draft_content.json').write_bytes(b'base')
        self.snapshot['tracks'][0]['id']='main'
        provider=Mock();provider._json.return_value=self.selection
        if recover:
            candidate=copy.deepcopy(self.catalog['candidates'][0]);candidate['resource_id']='456'
            self.catalog['candidates'].append(candidate)
            replacement=copy.deepcopy(self.selection);replacement['plan']['elements'][0]['resource_id']='456'
            provider._json.side_effect=[self.selection,replacement]
        adapter=Mock();adapter.preview.return_value={'frames':[{'requested_us':1_500_000,'sha256':'sample'}]}
        out=self.folder/'revisions/001';(out/'packaging-input-preview').mkdir(parents=True)
        atomic_json(out/'packaging-input-preview/preview-report.json',adapter.preview.return_value)
        def execute(source,request,work,**kwargs):
            chosen=json.loads(Path(request).read_text())
            self.assertTrue(kwargs['allow_isolated_host'])
            if recover and chosen['elements'][0]['resource_id']=='123':
                atomic_json(Path(work)/'packaging-report.json',{'error_type':'ResourceFailure','committed_elements':0,
                    'reason':'native_resource_command_failed:-100202'})
                atomic_json(Path(work)/'resources/music-123/provenance.json',{'resource_type':'music','resource_id':'123',
                    'status':'failed','reason':'native_resource_command_failed:-100202'})
                raise ToolFailed('official download failure fixture')
            self.assertEqual(chosen,(replacement if recover else self.selection)['plan'])
            final=Path(work)/'final/draft';final.mkdir(parents=True,exist_ok=True)
            (final/'draft_content.json').write_bytes(b'packaged')
            snap=copy.deepcopy(self.snapshot)
            snap['tracks'].append({'id':'music','type':'audio','segments':[{'id':'bgm',
                'target_timerange':{'start':0,'duration':6_000_000}}]})
            atomic_json(final.parent/'snapshot.json',snap)
            atomic_json(Path(work)/'journal.json',{'steps':[{'segment_id':'bgm'}]})
            return {'status':'native_packaging_ready_for_review','draft':str(final),'track_count':2}
        adapter.package_internal.side_effect=execute
        svc=Mock();svc.__enter__=Mock(return_value=svc);svc.__exit__=Mock(return_value=False)
        with patch('agent_core.native_workflow.collect_candidates',return_value=self.catalog), \
                patch('agent_core.native_workflow.native_modules',return_value=(Mock(return_value=svc),None,None)), \
                patch('agent_core.native_workflow.read_native',return_value=self.snapshot):
            args=(self.service,self.job,self.task,provider,adapter,draft,out,{'revision_id':'revision_001'}, {})
            first,report=package_draft(*args)
            self.assertEqual(package_draft(*args)[0],first)
            self.assertEqual(provider._json.call_count,2 if recover else 1)
            self.assertEqual(report['resource_replanning_count'],1 if recover else 0)
            if recover:
                offered=provider._json.call_args.args[1]['candidates']
                self.assertEqual([c['resource_id'] for c in offered],['456'])
            adapter.package.assert_not_called()
            self.assertEqual(report['source_audio_mode'],'preserved_embedded')
            self.assertFalse(report.get('delivery_accepted',False))
            (draft/'draft_content.json').write_bytes(b'changed source')
            with self.assertRaisesRegex(PackagingFailure,'evidence_changed'):package_draft(*args)
            self.assertEqual(adapter.package_internal.call_count,3 if recover else 2)


if __name__=='__main__':unittest.main()
