import copy
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_core import ad_narration, narration_timing
from agent_core.commerce import compile_ad, ScriptRevisionPending
from agent_core.task_store import atomic_json
from tests.test_auto_narration import AutoNarrationTests


class NarrationResumeMeasurementTests(unittest.TestCase):
    def fixture(self, raw):
        service, job, fixture, task, plan, _ = AutoNarrationTests().fixture(raw)
        task = ad_narration.configure(task)
        plan['narration_script'] = '画面のアイテムを見てみましょう。'
        timeline, _, _ = compile_ad(task, fixture.assets, fixture.sources, plan)
        return service, job, fixture, task, plan, timeline

    def save(self, service, job, kind, name, data):
        path = service.store.job_dir(job)/'auto-narration'/(name+'.json')
        atomic_json(path, data)
        service.store.put_artifact(job, kind, name, path)

    def test_missing_or_empty_measurement_reviews_then_measures_current_candidate(self):
        for measurement in (None, {}, {'available_us':0, 'measured_us':0}):
            with self.subTest(measurement=measurement), tempfile.TemporaryDirectory() as raw:
                service, job, f, task, plan, timeline = self.fixture(raw)
                saved = {'plan':plan, 'approved':False}
                if measurement is not None:
                    saved['measurement'] = measurement
                self.save(service, job, 'ad_auto_pending', 'pending', saved)
                order = []
                def review(*args):
                    order.append('review')
                    self.assertEqual(args[3], plan)
                    return {'decision':'accept', 'issues':[]}
                def measure(*args):
                    order.append('measure')
                    self.assertEqual(args[4], plan)
                    self.assertEqual(args[3]['ad_content_review']['decision'], 'accept')
                    return 'complete'
                with patch('agent_core.commerce.review_direct_script', side_effect=review), \
                     patch.object(service, '_run_direct_delivery', side_effect=measure), \
                     patch('agent_core.commerce.rewrite_direct_voiceover') as rewrite:
                    self.assertEqual(service._deliver_direct_ad(job, task, f.assets,
                        f.sources, timeline, plan, Mock()), 'complete')
                self.assertEqual(order, ['review', 'measure'])
                rewrite.assert_not_called()
                self.assertTrue(service._narration_checkpoint(job)['approved'])

    def test_changed_narration_does_not_reuse_prior_measurement_or_target(self):
        with tempfile.TemporaryDirectory() as raw:
            service, job, f, task, plan, timeline = self.fixture(raw)
            previous = copy.deepcopy(plan)
            previous['narration_script'] = '最初の長い文案をそのまま使うことはできません。'
            measured = {'available_us':15_000_000, 'measured_us':25_000_000}
            self.save(service, job, 'narration_measurement', 'old-measurement',
                      {'plan':previous, 'measurement':measured, 'target_units':1})
            self.save(service, job, 'ad_auto_pending', 'latest',
                      {'plan':plan, 'measurement':measured, 'target_units':1})
            with patch('agent_core.commerce.review_direct_script', return_value={'decision':'accept','issues':[]}) as review, \
                 patch.object(service, '_run_direct_delivery', return_value='complete') as delivery, \
                 patch('agent_core.commerce.rewrite_direct_voiceover') as rewrite:
                self.assertEqual(service._deliver_direct_ad(job, task, f.assets,
                    f.sources, timeline, previous, Mock()), 'complete')
            review.assert_called_once()
            rewrite.assert_not_called()
            self.assertEqual(delivery.call_args.args[4], plan)

    def test_legacy_paragraph_migration_requires_new_measurement(self):
        with tempfile.TemporaryDirectory() as raw:
            service, job, f, task, plan, timeline = self.fixture(raw)
            legacy = copy.deepcopy(plan)
            legacy['voiceover'] = legacy.pop('narration_script')
            measured = {'available_us':15_000_000, 'measured_us':25_000_000}
            self.save(service, job, 'narration_measurement', 'old-measurement',
                      {'plan':legacy, 'measurement':measured, 'target_units':1})
            self.save(service, job, 'ad_auto_pending', 'latest',
                      {'plan':legacy, 'measurement':measured, 'target_units':1})
            with patch('agent_core.commerce.review_direct_script', return_value={'decision':'accept','issues':[]}) as review, \
                 patch.object(service, '_run_direct_delivery', return_value='complete') as delivery, \
                 patch('agent_core.commerce.rewrite_direct_voiceover') as rewrite:
                service._deliver_direct_ad(job, task, f.assets, f.sources, timeline, plan, Mock())
            review.assert_called_once()
            rewrite.assert_not_called()
            self.assertEqual(delivery.call_args.args[4]['narration_script'], plan['narration_script'])

    def test_unreviewed_candidate_cannot_reach_measurement_when_review_rejects(self):
        with tempfile.TemporaryDirectory() as raw:
            service, job, f, task, plan, timeline = self.fixture(raw)
            self.save(service, job, 'ad_auto_pending', 'latest', {'plan':plan, 'measurement':{}})
            with patch('agent_core.commerce.review_direct_script', return_value={'decision':'revise','issues':['没有画面依据']}), \
                 patch.object(service, '_factual_narration_repair', return_value=None), \
                 patch.object(service, '_run_direct_delivery') as delivery:
                with self.assertRaisesRegex(ScriptRevisionPending, '没有画面依据'):
                    service._deliver_direct_ad(job, task, f.assets, f.sources, timeline, plan, Mock())
            delivery.assert_not_called()
            self.assertFalse(service._narration_checkpoint(job)['approved'])

    def test_review_timeout_keeps_latest_candidate_and_cannot_measure_it(self):
        with tempfile.TemporaryDirectory() as raw:
            service, job, f, task, plan, timeline = self.fixture(raw)
            self.save(service, job, 'ad_auto_pending', 'latest', {'plan':plan, 'measurement':{}})
            with patch('agent_core.commerce.review_direct_script', side_effect=TimeoutError('offline')), \
                 patch.object(service, '_run_direct_delivery') as delivery:
                with self.assertRaises(TimeoutError):
                    service._deliver_direct_ad(job, task, f.assets, f.sources, timeline, plan, Mock())
            delivery.assert_not_called()
            checkpoint = service._narration_checkpoint(job)
            self.assertFalse(checkpoint['approved'])
            self.assertEqual(checkpoint['plan'], plan)

    def test_actual_new_measurement_replaces_stale_target_before_repair(self):
        with tempfile.TemporaryDirectory() as raw:
            service, job, f, task, plan, timeline = self.fixture(raw)
            previous = copy.deepcopy(plan)
            previous['narration_script'] = '前の長い文案です。'
            stale = {'available_us':15_000_000, 'measured_us':25_000_000}
            fresh = {'available_us':15_000_000, 'measured_us':19_000_000}
            self.save(service, job, 'narration_measurement', 'old',
                      {'plan':previous, 'measurement':stale, 'target_units':1})
            self.save(service, job, 'ad_auto_pending', 'latest',
                      {'plan':plan, 'measurement':stale, 'target_units':1})
            revised = copy.deepcopy(plan)
            revised['narration_script'] = '近くで見ましょう。'
            with patch('agent_core.commerce.review_direct_script', return_value={'decision':'accept','issues':[]}) as review, \
                 patch.object(service, '_run_direct_delivery', side_effect=[narration_timing.NarrationDurationIssue(fresh), 'complete']) as delivery, \
                 patch('agent_core.commerce.rewrite_direct_voiceover', return_value=(revised,{})) as rewrite:
                self.assertEqual(service._deliver_direct_ad(job, task, f.assets,
                    f.sources, timeline, plan, Mock()), 'complete')
            expected = max(1, int(narration_timing.units(task, plan['narration_script'])*15/19*.82))
            self.assertGreater(expected, 1)
            self.assertEqual(rewrite.call_args.args[1]['_narration_repair_max_units'], expected)
            self.assertEqual(review.call_count, 2)
            self.assertEqual(delivery.call_count, 2)


if __name__ == '__main__':
    unittest.main()
