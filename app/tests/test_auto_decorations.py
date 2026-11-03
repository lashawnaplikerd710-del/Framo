from pathlib import Path
import json,tempfile,unittest
from PIL import Image
from agent_core.auto_decorations import (ROOT,fonts,select_caption_font,theme_for,
    collect_visual_context,plan_stickers)
from agent_core.direct_renderer import snapshot_from_timeline
from agent_core.independent_export import _materialize_subtitle_images

class AutoDecorationsTests(unittest.TestCase):
    def context(self,subject='宠物猫',confidence='high',index=0):
        return {'subject':subject,'description':subject,'visible_features':[],'confidence':confidence,
                'requested_us':100000,'frame_sha256':'a'*64,'clip_index':index,
                'timeline_start_us':index*8_000_000,'timeline_end_us':(index+1)*8_000_000}
    def timeline(self,count=3):
        return {'duration_us':count*8_000_000,'source_map':[
            {'asset_id':'asset_a','source_start_us':i*8_000_000,'source_end_us':(i+1)*8_000_000,
             'timeline_start_us':i*8_000_000,'timeline_end_us':(i+1)*8_000_000} for i in range(count)]}

    def test_library_contains_30_added_font_families(self):
        self.assertEqual(len(fonts().families),54)
        self.assertEqual(sum(len(f['files']) for f in fonts().families.values()),67)

    def test_language_coverage_and_explicit_font_preference(self):
        choice=select_caption_font({'instruction':'字幕字体用 Poppins'}, {'text':'Comfort every day'},[])
        self.assertEqual(choice['font_id'],'poppins')
        japanese=select_caption_font({'target_locale':'ja-JP'}, {'text':'軽くて快適'},[])
        self.assertEqual(japanese['language'],'ja')
        traditional=select_caption_font({'target_locale':'zh-TW'}, {'text':'舒適生活'},[])
        self.assertEqual(traditional['language'],'zh-TW')

    def test_visual_evidence_selects_relevant_theme(self):
        self.assertEqual(theme_for([self.context('宠物猫')])[0],'pet')
        self.assertEqual(theme_for([self.context('男士衬衫穿搭')])[0],'fashion')
        self.assertEqual(theme_for([self.context('篮球运动')])[0],'fitness')
        self.assertEqual(theme_for([self.context('猫',confidence='low')])[0],'general')
        self.assertEqual(theme_for([self.context('舒适的房间')])[0],'general')

    def test_explicit_font_uses_longest_name_and_ignores_case(self):
        choice=select_caption_font({'instruction':'字幕字体用 lobster two'}, {'text':'Comfort every day'},[])
        self.assertEqual(choice['font_id'],'lobstertwo')

    def test_unknown_theme_uses_neutral_sparkles_not_product_claims(self):
        with tempfile.TemporaryDirectory() as raw:
            selected,report=plan_stickers({},self.timeline(),{'width':1080,'height':1920},raw,[])
            self.assertEqual(len(selected),1)
            self.assertTrue(selected[0]['neutral'])
            self.assertEqual(selected[0]['theme_score'],0)

    def test_user_can_disable_all_stickers(self):
        with tempfile.TemporaryDirectory() as raw:
            for task in ({'instruction':'不要贴纸'}, {'explicit_editing_overrides':{'sticker':{'value':False}}}):
                selected,report=plan_stickers(task,self.timeline(),{'width':1080,'height':1920},raw,[self.context()])
                self.assertEqual(selected,[])
                self.assertEqual(report['status'],'disabled_by_user')

    def test_stickers_match_subject_and_fit_canvas_with_bounded_duration(self):
        with tempfile.TemporaryDirectory() as raw:
            contexts=[self.context('男士衬衫',index=i) for i in range(3)]
            selected,report=plan_stickers({},self.timeline(),{'width':720,'height':1280},raw,contexts)
            self.assertGreater(len(selected),0)
            self.assertLessEqual(len(selected),3)
            for row in selected:
                self.assertEqual(row['theme'],'fashion')
                self.assertTrue(row.get('neutral') or row['design']=='fluent_t_shirt')
                with Image.open(row['path']) as image:
                    self.assertEqual(image.size,(72,72))
                    self.assertEqual(image.mode,'RGBA')
                self.assertLessEqual(row['x']+72,720)
                self.assertLessEqual(row['y']+72,1280)
                self.assertLessEqual(row['end_us']-row['start_us'],1_800_000)
                self.assertEqual(row['frame_sha256'],['a'*64])
            self.assertEqual(len({r['design'] for r in selected}),len(selected))

    def test_only_3d_stickers_are_loaded(self):
        with tempfile.TemporaryDirectory() as raw:
            selected,_=plan_stickers({'instruction':'自动匹配贴纸'},self.timeline(),{'width':720,'height':1280},
                                     raw,[self.context('手机电子产品')])
            self.assertTrue(selected)
            self.assertTrue(all(r['style']=='3d' for r in selected))
            catalog=json.loads((ROOT/'sticker-library/manifest.json').read_text(encoding='utf-8'))
            legacy=[r for r in catalog['stickers'] if r.get('style')=='3d']
            self.assertEqual(len(legacy),100)
            self.assertTrue(all(r['id'].startswith('fluent_') for r in legacy))

    def test_new_food_asset_matches_recognized_food(self):
        with tempfile.TemporaryDirectory() as raw:
            selected,_=plan_stickers({},self.timeline(1),{'width':720,'height':1280},raw,[self.context('食品披萨')])
            self.assertEqual(selected[0]['id'],'fluent_pizza')

    def test_specific_product_matches_before_generic_theme(self):
        with tempfile.TemporaryDirectory() as raw:
            selected,_=plan_stickers({},self.timeline(1),{'width':720,'height':1280},raw,[self.context('男士衬衫')])
            self.assertEqual(selected[0]['id'],'fluent_t_shirt')

    def test_selections_are_deterministic_across_retry(self):
        with tempfile.TemporaryDirectory() as raw:
            a,_=plan_stickers({},self.timeline(),{'width':720,'height':1280},raw,[self.context()])
            b,_=plan_stickers({},self.timeline(),{'width':720,'height':1280},raw,[self.context()])
            self.assertEqual(a,b)

    def test_context_only_uses_accepted_images_in_source_window(self):
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/'sources/asset_a';path.mkdir(parents=True)
            inside=self.context();outside={**inside,'requested_us':30_000_000}
            (path/'visual-observations-01.json').write_text(json.dumps({'image_input_accepted':True,
                'asset_id':'asset_a','frames':[inside,outside]}),encoding='utf-8')
            rows=collect_visual_context(self.timeline(1),raw)
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['frame_sha256'],'a'*64)

    def test_selected_font_and_stickers_survive_actual_png_generation(self):
        with tempfile.TemporaryDirectory() as raw:
            timeline=self.timeline(1);timeline['visual_style_context']=[self.context('男士衬衫')]
            timeline['captions']=[{'start_us':0,'end_us':8_000_000,'text':'轻盈舒适，自在生活'}]
            snapshot=snapshot_from_timeline({'instruction':'黄色字幕','target':{'fps':30},
                'caption_motion':{'entry':'none','loop':'none'}},[
                {'asset_id':'asset_a','path':str(ROOT),'width':720,'height':1280,'has_audio':False}],timeline,work_dir=raw)
            self.assertTrue(snapshot['materials']['texts'][0]['font_selection'])
            self.assertIn('stickers',snapshot['materials'])
            images=_materialize_subtitle_images(snapshot,Path(raw)/'subtitles')
            self.assertEqual(images[0]['style']['fill_color'],'#FFFF00')
            self.assertTrue(images[0]['font_selection']['sha256'])
            self.assertTrue(Path(images[0]['path']).is_file())

if __name__=='__main__':unittest.main()
