import copy
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.auto_decorations import fonts, select_caption_font
from agent_core.independent_export import _materialize_subtitle_images
from agent_core.workbench import Workbench
from agent_core.user_messages import chinese_diagnostic


class FontFailureTests(unittest.TestCase):
    def test_actual_failed_japanese_job_chinese_headings_render_without_editing_text(self):
        library = fonts()
        headings = ['猫咪自己钻进去了', '三面包裹的抽屉空间', '在里面睡得踏实', 'サイズは商品ページで']
        for index, text in enumerate(headings):
            with self.subTest(text=text):
                cue = {'text':text, 'source_cue_id':'heading_001'}
                choice = select_caption_font({'target_locale':'ja-JP'}, cue, [])
                family = library.families[choice['font_id']]
                record = next(r for r in family['files'] if str(library.root/r['file']) == choice['path'])
                self.assertEqual(library.missing(record, text), [])
                self.assertEqual(choice['language'], 'ja')
                self.assertEqual(choice['locale_fallback'], index < 3)
                snapshot = {'duration':1_000_000, 'canvas_config':{'width':720,'height':1280},
                    'materials':{'texts':[{'id':'t1','content':text,'font_selection':choice}]},
                    'tracks':[{'type':'text','segments':[{'material_id':'t1', 'font_selection':choice,
                        'target_timerange':{'start':0,'duration':1_000_000}}]}]}
                with tempfile.TemporaryDirectory() as raw:
                    before = copy.deepcopy(snapshot)
                    images = _materialize_subtitle_images(snapshot, Path(raw))
                    self.assertEqual(images[0]['text'], text)
                    self.assertTrue(Path(images[0]['path']).is_file())
                    self.assertEqual(snapshot, before)

    def test_japanese_font_remains_first_choice(self):
        choice = fonts().select('猫のくつろぐ場所', 'ja')
        self.assertIn('ja', fonts().families[choice['font_id']]['languages'])
        self.assertFalse(choice['locale_fallback'])

    def test_unrenderable_character_still_fails_with_diagnostic(self):
        with self.assertRaisesRegex(ValueError, '缺少字符：U\\+10FFFF'):
            fonts().select('猫\U0010ffff', 'ja')

    def test_fallback_still_checks_font_hash(self):
        with patch.object(fonts(), '_path', side_effect=ValueError('Font checksum mismatch')):
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                fonts().select('猫咪自己钻进去了', 'ja')


class ChineseFailureMessagesTests(unittest.TestCase):
    def test_historical_diagnostics_have_chinese_reason(self):
        cases = {
            'No single bundled font covers this text and locale; revise symbols or add a font':'字体',
            'Runtime differs from the saved task; do not resume with different tools':'版本',
            'Artifact path or content changed':'产物',
            'authentication: Provider HTTP status 401: access_denied':'访问码',
            'timeout: Provider timeout during response_headers':'超时',
            'daily_allowance_exhausted':'今日额度',
            'Each retained speech clip must be 1..10 seconds':'口播',
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                result = Workbench.safe_error(None, value)
                self.assertIn(expected, result)
                self.assertNotIn(value, result)
                self.assertEqual(Workbench.safe_error(None, result), result)

    def test_uncertain_request_does_not_invite_duplicate_creation(self):
        result = chinese_diagnostic('Provider timeout during connect_or_response_headers; outcome may be uncertain. Request was not retried.')
        self.assertIn('不要重复创建任务', result)

    def test_unknown_english_is_not_guessed_or_exposed_and_credentials_redacted(self):
        text = 'Unexpected gizmo failed violently'
        result = Workbench.safe_error(None, text)
        self.assertIn('暂未分类', result)
        self.assertIn('诊断编号', result)
        self.assertNotIn(text, result)
        token = 'jyg_'+'a'*43
        result = Workbench.safe_error(None, 'invalid '+token)
        self.assertNotIn(token, result)
        self.assertIn('隐藏', result)

    def test_existing_chinese_and_counts_retained(self):
        text = '口播待修订：整条口播总量过长：165 characters，最多160 characters'
        result = chinese_diagnostic(text)
        self.assertIn('165 字符', result)
        self.assertIn('160 字符', result)
        self.assertNotIn('characters', result)


if __name__ == '__main__':
    unittest.main()
