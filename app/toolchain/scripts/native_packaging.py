"""Apply editable sound, image and alpha-transition layers to a NEW native draft."""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

from common import media_info
from native_service import Service, validate_player_state
from native_build import sidecar_metadata, alpha_keyframe_snapshot
from native_register import rebase
from native_subtitles import check_references
from native_draft_codec import file_digest
from packaging_assets import synthesize


def uid(): return str(uuid.uuid4()).upper()


def dump(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)


def verify_layers(snapshot, report):
    """Check real material references, time ranges, gains, geometry and alpha curves."""
    check_references(snapshot)
    tracks = {t['id']: t for t in snapshot['tracks']}
    segments = {s['id']: s for t in tracks.values() for s in t['segments']}
    materials = {m['id']: m for group in snapshot['materials'].values() if isinstance(group,list)
                 for m in group if isinstance(m,dict) and 'id' in m}
    if snapshot['duration'] != report['duration_us']: raise ValueError('Packaging changed project duration')
    for row in report['items']:
        seg = segments.get(row['segment_id'])
        if seg is None or seg not in tracks[row['track_id']]['segments']:
            raise ValueError('Missing packaged track/segment: '+row['role'])
        for key in ('target_timerange','source_timerange','volume','clip','common_keyframes'):
            if seg.get(key) != row['expected'].get(key): raise ValueError('Packaging property mismatch: '+key)
        mat = materials[seg['material_id']]
        if row['role'] in ('original_audio','music','sfx') and mat.get('type') != 'music':
            raise ValueError('Audio material is empty or has wrong native type')
        if row.get('sha256') and file_digest(Path(mat.get('path',''))) != row['sha256']:
            raise ValueError('Packaging media content changed')
    for old in report['original_tracks']:
        actual = copy.deepcopy(tracks.get(old['id']))
        expected = copy.deepcopy(old)
        if expected['type'] == 'video':
            for s in expected['segments']: s['volume'] = 0
        if expected['type'] == 'text' and actual:
            for i,(a,e) in enumerate(zip(actual['segments'],expected['segments'])):
                # Native video.add numbers cloned text segments within the same track.
                if a.get('render_index') == e.get('render_index',0)+i:
                    e['render_index']=a['render_index']
        # Only the primary track's human-readable name may change.
        for t in (actual, expected):
            if t: t.pop('name',None); t.pop('is_default_name',None)
        if actual != expected: raise ValueError('Unexpected change to source tracks')
    return True


