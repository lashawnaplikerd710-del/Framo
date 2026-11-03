import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'toolchain/scripts'))
from native_keyframes import add_point, validate_added_point
from native_motion import checked_request
from native_service import NativeServiceError
import native_build


def snapshot():
    return {'duration':3_000_000,'tracks':[{'type':'video','segments':[{
        'id':'clip','target_timerange':{'start':0,'duration':3_000_000},
        'source_timerange':{'start':0,'duration':3_000_000},'speed':1,
        'common_keyframes':[],'uniform_scale':{'on':True,'value':1},
        'clip':{'scale':{'x':1,'y':1}},'material_id':'user-video'}]},
        {'type':'text','segments':[{'id':'caption','content':'must survive'}]}],
        'materials':{'videos':[{'id':'user-video','path':'user.mp4','type':'video'}]}}


class NativeMotionTests(unittest.TestCase):
    def test_trim_and_constant_speed_map_target_time_to_source_time(self):
        for speed in (1,2,.5):
            data=snapshot();seg=data['tracks'][0]['segments'][0]
            seg['source_timerange']={'start':2_000_000,'duration':round(3_000_000*speed)}
            seg['speed']=speed
            after=add_point(data,'clip','position_x',1_500_000,.3)
            point=after['tracks'][0]['segments'][0]['common_keyframes'][0]['keyframe_list'][0]
            self.assertEqual(point['time_offset'],2_000_000+round(1_500_000*speed))
            validate_added_point(data,after,'clip','position_x',1_500_000,.3)

    def test_alpha_uses_the_same_trim_mapping_and_still_images_remain_target_relative(self):
        data=snapshot();seg=data['tracks'][0]['segments'][0]
        seg['source_timerange']['start']=2_000_000
        after=native_build.alpha_keyframe_snapshot(data,'clip',1_500_000,.5)
        point=after['tracks'][0]['segments'][0]['common_keyframes'][0]['keyframe_list'][0]
        self.assertEqual(point['time_offset'],3_500_000)
        native_build.validate_property_change(data,after,{'action':'keyframe.add','time_us':1_500_000,'value':.5},'clip')
        photo=snapshot();photo['materials']['videos'][0]['type']='photo'
        photo['tracks'][0]['segments'][0]['source_timerange']=None
        after=add_point(photo,'clip','scale',1_500_000,1.1)
        self.assertEqual(after['tracks'][0]['segments'][0]['common_keyframes'][0]['keyframe_list'][0]['time_offset'],1_500_000)

    def test_reverse_loop_and_curve_speed_fail_before_writing(self):
        for mode in ('reverse','is_loop','curve_speed','wrong_span'):
            data=snapshot();seg=data['tracks'][0]['segments'][0]
            if mode in ('reverse','is_loop'):seg[mode]=True
            elif mode=='wrong_span':seg['source_timerange']['duration']=2_000_000
            else:
                seg['extra_material_refs']=['speed']
                data['materials']['speeds']=[{'id':'speed','speed':1,'curve_speed':{'points':[]}}]
            with self.assertRaises(NativeServiceError):add_point(data,'clip','position_x',0,.1)
    def test_independent_curves_at_same_time_preserve_tracks_and_source(self):
        original=snapshot();before=copy.deepcopy(original)
        for prop,value in [('position_x',.2),('position_y',-.1),('rotation',10),('scale',1.1)]:
            after=add_point(before,'clip',prop,500000,value)
            validate_added_point(before,after,'clip',prop,500000,value)
            self.assertEqual(after['tracks'][1],original['tracks'][1])
            self.assertEqual(after['materials'],original['materials'])
            before=after
        self.assertEqual(original,snapshot())
        self.assertEqual(len(before['tracks'][0]['segments'][0]['common_keyframes']),4)

    def test_unverified_ignored_properties_and_invalid_values_rejected(self):
        for prop,time,value in [('scale_y',0,1.1),('brightness',0,.3),('rotation',0,float('nan')),
                                 ('scale',0,0),('scale',True,1),('position_x',3_000_001,.2)]:
            with self.subTest(prop=prop,time=time,value=value),self.assertRaises(ValueError):
                add_point(snapshot(),'clip',prop,time,value)

    def test_nonuniform_scaling_and_existing_y_curve_rejected(self):
        for change in ('off','unequal','y_curve'):
            data=snapshot();seg=data['tracks'][0]['segments'][0]
            if change=='off':seg['uniform_scale']['on']=False
            elif change=='unequal':seg['clip']['scale']['y']=.9
            else:seg['common_keyframes']=[{'property_type':'KFTypeScaleY','keyframe_list':[]}]
            with self.assertRaises(NativeServiceError):add_point(data,'clip','scale',0,1.1)

    def test_duplicate_points_and_accidental_other_edits_fail(self):
        data=snapshot();after=add_point(data,'clip','rotation',0,10)
        with self.assertRaises(NativeServiceError):add_point(after,'clip','rotation',0,20)
        changed=copy.deepcopy(after);changed['tracks'][1]['segments'][0]['content']='accidental edit'
        with self.assertRaises(NativeServiceError):validate_added_point(data,changed,'clip','rotation',0,10)
        changed=copy.deepcopy(after);changed['tracks'][0]['segments'][0]['common_keyframes'][0]['material_id']='external-effect'
        with self.assertRaises(NativeServiceError):validate_added_point(data,changed,'clip','rotation',0,10)

    def test_target_must_be_video_and_points_strictly_ordered(self):
        request={'schema':'jianying-native-motion-v1','curves':[{'segment_id':'clip','property':'scale',
                    'points':[{'time_us':0,'value':1},{'time_us':2_000_000,'value':1.1}]}]}
        self.assertEqual(checked_request(request,snapshot()),request)
        for target in ('caption','missing'):
            bad=copy.deepcopy(request);bad['curves'][0]['segment_id']=target
            with self.assertRaises(ValueError):checked_request(bad,snapshot())
        bad=copy.deepcopy(request);bad['curves'][0]['points'].reverse()
        with self.assertRaises(ValueError):checked_request(bad,snapshot())
        bad=copy.deepcopy(request);bad['curves'].append(bad['curves'][0])
        with self.assertRaises(ValueError):checked_request(bad,snapshot())

    def test_native_build_plan_motion_can_coexist_with_alpha(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'input.mp4';source.write_bytes(b'fixture')
            data={'schema':'jianying-native-plan-v1','name':'test',
                  'canvas':{'width':1080,'height':1920,'fps':30},'operations':[
                    {'id':'v','action':'video.add','params':{'path':str(source),'duration':3_000_000,
                     'sourceDuration':3_000_000,'sourceStartTime':0,'startTime':0,
                     'trackIndex':0,'needInsertTargetTrack':True}}],
                  'edits':[{'action':'keyframe.add','segment':'v','time_us':0,'value':1},
                           {'action':'keyframe.motion','segment':'v','property':'scale','time_us':0,'value':1.1}]}
            path=root/'plan.json';path.write_text(json.dumps(data),encoding='utf-8')
            info={'has_video':True,'width':1080,'height':1920,'is_image':False,'duration':3,'video_duration':3}
            with patch.object(native_build,'media_info',return_value=info):
                self.assertEqual(len(native_build.checked_plan(path)['edits']),2)
                data['edits'].append(data['edits'][-1]);path.write_text(json.dumps(data),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'Duplicate'):native_build.checked_plan(path)
