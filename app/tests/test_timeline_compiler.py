import copy
import unittest

from agent_core.timeline_compiler import PlanIssue, candidates_from_cues, compile_timeline, validate_edit_plan


class TimelineCompilerTests(unittest.TestCase):
    def setUp(self):
        self.cues = [{'start_us': 1_000_000, 'end_us': 2_000_000, 'text': '第一句。'},
                     {'start_us': 5_000_000, 'end_us': 6_000_000, 'text': '第二句。'}]
        self.candidates = candidates_from_cues('asset_123', self.cues, 20_000_000)
        self.asset = {'schema_version': '1.0', 'asset_id': 'asset_123', 'path': 'C:/素材/采访.mp4',
                      'width': 720, 'height': 1280, 'video_duration_us': 20_000_000,
                      'frame_rate_mode': 'CFR', 'rotation': 0}
        self.transcript = {'schema_version': '1.0', 'asset_id': 'asset_123', 'cues': self.cues}
        self.task = {'schema_version': '1.0', 'target': {'fps': 30, 'aspect_ratio': '9:16',
                                                       'duration_ms': 2000, 'tolerance_ms': 100}}
        self.plan = {'schema_version': '1.0', 'clips': [
            {'source_segment_id': 'segment_0002', 'role': 'opening', 'reason': '先提出结果',
             'evidence_ids': ['evidence_0002']},
            {'source_segment_id': 'segment_0001', 'role': 'context', 'reason': '补充原因',
             'evidence_ids': ['evidence_0001']}]}

    def test_reorder_maps_source_and_captions_without_inventing_text(self):
        result = compile_timeline(self.task, self.asset, self.transcript, self.candidates, self.plan)
        self.assertEqual([s['source_start_us'] for s in result['native_plan']['steps']],
                         [5_000_000, 1_000_000])
        self.assertEqual([m['timeline_start_us'] for m in result['source_map']], [0, 1_000_000])
        self.assertEqual([c['text'] for c in result['captions']], ['第二句。', '第一句。'])
        self.assertEqual([c['start_us'] for c in result['captions']], [0, 1_000_000])
        self.assertEqual(result['duration_us'], 2_000_000)
        self.assertEqual(result['native_plan']['canvas']['width'], 1080)

    def test_nonexistent_or_duplicate_segment_is_rejected(self):
        bad = copy.deepcopy(self.plan); bad['clips'][0]['source_segment_id'] = 'segment_9999'
        with self.assertRaisesRegex(PlanIssue, 'nonexistent'):
            validate_edit_plan(bad, self.candidates, self.task['target'])
        bad = copy.deepcopy(self.plan); bad['clips'][1]['source_segment_id'] = 'segment_0002'
        with self.assertRaisesRegex(PlanIssue, 'repeats'):
            validate_edit_plan(bad, self.candidates, self.task['target'])

    def test_duration_and_evidence_validation(self):
        bad = copy.deepcopy(self.plan); bad['clips'].pop()
        with self.assertRaisesRegex(PlanIssue, 'outside target'):
            validate_edit_plan(bad, self.candidates, self.task['target'])
        bad = copy.deepcopy(self.plan); bad['clips'][0]['evidence_ids'] = []
        with self.assertRaisesRegex(PlanIssue, 'cite'):
            validate_edit_plan(bad, self.candidates, self.task['target'])

    def test_unverified_crop_and_vfr_are_rejected(self):
        asset = {**self.asset, 'width': 1920, 'height': 1080}
        with self.assertRaisesRegex(PlanIssue, 'aspect ratios differ'):
            compile_timeline(self.task, asset, self.transcript, self.candidates, self.plan)
        asset = {**self.asset, 'frame_rate_mode': 'VFR'}
        with self.assertRaisesRegex(PlanIssue, 'Variable frame rate'):
            compile_timeline(self.task, asset, self.transcript, self.candidates, self.plan)
        asset = {**self.asset, 'audio_duration_us': 3_000_000}
        with self.assertRaisesRegex(PlanIssue, 'audio stream'):
            compile_timeline(self.task, asset, self.transcript, self.candidates, self.plan)

    def test_caption_cut_through_source_evidence_is_rejected(self):
        bad = copy.deepcopy(self.candidates)
        bad[0]['end_us'] = 1_500_000
        task = copy.deepcopy(self.task); task['target']['duration_ms'] = None
        with self.assertRaisesRegex(PlanIssue, 'does not fit'):
            compile_timeline(task, self.asset, self.transcript, bad, self.plan)

    def test_overlap_and_out_of_bounds_transcript_rejected(self):
        with self.assertRaises(PlanIssue):
            candidates_from_cues('asset_123', self.cues + [{'start_us': 5_500_000,
                               'end_us': 7_000_000, 'text': '重叠'}], 20_000_000)
        with self.assertRaises(PlanIssue):
            candidates_from_cues('asset_123', [{'start_us': 19_000_000,
                               'end_us': 21_000_000, 'text': '越界'}], 20_000_000)
