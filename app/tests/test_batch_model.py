import unittest

from agent_core.batch_model import (DEFAULT_GAP_US, MAX_BATCH_OUTPUTS, ResourceSlots,
                                     batch_counts, batch_state, estimate_timing,
                                     validate_batch_spec)


class BatchModelTests(unittest.TestCase):
    def row(self, index=1):
        return {'output_id': f'out-{index}', 'instruction': '制作短视频',
                'asset_ids': [f'asset_{index}']}

    def test_batch_limit_and_gap_are_normalized_to_frames(self):
        spec = validate_batch_spec({'outputs': [self.row()], 'gap_us': 1_000_001, 'fps': 30})
        self.assertEqual(spec['gap_us'], 1_000_000)
        with self.assertRaisesRegex(ValueError, '1..15'):
            validate_batch_spec({'outputs': [self.row(i) for i in range(1, MAX_BATCH_OUTPUTS + 2)]})

    def test_default_gap_is_three_seconds(self):
        spec = validate_batch_spec({'outputs': [self.row()]})
        self.assertEqual(DEFAULT_GAP_US, 3_000_000)
        self.assertEqual(spec['gap_us'], 3_000_000)

    def test_adaptive_budget_has_reference_slo_without_duration_branch(self):
        simple = estimate_timing({'duration_us': 15_000_000, 'output_count': 1,
                                  'source_count': 1, 'effect_count': 1})
        complex_ = estimate_timing({'duration_us': 180_000_000, 'output_count': 15,
                                    'source_count': 20, 'shot_count': 80,
                                    'effect_count': 20, 'export_count': 15})
        self.assertEqual(simple['soft_budget_seconds'], 600.0)
        self.assertGreater(complex_['soft_budget_seconds'], simple['soft_budget_seconds'])
        self.assertTrue(complex_['quality_preserved'])

    def test_slots_and_batch_report_are_isolated(self):
        slots = ResourceSlots({'native': 1})
        first = slots.acquire('native')
        self.assertIsNotNone(first)
        self.assertIsNone(slots.acquire('native'))
        slots.release(first)
        self.assertIsNotNone(slots.acquire('native'))
        self.assertEqual(batch_counts(['READY_FOR_REVIEW', 'FAILED', 'CREATED']),
                         {'success': 1, 'failed': 1, 'processing': 1,
                          'waiting_manual': 0, 'retryable': 1})
        self.assertEqual(batch_state(['READY_FOR_REVIEW', 'FAILED']), 'PARTIAL_FAILURE')


if __name__ == '__main__':
    unittest.main()
