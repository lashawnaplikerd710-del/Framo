import unittest

from agent_core.montage import montage_candidates
from agent_core.review_repairs import replace_repeated_ending
from agent_core.task_service import task_spec
from agent_core.timeline_compiler import PlanIssue


class ReviewRepairTests(unittest.TestCase):
    def setUp(self):
        self.assets = [{'schema_version': '1.0', 'asset_id': 'asset_' + c * 24,
                        'sha256': c * 64, 'path': f'C:/素材/{c}.mp4', 'width': 1080,
                        'height': 1920, 'video_duration_us': 10_000_000,
                        'audio_duration_us': 10_000_000, 'has_audio': True,
                        'frame_rate_mode': 'CFR', 'rotation': 0} for c in ('a', 'b')]
        self.task = task_spec('日语混剪', [a['asset_id'] for a in self.assets], 'product_montage')
        self.transcripts = {a['asset_id']: {'cues': [{'start_us': 0, 'end_us': 6_000_000,
                                                    'text': '背中の細部もチェック'}]} for a in self.assets}
        self.candidates = sum([montage_candidates(a, self.transcripts[a['asset_id']]) for a in self.assets], [])
        rows = [self.candidates[1], self.candidates[3]]
        self.plan = {'schema_version': '1.0', 'clips': [{'source_segment_id': r['segment_id'],
            'role': 'ending', 'reason': 'original model selection', 'evidence_ids': [r['evidence_id']]} for r in rows]}
        self.review = {'decision': 'revise', 'issues': [{'source_segment_id': rows[-1]['segment_id'],
                        'problem': '重复表达', 'suggested_fix': '删除结尾段'}]}
        self.probe = {'asset_id': self.assets[1]['asset_id'], 'source_sha256': 'b' * 64,
                      'range_us': [6_500_000, 9_966_666], 'engine': 'local_sensevoice_tail_probe',
                      'raw_result': {'text': '。'}}

    def test_excludes_repeated_speech_preserves_all_sources_and_original_plan(self):
        plan, evidence, timeline, resolution = replace_repeated_ending(self.task, self.assets,
            self.candidates, self.plan, self.review, self.transcripts, self.probe)
        self.assertEqual(timeline['revision_id'], 'revision_002')
        self.assertEqual(len({r['asset_id'] for r in timeline['source_map']}), 2)
        self.assertEqual(timeline['source_map'][-1]['source_start_us'], 6_500_000)
        self.assertTrue(resolution['known_repeated_speech_ranges_excluded'])
        self.assertFalse(plan['second_model_review_performed'])
        self.assertNotEqual(plan['clips'][-1], self.plan['clips'][-1])
        self.assertEqual(len(evidence), len(self.candidates) + 1)

    def test_detected_tail_speech_or_wrong_media_cannot_be_used_as_repair(self):
        for changed in ({**self.probe, 'raw_result': {'text': '新しい台詞'}},
                        {**self.probe, 'source_sha256': 'wrong'}):
            with self.assertRaises(PlanIssue):
                replace_repeated_ending(self.task, self.assets, self.candidates,
                                       self.plan, self.review, self.transcripts, changed)

    def test_unrelated_model_issue_does_not_trigger_automatic_deletion(self):
        self.review['issues'][0]['problem'] = '画面文字遮挡'
        with self.assertRaises(PlanIssue):
            replace_repeated_ending(self.task, self.assets, self.candidates,
                                   self.plan, self.review, self.transcripts, self.probe)


if __name__ == '__main__':
    unittest.main()
