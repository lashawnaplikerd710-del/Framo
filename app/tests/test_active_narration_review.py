import copy
import unittest
from unittest.mock import Mock

from agent_core import ad_narration, caption_policy
from agent_core.commerce import review_direct_script


class ActiveNarrationReviewTests(unittest.TestCase):
    def provider(self):
        provider = Mock()
        provider._json.return_value = {
            'schema_version': '1.0', 'decision': 'accept', 'issues': [],
            'product': '宠物帐篷窝', 'limits': '仅审核当前文案',
        }
        return provider

    def task(self):
        return {
            'workflow': 'custom', 'editing_rules_version': 'prompt_first_v1',
            'render_backend': 'direct_ffmpeg', 'narration_mode': 'new_native',
            'ad_narration_policy': ad_narration.POLICY,
            'caption_policy_version': caption_policy.VERSION, 'caption_mode': 'highlights',
        }

    def test_whole_output_review_excludes_unused_legacy_voice_without_mutating_plan(self):
        provider = self.provider()
        plan = {
            'title': '宠物窝展示', 'story': '按当前可见外观展示',
            'narration_script': '这款半封闭宠物窝，开口和内部空间清晰可见。',
            'voiceover': '旧的未使用口播：加大加厚加深。',
            'narrator_script': '旧的未使用旁白：加高围挡当小枕头。',
            'clips': [{
                'asset_id': 'asset_a', 'first_unit': 'v001', 'last_unit': 'v002',
                'heading': '半封闭宠物窝', 'reason': '展示可见结构',
                'voiceover': '旧的逐镜口播：更保暖。',
                'narrator_script': '旧的逐镜旁白。',
            }],
        }
        timeline = {'source_map': [], 'narration_cues': [{
            'start_us': 0, 'end_us': 4_000_000, 'text': plan['narration_script'],
        }], 'captions': [{'text': '半封闭宠物窝'}]}
        original_plan = copy.deepcopy(plan)
        original_timeline = copy.deepcopy(timeline)
        review_direct_script(provider, self.task(), [], plan, timeline)
        payload = provider._json.call_args.args[1]
        current = payload['plan']
        self.assertEqual(current['narration_script'], plan['narration_script'])
        self.assertEqual(payload['new_narration'], timeline['narration_cues'])
        self.assertEqual(payload['subtitle_cues'], timeline['captions'])
        for legacy in ('voiceover', 'narrator_script'):
            self.assertNotIn(legacy, current)
            self.assertNotIn(legacy, current['clips'][0])
        self.assertEqual(current['clips'][0]['heading'], '半封闭宠物窝')
        self.assertEqual(current['clips'][0]['first_unit'], 'v001')
        self.assertIsNot(current, plan)
        self.assertIsNot(current['clips'][0], plan['clips'][0])
        self.assertEqual(plan, original_plan)
        self.assertEqual(timeline, original_timeline)

    def test_legacy_per_clip_review_keeps_actual_voiceover_fields(self):
        task = self.task()
        task.pop('ad_narration_policy')
        provider = self.provider()
        plan = {'voiceover': '仍在使用的口播', 'narrator_script': '仍在使用的旁白',
                'clips': [{'voiceover': '仍在使用的逐镜口播', 'heading': '宠物窝'}]}
        original = copy.deepcopy(plan)
        review_direct_script(provider, task, [], plan,
                             {'source_map': [], 'narration_cues': []})
        self.assertEqual(provider._json.call_args.args[1]['plan'], original)
        self.assertEqual(plan, original)


if __name__ == '__main__':
    unittest.main()
