"""Explicit native integration fixture, never presented as a model-edited deliverable."""
import json
from pathlib import Path
import sys
import uuid
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'toolchain/scripts'))
from agent_core.packaging import CATEGORIES
from native_packaging import package
from native_preview import preview
from draft_quality import audit
from agent_core.quality_controller import check_native_draft

old=ROOT/'data/jobs/job_f7efd3dd259e42378d6799769306950b/revisions/005'
timeline=json.loads((old/'timeline.json').read_text(encoding='utf-8'))
plan={'schema_version':'1.0','decisions':{k:{'status':'applied','reason':'原生适配器集成测试'} for k in CATEGORIES},
    'music':{'theme':'light','volume':.15},
    'stickers':[{'clip_index':1,'icon':'sparkle','anchor':'upper_left','offset_ms':300,'duration_ms':1000,'reason':'测试'}],
    'sfx':[{'clip_index':2,'kind':'tick','offset_ms':300,'volume':.2,'reason':'测试'}],
    'transitions':[{'before_clip':2,'duration_frames':6,'reason':'测试'}]}
plan['decisions']['captions']={'status':'not_needed','reason':'已有烧录字幕'}
out=ROOT.parent/'work'/('editable-layers-native-smoke-'+uuid.uuid4().hex[:8]);out.mkdir()
req=out/'request.json';req.write_text(json.dumps({'schema':'jianying-editable-packaging-v1','plan':plan,'timeline':timeline},ensure_ascii=False),encoding='utf-8')
print(str(out),flush=True)
result=package('D:/JianyingPro/11.5.0.14471',old/'packaged/draft',req,out/'layers')
snapshot=json.loads((out/'layers/draft-snapshot.json').read_text(encoding='utf-8'))
struct=audit(snapshot,base_dir=out/'layers',primary_track=result['original_tracks'][0]['id'])
qc=check_native_draft(snapshot,timeline,struct,editable_captions=True,packaging_report=result)
(out/'qc.json').write_text(json.dumps(qc,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'tracks':result['tracks'],'issues':qc['issues']},ensure_ascii=False),flush=True)
boundary=timeline['source_map'][1]['timeline_start_us']
rendered=preview('D:/JianyingPro/11.5.0.14471',out/'layers/draft',out/'preview',[800000,boundary,boundary+100000,boundary+233333],540)
control=preview('D:/JianyingPro/11.5.0.14471',old/'packaged/draft',out/'control',[800000,boundary+100000,boundary+233333],540)
changes={'sticker_changes_pixels':rendered['frames'][0]['sha256']!=control['frames'][0]['sha256'],
         'transition_changes_pixels':rendered['frames'][2]['sha256']!=control['frames'][1]['sha256'],
         'after_transition_matches':rendered['frames'][3]['sha256']==control['frames'][2]['sha256']}
(out/'render-contrast.json').write_text(json.dumps(changes,indent=2),encoding='utf-8')
assert all(changes.values()),changes
