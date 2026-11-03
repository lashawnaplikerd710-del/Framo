"""Manual native test using the prior six-second user-media/internal-font draft."""
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'agent-runtime/toolchain/scripts'))
from native_motion import apply_motion
from native_build import dump

if __name__=='__main__':
    source=ROOT/'outputs/剪映内部资源接入_20260927/font-native-test/native-text'
    snap=json.loads((source/'draft-snapshot.json').read_text(encoding='utf-8'))
    ids=[s['id'] for t in snap['tracks'] if t['type']=='video' for s in t['segments']]
    destination=Path(sys.argv[1]).resolve()
    destination.mkdir(parents=True,exist_ok=False)
    curves=[]
    for segment,prop,first,last in [(ids[0],'scale',1,1.12),(ids[0],'position_x',0,-.02),
                                   (ids[1],'rotation',-2,2),(ids[1],'position_y',.03,0)]:
        curves.append({'segment_id':segment,'property':prop,
                       'points':[{'time_us':0,'value':first},{'time_us':2_500_000,'value':last}]})
    path=destination/'request.json';dump(path,{'schema':'jianying-native-motion-v1','curves':curves})
    result=apply_motion(Path('D:/JianyingPro/11.5.0.14471'),source/'draft',path,destination/'native')
    saved=json.loads((destination/'native/draft-snapshot.json').read_text(encoding='utf-8'))
    assert [t for t in snap['tracks'] if t['type']=='text']==[t for t in saved['tracks'] if t['type']=='text']
    assert snap['materials']['texts']==saved['materials']['texts']
    assert snap['duration']==saved['duration']
    count=sum(len(g['keyframe_list']) for t in saved['tracks'] for s in t['segments'] for g in s.get('common_keyframes',[]))
    assert count==8
    verification={'status':'native_motion_sample_verified','duration_us':saved['duration'],
                  'curves':4,'points':count,'native_text_objects_preserved':True,
                  'source_preserved':result['source_preserved'],
                  'cold_reload_frames':len(result['frames']),
                  'complete_advertisement':False,'mp4_exported':False,
                  'registered_on_homepage':False,'deepseek_used':False}
    dump(destination/'verification.json',verification)
    print(json.dumps(verification,ensure_ascii=False))
