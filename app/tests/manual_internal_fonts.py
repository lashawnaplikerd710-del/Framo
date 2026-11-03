"""Real native integration test using the user's two registered bowl sources.

Tests only built-in fonts; not a complete sales edit or online catalog test.
"""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'toolchain/scripts'))
from agent_core.task_store import TaskStore, atomic_json
from native_timeline import build
from native_subtitles import add_subtitles
from native_preview import preview


def main():
    out = Path(sys.argv[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    install = Path('D:/JianyingPro/11.5.0.14471')
    store = TaskStore(ROOT/'data')
    assets = [store.get_asset(a) for a in ('asset_e95f5a1a75032003a847c5f4', 'asset_c3e8ad29b751cc4ed61bcfc1')]
    plan = {'schema':'jianying-native-timeline-v1', 'name':'剪映内置字体原生接入测试',
            'canvas':{'width':1080,'height':1920,'fps':30}, 'steps':[
                {'action':'video.add','id':f'source_{i}','path':a['path'],'track':0,
                 'start_us':i*3_000_000,'duration_us':3_000_000,'source_start_us':0}
                for i,a in enumerate(assets)]}
    atomic_json(out/'native-plan.json',plan)
    print('Building native source timeline',flush=True)
    build(out/'native-plan.json',install,out/'source-build')
    cues = [
        {'start_us':0,'end_us':3_000_000,'text':'剪映内置字体 · 宠物碗',
         'ad_style':{'palette':'cream','placement':'upper','font':'source_han_cn','size':11}},
        {'start_us':3_000_000,'end_us':6_000_000,'text':'PET BOWL | Noto Sans',
         'ad_style':{'palette':'mint','placement':'upper','font':'noto_sans','size':10}},
    ]
    atomic_json(out/'text-cues.json',cues)
    print('Applying pinned built-in fonts through native text objects',flush=True)
    result = add_subtitles(install,out/'source-build/draft',out/'text-cues.json',out/'native-text',
                           provider='font-integration-test; no transcription')
    baseline = preview(install,out/'source-build/draft',out/'baseline-preview',[1_500_000,4_500_000],360)
    snapshot = json.loads((out/'native-text/draft-snapshot.json').read_text(encoding='utf-8'))
    expected_paths = {r['path'] for r in result['font_resources']}
    text_materials = snapshot['materials']['texts']
    assert len(text_materials)==2
    for material in text_materials:
        assert material['font_path'] in expected_paths
        rich = json.loads(material['content'])
        assert rich['styles'][0]['font']['path']==material['font_path']
    changed = [a['sha256']!=b['sha256'] for a,b in zip(result['frames'],baseline['frames'])]
    assert len(changed)==2 and all(changed)
    report = {'status':'native_font_integration_passed','scope':'two built-in fonts only',
              'full_resource_pipeline_connected':False,'online_catalog_connected':False,
              'draft':result['draft'],'native_save_readback_reload_verified':True,
              'font_material_paths_verified':True,'baseline_frame_difference':changed,
              'font_resources':result['font_resources'],'frames':result['frames'],
              'registered_to_homepage':False,'full_sales_edit':False}
    atomic_json(out/'verification.json',report)
    print(json.dumps({'status':report['status'],'report':str(out/'verification.json'),
                      'draft':report['draft'],'frames':report['frames']},ensure_ascii=False),flush=True)


if __name__=='__main__': main()
