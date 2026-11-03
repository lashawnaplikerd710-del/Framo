"""Official Lyra export adapter; authorization failure is a terminal capability stop.

app-server.mjs references a candidate export command, but the authenticated
installed CLI currently rejects it. Probe the actual command before use; the
static caller is not proof of capability. This module never launches a host.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

from .internal_resources import LYRA_SHA256
from .native_connection import check
from .task_store import atomic_json


def inspect_mp4(path):
    from pymediainfo import MediaInfo
    path=Path(path)
    if not path.is_file() or path.stat().st_size==0:
        return {'status':'missing_output','video_stream_verified':False}
    tracks=MediaInfo.parse(str(path)).tracks
    general=next((t for t in tracks if t.track_type=='General'),None)
    videos=[t for t in tracks if t.track_type=='Video']
    audio=[t for t in tracks if t.track_type=='Audio']
    valid=bool(general and general.format=='MPEG-4' and len(videos)==1 and
               videos[0].width and videos[0].height and videos[0].duration and float(videos[0].duration)>0)
    return {'status':'container_and_video_verified' if valid else 'invalid_output',
            'video_stream_verified':valid,'audio_stream_count':len(audio),
            'duration_ms':float(videos[0].duration) if valid else None,
            'width':int(videos[0].width) if valid else None,
            'height':int(videos[0].height) if valid else None,
            'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
            'continuous_av_review_passed':False}


def export(install, draft, output, *, runner=None):
    install=Path(install).resolve(strict=True)
    draft=Path(draft).resolve(strict=True)
    output=Path(output).resolve()
    if (not draft.is_dir() or not (draft/'draft_content.json').is_file() or
            output.exists() or output.is_relative_to(draft) or output.is_relative_to(install)):
        raise ValueError('Export needs an existing draft and a new output outside input/installation')
    binary=install/'lyra-cli.exe'
    if binary.resolve(strict=True)!=binary or hashlib.sha256(binary.read_bytes()).hexdigest()!=LYRA_SHA256:
        raise ValueError('Untested Lyra export binary')
    if any(p.is_symlink() for p in draft.rglob('*')):
        raise ValueError('Draft symlinks are not supported')
    before=hashlib.sha256((draft/'draft_content.json').read_bytes()).hexdigest()
    output.mkdir(parents=True,exist_ok=False)
    report={'schema_version':'1.0','provider':'installed_jianying_lyra',
            'operation':'draft export','contract_source':'app-server.mjs cP$/nF4/aF4',
            'native_export_verified':False,'delivery_accepted':False,'gui_requested':False,
            'external_encoder_used':False,'authorization_fabricated':False,
            'automatic_retry':False,'host_connection':check(install)}
    report['execution_transport']='official_app_owned_runner' if runner else 'standalone_cli'
    copied=output/'draft-copy'
    # Absolute media references remain valid in place; keep the original immutable.
    # The source draft copy is used only for export, never homepage registration.
    shutil.copytree(draft,copied)
    target=output/'native-output.mp4'
    allowed={'SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','PATHEXT','LOCALAPPDATA',
             'APPDATA','USERPROFILE','HOME','TEMP','TMP','PATH','PROGRAMDATA',
             'PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS'}
    env={k:v for k,v in os.environ.items() if k.upper() in allowed or
         k.startswith(('LYRA_CLI_','CUSTOM_AGENT_HOST_'))}
    env['PATH']=str(install)+os.pathsep+env.get('PATH','')
    env['LYRA_CLI_ROOT_WORKSPACE']=str(output)
    env['CUSTOM_AGENT_LYRA_CLI_ROOT_WORKSPACE']=str(output)
    command=[str(binary),'draft','export','--ref','local://draft-copy',
             '--output',str(target),'--timeout','45s']
    try:
        contract_unavailable=False
        if runner is not None:
            probe=runner(['draft','export','--help'],timeout=15)
            report['command_help_exit_code']=probe.returncode
            if probe.returncode != 0:
                process=probe
                try:contract_unavailable=json.loads(probe.stdout.decode('utf-8-sig')).get('err_code')==-100003
                except (ValueError,UnicodeError,AttributeError):pass
            else:
                args=['draft','export','--ref','local://'+copied.as_posix(),
                      '--output',target.as_posix(),'--timeout','45s']
                process=runner(args,timeout=55)
        else:
            process=subprocess.run(command,cwd=install,env=env,capture_output=True,timeout=55,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        report['exit_code']=process.returncode
        try:
            envelope=json.loads(process.stdout.decode('utf-8-sig'))
            if not isinstance(envelope,dict):raise ValueError('Invalid envelope')
        except (ValueError,UnicodeError):
            envelope={}
        code=envelope.get('err_code')
        report['official_error_code']=code if type(code) in (int,str) else None
        if contract_unavailable:
            report.update(status='EXPORT_COMMAND_UNAVAILABLE',reason='authenticated_installed_cli_rejects_draft_export',
                          contract_verified=False,resume_condition='A supported Jianying native export interface is available')
        elif code==-100015:
            report.update(status='HOST_AUTH_REQUIRED',reason='unauthorized host process',
                          resume_condition='Official authorized host/export capability changed')
        elif process.returncode!=0 or code!=0:
            report.update(status='export_failed',reason='official_cli_rejected_or_invalid_response')
        else:
            report['media_inspection']=inspect_mp4(target)
            if report['media_inspection']['video_stream_verified']:
                report.update(status='native_export_needs_review',native_export_verified=True,
                              reason='Exact duration, expected audio and continuous viewing still require validation')
            else:
                report.update(status='invalid_export',reason='official_success_without_valid_mp4')
    except subprocess.TimeoutExpired:
        report.update(status='export_timeout',reason='official_export_timeout')
    except (OSError,ValueError):
        report.update(status='export_failed',reason='export_or_media_inspection_failed')
    report['source_preserved']=hashlib.sha256((draft/'draft_content.json').read_bytes()).hexdigest()==before
    report['output_exists']=target.is_file()
    if not report['source_preserved']:
        report.update(status='input_changed',native_export_verified=False)
    atomic_json(output/'native-export-report.json',report)
    return report
