"""Offline installation smoke test. Must run with the installed Python executable."""
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(sys.executable).resolve().parents[1]
sys.path.insert(0, str(ROOT))
assert (ROOT/'installed-profile.json').is_file()
from agent_core.installed_runtime import prepare
prepare(ROOT)
from agent_core import local_voice
from agent_core.workbench import Workbench
from agent_core.direct_renderer import snapshot_from_timeline, render_snapshot
from agent_core.font_library import bundled_font_library
from agent_core.framo_supplements import apply_supplements
from agent_core.independent_export import _layout_crop_spec
from agent_core.motion_templates import TemplateLibrary
from agent_core.version import VERSION

report_dir = Path(sys.argv[1]).resolve(); report_dir.mkdir(parents=True, exist_ok=True)
report = {'installation':str(ROOT),'version':VERSION,'started':time.time(),'checks':{},'network_model_test':False}


def mark(key, value=True):
    report['checks'][key] = value
    print(key, json.dumps(value,ensure_ascii=False),flush=True)
    (report_dir/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


manifest=json.loads((ROOT/'package-manifest.json').read_text(encoding='utf-8'))
assert manifest['version']==VERSION
assert json.loads((ROOT/'installed-profile.json').read_text(encoding='utf-8'))['version']==VERSION
for row in manifest['files']:
    assert digest(ROOT/row['path'])==row['sha256'],row['path']
mark('installed_payload_hashes',len(manifest['files']))
with patch('agent_core.workbench.load_key',return_value=None):
    backend=Workbench(ROOT/'UserData')
    snapshot=backend.status()
    assert snapshot['jobs']==[] and snapshot['batches']==[]
    assert snapshot['settings']['install_dir']==''
    assert any(not c['ok'] and c['name']=='工作台访问凭证' for c in snapshot['checks'])
    try:
        backend.create_job({'instruction':'test','asset_ids':[]})
        raise AssertionError('Missing access code must block job creation')
    except ValueError as exc:
        assert '环境自检' in str(exc)
    mark('empty_history_and_missing_credential_gate')
    for key in ('toolchain_root','asr_model_dir','direct_ffmpeg_dir','direct_resources_root'):
        assert Path(backend.service.config[key]).is_relative_to(ROOT)
    assert Path(backend.default_output_directory()).is_relative_to(ROOT/'UserData')
    mark('relocated_dependencies_and_output')
    import tkinter as tk
    from agent_core.desktop_workbench import DesktopWorkbench
    window=tk.Tk(); window.geometry('1280x760+20+20')
    app=DesktopWorkbench(window,backend,poll=False)
    app.render_status(snapshot)
    pages=('create','templates','tasks','export','settings')
    assert set(app.views)==set(pages)
    for page in pages:
        app.show(page)
        for _ in range(4):
            window.update();time.sleep(.06)
        assert window.winfo_ismapped() and window.winfo_width()>=1280
    assert len(app.template_tree.get_children())==6
    app.shutdown()
    mark('five_real_tk_pages',list(pages))

templates=TemplateLibrary(ROOT/'UserData',root=ROOT)
template_checks=dict(templates.check())
assert template_checks and all(template_checks.values()),template_checks
families=templates.catalog()
assert {family['id'] for family in families}=={'01','02','03','04','05','06'}
styles=[style for family in families for style in family['styles']]
assert len(styles)==11 and all(style['installed'] for style in styles)
bindings=[]
for style in styles:
    for layout in style['layouts']:
        pack=templates.resolve_pack(style,layout)
        assert pack.is_file() and pack.resolve().is_relative_to(ROOT)
        bindings.append({'style':style['id'],'layout':layout,'manifest':str(pack.relative_to(ROOT))})
assert len(bindings)==21
mark('installed_template_dependencies',template_checks)
mark('installed_template_bindings',{'families':len(families),'styles':len(styles),'bindings':bindings,
                                    'full_template_export_tested':False})

fonts=bundled_font_library()
font_checks=[]
for language,text in [('zh-CN','Framo 安装验证，自动剪辑。'),
                      ('ja','フレイモで毎日の動画を楽しく。'),
                      ('en','Framo makes every detail clear.')]:
    selected=fonts.select(text,language=language)
    assert Path(selected['path']).is_relative_to(ROOT)
    assert Path(selected['license_file']).is_file()
    assert selected['coverage_policy']=='complete_text_verified'
    assert fonts.load_pillow_font(selected,40).getmask(text).getbbox()
    font_checks.append({'language':language,'font_id':selected['font_id'],'sha256':selected['sha256']})
mark('installed_fonts_load_and_cover_text',{'families':len(fonts.families),'selections':font_checks})

# Synthetic evidence verifies installation wiring, not an actual detector run.
layout_timeline={'source_map':[{'asset_id':'smoke','source_segment_id':'smoke-segment',
    'source_start_us':0,'source_end_us':1000000,'timeline_start_us':0,'timeline_end_us':1000000}]}
observations={'smoke':[{'asset_id':'smoke','requested_us':100000,
    'subject_bbox':{'x':.6,'y':.2,'w':.2,'h':.5},'confidence':'high'}]}
with_box,analysis=apply_supplements({},layout_timeline,[{'asset_id':'smoke'}],
                                  observations_by_asset=observations)
without_box,_=apply_supplements({},layout_timeline,[{'asset_id':'smoke'}])
assert with_box['layout_tracks'][0]['mode']=='subject_follow'
assert analysis['evidence'] and analysis['evidence'][0]['evidence_id']
assert without_box['layout_tracks'][0]['mode']=='center_fallback'
crop=_layout_crop_spec({'width':1920,'height':1080},with_box['layout_tracks'][0],
                      {'width':360,'height':640})
assert crop and crop['axis']=='x' and crop['points'][0]['center']==.7
mark('recomposition_supplement_available',{'version':with_box['supplement_version'],
    'bbox_mode':'subject_follow','missing_bbox_mode':'center_fallback',
    'crop_axis':crop['axis'],'synthetic_evidence_only':True})

from agent_core.desktop_workbench import default_instruction, WORKFLOWS
from agent_core import locked_opening, original_montage, video_limits
from agent_core.commerce import compile_ad
from unittest.mock import Mock
assert WORKFLOWS['长视频']=='long_video'
assert WORKFLOWS['长视频（无口播）']=='long_video_no_voice'
assert video_limits.MAX_ASSETS==20 and video_limits.MAX_SOURCE_US==420_000_000
assert video_limits.MAX_OUTPUT_MS==600_000 and original_montage.MAX_STICKER_EVENTS==100
task=Workbench._prepare_spec(None,{'workflow':'long_video_no_voice','asset_ids':['opening'],
    'instruction':default_instruction('long_video_no_voice'),'target_duration_ms':40000})
assert task['opening_policy']==locked_opening.POLICY and '前四十秒' in task['instruction']
asset={'asset_id':'opening','path':'fixture-not-rendered.mp4','video_duration_us':60_000_000,
    'audio_duration_us':60_000_000,'has_audio':True,'width':360,'height':640,
    'frame_rate_mode':'CFR','rotation':0}
source={**asset,'speech_mode':'speech','speech_units':[{'unit_id':f's{i:03d}',
    'start_us':i*3_000_000,'end_us':(i+1)*3_000_000,'text':'完整表达。'} for i in range(20)],
    'frames':[{'requested_us':i*1_000_000} for i in range(60)],'observations':[]}
provider=Mock();plan=original_montage.plan(provider,task,[source])
compiled,_,_=compile_ad(task,[asset],[source],plan)
assert compiled['duration_us']==40_000_000 and compiled['source_map'][0]['source_start_us']==0
assert all(m['source_start_us']==m['timeline_start_us'] and m['source_end_us']==m['timeline_end_us']
           for m in compiled['source_map'])
provider._json.assert_not_called()
stickers=json.loads((ROOT/'desktop-assets/media-library/sticker-library/manifest.json').read_text(encoding='utf-8'))
assert len(stickers['stickers'])==151
from agent_core.sound_design import catalog as sound_catalog
assert len(sound_catalog())>=12
assert not any(p.name.startswith('qa-') for p in (ROOT/'docs').glob('*'))
mark('latest_long_video_and_resource_features',{'modes':['长视频','长视频（无口播）'],
    'max_assets':20,'max_source_seconds':420,'max_output_seconds':600,
    'opening_policy':task['opening_policy'],'prefix_frame_mapping_verified':True,
    'sticker_catalog':151,'sticker_glint_event_cap':100,'sound_catalog':len(sound_catalog()),
    'synthetic_compile_only':True})

library=Path(backend.service.config['direct_media']['local_voice_library'])
catalog=local_voice.catalog(library)
assert len(catalog['voices'])==69
assert Counter(voice['locale'] for voice in catalog['voices'])=={'ja-JP':9,'en-US':50,'zh-CN':10}
assert Counter(v['gender'] for v in catalog['voices'] if v['locale']=='zh-CN')=={'female':5,'male':5}
assert catalog['defaults']=={'ja-JP':'kokoro_jf_alpha','en-US':'kokoro_af_heart','zh-CN':'kokoro_zh_female_01'}
for voice in catalog['voices']:
    local_voice.validate_wav(library/'samples'/(voice['id']+'.wav'))
mark('voice_samples_decoded',{'total':69,'ja-JP':9,'en-US':50,'zh-CN':10})
generated=[]
for locale, text, voice in [
    ('zh-CN','这是一段中文女声，带你了解视频里的实用细节。','kokoro_zh_female_01'),
    ('zh-CN','这是中文男声演示，画面和声音都在软件内完成。','kokoro_zh_male_01'),
    ('en-US','A fresh start, with every detail in view.',None),
    ('ja-JP','毎日の暮らしに、ちょっとした楽しみを。',None),
    ('en-US','This is a local voice, ready to use.',next(v['id'] for v in catalog['voices'] if v['engine']=='libritts')),
    ('ja-JP','明るい声で、新しい動画づくりを始めましょう。',
        next(v['id'] for v in catalog['voices'] if v['engine']=='voicevox_metan')),
    ('ja-JP','かわいい声で、毎日の楽しさを届けます。',
        next(v['id'] for v in catalog['voices'] if v['engine']=='voicevox_nana'))]:
    voice_record=local_voice.choose(library,locale,voice)
    voice_work=ROOT/'UserData/SmokeVoice'/voice_record['id']
    if voice_record['engine'].startswith('voicevox_'):
        # An empty, isolated cache proves fresh synthesis without changing the script.
        fresh_cache=voice_work/('fresh-cache-'+str(time.time_ns()))
        with patch.dict(os.environ,{'FRAMO_VOICE_CACHE':str(fresh_cache)}):
            rows=local_voice.generate(library,[text],locale,voice_work,voice_id=voice)
    else:
        rows=local_voice.generate(library,[text],locale,voice_work,voice_id=voice)
    assert len(rows)==1 and rows[0]['engine']==voice_record['engine']
    assert rows[0]['voice_id']==voice_record['id'] and rows[0]['text']==text
    assert Path(rows[0]['path']).is_relative_to(ROOT/'UserData')
    if voice_record['engine'].startswith('voicevox_'):
        assert rows[0]['cache_hit'] is False
        assert rows[0]['attribution_required'] is True
        assert rows[0]['attribution']==voice_record['attribution']
        assert rows[0]['terms_url']==voice_record['terms_url']
        credit=Path(rows[0]['attribution_file'])
        assert credit.is_relative_to(ROOT/'UserData') and credit.is_file()
        assert voice_record['attribution'] in credit.read_text(encoding='utf-8')
        assert voice_record['attribution'] in (voice_work/'配音署名.txt').read_text(encoding='utf-8')
    generated.extend(rows)
    mark('speech_'+rows[0]['voice_id'],{'engine':rows[0]['engine'],'cache_hit':rows[0]['cache_hit'],
        'checks':rows[0]['checks'],'attribution':rows[0].get('attribution'),
        'human_listened':False})

fixture=ROOT/'UserData/SmokeRender';fixture.mkdir(parents=True,exist_ok=True)
ffmpeg=str(ROOT/'toolchain/ffmpeg.exe')
subprocess.run([ffmpeg,'-v','error','-nostdin','-y','-f','lavfi','-i',
    'testsrc2=size=360x640:rate=30','-t','8','-c:v','libx264','-pix_fmt','yuv420p',str(fixture/'input.mp4')],
    check=True,capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
timeline={'duration_us':8000000,'source_map':[{'asset_id':'test','source_start_us':0,'source_end_us':8000000,
    'timeline_start_us':0,'timeline_end_us':8000000}],
    'captions':[{'start_us':0,'end_us':8000000,'text':'Framo 安装验证 / サウンドテスト'}]}
assets=[{'asset_id':'test','path':str(fixture/'input.mp4'),'sha256':digest(fixture/'input.mp4'),
         'width':360,'height':640,'has_audio':False}]
audios=[('voice',Path(generated[0]['path']),1.0,False),
        ('music',ROOT/'Resources/DirectBuiltin/music/framo-soft-pulse.wav',.16,True)]
media={'materials':{'audio':[]},'tracks':[]}
for mid,path,volume,loop in audios:
    media['materials']['audio'].append({'id':mid,'path':str(path),'has_audio':True,'sha256':digest(path)})
    length=8000000 if loop else min(8000000,generated[0]['checks']['duration_us'])
    media['tracks'].append({'type':'music' if loop else 'audio','segments':[{'material_id':mid,
        'target_timerange':{'start':0,'duration':length},'source_timerange':{'start':0,'duration':length},
        'volume':volume,'loop':loop,'fade_in_us':20000,'fade_out_us':120000}]})
snapshot=snapshot_from_timeline({'instruction':'字幕放在右上角','target':{'fps':30,'aspect_ratio':'source'}},assets,timeline,media_plan=media)
result=render_snapshot(snapshot,fixture/'installation-smoke.mp4',install_dir=ROOT/'toolchain',
                       work_dir=fixture/'render',font_path=ROOT/'desktop-assets/fonts/LXGW975YuanSC-700W.ttf')
assert result['status']=='COMPLETED' and result['media']['has_video'] and result['media']['has_audio']
assert result['playback_ok'] is True
assert result['subtitle_layout'] and all(r['position']=='top_right' for r in result['subtitle_layout'])
mark('subtitle_position_from_user_instruction','top_right')
mark('render_and_full_decode',result['media'])
subprocess.run([ffmpeg,'-v','error','-y','-ss','1','-i',str(fixture/'installation-smoke.mp4'),
                '-frames:v','1',str(report_dir/'render-frame.png')],check=True,capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW)
backend.save_output_directory(str(ROOT/'UserData/SelectedExports'))
reopened=Workbench(ROOT/'UserData')
assert Path(reopened.default_output_directory())==ROOT/'UserData/SelectedExports'
assert reopened.jobs()==[]
mark('settings_persist_across_reopen')
for row in manifest['files']:
    assert digest(ROOT/row['path'])==row['sha256'],row['path']
mark('installed_program_and_model_files_unchanged')
report['finished']=time.time(); report['elapsed_seconds']=round(report['finished']-report['started'],2)
(report_dir/'verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
