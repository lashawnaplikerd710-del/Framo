"""Single-source video -> explicit ASR engine/cues -> native subtitles -> registration.

No silent ASR fallback. This workflow retains source order at speed 1 and starts
at source time zero. For edited/reordered timelines use native_subtitles.py with
already mapped output-timeline cues instead.
"""
import argparse
import asyncio
import json
import math
from pathlib import Path
import sys

from common import media_info, local_file
from native_draft_codec import file_digest, editor_pids
from native_timeline import build
from native_subtitles import add_subtitles, dump
from native_register import publish
from native_asr import NativeASRBlocked, call_client, transcribe as native_transcribe


def run(args):
    source=Path(local_file(args.input,Path.cwd()))
    output=Path(args.out).resolve()
    install=Path(args.install_dir).resolve(strict=True)
    if output.exists() or output.is_symlink() or output.is_relative_to(install):
        raise ValueError('Choose a new output directory outside installation')
    if args.engine=='local' and not args.model_dir:raise ValueError('local engine requires --model-dir')
    if args.engine=='captions' and (not args.captions or not args.captions_provider):
        raise ValueError('captions engine requires --captions and --captions-provider')
    if args.engine!='local' and args.model_dir:raise ValueError('--model-dir is only for the local engine')
    if args.engine!='captions' and (args.captions or args.captions_provider):raise ValueError('Caption input requires --engine captions')
    if bool(args.draft_root)!=bool(args.user_data):raise ValueError('Registration requires both --draft-root and --user-data')
    meta=media_info(source)
    if not meta['has_video'] or not meta['has_audio']:raise ValueError('Require video with audio')
    if args.fps not in (24,25,30,50,60):raise ValueError('Choose fps 24/25/30/50/60')
    full_us=round(meta['video_duration']*1000000)
    duration=args.duration_us if args.duration_us is not None else math.floor(meta['video_duration']*args.fps+1e-6)*1000000//args.fps
    if type(duration) is not int or not 0<duration<=full_us:raise ValueError('Duration outside video stream')
    if ((duration*args.fps+999999)//1000000)*1000000//args.fps!=duration:
        raise ValueError('Duration must be frame-aligned')
    output.mkdir(parents=True)
    digest=file_digest(source)
    report={'status':'in_progress','requested_engine':args.engine,'source':str(source),
            'source_sha256':digest,'name':args.name,'duration_us':duration,
            'source_video_duration_us':full_us,'tail_omitted_us':full_us-duration,
            'gui_opened':False,'computer_use_used':False,'ffmpeg_invoked':False,
            'native_asr_completed':False,'local_asr_completed':False,
            'registration':'not_requested','gui_homepage_visibility':'not_checked','stages':[]}
    def stage(name):
        report['stages'].append(name)
        print(json.dumps({'stage':name,'engine':args.engine},ensure_ascii=False),flush=True)
    try:
        if args.engine=='jianying':
            stage('checking_official_host_authorization')
            check=call_client(install,output/'host-check')
            if check['manifest'].get('status')!='completed' or not any(
                c.get('capabilityId')=='asr.transcribe' for c in check['manifest'].get('capabilities',[])):
                raise NativeASRBlocked(check['manifest'].get('reason','asr_capability_unavailable'))
            # Decode with Windows only after a genuine host grant is available.
            from local_asr import decode_audio
            audio=output/'audio.wav';asyncio.run(decode_audio(source,audio))
            native,cues=native_transcribe(install,audio,output/'asr')
            provider='剪映原生 ASR';report['native_asr_completed']=True
        elif args.engine=='local':
            stage('local_sensevoice_transcription')
            from local_asr import transcribe
            transcribe(source,args.model_dir,output/'asr',args.fps)
            cues=json.loads((output/'asr/captions.json').read_text(encoding='utf-8'))
            provider='本地 SenseVoice，非剪映 ASR';report['local_asr_completed']=True
        else:
            stage('loading_existing_timed_captions')
            cues=json.loads(Path(args.captions).read_text(encoding='utf-8-sig'))
            provider=args.captions_provider
        # Validate full cue structure before clipping a deliberately shortened source.
        from native_subtitles import normalize_cues
        validate_limit=max(full_us,round(meta['duration']*1000000))
        normalized=normalize_cues(cues,validate_limit,args.fps)
        clipped=[]
        for cue in normalized:
            if cue['start_us']>=duration:continue
            clipped.append(dict(cue,end_us=min(cue['end_us'],duration)))
        report['cues_outside_selected_duration']=len(cues)-len(clipped)
        report['cues_clipped_at_selected_end']=sum(c['end_us']>duration for c in normalized if c['start_us']<duration)
        if not clipped:raise ValueError('No subtitles inside requested video duration')
        dump(output/'captions.json',clipped)
        report['transcription_provider']=provider
        audio_duration=min(duration,round(meta['audio_duration']*1000000))
        plan={'schema':'jianying-native-timeline-v1','name':args.name,
              'canvas':{'width':meta['width'],'height':meta['height'],'fps':args.fps},'steps':[
            {'action':'video.add','id':'video','path':str(source),'track':0,'start_us':0,'duration_us':duration,'source_start_us':0},
            {'action':'audio.add','id':'audio','path':str(source),'track':1,'start_us':0,'duration_us':audio_duration,'source_start_us':0},
            {'action':'video.setVolume','segment':'video','volume':0}]}
        dump(output/'timeline-plan.json',plan)
        stage('building_native_video_and_audio')
        build(output/'timeline-plan.json',install,output/'base')
        stage('writing_and_verifying_native_subtitles')
        caption=add_subtitles(install,output/'base/draft',output/'captions.json',output/'subtitles',provider=provider,y=args.y)
        report.update(draft=caption['draft'],subtitle_count=caption['subtitle_count'],frames=caption['frames'],
                      native_roundtrip_reload_verified=True,unique_ids_verified=True,
                      subtitle_track_written=True,audio_output_test='not_performed_in_this_command',
                      proofreading_required=True)
        if file_digest(source)!=digest:raise RuntimeError('Source file changed')
        report['source_preserved']=True
        if args.draft_root:
            if editor_pids():
                report.update(status='draft_ready_registration_pending',registration='pending_editor_exit')
            else:
                stage('registering_native_draft')
                registered=publish(install,caption['draft'],args.draft_root,args.user_data,args.name,output/'registration')
                report.update(status='registered',registration='registered',registered_draft=registered['destination'],
                              registration_index_readback_verified=registered['registration']['index_readback_equal'])
        else:report['status']='draft_ready'
        dump(output/'workflow-report.json',report)
        return report
    except Exception as exc:
        report.update(status='blocked' if isinstance(exc,NativeASRBlocked) else 'failed',error=str(exc))
        dump(output/'workflow-report.json',report)
        raise


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('input','install-dir','out','name'):p.add_argument('--'+name,required=True)
    p.add_argument('--engine',required=True,choices=('jianying','local','captions'))
    for name in ('model-dir','captions','captions-provider','draft-root','user-data'):p.add_argument('--'+name)
    p.add_argument('--duration-us',type=int);p.add_argument('--fps',type=int,default=30)
    p.add_argument('--y',type=float,default=-.45)
    a=p.parse_args()
    try:
        result=run(a);print(json.dumps(result,ensure_ascii=False,indent=2))
        raise SystemExit(3 if result['status']=='draft_ready_registration_pending' else 0)
    except Exception as exc:
        print(json.dumps({'status':'blocked' if isinstance(exc,NativeASRBlocked) else 'failed','error':str(exc)},ensure_ascii=False))
        raise SystemExit(3 if isinstance(exc,NativeASRBlocked) else 2)
