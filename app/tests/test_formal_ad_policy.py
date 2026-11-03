import json
import tempfile
import unittest
from pathlib import Path

from agent_core.commerce import formal_motion_request
from agent_core.formal_ad_policy import FORMAL_AD_REQUIRED_NATIVE_CATEGORIES
from agent_core.task_service import task_spec


class FormalAdPolicyTests(unittest.TestCase):
    def test_product_ad_defaults_to_full_new_ad_contract(self):
        spec = task_spec('商品广告混剪', ['a', 'b'], 'product_ad')
        policy = spec['formal_ad_policy']
        self.assertTrue(policy['formal_ad'])
        self.assertTrue(policy['new_narration_required'])
        self.assertTrue(policy['selling_copy_required'])
        self.assertTrue(policy['music_required'])
        self.assertTrue(policy['sound_effects_required'])
        self.assertTrue(policy['transitions_required'])
        self.assertTrue(policy['rhythm_changes_required'])
        self.assertTrue(policy['zoom_required'])
        self.assertEqual(spec['narration_mode'], 'new_native')
        self.assertEqual(spec['subtitle_policy'], 'editable_selling_copy_plus_original_burned_text')
        self.assertEqual(spec['required_native_categories'], list(FORMAL_AD_REQUIRED_NATIVE_CATEGORIES))

    def test_technical_other_montage_keeps_legacy_source_text_policy(self):
        spec = task_spec('商品素材测试', ['a', 'b'], 'product_montage')
        self.assertFalse(spec['formal_ad_policy']['formal_ad'])
        self.assertFalse(spec['constraints']['subtitles'])
        self.assertEqual(spec['subtitle_policy'], 'preserve_source')

    def test_crossborder_markets_use_full_ad_elements_without_narration(self):
        for country, locale in (('日本', 'ja-JP'), ('美国', 'en-US')):
            spec = task_spec('跨境电商广告', ['a', 'b'], 'crossborder_montage',
                             target_country=country, render_backend='direct_ffmpeg')
            policy = spec['formal_ad_policy']
            self.assertTrue(policy['formal_ad'])
            self.assertTrue(policy['music_required'])
            self.assertTrue(policy['sound_effects_required'])
            self.assertTrue(policy['sticker_required'])
            self.assertTrue(policy['transitions_required'])
            self.assertTrue(policy['effects_required'])
            self.assertTrue(policy['filters_required'])
            self.assertTrue(policy['zoom_required'])
            self.assertEqual(spec['target_locale'], locale)
            self.assertEqual(spec['narration_mode'], 'text_only')
            self.assertEqual(spec['subtitle_policy'],
                             'editable_localized_selling_copy_plus_original_burned_text')
            self.assertTrue(spec['constraints']['subtitles'])
            self.assertIn('sticker', spec['required_native_categories'])

    def test_motion_request_contains_verified_scale_curves(self):
        with tempfile.TemporaryDirectory() as folder:
            draft = Path(folder)
            (draft / 'draft_content.json').write_text(json.dumps({
                'tracks': [{'type': 'video', 'segments': [
                    {'id': 'clip1', 'target_timerange': {'start': 0, 'duration': 3_000_000}},
                ]}],
            }), encoding='utf-8')
            request = formal_motion_request(draft)
        self.assertEqual(request['schema'], 'jianying-native-motion-v1')
        self.assertEqual(request['curves'][0]['property'], 'scale')
        self.assertEqual(request['curves'][0]['points'][1]['value'], 1.06)


if __name__ == '__main__':
    unittest.main()

