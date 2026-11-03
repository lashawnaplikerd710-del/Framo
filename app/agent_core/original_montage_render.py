"""Bounded rendering for dense, long original-audio edits on Windows."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid


def split_snapshot(snapshot):
    video=next(t for t in snapshot['tracks'] if t['type']=='video')
    groups=[];current=[];duration=0
    for segment in video['segments']:
        length=segment['target_timerange']['duration']
        if current and (duration+length>40_000_000 or len(current)>=9):
            groups.append(current);current=[];duration=0
        current.append(segment);duration+=length
    if current:groups.append(current)
    result=[]
    for segments in groups:
        a=segments[0]['target_timerange']['start']
        b=segments[-1]['target_timerange']['start']+segments[-1]['target_timerange']['duration']
        chunk={**snapshot,'duration':b-a,'_original_montage_chunk':True,'tracks':[]}
        for track in snapshot['tracks']:
            values=[]
            selected=segments if track['type']=='video' else track.get('segments',[])
            for original in selected:
                s=deepcopy(original);slot=s['target_timerange'];left=max(a,slot['start']);right=min(b,slot['start']+slot['duration'])
                if right<=left:continue
                offset=left-slot['start'];s['target_timerange']={'start':left-a,'duration':right-left}
                if track['type'] in ('sound','music','audio'):
                    source=s.setdefault('source_timerange',{'start':0,'duration':slot['duration']})
                    source['start']+=offset;source['duration']=min(source['duration']-offset,right-left)
                for key in ('fade_in_us','fade_out_us'):
                    if key in s:s[key]=min(s[key],right-left)
                values.append(s)
            if track['type']=='video' and values:
                values[-1].pop('transition',None);values[-1].pop('transition_after',None)
            if values:chunk['tracks'].append({**track,'segments':values})
        result.append(chunk)
    return result


def render(snapshot,output_path,*,install_dir=None,work_dir=None,cancelled=None,font_path=None,
           progress_callback=None):
    from .direct_renderer import render_snapshot, _utc
    from .independent_export import discover_tools, probe_media, run_ffmpeg_progress, render_encoder_threads
    from .task_store import atomic_json
    tools=discover_tools(install_dir);ffmpeg=tools['ffmpeg']
    output_path=Path(output_path).resolve();output_path.parent.mkdir(parents=True,exist_ok=True)
    work=Path(work_dir or output_path.parent/'.original-render').resolve();work.mkdir(parents=True,exist_ok=True)
    started=_utc();total=snapshot['duration']/1e6
    def run(command,phase,duration=total):
        execution=run_ffmpeg_progress(command,progress_path=work/'ffmpeg-progress.log',
            duration_s=duration,timeout_s=max(180,total*12),cancelled=cancelled,
            progress_callback=progress_callback,phase=phase,timeout_message='长视频分段渲染超时')
        if execution['returncode']:raise RuntimeError(execution['stderr'][-4000:])
        return execution['stderr']
    # Balance embedded source audio before low-level SFX are mixed. Each source
    # is measured once per job; clips and frame hashes remain unchanged.
    snapshot=deepcopy(snapshot);gains={}
    for row in snapshot['materials'].get('videos',[]):
        path=row.get('path')
        if path in gains or not row.get('has_audio',True):continue
        measure=run([ffmpeg,'-hide_banner','-nostats','-i',path,'-vn',
            '-af','loudnorm=I=-16:TP=-1.5:LRA=9:print_format=json','-f','null','NUL'],
            f'分析第{len(gains)+1}条原声音量',0)
        data=json.loads(measure[measure.rfind('{'):measure.rfind('}')+1])
        level=float(data['input_i'])
        gains[path]=max(.1,min(4,10**((-16-level)/20))) if level!=float('-inf') else 1
    materials={m['id']:m for m in snapshot['materials'].get('videos',[])}
    for track in snapshot['tracks']:
        if track['type']=='video':
            for segment in track['segments']:
                path=materials[segment['material_id']]['path']
                segment['volume']=segment.get('volume',1)*gains.get(path,1)
    atomic_json(work/'source-audio-gains.json',gains)
    chunks=split_snapshot(snapshot);paths=[];reports=[]
    for i,chunk in enumerate(chunks):
        if cancelled and cancelled():raise RuntimeError('任务已取消')
        path=work/f'chunk-{i:03d}.mp4';signature=hashlib.sha256(json.dumps(chunk,sort_keys=True).encode()).hexdigest()
        checkpoint=work/f'chunk-{i:03d}-report.json'
        prior=json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.is_file() else None
        if prior and prior.get('snapshot_sha256')==signature and path.is_file() and prior.get('file_sha256')==hashlib.sha256(path.read_bytes()).hexdigest():
            report=prior
        else:
            def chunk_progress(value):
                if progress_callback:progress_callback({**value,
                    'phase':f'渲染分段 {i+1}/{len(chunks)}：'+value.get('phase','视频编码')})
            report=render_snapshot(chunk,path,install_dir=install_dir,work_dir=work/f'part-{i:03d}',
                cancelled=cancelled,font_path=font_path,progress_callback=chunk_progress)
            report.update(snapshot_sha256=signature,file_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
            atomic_json(checkpoint,report)
        paths.append(path);reports.append(report)
        atomic_json(work/'progress.json',{'completed':i+1,'total':len(chunks),'phase':'原声长视频分段渲染'})
    listing=work/'concat.txt';listing.write_text('\n'.join("file '"+p.as_posix().replace("'","'\\''")+"'" for p in paths),encoding='utf-8')
    joined=work/'joined.mp4'
    run([ffmpeg,'-v','error','-y','-f','concat','-safe','0','-i',listing,'-c','copy',joined],'合并全部视频分段')
    measured=run([ffmpeg,'-hide_banner','-nostats','-i',joined,'-vn',
                  '-af','loudnorm=I=-16:TP=-1.5:LRA=9:print_format=json','-f','null','NUL'],'分析成品音量')
    loud=json.loads(measured[measured.rfind('{'):measured.rfind('}')+1])
    norm='loudnorm=I=-16:TP=-1.5:LRA=9:measured_I={input_i}:measured_TP={input_tp}:measured_LRA={input_lra}:measured_thresh={input_thresh}:offset={target_offset}:linear=true'.format(**loud)
    temp=output_path.with_name('.'+output_path.stem+'.'+uuid.uuid4().hex+'.tmp.mp4')
    try:
        # AAC priming and per-chunk frame quantization can lose one frame on
        # remux. Explicit final clocks pad at most a short tail, never dialogue.
        run([ffmpeg,'-v','error','-y','-i',joined,'-map','0:v:0','-map','0:a:0',
            '-vf',f'setpts=PTS-STARTPTS,fps={snapshot["fps"]},tpad=stop_mode=clone:stop_duration=0.15,trim=duration={total:.6f}',
            '-af',norm+f',apad,atrim=duration={total:.6f},asetpts=PTS-STARTPTS',
            '-c:v','libx264','-preset','fast','-crf','18','-threads',str(render_encoder_threads()),'-pix_fmt','yuv420p',
            '-c:a','aac','-b:a','192k','-ar','48000','-t',f'{total:.6f}','-movflags','+faststart',temp],'整条成品最终编码')
        media=probe_media(temp,ffmpeg=ffmpeg,ffprobe=tools.get('ffprobe'))
        if abs(media.get('duration_s',0)-total)>.15:raise RuntimeError('原声长视频成品时长校验失败')
        if (media.get('width'),media.get('height'))!=(snapshot['canvas_config']['width'],snapshot['canvas_config']['height']):
            raise RuntimeError('原声长视频成品画幅校验失败')
        run([ffmpeg,'-v','error','-xerror','-i',temp,'-f','null','NUL'],'成品完整解码检查')
        temp.replace(output_path)
    finally:temp.unlink(missing_ok=True)
    return {'schema_version':'1.0','backend':'direct_ffmpeg','status':'COMPLETED',
        'output_file':str(output_path),'started_at':started,'finished_at':_utc(),
        'ffmpeg':ffmpeg,'ffprobe':tools.get('ffprobe'),'media':media,'file_size':output_path.stat().st_size,
        'playback_ok':True,'error':None,'chunks':reports,'source_audio_gains':gains,
        'subtitle_styles':[],'subtitle_layout':[],'caption_motion':[],
        'original_montage_policy':snapshot['original_montage_policy'],
        'sticker_selection':snapshot.get('sticker_selection_report'),
        'visual_treatment_plan':snapshot.get('visual_treatment_plan'),
        'command':'bounded semantic montage chunks + two-pass audio normalization + final frame clock'}
