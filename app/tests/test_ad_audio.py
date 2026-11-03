import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from agent_core import ad_narration, sound_design
from agent_core.commerce import compile_ad, NarrationPlanIssue, rewrite_direct_voiceover, planning_evidence
from agent_core.direct_resources import build_direct_media_plan
from tests.test_market_rules import MarketRuleTests


class AdAudioTests(unittest.TestCase):
    def fixture(self):
        f, task, plan = MarketRuleTests().fixture('美国')
        task = ad_narration.configure(task)
        for clip in plan['clips']:
            clip.pop('voiceover', None)
        return f, task, plan

    def test_missing_independent_script_cannot_fall_back_to_headings(self):
        f, task, plan = self.fixture()
        with self.assertRaises(NarrationPlanIssue) as error:
            compile_ad(task, f.assets, f.sources, plan)
        self.assertTrue(error.exception.timeline['source_map'])

    def test_whole_ad_has_one_cue_and_independent_short_copy(self):
        f, task, plan = self.fixture()
        plan['narration_script'] = 'Give your pet a cozy little corner. Open the curtains, and let them settle in. Take a closer look.'
        timeline, _, _ = compile_ad(task, f.assets, f.sources, plan)
        self.assertEqual(len(timeline['narration_cues']), 1)
        self.assertEqual(timeline['narration_cues'][0]['text'], plan['narration_script'])
        self.assertEqual([c['text'] for c in timeline['captions']], [c['heading'] for c in plan['clips']])
        self.assertNotIn('visual_anchor_us', timeline['narration_cues'][0])

    def test_repair_returns_whole_script_and_keeps_shots(self):
        f, task, plan = self.fixture()
        try:
            compile_ad(task, f.assets, f.sources, plan)
        except NarrationPlanIssue as error:
            timeline = error.timeline
        provider = Mock()
        provider._json.return_value = {'schema_version':'1.0', 'narration_script':'A little corner for your pet. Let them settle in, then take a closer look.'}
        revised, report = rewrite_direct_voiceover(provider, task, f.sources, plan, timeline, 'write independent narration')
        self.assertEqual(revised['clips'], plan['clips'])
        self.assertTrue(report['one_tts_request'])
        payload = provider._json.call_args.args[1]
        self.assertNotIn('heading', payload['selected_visual_evidence'][0])
        self.assertIn('observations', payload['selected_visual_evidence'][0])

    def test_transcript_request_retains_existing_caption_mode(self):
        f, task, plan = MarketRuleTests().fixture('美国')
        from agent_core.caption_policy import configure
        task = ad_narration.configure(configure({**task, 'instruction':'逐字字幕'}))
        self.assertFalse(ad_narration.enabled(task))
        timeline, _, _ = compile_ad(task, f.assets, f.sources, plan)
        self.assertEqual(timeline['captions'][0]['text'], plan['clips'][0]['voiceover'])

    def test_us_copy_language_is_rechecked_after_model_repairs(self):
        f, task, plan = self.fixture()
        plan['narration_script'] = 'Give your pet a cozy corner. Let them settle in, then take a closer look.'
        plan['clips'][0]['heading'] = '宠物的小天地'
        with self.assertRaisesRegex(NarrationPlanIssue, '屏幕文案'):
            compile_ad(task, f.assets, f.sources, plan)

    def test_early_ending_triggers_pacing_repair_but_intentional_silence_is_allowed(self):
        _, task, _ = self.fixture()
        cues = [{'start_us':0,'end_us':10_000_000}]
        self.assertIsNone(ad_narration.pacing_issue(task,cues,21_000_000))
        self.assertEqual(ad_narration.pacing_issue({**task,'instruction':'需要全程口播'},cues,21_000_000)['kind'],'underfilled')
        self.assertIsNone(ad_narration.pacing_issue({**task,'instruction':'保留无口播展示，最后留白'},cues,21_000_000))
        self.assertIsNone(ad_narration.pacing_issue(task,[{'start_us':0,'end_us':18_000_000}],21_000_000))

    def test_oversized_paragraph_is_rewritten_without_changing_shots(self):
        f, task, plan = self.fixture()
        try:
            compile_ad(task, f.assets, f.sources, plan)
        except NarrationPlanIssue as error:
            timeline = error.timeline
        provider = Mock()
        provider._json.side_effect = [
            {'schema_version':'1.0','narration_script':'Very cozy pet bed. '*60},
            {'schema_version':'1.0','narration_script':'Give your pet a quiet little corner. Open the curtains and let them settle in. Take a closer look.'}]
        revised, report = rewrite_direct_voiceover(provider, task, f.sources, plan, timeline, 'write independent narration')
        self.assertEqual(provider._json.call_count, 2)
        self.assertEqual(revised['clips'], plan['clips'])
        self.assertEqual(len(report['length_checks']), 1)

    def sounds_and_timeline(self):
        rows = [{'id':str(i), 'sha256':str(i), 'family':['movement','reveal','accent','soft_tap'][i%4],
                 'duration_us':200_000, 'recommended_gain':.2} for i in range(16)]
        timeline = {'duration_us':24_000_000, 'source_map':[
            {'timeline_start_us':i*2_000_000, 'timeline_end_us':(i+1)*2_000_000} for i in range(12)]}
        plan = {'clips':[{'role':'hook' if i==0 else 'closing' if i==11 else 'proof', 'reason':'细节展示'} for i in range(12)]}
        return rows, timeline, plan

    def test_event_sounds_vary_by_hash_and_family_with_bounded_frequency(self):
        rows, timeline, plan = self.sounds_and_timeline()
        task = {'narration_mode':'new_native', 'instruction':''}
        report = sound_design.arrange(task, timeline, plan, seed='one', candidates=rows)
        events = report['events']
        self.assertLessEqual(len(events), 5)
        for a,b in zip(events, events[1:]):
            self.assertNotEqual(a['sound']['sha256'], b['sound']['sha256'])
            self.assertNotEqual(a['sound']['family'], b['sound']['family'])
            self.assertGreaterEqual(b['start_us']-a['start_us'], report['min_gap_us'])
        self.assertTrue(all(e['start_us']+e['duration_us']<=timeline['duration_us'] for e in events))
        dense = sound_design.arrange({**task,'instruction':'密集音效'}, timeline, plan, candidates=rows)
        self.assertGreater(len(dense['events']), len(events))

    def test_one_sound_is_used_once_and_identical_audio_is_deduplicated(self):
        rows, timeline, plan = self.sounds_and_timeline()
        candidates = [rows[0], {**rows[0],'id':'same-file-different-name'}]
        report = sound_design.arrange({}, timeline, plan, candidates=candidates)
        self.assertEqual(len(report['events']), 1)

    def test_disabled_sound_adds_no_track_and_builtin_assets_are_verified(self):
        rows = sound_design.catalog()
        self.assertGreaterEqual(len(rows), 20)
        self.assertEqual(len({r['sha256'] for r in rows}), len(rows))
        _, timeline, plan = self.sounds_and_timeline()
        task = {'workflow':'custom', 'sound_design_version':sound_design.POLICY,
                'enabled_direct_resources':[], 'narration_mode':'text_only'}
        with tempfile.TemporaryDirectory() as folder:
            result = build_direct_media_plan(task, timeline, plan, folder, {})
        self.assertFalse(result['tracks'])


if __name__ == '__main__':
    unittest.main()
