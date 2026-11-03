import copy
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_core import ad_narration, narration_timing
from agent_core.commerce import (compile_ad, rewrite_direct_voiceover,
                                 NarrationPlanIssue, ScriptRevisionPending)
from agent_core.task_store import atomic_json
from tests import test_auto_narration


class NarrationConvergenceTests(unittest.TestCase):
    def fixture(self, raw):
        service, job, f, task, plan, timeline = test_auto_narration.AutoNarrationTests().fixture(raw)
        task = ad_narration.configure(task)
        plan['narration_script'] = 'うちの子が入ってくつろぐ姿がかわいいです。'
        timeline, _, _ = compile_ad(task, f.assets, f.sources, plan)
        timeline['ad_content_review'] = {'decision': 'accept', 'issues': []}
        return service, job, f, task, plan, timeline

    def test_legacy_complete_paragraph_wins_over_partially_repaired_clip_copy(self):
        task = ad_narration.configure({'render_backend':'direct_ffmpeg', 'workflow':'custom',
            'narration_mode':'new_native', 'caption_policy_version':'independent_highlights_v1',
            'caption_mode':'highlights'})
        original = {'voiceover':'画面中展示了宠物帐篷窝。',
                    'clips':[{'asset_id':'a', 'voiceover':'局部新句。', 'heading':'原字幕'}]}
        snapshot = copy.deepcopy(original)
        revised = ad_narration.normalize_plan(task, original)
        self.assertEqual(revised['narration_script'], original['voiceover'])
        self.assertEqual(original, snapshot)
        revised['clips'][0]['heading'] = '修改副本'
        self.assertEqual(original['clips'][0]['heading'], '原字幕')
        self.assertIs(ad_narration.normalize_plan({**task, 'ad_narration_policy':None}, original), original)

    def test_legacy_plan_joins_only_actual_voiceover_without_inventing_copy(self):
        task = {'ad_narration_policy':ad_narration.POLICY, 'render_backend':'direct_ffmpeg',
                'narration_mode':'new_native', 'caption_policy_version':'independent_highlights_v1',
                'caption_mode':'highlights'}
        plan = {'clips':[{'voiceover':'完整句一。'}, {'voiceover':'', 'heading':'不能变口播'},
                         {'voiceover':'完整句二。', 'reason':'不能变口播'}]}
        revised = ad_narration.normalize_plan(task, plan)
        self.assertEqual(revised['narration_script'], '完整句一。 完整句二。')
        revised['narration_script'] = '保留已存在的完整旁白。'
        self.assertEqual(ad_narration.normalize_plan(task, revised)['narration_script'], revised['narration_script'])

    def test_whole_paragraph_accepts_explicit_alias_without_replanning_shots(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            provider = Mock()
            provider._json.return_value = {'narrator_script':plan['narration_script'],
                                           'clips':[{'asset_id':'untrusted replacement'}]}
            revised, report = ad_narration.rewrite(provider, task, f.sources, plan, timeline, '修订', NarrationPlanIssue)
            self.assertEqual(provider._json.call_count, 1)
            self.assertEqual(revised['clips'], plan['clips'])
            self.assertEqual(revised['narration_script'], plan['narration_script'])
            self.assertTrue(report['requires_content_review'])

    def test_whole_paragraph_accepts_all_numbered_legacy_rows_in_original_order(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            text = plan['narration_script']
            provider = Mock()
            provider._json.return_value = {'schema_version':'1.0', 'clips':[
                {'clip_index':str(i), 'voiceover':text if i == 1 else ''}
                for i in reversed(range(1, len(plan['clips'])+1))]}
            revised, _ = ad_narration.rewrite(provider, task, f.sources, plan, timeline, '修订', NarrationPlanIssue)
            self.assertEqual(revised['narration_script'], text)
            self.assertEqual(revised['clips'], plan['clips'])
            self.assertEqual(provider._json.call_count, 1)

    def test_continuous_legacy_repair_accepts_complete_paragraph_on_one_cue(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            task.pop('ad_narration_policy')
            provider = Mock()
            provider._json.return_value = {'voiceover':plan['narration_script']}
            revised, report = rewrite_direct_voiceover(provider, task, f.sources, plan, timeline, '修订')
            self.assertEqual(revised['clips'][0]['voiceover'], plan['narration_script'])
            self.assertTrue(all(c['voiceover'] == '' for c in revised['clips'][1:]))
            self.assertEqual(provider._json.call_count, 1)
            self.assertTrue(report['shot_positions_unchanged'])

    def test_continuous_legacy_repair_accepts_complete_positional_text_rows(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            task.pop('ad_narration_policy')
            provider = Mock()
            provider._json.return_value = {'clips':[
                {'voiceover':plan['narration_script'] if i == 0 else ''}
                for i in range(len(plan['clips']))]}
            revised, _ = rewrite_direct_voiceover(provider, task, f.sources, plan, timeline, '修订')
            self.assertEqual(revised['clips'][0]['voiceover'], plan['narration_script'])
            self.assertEqual(provider._json.call_count, 1)
            for old, new in zip(plan['clips'], revised['clips']):
                self.assertEqual({k:v for k,v in old.items() if k != 'voiceover'},
                                 {k:v for k,v in new.items() if k != 'voiceover'})

    def test_ambiguous_clip_numbering_is_not_silently_reordered(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            provider = Mock()
            provider._json.return_value = {'schema_version':'1.0', 'clips':[
                {'clip_index':1, 'voiceover':plan['narration_script']} for _ in plan['clips']]}
            with self.assertRaises(NarrationPlanIssue):
                ad_narration.rewrite(provider, task, f.sources, plan, timeline, '修订', NarrationPlanIssue)
            self.assertEqual(provider._json.call_count, 3)

    def test_valid_caption_fixes_survive_a_later_response_omitting_them(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            task['_narration_repair_max_units'] = 40
            fix = {'clip_index':'1', 'heading':'くつろぐ姿', 'placement':'lower'}
            provider = Mock()
            provider._json.side_effect = [
                {'schema_version':'1.0', 'narration_script':plan['narration_script']*4, 'caption_fixes':[fix]},
                {'schema_version':'1.0', 'narration_script':plan['narration_script']}]
            revised, report = ad_narration.rewrite(provider, task, f.sources, plan, timeline,
                json.dumps({'issues':['第一处字幕没有依据']}), NarrationPlanIssue)
            self.assertEqual(revised['clips'][0]['heading'], fix['heading'])
            self.assertEqual(report['response']['caption_fixes'][0]['clip_index'], 1)
            self.assertEqual(provider._json.call_args.args[1]['retained_caption_fixes'][0]['heading'], fix['heading'])

    def test_bad_caption_fix_retries_without_losing_valid_narration(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            provider = Mock()
            provider._json.side_effect = [
                {'narration_script':plan['narration_script'], 'caption_fixes':[{'clip_index':99, 'heading':'くつろぐ姿'}]},
                {'narration_script':plan['narration_script'], 'caption_fixes':[{'clip_index':1, 'heading':'くつろぐ姿'}]}]
            revised, _ = ad_narration.rewrite(provider, task, f.sources, plan, timeline,
                json.dumps({'issues':['第一处字幕没有依据']}), NarrationPlanIssue)
            self.assertEqual(provider._json.call_count, 2)
            self.assertEqual(revised['clips'][0]['heading'], 'くつろぐ姿')

    def test_minimal_strategy_does_not_require_filling_an_expansion_target(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            task.update(_narration_repair_min_units=30, _narration_repair_max_units=40,
                        _narration_repair_strategy='minimal')
            provider = Mock()
            provider._json.return_value = {'narration_script':'近くで見てみましょう。'}
            _, report = ad_narration.rewrite(provider, task, f.sources, plan, timeline, '换方案', NarrationPlanIssue)
            self.assertEqual(report['strategy'], 'minimal')
            self.assertIsNone(report['min_speech_units'])
            self.assertNotIn('target_speech_units', provider._json.call_args.args[1])
            self.assertIn('最简完整方案', provider._json.call_args.args[0])

    def test_local_factual_candidates_need_real_selected_evidence_and_new_review(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            task.update(workflow='custom', narration_locale='zh-CN')
            self.assertEqual(ad_narration.factual_candidates(task, f.sources, plan, timeline), [])
            slot = timeline['source_map'][0]
            source = next(s for s in f.sources if s['asset_id'] == slot['asset_id'])
            source['observations'] = [{'frames':[{'asset_id':slot['asset_id'],
                'requested_us':slot['source_start_us'], 'frame_sha256':'a'*64,
                'subject':'宠物帐篷窝', 'confidence':'high'}]}]
            candidates = ad_narration.factual_candidates(task, f.sources, plan, timeline)
            self.assertEqual(len(candidates), 3)
            for candidate, report in candidates:
                self.assertTrue(report['requires_content_review'])
                self.assertFalse(report['reviewed'])
                self.assertNotIn('decision', report)
                self.assertNotRegex(candidate['narration_script'], '加厚|保暖|安心|加高围挡')
                for old, new in zip(plan['clips'], candidate['clips']):
                    self.assertEqual({k:v for k,v in old.items() if k != 'heading'},
                                     {k:v for k,v in new.items() if k != 'heading'})
                compiled, _, _ = compile_ad(task, f.assets, f.sources, candidate)
                self.assertEqual(compiled['source_map'], timeline['source_map'])
            task['instruction'] = '旁白一字不改'
            self.assertEqual(ad_narration.factual_candidates(task, f.sources, plan, timeline), [])

    def test_measured_japanese_expansion_replaces_initial_estimate(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            timeline['duration_us'] = 21_400_000
            task.update(_narration_repair_min_units=89, _narration_repair_max_units=99)
            text = 'うちの子が入ってくつろぐ姿がかわいいです。' * 4 + 'ふわふわの中でゆったり。'
            provider = Mock()
            provider._json.return_value = {'schema_version':'1.0', 'narration_script':text}
            revised, report = ad_narration.rewrite(provider, task, f.sources, plan, timeline, '扩写', NarrationPlanIssue)
            payload = provider._json.call_args.args[1]
            self.assertEqual(payload['target_speech_units'], [89, 99])
            self.assertEqual(report['max_speech_units'], 99)
            self.assertEqual(revised['narration_script'], text)

    def test_short_english_uses_complete_short_alternative_without_filling_hard_limit(self):
        from tests.test_ad_audio import AdAudioTests
        f, task, plan = AdAudioTests().fixture()
        task['instruction'] = '制作8秒视频，简短英语旁白，允许结尾留白'
        timeline = {'duration_us':8_000_000, 'source_map':[
            {'asset_id':f.sources[0]['asset_id'], 'source_start_us':0, 'source_end_us':2_000_000,
             'timeline_start_us':0, 'timeline_end_us':8_000_000}]}
        plan['clips'] = plan['clips'][:1]
        provider = Mock()
        provider._json.return_value = {'schema_version':'1.0',
            'narration_script':'This striped fabric pet tent has a tied-back curtain and a handle on top. A small dog rests inside on the soft cushion. Take a closer look.',
            'alternatives':['A little dog rests inside a striped pet tent.']}
        revised, report = ad_narration.rewrite(provider, task, f.sources, plan, timeline, '写完整旁白', NarrationPlanIssue)
        self.assertEqual(provider._json.call_count, 1)
        self.assertEqual(revised['narration_script'], provider._json.return_value['alternatives'][0])
        payload = provider._json.call_args.args[1]
        self.assertEqual(payload['max_speech_units'], 13)
        self.assertNotIn('target_speech_units', payload)
        self.assertTrue(report['length_checks'][0]['selected_alternative'])
        self.assertEqual(revised['clips'], plan['clips'])

    def test_length_retries_tighten_the_model_target_not_the_real_duration_limit(self):
        from tests.test_ad_audio import AdAudioTests
        f, task, plan = AdAudioTests().fixture()
        timeline = {'duration_us':8_000_000, 'source_map':[
            {'asset_id':f.sources[0]['asset_id'], 'source_start_us':0, 'source_end_us':2_000_000,
             'timeline_start_us':0, 'timeline_end_us':8_000_000}]}
        plan['clips'] = plan['clips'][:1]
        calls = []
        def generate(prompt, payload):
            calls.append(copy.deepcopy(payload))
            return {'schema_version':'1.0', 'narration_script':
                    'A little dog rests quietly inside the striped tent beside a cushion and curtain.' if len(calls)==1
                    else 'A little dog rests inside the striped tent.'}
        provider = Mock(); provider._json.side_effect = generate
        revised, _ = ad_narration.rewrite(provider, task, f.sources, plan, timeline, '精简', NarrationPlanIssue)
        self.assertEqual([c['max_speech_units'] for c in calls], [13, 10])
        self.assertEqual(narration_timing.budget(task, timeline, 1)[0], 22)
        self.assertEqual(revised['narration_script'], 'A little dog rests inside the striped tent.')

    def test_short_expansion_is_rejected_and_feedback_keeps_direction(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            task.update(_narration_repair_min_units=30, _narration_repair_max_units=40)
            provider = Mock()
            provider._json.return_value = {'schema_version':'1.0', 'narration_script':plan['narration_script']}
            with self.assertRaisesRegex(NarrationPlanIssue, '扩写未增加'):
                ad_narration.rewrite(provider, task, f.sources, plan, timeline, '扩写', NarrationPlanIssue)
            self.assertEqual(provider._json.call_count, 3)
            self.assertIn('不要再次缩短', provider._json.call_args.args[1]['validation_error'])
            self.assertEqual(provider._json.call_args.args[1]['issue'], '扩写')

    def test_partial_repairs_accumulate_and_resume_never_renders_rejected_script(self):
        with tempfile.TemporaryDirectory() as raw:
            s, job, f, task, plan, timeline = self.fixture(raw)
            measurement = {'kind':'underfilled', 'available_us':15_000_000, 'measured_us':5_000_000}
            candidates = []
            def rewrite(provider, task, sources, current, timeline, issue):
                revised = copy.deepcopy(current)
                revised['clips'][len(candidates) % len(revised['clips'])]['heading'] = 'やわらかな形'
                candidates.append(revised)
                return revised, {}
            reject = {'decision':'revise','issues':['画面依据需修正文案'],'limits':'仅文字'}
            with patch.object(s, '_run_direct_delivery', side_effect=narration_timing.NarrationDurationIssue(measurement)) as delivery, \
                 patch('agent_core.commerce.rewrite_direct_voiceover', side_effect=rewrite), \
                 patch('agent_core.commerce.review_direct_script', return_value=reject):
                with self.assertRaisesRegex(ScriptRevisionPending, '自动扩写'):
                    s._deliver_direct_ad(job, task, f.assets, f.sources, timeline, plan, Mock())
                self.assertEqual(delivery.call_count, 1)
            self.assertEqual(candidates[-1]['clips'][0]['heading'], 'やわらかな形')
            # Restart after exhausted review uses the latest candidate, not the original.
            with patch('agent_core.commerce.review_direct_script') as review:
                restored, _, _, _ = s._plan_direct_ad(job, task, f.assets, f.sources, Mock())
                self.assertEqual(restored, candidates[-1])
                review.assert_not_called()
            with patch.object(s, '_run_direct_delivery', return_value='rendered') as delivery, \
                 patch('agent_core.commerce.rewrite_direct_voiceover', return_value=(candidates[-1], {})) as retry, \
                 patch('agent_core.commerce.review_direct_script', return_value={'decision':'accept','issues':[]}):
                self.assertEqual(s._deliver_direct_ad(job, task, f.assets, f.sources, timeline, plan, Mock()), 'rendered')
                self.assertEqual(retry.call_args.args[3], candidates[-1])
                self.assertEqual(delivery.call_count, 1)
                self.assertTrue(delivery.call_args.args[1]['_narration_pacing_retried'])
            with patch.object(s, '_run_direct_delivery', return_value='rendered') as delivery, \
                 patch('agent_core.commerce.rewrite_direct_voiceover') as retry:
                s._deliver_direct_ad(job, task, f.assets, f.sources, timeline, plan, Mock())
                retry.assert_not_called()
                self.assertTrue(delivery.call_args.args[1]['_narration_pacing_retried'])

    def test_checkpoint_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            s, job, f, task, plan, timeline = self.fixture(raw)
            path = s.store.job_dir(job)/'auto-narration/pending.json'
            atomic_json(path, {'plan':plan})
            s.store.put_artifact(job, 'ad_auto_pending', 'pending', path)
            atomic_json(path, {'plan':{}})
            with self.assertRaises(ValueError):
                s._narration_checkpoint(job)

    def test_length_retry_preserves_content_feedback_and_caption_fixes(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            task.update(_narration_repair_max_units=40)
            issue = json.dumps({'issues':['第一条字幕属性没有依据']}, ensure_ascii=False)
            fix = {'clip_index':1, 'heading':'くつろぐ姿', 'placement':'lower'}
            provider = Mock()
            provider._json.side_effect = [
                {'schema_version':'1.0','narration_script':plan['narration_script']*4,'caption_fixes':[fix]},
                {'schema_version':'1.0','narration_script':plan['narration_script'],'caption_fixes':[fix]}]
            revised, _ = ad_narration.rewrite(provider, task, f.sources, plan, timeline, issue, NarrationPlanIssue)
            payload = provider._json.call_args.args[1]
            self.assertEqual(payload['issue'], issue)
            self.assertEqual(payload['invalid_response']['caption_fixes'], [fix])
            self.assertEqual(revised['clips'][0]['heading'], fix['heading'])

    def test_factual_repair_below_pacing_target_does_not_force_padding(self):
        with tempfile.TemporaryDirectory() as raw:
            _, _, f, task, plan, timeline = self.fixture(raw)
            task.update(_narration_repair_min_units=30, _narration_repair_max_units=40)
            provider = Mock()
            provider._json.return_value = {'schema_version':'1.0','narration_script':plan['narration_script']}
            _, report = ad_narration.rewrite(provider, task, f.sources, plan, timeline,
                json.dumps({'issues':['不编造轻便性承诺']}), NarrationPlanIssue)
            self.assertFalse(report['target_minimum_met'])
            self.assertTrue(report['minimum_is_pacing_target'])

    def test_review_timeout_preserves_candidate_without_accepting_it(self):
        with tempfile.TemporaryDirectory() as raw:
            s, job, f, task, plan, timeline = self.fixture(raw)
            revised = copy.deepcopy(plan)
            revised['clips'][0]['heading'] = 'くつろぐ姿'
            measured = {'kind':'underfilled','available_us':15_000_000,'measured_us':5_000_000}
            with patch.object(s, '_run_direct_delivery', side_effect=narration_timing.NarrationDurationIssue(measured)), \
                 patch('agent_core.commerce.rewrite_direct_voiceover', return_value=(revised, {})), \
                 patch('agent_core.commerce.review_direct_script', side_effect=TimeoutError('review unavailable')):
                with self.assertRaises(TimeoutError):
                    s._deliver_direct_ad(job, task, f.assets, f.sources, timeline, plan, Mock())
            checkpoint = s._narration_checkpoint(job)
            self.assertEqual(checkpoint['plan'], revised)
            self.assertFalse(checkpoint['approved'])


if __name__ == '__main__':
    unittest.main()
