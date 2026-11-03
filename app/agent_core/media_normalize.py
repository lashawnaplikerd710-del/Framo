"""Hash-bound CFR working media. Never overwrites user input."""
from pathlib import Path
import json
import subprocess
import uuid
from .task_store import digest, atomic_json
from .local_executor import draft_lock

POLICY='source_vfr_to_cfr30_v1'

def normalize(record, inspector, cache_root, ffmpeg):
    if str(record.get('frame_rate_mode','')).upper() != 'VFR':return record
    source=Path(record['path']).resolve(strict=True)
    if digest(source)!=record['sha256']:raise ValueError('原素材已变化，请重新添加素材')
    folder=Path(cache_root)/POLICY/record['sha256'];folder.mkdir(parents=True,exist_ok=True)
    target=folder/(source.stem[:80]+'.cfr.mp4');manifest=folder/'normalization.json'
    assert target.resolve()!=source
    with draft_lock(folder/'.lock'):
        saved=json.loads(manifest.read_text('utf-8')) if manifest.is_file() else None
        cached=bool(saved and saved.get('original_sha256')==record['sha256'] and saved.get('policy')==POLICY
            and saved.get('output')==str(target) and target.is_file() and saved.get('sha256')==digest(target))
        if not cached:
            temp=folder/(uuid.uuid4().hex+'.partial.mp4')
            flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
            try:
                probe=subprocess.run([str(Path(ffmpeg).with_name('ffprobe.exe')),'-v','error',
                    '-show_entries','format=start_time:stream=codec_type,start_time,duration',
                    '-of','json',str(source)],capture_output=True,check=True,timeout=60,creationflags=flags)
                timing=json.loads(probe.stdout)
                origin=float(timing.get('format',{}).get('start_time',0) or 0)
                def expected(kind,fallback):
                    stream=next((s for s in timing['streams'] if s['codec_type']==kind),{})
                    duration=stream.get('duration')
                    if duration not in (None,'N/A'):
                        return max(0,round((float(stream.get('start_time',origin) or origin)-origin+float(duration))*1e6))
                    # MediaInfo duration can already include the track delay.
                    return fallback or 0
                cmd=[str(ffmpeg),'-hide_banner','-v','error','-nostdin','-y','-i',str(source),
                    '-map','0:v:0','-map','0:a:0?','-vf','fps=fps=30:start_time=0','-fps_mode','cfr',
                    '-c:v','libx264','-preset','veryfast','-crf','18','-pix_fmt','yuv420p',
                    '-c:a','aac','-b:a','192k','-af','aresample=async=1:first_pts=0',
                    '-movflags','+faststart',str(temp)]
                result=subprocess.run(cmd,capture_output=True,timeout=1800,creationflags=flags)
                if result.returncode:raise ValueError('素材帧率自动转换失败：'+result.stderr.decode('utf-8','replace')[-500:])
                converted=inspector.inspect(temp)
                if converted.get('frame_rate_mode')!='CFR' or abs(float(converted['frame_rate'])-30)>.001:
                    raise ValueError('转换后未达到固定30帧，已停止使用副本')
                if abs(converted['video_duration_us']-expected('video',record['video_duration_us']))>100_000:
                    raise ValueError('帧率转换前后视频时长不一致，已保留原素材')
                if record.get('has_audio') and (not converted.get('has_audio') or
                    abs((converted.get('audio_duration_us') or 0)-expected('audio',record.get('audio_duration_us')))>150_000):
                    raise ValueError('帧率转换前后声音时长不一致，已保留原素材')
                subprocess.run([str(ffmpeg),'-v','error','-xerror','-i',str(temp),'-f','null','-'],
                    capture_output=True,check=True,timeout=1800,creationflags=flags)
                if digest(source)!=record['sha256']:raise ValueError('转换时原素材发生变化，请重新添加')
                temp.replace(target)
                atomic_json(manifest,{'policy':POLICY,'original_path':str(source),'original_sha256':record['sha256'],
                    'output':str(target),'sha256':digest(target),'full_decode_passed':True})
            finally:
                if temp.exists():temp.unlink()
        converted=inspector.inspect(target)
    return {**converted,'normalization':{'policy':POLICY,'original_path':str(source),
        'original_sha256':record['sha256'],'original_frame_rate_mode':'VFR','cache_hit':cached,'report':str(manifest)}}
