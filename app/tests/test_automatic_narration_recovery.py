import copy
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_core import ad_narration, editing_rules, narration_timing
from agent_core.commerce import compile_ad, NarrationPlanIssue
from agent_core.task_service import TaskService, task_spec
from agent_core.task_store import TaskStore
from tests import test_auto_narration


class AutomaticNarrationRecoveryTests(unittest.TestCase):
    def test_executed_chinese_options_and_speech_policy_survive_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            service = TaskService.__new__(TaskService)
            service.store = TaskStore(raw)
            task = editing_rules.configure_rules(task_spec(
                '生成中文口播添加中文文案，添加对应的贴纸和字体动效还有转场',
                ['asset_'+'a'*24], 'custom', render_backend='direct_ffmpeg'))
            task['caption_mode'] = 'highlights'
            job = service.store.put_job(task)
            resolved = {'schema_version':'1.0', 'overrides':{}, 'unsupported':[]}
            executed = service._persist_editing_rules(job, task, resolved)
            saved = service.store.get_job(job)['task_spec']
            self.assertTrue(ad_narration.enabled(saved))
            self.assertTrue(saved['constraints']['subtitles'])
            self.assertTrue(saved['resolved_editing_options']['sticker'])
            self.assertEqual(saved, executed)
            self.assertEqual(saved, json.loads((service.store.job_dir(job)/'task.json').read_text('utf-8')))

    def test_format_failures_switch_to_reviewed_alternative_and_measure_again(self):
        with tempfile.TemporaryDirectory() as raw:
            service, job, fixture, task, plan, timeline = test_auto_narration.AutoNarrationTests().fixture(raw)
            alternative = copy.deepcopy(plan)
            alternative['clips'][0]['voiceover'] = '近くで見てみましょう。'
            measurement = {'available_us':15_000_000, 'measured_us':22_000_000}
            calls = []
            def deliver(*args):
                calls.append(copy.deepcopy(args[4]))
                if len(calls) == 1:
                    raise narration_timing.NarrationDurationIssue(measurement)
                return {'state':'COMPLETED'}
            with patch.object(service, '_run_direct_delivery', side_effect=deliver), \
                 patch('agent_core.commerce.rewrite_direct_voiceover', side_effect=NarrationPlanIssue('格式错误')), \
                 patch('agent_core.ad_narration.factual_candidates', return_value=[(alternative, {'requires_content_review':True})]), \
                 patch('agent_core.commerce.review_direct_script', return_value={'decision':'accept', 'issues':[]}) as review:
                result = service._deliver_direct_ad(job, task, fixture.assets, fixture.sources, timeline, plan, Mock())
            self.assertEqual(result['state'], 'COMPLETED')
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[1], alternative)
            review.assert_called_once()
            self.assertTrue(any(a['kind']=='ad_auto_script' for a in service.store.list_artifacts(job)))

    def test_rejected_alternative_never_reaches_delivery(self):
        with tempfile.TemporaryDirectory() as raw:
            service, job, fixture, task, plan, timeline = test_auto_narration.AutoNarrationTests().fixture(raw)
            with patch('agent_core.ad_narration.factual_candidates', return_value=[(plan,{})]), \
                 patch('agent_core.commerce.review_direct_script', return_value={'decision':'revise','issues':['没有依据']}):
                result = service._factual_narration_repair(job, task, fixture.assets,
                    fixture.sources, plan, timeline, Mock())
            self.assertIsNone(result)
            self.assertFalse(service._narration_checkpoint(job)['approved'])

    def test_independent_rejection_is_fed_back_before_one_final_correction(self):
        with tempfile.TemporaryDirectory() as raw:
            service,job,fixture,task,plan,timeline=test_auto_narration.AutoNarrationTests().fixture(raw)
            rejected={'decision':'revise','issues':['前帘为中文词，应使用入口のカーテン']}
            with patch('agent_core.ad_narration.factual_candidates',return_value=[(plan,{})]) as generate, \
                 patch('agent_core.commerce.review_direct_script',side_effect=[rejected,{'decision':'accept','issues':[]}]):
                result=service._factual_narration_repair(job,task,fixture.assets,fixture.sources,plan,timeline,Mock())
            self.assertIsNotNone(result)
            self.assertEqual(generate.call_count,2)
            self.assertIn('前帘为中文词',generate.call_args.kwargs['feedback'])
            self.assertTrue(service._narration_checkpoint(job)['approved'])

    def test_fixed_length_is_never_extended(self):
        with tempfile.TemporaryDirectory() as raw:
            service, job, fixture, task, plan, timeline = test_auto_narration.AutoNarrationTests().fixture(raw)
            task = ad_narration.configure(task)
            task['target']['duration_ms'] = 16000
            result = service._extend_narration_shots(job, task, fixture.assets, fixture.sources,
                plan, timeline, Mock(), {'measured_us':30_000_000})
            self.assertIsNone(result)

    def test_open_length_extends_only_observed_nonoverlapping_source_and_reviews(self):
        with tempfile.TemporaryDirectory() as raw:
            service, job, fixture, task, plan, timeline = test_auto_narration.AutoNarrationTests().fixture(raw)
            task = ad_narration.configure(task)
            for source in fixture.sources:
                source['speech_units'].append({'unit_id':'u003', 'start_us':8_000_000,
                    'end_us':12_000_000, 'text':'完整展示。'})
                source['frames'].append({'requested_us':10_000_000})
            with patch('agent_core.commerce.review_direct_script',
                       return_value={'decision':'accept','issues':[]}) as review:
                result = service._extend_narration_shots(job, task, fixture.assets,
                    fixture.sources, plan, timeline, Mock(), {'measured_us':18_000_000})
            self.assertIsNotNone(result)
            changed, expanded = result
            self.assertEqual(expanded['duration_us'],20_000_000)
            review.assert_called_once()
            self.assertEqual(plan['clips'][1]['last_unit'],'u002')
            self.assertEqual(changed['clips'][1]['last_unit'],'u003')
            self.assertEqual(changed['clips'][0],plan['clips'][0])
            checkpoint = service._narration_checkpoint(job)
            self.assertTrue(checkpoint['approved'])
            self.assertTrue(checkpoint['repair']['source_only'])

    def test_initial_content_rejections_switch_to_independently_reviewed_factual_plan(self):
        with tempfile.TemporaryDirectory() as raw:
            service, job, fixture, task, plan, timeline = test_auto_narration.AutoNarrationTests().fixture(raw)
            alternative = copy.deepcopy(plan)
            alternative['clips'][0]['voiceover'] = '見てみましょう。'
            reviews = [{'decision':'revise','issues':['无法证实']}]*3 + [{'decision':'accept','issues':[]}]
            with patch('agent_core.commerce.plan_ad',return_value=plan), \
                 patch('agent_core.commerce.rewrite_direct_voiceover',return_value=(plan,{})), \
                 patch('agent_core.ad_narration.factual_candidates',return_value=[(alternative,{})]), \
                 patch('agent_core.commerce.review_direct_script',side_effect=reviews) as review:
                changed, expanded, _, _ = service._plan_direct_ad(
                    job,task,fixture.assets,fixture.sources,Mock())
            self.assertEqual(changed,alternative)
            self.assertEqual(expanded['ad_content_review']['decision'],'accept')
            self.assertEqual(review.call_count,4)

    def test_cancel_interrupts_automatic_repairs(self):
        with tempfile.TemporaryDirectory() as raw:
            service, job, fixture, task, plan, timeline = test_auto_narration.AutoNarrationTests().fixture(raw)
            service.store.request_cancel(job)
            from agent_core.local_executor import ToolCancelled
            with patch.object(service, '_run_direct_delivery') as deliver:
                with self.assertRaises(ToolCancelled):
                    service._deliver_direct_ad(job, task, fixture.assets, fixture.sources, timeline, plan, Mock())
                deliver.assert_not_called()


if __name__ == '__main__':
    unittest.main()
