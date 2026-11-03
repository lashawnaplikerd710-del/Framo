import copy
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from agent_core.subtitle_styles import parse_color_instruction, resolve_style
from agent_core.direct_renderer import snapshot_from_timeline
from agent_core.independent_export import _materialize_subtitle_images, _subtitle_rows

FONT=Path(__file__).resolve().parents[1]/'desktop-assets/fonts/LXGW975YuanSC-400W.ttf'

class SubtitleStyleTests(unittest.TestCase):
    def test_reported_instruction_overrides_every_seed_and_planned_style(self):
        instruction='去除原声，字幕使用深灰色，不要加字幕背景，需要添加贴纸，和转场'
        for index in range(64):
            style,evidence=resolve_style({'instruction':instruction,'subtitle_style_seed':f'job_{index}'},
                {'subtitle_style':{'background_color':'#000000A5','fill_color':'#FFFFFF'}})
            self.assertEqual(style['background_color'],'#00000000')
            self.assertEqual(style['fill_color'],'#404040')
            self.assertEqual(evidence['background_color'],'不要加字幕背景')
        snapshot=self.snapshot(instruction,{'background_color':'#000000A5'})
        from agent_core.caption_policy import acceptance
        report=acceptance({'instruction':instruction},snapshot,{})
        checks={c['name']:c['passed'] for c in report['checks']}
        self.assertTrue(checks['user_subtitle_background_color'])
        self.assertTrue(checks['user_subtitle_fill_color'])
        snapshot['materials']['texts'][0]['subtitle_style']['background_color']='#000000A5'
        self.assertEqual(acceptance({'instruction':instruction},snapshot,{})['status'],'failed')
        with tempfile.TemporaryDirectory() as tmp:
            rows=_materialize_subtitle_images(self.snapshot(instruction),Path(tmp),font_path=FONT)
            img=Image.open(rows[0]['path']).convert('RGBA')
            pixels=set(img.getdata())
            self.assertIn((64,64,64,255),pixels)
            self.assertTrue(any(p[3]==0 for p in pixels))
            left,top,right,bottom=rows[0]['bounds']
            # Outline antialiasing can also contain black at alpha 165.
            # Backplate padding, outside the glyphs, must be transparent.
            self.assertEqual(img.getpixel(((left+right)//2,top+2))[3],0)
            self.assertEqual(img.getpixel(((left+right)//2,bottom-2))[3],0)
            self.assertEqual(img.getpixel((left+2,(top+bottom)//2))[3],0)

    def test_natural_background_removal_and_scope(self):
        for instruction in ('不要添加字幕背景','不需要添加字幕背景','无需加字幕底板',
                            '字幕背景不要加','不要加花字的背景','字幕背景透明',
                            '字幕不要加背景','不添加字幕背景','不要再加字幕背景','字幕不要有背景'):
            with self.subTest(instruction=instruction):
                self.assertEqual(parse_color_instruction(instruction)['values']['background_color'],'#00000000')
        for instruction in ('不要添加背景音乐','不要背景','不要移除字幕背景',
                            '字幕背景不要黑色','字幕背景不要去掉','不要把字幕背景去掉'):
            self.assertNotIn('background_color',parse_color_instruction(instruction)['values'])
        self.assertEqual(parse_color_instruction('不要加字幕背景，字幕背景用蓝色')['values']['background_color'],'#0000FF')
        self.assertEqual(parse_color_instruction('字幕背景用蓝色，不要添加字幕背景')['values']['background_color'],'#00000000')

    def snapshot(self,instruction='',cue_style=None):
        # Color/placement pixel tests inspect one static image; automatic
        # caption motion is exercised by the dedicated motion tests.
        task={'instruction':instruction,'target':{'fps':30,'aspect_ratio':'source'},
              'caption_motion':{'entry':'none','loop':'none'}}
        cue={'start_us':0,'end_us':1_000_000,'text':'字幕颜色测试'}
        if cue_style:cue['subtitle_style']=cue_style
        timeline={'duration_us':1_000_000,'captions':[cue],'source_map':[
            {'asset_id':'a','source_start_us':0,'source_end_us':1_000_000,
             'timeline_start_us':0,'timeline_end_us':1_000_000}]}
        return snapshot_from_timeline(task,[{'asset_id':'a','path':str(FONT),'width':720,
             'height':1280,'has_audio':False}],timeline)

    def test_fill_outline_and_background_are_independent(self):
        result=parse_color_instruction('字幕用黄色，黑色描边，底板为深蓝色')['values']
        self.assertEqual(result,{'fill_color':'#FFFF00','stroke_color':'#000000','background_color':'#163B70'})

    def test_hex_and_transparent_background(self):
        result=parse_color_instruction('字幕颜色改成 #FF69B4，描边色 #163B70，底板色 #00000080')['values']
        self.assertEqual(result,{'fill_color':'#FF69B4','stroke_color':'#163B70','background_color':'#00000080'})
        self.assertEqual(parse_color_instruction('粉色字幕，不要底板')['values']['background_color'],'#00000000')

    def test_instruction_only_changes_subtitle_not_product_or_frame(self):
        self.assertEqual(parse_color_instruction('红色衣服，背景是蓝色，画面更明亮')['values'],{})
        self.assertNotIn('fill_color',parse_color_instruction('字幕背景为蓝色')['values'])
        self.assertEqual(parse_color_instruction('字幕背景为蓝色')['values']['background_color'],'#0000FF')

    def test_negated_color_does_not_apply(self):
        self.assertEqual(parse_color_instruction('不要红色字幕')['values'],{})
        self.assertEqual(parse_color_instruction('不要把字幕改成红色')['values'],{})
        self.assertEqual(parse_color_instruction('不要红色字幕，字幕改为黄色')['values']['fill_color'],'#FFFF00')

    def test_later_explicit_request_wins(self):
        self.assertEqual(parse_color_instruction('黄色字幕，最后字幕改为粉色')['values']['fill_color'],'#FF69B4')

    def test_english_solid_color(self):
        self.assertEqual(parse_color_instruction('text color: yellow')['values']['fill_color'],'#FFFF00')

    def test_background_varies_per_video_but_is_stable_within_video(self):
        choices=[]
        for index in range(64):
            task={'subtitle_style_seed':f'job_{index}'}
            first=resolve_style(task)[0]['background_color']
            self.assertEqual(resolve_style(task,{'text':'different caption'})[0]['background_color'],first)
            choices.append(first)
        self.assertEqual(set(choices),{'#00000000','#000000A5'})
        self.assertGreater(choices.count('#00000000'),choices.count('#000000A5'))

    def test_explicit_background_wins_over_automatic_choice(self):
        for index in range(16):
            task={'subtitle_style_seed':f'job_{index}','instruction':'不要底板'}
            self.assertEqual(resolve_style(task)[0]['background_color'],'#00000000')
            task['instruction']='底板用蓝色'
            self.assertEqual(resolve_style(task)[0]['background_color'],'#0000FF')

    def test_palette_survives_but_user_color_has_priority(self):
        cue={'ad_style':{'palette':'mint'},'subtitle_style':{'fill_color':'#123456'}}
        original=copy.deepcopy(cue)
        style,evidence=resolve_style({'instruction':'字幕用黄色'},cue)
        self.assertEqual(style['fill_color'],'#FFFF00')
        self.assertEqual(evidence['fill_color'],'字幕用黄色')
        self.assertEqual(cue,original)
        self.assertEqual(resolve_style({}, {'ad_style':{'palette':'mint'}})[0]['fill_color'],'#A3FFD9')

    def test_snapshot_retains_style_and_user_evidence(self):
        snapshot=self.snapshot('字幕用黄色，不要底板',{'fill_color':'#FF0000'})
        row=_subtitle_rows(snapshot,with_styles=True)[0]
        self.assertEqual(row[4]['fill_color'],'#FFFF00')
        self.assertEqual(row[4]['background_color'],'#00000000')
        self.assertEqual(snapshot['subtitle_style_evidence']['fill_color'],'字幕用黄色')

    def test_png_has_requested_pixels_without_white_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot=self.snapshot('字幕用黄色，红色描边，底板为蓝色')
            rows=_materialize_subtitle_images(snapshot,Path(tmp),font_path=FONT)
            colors=set(Image.open(rows[0]['path']).getdata())
            self.assertIn((255,255,0,255),colors)
            self.assertIn((255,0,0,255),colors)
            self.assertIn((0,0,255,255),colors)
            self.assertNotIn((255,255,255,255),colors)

    def test_background_can_be_removed_without_removing_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows=_materialize_subtitle_images(self.snapshot('黄色字幕，不要底板，不要描边'),Path(tmp),font_path=FONT)
            colors=set(Image.open(rows[0]['path']).getdata())
            self.assertIn((255,255,0,255),colors)
            self.assertNotIn((0,0,0,165),colors)
            self.assertEqual(rows[0]['style']['stroke_width'],0)

    def test_srt_input_also_obeys_snapshot_style(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);srt=root/'captions.srt'
            srt.write_text('1\n00:00:00,000 --> 00:00:01,000\n测试\n',encoding='utf-8')
            snap=self.snapshot('字幕用粉色');snap['subtitle_path']=str(srt)
            rows=_materialize_subtitle_images(snap,root,font_path=FONT)
            self.assertEqual(rows[0]['style']['fill_color'],'#FF69B4')

if __name__=='__main__':unittest.main()
