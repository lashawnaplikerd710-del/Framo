"""Check owned Jianying process audio output, with a muted control; no recording."""
import argparse
import copy
import json
from pathlib import Path
import subprocess
import sys
import time

from native_service import Service
from native_frames import capture_at, prime
from native_draft_codec import file_digest


def check(install, draft, output, positions, solo=None):
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    source=Path(draft).resolve(strict=True)
    if source.is_dir():source=source/'draft_content.json'
    install=Path(install).resolve(strict=True);output=Path(output).resolve()
    if output.exists() or output.is_relative_to(source.parent) or output.is_relative_to(install):
        raise ValueError('Choose a new check directory outside the draft and installation')
    digest=file_digest(source)
    output.mkdir(parents=True)
    report={'status':'checking','draft_file':str(source),'draft_sha256':digest,'checks':[],
            'method':'Windows Core Audio meter for owned native service PID; muted control',
            'human_listening_verified':False,'content_or_sync_verified':False,'recorded_audio':False}
    try:
        with Service(install,output/'service') as svc:
            snap=json.loads(svc.request('draft','readFile',{'path':str(source),'rootPath':str(source.parent)})['result']['content'])
            duration=snap['duration'];canvas=snap['canvas_config']
            if (not positions or len(positions)>10 or
                any(type(t) is not int or not 0<=t<=duration-500000 for t in positions)):
                raise ValueError('Choose 1..10 timestamps, each with at least 0.5 seconds remaining')
            segment_ids={s['id'] for t in snap['tracks'] if t['type'] in ('video','audio') for s in t['segments']}
            if solo and solo not in segment_ids:raise ValueError('Unknown audio/video segment ID')
            report['solo_segment_id']=solo
            width=360;height=round(width*canvas['height']/canvas['width'])
            if height>1920:width=max(16,round(width*1920/height));height=1920
            sid=svc.request('player','create',{'kind':'draft','serviceGeneration':1})['result']['sessionId']
            consumer=None
            try:
                svc.request('player','load',{'draftPath':str(source),'workspacePath':str(output)},sid)
                prime(svc,sid,duration,width,height)
                ready=output/'consumer-ready.json'
                consumer=subprocess.Popen([str(install/'bun.exe'),str(Path(__file__).with_name('native_frame_consumer.mjs')),
                    svc.endpoint,sid,str(ready),str(width),str(height)],stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
                deadline=time.monotonic()+5
                while not ready.exists():
                    if consumer.poll() is not None or time.monotonic()>deadline:
                        raise RuntimeError('Native frame consumer failed to attach')
                    time.sleep(.05)
                for muted,when in [(True,positions[0])]+[(False,t) for t in positions]:
                    state=copy.deepcopy(snap)
                    for track in state['tracks']:
                        if track['type'] not in ('audio','video'):continue
                        for segment in track['segments']:
                            if muted or (solo and segment['id']!=solo):segment['volume']=0
                    svc.request('edit','draft.setDraft',{'draftSnapshot':state},sid)
                    capture_at(svc,sid,when,width,height)
                    before=svc.request('player','getState',{},sid)['result']
                    play=svc.request('player','play',{},sid)['result']
                    if play.get('deferred'):raise RuntimeError('Native playback still deferred')
                    samples=[];meter=None
                    deadline=time.monotonic()+min(2.5,(duration-when)/1000000-.15)
                    while time.monotonic()<deadline:
                        if meter is None:
                            for session in AudioUtilities.GetAllSessions():
                                if session.ProcessId==svc.report['pid']:
                                    meter=session._ctl.QueryInterface(IAudioMeterInformation)
                                    report['session_mute']=bool(session.SimpleAudioVolume.GetMute())
                                    report['session_volume']=session.SimpleAudioVolume.GetMasterVolume()
                                    break
                        if meter is not None:samples.append(float(meter.GetPeakValue()))
                        time.sleep(.04)
                    after=svc.request('player','getState',{},sid)['result']
                    svc.request('player','pause',{},sid)
                    peak=max(samples,default=0);signal=sum(x>.0001 for x in samples)
                    advanced=after.get('positionUs',0)-before.get('positionUs',0)>100000
                    passed=meter is not None and advanced and (peak<.0001 if muted else signal>2)
                    report['checks'].append({'muted':muted,'position_us':when,'peak':peak,
                        'nonzero_samples':signal,'samples':len(samples),'clock_advanced':advanced,
                        'owned_session_found':meter is not None,'passed':passed})
            finally:
                if consumer is not None:
                    if consumer.poll() is None:consumer.terminate()
                    consumer.wait(timeout=5)
                svc.request('player','destroy',{},sid)
        if file_digest(source)!=digest:raise RuntimeError('Source draft changed during audio check')
        report['source_file_preserved']=True
        report['status']='signal_probes_passed' if all(r['passed'] for r in report['checks']) else 'signal_probe_failed'
    except Exception as exc:
        report.update(status='failed',error=str(exc));raise
    finally:
        (output/'audio-check-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__=='__main__':
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('install-dir','draft','out'):p.add_argument('--'+name,required=True)
    p.add_argument('--time-us',type=int,action='append',required=True)
    p.add_argument('--solo-segment-id')
    a=p.parse_args()
    try:
        result=check(a.install_dir,a.draft,a.out,a.time_us,a.solo_segment_id)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        raise SystemExit(0 if result['status']=='signal_probes_passed' else 2)
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)},ensure_ascii=False),file=sys.stderr)
        raise SystemExit(2)
