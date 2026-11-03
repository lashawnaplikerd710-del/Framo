import copy
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from agent_core.caption_layout import schedule_from_task, expand_cues, global_layout
from agent_core.caption_policy import acceptance
from agent_core.subtitle_styles import normalize_style, parse_color_instruction
from agent_core.independent_export import _materialize_subtitle_images, _subtitle_rows
from tests.test_subtitle_styles import SubtitleStyleTests,FONT


class CaptionLayoutTests(unittest.TestCase):
    def test_user_schedule_splits_existing_text_without_losing_duration(self):
        schedule=schedule_from_task({'instruction':'前2秒字幕上方居中，2-4秒字幕下方两行，4秒后字幕居中'},6_000_000)
        cue={'start_us':0,'end_us':6_000_000,'text':'これは猫のためのテントです','caption_motion':{'entry':'assemble','loop':'float'}}
        original=copy.deepcopy(cue)
        parts=expand_cues([cue],schedule)
        self.assertEqual(cue,original)
        self.assertEqual([(p['start_us'],p['end_us']) for p in parts],[(0,2_000_000),(2_000_000,4_000_000),(4_000_000,6_000_000)])
        self.assertEqual([p['subtitle_style']['position'] for p in parts],['top_center','bottom_center','middle_center'])
        self.assertEqual([p['caption_motion']['entry'] for p in parts],['assemble','none','none'])
        self.assertTrue(all(p['caption_origin_start_us']==0 for p in parts))

    def test_continued_time_clause_and_later_override(self):
        rows=schedule_from_task({'instruction':'前5秒字幕上方，2-4秒下方；人物站在右边'},8_000_000)
        parts=expand_cues([{'start_us':0,'end_us':8_000_000,'text':'位置'}],rows)
        self.assertEqual([(p['start_us'],p['subtitle_style'].get('position')) for p in parts],
                         [(0,'top_center'),(2_000_000,'bottom_center'),(4_000_000,'top_center'),(5_000_000,None)])

    def test_real_pngs_land_at_requested_percentages_and_two_lines(self):
        from agent_core.direct_renderer import snapshot_from_timeline
        task={'instruction':'前2秒字幕横向50%纵向25%，2-4秒字幕横向50%纵向70%两行；字幕不要动效',
              'target':{'fps':30,'aspect_ratio':'source'}}
        timeline={'duration_us':4_000_000,'captions':[{'start_us':0,'end_us':4_000_000,'text':'日本語ではなく位置を確認するテストです'}],
                  'source_map':[{'asset_id':'a','source_start_us':0,'source_end_us':4_000_000,'timeline_start_us':0,'timeline_end_us':4_000_000}]}
        snapshot=snapshot_from_timeline(task,[{'asset_id':'a','path':str(FONT),'width':720,'height':1280,'has_audio':False}],timeline)
        self.assertEqual(acceptance(task,snapshot,{})['status'],'passed')
        with tempfile.TemporaryDirectory() as tmp:
            rows=_materialize_subtitle_images(snapshot,Path(tmp),font_path=FONT)
            self.assertEqual(len(rows),2)
            self.assertAlmostEqual(sum(rows[0]['bounds'][1::2])/2,320,delta=1)
            self.assertAlmostEqual(sum(rows[1]['bounds'][1::2])/2,896,delta=1)
            self.assertEqual(len(rows[1]['lines']),2)
            for row in rows:
                self.assertIsNotNone(Image.open(row['path']).getbbox())
        damaged=copy.deepcopy(snapshot)
        damaged['materials']['texts'][1]['subtitle_style']['y']=.2
        self.assertEqual(acceptance(task,damaged,{})['status'],'failed')

    def test_timed_colors_do_not_leak_to_whole_video(self):
        instruction='前2秒字幕用红色，2-4秒字幕用黄色；字幕放中间'
        self.assertNotIn('fill_color',parse_color_instruction(instruction)['values'])
        rows=schedule_from_task({'instruction':instruction},5_000_000)
        self.assertEqual([r['style']['fill_color'] for r in rows],['#FF0000','#FFFF00'])

    def test_global_request_survives_outside_schedule(self):
        from agent_core.direct_renderer import snapshot_from_timeline
        task={'instruction':'字幕放下方，前1秒字幕放上方','target':{'fps':30,'aspect_ratio':'source'}}
        timeline={'duration_us':2_000_000,'captions':[{'start_us':0,'end_us':2_000_000,'text':'Test'}],
                  'source_map':[{'asset_id':'a','source_start_us':0,'source_end_us':2_000_000,'timeline_start_us':0,'timeline_end_us':2_000_000}]}
        snap=snapshot_from_timeline(task,[{'asset_id':'a','path':str(FONT),'width':720,'height':1280}],timeline)
        self.assertEqual([r[4]['position'] for r in _subtitle_rows(snap,with_styles=True)],['top_center','bottom_center'])
        self.assertEqual(acceptance(task,snap,{})['status'],'passed')

    def test_relative_ranges_and_non_caption_scope(self):
        rows=schedule_from_task({'instruction':'前半段字幕上方，后半段字幕下方'},7_000_000)
        self.assertEqual([(r['start_us'],r['end_us']) for r in rows],[(0,3_500_000),(3_500_000,7_000_000)])
        self.assertEqual(schedule_from_task({'instruction':'前2秒人物在右边，后2秒镜头上移'},6_000_000),[])

    def test_invalid_coordinates_and_ranges_are_chinese(self):
        for style in ({'x':float('nan')},{'y':1.2},{'max_lines':0},{'font_scale':3},{'wrap_width':.1}):
            with self.assertRaisesRegex(ValueError,'字幕'):normalize_style(style)
        with self.assertRaisesRegex(ValueError,'结束时间'):
            schedule_from_task({'instruction':'5-2秒字幕放上方'},8_000_000)

    def test_comma_separated_coordinate_continuation_does_not_leak_to_subject(self):
        self.assertEqual(global_layout('字幕横向50%，纵向20%，人物在左边'), {'x':.5,'y':.2})
        self.assertEqual(global_layout('前5秒字幕横向50%，人物左边，纵向20%'), {})

    def test_balanced_japanese_lines_do_not_start_with_closing_punctuation(self):
        snapshot=SubtitleStyleTests().snapshot('字幕白色，字幕不要动画')
        text='ねこちゃんの、ひみつ基地'
        for track in snapshot['tracks']:
            if track['type']=='text':
                for segment in track['segments']:
                    segment['text']=text
                    segment['subtitle_style']={'line_count':2,'max_lines':2}
        with tempfile.TemporaryDirectory() as tmp:
            rows=_materialize_subtitle_images(snapshot,Path(tmp),font_path=FONT)
            for row in rows:
                self.assertEqual(len(row['lines']),2)
                self.assertEqual(''.join(row['lines']),text)
                self.assertTrue(all(not line.startswith('、') for line in row['lines']))


if __name__=='__main__':unittest.main()
