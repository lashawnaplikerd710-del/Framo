"""Regression: trimmed, sped-up second timeline clip must animate at target time."""
import copy
import json
from pathlib import Path
import sys
import uuid
from PIL import Image, ImageChops, ImageStat

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'agent-runtime/toolchain/scripts'))
from native_build import dump, alpha_keyframe_snapshot, validate_property_change
from native_motion import apply_motion
from native_preview import preview
from native_service import Service

if __name__=='__main__':
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=False)
    install=Path('D:/JianyingPro/11.5.0.14471')
    base=json.loads((ROOT/'outputs/剪映内部资源接入_20260927/font-native-test/native-text/draft-snapshot.json').read_text(encoding='utf-8'))
    reports=[]
    for speed in (1,2):
        snap=copy.deepcopy(base);snap['id']=str(uuid.uuid4()).upper()
        seg=next(t for t in snap['tracks'] if t['type']=='video')['segments'][1]
        seg['source_timerange']={'start':2_000_000,'duration':3_000_000*speed};seg['speed']=speed
        speed_material=next(m for m in snap['materials']['speeds'] if m['id'] in seg['extra_material_refs'])
        speed_material['speed']=speed
        case=out/f'speed-{speed}';case.mkdir()
        for mode in ('input','reference'):
            folder=case/mode;folder.mkdir();data=copy.deepcopy(snap)
            if mode=='reference':
                next(t for t in data['tracks'] if t['type']=='video')['segments'][1]['clip']['transform']['x']=.3
            with Service(install,case/(mode+'-service')) as service:
                service.request('draft','writeFile',{'path':str(folder/'draft_content.json'),
                    'rootPath':str(case),'content':json.dumps(data,ensure_ascii=False)})
        request=case/'motion.json'
        dump(request,{'schema':'jianying-native-motion-v1','curves':[{
             'segment_id':seg['id'],'property':'position_x','points':[
                 {'time_us':0,'value':0},{'time_us':2_500_000,'value':.5}]}]})
        result=apply_motion(install,case/'input',request,case/'animated')
        frames=preview(install,case/'reference',case/'reference-preview',[4_500_000],360)
        frame=next(f for f in result['frames'] if f['requested_us']==4_500_000)
        error=ImageStat.Stat(ImageChops.difference(Image.open(frame['path']),Image.open(frames['frames'][0]['path']))).mean[:3]
        assert max(error)<=1,(speed,error)
        saved=json.loads((case/'animated/draft-snapshot.json').read_text(encoding='utf-8'))
        segment=next(t for t in saved['tracks'] if t['type']=='video')['segments'][1]
        times=[p['time_offset'] for p in segment['common_keyframes'][0]['keyframe_list']]
        assert times==[2_000_000,2_000_000+2_500_000*speed]
        # Alpha shares this mapping; verify its exact object and native render too.
        alpha=snap
        for time,value in [(0,.2),(2_500_000,.8)]:
            updated=alpha_keyframe_snapshot(alpha,seg['id'],time,value)
            validate_property_change(alpha,updated,{'action':'keyframe.add','time_us':time,'value':value},seg['id'])
            alpha=updated
        alpharef=copy.deepcopy(snap)
        next(t for t in alpharef['tracks'] if t['type']=='video')['segments'][1]['clip']['alpha']=.56
        alpha_frames=[]
        for mode,data in [('alpha',alpha),('alpha-reference',alpharef)]:
            folder=case/mode;folder.mkdir()
            with Service(install,case/(mode+'-service')) as service:
                service.request('draft','writeFile',{'path':str(folder/'draft_content.json'),
                    'rootPath':str(case),'content':json.dumps(data,ensure_ascii=False)})
            alpha_frames.append(preview(install,folder,case/(mode+'-preview'),[4_500_000],360)['frames'][0]['path'])
        alpha_error=ImageStat.Stat(ImageChops.difference(Image.open(alpha_frames[0]),Image.open(alpha_frames[1]))).mean[:3]
        assert max(alpha_error)<=1,(speed,alpha_error)
        reports.append({'speed':speed,'source_start_us':2_000_000,'target_start_us':3_000_000,
            'target_time_us':4_500_000,'native_keyframe_source_times':times,
            'position_rgb_mae':error,'alpha_rgb_mae':alpha_error,'passed':True})
        print(json.dumps(reports[-1]),flush=True)
    dump(out/'verification.json',{'status':'native_motion_trim_speed_verified','cases':reports})
