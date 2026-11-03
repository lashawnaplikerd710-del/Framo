import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_core import native_packaging as p
from agent_core.native_resource_planner import validate_selection,CATALOG_SCHEMA
from agent_core.native_resource_planner import compile_selection


class CompleteEditTests(unittest.TestCase):
    def test_long_native_title_is_repositioned_without_changing_its_words(self):
        font=Path('C:/Windows/Fonts/msyh.ttc')
        if not font.is_file():self.skipTest('Windows font needed for metric regression')
        row={'key':'headline','text':'马卡龙配色 双碗结构','size':18,'x':-.5,'y':-.5}
        before=copy.deepcopy(row)
        result=p.fit_native_text(row,font,{'width':1080,'height':1920})
        self.assertEqual(row,before)
        self.assertGreaterEqual(result['size'],8)
        self.assertLess(result['size'],18)
        self.assertLess(abs(result['x']),.2)
        self.assertEqual(result['y'],-.5)

    def test_compiler_snaps_half_frames_and_does_not_extend_one_microsecond_past_end(self):
        original={'plan':{'elements':[{'key':'title','kind':'text','start_us':1916667,'duration_us':1916667},
            {'key':'music','kind':'music','start_us':0,'duration_us':28366667}]}}
        result,changes=compile_selection(original,{'duration':28366666,'fps':30})
        self.assertEqual(result['plan']['elements'][1]['duration_us'],28366666)
        self.assertEqual(result['plan']['elements'][1]['volume'],.08)
        self.assertEqual(original['plan']['elements'][1]['duration_us'],28366667)
        self.assertEqual(len(changes),2)

    def test_music_keeps_remote_id_and_sets_duration_after_add(self):
        calls=[]
        row={'key':'bgm','kind':'music','resource_id':'123','start_us':0,'duration_us':27000000,'volume':.08}
        def request(client,args,**kw):
            calls.append(args)
            return {'segment_ids':['audio']}
        nodes=[{'type':'audio','id':'audio','timeline':{'start.ms':0,'duration.ms':27000}}]
        with patch.object(p,'extract',side_effect=[[],nodes]),patch.object(p,'envelope',side_effect=request):
            p.apply_element(None,Path('C:/draft'),row,None)
        self.assertIn('vimo://123',calls[0]);self.assertNotIn('--duration',calls[0])
        self.assertEqual(calls[1][1:3],['segment','time-range'])
        self.assertIn('27000000us',calls[1])

    def test_explicit_required_categories_cannot_be_declined(self):
        row={'key':'bgm','kind':'music','resource_id':'123','start_us':0,'duration_us':6000000,'volume':.1}
        snap={'duration':6000000,'fps':30,'materials':{},'tracks':[]}
        catalog={'schema':CATALOG_SCHEMA,'status':'official_candidates_verified','purpose':'local_test',
            'candidates':[{'kind':'music','resource_id':'123','exact_query_verified':True,
                'entitlement':{'nonmember_metadata_verified':True}}]}
        result={'plan':{'schema':p.SCHEMA,'purpose':'local_test','elements':[row]},
            'decisions':{k:{'status':'applied' if k=='music' else 'not_needed','reason':'理由'} for k in p.KINDS}}
        with self.assertRaisesRegex(p.PackagingFailure,'required_native_categories_missing'):
            validate_selection(result,catalog,snap,'unused',required=['music','filter'])
        with self.assertRaisesRegex(p.PackagingFailure,'personalized_font_missing'):
            validate_selection(result,catalog,snap,'unused',require_online_font=True)

    def test_unknown_online_font_cannot_pass_catalog_validation(self):
        result={'plan':{'schema':p.SCHEMA,'purpose':'local_test','elements':[{'key':'text','kind':'text',
            'font_id':'123','text':'你好','size':12,'color':'#FFFFFF','x':0,'y':.7,'start_us':0,'duration_us':6000000}]},
            'decisions':{k:{'status':'applied' if k=='text' else 'not_needed','reason':'理由'} for k in p.KINDS}}
        catalog={'schema':CATALOG_SCHEMA,'status':'official_candidates_verified','purpose':'local_test','candidates':[]}
        with self.assertRaisesRegex(p.PackagingFailure,'unverified_font'):
            validate_selection(result,catalog,{'duration':6000000,'fps':30,'tracks':[],'materials':{}},'unused')

    def test_filter_intensity_must_be_finite_and_bounded(self):
        row={'key':'look','kind':'filter','resource_id':'123','start_us':0,'duration_us':6000000,'intensity':.2}
        plan={'schema':p.SCHEMA,'purpose':'local_test','elements':[row]}
        snap={'duration':6000000,'fps':30,'materials':{},'tracks':[]}
        p.validate_plan(plan,snap,'unused')
        for value in (float('nan'),1.1,0):
            row['intensity']=value
            with self.assertRaisesRegex(p.PackagingFailure,'intensity'):p.validate_plan(plan,snap,'unused')

    def test_official_audio_cache_is_not_a_local_import(self):
        import sys
        scripts=Path(__file__).resolve().parents[1]/'toolchain/scripts'
        sys.path.insert(0,str(scripts))
        try:
            from native_build import local_bin_entries
            snapshot={'materials':{'audios':[{'type':'music','resource_id':'123','path':'C:/not-read.mp3'},
                {'type':'sound','resource_id':'456','path':'C:/not-read2.mp3'}]}}
            self.assertEqual(local_bin_entries(snapshot),[])
        finally:sys.path.pop(0)

    def test_preview_batches_keep_all_positions_including_last(self):
        import sys,json
        scripts=Path(__file__).resolve().parents[1]/'toolchain/scripts';sys.path.insert(0,str(scripts))
        try:
            import native_preview
            def capture(install,draft,out,positions,width):
                Path(out).mkdir(parents=True)
                value={'frames':[{'requested_us':t} for t in positions]}
                (Path(out)/'preview-report.json').write_text(json.dumps(value))
                return value
            with tempfile.TemporaryDirectory() as folder,patch.object(native_preview,'preview',side_effect=capture) as call:
                result=native_preview.preview_batches('install','draft',Path(folder)/'review',list(range(43)))
                self.assertEqual([r['requested_us'] for r in result['frames']],list(range(43)))
                self.assertEqual([len(c.args[3]) for c in call.call_args_list],[20,20,3])
        finally:sys.path.pop(0)


if __name__=='__main__':unittest.main()
