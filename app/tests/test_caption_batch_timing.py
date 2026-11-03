import unittest
import tempfile
from pathlib import Path

from agent_core.independent_export import _extract_output, _subtitle_rows, _subtitle_image_rows


class CaptionBatchTimingTests(unittest.TestCase):
    def test_extraction_rebases_layout_and_keeps_continuous_loop_phase(self):
        master = {
            'duration': 12_000_000,
            'batch_outputs': [{'output_id': 'second', 'start_us': 7_000_000,
                               'end_us': 12_000_000}],
            'caption_layout_schedule': [
                {'start_us': 0, 'end_us': 2_000_000, 'style': {'placement': 'upper'}},
                {'start_us': 6_000_000, 'end_us': 9_000_000, 'style': {'y': .25}},
                {'start_us': 9_000_000, 'end_us': 12_000_000, 'style': {'y': .7}},
            ],
            'materials': {'texts': [{'id': 'text', 'content': '位置变化时动画继续',
                                    'caption_motion': {'entry': 'none', 'loop': 'float'}}]},
            'tracks': [{'type': 'text', 'segments': [{
                'material_id': 'text', 'caption_origin_start_us': 7_000_000,
                'target_timerange': {'start': 9_000_000, 'duration': 2_000_000}}]}],
        }
        child = _extract_output(master, 'second')
        self.assertEqual(child['caption_layout_schedule'], [
            {'start_us': 0, 'end_us': 2_000_000, 'style': {'y': .25}},
            {'start_us': 2_000_000, 'end_us': 5_000_000, 'style': {'y': .7}}])
        row = _subtitle_rows(child, with_styles=True, with_motion=True)[0]
        self.assertEqual(row[:2], (2_000_000, 4_000_000))
        self.assertEqual(row[6]['phase_us'], 2_000_000)
        self.assertEqual(master['tracks'][0]['segments'][0]['caption_origin_start_us'], 7_000_000)

    def test_srt_uses_layout_schedule_and_clips_to_independent_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'master.srt'
            original = '1\n00:00:06,000 --> 00:00:10,000\nKeep the loop phase\n'
            path.write_text(original, encoding='utf-8')
            master = {'duration': 12_000_000, 'tracks': [], 'subtitle_path': str(path),
                      'caption_motion': {'entry': 'assemble', 'loop': 'float'},
                      'caption_layout_schedule': [
                          {'start_us': 8_000_000, 'end_us': 10_000_000, 'style': {'y': .7}}],
                      'batch_outputs': [{'output_id': 'second', 'start_us': 7_000_000,
                                         'end_us': 12_000_000}]}
            child = _extract_output(master, 'second')
            rows = _subtitle_image_rows(child)
            self.assertEqual([row[:2] for row in rows], [(0, 1_000_000), (1_000_000, 3_000_000)])
            self.assertEqual([row[6]['phase_us'] for row in rows], [1_000_000, 2_000_000])
            self.assertEqual([row[6]['entry'] for row in rows], ['none', 'none'])
            self.assertEqual(rows[1][4]['y'], .7)
            self.assertEqual(path.read_text(encoding='utf-8'), original)


if __name__ == '__main__':
    unittest.main()
