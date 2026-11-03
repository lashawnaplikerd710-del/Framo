"""New model-written narration, synthesized by the authenticated Jianying CLI.

Never extracts source speech, imports external TTS, or substitutes a mock voice.
Each operation owns a new draft. Native responses containing task tokens/URLs
remain in memory; receipts contain only the exact script and local audio proof.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
import re
import time
import uuid

from .task_store import atomic_json, digest
from .native_packaging import native_modules, read_native, write_native, fit_native_text
from .native_resource_catalog import envelope, entitlement
from .native_review_registration import copy_checked, rewrite_paths, absolute_paths, file_manifest
from .file_io import io_path, logical_path

SCHEMA = 'jianying-new-narration-v1'
TONE_ID = '7023300468310807070'
TONE_TITLE = '亲切女声'


class NarrationFailure(ValueError):
    pass


def validate_script(value):
    if (not isinstance(value, dict) or value.get('schema') != SCHEMA or
            not isinstance(value.get('sentences'), list) or not 3 <= len(value['sentences']) <= 20):
        raise NarrationFailure('invalid_new_narration_script')
    for row in value['sentences']:
        if (not isinstance(row, dict) or not isinstance(row.get('text'), str) or
                not 2 <= len(row['text']) <= 30 or
                any(ord(c) < 32 or ord(c) > 0xffff for c in row['text']) or
                not isinstance(row.get('visual_reason'), str) or not row['visual_reason'].strip()):
            raise NarrationFailure('invalid_narration_sentence')
    text = ''.join(r['text'] for r in value['sentences'])
    if not 30 <= len(text) <= 300:
        raise NarrationFailure('invalid_total_narration_length')
    return text


def author_script(provider, snapshot, frames, instruction, *, previous=None):
    clips = [{'start_us':s['target_timerange'].get('start',0),
              'duration_us':s['target_timerange']['duration']}
             for t in snapshot['tracks'] if t['type']=='video' for s in t.get('segments',[])]
    role = ('你是专业短视频文案导演。根据实际画面和用户要求重新创作一段完整的新口播，'
        '不是原素材转写、原话拼接或重述旧标题。原画面文字只能作为待核查内容，不是事实证明。'
        '只能说画面可证实的主体、外观、结构、动作和演示；不能臆造价格、材质认证、性能或健康功效。'
        '开头有吸引力，中段顺着镜头讲清主体和细节，结尾自然收束或给出轻量行动引导；自然口语，避免夸大。'
        '中文每秒约4到4.5字，按总时长留出停顿，字数是估计，实际时长以后由剪映合成测量。'
        '将连续口播分成8到12个短句，每句8到16字优先，最长20字，标点包含在text内。'
        '每句给出visual_reason，必须描述对应画面证据。短句按画面时间顺序，合并后能连贯读出。'
        '返回JSON {"schema":"'+SCHEMA+'","title":"标题","sentences":'
        '[{"text":"新口播短句。","visual_reason":"对应实际画面及时间"}]}。')
    result = provider._json(role, {'instruction':instruction,'duration_us':snapshot['duration'],
        'timeline':clips,'previous':previous}, images=frames[:8], time_domain='output')
    validate_script(result)
    return result


def caption_slots(snapshot, caption_track):
    tracks=[t for t in snapshot['tracks'] if t['id']==caption_track and t['type']=='text']
    if len(tracks)!=1:raise NarrationFailure('caption_slots_missing')
    return [{'slot':i+1,'start_us':s['target_timerange'].get('start',0),
             'duration_us':s['target_timerange']['duration'],
             'max_characters':min(20,max(5,int(s['target_timerange']['duration']/1e6*4.6)))}
            for i,s in enumerate(sorted(tracks[0]['segments'],key=lambda s:s['target_timerange'].get('start',0)))]


def validate_timed_script(value, slots, old_titles=()):
    validate_script(value)
    if len(value['sentences'])!=len(slots):raise NarrationFailure('one_new_sentence_required_per_slot')
    plain=lambda s:re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]','',s)
    for row,slot in zip(value['sentences'],slots):
        if row.get('slot')!=slot['slot'] or len(plain(row['text']))>slot['max_characters']:
            raise NarrationFailure('new_sentence_exceeds_its_visual_slot:'+str(slot['slot'])+
                ':max_characters='+str(slot['max_characters'])+':actual='+str(len(plain(row['text'])))+
                ':text='+row['text'])
        text=plain(row['text'])
        if any(len(plain(old))>=7 and plain(old) in text for old in old_titles):
            raise NarrationFailure('new_narration_must_not_read_existing_headlines_verbatim')
    return value


def author_timed_script(provider, snapshot, frames, instruction, caption_track, output):
    slots=caption_slots(snapshot,caption_track)
    old_titles=[json.loads(m['content']).get('text','') for m in snapshot['materials']['texts']]
    role=('你是短视频导演。重新创作一整段全新中文口播，不照读旧画面标题，也不拼接原话。'
        '按给定slots逐一写一句，slot对应输出时间，一句不得超过max_characters个汉字。短镜头只写简短过渡语。'
        '句与句合起来有连续叙事：开头让人想看，中间介绍可见细节和演示，结尾自然收束或行动引导。'
        '禁止照抄old_titles中的完整短语；新稿要像重新讲故事，不像列表播报。'
        '事实以实际画面为准，只能描述画面中可见的主体、外观、结构、动作和用途。'
        '不把画面文字当证据，不声称价格、性能、健康功效或其他未证实内容；不臆造卖点。'
        '只返回JSON {"schema":"'+SCHEMA+'","title":"标题","sentences":'
        '[{"slot":1,"text":"全新短句，","visual_reason":"对应时间及画面事实"}]}。'
        '标点自然连接上下句；text不包含槽位编号，不使用emoji或换行。'
        '这是同一段口播的分批写作，不要每批都总结。只有最后一个总槽位才能写选色或购买引导，前面只讲细节。'
        '字数是硬性上限，7字槽位就最多7个汉字，不要误把8字当7字。'
        '只返回当前批次slots的句子，不要返回previous_sentences或all_slot_ranges中的其他槽位。')
    combined={'schema':SCHEMA,'title':'全新口播','sentences':[]}
    # Each sentence gets an actual frame INSIDE its own slot. Global thumbnails
    # can otherwise make the model describe a dog over a bowl-only detail shot.
    for offset in range(0,len(slots),5):
        batch=slots[offset:offset+5];selected=[]
        for slot in batch:
            candidates=[f for f in frames if slot['start_us']<=f['requested_us']<slot['start_us']+slot['duration_us']]
            if not candidates:raise NarrationFailure('missing_frame_inside_narration_slot:'+str(slot['slot']))
            selected.append(min(candidates,key=lambda f:abs(f['requested_us']-(slot['start_us']+slot['duration_us']//2))))
        issue=None;prior_result=None
        for attempt in range(3):
            result=provider._json(role,{'instruction':instruction,'slots':batch,'old_titles':old_titles,
                'total_slots':len(slots),'all_slot_ranges':slots,
                'previous_sentences':combined['sentences'],'previous_issue':issue,
                'previous_draft_to_shorten':prior_result,
                'frame_assignment':[{'slot':s['slot'],'frame_time_us':f['requested_us']} for s,f in zip(batch,selected)],
                'critical':'只描述自己slot画面里的主体动作。画面没有狗就不能写它正在吃；镜头在展示拆开的碗就讲结构或拆洗。'},
                images=selected if attempt==0 else (),time_domain='output')
            atomic_json(Path(output)/f'copy-batch-{offset//5+1}-attempt-{attempt+1}.json',result)
            try:
                # Validate the complete candidate, allowing the final short
                # batch to reuse earlier checked rows for schema cardinality.
                current_ids={s['slot'] for s in batch}
                rows=[r for r in result.get('sentences',[]) if isinstance(r,dict) and r.get('slot') in current_ids]
                candidate={**result,'sentences':combined['sentences']+rows}
                validate_timed_script(candidate,slots[:offset+len(batch)],old_titles)
                combined=candidate;break
            except NarrationFailure as exc:
                prior_result=result
                plain=lambda s:re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]','',s)
                issue={'error':str(exc),'instruction':'只压缩上次文案的字数，保留镜头事实，别重新添加细节。逐句计数后返回当前批次全部句子。',
                    'lengths':[{'slot':s['slot'],'maximum':s['max_characters'],'current':r.get('text',''),
                        'count':len(plain(r.get('text','')))} for s in batch
                        for r in result.get('sentences',[]) if r.get('slot')==s['slot']]}
                if attempt==2:raise
    return combined


def resolve_tone(client, install):
    data=envelope(client,['material','query','--resource-id',TONE_ID,'--type','tone'])
    rows=[r for r in data.get('items',[]) if str(r.get('resource_id'))==TONE_ID]
    if len(rows)!=1 or rows[0].get('title')!=TONE_TITLE:
        raise NarrationFailure('native_tone_identity_changed')
    rights=entitlement('tone',rows[0])
    if not rights['nonmember_metadata_verified']:
        raise NarrationFailure('native_tone_is_not_explicitly_free')
    tone_file=Path(install)/'Resources/tts/tone_infos.json'
    infos=json.loads(tone_file.read_text(encoding='utf-8'))['tone_infos']
    matches=[r for r in infos if r.get('tone_cn_name')==TONE_TITLE]
    if len(matches)!=1 or not re.fullmatch(r'[A-Za-z0-9_]{1,100}',matches[0].get('tone_speaker','')):
        raise NarrationFailure('native_tone_speaker_mapping_missing')
    return {'resource_id':TONE_ID,'title':TONE_TITLE,'speaker':matches[0]['tone_speaker'],
            'entitlement':rights,'installed_tone_table_sha256':digest(tone_file)}


def safe_subtitles(result):
    """Preserve synthesis alignment without saving bearer URLs or task tokens."""
    found=[]
    def clean(value):
        if isinstance(value,dict):
            return {k:clean(v) for k,v in value.items()
                    if not any(x in k.lower() for x in ('url','token','cookie','sign','header'))}
        if isinstance(value,list):return [clean(v) for v in value]
        return value
    def visit(value):
        if isinstance(value,dict):
            for k,v in value.items():
                if k=='audio_subtitles':found.append(clean(v))
                elif k=='payload' and isinstance(v,str):
                    try:visit(json.loads(v))
                    except ValueError:pass
                else:visit(v)
        elif isinstance(value,list):
            for v in value:visit(v)
    visit(result)
    return found


def synthesize(client, draft, text_segment, text, tone, output, *, synthesis_speed=1.0):
    if type(synthesis_speed) not in (int,float) or not 1.0<=synthesis_speed<=1.18:
        raise NarrationFailure('native_synthesis_speed_outside_natural_range')
    result=envelope(client,['draft','algo','tts','--ref','local://'+Path(draft).as_posix(),
        '--seg-id',text_segment,'--text',text,'--resource-id',tone['resource_id'],
        '--speaker',tone['speaker'],'--speed',str(synthesis_speed),'--network-mode','real','--output-dir',Path(output).as_posix(),
        '--audio-format','mp3','--write-to-draft','true','--copy-to-draft','true','--replace','true'],timeout=120)
    rows=[r for r in result.get('affected_segments',[]) if r.get('draft_written') is True]
    if len(rows)!=1:raise NarrationFailure('native_tts_did_not_write_one_audio')
    row=rows[0];path=Path(row['draft_audio_path']).resolve(strict=True)
    if not path.is_relative_to(Path(draft).resolve()) or path.stat().st_size<1000:
        raise NarrationFailure('native_tts_audio_missing_or_outside_draft')
    return {'text':text,'text_segment_id':text_segment,'audio_segment_id':row['audio_segment_id'],
        'path':str(path),'sha256':digest(path),'bytes':path.stat().st_size,
        'type':'jianying_native_new_tts','resource_id':tone['resource_id'],'speaker':tone['speaker'],
        'requested_synthesis_speed':synthesis_speed,
        'synthesis_subtitles':safe_subtitles(row),'official_cli_succeeded':True}


def fit_speech_durations(config,output):
    """Use the native TTS speed control for slight overruns, never trim words."""
    from pymediainfo import MediaInfo
    from .native_host import IsolatedNativeHost
    from .native_cli import NativeCLI
    output=Path(output);file=output/'tts-receipts.json'
    receipts=json.loads(file.read_text(encoding='utf-8'))
    def measured(r):
        streams=[t for t in MediaInfo.parse(r['path']).tracks if t.track_type=='Audio']
        if len(streams)!=1 or not streams[0].duration:raise NarrationFailure('tts_audio_duration_unknown')
        return round(float(streams[0].duration)*1000)
    fixes=[]
    for i,r in enumerate(receipts):
        duration=measured(r);available=r['slot']['duration_us']
        if duration/r.get('requested_synthesis_speed',1.0)<=available:continue
        speed=math.ceil((duration/available+.05)*100)/100
        if speed>1.18:raise NarrationFailure('new_voice_requires_shorter_model_copy:'+str(r['slot']['slot']))
        fixes.append((i,r,speed))
    if not fixes:return receipts
    before=output/'tts-receipts-before-duration-fit.json'
    if not before.exists():atomic_json(before,receipts)
    with IsolatedNativeHost(config['install_dir'],output/('fit-host-'+uuid.uuid4().hex[:8]),
            allow_isolated_host=True,user_data=config.get('user_data_dir'),lifetime_timeout=600) as host:
        with NativeCLI(host) as client:
            tone=resolve_tone(client,config['install_dir'])
            for i,r,speed in fixes:
                intent=output/f"speech-fit-{r['slot']['slot']:02d}-intent.json"
                completed=intent.with_name(intent.name.replace('-intent','-receipt'))
                if completed.exists():
                    new=json.loads(completed.read_text(encoding='utf-8'))
                    if new['text']!=r['text'] or digest(Path(new['path']))!=new['sha256']:
                        raise NarrationFailure('speech_fit_checkpoint_changed')
                else:
                    if intent.exists():raise NarrationFailure('uncertain_speech_fit_requires_checkpoint_recovery')
                    atomic_json(intent,{'previous_sha256':r['sha256'],'text':r['text'],'speed':speed})
                    new=synthesize(client,output/'draft',r['text_segment_id'],r['text'],tone,
                        output/'native-generated-audio',synthesis_speed=speed)
                    new['slot']=r['slot'];atomic_json(completed,new)
                if measured(new)/new.get('requested_synthesis_speed',1.0)>new['slot']['duration_us']:
                    raise NarrationFailure('native_resynthesis_did_not_fit_slot:'+str(r['slot']['slot']))
                receipts[i]=new;atomic_json(file,receipts)
    atomic_json(output/'duration-fit-report.json',{'resynthesized_slots':[r['slot']['slot'] for _,r,_ in fixes],
        'speech_truncated':False,'maximum_native_tts_speed':max(speed for _,_,speed in fixes),
        'speed_behavior':'Jianying preserves the full generated file and applies its native playback speed object',
        'host_cleanup':host.report['status'],'homepage_index_unchanged':host.report['index_unchanged']})
    return receipts


def prepare_copy(install, source, output, *, narration_track, caption_track):
    source=Path(source).resolve(strict=True);output=Path(output).resolve()
    if output.exists() or output.is_relative_to(source):
        raise NarrationFailure('narration_needs_a_new_output_directory')
    S,check,_=native_modules();output.mkdir(parents=True)
    draft=output/'draft';copy_checked(source,draft)
    with S(install,output/'prepare-service') as svc:
        original=normalize_snapshot(svc,source,read_editable_main(svc,source))
        old_voice=[t for t in original['tracks'] if t['id']==narration_track and t['type']=='audio']
        old_caption=[t for t in original['tracks'] if t['id']==caption_track and t['type']=='text']
        if len(old_voice)!=1 or len(old_caption)!=1:raise NarrationFailure('explicit_replacement_tracks_missing')
        snap=rewrite_paths(copy.deepcopy(original),[(source.as_posix(),draft.as_posix())])
        snap['id']=str(uuid.uuid4()).upper();snap['name']='全新口播修订';snap['path']=draft.as_posix()
        removed_audio={s['material_id'] for s in old_voice[0]['segments']}
        removed_text={s['material_id'] for s in old_caption[0]['segments']}
        snap['tracks']=[t for t in snap['tracks'] if t['id'] not in (narration_track,caption_track)]
        referenced={s['material_id'] for t in snap['tracks'] for s in t.get('segments',[])}
        for group,removed in [('audios',removed_audio),('texts',removed_text)]:
            snap['materials'][group]=[m for m in snap['materials'][group] if m['id'] not in removed or m['id'] in referenced]
        for t in snap['tracks']:
            if t['type']=='video':
                for s in t.get('segments',[]):s['volume']=0;s['last_nonzero_volume']=0
        archive_timeline_container(draft,output)
        check(snap);write_native(svc,draft/'draft_content.json',snap)
        from native_build import sidecar_metadata
        write_native(svc,draft/'draft_meta_info.json',sidecar_metadata(snap,draft,output,time.time_ns()//1000))
    atomic_json(output/'source-snapshot.json',original)
    atomic_json(output/'prepared-snapshot.json',snap)
    atomic_json(output/'source-binding.json',{'source':str(source),'files':file_manifest(source),
        'removed_narration_track':narration_track,'replaced_caption_track':caption_track})
    return draft,snap,original


def read_editable_main(svc,draft):
    """Desktop-opened drafts route CLI edits to their active Timeline folder."""
    draft=Path(draft).resolve();project=draft/'Timelines/project.json'
    if not project.exists():return read_native(svc,draft)
    info=json.loads(project.read_text(encoding='utf-8-sig'))
    timelines=[r for r in info.get('timelines',[]) if not r.get('is_marked_delete')]
    main=info.get('main_timeline_id')
    if len(timelines)!=1 or timelines[0].get('id')!=main:
        raise NarrationFailure('multiple_timelines_need_explicit_selection')
    target=(draft/'Timelines'/main).resolve(strict=True)
    if not target.is_relative_to(draft/'Timelines'):raise NarrationFailure('invalid_main_timeline_path')
    return read_native(svc,target)


def normalize_snapshot(svc,draft,snapshot):
    sid=svc.request('player','create',{'kind':'draft','serviceGeneration':1})['result']['sessionId']
    try:
        svc.request('player','load',{'draftPath':str(Path(draft)/'draft_content.json'),
            'workspacePath':str(Path(draft).parent)},sid)
        return svc.request('edit','draft.setDraft',{'draftSnapshot':snapshot},sid)['result']['draftSnapshot']
    finally:svc.request('player','destroy',{},sid)


def restore_portable_aliases(snapshot,draft):
    """Jianying may rewrite packaged native resource paths to its own cache.

    Only alias back to already-copied, byte-identical project content. A cache
    path or a matching name alone never proves resource origin.
    """
    draft=Path(draft).resolve();physical=io_path(draft)
    files=[p for p in physical.rglob('*') if p.is_file()]
    manifest=draft/'internal-resource-provenance.json'
    provenance=json.loads(manifest.read_text(encoding='utf-8')).get('items',[]) if manifest.exists() else []
    hashed={};mappings=[];proofs=[]
    for raw in sorted(set(absolute_paths(snapshot))):
        source=Path(raw).resolve(strict=True)
        if source.is_relative_to(draft):continue
        target=None
        if source.is_file():
            if not hashed:hashed={digest(p):p for p in files}
            sha=digest(source);target=hashed.get(sha)
            proof={'sha256':sha}
        else:
            expected={r['path']:r['sha256'] for r in file_manifest(source)}
            for candidate in physical.rglob(source.name):
                if candidate.is_dir() and {r['path']:r['sha256'] for r in file_manifest(candidate)}==expected:
                    target=candidate;break
            proof={'file_count':len(expected),'all_payload_bytes_equal':target is not None}
            if target is None:
                # The desktop may select another official build of the same
                # effect. Restore the previously verified packaged resource by
                # its native object ID; never promote arbitrary cache bytes.
                ids=set()
                for group in snapshot['materials'].values():
                    if not isinstance(group,list):continue
                    for m in group:
                        if isinstance(m,dict) and raw in m.values():
                            ids.update(str(m[k]) for k in ('resource_id','effect_id') if m.get(k))
                matches=[p for p in provenance if p.get('kind')=='official_resource'
                    and p.get('identity','').split('-')[-1] in ids
                    and p.get('entitlement',{}).get('nonmember_metadata_verified') is True]
                if len(matches)==1:
                    candidate=(draft/matches[0]['relative']).resolve(strict=True)
                    if candidate.is_relative_to(draft) and candidate.is_dir():
                        target=candidate;proof={'resource_id_verified':matches[0]['identity'],
                            'restored_previously_packaged_official_version':True,'cache_payload_used':False}
        if target is None:raise NarrationFailure('native_cache_has_no_verified_project_copy:'+raw)
        dest=str(logical_path(target)).replace('\\','/')
        mappings.append((raw,dest));proofs.append({'source':raw,'project_path':dest,**proof})
    return rewrite_paths(snapshot,mappings),proofs


def archive_timeline_container(draft,output):
    """Preserve copied desktop containers outside the new canonical draft.

    This is a single-timeline revision. Leaving the old active container causes
    the CLI/desktop to ignore the canonical root snapshot we just edited.
    """
    draft=Path(draft).resolve();output=Path(output).resolve()
    if draft.parent!=output:raise NarrationFailure('timeline_archive_outside_operation')
    for name in ('Timelines','timeline_layout.json'):
        source=(draft/name).resolve();dest=(output/('original-'+name)).resolve()
        if source.exists():
            if not source.is_relative_to(output) or not dest.is_relative_to(output) or dest.exists():
                raise NarrationFailure('timeline_archive_path_conflict')
            source.rename(dest)


def layout_voice_caption(row,font_path,canvas):
    """Wrap only the displayed caption; retain every synthesized character."""
    from PIL import ImageFont
    text=row['text']
    if '\n' in text or '\r' in text:raise NarrationFailure('narration_script_must_not_contain_layout_breaks')
    for size in range(int(row['size']),7,-1):
        pixels=max(1,math.ceil(size*canvas['height']/320))
        font=ImageFont.truetype(str(font_path),pixels);stroke=math.ceil(pixels*.08)+2
        def fits(value):
            box=font.getbbox(value,stroke_width=stroke)
            return (box[2]-box[0])*1.05<=canvas['width']*.8
        remaining=text;lines=[]
        while remaining:
            end=0
            for i in range(1,len(remaining)+1):
                if not fits(remaining[:i]):break
                end=i
            if not end:break
            if end<len(remaining):
                punctuation=[i+1 for i,c in enumerate(remaining[:end]) if c in '，。；、！？,;!?']
                if punctuation and punctuation[-1]>=end/2:end=punctuation[-1]
                elif remaining[end] in '，。；、！？,;!?' and end>1:end-=1
            lines.append(remaining[:end]);remaining=remaining[end:]
        if remaining or len(lines)>2:continue
        display='\n'.join(lines)
        boxes=[font.getbbox(line,stroke_width=stroke) for line in lines]
        tw=max(b[2]-b[0] for b in boxes)*1.05
        th=(max(b[3]-b[1] for b in boxes)+(len(lines)-1)*pixels*1.3)*1.05
        fit={'size':size}
        for axis,limit in (('x',max(0,1-.16-tw/canvas['width'])),('y',max(0,1-.12-th/canvas['height']))):
            fit[axis]=round(max(-limit,min(limit,row[axis])),6)
        return {**fit,'text':display,'line_count':len(lines)}
    raise NarrationFailure('native_caption_requires_shorter_copy:'+row['key'])


def check_new_voice(snapshot, original, receipts, removed_voice, caption_track):
    segments={s['id']:s for t in snapshot['tracks'] for s in t.get('segments',[])}
    audio={m['id']:m for m in snapshot['materials'].get('audios',[])}
    text={m['id']:m for m in snapshot['materials'].get('texts',[])}
    if snapshot['duration']!=original['duration']:
        raise NarrationFailure('new_voice_changed_video_duration')
    if any(t['id']==removed_voice for t in snapshot['tracks']):
        raise NarrationFailure('old_source_narration_track_remains')
    if any(s.get('volume',1)!=0 for t in snapshot['tracks'] if t['type']=='video' for s in t.get('segments',[])):
        raise NarrationFailure('source_speech_not_muted')
    for r in receipts:
        s=segments.get(r['audio_segment_id']);c=segments.get(r['text_segment_id'])
        if not s or not c:raise NarrationFailure('new_tts_segment_missing')
        a=audio.get(s['material_id'],{});m=text.get(c['material_id'],{})
        if (a.get('type')!='text_to_audio' or a.get('text_id')!=m.get('id') or
                a.get('resource_id')!=r['resource_id'] or
                digest(Path(a['path']))!=r['sha256'] or
                json.loads(m['content']).get('text','').replace('\n','')!=r['text']):
            raise NarrationFailure('new_tts_provenance_or_caption_mismatch')
        dur=a.get('duration');span=s['target_timerange'];src=s['source_timerange']
        speed=r.get('requested_synthesis_speed',1.0)
        if (type(dur) is not int or dur<=0 or src.get('start',0)!=0 or src['duration']!=dur or
                span.get('start',0)!=r['slot']['start_us'] or
                span['duration']<dur/speed or span['duration']>r['slot']['duration_us'] or
                not 1<=speed<=1.18 or s.get('speed',1)!=speed or s.get('volume',0)<=0 or c['target_timerange']!=span):
            raise NarrationFailure('new_tts_clipped_retimed_or_misaligned')
        if speed!=1:
            speeds=[m for m in snapshot['materials'].get('speeds',[]) if m['id'] in s.get('extra_material_refs',[])]
            if len(speeds)!=1 or speeds[0].get('speed')!=speed:
                raise NarrationFailure('native_tts_speed_object_not_verified')
    # Existing layer timing/content is compared after native normalization by
    # the finalizer. Here reject removed/replaced independent video/FX objects.
    for t in original['tracks']:
        if t['id'] in (removed_voice,caption_track):continue
        for old in t.get('segments',[]):
            new=segments.get(old['id'])
            if (not new or new['material_id']!=old['material_id'] or
                    new['target_timerange']!=old['target_timerange'] or
                    new.get('source_timerange')!=old.get('source_timerange')):
                raise NarrationFailure('unrelated_existing_layer_changed')
    return {'new_voice_from_text_verified':True,'original_speech_removed':True,
        'source_video_muted':True,'new_voice_segments':len(receipts),
        'caption_matches_synthesis_input':True,'complete_audio_not_truncated':True,
        'unchanged_existing_layer_ranges':True}


def finalize(install, output, name):
    output=Path(output).resolve();draft=output/'draft'
    original=json.loads((output/'source-snapshot.json').read_text(encoding='utf-8'))
    binding=json.loads((output/'source-binding.json').read_text(encoding='utf-8'))
    receipts=json.loads((output/'tts-receipts.json').read_text(encoding='utf-8'))
    caption_track=binding['replaced_caption_track'];removed_voice=binding['removed_narration_track']
    S,check,_=native_modules()
    from native_build import sidecar_metadata
    from native_service import validate_player_state
    from .native_review_registration import verify_manifest
    verify_manifest(Path(binding['source']),binding['files'])
    templates=next(t for t in original['tracks'] if t['id']==caption_track)['segments']
    text_materials={m['id']:m for m in original['materials']['texts']}
    with S(install,output/('finalize-service-'+uuid.uuid4().hex[:8])) as svc:
        original=normalize_snapshot(svc,Path(binding['source']),original)
        generated=read_editable_main(svc,draft)
        atomic_json(output/'synthesized-snapshot.json',generated)
        # Keep the approved baseline verbatim. Native add's numeric track index
        # can insert into another same-type track; group only its returned IDs.
        snap=json.loads((output/'prepared-snapshot.json').read_text(encoding='utf-8'))
        voice_ids={r['audio_segment_id'] for r in receipts};caption_ids={r['text_segment_id'] for r in receipts}
        generated_segments={s['id']:s for t in generated['tracks'] for s in t.get('segments',[])}
        if not voice_ids|caption_ids <= generated_segments.keys():
            raise NarrationFailure('native_tts_checkpoint_segments_missing')
        needed={generated_segments[s]['material_id'] for s in voice_ids|caption_ids}
        needed.update(r for sid in voice_ids|caption_ids for r in generated_segments[sid].get('extra_material_refs',[]))
        for group,values in generated['materials'].items():
            if not isinstance(values,list):continue
            old_ids={m.get('id') for m in snap['materials'].get(group,[]) if isinstance(m,dict)}
            additions=[rewrite_paths(copy.deepcopy(m),[(binding['source'],draft.as_posix())])
                for m in values if isinstance(m,dict) and m.get('id') in needed and m['id'] not in old_ids]
            if additions:snap['materials'].setdefault(group,[]).extend(additions)
        for t in generated['tracks']:
            chosen=[s for s in t.get('segments',[]) if s['id'] in voice_ids|caption_ids]
            if chosen:
                track=copy.deepcopy(t);track['id']=str(uuid.uuid4()).upper();track['segments']=copy.deepcopy(chosen)
                snap['tracks'].append(track)
        segments={s['id']:s for t in snap['tracks'] for s in t.get('segments',[])}
        audios={m['id']:m for m in snap['materials']['audios']}
        texts={m['id']:m for m in snap['materials']['texts']}
        voice_ids={r['audio_segment_id'] for r in receipts};caption_ids={r['text_segment_id'] for r in receipts}
        adjustments=[]
        for i,r in enumerate(receipts):
            a_seg=segments[r['audio_segment_id']];a=audios[a_seg['material_id']]
            duration=a.get('duration',0);slot=r['slot'];fps=original['fps']
            speed=r.get('requested_synthesis_speed',1.0)
            if a_seg.get('speed',1)!=speed:raise NarrationFailure('native_tts_speed_not_applied')
            target_end=math.ceil((slot['start_us']+duration/speed)*fps/1e6)*1_000_000//fps
            length=target_end-slot['start_us']
            if duration<=0 or length>slot['duration_us']:
                raise NarrationFailure(f"new_voice_exceeds_slot:{slot['slot']}:{duration}:{slot['duration_us']}")
            a_seg['target_timerange']={'start':slot['start_us'],'duration':length}
            a_seg['source_timerange']={'start':0,'duration':duration}
            a_seg.update(volume=1.0,last_nonzero_volume=1.0)
            c_seg=segments[r['text_segment_id']];current=texts[c_seg['material_id']]
            template=templates[i];style=rewrite_paths(copy.deepcopy(text_materials[template['material_id']]),
                [(binding['source'],draft.as_posix())])
            rich=json.loads(style['content']);rich['text']=r['text']
            base=copy.deepcopy(rich['styles'][0]);base['range']=[0,len(r['text'])];rich['styles']=[base]
            point=copy.deepcopy(template['clip'])
            row={'key':'voice_'+str(i+1),'text':r['text'],'size':min(12,base.get('size',12)),
                'x':0,'y':point['transform']['y']}
            fit=layout_voice_caption(row,style['font_path'],original['canvas_config'])
            rich['text']=fit['text'];base['range']=[0,len(fit['text'])]
            base['size']=fit['size'];point['transform'].update(x=fit['x'],y=fit['y'])
            # Keep freshly generated native text/audio linkage and TTS metadata.
            style.update(id=current['id'],content=json.dumps(rich,ensure_ascii=False),font_size=fit['size'])
            for k in ('text_to_audio_ids','tts_auto_update','relevance_segment','current_words','words'):
                if k in current:style[k]=current[k]
            current.clear();current.update(style)
            c_seg.update(clip=point,target_timerange=copy.deepcopy(a_seg['target_timerange']),
                render_index=template.get('render_index',14000),visible=True)
            adjustments.append({'slot':slot['slot'],'audio_duration_us':duration,
                'target_timerange':a_seg['target_timerange'],'text_size':fit['size'],
                'caption_lines':fit['line_count'],'native_tts_speed':speed})
        # TTS can create several temporary tracks; present one voice track and
        # one matching caption track, preserving every original nonvoice layer.
        voice=[segments[r['audio_segment_id']] for r in receipts]
        captions=[segments[r['text_segment_id']] for r in receipts]
        kept=[];voice_template=caption_template=None
        for t in snap['tracks']:
            ids={s['id'] for s in t.get('segments',[])}
            if ids&voice_ids:voice_template=voice_template or copy.deepcopy(t)
            if ids&caption_ids:caption_template=caption_template or copy.deepcopy(t)
            t['segments']=[s for s in t.get('segments',[]) if s['id'] not in voice_ids|caption_ids]
            if t['segments']:kept.append(t)
        if not voice_template or not caption_template:raise NarrationFailure('generated_tracks_missing')
        voice_template.update(name='全新口播（剪映亲切女声）',segments=voice)
        caption_template.update(name='新口播字幕（DeepSeek原创文案）',segments=captions)
        snap['tracks']=[*kept,caption_template,voice_template]
        snap['name']=name;snap['duration']=original['duration']
        snap,aliases=restore_portable_aliases(snap,draft)
        check(snap)
        # Expand sparse CLI protobuf JSON through the native editor before
        # validating and saving the final revision.
        sid=svc.request('player','create',{'kind':'draft','serviceGeneration':1})['result']['sessionId']
        try:
            svc.request('player','load',{'draftPath':str(draft/'draft_content.json'),'workspacePath':str(output)},sid)
            snap=svc.request('edit','draft.setDraft',{'draftSnapshot':snap},sid)['result']['draftSnapshot']
        finally:svc.request('player','destroy',{},sid)
        proof=check_new_voice(snap,original,receipts,removed_voice,caption_track)
        check(snap)
        for raw in absolute_paths(snap):
            if not Path(raw).resolve(strict=True).is_relative_to(draft):
                raise NarrationFailure('new_voice_project_has_external_dependency:'+raw)
        archive_timeline_container(draft,output)
        write_native(svc,draft/'draft_content.json',snap)
        meta=sidecar_metadata(snap,draft,output,time.time_ns()//1000)
        write_native(svc,draft/'draft_meta_info.json',meta)
    atomic_json(output/'final-snapshot.json',snap)
    atomic_json(output/'voice-validation.json',{**proof,'adjustments':adjustments,'duration_us':snap['duration'],
        'draft_sha256':digest(draft/'draft_content.json'),'voice_material_type':'text_to_audio',
        'audio_listened':False,'final_export_created':False,'verified_resource_cache_aliases':aliases})
    verify_manifest(Path(binding['source']),binding['files'])
    return proof


def finish_revision(install,output,name):
    """Cold native readback, pixels and whole-duration signal sampling."""
    output=Path(output).resolve();draft=output/'draft';S,check,_=native_modules()
    from native_preview import preview_batches
    from native_audio_scan import scan,verify_artifacts
    from draft_quality import audit
    from .native_review_registration import verify_manifest
    done=output/'narration-report.json'
    if done.exists():
        prior=json.loads(done.read_text(encoding='utf-8'))
        script=json.loads((output/'new-script.json').read_text(encoding='utf-8')) if (output/'new-script.json').exists() else {}
        if prior.get('voice_count') == len(script.get('sentences', [])):
            return validate_revision(output)
    validation=output/'voice-validation.json'
    # A recoverable run can have a partial narration report while the native
    # draft already contains additional committed TTS slots.  In that case the
    # old validation fingerprint is intentionally stale; rebuild the final
    # snapshot and validation proof from the complete receipt set before doing
    # cold readback.  Keep the strict failure for a changed draft after a
    # complete report, because that indicates an out-of-band edit.
    partial_report=True
    script_file=output/'new-script.json'
    if done.exists() and script_file.exists():
        prior=json.loads(done.read_text(encoding='utf-8'))
        script=json.loads(script_file.read_text(encoding='utf-8'))
        partial_report=prior.get('voice_count', 0) != len(script.get('sentences', []))
    if not validation.exists() or partial_report:
        finalize(install,output,name)
    current_draft_sha=digest(draft/'draft_content.json')
    proof=json.loads(validation.read_text(encoding='utf-8'))
    if proof['draft_sha256']!=current_draft_sha:
        if partial_report:
            # The previous validation belongs to an incomplete voice
            # checkpoint. Re-finalize once so the proof matches the current
            # native draft.
            finalize(install,output,name)
            proof=json.loads(validation.read_text(encoding='utf-8'))
        else:
            raise NarrationFailure('narration_changed_after_validation')
    if proof['draft_sha256']!=digest(draft/'draft_content.json'):
        raise NarrationFailure('narration_changed_after_validation')
    snap=json.loads((output/'final-snapshot.json').read_text(encoding='utf-8'))
    receipts=json.loads((output/'tts-receipts.json').read_text(encoding='utf-8'))
    revision=proof['draft_sha256'][:12]
    structural=audit(snap,base_dir=draft,primary_track=next(t['id'] for t in snap['tracks'] if t['type']=='video'))
    atomic_json(output/'structural-audit.json',structural)
    if any(i['severity'] in ('error','warning') for i in structural['issues']):
        raise NarrationFailure('new_narration_structural_review_required')
    positions=sorted({0,snap['duration']-1,*[r['slot']['start_us']+
        min(700000,r['slot']['duration_us']//2) for r in receipts]})
    preview_dir=output/('preview-'+revision)
    if (preview_dir/'preview-report.json').exists():
        preview=json.loads((preview_dir/'preview-report.json').read_text(encoding='utf-8'))
    else:preview=preview_batches(install,draft,preview_dir,positions,540)
    for f in preview['frames']:
        if digest(Path(f['path']))!=f['sha256']:raise NarrationFailure('narration_preview_changed')
    audio_dir=output/('audio-'+revision)
    if (audio_dir/'audio-scan-report.json').exists():
        signal=json.loads((audio_dir/'audio-scan-report.json').read_text(encoding='utf-8'))
    else:signal=scan(install,draft,audio_dir)
    if (signal.get('draft_sha256')!=proof['draft_sha256'] or
            not signal.get('full_duration_signal_sampling_complete') or verify_artifacts(signal)):
        raise NarrationFailure('narration_native_audio_scan_incomplete')
    voice_dir=output/('voice-only-'+revision);voice_dir.mkdir(exist_ok=True)
    if not (voice_dir/'draft/draft_content.json').exists():
        solo=copy.deepcopy(snap);voice_ids={r['audio_segment_id'] for r in receipts}
        for t in solo['tracks']:
            if t['type'] in ('audio','video'):
                for s in t['segments']:
                    if s['id'] not in voice_ids:s['volume']=0;s['last_nonzero_volume']=0
        (voice_dir/'draft').mkdir(exist_ok=True)
        with S(install,voice_dir/'prepare-service') as svc:
            write_native(svc,voice_dir/'draft/draft_content.json',solo)
    if (voice_dir/'scan/audio-scan-report.json').exists():
        solo_signal=json.loads((voice_dir/'scan/audio-scan-report.json').read_text(encoding='utf-8'))
    else:solo_signal=scan(install,voice_dir/'draft',voice_dir/'scan')
    if (not solo_signal.get('full_duration_signal_sampling_complete') or verify_artifacts(solo_signal)
            or solo_signal.get('zero_signal_windows_needing_review')):
        raise NarrationFailure('new_tts_not_audible_in_native_isolated_playback')
    from pymediainfo import MediaInfo
    for r in receipts:
        streams=[t for t in MediaInfo.parse(r['path']).tracks if t.track_type=='Audio']
        if len(streams)!=1 or not streams[0].duration or float(streams[0].duration)<=0:
            raise NarrationFailure('new_tts_file_has_no_decodable_audio')
    binding=json.loads((output/'source-binding.json').read_text(encoding='utf-8'))
    verify_manifest(Path(binding['source']),binding['files'])
    result={'schema':SCHEMA,'status':'new_native_narration_ready_for_review','draft':str(draft),
        'draft_sha256':proof['draft_sha256'],'duration_us':snap['duration'],'voice_count':len(receipts),
        'new_narration_verified':True,'original_speech_removed':True,'source_preserved':True,
        'tone_resource_id':TONE_ID,'tone_title':TONE_TITLE,'native_text_to_audio_objects':len(receipts),
        'preview_report':str(preview_dir/'preview-report.json'),'frames':preview['frames'],
        'audio_report':str(audio_dir/'audio-scan-report.json'),'voice_only_audio_report':str(voice_dir/'scan/audio-scan-report.json'),
        'voice_only_native_signal_checked':True,'audio_listened':False,'continuous_av_reviewed':False,
        'final_export_created':False,'full_quality_accepted':False,'codex_runtime_required':False,
        'tracks':[{'name':t.get('name',''),'type':t['type'],'segments':len(t['segments'])} for t in snap['tracks']],
        'files':file_manifest(draft),'evidence':[{'path':str(p),'sha256':digest(p)} for p in
            [output/'new-script.json',output/'tts-receipts.json',validation,output/'tone-receipt.json',
             preview_dir/'preview-report.json',audio_dir/'audio-scan-report.json',voice_dir/'scan/audio-scan-report.json']]}
    atomic_json(done,result)
    return validate_revision(output)


def validate_revision(output):
    from .native_review_registration import verify_manifest
    output=Path(output);report=json.loads((output/'narration-report.json').read_text(encoding='utf-8'))
    if report.get('status')!='new_native_narration_ready_for_review':raise NarrationFailure('narration_not_ready')
    verify_manifest(Path(report['draft']),report['files'])
    for row in report['evidence']:
        if digest(Path(row['path']))!=row['sha256']:raise NarrationFailure('narration_evidence_changed')
    for row in report['frames']:
        if digest(Path(row['path']))!=row['sha256']:raise NarrationFailure('narration_pixels_changed')
    return report


def register_revision(config,output,name):
    import os
    from .native_review_registration import host_exclusion,validate_registered,verify_dependency_copy
    output=Path(output).resolve();report=validate_revision(output)
    publication=output/'publication'
    if (publication/'registration-report.json').exists():return validate_registered(publication)
    native_modules()
    from native_register import publish
    from native_preview import preview_batches
    source=Path(report['draft']);S,_,_=native_modules()
    with host_exclusion():
        def before(dest):
            verify_dependency_copy(source,dest)
            with S(config['install_dir'],output/'registration-read') as svc:
                snap=normalize_snapshot(svc,dest,read_native(svc,dest))
            for raw in absolute_paths(snap):
                if not Path(raw).resolve(strict=True).is_relative_to(dest):
                    raise NarrationFailure('registered_voice_dependency_not_portable')
            actual=preview_batches(config['install_dir'],dest,output/'registered-preview',
                [f['requested_us'] for f in report['frames']],540)['frames']
            if any(a['sha256']!=b['sha256'] for a,b in zip(actual,report['frames'])):
                raise NarrationFailure('registered_new_voice_pixels_changed')
            expected=json.loads((output/'final-snapshot.json').read_text(encoding='utf-8'))
            if snap['tracks']!=expected['tracks']:
                raise NarrationFailure('registered_new_voice_tracks_changed')
            return {'native_frame_comparison':True,'frames':len(actual),'voice_track_readback':True}
        value=publish(config['install_dir'],source,config['draft_root'],
            config.get('user_data_dir') or str(Path(os.environ['LOCALAPPDATA'])/'JianyingPro/User Data'),
            name,publication,before_register=before)
        value.update(destination_files=file_manifest(Path(value['destination'])),self_contained_dependencies=True,
            new_narration_verified=True,full_quality_accepted=False,final_export_created=False)
        atomic_json(publication/'registration-report.json',value)
    return validate_registered(publication)


def revise(config,source,output,instruction,*,narration_track=None,caption_track=None,
           provider=None,resume=False,name='全新口播修订',progress=lambda value:None):
    """Standalone Agent operation: understand -> write -> TTS -> verify.

    One resume only reuses byte-checked committed sentences. A submitted but
    unacknowledged TTS request is retained for recovery, never blindly retried.
    """
    from .native_host import IsolatedNativeHost
    from .native_cli import NativeCLI
    from .model_router import create_provider
    from .credential_store import credential_environment
    from .native_review_registration import verify_manifest
    if config.get('native_resources',{}).get('allow_isolated_host') is not True:
        raise NarrationFailure('native_narration_needs_authorized_isolated_host')
    source=Path(source).resolve(strict=True);output=Path(output).resolve()
    if resume:
        binding=json.loads((output/'source-binding.json').read_text(encoding='utf-8'))
        if Path(binding['source']).resolve()!=source:raise NarrationFailure('narration_resume_source_changed')
        verify_manifest(source,binding['files'])
        original=json.loads((output/'source-snapshot.json').read_text(encoding='utf-8'))
        snap=json.loads((output/'prepared-snapshot.json').read_text(encoding='utf-8'))
        narration_track=binding['removed_narration_track'];caption_track=binding['replaced_caption_track']
        draft=output/'draft'
    else:
        if not narration_track or not caption_track:
            S,_,_=native_modules()
            with S(config['install_dir'],output.parent/('voice-inspect-'+uuid.uuid4().hex[:8])) as svc:
                inspected=read_editable_main(svc,source)
            voices=[t['id'] for t in inspected['tracks'] if t['type']=='audio' and t.get('name')=='原素材口播']
            texts=[t['id'] for t in inspected['tracks'] if t['type']=='text' and t.get('name')=='字幕（DeepSeek卖点标题）']
            if (not narration_track and len(voices)!=1) or (not caption_track and len(texts)!=1):
                raise NarrationFailure('replacement_tracks_ambiguous_supply_explicit_ids')
            narration_track=narration_track or voices[0];caption_track=caption_track or texts[0]
        draft,snap,original=prepare_copy(config['install_dir'],source,output,
            narration_track=narration_track,caption_track=caption_track)
    inputs={'instruction':instruction,'name':name,'tone_id':TONE_ID,'mode':'new_native'}
    if (output/'request.json').exists():
        if json.loads((output/'request.json').read_text(encoding='utf-8'))!=inputs:
            raise NarrationFailure('narration_resume_request_changed')
    else:atomic_json(output/'request.json',inputs)
    if (output/'narration-report.json').exists():
        prior_report=json.loads((output/'narration-report.json').read_text(encoding='utf-8'))
        prior_script=json.loads((output/'new-script.json').read_text(encoding='utf-8')) if (output/'new-script.json').exists() else {}
        if prior_report.get('voice_count') == len(prior_script.get('sentences', [])):
            return validate_revision(output)
    script_file=output/'new-script.json';slots=caption_slots(original,caption_track)
    if script_file.exists():script=json.loads(script_file.read_text(encoding='utf-8'))
    else:
        native_modules();from native_preview import preview_batches
        preview_file=output/'input-preview/preview-report.json'
        if preview_file.exists():frames=json.loads(preview_file.read_text(encoding='utf-8'))['frames']
        else:frames=preview_batches(config['install_dir'],draft,output/'input-preview',
                [s['start_us']+s['duration_us']//2 for s in slots],540)['frames']
        with credential_environment(config['provider']):
            provider=provider or create_provider(config)
            script=author_timed_script(provider,original,frames,instruction,caption_track,output)
        atomic_json(script_file,script)
        atomic_json(output/'model-usage.json',{'model':provider.config['model'],**provider.usage,
            'codex_runtime_required':False,'input_frames':frames})
    validate_timed_script(script,slots)
    progress({'stage':'new_script_written','text':validate_script(script)})
    receipt_file=output/'tts-receipts.json'
    receipts=json.loads(receipt_file.read_text(encoding='utf-8')) if receipt_file.exists() else []
    # A prior run may have committed only a prefix of the sentences. Resume
    # the missing slots instead of treating the partial receipt as complete.
    existing_by_slot={r.get('slot',{}).get('slot'):r for r in receipts if isinstance(r,dict)}
    with IsolatedNativeHost(config['install_dir'],output/('tts-host-'+uuid.uuid4().hex[:8]),
            allow_isolated_host=True,user_data=config.get('user_data_dir'),lifetime_timeout=600) as host:
        with NativeCLI(host) as client:
            client.sync_lyra_auth()
            tone=resolve_tone(client,config['install_dir']);atomic_json(output/'tone-receipt.json',tone)
            completed=[]
            for row,slot in zip(script['sentences'],slots):
                target=output/f"speech-{slot['slot']:02d}.json";intent=target.with_suffix('.intent.json')
                if target.exists():
                    receipt=json.loads(target.read_text(encoding='utf-8'))
                    if receipt['text']!=row['text'] or digest(Path(receipt['path']))!=receipt['sha256']:
                        raise NarrationFailure('saved_voice_checkpoint_changed')
                elif slot['slot'] in existing_by_slot:
                    receipt=existing_by_slot[slot['slot']]
                else:
                    if intent.exists():
                        checkpoint=json.loads(intent.read_text(encoding='utf-8'))
                        if (checkpoint.get('script_sha256')!=digest(script_file) or
                                not isinstance(checkpoint.get('text_segment_id'),str)):
                            raise NarrationFailure('native_tts_checkpoint_changed')
                        sid=checkpoint['text_segment_id']
                    else:
                        add=envelope(client,['draft','text','add','--ref','local://'+draft.as_posix(),
                            '--content',row['text'],'--track',str(len(snap['tracks'])),
                            '--start',str(slot['start_us'])+'us','--duration',str(slot['duration_us'])+'us'])
                        sid=add['segment_ids'][0]
                        atomic_json(intent,{'text_segment_id':sid,'script_sha256':digest(script_file),
                            'source_draft_sha256':digest(draft/'draft_content.json')})
                    receipt=synthesize(client,draft,sid,row['text'],tone,output/'native-generated-audio')
                    receipt['slot']=slot;atomic_json(target,receipt)
                completed.append(receipt);progress({'stage':'sentence_synthesized','slot':slot['slot']})
            receipts=completed
            atomic_json(receipt_file,receipts)
    atomic_json(output/'host-summary.json',{'status':host.report['status'],'index_unchanged':host.report['index_unchanged']})
    progress({'stage':'native_narration_quality_check'})
    fit_speech_durations(config,output)
    return finish_revision(config['install_dir'],output,name)
