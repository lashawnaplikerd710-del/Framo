import hashlib,json,tempfile,unittest
from PIL import Image
from agent_core.auto_decorations import ROOT,plan_stickers
from agent_core.original_montage import POLICY

class DomesticStickerTests(unittest.TestCase):
    def test_downloaded_designs_have_unique_real_large_transparency_and_licenses(self):
        root=ROOT/'sticker-library'
        rows=[r for r in json.loads((root/'manifest.json').read_text(encoding='utf-8'))['stickers']
              if r.get('collection')=='iconpark_cn_commerce']
        self.assertGreaterEqual(len(rows),30)
        self.assertEqual(len(rows),len({r['sha256'] for r in rows}))
        for row in rows:
            self.assertFalse(row['ai_generated']);self.assertEqual(row['license'],'Apache-2.0')
            self.assertTrue((root/row['license_file']).is_file())
            self.assertTrue((root/row['source_svg']).is_file())
            self.assertEqual(hashlib.sha256((root/row['file']).read_bytes()).hexdigest(),row['sha256'])
            with Image.open(root/row['file']) as im:
                self.assertEqual(im.size,(1024,1024));self.assertEqual(im.mode,'RGBA')
                self.assertEqual(im.getchannel('A').getextrema(),(0,255))

    def test_domestic_commerce_uses_large_downloaded_assets_without_affecting_japanese(self):
        timeline={'duration_us':20000000,'source_map':[{'timeline_start_us':0,'timeline_end_us':10000000},
                   {'timeline_start_us':10000000,'timeline_end_us':20000000}]}
        with tempfile.TemporaryDirectory() as raw:
            rows,report=plan_stickers({'instruction':'国内电商带货'},timeline,{'width':1080,'height':1920},raw,[])
            self.assertEqual(len(rows),2)
            for row in rows:
                self.assertEqual(row['collection'],'premium_commerce_3d_v1')
                with Image.open(row['path']) as im:self.assertEqual(im.size,(259,259))
                self.assertLessEqual(row['x']+259,1080)
            rows,_=plan_stickers({'instruction':'日本电商带货','target_country':'日本'},timeline,{'width':1080,'height':1920},raw,[])
            self.assertTrue(all(row['style']=='3d' for row in rows))

    def test_original_montage_can_select_large_domestic_assets(self):
        task={'instruction':'电商带货长视频','original_montage_policy':POLICY}
        timeline={'duration_us':10000000,'source_map':[{'timeline_start_us':0,'timeline_end_us':10000000}]}
        with tempfile.TemporaryDirectory() as raw:
            rows,_=plan_stickers(task,timeline,{'width':540,'height':960},raw,[])
            self.assertTrue(rows);self.assertEqual(rows[0]['collection'],'premium_commerce_3d_v1')
            with Image.open(rows[0]['path']) as im:self.assertEqual(im.size,(130,130))
            self.assertLessEqual(rows[0]['x']+130,540)

    def test_three_minute_original_montage_can_use_one_hundred_combined_events(self):
        task={'instruction':'电商带货长视频，大量贴纸光点','original_montage_policy':POLICY}
        timeline={'duration_us':180_000_000,'source_map':[
            {'timeline_start_us':i*2_000_000,'timeline_end_us':(i+1)*2_000_000} for i in range(90)]}
        with tempfile.TemporaryDirectory() as raw:
            rows,report=plan_stickers(task,timeline,{'width':540,'height':960},raw,[])
            self.assertEqual(report['max_per_video'],100)
            self.assertEqual(len(rows),100)
            self.assertTrue(any(r['id']=='original_clean_glint' for r in rows))
            self.assertTrue(any(r.get('style')=='premium_3d' for r in rows))
            self.assertTrue(all(0<=r['start_us']<r['end_us']<=timeline['duration_us'] for r in rows))

if __name__=='__main__':unittest.main()
