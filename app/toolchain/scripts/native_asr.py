"""Use the installed official Host Capability client with inherited authorization.

Does not create grants, launch the GUI, or substitute a local ASR model.
Reports the actual provider response without implicit engine fallback.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

CLIENT_SHA256='e2d60d41624b41cd8911335a19fa47d566edcb9c7b403648531612e7c7e6c5ce'


class NativeASRBlocked(RuntimeError):
    pass


def call_client(install, output, audio=None):
    install=Path(install).resolve(strict=True)
    output=Path(output).resolve()
    if output.exists() or output.is_symlink() or output.is_relative_to(install):
        raise ValueError('Choose a new output directory outside installation')
    client=install/'Resources/canvas_agent/skills/_lib/host-capability-client.mjs'
    if hashlib.sha256(client.read_bytes()).hexdigest()!=CLIENT_SHA256:
        raise ValueError('Untested official Host Capability client version')
    from native_draft_codec import signature
    if signature(install/'bun.exe').get('status')!='Valid':
        raise ValueError('Invalid Bun signature')
    output.mkdir(parents=True)
    script=output/'host-client.mjs'
    script.write_text('import {getCapabilityManifest,invokeCapability} from '+json.dumps(client.as_uri())+';\n'+
        'const input=JSON.parse(await Bun.stdin.text());\n'+
        'const manifest=await getCapabilityManifest();\n'+
        'const usable=manifest.status==="completed" && manifest.capabilities?.some(c=>c.capabilityId==="asr.transcribe");\n'+
        'let result=null; if(input.audioPath && usable) result=await invokeCapability("asr.transcribe",{input:{audioPath:input.audioPath,language:"Auto"}});\n'+
        'console.log(JSON.stringify({manifest,invoked:Boolean(input.audioPath && usable),result}));\n',encoding='utf-8')
    body={'audioPath':str(Path(audio).resolve(strict=True)) if audio else None}
    try:
        proc=subprocess.run([str(install/'bun.exe'),str(script)],input=json.dumps(body).encode(),
            capture_output=True,timeout=320 if audio else 15,creationflags=subprocess.CREATE_NO_WINDOW)
        if proc.returncode:
            raise RuntimeError('Official host client process failed: '+proc.stderr.decode('utf-8','replace')[-500:])
        response=json.loads(proc.stdout)
    except subprocess.TimeoutExpired:
        response={'manifest':{'status':'blocked','reason':'host_client_timeout'},'invoked':None,'result':None}
    report={'provider':'Jianying official Host Capability / Lyra ASR','official_host_client':str(client),
            'authorization_source':'inherited official runtime environment; no fabricated token',
            'gui_opened':False,'ffmpeg_invoked':False,**response}
    (output/'host-client-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def transcribe(install,audio,output):
    report=call_client(install,output,audio)
    result=report.get('result') or {}
    if result.get('status')!='completed':
        reason=result.get('reason') or report.get('manifest',{}).get('reason') or 'native_asr_not_completed'
        raise NativeASRBlocked(reason)
    data=result.get('output') or {}
    cues=[]
    for segment in data.get('segments',[]):
        start,end,text=segment.get('startUs'),segment.get('endUs'),segment.get('text')
        if type(start) is not int or type(end) is not int or not 0<=start<end or not isinstance(text,str) or not text.strip():
            raise RuntimeError('Native ASR returned missing/invalid microsecond timestamps')
        cues.append({'start_us':start,'end_us':end,'text':text})
    if not cues:
        raise RuntimeError('Native ASR returned no timed subtitle segments')
    path=Path(output)/'captions.json'
    path.write_text(json.dumps(cues,ensure_ascii=False,indent=2),encoding='utf-8')
    return report,cues


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--install-dir',required=True);p.add_argument('--out',required=True)
    p.add_argument('--input',help='Audio path; omit for a read-only host capability check')
    a=p.parse_args()
    try:
        if a.input: report,_=transcribe(a.install_dir,a.input,a.out)
        else: report=call_client(a.install_dir,a.out)
        print(json.dumps(report,ensure_ascii=False))
        if report['manifest'].get('status')!='completed':raise SystemExit(3)
    except Exception as exc:
        print(json.dumps({'status':'blocked' if isinstance(exc,NativeASRBlocked) else 'failed','error':str(exc)},ensure_ascii=False))
        raise SystemExit(3 if isinstance(exc,NativeASRBlocked) else 2)
