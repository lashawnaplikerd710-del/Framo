import copy
from fractions import Fraction
import unittest

from agent_core.video_geometry import canvas_for
from agent_core.workbench import Workbench
from agent_core.desktop_workbench import default_instruction
from agent_core.montage import compile_montage
from agent_core.timeline_compiler import compile_timeline
from agent_core.direct_renderer import snapshot_from_timeline
from tests import test_montage, test_timeline_compiler


class SourceAspectTests(unittest.TestCase):
    def test_market_defaults_follow_source_but_explicit_orientation_is_preserved(self):
        for market in ('日本','美国'):
            task=Workbench._prepare_spec(None,{'workflow':'crossborder_montage',
                'target_country':market,'asset_ids':['a'],'instruction':default_instruction('crossborder_montage',market)})
            self.assertEqual(task['target']['aspect_ratio'],'source')
        for phrase,ratio in [('横屏','16:9'),('竖屏','9:16')]:
            task=Workbench._prepare_spec(None,{'workflow':'crossborder_montage',
                'target_country':'日本','asset_ids':['a'],'instruction':phrase})
            self.assertEqual(task['target']['aspect_ratio'],ratio)

    def test_canvas_keeps_nonstandard_and_odd_ratios_and_handles_orientation(self):
        for w,h in ((320,320),(360,240),(240,360),(353,571)):
            canvas=canvas_for({'aspect_ratio':'source'},{'width':w,'height':h})
            self.assertEqual(Fraction(canvas['width'],canvas['height']),Fraction(w,h))
            self.assertEqual(canvas['width']%2,0);self.assertEqual(canvas['height']%2,0)
        self.assertEqual(canvas_for({'aspect_ratio':'source'},
            {'width':320,'height':180,'rotation':90}),{'width':180,'height':320})

    def test_mixed_shapes_compile_and_preserve_source_cuts_in_direct_delivery(self):
        fixture=test_montage.MontageTests();fixture.setUp()
        task={**fixture.task,'render_backend':'direct_ffmpeg'}
        assets=copy.deepcopy(fixture.assets)
        assets[0].update(width=320,height=200);assets[1].update(width=320,height=320)
        result=compile_montage(task,assets,fixture.candidates,fixture.plan(fixture.voice[1],fixture.visual[1]))
        self.assertEqual(result['native_plan']['canvas'],{'width':320,'height':200,'fps':30})
        self.assertEqual(result['source_map'][0]['source_start_us'],fixture.voice[1]['start_us'])
        # Legacy native-project creation retains its separate strict contract.
        with self.assertRaisesRegex(Exception,'aspect ratio'):
            compile_montage(fixture.task,assets,fixture.candidates,fixture.plan(fixture.voice[1],fixture.visual[1]))

    def test_talking_head_direct_path_uses_same_source_geometry(self):
        f=test_timeline_compiler.TimelineCompilerTests();f.setUp();f.task['render_backend']='direct_ffmpeg'
        f.task['target']['aspect_ratio']='source';f.asset.update(width=320,height=320)
        result=compile_timeline(f.task,f.asset,f.transcript,f.candidates,f.plan)
        self.assertEqual(result['native_plan']['canvas'],{'width':320,'height':320,'fps':30})

    def test_source_snapshot_does_not_apply_subject_crop_to_mixed_shapes(self):
        task={'workflow':'custom','instruction':'不添加转场','target':{'fps':30,'aspect_ratio':'source'}}
        assets=[{'asset_id':'a','path':__file__,'width':320,'height':320},
                {'asset_id':'b','path':__file__,'width':180,'height':320}]
        timeline={'duration_us':2_000_000,'source_map':[
            {'asset_id':a['asset_id'],'source_segment_id':f's{i}','source_start_us':0,
             'source_end_us':1_000_000,'timeline_start_us':i*1_000_000,'timeline_end_us':(i+1)*1_000_000}
            for i,a in enumerate(assets)],'layout_tracks':[{'source_segment_id':'s1','mode':'subject_follow'}]}
        snapshot=snapshot_from_timeline(task,assets,timeline)
        self.assertEqual(snapshot['canvas_config'],{'width':320,'height':320,'fps':30})
        self.assertNotIn('layout',snapshot['tracks'][0]['segments'][1])
