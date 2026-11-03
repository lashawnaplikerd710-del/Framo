import unittest
from agent_core.video_limits import duration_from_instruction

class ComplexDurationTests(unittest.TestCase):
    def test_local_editing_clocks_are_not_output_durations(self):
        for text in ['前3秒字幕上方，3-8秒字幕下方','制作15秒日区视频，前3秒字幕上方，3秒后下方，转场0.3秒',
                     '成片15秒，口播10秒，最后2秒展示商品','制作0.5分钟视频，字幕显示2秒']:
            expected=None if text.startswith('前') else 30000 if '0.5分钟' in text else 15000
            self.assertEqual(duration_from_instruction(text),expected,text)
    def test_real_conflicts_and_bounds_still_reject(self):
        for text in ['制作15秒视频，成片30秒','成片11分钟','成片0.5秒']:
            with self.assertRaises(ValueError):duration_from_instruction(text)
