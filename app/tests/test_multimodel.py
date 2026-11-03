import copy
import base64
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from agent_core.model_router import DualModelProvider, create_provider, validate_vision_config
from agent_core.image_payload import MAX_DATA_URL_CHARS, SAFE_PNG_BYTES, data_url, fit_png_bytes
from agent_core.deepseek import DeepSeekProvider, ModelOutputError
from agent_core.provider import ProviderClient, ProviderError
from agent_core.task_store import digest
from agent_core import qwen_credentials
from agent_core.credential_store import QWEN_TARGET

LANGUAGE = {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
            'api_key_env': 'DEEPSEEK_API_KEY', 'capabilities': {'vision': True, 'tool_calls': True}}
VISION = {'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
          'model': 'qwen3.8-omni-flash', 'api_key_env': 'DASHSCOPE_API_KEY',
          'capabilities': {'vision': True, 'tool_calls': False}, 'stream': True,
          'modalities': ['text'], 'reasoning_effort': 'none'}
COMPLEX = {**VISION, 'model': 'qwen3.8-max', 'reasoning_effort': 'low'}


def response(value):
    return {'message': {'role': 'assistant', 'content': json.dumps(value)},
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
            'finish_reason': 'stop', 'request_id': 'test-request'}


def observation():
    return {'summary': '红色方块',
            'complexity_assessment': {'level': 'routine', 'reasons': [], 'frame_ids': []},
            'frames': [{'frame_id': 'f0', 'description': '红色方块',
            'visible_text': '', 'burned_captions': 'absent', 'quality_issues': [],
            'editorial_notes': '静态展示', 'uncertainty': '静帧无法判断声音'}]}


