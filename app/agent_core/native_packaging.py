"""Typed official-resource packaging with immutable, resumable draft checkpoints.

No model-supplied shell command or arbitrary resource path is accepted. A CLI
write is committed only after native extraction confirms its object/timing.
An interrupted candidate is retained for diagnosis; recovery starts from the
last committed draft, so an uncertain add cannot duplicate a layer.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from .internal_fonts import resolve_font, validate_text
from .native_resource_catalog import acquire, envelope, entitlement, ResourceFailure
from .task_store import atomic_json, digest
from .file_io import io_path, logical_path

SCHEMA = 'jianying-native-packaging-v1'
KINDS = ('music', 'sound', 'sticker', 'effect', 'transition', 'text', 'filter')
IMPLEMENTATION_SHA256 = digest(Path(__file__))


class PackagingFailure(ValueError):
    pass


def native_modules():
    scripts = Path(__file__).resolve().parents[1] / 'toolchain/scripts'
    if str(scripts) not in sys.path: sys.path.insert(0, str(scripts))
    from native_service import Service
    from native_subtitles import check_references
    from native_register import rebase
    return Service, check_references, rebase


def bounded(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def validate_plan(plan, snapshot, install):
    if (not isinstance(plan, dict) or set(plan)-{'schema', 'purpose', 'elements','separate_source_audio'} or
            not {'schema','purpose','elements'} <= set(plan) or
            type(plan.get('separate_source_audio',False)) is not bool or
            plan['schema'] != SCHEMA or plan['purpose'] not in ('local_test', 'commercial') or
            not isinstance(plan['elements'], list) or not 1 <= len(plan['elements']) <= 32):
        raise PackagingFailure('invalid_native_packaging_plan')
    duration, fps = snapshot['duration'], snapshot['fps']
    if type(fps) is not int or not 1 <= fps <= 120 or type(duration) is not int or duration <= 0:
        raise PackagingFailure('invalid_native_timeline')
    video_tracks = [t for t in snapshot['tracks'] if t['type'] == 'video']
    videos = {s['id']: (track, i) for track in video_tracks for i, s in enumerate(track['segments'])}
    transition_ids = {m['id'] for m in snapshot['materials'].get('transitions', [])}
    keys, transitions = set(), set()
    for row in plan['elements']:
        if not isinstance(row, dict): raise PackagingFailure('invalid_element')
        kind = row.get('kind')
        fields = {'key', 'kind', 'duration_us'}
        if kind not in KINDS: raise PackagingFailure('unsupported_native_element')
        fields |= {'after_segment_id', 'resource_id'} if kind == 'transition' else {'start_us'}
        if kind == 'text': fields |= {'text', 'font_id', 'color', 'size', 'x', 'y'}
        elif kind != 'transition': fields.add('resource_id')
        if kind in ('music', 'sound'): fields.add('volume')
        if kind == 'sticker': fields |= {'x', 'y'}
        if kind == 'filter': fields.add('intensity')
        if (set(row) != fields or not isinstance(row['key'], str) or
                not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', row['key']) or row['key'] in keys):
            raise PackagingFailure('invalid_or_duplicate_element_fields:'+str(row.get('key'))+
                ':missing='+str(sorted(fields-set(row)))+':extra='+str(sorted(set(row)-fields)))
        keys.add(row['key'])
        length = row['duration_us']
        if (type(length) is not int or not round(1_000_000/fps) <= length <= duration or
                abs(length - round(length*fps/1_000_000)*1_000_000/fps) > 1):
            raise PackagingFailure('element_duration_not_frame_aligned:'+row['key']+':'+str(length)+':timeline='+str(duration))
        if kind != 'text' and (not isinstance(row['resource_id'], str) or not re.fullmatch(r'[0-9]{1,32}',row['resource_id'])):
            raise PackagingFailure('invalid_official_resource_id')
        if kind == 'transition':
            sid = row['after_segment_id']
            if sid not in videos or sid in transitions: raise PackagingFailure('invalid_transition_target')
            track, index = videos[sid]; a = track['segments'][index]
            if index+1 >= len(track['segments']): raise PackagingFailure('transition_has_no_following_clip')
            b = track['segments'][index+1]
            ta, tb = a['target_timerange'], b['target_timerange']
            if (ta.get('start',0)+ta['duration'] != tb.get('start',0) or
                    length*2 > min(ta['duration'],tb['duration']) or
                    transition_ids.intersection(a.get('extra_material_refs',[]))):
                raise PackagingFailure('transition_overwrites_or_exceeds_neighboring_clips')
            transitions.add(sid)
        else:
            start = row['start_us']
            if (type(start) is not int or not 0 <= start <= duration-length or
                    abs(start-round(start*fps/1_000_000)*1_000_000/fps) > 1):
                raise PackagingFailure('element_outside_timeline_or_not_frame_aligned')
        if kind in ('music','sound') and not bounded(row['volume'],0.01,1):
            raise PackagingFailure('invalid_audio_gain')
        if kind == 'filter' and not bounded(row['intensity'],.01,1):
            raise PackagingFailure('invalid_filter_intensity')
        if kind in ('text','sticker') and not all(bounded(row[k],-.8,.8) for k in ('x','y')):
            raise PackagingFailure('invalid_element_position')
        if kind == 'text':
            if (not isinstance(row['text'],str) or not 1 <= len(row['text'].strip()) <= 200 or
                    not isinstance(row['color'],str) or not re.fullmatch(r'#[0-9a-fA-F]{6}',row['color']) or
                    not bounded(row['size'],8,24)):
                raise PackagingFailure('invalid_native_text')
            if not re.fullmatch(r'[0-9]{1,32}',str(row['font_id'])):
                validate_text(resolve_font(install,row['font_id']),row['text'])
    return plan


def verify_files(proof):
    root = Path(proof['path']).resolve(strict=True)
    base = Path(proof['_directory']).resolve(strict=True)
    if not root.is_relative_to(base) or not proof.get('files'):
        raise PackagingFailure('resource_payload_outside_receipt')
    for row in proof['files']:
        path = io_path(base/row['path']).resolve(strict=True)
        if (not logical_path(path).is_relative_to(base) or not path.is_file() or path.stat().st_size != row['bytes'] or
                digest(path) != row['sha256']):
            raise PackagingFailure('resource_payload_changed')


def fresh_rights(client, kind, rid, purpose):
    data = envelope(client,['material','query','--resource-id',rid,'--type',kind])
    rows = [r for r in data.get('items',[]) if isinstance(r,dict) and str(r.get('resource_id')) == rid]
    if len(rows) != 1: raise PackagingFailure('resource_identity_unconfirmed')
    rights = entitlement(kind,rows[0])
    if not rights['nonmember_metadata_verified']: raise PackagingFailure('resource_not_explicitly_nonmember')
    if purpose == 'commercial' and rights['commercial_field_value'] is not True:
        raise PackagingFailure('resource_lacks_positive_commercial_metadata')
    return rights


@contextmanager
def process_lock(path):
    import msvcrt
    with Path(path).open('a+b') as stream:
        if stream.tell() == 0: stream.write(b'0');stream.flush()
        stream.seek(0)
        try: msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        except OSError: raise PackagingFailure('packaging_run_already_active') from None
        try: yield
        finally:
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)


def extract(client,draft):
    result=envelope(client,['draft','extract','--ref','local://'+draft.as_posix(),'--scope','full'])
    try: nodes=json.loads(result['json_result'])
    except (KeyError,ValueError,TypeError): raise PackagingFailure('native_extraction_invalid') from None
    if not isinstance(nodes,list) or any(not isinstance(v,dict) for v in nodes):
        raise PackagingFailure('native_extraction_invalid')
    return nodes


def time_arg(value): return f'{value}us'


def resource_identity(row):
    if row['kind']=='text':
        return ('font',row['font_id']) if re.fullmatch(r'[0-9]{1,32}',str(row['font_id'])) else None
    return row['kind'],row['resource_id']


def apply_element(client,draft,row,proof):
    before=extract(client,draft);ref='local://'+draft.as_posix();kind=row['kind']
    if kind == 'transition':
        result=envelope(client,['draft','video','transition','add','--ref',ref,'--seg-id',row['after_segment_id'],
            '--resource','vimo://'+row['resource_id'],'--duration',time_arg(row['duration_us']),'--is-overlap','true'])
        sid=row['after_segment_id']
    else:
        track_count=len([n for n in before if n.get('type')=='track'])
        domain='audio' if kind in ('music','sound') else kind
        args=['draft',domain,'add','--ref',ref,'--track',str(track_count),
              '--start',time_arg(row['start_us'])]
        # Explicit duration on music add enters a broken remote loop finalizer
        # in 11.5. Add its native ID first, then use the native time-range edit.
        if kind != 'music': args += ['--duration',time_arg(row['duration_us'])]
        if kind == 'text': args += ['--content',row['text']]
        else:
            resource='vimo://'+row['resource_id']
            args += ['--resource',resource]
            if domain=='audio': args += ['--kind',kind]
            if kind=='filter': args += ['--intensity',str(row['intensity'])]
        result=envelope(client,args,timeout=60)
        ids=result.get('segment_ids')
        if not isinstance(ids,list) or len(ids)!=1 or not isinstance(ids[0],str):
            raise PackagingFailure('native_add_did_not_return_one_segment')
        sid=ids[0]
        if kind=='music':
            envelope(client,['draft','segment','time-range','--ref',ref,'--seg-id',sid,
                '--duration',time_arg(row['duration_us'])])
        if kind in ('music','sound'):
            envelope(client,['draft','audio','volume','--ref',ref,'--seg-id',sid,'--linear',f"{row['volume']*100:g}%"])
        if kind in ('text','sticker'):
            envelope(client,['draft',kind,'translate','--ref',ref,'--seg-id',sid,'--x',str(row['x']),'--y',str(row['y'])])
        if kind=='text':
            style=['draft','text','style','--ref',ref,'--seg-id',sid,'--color',row['color'],
                '--size',str(row['size']),'--bold','true','--outline-color','#243D35','--outline-width','0.045']
            if re.fullmatch(r'[0-9]{1,32}',str(row['font_id'])):
                style += ['--font','vimo://'+row['font_id']]
            envelope(client,style,timeout=60)
    after=extract(client,draft)
    target=next((n for n in after if n.get('id')==sid),None)
    if not target: raise PackagingFailure('native_added_segment_missing')
    if kind=='transition':
        attrs=target.get('attributes',{})
        if (attrs.get('transition')!=row['resource_id'] or attrs.get('transition.duration.ms')!=row['duration_us'] or
                attrs.get('transition.overlap') is not True):
            raise PackagingFailure('native_transition_readback_mismatch')
    else:
        delta={n.get('id') for n in after if n.get('type')!='track'}-{n.get('id') for n in before if n.get('type')!='track'}
        timerange=target.get('timeline',{})
        expected_type='audio' if kind in ('music','sound') else kind
        if (delta!={sid} or target.get('type')!=expected_type or
                abs(timerange.get('start.ms')*1000-row['start_us'])>=1000 or
                abs(timerange.get('duration.ms')*1000-row['duration_us'])>=1000):
            raise PackagingFailure('native_add_readback_mismatch')
        if kind in ('sticker','effect','filter') and target.get('attributes',{}).get('source')!=row['resource_id']:
            raise PackagingFailure('native_resource_readback_mismatch')
        if kind=='filter' and not math.isclose(target.get('attributes',{}).get('intensity',-1),row['intensity'],abs_tol=1e-6):
            raise PackagingFailure('native_filter_intensity_mismatch')
    return {'key':row['key'],'kind':kind,'segment_id':sid,'native_object_observed':True,'extracted_nodes':after}


def run_steps(client,plan,out,journal):
    receipts={}
    previous_receipts=json.loads((out/'resources-verified.json').read_text(encoding='utf-8')) if (out/'resources-verified.json').is_file() else {}
    for row in plan['elements']:
        pair=resource_identity(row)
        if pair is None: continue
        kind,rid=pair
        identity=kind+'-'+rid
        if identity in receipts: continue
        rights=fresh_rights(client,kind,rid,plan['purpose'])
        folder=out/'resources'/identity
        cached=previous_receipts.get(identity)
        if cached and cached.get('file_inventory')=='extended_windows_paths_v1':
            alternative=Path(cached['_directory']).resolve(strict=True)
            if not alternative.is_relative_to((out/'resources').resolve()):
                raise PackagingFailure('cached_resource_receipt_outside_output')
            folder=alternative
        if (folder/'provenance.json').is_file():
            proof=json.loads((folder/'provenance.json').read_text(encoding='utf-8'))
            if (proof.get('status')!='downloaded_nonmember_resource_needs_application_review' or
                    proof.get('file_inventory')!='extended_windows_paths_v1'):
                # A partial download is never promoted to a resource. Fresh attempt.
                folder=out/'resources'/(identity+'-'+uuid.uuid4().hex[:6])
                proof=acquire(client,kind,rid,folder)
        else:
            if folder.exists():folder=out/'resources'/(identity+'-'+uuid.uuid4().hex[:6])
            proof=acquire(client,kind,rid,folder)
        proof={**proof,'_directory':str(folder),'current_entitlement':rights}
        if proof['resource_id']!=rid or proof['resource_type']!=kind:
            raise PackagingFailure('cached_receipt_identity_mismatch')
        if plan['purpose']=='commercial' and proof['entitlement']['commercial_field_value'] is not True:
            raise PackagingFailure('download_receipt_lacks_commercial_metadata')
        verify_files(proof);receipts[identity]=proof
    atomic_json(out/'resources-verified.json',receipts)
    if plan.get('separate_source_audio') and 'narration_draft' not in journal:
        source=Path(journal['base'])
        if digest(source/'draft_content.json')!=journal['base_sha256']:
            raise PackagingFailure('checkpoint_changed_before_narration')
        attempt=out/('narration-'+uuid.uuid4().hex[:8]);draft=attempt/'draft'
        shutil.copytree(source,draft);ref='local://'+draft.as_posix()
        videos=[n for n in extract(client,draft) if n.get('type')=='video']
        records=[]
        for video in videos:
            before={n.get('id') for n in extract(client,draft)}
            response=envelope(client,['draft','video','extract-music','--ref',ref,'--seg-id',video['id']],timeout=60)
            after=extract(client,draft)
            added=[n for n in after if n.get('id') not in before and n.get('type')=='audio']
            if len(added)!=1 or added[0]['timeline']!=video['timeline']:
                raise PackagingFailure('native_narration_time_mapping_mismatch')
            gain=video.get('attributes',{}).get('volume',1)
            envelope(client,['draft','audio','volume','--ref',ref,'--seg-id',added[0]['id'],'--linear',f'{gain*100:g}%'])
            envelope(client,['draft','video','volume','--ref',ref,'--seg-id',video['id'],'--linear','0%'])
            records.append({'source_segment_id':video['id'],'segment_id':added[0]['id'],
                'timeline':video['timeline'],'target_timerange':journal['source_video_ranges'][video['id']],
                'volume':gain,'native_response':response})
        journal.update(narration_draft=str(draft),narration_sha256=digest(draft/'draft_content.json'),narration=records)
        atomic_json(out/'journal.json',journal)
    for index,row in enumerate(plan['elements']):
        if index<len(journal['steps']):
            record=journal['steps'][index]
            if record['key']!=row['key'] or digest(Path(record['draft'])/'draft_content.json')!=record['sha256']:
                raise PackagingFailure('committed_checkpoint_changed')
            continue
        last=Path(journal['steps'][-1]['draft']) if journal['steps'] else Path(journal.get('narration_draft',journal['base']))
        if digest(last/'draft_content.json')!=(journal['steps'][-1]['sha256'] if journal['steps'] else journal.get('narration_sha256',journal['base_sha256'])):
            raise PackagingFailure('checkpoint_changed_before_copy')
        attempt=out/'steps'/f'{index+1:02d}-{row["key"]}-{uuid.uuid4().hex[:8]}'
        draft=attempt/'draft';shutil.copytree(last,draft)
        atomic_json(attempt/'intent.json',{'element':row,'previous_checkpoint':str(last)})
        proof=receipts.get(row['kind']+'-'+row.get('resource_id',''))
        result=apply_element(client,draft,row,proof)
        atomic_json(attempt/'native-result.json',result)
        record={k:result[k] for k in ('key','kind','segment_id','native_object_observed')}
        record.update(draft=str(draft),sha256=digest(draft/'draft_content.json'))
        journal['steps'].append(record)
        atomic_json(out/'journal.json',journal)
    return receipts


def assert_retains(source,result,path=''):
    if isinstance(source,dict):
        for key,value in source.items():
            if path=='' and key=='is_minimal_draft' and key not in result:continue
            if key not in result:raise PackagingFailure('native_removed_field:'+path+'/'+key)
            assert_retains(value,result[key],path+'/'+key)
    elif isinstance(source,list):
        if len(source)!=len(result):raise PackagingFailure('native_changed_list:'+path)
        for i,(a,b) in enumerate(zip(source,result)):assert_retains(a,b,path+'/'+str(i))
    elif source!=result:raise PackagingFailure('native_changed_value:'+path)


def read_native(svc,draft):
    return json.loads(svc.request('draft','readFile',{'path':str(draft/'draft_content.json'),
        'rootPath':str(draft)})['result']['content'])


def write_native(svc,path,value):
    svc.request('draft','writeFile',{'path':str(path),'rootPath':str(path.parent),
        'content':json.dumps(value,ensure_ascii=False)})
    back=svc.request('draft','readFile',{'path':str(path),'rootPath':str(path.parent)})['result']
    if not back.get('encrypted') or json.loads(back['content'])!=value:
        raise PackagingFailure('native_encrypted_roundtrip_mismatch')


def validate_completed(out):
    """Read-only validation for operation reuse; never starts an app or edits."""
    out=Path(out)
    report=json.loads((out/'packaging-report.json').read_text(encoding='utf-8'))
    journal=json.loads((out/'journal.json').read_text(encoding='utf-8'))
    if report.get('status')!='native_packaging_ready_for_review':raise PackagingFailure('packaging_not_complete')
    if (report.get('implementation_sha256')!=IMPLEMENTATION_SHA256 or digest(Path(__file__))!=IMPLEMENTATION_SHA256):
        raise PackagingFailure('packaging_implementation_changed')
    for media in journal['source_media']:
        if digest(Path(media['path']))!=media['sha256']:raise PackagingFailure('source_media_changed')
    if digest(Path(report['draft'])/'draft_content.json')!=report['draft_sha256']:
        raise PackagingFailure('completed_draft_changed')
    for proof in json.loads((out/'resources-verified.json').read_text(encoding='utf-8')).values():verify_files(proof)
    for file in report.get('narration_files',[]):
        if digest(Path(file['path']))!=file['sha256']:raise PackagingFailure('native_narration_file_changed')
    for frame in report['frames']:
        if digest(Path(frame['path']))!=frame['sha256']:raise PackagingFailure('preview_evidence_changed')
    if report['audio_report'] and digest(Path(report['audio_report']))!=report['audio_report_sha256']:
        raise PackagingFailure('audio_evidence_changed')
    return report


def fit_native_text(row,font_path,canvas):
    """Conservative measured layout for the pinned 11.5 text coordinate system.

    Native font points scale with canvas height / 320. Measure the actual
    selected font with extra stroke padding; final native pixels remain the
    authority. This changes layout only, never text, fonts or resource choices.
    """
    from PIL import ImageFont
    width,height=canvas['width'],canvas['height']
    result={k:row[k] for k in ('size','x','y')}
    def extent(size):
        pixels=max(1,math.ceil(size*height/320))
        font=ImageFont.truetype(str(font_path),pixels)
        stroke=math.ceil(pixels*.08)+2
        box=font.getbbox(row['text'],stroke_width=stroke)
        return (box[2]-box[0])*1.05,(box[3]-box[1])*1.05
    for _ in range(3):
        tw,th=extent(result['size'])
        if tw<=width*.80:break
        result['size']=math.floor(result['size']*(width*.80)/tw)
        if result['size']<8:raise PackagingFailure('native_text_needs_shorter_copy:'+row['key'])
    tw,th=extent(result['size'])
    if tw>width*.80:raise PackagingFailure('native_text_width_unresolved:'+row['key'])
    for axis,limit in (('x',max(0,1-.16-tw/width)),('y',max(0,1-.12-th/height))):
        result[axis]=round(max(-limit,min(limit,result[axis])),6)
    return result


def finalize(install,plan,out,journal,receipts):
    Service,check_references,rebase=native_modules()
    from native_build import sidecar_metadata
    from native_service import validate_player_state
    import time
    last=Path(journal['steps'][-1]['draft'])
    attempt=out/('final-'+uuid.uuid4().hex[:8]);attempt.mkdir()
    draft=attempt/'draft';draft.mkdir()
    applied_elements=copy.deepcopy(plan['elements']);adjustments=[]
    with Service(install,attempt/'service') as svc:
        snap=read_native(svc,last)
        original=read_native(svc,Path(journal['base']))
        # Lyra persists a sparse protobuf JSON; expand both snapshots through
        # the native engine before comparing unchanged baseline semantics.
        # Loading a draft normalizes the no-op sound mapping enum from an
        # empty string to ``none``. Normalize the expected snapshots first so
        # this native representation change is compared explicitly.
        def normalize_sound_mappings(value):
            value = copy.deepcopy(value)
            for material in value.get('materials', {}).get('sound_channel_mappings', []):
                if material.get('type') == '':
                    material['type'] = 'none'
            return value
        snap = normalize_sound_mappings(snap)
        original = normalize_sound_mappings(original)
        sid=svc.request('player','create',{'kind':'draft','serviceGeneration':1})['result']['sessionId']
        try:
            svc.request('player','load',{'draftPath':str(last/'draft_content.json'),'workspacePath':str(attempt)},sid)
            normalized=svc.request('edit','draft.setDraft',{'draftSnapshot':snap},sid)['result']['draftSnapshot']
            assert_retains(snap,normalized);snap=normalized
            normalized=svc.request('edit','draft.setDraft',{'draftSnapshot':original},sid)['result']['draftSnapshot']
            assert_retains(original,normalized);original=normalized
        finally:svc.request('player','destroy',{},sid)
        old_transition_ids={m['id'] for m in original['materials'].get('transitions',[])}
        new_transition_ids={m['id'] for m in snap['materials'].get('transitions',[])}-old_transition_ids
        current_tracks={t['id']:t for t in snap['tracks']}
        separated={r['source_segment_id']:r for r in journal.get('narration',[])}
        for old in original['tracks']:
            current=copy.deepcopy(current_tracks.get(old['id'],{}))
            old_segments={s['id']:s for s in old['segments']}
            sequential_text=(old.get('type')=='text' and len({s.get('render_index') for s in old['segments']})==1
                and all(a['target_timerange'].get('start',0)+a['target_timerange']['duration']<=b['target_timerange'].get('start',0)
                        for a,b in zip(old['segments'],old['segments'][1:])))
            for index,seg in enumerate(current.get('segments',[])):
                seg['extra_material_refs']=[r for r in seg.get('extra_material_refs',[]) if r not in new_transition_ids]
                # Lyra numbers sequential non-overlapping headings 14000+n;
                # the SDK template initially gave every heading 14000.
                # Permit only this exact native normalization, not z-order loss.
                if sequential_text and seg['id'] in old_segments:
                    previous_index=old_segments[seg['id']]['render_index']
                    if seg.get('render_index')==previous_index+index:seg['render_index']=previous_index
                if seg['id'] in separated:
                    if seg.get('volume',1)!=0:raise PackagingFailure('double_source_audio')
                    seg['volume']=separated[seg['id']]['volume']
            assert_retains(old,current)
        for group,values in original['materials'].items():
            if not isinstance(values,list):continue
            by_id={m['id']:m for m in snap['materials'].get(group,[]) if isinstance(m,dict) and m.get('id')}
            for old in values:
                if isinstance(old,dict) and old.get('id'):assert_retains(old,by_id.get(old['id'],{}))
        segments={s['id']:s for t in snap['tracks'] for s in t['segments']}
        materials={m['id']:m for group in snap['materials'].values() if isinstance(group,list)
                   for m in group if isinstance(m,dict) and m.get('id')}
        for record in journal.get('narration',[]):
            seg=segments[record['segment_id']];mat=materials[seg['material_id']]
            target=seg['target_timerange'];expected=record['target_timerange']
            if (target.get('start',0)!=expected.get('start',0) or target['duration']!=expected['duration']
                    or not math.isclose(seg.get('volume',1),record['volume'],abs_tol=1e-6)
                    or mat.get('type')!='extract_music' or not Path(mat['path']).is_file()):
                raise PackagingFailure('separated_narration_snapshot_mismatch')
            original_segment=next(s for t in original['tracks'] for s in t['segments'] if s['id']==record['source_segment_id'])
            if seg['source_timerange']!=original_segment['source_timerange']:
                raise PackagingFailure('native_narration_source_mapping_changed')
            from pymediainfo import MediaInfo
            audio_tracks=[t for t in MediaInfo.parse(mat['path']).tracks if t.track_type=='Audio']
            if len(audio_tracks)!=1 or not audio_tracks[0].duration:
                raise PackagingFailure('native_narration_duration_unknown')
            measured=round(float(audio_tracks[0].duration)*1000)
            source_range=seg['source_timerange']
            if source_range.get('start',0)+source_range['duration']>measured:
                raise PackagingFailure('native_narration_actual_source_range_exceeded')
            # extract-music caches the full source audio but writes the selected
            # segment length into material.duration. Correct it from the file.
            mat['duration']=measured
        for row,record in zip(applied_elements,journal['steps']):
            seg=segments[record['segment_id']];kind=row['kind']
            if kind=='transition':
                matches=[m for m in snap['materials']['transitions'] if m['id'] in seg.get('extra_material_refs',[])
                         and m.get('resource_id')==row['resource_id']]
                if len(matches)!=1 or matches[0]['duration']!=row['duration_us']:
                    raise PackagingFailure('transition_snapshot_mismatch')
                mat=matches[0]
            else:
                mat=materials[seg['material_id']]
                timerange=seg['target_timerange']
                actual_start=timerange.get('start',0);actual_duration=timerange['duration']
                if abs(actual_start-row['start_us'])>1 or abs(actual_duration-row['duration_us'])>1:
                    raise PackagingFailure('native_timing_changed:'+row['key'])
                if actual_start!=row['start_us'] or actual_duration!=row['duration_us']:
                    adjustments.append({'key':row['key'],'requested_start_us':row['start_us'],
                        'requested_duration_us':row['duration_us'],'applied_start_us':actual_start,
                        'applied_duration_us':actual_duration,'reason':'native_frame_endpoint_rounding_max_one_microsecond'})
                    row.update(start_us=actual_start,duration_us=actual_duration)
            if kind=='text':
                rich=json.loads(mat['content'])
                if resource_identity(row):
                    proof=receipts['font-'+row['font_id']];verify_files(proof)
                    candidates=[Path(proof['_directory'])/f['path'] for f in proof['files']
                        if Path(f['path']).suffix.lower() in ('.ttf','.otf')]
                    if len(candidates)!=1:raise PackagingFailure('official_font_payload_ambiguous')
                    font={'path':candidates[0].as_posix(),'sha256':digest(candidates[0]),
                          'family':proof['source_metadata']['title']}
                    if any(str(style.get('font',{}).get('id'))!=row['font_id'] for style in rich['styles']):
                        raise PackagingFailure('official_font_identity_mismatch')
                else:font=resolve_font(install,row['font_id'])
                if rich['text']!=row['text']:raise PackagingFailure('native_text_changed')
                validate_text(font,rich['text'])
                if any(not math.isclose(seg['clip']['transform'][k],row[k],abs_tol=1e-6) for k in ('x','y')):
                    raise PackagingFailure('native_position_mismatch')
                requested={k:row[k] for k in ('size','x','y')}
                fitted=fit_native_text(row,font['path'],snap['canvas_config'])
                if fitted!=requested:
                    adjustments.append({'key':row['key'],'requested_layout':requested,
                        'applied_layout':fitted,'reason':'measured_font_safe_canvas_bounds'})
                    row.update(fitted)
                    seg['clip']['transform'].update({k:fitted[k] for k in ('x','y')})
                mat.update(font_path=font['path'],font_name=font['family'],font_size=row['size'])
                for style in rich['styles']:style['font']['path']=font['path'];style['size']=row['size']
                mat['content']=json.dumps(rich,ensure_ascii=False)
            else:
                proof=receipts[kind+'-'+row['resource_id']];verify_files(proof)
                if str(mat.get('resource_id'))!=row['resource_id']:
                    raise PackagingFailure('resource_snapshot_identity_mismatch')
                mat['path']=Path(proof['path']).as_posix()
                if kind in ('music','sound'):
                    if mat.get('type')!=kind or not math.isclose(seg.get('volume',1),row['volume'],abs_tol=1e-6):
                        raise PackagingFailure('native_audio_type_or_gain_mismatch')
                    from pymediainfo import MediaInfo
                    tracks=[t for t in MediaInfo.parse(mat['path']).tracks if t.track_type=='Audio']
                    if len(tracks)!=1 or not tracks[0].duration:raise PackagingFailure('official_audio_duration_unknown')
                    length_us=round(float(tracks[0].duration)*1000)
                    if length_us < row['duration_us']:
                        if kind!='sound':raise PackagingFailure('audio_resource_too_short_for_nonlooping_segment')
                        frames=length_us*snap['fps']//1_000_000
                        first_frame=round(row['start_us']*snap['fps']/1_000_000)
                        actual_duration=(first_frame+frames)*1_000_000//snap['fps']-row['start_us']
                        if actual_duration<round(1_000_000/snap['fps']):
                            raise PackagingFailure('sound_resource_shorter_than_one_frame')
                        adjustments.append({'key':row['key'],'requested_duration_us':row['duration_us'],
                            'applied_duration_us':actual_duration,'measured_resource_duration_us':length_us,
                            'reason':'one_shot_sound_limited_to_verified_resource_duration'})
                        row['duration_us']=actual_duration
                        seg['target_timerange']['duration']=actual_duration
                        seg['source_timerange']['duration']=actual_duration
                    mat['duration']=length_us
            if kind in ('text','sticker'):
                if any(not math.isclose(seg['clip']['transform'][k],row[k],abs_tol=1e-6) for k in ('x','y')):
                    raise PackagingFailure('native_position_mismatch')
        snap=rebase(snap,last,draft,snap['id'],str(uuid.uuid4()).upper())
        # Group non-overlapping native objects into readable editable lanes.
        groups={}
        for record in journal.get('narration',[]):groups[record['segment_id']]='原素材口播'
        names={'music':'背景音乐','sound':'节点音效','sticker':'剪映贴纸','text':'个性文案','effect':'画面特效','filter':'滤镜'}
        for row,record in zip(plan['elements'],journal['steps']):
            if row['kind'] in names:groups[record['segment_id']]=names[row['kind']]
        lanes=[]
        for track in snap['tracks']:
            label=groups.get(track['segments'][0]['id']) if track.get('segments') else None
            if label and all(groups.get(s['id'])==label for s in track['segments']):
                track.update(name=label,is_default_name=False)
                destination=next((t for t in lanes if t.get('name')==label and all(
                    a['target_timerange'].get('start',0)+a['target_timerange']['duration']<=b['target_timerange'].get('start',0)
                    or b['target_timerange'].get('start',0)+b['target_timerange']['duration']<=a['target_timerange'].get('start',0)
                    for a in t['segments'] for b in track['segments'])),None)
                if destination is not None:
                    destination['segments'].extend(track['segments']);destination['segments'].sort(key=lambda s:s['target_timerange'].get('start',0));continue
            lanes.append(track)
        snap['tracks']=lanes
        check_references(snap);write_native(svc,attempt/'seed.json',snap)
        sid=svc.request('player','create',{'kind':'draft','serviceGeneration':1})['result']['sessionId']
        try:
            svc.request('player','load',{'draftPath':str(attempt/'seed.json'),'workspacePath':str(attempt)},sid)
            result=svc.request('edit','draft.setDraft',{'draftSnapshot':snap},sid)['result']['draftSnapshot']
            assert_retains(snap,result);check_references(result)
            write_native(svc,draft/'draft_content.json',result)
            write_native(svc,draft/'draft_meta_info.json',sidecar_metadata(result,draft,out,time.time_ns()//1000))
        finally:svc.request('player','destroy',{},sid)
    with Service(install,attempt/'reload-service') as svc:
        if read_native(svc,draft)!=result:raise PackagingFailure('cold_readback_mismatch')
        sid=svc.request('player','create',{'kind':'draft','serviceGeneration':2})['result']['sessionId']
        try:
            svc.request('player','load',{'draftPath':str(draft/'draft_content.json'),'workspacePath':str(attempt)},sid)
            validate_player_state(svc.request('player','getState',{},sid)['result'],result['duration'],result['canvas_config'])
        finally:svc.request('player','destroy',{},sid)
    for name in ('draft_content.json','draft_meta_info.json'):shutil.copyfile(draft/name,draft/(name+'.bak'))
    shutil.copyfile(draft/'draft_content.json',draft/'template-2.tmp')
    atomic_json(attempt/'snapshot.json',result)
    from native_preview import preview_batches
    positions={0,result['duration']-round(1_000_000/result['fps'])}
    segments={s['id']:s for t in result['tracks'] for s in t['segments']}
    for row,record in zip(plan['elements'],journal['steps']):
        if row['kind']=='transition':
            t=segments[record['segment_id']]['target_timerange'];positions.add(t.get('start',0)+t['duration'])
        else:positions.add(row['start_us']+row['duration_us']//2)
    frames=preview_batches(install,draft,attempt/'preview',sorted(positions),360)['frames']
    audio=None
    if any(r['kind'] in ('music','sound') for r in plan['elements']):
        from native_audio_scan import scan
        audio=scan(install,draft,attempt/'audio',60_000_000)
        if not audio['full_duration_signal_sampling_complete'] or audio['zero_signal_windows_needing_review']:
            raise PackagingFailure('native_audio_signal_requires_review')
    return {'status':'native_packaging_ready_for_review','draft':str(draft),'draft_sha256':digest(draft/'draft_content.json'),
        'duration_us':result['duration'],'track_count':len(result['tracks']),'native_saved_and_cold_reloaded':True,
        'existing_timeline_preserved_except_requested_transitions':True,
        'source_audio_mode':'separated_by_native_cli' if journal.get('narration') else 'preserved_embedded',
        'narration_segments':[{k:r[k] for k in ('source_segment_id','segment_id','timeline','target_timerange','volume')} for r in journal.get('narration',[])],
        'narration_files':[{'path':m['path'],'sha256':digest(Path(m['path']))} for m in result['materials'].get('audios',[])
                           if m.get('type')=='extract_music'],
        'applied_elements':applied_elements,'execution_adjustments':adjustments,
        'frames':frames,'audio_report':str(attempt/'audio/audio-scan-report.json') if audio else None,
        'audio_report_sha256':digest(attempt/'audio/audio-scan-report.json') if audio else None,
        'publishing_scope_verified':False,'continuous_av_review_passed':False,'delivery_accepted':False}


def package(install,source,plan,out,*,allow_isolated_host=False,user_data=None,resume=False):
    if allow_isolated_host is not True:raise PackagingFailure('isolated_host_opt_in_required')
    install=Path(install).resolve(strict=True);source=Path(source).resolve(strict=True);out=Path(out).resolve()
    if (not (source/'draft_content.json').is_file() or out.is_relative_to(source) or source.is_relative_to(out) or
            out.is_relative_to(install) or not str(out).isascii() or len(str(out))>105):
        raise PackagingFailure('use_new_short_ascii_output_outside_source_and_install')
    if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in source.rglob('*')):
        raise PackagingFailure('linked_draft_files_unsupported')
    source_hash=digest(source/'draft_content.json')
    binding=hashlib.sha256(json.dumps([source.as_posix(),source_hash,plan],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
    if out.exists() and not resume:raise PackagingFailure('existing_packaging_output_requires_resume')
    out.mkdir(parents=True,exist_ok=True)
    with process_lock(out/'active.lock'):
        return _package_locked(install,source,plan,out,binding,source_hash,user_data)


def _package_locked(install,source,plan,out,binding,source_hash,user_data):
    Service,check_references,rebase=native_modules()
    journal_path=out/'journal.json'
    if journal_path.exists():
        journal=json.loads(journal_path.read_text(encoding='utf-8'))
        if journal['binding']!=binding:raise PackagingFailure('resume_inputs_changed')
        for media in journal['source_media']:
            if digest(Path(media['path']))!=media['sha256']:raise PackagingFailure('source_media_changed')
        if (out/'packaging-report.json').is_file():
            report=json.loads((out/'packaging-report.json').read_text(encoding='utf-8'))
            if report['status']=='native_packaging_ready_for_review':
                if report.get('implementation_sha256')==IMPLEMENTATION_SHA256:
                    return validate_completed(out)
    else:
        prep=out/('prepare-'+uuid.uuid4().hex[:8])
        with Service(install,prep) as svc:
            snap=read_native(svc,source);validate_plan(plan,snap,install);check_references(snap)
            source_media=[]
            for kind in ('videos','audios'):
                for material in snap['materials'].get(kind,[]):
                    if material.get('path'):
                        path=Path(material['path']).resolve(strict=True)
                        if not path.is_file():raise PackagingFailure('source_media_not_regular_file')
                        source_media.append({'path':str(path),'sha256':digest(path)})
            base=out/('base-'+uuid.uuid4().hex[:8]);shutil.copytree(source,base)
            snap=rebase(snap,source,base,snap['id'],str(uuid.uuid4()).upper())
            write_native(svc,base/'draft_content.json',snap)
        journal={'schema':SCHEMA,'binding':binding,'source_sha256':source_hash,
                 'source_media':source_media,'base':str(base),'base_sha256':digest(base/'draft_content.json'),'steps':[],
                 'source_video_ranges':{s['id']:s['target_timerange'] for t in snap['tracks'] if t['type']=='video' for s in t['segments']}}
        atomic_json(journal_path,journal);atomic_json(out/'plan.json',plan)
    report={'status':'in_progress','delivery_accepted':False,'source_preserved':True}
    try:
        from .native_host import IsolatedNativeHost
        from .native_cli import NativeCLI
        for attempt in range(2):
            host_out=out/('host-'+uuid.uuid4().hex[:8])
            try:
                with IsolatedNativeHost(install,host_out,allow_isolated_host=True,user_data=user_data,lifetime_timeout=600) as host:
                    with NativeCLI(host) as client:receipts=run_steps(client,plan,out,journal)
                break
            except (subprocess.TimeoutExpired,TimeoutError,ConnectionError):
                if attempt:raise
                journal.setdefault('recoveries',[]).append({'reason':'transport_timeout_or_disconnect',
                    'committed_elements':len(journal['steps']),'discard_uncommitted_candidate':True})
                atomic_json(journal_path,journal)
        report=finalize(install,plan,out,journal,receipts)
        if digest(Path(__file__))!=IMPLEMENTATION_SHA256:raise PackagingFailure('packaging_implementation_changed_during_run')
        report.update(host_report=str(host_out/'host-lifecycle.json'),host_cleanup=host.report['status'],
                      homepage_index_unchanged=host.report['index_unchanged'],implementation_sha256=IMPLEMENTATION_SHA256)
    except Exception as exc:
        report.update(status='needs_resume_or_plan_revision',error_type=type(exc).__name__,
                      reason=str(exc) if isinstance(exc,(PackagingFailure,ResourceFailure)) else 'native_operation_failed',
                      committed_elements=len(journal['steps']))
        atomic_json(out/'failures'/(uuid.uuid4().hex+'.json'),report)
        raise
    finally:
        try:
            report['source_preserved']=digest(source/'draft_content.json')==source_hash and all(
                digest(Path(m['path']))==m['sha256'] for m in journal['source_media'])
        except OSError:report['source_preserved']=False
        if not report['source_preserved']:report.update(status='input_changed',delivery_accepted=False)
        atomic_json(out/'packaging-report.json',report)
    if not report['source_preserved']:raise PackagingFailure('source_draft_changed')
    return report
