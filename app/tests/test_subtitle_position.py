import copy
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from tests import test_subtitle_styles as base
from agent_core.subtitle_styles import parse_position_instruction, normalize_style, resolve_style
from agent_core.independent_export import _materialize_subtitle_images
from agent_core.caption_policy import acceptance
from agent_core.editing_rules import apply_rules
from tests import test_editing_rules as editing_fixture

class SubtitlePositionTests(unittest.TestCase):
    snapshot=base.SubtitleStyleTests.snapshot

    def test_nine_locations_and_short_words(self):
        cases={'上方':'top_center','下方':'bottom_center','中间':'middle_center',
               '左侧':'middle_left','右侧':'middle_right','左上角':'top_left',
               '右上':'top_right','左下':'bottom_left','右下角':'bottom_right',
               '上':'top_center','下':'bottom_center','左':'middle_left','右':'middle_right',
               '画面中间偏左':'middle_left','顶部居中':'top_center','右侧居中':'middle_right',
               '上方靠右':'top_right','右下方':'bottom_right'}
        for words,want in cases.items():
            with self.subTest(words=words):
                self.assertEqual(parse_position_instruction('字幕放在'+words)['values'],{'position':want})

    def test_scope_negation_corrections_and_continuation(self):
        for text in ('人物放在右侧，镜头在上方','字幕用白色，商品放在中间',
                     '不要把字幕放上方','字幕不要放在右侧','保留原字幕在下方',
                     '字幕上方的人物保持清晰'):
            with self.subTest(text=text):self.assertEqual(parse_position_instruction(text)['values'],{})
        for text in ('字幕不要放在上方，要放下方','字幕放上方，最后改到下方'):
            self.assertEqual(parse_position_instruction(text)['values']['position'],'bottom_center')
        self.assertEqual(parse_position_instruction('字幕放在上方，靠左')['values']['position'],'top_left')
        self.assertEqual(parse_position_instruction('字幕放在上方，字幕放在右侧')['values']['position'],'middle_right')
        self.assertEqual(parse_position_instruction('subtitles at bottom right')['values']['position'],'bottom_right')

    def test_invalid_positions_and_time_scoped_requests_are_explicit(self):
        with self.assertRaisesRegex(ValueError,'字幕位置'):normalize_style({'position':'random'})
        self.assertEqual(parse_position_instruction('开头字幕在上方，结尾字幕在下方')['values'],{})
        from agent_core.caption_layout import schedule_from_task
        rows=schedule_from_task({'instruction':'开头字幕在上方，结尾字幕在下方'},10_000_000)
        self.assertEqual([(r['start_us'],r['end_us'],r['style']['position']) for r in rows],
                         [(0,3_000_000,'top_center'),(7_000_000,10_000_000,'bottom_center')])

    def test_user_request_wins_over_model_and_upper_narration_default(self):
        cue={'subtitle_style':{'position':'top_right'}}
        original=copy.deepcopy(cue)
        style,evidence=resolve_style({'instruction':'字幕放在画面下方'},cue)
        self.assertEqual(style['position'],'bottom_center')
        self.assertEqual(cue,original);self.assertIn('下方',evidence['position'])
        snap=self.snapshot('字幕在下方');snap['subtitle_placement']='upper'
        with tempfile.TemporaryDirectory() as tmp:
            row=_materialize_subtitle_images(snap,Path(tmp),font_path=base.FONT)[0]
            self.assertGreater(row['bounds'][1],snap['canvas_config']['height']/2)
            self.assertEqual(row['position'],'bottom_center')

    def test_actual_png_bounds_for_nine_positions_land_in_requested_regions(self):
        for name in ('上方','下方','中间','左侧','右侧','左上角','右上角','左下角','右下角'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                snap=self.snapshot('字幕放在'+name+'，字幕用黄色，不要底板')
                row=_materialize_subtitle_images(snap,Path(tmp),font_path=base.FONT)[0]
                image=Image.open(row['path']).convert('RGBA')
                left,top,right,bottom=image.getchannel('A').getbbox()
                width,height=image.size;v,h=row['position'].split('_')
                self.assertTrue(0<=left<right<=width and 0<=top<bottom<=height)
                if v=='top':self.assertLess(bottom,height/3)
                elif v=='bottom':self.assertGreater(top,height*2/3)
                else:self.assertAlmostEqual((top+bottom)/2,height/2,delta=3)
                if h=='left':self.assertLess((left+right)/2,width/2)
                elif h=='right':self.assertGreater((left+right)/2,width/2)
                else:self.assertAlmostEqual((left+right)/2,width/2,delta=3)

    def test_acceptance_rejects_position_that_disagrees_with_user(self):
        task={'instruction':'字幕放左下角'}
        snap=self.snapshot(task['instruction'])
        self.assertEqual(acceptance(task,snap,{})['status'],'passed')
        snap['materials']['texts'][0]['subtitle_style']['position']='top_right'
        report=acceptance(task,snap,{})
        self.assertEqual(report['status'],'failed')
        self.assertTrue(any(c['name']=='user_subtitle_position' and not c['passed'] for c in report['checks']))

    def test_custom_position_enables_text_without_model_override(self):
        task=editing_fixture.EditingRuleTests().spec('字幕放在上方')
        self.assertTrue(task['constraints']['subtitles'])
        self.assertIn('text',task['required_native_categories'])
        updated=apply_rules(task,{'schema_version':'1.0','overrides':{},'unsupported':[]})
        self.assertTrue(updated['resolved_editing_options']['text'])
        task['instruction']+='，不要新增字幕'
        disabled=apply_rules(task,{'schema_version':'1.0','overrides':{
            'text':{'value':False,'quote':'不要新增字幕'}},'unsupported':[]})
        self.assertFalse(disabled['constraints']['subtitles'])

    def test_position_request_cannot_pass_with_missing_text(self):
        task={'instruction':'字幕放在右侧'}
        snap=self.snapshot(task['instruction']);snap['materials']['texts']=[]
        self.assertEqual(acceptance(task,snap,{})['status'],'failed')
        task['resolved_editing_options']={'text':False}
        self.assertEqual(acceptance(task,snap,{})['status'],'passed')

    def test_srt_also_uses_requested_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);srt=root/'test.srt'
            srt.write_text('1\n00:00:00,000 --> 00:00:01,000\nTest captions\n',encoding='utf-8')
            snap=self.snapshot('字幕放在右上角');snap['subtitle_path']=str(srt)
            row=_materialize_subtitle_images(snap,root,font_path=base.FONT)[0]
            self.assertEqual(row['position'],'top_right')
