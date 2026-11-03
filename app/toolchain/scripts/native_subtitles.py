"""Add timed editable subtitles to a COPY of a native draft, then verify pixels.

Input cues are in the output timeline: [{start_us, end_us, text}, ...].
Recognition and source-to-output timing mapping are separate operations.
"""
import argparse
import copy
import json
import math
from pathlib import Path
import shutil
import sys
import time
import uuid

from native_service import Service, validate_player_state
from native_register import rebase
from native_build import sidecar_metadata
from native_preview import preview
from native_draft_codec import file_digest


def dump(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def normalize_cues(rows, duration, fps):
    if not isinstance(rows, list) or not rows or len(rows) > 10000:
        raise ValueError('Require 1..10000 timed subtitle cues')
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ValueError('Require integer project fps in 1..120')
    cues = []
    previous_end = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Each cue must be an object')
        if 'ad_style' in row:
            ads = row['ad_style']
            if (not isinstance(ads, dict) or set(ads) != {'palette', 'placement', 'font', 'size'} or
                    ads['palette'] not in ('cream', 'mint', 'pink') or
                    ads['placement'] not in ('upper', 'lower') or
                    ads['font'] not in ('source_han_cn', 'noto_sans') or
                    type(ads['size']) not in (int, float) or not 8 <= ads['size'] <= 14):
                raise ValueError('Invalid bounded product-ad text style')
        start, end, text = row.get('start_us'), row.get('end_us'), row.get('text')
        if (type(start) is not int or type(end) is not int or
                not 0 <= start < end <= duration or start < previous_end):
            raise ValueError('Cues must be ordered, nonoverlapping, and inside the draft')
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError('Cue text must contain 1..2000 characters')
        previous_end = end
        cue = dict(row, input_start_us=start, input_end_us=end)
        for key in ('start_us', 'end_us'):
            # Native service performs ceil-to-frame normalization too.
            cue[key] = min(duration, ((row[key]*fps+999999)//1000000)*1000000//fps)
        if cue['end_us'] <= cue['start_us']:
            raise ValueError('Cue collapses after frame alignment; revise its timing')
        if cues and cue['start_us'] < cues[-1]['end_us']:
            raise ValueError('Frame-aligned subtitle cues overlap')
        cues.append(cue)
    return cues


def check_references(snapshot):
    objects = list(snapshot['tracks'])
    segments = [s for t in snapshot['tracks'] for s in t['segments']]
    materials = [m for group in snapshot['materials'].values() if isinstance(group, list)
                 for m in group if isinstance(m, dict) and m.get('id')]
    ids = [o['id'].casefold() for o in objects+segments+materials]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate native object IDs')
    material_ids = {m['id'] for m in materials}
    for segment in segments:
        if segment.get('material_id') not in material_ids:
            raise ValueError('Segment refers to a missing native material')
        if any(ref not in material_ids for ref in segment.get('extra_material_refs', [])):
            raise ValueError('Segment refers to a missing auxiliary material')


def add_subtitles(install, source, cue_path, output, *, provider, y=-.45,
                  font='source_han_cn', size=10):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent_core.internal_fonts import resolve_font, validate_text
    from agent_core.resource_policy import POLICY
    font_record = resolve_font(install, font)
    source = Path(source).resolve(strict=True)
    output = Path(output).resolve()
    install = Path(install).resolve(strict=True)
    if (output.exists() or output.is_symlink() or output.is_relative_to(source)
            or output.is_relative_to(install)):
        raise ValueError('Choose a new output directory outside source and installation')
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError('Provide the actual transcription origin')
    if not math.isfinite(y) or not -1 <= y <= 1 or not math.isfinite(size) or not 1 <= size <= 100:
        raise ValueError('Invalid subtitle position or font size')
    font = font_record['path']
    rows = json.loads(Path(cue_path).read_text(encoding='utf-8-sig'))
    digest = file_digest(source/'draft_content.json')
    output.mkdir(parents=True)
    report = {'status':'in_progress','transcription_provider':provider,
              'gui_opened':False,'ffmpeg_invoked':False,'gui_acceptance':'not_checked'}
    try:
        draft = output/'draft'
        with Service(install, output/'service') as service:
            snap = json.loads(service.request('draft','readFile',
                {'path':str(source/'draft_content.json'),'rootPath':str(source)})['result']['content'])
            duration = snap['duration']
            cues = normalize_cues(rows, duration, snap['fps'])
            for cue in cues:
                chosen = resolve_font(install, cue.get('ad_style', {}).get('font', font_record['adapter_font_id']))
                validate_text(chosen, cue['text'])
            check_references(snap)
            draft.mkdir()
            skip={'draft_content.json','draft_content.json.bak','draft_meta_info.json',
                  'draft_meta_info.json.bak','template-2.tmp'}
            for path in source.iterdir():
                if path.name in skip: continue
                if path.is_dir(): shutil.copytree(path,draft/path.name)
                else: shutil.copyfile(path,draft/path.name)
            snap = rebase(snap, source, draft, snap['id'], snap['id'])
            old_tracks = copy.deepcopy(snap['tracks'])
            old_materials = copy.deepcopy(snap['materials'])
            def write(path,value):
                if path.exists(): raise FileExistsError(path)
                service.request('draft','writeFile',{'path':str(path),'rootPath':str(output),
                    'content':json.dumps(value,ensure_ascii=False,allow_nan=False)})
                back=service.request('draft','readFile',{'path':str(path),'rootPath':str(output)})['result']
                if not back.get('encrypted') or json.loads(back['content']) != value:
                    raise RuntimeError('Native encrypted roundtrip mismatch')
            seed=output/'seed.json';write(seed,snap)
            session=service.request('player','create',{'kind':'draft','serviceGeneration':1})['result']['sessionId']
            try:
                service.request('player','load',{'draftPath':str(seed),'workspacePath':str(output)},session)
                first=cues[0]
                snap=service.request('edit','text.add',{'content':first['text'],
                    'startTime':first['start_us'],'duration':first['end_us']-first['start_us'],
                    'trackIndex':len(old_tracks),'needInsertTargetTrack':True},session)['result']['draftSnapshot']
                new_tracks=[t for t in snap['tracks'] if t['id'] not in {t['id'] for t in old_tracks}]
                if len(new_tracks)!=1 or new_tracks[0]['type']!='text':
                    raise RuntimeError('Native text.add did not create one new text track')
                track=new_tracks[0];template=copy.deepcopy(track['segments'][0])
                material=copy.deepcopy(next(m for m in snap['materials']['texts'] if m['id']==template['material_id']))
                animations={m['id']:copy.deepcopy(m) for m in snap['materials'].get('material_animations',[])
                            if m['id'] in template['extra_material_refs']}
                if set(template['extra_material_refs'])!=set(animations):
                    raise RuntimeError('Unsupported native text auxiliary material type')
                snap['materials']['texts']=[m for m in snap['materials']['texts'] if m['id']!=material['id']]
                snap['materials']['material_animations']=[m for m in snap['materials']['material_animations'] if m['id'] not in animations]
                track['segments']=[];track['name']='字幕（'+provider+'）';track['is_default_name']=False
                material.update(font_size=size,font_path=font,font_name=font_record['family'])
                def uid():return str(uuid.uuid4()).upper()
                for cue in cues:
                    seg,mat=copy.deepcopy(template),copy.deepcopy(material)
                    mat['id']=uid();seg['id']=uid();seg['material_id']=mat['id'];seg['extra_material_refs']=[]
                    for animation in animations.values():
                        clone=copy.deepcopy(animation);clone['id']=uid()
                        snap['materials']['material_animations'].append(clone)
                        seg['extra_material_refs'].append(clone['id'])
                    seg['target_timerange']={'start':cue['start_us'],'duration':cue['end_us']-cue['start_us']}
                    seg['clip']['transform']={'x':0,'y':y}
                    rich=json.loads(mat['content']);rich['text']=cue['text'];style=rich['styles'][0]
                    style['range']=[0,len(cue['text'].encode('utf-16-le'))//2];style['size']=size
                    style['font']['path']=font
                    style['strokes']=[{'content':{'solid':{'alpha':.95,'color':[0,0,0]}},'width':.045}]
                    if 'ad_style' in cue:
                        ads=cue['ad_style']
                        chosen=resolve_font(install,ads['font'])
                        selected_font,font_name=chosen['path'],chosen['family']
                        colors={'cream':[1,.92,.68],'mint':[.64,1,.86],'pink':[1,.74,.82]}
                        color=colors[ads['palette']]
                        mat.update(font_size=ads['size'],font_path=selected_font,font_name=font_name,
                                   text_color='#'+''.join(f'{round(c*255):02X}' for c in color))
                        style['font']['path']=selected_font;style['size']=ads['size']
                        style['fill']={'alpha':1,'content':{'render_type':'solid','solid':{'alpha':1,'color':color}}}
                        style['strokes']=[{'content':{'solid':{'alpha':.9,'color':[.08,.10,.11]}},'width':.035}]
                        seg['clip']['transform']={'x':0,'y':.69 if ads['placement']=='upper' else -.68}
                    mat['content']=json.dumps(rich,ensure_ascii=False)
                    track['segments'].append(seg);snap['materials']['texts'].append(mat)
                    cue.update(native_segment_id=seg['id'],native_material_id=mat['id'])
                expected=copy.deepcopy(snap)
                snap=service.request('edit','draft.setDraft',{'draftSnapshot':snap},session)['result']['draftSnapshot']
                dump(output/'submitted-snapshot.json',expected)
                dump(output/'returned-snapshot.json',snap)
                if snap!=expected: raise RuntimeError('Native service changed requested subtitle content or timing')
                if any(next(t for t in snap['tracks'] if t['id']==old['id'])!=old for old in old_tracks):
                    raise RuntimeError('Original tracks changed')
                for key,group in old_materials.items():
                    if not isinstance(group,list):continue
                    for item in group:
                        checked=copy.deepcopy(item)
                        # This build initializes the empty enum on session reload.
                        # Accept only this observed field change, not arbitrary edits.
                        if key=='sound_channel_mappings' and checked.get('type')=='':
                            checked['type']='none'
                            report.setdefault('native_enum_normalizations',[]).append(item['id'])
                        if checked not in snap['materials'].get(key,[]):
                            raise RuntimeError('Original materials changed: '+key)
                check_references(snap)
                write(draft/'draft_content.json',snap)
                write(draft/'draft_meta_info.json',sidecar_metadata(snap,draft,output,time.time_ns()//1000))
            finally: service.request('player','destroy',{},session)
            session=service.request('player','create',{'kind':'draft','serviceGeneration':2})['result']['sessionId']
            try:
                service.request('player','load',{'draftPath':str(draft/'draft_content.json'),'workspacePath':str(output)},session)
                validate_player_state(service.request('player','getState',{},session)['result'],duration,snap['canvas_config'])
            finally: service.request('player','destroy',{},session)
        for filename in ('draft_content.json','draft_meta_info.json'):
            shutil.copyfile(draft/filename,draft/(filename+'.bak'))
        shutil.copyfile(draft/'draft_content.json',draft/'template-2.tmp')
        dump(output/'draft-snapshot.json',snap);dump(output/'captions.json',cues)
        positions=sorted({(c['start_us']+c['end_us'])//2 for c in (cues[0],cues[len(cues)//2],cues[-1])})
        frames=preview(install,draft,output/'preview',positions,360)['frames']
        if file_digest(source/'draft_content.json')!=digest:
            raise RuntimeError('Original draft changed')
        report.update(status='native_subtitles_created',draft=str(draft),subtitle_count=len(cues),
                      resource_policy=POLICY, font_resources=[resolve_font(install, fid) for fid in
                          sorted({font_record['adapter_font_id']} | {c['ad_style']['font'] for c in cues if 'ad_style' in c})],
                      duration_us=duration,native_roundtrip_reload_verified=True,
                      original_tracks_preserved=True,original_materials_preserved_except_recorded_normalization=True,unique_ids_verified=True,
                      source_draft_preserved=True,frames=frames,registration='not_requested')
        dump(output/'subtitle-report.json',report)
        return report
    except Exception as exc:
        report.update(status='failed',error=str(exc));dump(output/'subtitle-failure.json',report)
        raise


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('install-dir','draft','captions','out','provider'):p.add_argument('--'+name,required=True)
    p.add_argument('--y',type=float,default=-.45)
    a=p.parse_args()
    try: print(json.dumps(add_subtitles(a.install_dir,a.draft,a.captions,a.out,provider=a.provider,y=a.y),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)},ensure_ascii=False));raise SystemExit(2)
