import hashlib,json,tempfile,unittest
from PIL import Image
from agent_core.auto_decorations import ROOT,plan_stickers
from agent_core.desktop_stickers import category_rows

class PremiumStickerTests(unittest.TestCase):
    def setUp(self):
        self.root=ROOT/'sticker-library'
        self.catalog=json.loads((self.root/'manifest.json').read_text(encoding='utf-8'))['stickers']

    def test_exactly_fifteen_distinct_original_cc0_assets_and_download_evidence(self):
        rows=category_rows(self.catalog,'高级立体')
        self.assertEqual(len(rows),15)
        self.assertEqual(len({r['design'] for r in rows}),15)
        self.assertEqual(len({r['sha256'] for r in rows}),15)
        for row in rows:
            self.assertFalse(row['ai_generated']);self.assertEqual(row['license'],'CC0-1.0')
            self.assertTrue((self.root/row['license_file']).is_file())
            self.assertIn(row['source_url'],(self.root/row['source_metadata']).read_text(encoding='utf-8'))
            self.assertEqual(hashlib.sha256((self.root/row['file']).read_bytes()).hexdigest(),row['sha256'])
            with Image.open(self.root/row['file']) as im:
                self.assertEqual(im.size,(row['width'],row['height']))
                self.assertEqual(im.mode,'RGBA');self.assertEqual(im.getchannel('A').getextrema(),(0,255))

    def test_existing_assets_remain_available_in_gallery(self):
        self.assertEqual(len(category_rows(self.catalog,'国内电商')),51)
        self.assertEqual(len(category_rows(self.catalog,'全部')),151)
        self.assertEqual(sum(r.get('collection')=='iconpark_cn_commerce' for r in self.catalog),36)
        self.assertEqual(sum(r.get('style')=='3d' for r in self.catalog),100)

    def test_capability_preflight_accepts_installed_styles_without_claiming_tracking_or_new_models(self):
        from unittest.mock import patch
        from agent_core.editing_rules import _supported_visual_selection,_visual_capability_prompt
        for label in ('高级立体电商贴纸','使用新增的高级立体电商贴纸','黑金贴纸','金属质感电商贴纸','鎏金福袋贴纸'):
            self.assertTrue(_supported_visual_selection(label),label)
        for label in ('3D模型生成','高级立体电商贴纸跟踪人脸','指定迪士尼角色贴纸','生成新3D贴纸'):
            self.assertFalse(_supported_visual_selection(label),label)
        prompt=_visual_capability_prompt()
        self.assertIn('15款高级立体电商贴纸',prompt)
        with patch('agent_core.editing_rules._available_commerce_stickers',return_value=[]):
            self.assertFalse(_supported_visual_selection('高级立体电商贴纸'))

    def test_saved_false_negative_resolution_recovers_only_verified_sticker_request(self):
        from tests.test_editing_rules import EditingRuleTests
        from agent_core.editing_rules import apply_rules
        spec=EditingRuleTests().spec('添加高级立体电商贴纸')
        result={'schema_version':'1.0','overrides':{},'unsupported':['高级立体电商贴纸']}
        updated=apply_rules(spec,result)
        self.assertTrue(updated['resolved_editing_options']['sticker'])
        result['unsupported'].append('指定迪士尼角色贴纸')
        with self.assertRaisesRegex(ValueError,'迪士尼'):apply_rules(spec,result)

    def test_preference_is_stable_and_flat_style_can_still_be_requested(self):
        timeline={'duration_us':16000000,'source_map':[{'timeline_start_us':0,'timeline_end_us':8000000},
                 {'timeline_start_us':8000000,'timeline_end_us':16000000}]}
        with tempfile.TemporaryDirectory() as raw:
            task={'instruction':'国内电商带货'};canvas={'width':1080,'height':1920}
            first,_=plan_stickers(task,timeline,canvas,raw,[])
            second,_=plan_stickers(task,timeline,canvas,raw,[])
            self.assertEqual(first,second);self.assertTrue(first)
            self.assertTrue(all(r['style']=='premium_3d' for r in first))
            flat,_=plan_stickers({'instruction':'国内电商带货，使用扁平贴纸'},timeline,canvas,raw,[])
            self.assertTrue(flat);self.assertTrue(all(r['style']=='cn_commerce' for r in flat))

    def test_high_resolution_output_does_not_upscale_the_original_sticker(self):
        timeline={'duration_us':8000000,'source_map':[{'timeline_start_us':0,'timeline_end_us':8000000}]}
        with tempfile.TemporaryDirectory() as raw:
            rows,_=plan_stickers({'instruction':'国内电商带货'},timeline,{'width':2160,'height':3840},raw,[])
            self.assertTrue(rows)
            for row in rows:
                with Image.open(row['path']) as im:self.assertLessEqual(max(im.size),400)

if __name__=='__main__':unittest.main()