def package(install, source, request_file, output):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from agent_core.resource_policy import require_internal_resources
    require_internal_resources('legacy_external_packaging_script')
    source = Path(source).resolve(strict=True); output = Path(output).resolve()
    install = Path(install).resolve(strict=True)
    if output.exists() or output.is_relative_to(source) or output.is_relative_to(install):
        raise ValueError('Packaging requires a new output outside source and installation')
    request = json.loads(Path(request_file).read_text(encoding='utf-8-sig'))
    if request.get('schema') != 'jianying-editable-packaging-v1': raise ValueError('Invalid packaging schema')
    # Import the same strict validator as the Agent, using only the installed runtime path.
    runtime = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(runtime))
    from agent_core.packaging import validate_packaging
    plan = validate_packaging(request['plan'], request['timeline'])
    original_hash = file_digest(source/'draft_content.json')
    output.mkdir(parents=True); draft = output/'draft'; draft.mkdir()
    skip = {'draft_content.json','draft_content.json.bak','draft_meta_info.json','draft_meta_info.json.bak','template-2.tmp'}
    for p in source.iterdir():
        if p.name not in skip:
            if p.is_dir(): shutil.copytree(p, draft/p.name)
            else: shutil.copyfile(p, draft/p.name)
    media = draft/'media'; media.mkdir(exist_ok=True)
    duration = request['timeline']['duration_us']
    sound = synthesize(media/'original-audio', duration, (plan['music'] or {}).get('theme','light'))
    report = {'status':'in_progress','duration_us':duration,'items':[], 'decisions':plan['decisions'],
              'asset_origin':'Bundled original diagrams and procedural original source audio',
              'online_or_vip_resources_used':False, 'continuous_audio_listened':False,
              'transition_implementation':'outgoing tail overlay with native alpha keyframes; voice timing unchanged'}
    def align(t): return round(t*request['timeline']['native_plan']['canvas']['fps']/1e6)*1_000_000//request['timeline']['native_plan']['canvas']['fps']
    with Service(install, output/'service') as service:
        snap = json.loads(service.request('draft','readFile',
            {'path':str(source/'draft_content.json'),'rootPath':str(source)})['result']['content'])
        snap = rebase(snap, source, draft, snap['id'], snap['id'])
        def normalize():
            for m in snap['materials'].get('sound_channel_mappings',[]):
                if m.get('type') == '': m['type'] = 'none'
        normalize()
        report['original_tracks'] = copy.deepcopy(snap['tracks'])
        main = next(t for t in snap['tracks'] if t['type']=='video')
        if len(main['segments']) != len(request['timeline']['source_map']):
            raise ValueError('Main video track does not match compiled source map')
        originals = copy.deepcopy(main['segments'])
        videos = {v['id']:v for v in snap['materials']['videos']}
        def write(path, value):
            service.request('draft','writeFile',{'path':str(path),'rootPath':str(output),
                'content':json.dumps(value,ensure_ascii=False,allow_nan=False)})
            back = service.request('draft','readFile',{'path':str(path),'rootPath':str(output)})['result']
            if not back.get('encrypted') or json.loads(back['content']) != value:
                raise ValueError('Native encrypted readback mismatch')
        seed = output/'seed.json'; write(seed,snap)
        session = service.request('player','create',{'kind':'draft','serviceGeneration':1})['result']['sessionId']
        try:
            service.request('player','load',{'draftPath':str(seed),'workspacePath':str(output)},session)
            def commit():
                nonlocal snap
                normalize(); check_references(snap)
                expected = copy.deepcopy(snap)
                snap = service.request('edit','draft.setDraft',{'draftSnapshot':snap},session)['result']['draftSnapshot']
                if snap != expected:
                    dump(output/'unexpected-submitted.json',expected);dump(output/'unexpected-returned.json',snap)
                    raise ValueError('Native engine altered packaging snapshot')
            def segment(sid): return next(s for t in snap['tracks'] for s in t['segments'] if s['id']==sid)
            def remember(role, sid):
                seg = segment(sid); track = next(t for t in snap['tracks'] if seg in t['segments'])
                mat = next(m for group in snap['materials'].values() if isinstance(group,list)
                    for m in group if isinstance(m,dict) and m.get('id')==seg['material_id'])
                report['items'].append({'role':role, 'segment_id':sid, 'track_id':track['id'],
                    'expected':copy.deepcopy(seg), 'sha256':file_digest(Path(mat['path'])) if mat.get('path') else None})
            def audio(role, name, path, start, length, source_start, volume, fade):
                nonlocal snap
                info = media_info(path)
                if not info['has_audio'] or source_start+length > round((info.get('audio_duration') or info['duration'])*1e6):
                    raise ValueError('Audio source range unavailable')
                old_ids={s['id'] for t in snap['tracks'] for s in t['segments']}
                response = service.request('edit','audio.add',{'segmentId':uid(),'trackId':uid(),
                    'duration':round(info['duration']*1e6),'sourceDuration':length,'sourceStartTime':source_start,
                    'startTime':start,'targetTrackIndex':len(snap['tracks']),'needInsertTargetTrack':True,
                    'audioPath':str(path),'audioTitle':name,'targetTrackName':name,'volume':volume,'type':4},session)['result']
                snap=response['draftSnapshot']
                new_ids={s['id'] for t in snap['tracks'] for s in t['segments']}-old_ids
                if response.get('accepted') is not True or len(new_ids)!=1:
                    raise ValueError('Native audio.add did not add exactly one segment')
                sid=new_ids.pop()
                actual=segment(sid)
                if (actual['target_timerange']['start']!=start or abs(actual['target_timerange']['duration']-length)>1 or
                        actual['source_timerange'] != {'start':source_start,'duration':length}):
                    raise ValueError(f'Native audio.add changed requested time mapping: {start=}, {length=}, {source_start=}; actual {actual["target_timerange"]}, {actual["source_timerange"]}')
                snap=service.request('edit','audio.setFade',{'draftId':snap['id'],'segIds':[sid],
                    'fadeInDuration':min(fade,length//4),'fadeOutDuration':min(fade,length//4)},session)['result']['draftSnapshot']
                seg=segment(sid); seg['volume']=volume
                track=next(t for t in snap['tracks'] if seg in t['segments'])
                track.update(name=name,is_default_name=False)
                commit(); remember(role,sid)
            main.update(name='主画面（原声已独立）',is_default_name=False)
            for seg in main['segments']: seg['volume']=0
            commit()
            for index, old in enumerate(originals):
                material = videos[old['material_id']]
                if material.get('has_audio'):
                    audio('original_audio','口播与同期原声',material['path'], old['target_timerange']['start'],
                        old['target_timerange']['duration'],old['source_timerange']['start'],old.get('volume',1),40_000)
            if plan['music']:
                audio('music','原创背景音乐',sound['music'],0,duration,0,plan['music']['volume'],700_000)
            for row in plan['sfx']:
                clip=originals[row['clip_index']-1]; start=align(clip['target_timerange']['start']+row['offset_ms']*1000)
                fps=snap['fps']
                length=min(int(media_info(sound[row['kind']])['duration']*fps)*1_000_000//fps,duration-start)
                audio('sfx','节点音效',sound[row['kind']],start,length,0,row['volume'],20_000)
            # Consolidate like-role audio into useful visible tracks, without bouncing the sound.
            for role in ('original_audio','sfx'):
                rows=[r for r in report['items'] if r['role']==role]
                if not rows: continue
                first=next(t for t in snap['tracks'] if t['id']==rows[0]['track_id'])
                for row in rows[1:]:
                    other=next(t for t in snap['tracks'] if t['id']==row['track_id'])
                    first['segments'].extend(other['segments']); snap['tracks'].remove(other); row['track_id']=first['id']
                first['segments'].sort(key=lambda s:s['target_timerange']['start'])
            commit()
            fps = snap['fps']; frame=1_000_000//fps
            for row in plan['transitions']:
                before=originals[row['before_clip']-2]; incoming=originals[row['before_clip']-1]
                length=row['duration_frames']*1_000_000//fps
                r=service.request('edit','video.add',{'path':videos[before['material_id']]['path'],
                    'duration':length,'sourceDuration':length,
                    'sourceStartTime':before['source_timerange']['start']+before['source_timerange']['duration']-length,
                    'startTime':incoming['target_timerange']['start'],'trackIndex':len(snap['tracks']),
                    'needInsertTargetTrack':True},session)['result']
                snap=r['draftSnapshot'];seg=segment(r['segmentId']);seg['volume']=0
                track=next(t for t in snap['tracks'] if seg in t['segments'])
                track.update(name='转场·原生叠化',is_default_name=False)
                # Native video alpha keyframes are evaluated on source time, unlike still images.
                origin=seg['source_timerange']['start']
                snap=alpha_keyframe_snapshot(snap,seg['id'],origin,1)
                snap=alpha_keyframe_snapshot(snap,seg['id'],origin+length-frame,0)
                commit();remember('transitions',seg['id'])
            for row in plan['stickers']:
                path=Path(__file__).resolve().parents[1]/'assets/packaging'/(row['icon']+'.png')
                copied=media/(row['icon']+'.png')
                if not copied.exists(): shutil.copyfile(path,copied)
                clip=originals[row['clip_index']-1]
                start=align(clip['target_timerange']['start']+row['offset_ms']*1000)
                end=align(clip['target_timerange']['start']+(row['offset_ms']+row['duration_ms'])*1000)
                r=service.request('edit','video.add',{'path':str(copied),'duration':end-start,
                    'sourceDuration':end-start,'sourceStartTime':0,'startTime':start,'trackIndex':len(snap['tracks']),
                    'needInsertTargetTrack':True},session)['result']
                snap=r['draftSnapshot']; sid=r['segmentId'];seg=segment(sid)
                seg['volume']=0;seg['clip']['scale']={'x':.11,'y':.11}
                seg['clip']['transform']={'x':-.75 if row['anchor'].endswith('left') else .75,
                                        'y':.4 if row['anchor'].startswith('upper') else -.4}
                track=next(t for t in snap['tracks'] if seg in t['segments'])
                track.update(name='本地贴图·'+row['icon'],is_default_name=False)
                for offset,value in ((0,0),(3*frame,1),(end-start-4*frame,1),(end-start-frame,0)):
                    snap=alpha_keyframe_snapshot(snap,sid,offset,value)
                commit();remember('stickers',sid)
            if request['timeline'].get('has_original_audio',True) and not any(r['role']=='original_audio' for r in report['items']):
                raise ValueError('No actual original audio material was added')
            verify_layers(snap,report)
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
    if file_digest(source/'draft_content.json') != original_hash: raise ValueError('Source draft changed')
    report.update(status='native_packaging_created',draft=str(draft),source_preserved=True,
        native_roundtrip_reload_verified=True,editable_layers_verified=True,
        tracks=[{'id':t['id'],'type':t['type'],'name':t.get('name'),'segments':len(t['segments'])} for t in snap['tracks']])
    dump(output/'draft-snapshot.json',snap);dump(output/'packaging-report.json',report)
    return report


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    for arg in ('install-dir','draft','request','out'): parser.add_argument('--'+arg,required=True)
    a=parser.parse_args()
    try:
        result=package(a.install_dir,a.draft,a.request,a.out)
        print(json.dumps({'status':result['status'],'draft':result['draft']},ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)},ensure_ascii=False));raise