class RoutingTests(unittest.TestCase):
    def test_source_analysis_preserves_visual_evidence_without_language_roundtrip(self):
        report=self.agent.analyze_montage_source({}, {'asset_id':'asset_a','video_duration_us':2_000_000}, {'cues':[]},self.frames)
        self.language.complete.assert_not_called()
        self.assertEqual(report['frame_times_us'],[1234])
        self.assertEqual(report['visual_evidence'][0]['frames'][0]['sha256'],self.frames[0]['sha256'])
        self.assertIn('uncertainty',report)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name)/'frame.png'
        Image.new('RGB', (48, 48), 'red').save(path)
        self.frames = [{'path': str(path), 'sha256': digest(path), 'requested_us': 1234, 'asset_id': 'asset_a'}]
        self.language = Mock(); self.language.complete.return_value = response({'answer': 'new copy'})
        self.vision = Mock(); self.vision.complete.return_value = response(observation())
        self.review = Mock(); self.review.complete.return_value = response(observation())
        self.env = patch('agent_core.model_router.credential_environment', side_effect=lambda c: nullcontext())
        self.env.start(); self.addCleanup(self.env.stop)
        self.agent = DualModelProvider(LANGUAGE, VISION, complex_config=COMPLEX,
            client=self.language, vision_client=self.vision, complex_client=self.review)

    def test_factory_keeps_legacy_and_routes_multimodel_profile(self):
        self.assertIsInstance(create_provider({'provider': LANGUAGE}), DeepSeekProvider)
        self.assertNotIsInstance(create_provider({'provider': LANGUAGE}), DualModelProvider)
        self.assertIsInstance(create_provider({'provider': LANGUAGE, 'vision_provider': VISION}), DualModelProvider)

    def test_text_only_never_calls_visual_provider(self):
        self.agent._json('写文案', {'instruction': 'short'})
        self.vision.complete.assert_not_called(); self.review.complete.assert_not_called()
        self.assertEqual(self.agent.usage['model_calls'], 1)

    def test_qwen_sees_pixels_and_language_gets_timestamped_facts_only(self):
        self.agent._json('写文案', {'instruction': 'short'}, images=self.frames)
        qwen_messages = self.vision.complete.call_args.args[0]
        self.assertIn('data:image/png;base64,', json.dumps(qwen_messages))
        ds_messages = self.language.complete.call_args.args[0]
        self.assertNotIn('image_url', json.dumps(ds_messages))
        self.assertNotIn('base64', json.dumps(ds_messages))
        supplied = json.loads(ds_messages[1]['content'])['qwen_visual_evidence'][0]
        self.assertEqual(supplied['frames'][0]['requested_us'], 1234)
        self.assertEqual(supplied['frames'][0]['sha256'], self.frames[0]['sha256'])
        self.assertFalse(supplied['continuous_video_reviewed'])
        self.assertEqual(self.agent.usage['model_calls'], 2)
        self.assertEqual(self.agent.usage['total_tokens'], 30)
        self.assertEqual(self.agent.usage['providers']['qwen_omni']['model_calls'], 1)
        self.review.complete.assert_not_called()

    def test_oversized_png_is_compressed_before_qwen_request(self):
        path = Path(self.temp.name)/'large-frame.png'
        pixels = os.urandom(540 * 960 * 3)
        Image.frombytes('RGB', (540, 960), pixels).save(path)
        raw = path.read_bytes()
        self.assertGreater(len(raw), SAFE_PNG_BYTES)
        self.frames[0] = {'path': str(path), 'sha256': digest(path), 'requested_us': 1234}

        self.agent._json('写文案', {'instruction': 'short'}, images=self.frames)
        sent = self.vision.complete.call_args.args[0][1]['content'][-1]['image_url']['url']
        payload = base64.b64decode(sent.removeprefix('data:image/png;base64,'))
        self.assertLessEqual(len(payload), SAFE_PNG_BYTES)
        self.assertLessEqual(len(sent), MAX_DATA_URL_CHARS)
        report_frame = self.agent.usage['visual_reports'][0]['frames'][0]
        self.assertTrue(report_frame['payload_compressed'])
        self.assertEqual(report_frame['sha256'], self.frames[0]['sha256'])

    def test_payload_helper_keeps_small_png_and_bounds_data_url(self):
        raw = Path(self.frames[0]['path']).read_bytes()
        self.assertIs(fit_png_bytes(raw), raw)
        self.assertLessEqual(len(data_url(raw)), MAX_DATA_URL_CHARS)

    def test_output_quality_review_uses_omni_and_preserves_output_time_domain(self):
        self.agent._json('质检', {}, images=self.frames, time_domain='output')
        self.review.complete.assert_not_called()
        self.assertEqual(self.vision.complete.call_count, 1)
        report = self.agent.usage['visual_reports'][0]
        self.assertEqual(report['model'], 'qwen3.8-omni-flash')
        self.assertEqual(report['routing_reason'], 'output_review')
        self.assertEqual(report['frames'][0]['time_domain'], 'output')

    def test_source_duration_at_threshold_routes_directly_to_max(self):
        self.agent._json('素材分析', {'video_duration_us': 120_000_000}, images=self.frames)
        self.vision.complete.assert_not_called()
        self.assertEqual(self.review.complete.call_count, 1)
        self.assertEqual(self.agent.usage['visual_reports'][0]['routing_reason'], 'long_source')
        self.assertNotIn('image_url', json.dumps(self.language.complete.call_args.args[0]))

    def test_source_duration_below_threshold_stays_omni(self):
        self.agent._json('素材分析', {'video_duration_us': 119_999_999}, images=self.frames)
        self.review.complete.assert_not_called()
        self.assertEqual(self.vision.complete.call_count, 1)

    def test_frame_duration_metadata_routes_talking_head_planning_to_max(self):
        self.frames[0]['source_duration_us'] = 600_000_000
        self.agent._json('选择镜头', {}, images=self.frames)
        self.vision.complete.assert_not_called()
        self.assertEqual(self.review.complete.call_count, 1)

    def test_complex_source_is_automatically_escalated_from_omni_to_max(self):
        initial = observation()
        initial['complexity_assessment'] = {'level': 'complex', 'reasons': ['关键部件存在重叠遮挡'], 'frame_ids': ['f0']}
        self.vision.complete.return_value = response(initial)
        self.agent._json('素材分析', {}, images=self.frames)
        self.assertEqual(self.vision.complete.call_count, 1)
        self.assertEqual(self.review.complete.call_count, 1)
        reports = self.agent.usage['visual_reports']
        self.assertEqual(reports[-1]['routing_reason'], 'omni_flagged_complex_source')
        sent = json.loads(self.language.complete.call_args.args[0][1]['content'])
        self.assertEqual(sent['qwen_visual_evidence'][0]['provider'], 'qwen_max')
        self.assertEqual(self.agent.usage['model_calls'], 3)

    def test_long_complex_output_never_escalates_to_max(self):
        observed = observation()
        observed['complexity_assessment'] = {'level':'complex', 'reasons':['复杂画面'], 'frame_ids':['f0']}
        self.vision.complete.return_value = response(observed)
        self.frames[0]['source_duration_us'] = 600_000_000
        report = self.agent.observe_frames(self.frames, context={'video_duration_us':600_000_000},
            time_domain='output', review=True, complex_scene=True)
        self.review.complete.assert_not_called()
        self.assertEqual(report['provider'], 'qwen_omni')

    def test_complexity_flag_without_real_frame_reference_is_rejected(self):
        observed = observation()
        observed['complexity_assessment'] = {'level':'complex', 'reasons':['复杂画面'], 'frame_ids':['unknown']}
        self.vision.complete.return_value = response(observed)
        with self.assertRaises(ModelOutputError): self.agent._json('素材分析', {}, images=self.frames)
        self.review.complete.assert_not_called(); self.language.complete.assert_not_called()

    def test_missing_max_fails_long_source_without_silent_fallback(self):
        agent = DualModelProvider(LANGUAGE, VISION, client=self.language, vision_client=self.vision)
        with self.assertRaisesRegex(ModelOutputError, 'requires complex_vision_provider'):
            agent._json('素材分析', {'video_duration_us':120_000_000}, images=self.frames)
        self.vision.complete.assert_not_called(); self.language.complete.assert_not_called()

    def test_wrong_role_assignment_is_rejected(self):
        for args in ((LANGUAGE, COMPLEX), (LANGUAGE, VISION)):
            with self.assertRaises(ValueError):
                DualModelProvider(*args, complex_config=VISION)

    def test_same_visual_evidence_cached_but_changed_context_rechecked(self):
        for _ in range(2): self.agent._json('copy', {'x':1}, images=self.frames)
        self.assertEqual(self.vision.complete.call_count, 1)
        self.agent._json('copy', {'x':2}, images=self.frames)
        self.assertEqual(self.vision.complete.call_count, 2)

    def test_narration_text_repair_keeps_visual_evidence(self):
        payload = {'frame_assignment': [{'slot': 1, 'frame_time_us': 1234}]}
        self.agent._json('copy', payload, images=self.frames, time_domain='output')
        self.agent._json('shorten', {**payload, 'previous_issue': 'too long'})
        self.assertIn('qwen_visual_evidence', self.language.complete.call_args.args[0][1]['content'])
        self.assertEqual(self.vision.complete.call_count, 1)

    def test_altered_frame_rejected_before_any_network(self):
        Path(self.frames[0]['path']).write_bytes(b'changed')
        with self.assertRaises(ValueError): self.agent._json('copy', {}, images=self.frames)
        self.language.complete.assert_not_called(); self.vision.complete.assert_not_called()

    def test_invented_frame_id_blocks_language_and_no_fallback(self):
        invalid = observation(); invalid['frames'][0]['frame_id'] = 'invented'
        self.vision.complete.return_value = response(invalid)
        with self.assertRaises(ModelOutputError): self.agent._json('copy', {}, images=self.frames)
        self.assertEqual(self.vision.complete.call_count, 2)
        self.language.complete.assert_not_called(); self.review.complete.assert_not_called()

    def test_shared_budget_counts_both_providers_and_blocks_next_request(self):
        self.agent.max_calls = 2
        self.agent._json('copy', {}, images=self.frames)
        with self.assertRaisesRegex(ModelOutputError, 'budget'): self.agent._json('copy', {})
        self.assertEqual(self.language.complete.call_count, 1)

    def test_uncertain_network_failure_is_not_retried_or_rerouted(self):
        self.vision.complete.side_effect = ProviderError('Connection outcome uncertain')
        with self.assertRaises(ProviderError): self.agent._json('copy', {}, images=self.frames)
        self.assertEqual(self.vision.complete.call_count, 1)
        self.language.complete.assert_not_called(); self.review.complete.assert_not_called()
        self.assertEqual(self.agent.usage['model_calls'], 1)

    def test_language_transport_refuses_raw_media(self):
        with self.assertRaises(ModelOutputError):
            self.agent._call([{'role':'user', 'content':[{'type':'image_url','image_url':{'url':'data:...'}}]}])
        self.language.complete.assert_not_called()

    def test_bad_visual_profile_rejected(self):
        for changed in ({'api_key':'secret'}, {'base_url':'https://evil.test/compatible-mode/v1'},
                        {'thinking':'enabled'}, {'model':'unapproved'}, {'capabilities':{'vision':False}}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                validate_vision_config({**VISION, **changed})


class QwenCredentialTests(unittest.TestCase):
    def test_pipe_saves_only_qwen_owned_target(self):
        with patch('agent_core.qwen_credentials.save_key') as save:
            report = qwen_credentials.save_from_pipe(io.StringIO('synthetic-qwen-secret'))
        save.assert_called_once_with('synthetic-qwen-secret', target=QWEN_TARGET)
        self.assertNotIn('synthetic-qwen-secret', str(report))

    def test_key_loaded_only_for_beijing_official_host_then_removed(self):
        with patch.dict(os.environ, {}, clear=True), patch('agent_core.qwen_credentials.load_key', return_value='synthetic-qwen-secret') as load:
            with qwen_credentials.credential_environment(VISION):
                self.assertEqual(os.environ['DASHSCOPE_API_KEY'], 'synthetic-qwen-secret')
            self.assertNotIn('DASHSCOPE_API_KEY', os.environ)
            load.assert_called_once_with(target=QWEN_TARGET)
        for url in ('https://dashscope-intl.aliyuncs.com/compatible-mode/v1',
                    'https://dashscope.aliyuncs.com.evil.test/compatible-mode/v1',
                    'http://dashscope.aliyuncs.com/compatible-mode/v1',
                    'https://dashscope.aliyuncs.com/compatible-mode/v1?key=x'):
            self.assertFalse(qwen_credentials.eligible_provider({**VISION,'base_url':url}))


class StreamingTests(unittest.TestCase):
    def test_sse_content_usage_and_qwen_parameters(self):
        events = [
            {'id':'r1','model':'qwen3.8-omni-flash','choices':[{'index':0,'delta':{'reasoning_content':'private thinking'}}]},
            {'choices':[{'index':0,'delta':{'content':'{"ok"'}}]},
            {'choices':[{'index':0,'delta':{'content':':true}'},'finish_reason':'stop'}]},
            {'choices':[],'usage':{'prompt_tokens':15,'completion_tokens':5,'total_tokens':20}}]
        raw = ('\n\n'.join('data: '+json.dumps(e) for e in events)+'\n\ndata: [DONE]\n\n').encode()
        reply=Mock(status=200); reply.read.return_value=raw
        reply.__enter__=Mock(return_value=reply);reply.__exit__=Mock(return_value=False)
        with patch('agent_core.provider.urllib.request.build_opener') as opener:
            opener.return_value.open.return_value=reply
            result=ProviderClient(VISION,key_value='synthetic-secret').complete([{'role':'user','content':'Return JSON'}],response_format={'type':'json_object'})
            sent=json.loads(opener.return_value.open.call_args.args[0].data)
        self.assertTrue(sent['stream']);self.assertTrue(sent['stream_options']['include_usage'])
        self.assertEqual(sent['reasoning_effort'],'none');self.assertNotIn('thinking',sent)
        self.assertEqual(result['message']['content'],'{"ok":true}')
        self.assertEqual(result['usage']['total_tokens'],20)
        self.assertNotIn('private thinking',json.dumps(result))

    def test_incomplete_stream_rejected(self):
        for raw in ('data: {"choices":[{"delta":{"content":"x"}}]}\n\n',
                    'data: [DONE]\n\n', 'data: {"error":{"message":"secret"}}\n\n'):
            with self.assertRaises(ProviderError):ProviderClient(VISION)._parse_stream(raw)

    def test_video_cannot_bypass_vision_capability(self):
        with self.assertRaisesRegex(ProviderError,'Vision'):
            ProviderClient({**VISION,'capabilities':{'vision':False}}).complete([
                {'role':'user','content':[{'type':'video_url','video_url':{'url':'data:...'}}]}])


if __name__ == '__main__':
    unittest.main()
