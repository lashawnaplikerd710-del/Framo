import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_core.deepseek import DeepSeekProvider, VisualEvidenceError
from agent_core.direct_vision import analyze_frames, cached_analyze_frames, VisualEvidencePending, selected_observations
from agent_core.provider import ProviderError
from agent_core.commerce import planning_evidence, rewrite_direct_voiceover
from agent_core.task_service import TaskService, task_spec
from tests.test_task_service import FakeInspector


def observation(time=0):
    return {'requested_us':time,'subject':'柑橘','description':'手持切开的柑橘',
            'visible_features':['橙色果肉'],'action':'展示果肉','visible_text':[],
            'confidence':'high','uncertainty':'不判断甜度'}


def answer(frames):
    return {'schema_version':'1.0','frames':[observation(f['requested_us']) for f in frames],
            '_input_evidence':{'images':'submitted'}}


class DirectVisionTests(unittest.TestCase):
    def test_incomplete_batch_is_reobserved_as_real_single_frames(self):
        frames=[{'requested_us':i*1000,'sha256':'hash'} for i in range(4)]
        provider=Mock();provider.config={'model':'deepseek-flash'}
        provider._json.side_effect=[answer(frames[:2])]+[answer([f]) for f in frames]
        result=analyze_frames(provider,'asset_a',frames)
        self.assertEqual([r['requested_us'] for r in result['frames']],[f['requested_us'] for f in frames])
        self.assertEqual(provider._json.call_count,5)
        for call in provider._json.call_args_list:
            self.assertTrue(call.kwargs['require_images'])
            self.assertTrue(call.kwargs['images'])

    def test_required_images_fail_once_without_text_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            p=Path(root)/'f.png';p.write_bytes(b'\x89PNG\r\n\x1a\nfixture')
            provider=DeepSeekProvider({'base_url':'https://api.deepseek.com','model':'deepseek-flash',
                'api_key_env':'DEEPSEEK_API_KEY'}, client=Mock())
            with patch.object(provider,'_call',side_effect=ProviderError('unsupported_content',category='invalid_request')) as call:
                with self.assertRaises(VisualEvidenceError):
                    provider._json('observe',{},images=[{'path':str(p),'requested_us':0,
                        'sha256':hashlib.sha256(p.read_bytes()).hexdigest()}],require_images=True)
                self.assertEqual(call.call_count,1)

    def test_timestamp_and_transport_provenance_are_required(self):
        frames=[{'requested_us':0,'sha256':'local-hash'}]
        provider=Mock();provider.config={'model':'deepseek-flash'}
        provider._json.return_value=answer(frames)
        result=analyze_frames(provider,'asset_a',frames)
        self.assertEqual(result['frames'][0]['frame_sha256'],'local-hash')
        self.assertFalse(result['continuous_video_reviewed'])
        for mutate in ('timestamp','transport','schema'):
            bad=answer(frames)
            if mutate=='timestamp':bad['frames'][0]['requested_us']=1
            elif mutate=='transport':bad['_input_evidence']={'images':'rejected_by_provider'}
            else:bad['frames'][0]['visible_features']='not a list'
            provider._json.return_value=bad
            with self.assertRaises(VisualEvidencePending):analyze_frames(provider,'asset_a',frames)

    def test_visual_evidence_reaches_planner_and_exact_shot_rewrite(self):
        source={'asset_id':'a','speech_units':[{'unit_id':'v001','start_us':0,'end_us':3_000_000,'text':'','visual_only':True}],
                'observations':[{'frames':[observation(0),observation(4_000_000)]}],
                'frames':[{'requested_us':0}], 'transcript':{'cues':[]},'speech_mode':'visual'}
        self.assertEqual(planning_evidence([source])[0]['observations'],source['observations'])
        provider=Mock();provider._json.return_value={'schema_version':'1.0','clips':[
            {'clip_index':1,'heading':'果肉展示','voiceover':'看看切开的柑橘果肉。'}]}
        plan={'clips':[{'role':'hook','heading':'柑橘','reason':'果肉展示','voiceover':'旧文案'}]}
        timeline={'source_map':[{'asset_id':'a','source_start_us':0,'source_end_us':3_000_000,
                                'timeline_start_us':0,'timeline_end_us':3_000_000}]}
        _,report=rewrite_direct_voiceover(provider,{'instruction':'广告'},[source],plan,timeline,'修订')
        self.assertEqual(report['frozen_shots'][0]['selected_visual_observations'],[observation(0)])
        self.assertEqual(selected_observations(source,3_000_000,5_000_000),[observation(4_000_000)])

    def test_partial_analysis_is_cached_and_resumes_only_missing_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);scripts=root/'scripts';scripts.mkdir()
            for name in ('native_timeline.py','native_subtitles.py','native_preview.py','native_register.py',
                         'local_asr.py','autocut_task.py','draft_quality.py','native_packaging.py','packaging_assets.py','native_audio_scan.py'):
                (scripts/name).write_text('# fixture')
            service=TaskService({'provider':{'model':'deepseek-flash'},'toolchain_root':str(root),'install_dir':str(root)},root/'data',inspector=FakeInspector())
            media=root/'media.mp4';media.write_bytes(b'original')
            aid=service.register_asset(media)
            spec=task_spec('剪辑15秒',[aid],'custom',render_backend='direct_ffmpeg')
            job=service.create_job(spec);asset=service.store.get_asset(aid)
            frames=[]
            for i in range(8):
                path=root/f'frame-{i}.png';path.write_bytes(bytes([i]))
                frames.append({'requested_us':i*1_000_000,'path':str(path),
                               'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
            provider=Mock();provider.config={'model':'deepseek-flash'}
            provider._json.side_effect=[answer(frames[:4]),ProviderError('timeout',category='transport')]
            with patch.object(service,'_transcribe',return_value={'cues':[]}),patch.object(service,'_sample',return_value={'frames':frames}):
                with self.assertRaises(VisualEvidencePending):service._prepare_direct_ad_sources(job,spec,[asset],None,provider)
                first=service.store.artifact(job,f'visual_{aid}_01')
                before=Path(first['path']).read_bytes()
                provider._json.side_effect=[answer(frames[4:])]
                sources=service._prepare_direct_ad_sources(job,spec,[asset],None,provider)
                self.assertEqual(provider._json.call_count,3)
                self.assertEqual(len(sources[0]['observations']),2)
                self.assertEqual(Path(first['path']).read_bytes(),before)
                Path(first['path']).write_text('{}')
                with self.assertRaisesRegex(ValueError,'changed'):
                    service._prepare_direct_ad_sources(job,spec,[asset],None,provider)
            self.assertEqual(media.read_bytes(),b'original')

    def test_shared_visual_cache_reuses_pixels_but_detects_changed_identity_or_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);frame=root/'frame.png';frame.write_bytes(b'pixels')
            frames=[{'path':str(frame),'requested_us':0,'sha256':hashlib.sha256(b'pixels').hexdigest()}]
            asset={'asset_id':'asset_a','sha256':'source-hash'}
            provider=Mock();provider.config={'model':'deepseek-flash','base_url':'https://gateway.example/v1'}
            provider._json.return_value=answer(frames)
            first=cached_analyze_frames(root,provider,asset,frames)
            copy_frame=root/'second-job-frame.png';copy_frame.write_bytes(b'pixels')
            second=cached_analyze_frames(root,provider,asset,[{**frames[0],'path':str(copy_frame)}])
            self.assertEqual(first,second);self.assertEqual(provider._json.call_count,1)
            provider.config['base_url']='https://other.example/v1'
            cached_analyze_frames(root,provider,asset,frames)
            self.assertEqual(provider._json.call_count,2)
            provider.config['base_url']='https://gateway.example/v1'
            for p in (root/'cache/analysis/visual').glob('*.json'):
                record=json.loads(p.read_text('utf-8'));record['report']['frames'][0]['subject']='forged'
                p.write_text(json.dumps(record),encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'cache content changed'):
                cached_analyze_frames(root,provider,asset,frames)
            frame.write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError,'Frame evidence changed'):
                cached_analyze_frames(root,provider,asset,frames)

    def test_quota_rejection_is_not_a_visual_recognition_error_and_is_not_retried(self):
        provider=Mock();provider._json.side_effect=ProviderError('daily_allowance_exhausted',
            status_code=402,category='balance',details={'code':'daily_allowance_exhausted'})
        with self.assertRaises(ProviderError) as raised:
            analyze_frames(provider,'asset_a',[{'requested_us':0,'sha256':'hash'}])
        self.assertEqual(raised.exception.category,'balance')
        self.assertEqual(provider._json.call_count,1)

    def test_visual_service_failure_waits_without_planning_or_losing_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);scripts=root/'scripts';scripts.mkdir()
            for name in ('native_timeline.py','native_subtitles.py','native_preview.py','native_register.py',
                         'local_asr.py','autocut_task.py','draft_quality.py','native_packaging.py','packaging_assets.py','native_audio_scan.py'):
                (scripts/name).write_text('# fixture')
            provider=Mock();provider.config={'model':'deepseek-flash'};provider.usage={'model_calls':0};provider._clients={}
            service=TaskService({'provider':{'model':'deepseek-flash'},'toolchain_root':str(root),'install_dir':str(root)},
                                root/'data',inspector=FakeInspector(),provider_factory=lambda:provider)
            media=root/'media.mp4';media.write_bytes(b'original')
            aid=service.register_asset(media)
            job=service.create_job(task_spec('剪辑15秒',[aid],'custom',render_backend='direct_ffmpeg'))
            evidence=service.store.job_dir(job)/'saved.json';evidence.write_text('{"kept":true}')
            service.store.put_artifact(job,'prior_evidence','revision_001',evidence,verified=True)
            errors = [VisualEvidencePending('图片接口不可用'),
                      ProviderError('daily_allowance_exhausted', category='balance', status_code=402,
                                    details={'code':'daily_allowance_exhausted'})]
            for index, error in enumerate(errors):
                with patch('agent_core.model_recovery.model_preflight',return_value={'status':'passed'}), \
                     patch.object(service,'_prepare_direct_ad_sources',side_effect=error), \
                     patch.object(service,'_plan_direct_ad') as plan:
                    result=service.start_job(job) if index == 0 else service.resume_job(job)
                self.assertEqual(result['state'],'WAITING_FOR_INPUT',result.get('error'))
                plan.assert_not_called()
                self.assertEqual(evidence.read_text(),'{"kept":true}')
                self.assertEqual(len(list((service.store.job_dir(job)/'visual-pending').glob('*.json'))),1)
                if index:
                    self.assertIn('每日额度',result['error'])
                    self.assertNotIn('待补画面',result['error'])


if __name__=='__main__':unittest.main()
