import copy
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from agent_core import narration_timing as timing
from agent_core.commerce import validate_ad_script, NarrationPlanIssue
from agent_core.direct_resources import build_direct_media_plan
from agent_core.direct_renderer import snapshot_from_timeline
from tests.test_market_rules import spec


class NarrationTimingTests(unittest.TestCase):
    def test_actual_reported_short_shot_accepts_long_sentence_in_output_budget(self):
        task = spec('美国')
        plan = {'clips': [
            {'heading': 'Cozy corner', 'voiceover': 'Check this out. A fluffy little hideout, and your cat peeks right out the front.'},
            {'heading': 'Details', 'voiceover': 'Take a look.'}]}
        timeline = {'duration_us': 12_000_000, 'source_map': [
            {'timeline_start_us': 0, 'timeline_end_us': 1_933_333},
            {'timeline_start_us': 1_933_333, 'timeline_end_us': 12_000_000}]}
        validate_ad_script(task, plan, timeline)
        timeline['duration_us'] = 3_000_000
        with self.assertRaisesRegex(NarrationPlanIssue, '整条成品'):
            validate_ad_script(task, plan, timeline)

    def test_sequential_spans_cannot_cross_output_boundary(self):
        spans = timing.schedule([3_000_000, 1_000_000], 5_000_000)
        self.assertEqual(spans, [{'start_us': 0, 'end_us': 3_000_000},
                                {'start_us': 3_120_000, 'end_us': 4_120_000}])
        with self.assertRaisesRegex(ValueError, '总时长'):
            timing.schedule([4_000_000, 2_000_000], 5_000_000)

    def fixture(self):
        cues = [{'source_cue_id': 'narration_001', 'start_us': 0, 'end_us': 1_000_000,
                 'text': '给毛孩子准备一个柔软小窝。'},
                {'source_cue_id': 'narration_002', 'start_us': 1_000_000, 'end_us': 5_000_000,
                 'text': '看看商品详情吧。'}]
        captions = [{**c, 'source_cue_id': 'heading_'+str(i+1),
                     'narration_cue_id': c['source_cue_id']} for i,c in enumerate(cues)]
        timeline = {'duration_us': 5_000_000, 'narration_timing_policy': timing.POLICY,
                    'narration_cues': cues, 'captions': captions, 'source_map': [
                        {'asset_id': 'a', 'source_start_us': 0, 'source_end_us': 1_000_000,
                         'timeline_start_us': 0, 'timeline_end_us': 1_000_000},
                        {'asset_id': 'a', 'source_start_us': 1_000_000, 'source_end_us': 5_000_000,
                         'timeline_start_us': 1_000_000, 'timeline_end_us': 5_000_000}]}
        task = {'workflow': 'custom', 'render_backend': 'direct_ffmpeg', 'narration_mode': 'new_native',
                'target': {'fps': 30, 'aspect_ratio': '9:16'}}
        return task, timeline

    def test_measured_pcm_and_subtitles_cross_cut_without_speedup_or_input_mutation(self):
        task, timeline = self.fixture()
        before = copy.deepcopy(timeline)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            def synth(text, output, **kwargs):
                seconds = 3 if text == timeline['narration_cues'][0]['text'] else 1
                with wave.open(str(output), 'wb') as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                    wav.writeframes(b'\1\0' * 16000 * seconds)
            with patch('agent_core.direct_resources.synthesize_windows_sapi', side_effect=synth) as tts:
                media = build_direct_media_plan(task, timeline, {}, root, {})
                again = build_direct_media_plan(task, timeline, {}, root, {})
                self.assertEqual(tts.call_count, 2)  # second pass reuses complete PCM
            self.assertFalse(media['pending'])
            self.assertEqual(media['narration_cues'], again['narration_cues'])
            self.assertEqual(media['captions'][0]['end_us'], 3_000_000)
            self.assertEqual(media['captions'][1]['start_us'], 3_120_000)
            self.assertTrue(all(r['tempo'] == 1 for r in media['resources'] if r['kind']=='narration'))
            assets = [{'asset_id': 'a', 'path': str(root/'video.mp4'), 'width': 720,
                       'height': 1280, 'has_audio': False}]
            snapshot = snapshot_from_timeline(task, assets, timeline, media_plan=media)
            text = next(t for t in snapshot['tracks'] if t['type']=='text')
            self.assertEqual(text['segments'][0]['target_timerange']['duration'], 3_000_000)
            self.assertEqual(timeline, before)

    def test_total_audio_overflow_preserves_cache_and_does_not_render_partial_voice(self):
        task, timeline = self.fixture()
        with tempfile.TemporaryDirectory() as raw:
            def synth(text, output, **kwargs):
                with wave.open(str(output), 'wb') as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                    wav.writeframes(b'\1\0' * 16000 * 3)
            with patch('agent_core.direct_resources.synthesize_windows_sapi', side_effect=synth):
                media = build_direct_media_plan(task, timeline, {}, Path(raw), {})
            self.assertIn('总时长', media['pending'][0]['reason'])
            self.assertFalse(media['tracks'])
            self.assertEqual(len(list(Path(raw).glob('direct-resources/narration/*.wav'))), 2)
            self.assertEqual(media['report']['tts']['status'], 'FAILED')
            with self.assertRaisesRegex(ValueError, '拒绝静默'):
                snapshot_from_timeline(task, [{'asset_id': 'a', 'path': 'fixture.mp4',
                    'width': 720, 'height': 1280, 'has_audio': False}], timeline, media_plan=media)


if __name__ == '__main__':
    unittest.main()
