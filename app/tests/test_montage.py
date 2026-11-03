import copy
import unittest

from agent_core.montage import montage_candidates, validate_montage_plan, compile_montage, sample_positions
from agent_core.task_service import task_spec
from agent_core.timeline_compiler import PlanIssue


class MontageTests(unittest.TestCase):
    def setUp(self):
        self.assets = [{'schema_version': '1.0', 'asset_id': 'asset_' + char * 24,
                        'path': f'C:/素材 空格/{char}.mp4', 'width': 1080, 'height': 1920,
                        'video_duration_us': 10_000_000, 'audio_duration_us': 10_000_000,
                        'frame_rate_mode': 'CFR', 'rotation': 0, 'has_audio': True}
                       for char in ('a', 'b')]
        self.task = task_spec('日语商品混剪，保留原有文字和声音',
                              [a['asset_id'] for a in self.assets], 'product_montage')
        self.transcript = {'cues': [{'start_us': 200_000, 'end_us': 6_300_000,
                                    'text': 'この色の並びを見て、背中もチェック。'}]}
        self.voice = montage_candidates(self.assets[0], self.transcript)
        self.visual = montage_candidates(self.assets[1], {'cues': []})
        self.candidates = self.voice + self.visual

    def plan(self, *rows):
        return {'schema_version': '1.0', 'clips': [
            {'source_segment_id': r['segment_id'], 'role': 'detail', 'reason': '测试映射',
             'evidence_ids': [r['evidence_id']]} for r in rows]}

    def test_detected_speech_is_not_cut_into_character_groups(self):
        self.assertEqual(len(self.voice), 2)
        self.assertEqual(self.voice[1]['start_us'], 0)
        self.assertGreaterEqual(self.voice[1]['end_us'], 6_800_000)
        self.assertEqual(self.voice[1]['text'], self.transcript['cues'][0]['text'])

    def test_fractional_source_tail_stays_inside_native_observation_clip(self):
        for duration in (10_334_000, 10_267_000, 10_100_000, 10_000_000):
            positions = sample_positions(duration)
            native_duration = (int(duration / 1e6 * 30) - 1) * 1_000_000 // 30
            self.assertEqual(len(positions), 8)
            self.assertEqual(positions[0], 0)
            self.assertTrue(all(0 <= t < native_duration for t in positions))

    def test_empty_asr_is_labeled_uncertain_and_does_not_invent_dialogue(self):
        self.assertGreater(len(self.visual), 1)
        self.assertTrue(all(row['text'] == '' for row in self.visual))
        self.assertIn('does not prove', self.visual[1]['uncertainty'])
        self.assertTrue(all(row['end_us'] <= 10_000_000 for row in self.visual))

    def test_multiple_sources_compile_without_gaps_and_keep_embedded_sound(self):
        result = compile_montage(self.task, self.assets, self.candidates,
                                 self.plan(self.voice[1], self.visual[1], self.visual[2]))
        mapping = result['source_map']
        self.assertEqual(mapping[0]['timeline_start_us'], 0)
        self.assertEqual([r['asset_id'] for r in mapping],
                         [self.assets[0]['asset_id'], self.assets[1]['asset_id'], self.assets[1]['asset_id']])
        for left, right in zip(mapping, mapping[1:]):
            self.assertEqual(left['timeline_end_us'], right['timeline_start_us'])
        self.assertEqual(result['duration_us'], mapping[-1]['timeline_end_us'])
        self.assertEqual(result['captions'], [])
        self.assertEqual(sum(s['action'] == 'video.setFade' for s in result['native_plan']['steps']), 3)
        self.assertFalse(any(s['action'] == 'video.setVolume' for s in result['native_plan']['steps']))

    def test_millisecond_source_tail_rounds_inward_on_frame_grid(self):
        assets=copy.deepcopy(self.assets)
        assets[0]['video_duration_us']=15_933_000
        assets[0]['audio_duration_us']=15_933_000
        tail={**self.voice[0],'start_us':13_941_375,'end_us':15_933_000}
        # The exact failing customer interval previously rounded out by 333us.
        rows=[self.visual[1],tail]
        result=compile_montage(self.task,assets,rows,self.plan(*rows))
        final=result['source_map'][-1]
        self.assertLessEqual(final['source_end_us'],15_933_000)
        self.assertLess(15_933_000-final['source_end_us'],34_000)
        self.assertEqual(result['source_map'][0]['timeline_end_us'],final['timeline_start_us'])
        self.assertGreater(final['source_end_us'],final['source_start_us'])

    def test_real_out_of_range_interval_is_not_accepted_by_rounding(self):
        invalid={**self.voice[0],'start_us':9_000_000,'end_us':10_001_000}
        rows=[invalid,self.visual[1]]
        with self.assertRaisesRegex(PlanIssue,'leaves the video stream'):
            compile_montage(self.task,self.assets,rows,self.plan(*rows))

    def test_overlapping_alternatives_and_single_source_are_rejected(self):
        for plan in (self.plan(self.voice[0], self.voice[1], self.visual[1]),
                     self.plan(self.visual[1], self.visual[2])):
            with self.assertRaises(PlanIssue):
                validate_montage_plan(plan, self.candidates, self.task)

    def test_wrong_source_manifest_ratio_and_duration_are_rejected(self):
        plan = self.plan(self.voice[1], self.visual[1])
        with self.assertRaisesRegex(PlanIssue, 'manifest'):
            compile_montage(self.task, self.assets[:1], self.candidates, plan)
        assets = copy.deepcopy(self.assets)
        assets[1]['width'], assets[1]['height'] = 1920, 1080
        with self.assertRaisesRegex(PlanIssue, 'aspect ratio'):
            compile_montage(self.task, assets, self.candidates, plan)
        task = copy.deepcopy(self.task)
        task['target']['duration_ms'] = 30_000
        with self.assertRaises(PlanIssue):
            compile_montage(task, self.assets, self.candidates, plan)

    def test_original_order_constraint_and_duplicate_inputs_are_checked(self):
        task = task_spec('日语混剪，保持原顺序', [a['asset_id'] for a in self.assets], 'product_montage')
        with self.assertRaisesRegex(PlanIssue, 'order'):
            validate_montage_plan(self.plan(self.visual[1], self.voice[1]), self.candidates, task)
        with self.assertRaisesRegex(ValueError, 'distinct'):
            task_spec('混剪', [self.assets[0]['asset_id']] * 2, 'product_montage')


if __name__ == '__main__':
    unittest.main()
