"""Safe external MP4 export for batch output ranges.

The exporter deliberately works from an immutable draft snapshot.  It never
writes a Jianying draft or an input asset.  Unsupported Jianying-only tracks
are reported as a real failure instead of being silently dropped.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import tempfile
import uuid
from typing import Callable

from .task_store import atomic_json
from .visual_presets import TRANSITIONS, ANIMATIONS, effect_filters, sticker_motion
from .compound_transitions import COMPOUND_TRANSITIONS, compound_transition_filters

EXPORT_STATES = {"QUEUED", "PREPARING", "EXPORTING", "COMPLETED", "FAILED", "CANCELLED"}
CONFLICTS = {"overwrite", "skip", "rename"}
REPORT_NAME = "independent-export-report.json"


def _utc():
    return datetime.now(timezone.utc).isoformat()


def render_encoder_threads():
    """Bound only encoder parallelism; dense filter/decoder queues stay bounded."""
    return min(4, max(1, (os.cpu_count() or 2) // 2))


def run_ffmpeg_progress(command, *, progress_path, duration_s, timeout_s,
                        cancelled=None, progress_callback=None, phase='渲染',
                        timeout_message='FFmpeg 渲染超时'):
    """Drain both pipes while publishing truthful FFmpeg clock updates.

    Progress has its own file, so stdout never fills an unread pipe. All
    process exits, including callback errors/cancellation, reap the child.
    Phase percentages describe processing, never bypass final media checks.
    """
    path=Path(progress_path);path.parent.mkdir(parents=True,exist_ok=True)
    path.unlink(missing_ok=True)
    command=[str(v) for v in command]
    command[1:1]=['-progress',str(path),'-stats_period','1','-nostats']
    proc=subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    started=time.monotonic();deadline=started+timeout_s;last_notify=0
    def notify(force=False):
        nonlocal last_notify
        now=time.monotonic()
        if not progress_callback or (not force and now-last_notify<10):return
        last_notify=now;values={}
        if path.is_file():
            for line in path.read_text('utf-8',errors='replace').splitlines():
                if '=' in line:
                    k,v=line.split('=',1);values[k]=v
        try:seconds=max(0,int(values.get('out_time_us','0'))/1e6)
        except ValueError:seconds=0
        duration=max(0,float(duration_s or 0))
        progress_callback({'phase':phase,'processed_s':min(seconds,duration) if duration else seconds,
            'duration_s':duration,'percent':min(100,seconds/duration*100) if duration else None,
            'speed':values.get('speed','').strip(),'elapsed_s':now-started})
    try:
        notify(True)
        while True:
            if cancelled and cancelled():raise RuntimeError('任务已取消')
            if time.monotonic()>=deadline:raise RuntimeError(timeout_message)
            try:
                out,err=proc.communicate(timeout=.25)
                if proc.returncode==0:notify(True)
                return {'returncode':proc.returncode,
                    'stdout':out.decode('utf-8',errors='replace'),
                    'stderr':err.decode('utf-8',errors='replace')}
            except subprocess.TimeoutExpired:notify()
    finally:
        if proc.poll() is None:
            proc.kill();proc.communicate()


def discover_tools(install_dir: str | Path | None = None) -> dict:
    """Find a standalone FFmpeg/FFprobe pair without using an editor install.

    A file named ``ffmpeg.exe`` is not enough: older editor bundles can be a
    DLL stub that fails before startup.  Probe each candidate once and ignore
    unusable binaries so a direct task fails with a truthful missing-tool
    message instead of accidentally routing through an editor directory.
    """
    candidates = []
    if install_dir:
        root = Path(install_dir)
        candidates.extend([root / "ffmpeg.exe", root / "ffmpeg", root / "ffprobe.exe", root / "ffprobe"])
    for name in ("ffmpeg", "ffprobe"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    result = {"ffmpeg": None, "ffprobe": None}
    for path in candidates:
        if path.is_file() and _binary_works(path):
            key = "ffprobe" if "ffprobe" in path.name.casefold() else "ffmpeg"
            if result[key] is None:
                result[key] = str(path.resolve())
    return result


def _binary_works(path: Path) -> bool:
    try:
        completed = subprocess.run([str(path), '-version'], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=8,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _iso_seconds(value):
    return max(0.0, float(value or 0) / 1_000_000.0)


def _safe_name(value):
    value = re.sub(r"[^0-9A-Za-z._-]+", "_", str(value)).strip("._")
    return value or "output"


def _same_or_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def validate_output_directory(output_dir, *, source_paths=(), draft_paths=()):
    """Reject a directory that could overwrite a source or draft tree."""
    if not output_dir:
        raise ValueError("请先选择独立导出输出文件夹")
    target = Path(output_dir).expanduser().resolve()
    # A non-existing folder is safe to create after this check.  Comparing
    # parents prevents accidentally putting results next to/in an input tree.
    for raw in source_paths:
        if not raw:
            continue
        item = Path(raw).expanduser().resolve()
        parent = item if item.is_dir() else item.parent
        # A loose file at a drive/filesystem root does not make the entire
        # volume a source tree. Keep the root itself forbidden, but allow a
        # dedicated output folder on that volume. Explicit directory inputs
        # and ordinary source folders retain their existing protection.
        root_file = not item.is_dir() and parent == Path(parent.anchor)
        if ((item.is_dir() and _same_or_inside(target, item)) or
                (not item.is_dir() and (target == parent or
                 (not root_file and _same_or_inside(target, parent))))):
            raise ValueError("导出目录必须与原始素材目录和剪映草稿目录分开")
    for raw in draft_paths:
        if not raw:
            continue
        draft = Path(raw).expanduser().resolve()
        if _same_or_inside(target, draft):
            raise ValueError("导出目录必须与原始素材目录和剪映草稿目录分开")
    target.mkdir(parents=True, exist_ok=True)
    if not target.is_dir() or not os.access(target, os.W_OK):
        raise ValueError("导出目录不可写")
    return target


def _materials(snapshot):
    result = {}
    for group in (snapshot.get("materials") or {}).values():
        if not isinstance(group, list):
            continue
        for item in group:
            if isinstance(item, dict) and item.get("id"):
                result[item["id"]] = item
    return result


def _media_path(material):
    if not isinstance(material, dict):
        return None
    for key in ("path", "file_path", "material_path", "source_path", "local_path"):
        value = material.get(key)
        if isinstance(value, str) and value:
            return Path(value).expanduser().resolve()
    return None


def _fades(segment, materials):
    refs = [materials[r] for r in segment.get('extra_material_refs', [])
            if r in materials and materials[r].get('type') == 'audio_fade']
    if len(refs) > 1:
        raise UnsupportedExternalRender('多个音频淡入淡出定义无法确定')
    native = refs[0] if refs else {}
    values = [segment.get('fade_in_us', native.get('fade_in_duration', 0)),
              segment.get('fade_out_us', native.get('fade_out_duration', 0))]
    if any(type(v) is not int or v < 0 or v > segment['target_timerange']['duration'] for v in values):
        raise UnsupportedExternalRender('音频淡入淡出时长无效')
    return values


def _zoom_points(segment, source_start, duration):
    points = (segment.get('clip') or {}).get('zoom_keyframes') or segment.get('zoom_keyframes') or []
    groups = segment.get('common_keyframes') or []
    if groups:
        if len(groups) != 1 or groups[0].get('property_type') != 'KFTypeScaleX':
            raise UnsupportedExternalRender('仅支持等比线性缩放关键帧')
        points = []
        for k in groups[0].get('keyframe_list', []):
            if k.get('curveType') != 'Line' or len(k.get('values', [])) != 1:
                raise UnsupportedExternalRender('缩放曲线尚不支持外部导出')
            points.append({'time_us': k['time_offset'] - source_start, 'scale': k['values'][0]})
    previous = -1
    for p in points:
        t, v = p.get('time_us'), p.get('scale')
        if (type(t) is not int or not previous < t <= duration or t < 0 or
                type(v) not in (int, float) or not math.isfinite(v) or not 1 <= v <= 2):
            raise UnsupportedExternalRender('缩放关键帧时间或范围无效')
        previous = t
    return points


def _zoom_expression(points, fps):
    expression = str(points[-1]['scale'])
    for a, b in reversed(list(zip(points, points[1:]))):
        start, end = a['time_us'] * fps / 1e6, b['time_us'] * fps / 1e6
        value = f"({a['scale']}+({b['scale']}-{a['scale']})*(on-{start})/({end}-{start}))"
        expression = f"if(lt(on,{end}),{value},{expression})"
    return f"if(lt(on,{points[0]['time_us']*fps/1e6}),{points[0]['scale']},{expression})"


def _layout_crop_spec(material, layout, canvas):
    """Translate a verified subject track into a bounded cover crop."""
    if not isinstance(layout, dict) or layout.get('mode') != 'subject_follow':
        return None
    source_width, source_height = material.get('width'), material.get('height')
    target_width, target_height = canvas.get('width'), canvas.get('height')
    if not all(type(value) in (int, float) and value > 0 for value in
               (source_width, source_height, target_width, target_height)):
        return None
    source_ratio = float(source_width) / float(source_height)
    target_ratio = float(target_width) / float(target_height)
    if abs(source_ratio - target_ratio) < 0.002:
        return None
    axis = 'x' if source_ratio > target_ratio else 'y'
    points = []
    for row in layout.get('keyframes') or []:
        if not isinstance(row, dict):
            continue
        timestamp = row.get('time_us')
        center = row.get('center_x' if axis == 'x' else 'center_y')
        if type(timestamp) is not int or type(center) not in (int, float):
            continue
        if timestamp < 0 or not math.isfinite(float(center)):
            continue
        points.append({'time_us': timestamp,
                       'center': min(1.0, max(0.0, float(center)))})
    if not points:
        return None
    points.sort(key=lambda item: item['time_us'])
    deduped = []
    for point in points:
        if deduped and point['time_us'] == deduped[-1]['time_us']:
            deduped[-1] = point
        else:
            deduped.append(point)
    return {'axis': axis, 'target_ratio': target_ratio, 'points': deduped}


def _layout_center_expression(points):
    expression = f"{points[-1]['center']:.6f}"
    for left, right in reversed(list(zip(points, points[1:]))):
        start, end = left['time_us'] / 1e6, right['time_us'] / 1e6
        if end <= start:
            continue
        value = (f"({left['center']:.6f}+({right['center']:.6f}-{left['center']:.6f})*"
                 f"(t-{start:.6f})/{end-start:.6f})")
        expression = f"if(lt(t,{end:.6f}),{value},{expression})"
    return f"if(lt(t,{points[0]['time_us']/1e6:.6f}),{points[0]['center']:.6f},{expression})"


def _video_segments(snapshot):
    materials = _materials(snapshot)
    transition_materials = {
        key: value for key, value in materials.items()
        if isinstance(value, dict) and value.get('type') == 'transition'
    }
    tracks = snapshot.get("tracks") or []
    video = [track for track in tracks if track.get("type") == "video"]
    if not video:
        raise ValueError("成品没有视频轨道，无法生成 MP4")
    rows = []
    for track in video:
        for segment in track.get("segments") or []:
            timerange = segment.get("target_timerange") or {}
            start = timerange.get("start")
            duration = timerange.get("duration")
            if type(start) is not int or type(duration) is not int or duration <= 0:
                raise ValueError("视频片段缺少合法绝对时间范围")
            material = materials.get(segment.get("material_id"))
            path = _media_path(material)
            if not path or not path.is_file():
                raise ValueError("视频片段的源素材不存在")
            source = segment.get("source_timerange") or {}
            source_start = source.get("start", 0)
            source_duration = source.get("duration", duration)
            if type(source_start) is not int or type(source_duration) is not int or source_start < 0 or source_duration <= 0:
                raise ValueError("视频片段缺少合法源时间范围")
            if segment.get('speed', 1) != 1 or segment.get('reverse') or source_duration != duration:
                raise UnsupportedExternalRender('暂不支持变速或倒放片段')
            clip = segment.get('clip') or {}
            if (clip.get('rotation', 0) or clip.get('alpha', 1) != 1
                    or any((clip.get('flip') or {}).values())
                    or any((clip.get('transform') or {}).values())):
                raise UnsupportedExternalRender('暂不支持该片段的旋转、透明度或翻转变换')
            # Static punch-in and the common linear zoom request are rendered
            # as a centered crop. Unknown keyframe curves remain explicit
            # failures rather than being silently discarded.
            zoom = clip.get('zoom') or segment.get('zoom')
            keyframes = _zoom_points(segment, source_start, duration)
            if isinstance(clip.get('scale'), dict):
                sx, sy = clip['scale'].get('x', 1), clip['scale'].get('y', 1)
                if sx != sy or type(sx) not in (int, float) or not 0.5 <= float(sx) <= 2:
                    raise UnsupportedExternalRender('非等比缩放无法复现')
                if sx != 1:
                    zoom = float(sx)
            if zoom is not None and (type(zoom) not in (int, float) or not 1 <= float(zoom) <= 2):
                raise UnsupportedExternalRender('缩放关键帧范围无法复现')
            transition = segment.get('transition_after') or segment.get('transition')
            if transition is None:
                for ref in segment.get('extra_material_refs') or []:
                    value = transition_materials.get(ref)
                    if value:
                        # Only this known basic dissolve is mapped. Never
                        # reinterpret an arbitrary Jianying effect as a fade.
                        if str(value.get('resource_id')) not in ('6724845717472416269', '6919369138800431629'):
                            raise UnsupportedExternalRender('不支持剪映转场：' + str(value.get('name') or value.get('resource_id')))
                        transition = {'type': 'dissolve', 'duration_us': int(value['duration']), 'name': value.get('name')}
                        break
            fade_in, fade_out = _fades(segment, materials)
            visual_filters = []
            color = segment.get('filter') or {}
            if color:
                if not isinstance(color, dict) or color.get('type') != 'ad_color':
                    raise UnsupportedExternalRender('不支持的直接滤镜：' + str(color.get('type') if isinstance(color, dict) else color))
                contrast = float(color.get('contrast', 1.0)); saturation = float(color.get('saturation', 1.0)); brightness = float(color.get('brightness', 0.0))
                if not 0.5 <= contrast <= 1.5 or not 0.5 <= saturation <= 1.5 or not -0.5 <= brightness <= 0.5:
                    raise UnsupportedExternalRender('直接滤镜参数超出安全范围')
                visual_filters.append(f'eq=contrast={contrast:.4f}:saturation={saturation:.4f}:brightness={brightness:.4f}')
            effect = segment.get('effect') or {}
            if effect:
                if not isinstance(effect, dict):
                    raise UnsupportedExternalRender('画面特效参数无效')
                try:
                    visual_filters.extend(effect_filters(effect))
                except ValueError as exc:
                    raise UnsupportedExternalRender(str(exc)) from exc
            layout = segment.get('layout') or {}
            layout_crop = _layout_crop_spec(material or {}, layout, snapshot.get('canvas_config') or {})
            rows.append({"target_start": start, "target_duration": duration,
                         "source_start": source_start, "source_duration": min(duration, source_duration),
                         "path": path, "volume": segment.get('volume', 1),
                         "has_audio": bool((material or {}).get("has_audio", True)),
                         "zoom": float(zoom) if zoom is not None else 1.0,
                         "zoom_points": keyframes, "fade_in_us": fade_in, "fade_out_us": fade_out,
                         "is_image": material.get('type') == 'photo' or path.suffix.lower() in ('.jpg','.jpeg','.png','.webp'),
                         "transition": transition, "visual_filters": visual_filters,
                         "layout_mode": layout.get('mode') if isinstance(layout, dict) else None,
                         "layout_crop": layout_crop})
    rows.sort(key=lambda item: item["target_start"])
    end = int(snapshot.get("duration") or 0)
    previous_end = 0
    for row in rows:
        row_end = row["target_start"] + row["target_duration"]
        if row["target_start"] < previous_end or row_end > end:
            raise ValueError("视频片段超出成品绝对时间范围或相互重叠")
        if row["target_start"] != previous_end:
            raise ValueError("成品内部存在未定义的空白区段，拒绝猜测导出")
        previous_end = row_end
    if not rows or previous_end != end:
        raise ValueError("视频轨道未覆盖成品完整时间范围")
    return rows


def _audio_segments(snapshot, duration):
    """Collect music, narration and SFX tracks without touching the draft."""
    materials = _materials(snapshot)
    rows = []
    for track in snapshot.get('tracks') or []:
        if track.get('type') not in ('audio', 'music', 'sound'):
            continue
        for segment in track.get('segments') or []:
            timerange = segment.get('target_timerange') or {}
            start, length = timerange.get('start'), timerange.get('duration')
            if type(start) is not int or type(length) is not int or length <= 0 or start < 0 or start + length > duration:
                raise ValueError('音频片段缺少合法绝对时间范围')
            material = materials.get(segment.get('material_id'))
            path = _media_path(material)
            if not path or not path.is_file():
                raise ValueError('音频片段的源素材不存在')
            source = segment.get('source_timerange') or {}
            source_start = source.get('start', 0)
            source_duration = source.get('duration', length)
            if type(source_start) is not int or type(source_duration) is not int or source_start < 0 or source_duration <= 0:
                raise ValueError('音频片段缺少合法源时间范围')
            if segment.get('speed', 1) != 1 or segment.get('reverse') or segment.get('common_keyframes'):
                raise UnsupportedExternalRender('音频变速或关键帧尚不支持外部导出')
            fade_in, fade_out = _fades(segment, materials)
            rows.append({'target_start': start, 'target_duration': length,
                         'source_start': source_start, 'source_duration': min(source_duration, length),
                         'path': path, 'volume': float(segment.get('volume', 1)),
                         'fade_in_us': fade_in, 'fade_out_us': fade_out})
            rows[-1]['loop'] = bool(segment.get('loop'))
    return rows


def _sticker_segments(snapshot, duration):
    materials = _materials(snapshot)
    rows = []
    for track in snapshot.get('tracks') or []:
        if track.get('type') != 'sticker':
            continue
        for segment in track.get('segments') or []:
            timerange = segment.get('target_timerange') or {}
            start, length = timerange.get('start'), timerange.get('duration')
            if type(start) is not int or type(length) is not int or length <= 0 or start < 0 or start + length > duration:
                raise ValueError('贴纸片段缺少合法绝对时间范围')
            material = materials.get(segment.get('material_id'))
            path = _media_path(material)
            if not path or not path.is_file():
                raise ValueError('贴纸资源不存在')
            position = segment.get('position') or {}
            x = position.get('x', 'main_w-overlay_w-40')
            y = position.get('y', '40')
            if not isinstance(x, (int, str)) or not isinstance(y, (int, str)):
                raise ValueError('贴纸位置无效')
            animation = segment.get('animation', 'none')
            if animation != 'none' and animation not in ANIMATIONS:
                raise UnsupportedExternalRender('不支持的贴纸动画：' + str(animation))
            from PIL import Image
            if animation != 'none':
                with Image.open(path) as image:
                    width, height = image.size
            else:
                width = height = 0
            rows.append({'target_start': start, 'target_duration': length,
                         'path': path, 'x': str(x), 'y': str(y),
                         'animation': animation, 'width': width, 'height': height})
    return rows


def _subtitle_texts(snapshot):
    return [(start, end, text) for start, end, text, _layer in _subtitle_rows(snapshot)]


def _subtitle_rows(snapshot, *, with_styles=False, with_motion=False):
    materials = _materials(snapshot)
    rows = []
    layer = 0
    for track in snapshot.get('tracks') or []:
        if track.get('type') != 'text':
            continue
        for segment in track.get('segments') or []:
            timerange = segment.get('target_timerange') or {}
            start, length = timerange.get('start'), timerange.get('duration')
            if type(start) is not int or type(length) is not int or length <= 0:
                raise ValueError('字幕片段缺少合法绝对时间范围')
            material = materials.get(segment.get('material_id')) or {}
            raw_text = segment.get('text') or material.get('content') or material.get('text') or ''
            # Jianying stores editable text materials as a JSON document
            # containing styles plus a separate ``text`` field.  Rendering
            # the raw content would burn font paths and style metadata into
            # the video, so unwrap the user-visible text before materializing
            # subtitle images or SRT cues.
            text = raw_text
            if isinstance(raw_text, dict):
                text = raw_text.get('text') or raw_text.get('content') or ''
            elif isinstance(raw_text, str):
                try:
                    decoded = json.loads(raw_text)
                except (TypeError, ValueError):
                    decoded = None
                if isinstance(decoded, dict):
                    text = decoded.get('text') or ''
            if not isinstance(text, str) or not text.strip():
                raise ValueError('字幕片段缺少文字内容')
            row = (start, start + length, text.strip(), layer)
            if with_styles:
                from .subtitle_styles import DEFAULT_STYLE, normalize_style
                style = dict(DEFAULT_STYLE)
                for source in (snapshot, material, segment):
                    style.update(normalize_style(source.get('subtitle_style')))
                row += (style, segment.get('font_selection') or material.get('font_selection'),)
            if with_motion:
                motion=dict(segment.get('caption_motion') or material.get('caption_motion') or {})
                motion['phase_us']=max(0,start-segment.get('caption_origin_start_us',start))
                row+=(motion,)
            rows.append(row)
        layer += 1
    return sorted(rows, key=lambda row: row[:4])


def _materialize_subtitles(snapshot, output_dir):
    if snapshot.get('subtitle_path'):
        path = Path(snapshot['subtitle_path']).resolve()
        if not path.is_file():
            raise ValueError('字幕文件不存在')
        return str(path)
    rows = _subtitle_texts(snapshot)
    if not rows:
        return None
    path = Path(output_dir) / ('.external-' + _safe_name(snapshot.get('id', 'draft')) + '.srt')
    def clock(us):
        ms = us // 1000
        return f'{ms//3600000:02d}:{(ms//60000)%60:02d}:{(ms//1000)%60:02d},{ms%1000:03d}'
    content = '\n\n'.join(f'{i}\n{clock(a)} --> {clock(b)}\n{text}' for i, (a, b, text) in enumerate(rows, 1)) + '\n'
    path.write_text(content, encoding='utf-8-sig')
    return str(path)


def _read_srt_rows(path):
    """Read a small SRT sidecar into the same absolute-time cue format."""
    text = Path(path).read_text(encoding='utf-8-sig')
    rows = []
    for block in re.split(r'\r?\n\s*\r?\n', text.strip()):
        lines = [line.strip('\ufeff') for line in block.splitlines() if line.strip()]
        if len(lines) < 3 or '-->' not in lines[1]:
            continue
        left, right = [part.strip().split(',') for part in lines[1].split('-->', 1)]
        def parse(value):
            h, m, s = value[0].split(':')
            return ((int(h) * 3600 + int(m) * 60 + float(s)) * 1_000_000) + int(value[1]) * 1000
        rows.append((int(parse(left)), int(parse(right)), ' '.join(lines[2:])))
    return rows


def _wrap_caption(text, font, max_width):
    """Preserve ordinary Latin words; CJK retains character wrap points."""
    from PIL import Image, ImageDraw
    draw = ImageDraw.Draw(Image.new('RGB',(1,1)))
    tokens = re.findall(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|\s+|.", str(text).replace('\n',' ').replace('\r',' '))
    line='';lines=[]
    for token in tokens:
        if not line:token=token.lstrip()
        if line and draw.textlength(line+token,font=font)>max_width:
            lines.append(line.rstrip());line='';token=token.lstrip()
        # A URL or a single exceptionally long word still must fit the frame.
        if draw.textlength(token,font=font)>max_width:
            for char in token:
                if line and draw.textlength(line+char,font=font)>max_width:
                    lines.append(line.rstrip());line=''
                line+=char
        else:line+=token
    if line.strip():lines.append(line.rstrip())
    return lines


def _subtitle_image_rows(snapshot):
    if not snapshot.get('subtitle_path'):
        return _subtitle_rows(snapshot, with_styles=True, with_motion=True)
    from .subtitle_styles import DEFAULT_STYLE, normalize_style
    from .caption_layout import expand_cues
    base_style = {**DEFAULT_STYLE, **normalize_style(snapshot.get('subtitle_style'))}
    offset = snapshot.get('subtitle_time_offset_us', 0)
    if type(offset) is not int or offset < 0:
        raise ValueError('字幕文件时间偏移无效')
    duration = snapshot['duration']
    cues = []
    for start, end, text in _read_srt_rows(snapshot['subtitle_path']):
        a, b = max(0, start-offset), min(duration, end-offset)
        if a >= b:
            continue
        motion = dict(snapshot.get('caption_motion') or {})
        if start < offset:
            motion['entry'] = 'none'
        cues.append({'start_us': a, 'end_us': b, 'text': text,
                     'subtitle_style': base_style, 'caption_motion': motion,
                     'caption_origin_start_us': start-offset})
    cues = expand_cues(cues, snapshot.get('caption_layout_schedule') or [])
    return [(cue['start_us'], cue['end_us'], cue['text'], 0,
             cue['subtitle_style'], snapshot.get('font_selection'),
             {**cue['caption_motion'],
              'phase_us': max(0, cue['start_us']-cue['caption_origin_start_us'])})
            for cue in cues]


def _materialize_subtitle_images(snapshot, output_dir, *, font_path=None, ffmpeg_path=None):
    """Render subtitles as transparent PNGs when bundled FFmpeg lacks libass.

    The Jianying FFmpeg build used on this machine does not ship ``subtitles``
    or ``drawtext``.  PNG overlays keep the text visible while preserving the
    immutable-draft contract.  Files are written only to the exporter temp
    directory and are removed after the render.
    """
    from .subtitle_styles import DEFAULT_STYLE, normalize_style, rgba
    rows = _subtitle_image_rows(snapshot)
    if not rows:
        return []
    if snapshot.get('subtitle_path') and not font_path:
        # SRT cues have no text material carrying a selected font. Use the
        # same verified bundled selector as normal captions, per cue, rather
        # than silently omitting them or depending on a system font fallback.
        from .auto_decorations import select_caption_font
        rows = [(start, end, text, layer, style,
                 selected or select_caption_font(snapshot, {'text': text}, []), motion)
                for start, end, text, layer, style, selected, motion in rows]
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise UnsupportedExternalRender('外部导出缺少 Pillow，无法可靠渲染字幕') from exc
    canvas = snapshot.get('canvas_config') or {}
    width, height = int(canvas.get('width', 0)), int(canvas.get('height', 0))
    if width <= 0 or height <= 0:
        raise ValueError('草稿缺少明确画幅，无法渲染字幕')
    chosen_font = Path(font_path).resolve() if font_path else None
    if not chosen_font:
        selected = next((row[5] for row in rows if row[5]), None)
        if selected:
            chosen_font = Path(selected['path']).resolve()
    if not chosen_font or not chosen_font.is_file():
        # Prefer the licensed font copied into the isolated draft.
        candidates = []
        for suffix in ('*.ttf', '*.ttc', '*.otf'):
            candidates.extend(Path(output_dir).glob(suffix))
            candidates.extend(Path(output_dir).parent.glob('fonts/' + suffix))
        chosen_font = next((item for item in candidates if item.is_file()), None)
    if not chosen_font:
        raise UnsupportedExternalRender('没有可验证的中文字体，拒绝用系统回退字体导出字幕')
    try:
        font = ImageFont.truetype(str(chosen_font), max(28, min(64, width // 18)))
    except OSError as exc:
        raise UnsupportedExternalRender('字幕字体无法读取') from exc
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = []
    margin = max(24, width // 18)
    line_gap = max(8, width // 160)
    for index, (start, end, text, layer, style, font_selection, motion) in enumerate(rows, 1):
        if end <= start:
            continue
        font_size = round(max(28, min(64, width // 18))*style.get('font_scale',1))
        if font_selection:
            from .auto_decorations import load_selected_font
            font = load_selected_font(font_selection, font_size)
        else:
            font = ImageFont.truetype(str(chosen_font), font_size)
        # Include the background padding and outline in the safe width.
        max_width = width - 2*margin - 2*max(18,width//60) - 2*style['stroke_width']
        if 'wrap_width' in style:max_width=min(max_width,int(width*style['wrap_width']))
        position = style.get('position')
        vertical, horizontal = position.split('_') if position else (None, None)
        # A left/right request must remain visibly on that side even for long
        # copy; wrapping is preferable to a full-width supposedly left layer.
        if horizontal in ('left','right'):
            max_width = min(max_width, int(width*.6)-2*max(18,width//60))
        def arranged_lines():
            if style.get('line_count',1)>1:
                count=style['line_count']
                tokens=re.findall(r"[A-Za-z0-9]+(?:['’\-][A-Za-z0-9]+)*|\s+|.",text.replace('\n',' '))
                chunks=[];remaining=tokens[:]
                for part in range(count-1):
                    if len(remaining)<=1:break
                    measure=ImageDraw.Draw(Image.new('RGB',(1,1)))
                    target=measure.textlength(''.join(remaining),font=font)/(count-part)
                    # Avoid orphaned Japanese/Chinese closing punctuation at
                    # the beginning of a balanced subtitle line.
                    cuts=[k for k in range(1,len(remaining))
                          if remaining[k].lstrip()[:1] not in '、。，．！？!?；;：:)]）】」』》〉…ー'
                          and remaining[k-1].rstrip()[-1:] not in '([（【「『《〈']
                    cut=min(cuts or range(1,len(remaining)),key=lambda k:abs(measure.textlength(''.join(remaining[:k]),font=font)-target))
                    chunks.append(''.join(remaining[:cut]).strip());remaining=remaining[cut:]
                chunks.append(''.join(remaining).strip())
                return [line for chunk in chunks if chunk for line in _wrap_caption(chunk,font,max_width)]
            return _wrap_caption(text,font,max_width)
        max_lines=style.get('max_lines',2 if snapshot.get('caption_mode')=='highlights' else 4)
        lines = arranged_lines()
        while len(lines)>max_lines and font_size>max(18,width//45):
            font_size=max(max(18,width//45),font_size-2)
            font=load_selected_font(font_selection,font_size) if font_selection else ImageFont.truetype(str(chosen_font),font_size)
            lines=arranged_lines()
        if len(lines)>max_lines:
            raise ValueError('字幕超过指定行数，请缩短文案或增加排版宽度')
        cue_line_gap=max(0,round(font_size*(style['line_spacing']-1))) if 'line_spacing' in style else line_gap
        probe = Image.new('RGBA', (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(probe)
        stroke_width = style['stroke_width']
        boxes = [draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width) for line in lines]
        text_w = max((box[2] - box[0] for box in boxes), default=0)
        text_h = sum(box[3] - box[1] for box in boxes) + cue_line_gap * max(0, len(lines) - 1)
        pad_x, pad_y = max(18, width // 60), max(12, width // 90)
        box_left = max(margin, (width - text_w) // 2 - pad_x)
        box_right = min(width - margin, (width + text_w) // 2 + pad_x)
        if position:
            box_width, box_height = text_w+2*pad_x, text_h+2*pad_y
            safe_y = max(24, height//24)
            if box_width > width-2*margin or box_height > height-2*safe_y:
                raise ValueError('字幕过长，超出指定位置的画面范围，请缩短字幕或拆分句子')
            box_left = (margin if horizontal=='left' else width-margin-box_width
                        if horizontal=='right' else (width-box_width)//2)
            box_top = (safe_y if vertical=='top' else height-safe_y-box_height
                       if vertical=='bottom' else (height-box_height)//2)
            box_right,box_bottom = box_left+box_width,box_top+box_height
        elif snapshot.get('subtitle_placement') == 'upper':
            box_top = max(100, height // 8)
            box_bottom = min(height, box_top + text_h + 2 * pad_y)
        elif layer % 2 == 0:
            box_bottom = height - max(80, height // 15)
            box_top = max(0, box_bottom - text_h - 2 * pad_y)
        else:
            box_top = max(80, height // 18)
            box_bottom = min(height, box_top + text_h + 2 * pad_y)
        if 'x' in style:
            panel_width=box_right-box_left
            box_left=max(margin,min(width-margin-panel_width,round(width*style['x']-panel_width/2)))
            box_right=box_left+panel_width
        if 'y' in style:
            panel_height=box_bottom-box_top
            box_top=max(24,min(height-24-panel_height,round(height*style['y']-panel_height/2)))
            box_bottom=box_top+panel_height
        if rgba(style['background_color'])[3]:
            draw.rounded_rectangle((box_left, box_top, box_right, box_bottom), radius=max(8, width // 90),
                                   fill=rgba(style['background_color']))
        background=probe.copy()
        glyphs=[]
        y = box_top + pad_y
        for line, box in zip(lines, boxes):
            tw = box[2] - box[0]
            text_x = (box_left+pad_x if horizontal=='left' else box_right-pad_x-tw
                      if horizontal=='right' else (box_left+box_right-tw)/2)
            from .caption_motion import glyph_layer, graphemes
            prefix=''
            for cluster in graphemes(line):
                glyph,bbox=glyph_layer(cluster,font,fill=rgba(style['fill_color']),
                    stroke_width=stroke_width,stroke_fill=rgba(style['stroke_color']))
                glyphs.append({'image':glyph,'x':text_x-box[0]+draw.textlength(prefix+cluster,font=font)-draw.textlength(cluster,font=font)+bbox[0],
                               'y':y-box[1]+bbox[1]})
                prefix+=cluster
            draw.text((text_x - box[0], y - box[1]), line, font=font,
                      fill=rgba(style['fill_color']), stroke_width=stroke_width,
                      stroke_fill=rgba(style['stroke_color']))
            y += box[3] - box[1] + cue_line_gap
        path = output_dir / f'.subtitle-{index:03d}-{int(start)}-{int(end)}.png'
        probe.save(path, 'PNG')
        metadata={'path': str(path), 'start_us': int(start), 'end_us': int(end),
                       'text': str(text), 'layer': int(layer), 'style': dict(style),
                       'font_selection': font_selection, 'lines':lines, 'font_size':font_size,
                       'position': position or ('top_center' if snapshot.get('subtitle_placement')=='upper' or layer%2 else 'bottom_center'),
                       'bounds':[box_left,box_top,box_right,box_bottom]}
        from .caption_motion import normalize_motion, render_caption_motion
        phase=motion.get('phase_us',0)
        normalized=normalize_motion({k:v for k,v in motion.items() if k!='phase_us'})
        if normalized['entry']!='none' or normalized['loop']!='none':
            if not ffmpeg_path:ffmpeg_path=discover_tools(Path(__file__).resolve().parents[1]).get('ffmpeg')
            animated=render_caption_motion(probe,glyphs,output_dir,name=f'caption-{index:03d}',
                start_us=start,end_us=end,motion=normalized,ffmpeg_path=ffmpeg_path,
                fps=int(snapshot.get('fps',30)),background_image=background,phase_us=phase)
            result.extend({**metadata,**resource} for resource in animated)
        else:result.append(metadata)
    return result


def build_ffmpeg_command(snapshot, output_path, ffmpeg_path, *, subtitle_images=None):
    """Build a deterministic concat filter for the snapshot's video segments."""
    tracks = snapshot.get("tracks") or []
    if snapshot.get('external_omitted_groups'):
        raise UnsupportedExternalRender('快照曾移除草稿效果，拒绝将删减版标为完整成品：' + ', '.join(snapshot['external_omitted_groups']))
    # Audio, music and sound tracks are rendered through _audio_segments and
    # mixed below. They used to be rejected here, which made every formal ad
    # with narration or BGM fail before FFmpeg was even invoked.
    unsupported = [track.get("type") for track in tracks
                   if track.get("type") not in ("video", "text", "sticker", "audio", "music", "sound")
                   and track.get("segments")]
    text_tracks = [track for track in tracks if track.get("type") == "text" and track.get("segments")]
    if unsupported:
        raise UnsupportedExternalRender("外部导出暂不支持这些轨道：" + ', '.join(unsupported))
    for group in ('effects', 'video_effects', 'filters', 'masks', 'material_animations'):
        if (snapshot.get('materials') or {}).get(group):
            raise UnsupportedExternalRender('外部导出暂不支持草稿效果：' + group)
    rows = _video_segments(snapshot)
    sticker_rows = _sticker_segments(snapshot, int(snapshot.get('duration') or 0))
    canvas = snapshot.get('canvas_config') or {}
    width, height = canvas.get('width'), canvas.get('height')
    fps = snapshot.get('fps')
    if not all(type(v) is int and v > 0 for v in (width, height, fps)):
        raise ValueError('草稿缺少明确画幅或帧率')
    subtitle_images = list(subtitle_images or [])
    # Multi-source vertical ads can create several full-size frame buffers
    # (xfade, subtitle and sticker overlays).  The bundled FFmpeg may report
    # an allocation failure even when system memory is available if those
    # filters fan out across all worker threads.  Keep the exact same filter
    # graph and constrain only its scheduling/codec workers; this trades a
    # little render time for deterministic memory use and does not reduce
    # output resolution or quality settings.
    command = [str(ffmpeg_path), "-hide_banner", "-y", "-loglevel", "error",
               "-filter_threads", "1", "-filter_complex_threads", "1",
               # Keep a bounded filter queue.  The direct ad graph can contain
               # several full-frame subtitle/sticker overlays; an unbounded
               # queue turns temporary pressure into an OS-level allocation
               # failure.  This is a safety bound, not a quality downgrade:
               # FFmpeg fails truthfully if a graph cannot be scheduled.
               "-filter_buffered_frames", "256"]
    for row in rows:
        if row['is_image']:
            command += ['-loop', '1']
        if snapshot.get('creation_mode') == 'long_video_v1' or len(rows) > 10:
            command += ['-threads','1']
        command += ["-ss", f"{_iso_seconds(row['source_start']):.6f}",
                    "-t", f"{_iso_seconds(row['source_duration']):.6f}", "-i", str(row["path"])]
    audio_rows = _audio_segments(snapshot, int(snapshot['duration']))
    filters = []
    video_labels = []
    source_audio_labels = []
    for index, row in enumerate(rows):
        duration = _iso_seconds(row['target_duration'])
        zoom = row.get('zoom', 1.0)
        layout_crop = row.get('layout_crop')
        layout_filter = ''
        if layout_crop:
            ratio = float(layout_crop['target_ratio'])
            center = _layout_center_expression(layout_crop['points']).replace(',', r'\,')
            if layout_crop['axis'] == 'x':
                crop_width = f"ih*{ratio:.8f}"
                layout_filter = f"crop={crop_width}:ih:x='(iw-{crop_width})*{center}':y=0,"
            else:
                crop_height = f"iw/{ratio:.8f}"
                layout_filter = f"crop=iw:{crop_height}:x=0:y='(ih-{crop_height})*{center}',"
        # Apply the punch-in before the final canvas normalization.  Cropping
        # after padding changes the link dimensions and makes concat reject a
        # perfectly valid mixed-zoom sequence.
        zoom_filter = ('' if zoom == 1 else
                        f"crop=iw/{zoom}:ih/{zoom}:(iw-iw/{zoom})/2:(ih-ih/{zoom})/2,")
        if row['zoom_points']:
            expression = _zoom_expression(row['zoom_points'], fps)
            zoom_filter = ''
            motion = f",zoompan=z='{expression}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={width}x{height}:fps={fps}"
        else:
            motion = ''
        visual_prefix = (','.join(row.get('visual_filters') or []) + ',') if row.get('visual_filters') else ''
        filters.append(f"[{index}:v]{visual_prefix}{layout_filter}{zoom_filter}scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                       f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},"
                       f"trim=duration={duration:.6f},setpts=PTS-STARTPTS{motion},settb=AVTB[v{index}]")
        video_labels.append(f"[v{index}]")
        audio_fade = ''
        if row['fade_in_us']:
            audio_fade += f",afade=t=in:d={row['fade_in_us']/1e6:.6f}"
        if row['fade_out_us']:
            audio_fade += f",afade=t=out:st={duration-row['fade_out_us']/1e6:.6f}:d={row['fade_out_us']/1e6:.6f}"
        if row["has_audio"] and not row['is_image'] and not snapshot.get('source_audio_muted', False):
            filters.append(f"[{index}:a]aresample=48000,aformat=channel_layouts=stereo,"
                           f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,volume={float(row['volume'])}{audio_fade}[a{index}]")
        else:
            filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a{index}]")
        source_audio_labels.append(f"[a{index}]")
    transitions = {}
    for i, row in enumerate(rows[:-1]):
        tr = row.get('transition')
        if not tr:
            continue
        kind = tr.get('type') if isinstance(tr, dict) else str(tr)
        kind = {'dissolve': 'fade', 'crossfade': 'fade'}.get(kind, kind)
        if kind not in TRANSITIONS:
            raise UnsupportedExternalRender('不支持的转场：' + str(kind))
        length = float(tr.get('duration_us', 300_000) if isinstance(tr, dict) else 300_000) / 1e6
        if not math.isfinite(length) or not 0 < length <= min(row['target_duration'], rows[i+1]['target_duration'])/2e6:
            raise ValueError('转场时长超出相邻镜头的可用范围')
        transitions[i] = (length, kind)
    # A centered dissolve uses explicit held boundary frames outside each
    # source trim. Padding half its length on either side keeps every cut,
    # caption and audio at the original absolute timeline position.
    extended = []
    for i in range(len(rows)):
        before = transitions.get(i-1, (0, None))[0] / 2
        after = transitions.get(i, (0, None))[0] / 2
        label = f'[v{i}]'
        if before or after:
            output = f'[vpadded{i}]'
            filters.append(f'{label}tpad=start_mode=clone:start_duration={before:.6f}:stop_mode=clone:stop_duration={after:.6f},fps={fps},settb=AVTB{output}')
            label = output
        extended.append(label)
    current = extended[0]
    accumulated = rows[0]['target_duration']/1e6
    for i in range(1, len(rows)):
        out = f'[vjoined{i}]'
        if i-1 in transitions:
            length, kind = transitions[i-1]
            if kind in COMPOUND_TRANSITIONS:
                filters.extend(compound_transition_filters(current,extended[i],out,kind,
                    duration=length,offset=accumulated-length/2,width=width,height=height,
                    fps=fps,prefix=f'compound{i}'))
            else:
                filters.append(f'{current}{extended[i]}xfade=transition={kind}:duration={length:.6f}:offset={accumulated-length/2:.6f}{out}')
        else:
            filters.append(f'{current}{extended[i]}concat=n=2:v=1:a=0{out}')
        current = out
        accumulated += rows[i]['target_duration']/1e6
    filters.append(f'{current}tpad=stop_mode=clone:stop_duration={1/fps:.6f},trim=duration={snapshot["duration"]/1e6:.6f},setpts=PTS-STARTPTS[vout]')
    filters.append(''.join(source_audio_labels) + f'concat=n={len(rows)}:v=0:a=1[aout]')
    input_index = len(rows)
    extra_audio_labels = []
    for offset, row in enumerate(audio_rows):
        # Music beds may be shorter than the output.  Looping is explicit in
        # the snapshot and is limited to those tracks; narration/SFX remain
        # bounded one-shot inputs.
        if row.get('loop'):
            command += ["-stream_loop", "-1"]
        command += ["-ss", f"{_iso_seconds(row['source_start']):.6f}",
                    "-t", f"{_iso_seconds(row['target_duration']):.6f}",
                    "-i", str(row['path'])]
        idx = input_index + offset
        delay = row['target_start'] / 1000
        fade = f",afade=t=in:st=0:d={row['fade_in_us']/1e6:.6f}" if row['fade_in_us'] > 0 else ''
        if row['fade_out_us'] > 0:
            fade += f",afade=t=out:st={max(0, row['target_duration']-row['fade_out_us'])/1e6:.6f}:d={row['fade_out_us']/1e6:.6f}"
        label = f'[extraa{offset}]'; extra_audio_labels.append(label)
        # Looping inputs must survive for the whole target slot. Trimming
        # them to the original source length silently discarded every repeat.
        audio_length = row['target_duration'] if row.get('loop') else row['source_duration']
        filters.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration={_iso_seconds(audio_length):.6f},"
                       f"asetpts=PTS-STARTPTS,volume={row['volume']:.6f}{fade},adelay={delay:.3f}:all=1{label}")
    if extra_audio_labels:
        labels = '[aout]' + ''.join(extra_audio_labels)
        filters.append(labels + f"amix=inputs={len(extra_audio_labels)+1}:duration=first:normalize=0,aresample=48000[aoutmix]")
        audio_map = '[aoutmix]'
    else:
        audio_map = '[aout]'
    video_map = "[vout]"
    sticker_input_base = len(rows) + len(audio_rows)
    for offset, row in enumerate(sticker_rows):
        animated = row['animation'] != 'none'
        command += ["-loop", "1", "-framerate", str(fps if animated else 1),
                    "-t", f"{_iso_seconds(row['target_duration']):.6f}",
                    "-i", str(row['path'])]
        label = f'[vsticker{offset}]'
        start = _iso_seconds(row['target_start'])
        end = _iso_seconds(row['target_start'] + row['target_duration'])
        sticker_label = f'[{sticker_input_base + offset}:v]'
        chain, x, y = sticker_motion(row, fps)
        if chain:
            motion_label = f'[sticker_motion{offset}]'
            filters.append(f'{sticker_label}{chain}{motion_label}')
            sticker_label = motion_label
        timing = ':eof_action=pass:repeatlast=0' if animated else ''
        filters.append(f"{video_map}{sticker_label}overlay=x='{x}':y='{y}':enable='between(t,{start:.6f},{end:.6f})':format=auto{timing}{label}")
        video_map = label
    # This bundled FFmpeg has no libass/drawtext filters.  Add each rendered
    # transparent subtitle image as an input and overlay it in its time range.
    subtitle_input_base = len(rows) + len(audio_rows) + len(sticker_rows)
    for offset, cue in enumerate(subtitle_images):
        animated=cue.get('kind')=='video'
        if animated:
            if cue.get('loop'):command += ['-stream_loop','-1']
        else:
            command += ['-loop','1','-framerate','1']
        command += ['-t',f"{_iso_seconds(cue['end_us']-cue['start_us']):.6f}",'-i',str(cue['path'])]
        label = f'[vsub{offset}]'
        start = _iso_seconds(cue['start_us'])
        end = _iso_seconds(cue['end_us'])
        input_label=f'[caption_pts{offset}]'
        filters.append(f'[{subtitle_input_base+offset}:v]setpts=PTS-STARTPTS+{start:.6f}/TB{input_label}')
        # Keep the last entrance frame alive through the exact handoff to the
        # static hold.  The qtrle alpha clip can end a fraction before its
        # nominal ``-t`` duration; using ``pass:repeatlast=0`` then exposes a
        # transparent frame between the pop and the hold, which reads as a
        # one-frame flash.  Caption overlays are bounded by ``enable`` below,
        # so repeating the settled frame cannot bleed past the cue.
        timing=(':eof_action=repeat:repeatlast=1' if animated else '')
        filters.append(f"{video_map}{input_label}overlay=x={cue.get('x',0)}:y={cue.get('y',0)}:enable='gte(t,{start:.6f})*lt(t,{end:.6f})':format=auto{timing}{label}")
        video_map = label
    command += ["-filter_complex", ";".join(filters), "-map", video_map, "-map", audio_map,
                # Browser and desktop previews need AVC, not MP4V. The
                # standalone Framo toolchain includes libx264. Keep bounded
                # encoder threads and a seekable, widely supported pixel format.
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-threads", str(render_encoder_threads()), "-pix_fmt", "yuv420p",
                "-r", str(fps), "-t", f"{_iso_seconds(snapshot['duration']):.6f}",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(output_path)]
    return command


class UnsupportedExternalRender(ValueError):
    code = "UNSUPPORTED_EXTERNAL_RENDER"


def write_voice_credits(snapshot, output_path):
    credits=snapshot.get('voice_attributions') or []
    if not credits:return None
    rows=['配音发布署名说明','发布视频时，请在视频说明或视频内保留以下署名。此文件本身不代替发布时的署名。','']
    for credit in credits:
        if not isinstance(credit,dict) or not all(isinstance(credit.get(k),str) and credit[k].strip() for k in ('attribution','terms_url')):
            raise ValueError('配音缺少发布署名说明，暂不能交付成品')
        rows.extend([credit['attribution'],'音色使用条款：'+credit['terms_url'],''])
    target=Path(output_path).with_name(Path(output_path).stem+'-配音署名.txt')
    temp=target.with_name('.'+target.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text('\n'.join(rows),encoding='utf-8-sig')
        temp.replace(target)
    finally:temp.unlink(missing_ok=True)
    return str(target)


def _probe_with_ffprobe(path, ffprobe):
    if not ffprobe:
        return None
    command = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                            stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return {"duration_s": float((data.get("format") or {}).get("duration") or 0),
            "width": (video or {}).get("width"), "height": (video or {}).get("height"),
            "fps": _frame_rate((video or {}).get('avg_frame_rate', '0')),
            "has_video": bool(video), "has_audio": bool(audio), "probe_backend": "ffprobe"}


def _frame_rate(value):
    parts = str(value).split('/')
    return float(parts[0]) / float(parts[1]) if len(parts) == 2 and float(parts[1]) else float(parts[0])


def probe_media(path, *, ffmpeg=None, ffprobe=None):
    """Return basic media evidence, using ffprobe or ffmpeg's read-only banner."""
    path = Path(path).resolve(strict=True)
    result = _probe_with_ffprobe(path, ffprobe)
    if result:
        return result
    if not ffmpeg:
        raise ValueError("缺少 FFmpeg，无法检查输出文件")
    proc = subprocess.run([ffmpeg, "-hide_banner", "-i", str(path)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=30,
                          stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    text = proc.stderr + "\n" + proc.stdout
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    duration = 0.0 if not match else int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
    dims = re.search(r"Video:.*?\b(\d{2,5})x(\d{2,5})\b", text, re.S)
    rate = re.search(r'Video:[^\r\n]*?([\d.]+) fps', text)
    return {"duration_s": duration, "width": int(dims.group(1)) if dims else None,
            "height": int(dims.group(2)) if dims else None,
            "fps": float(rate.group(1)) if rate else None,
            "has_video": bool(re.search(r"Stream.*Video:", text)),
            "has_audio": bool(re.search(r"Stream.*Audio:", text)),
            "probe_backend": "ffmpeg-banner", "ffprobe_unavailable": not bool(ffprobe)}


class IndependentExport:
    """One isolated export batch persisted in the user-selected directory."""
    def __init__(self, *, batch_id, master_snapshot, output_dir, install_dir=None,
                 conflict="rename", source_draft=None, cancel_event=None,
                 on_update: Callable[[dict], None] | None = None):
        if conflict not in CONFLICTS:
            raise ValueError("文件已存在处理方式必须为覆盖、跳过或自动重命名")
        self.batch_id = batch_id
        self.snapshot = master_snapshot
        if not output_dir:
            raise ValueError('请先选择独立导出输出文件夹')
        self.output_dir = Path(output_dir).resolve()
        self.tools = discover_tools(install_dir)
        if not self.tools["ffmpeg"]:
            raise ValueError("未找到 FFmpeg；请安装或配置可执行文件后再导出")
        self.conflict = conflict
        self.source_draft = Path(source_draft).resolve() if source_draft else None
        self.cancel_event = cancel_event or threading.Event()
        self.on_update = on_update

    def _report_path(self):
        return self.output_dir / REPORT_NAME

    def _write(self, report):
        atomic_json(self._report_path(), report)
        if self.on_update:
            self.on_update(report)

    def _items(self, selected=None):
        selected = set(selected or [row.get("output_id") for row in self.snapshot.get("batch_outputs", [])])
        items = []
        for row in self.snapshot.get("batch_outputs") or []:
            if row.get("output_id") not in selected:
                continue
            items.append({"output_id": row["output_id"],
                          "start_us": row.get("start_us"), "end_us": row.get("end_us")})
        if not items:
            raise ValueError("批次没有可导出的成品")
        return items

    def run(self, selected=None):
        source_paths = []
        for material in _materials(self.snapshot).values():
            path = _media_path(material)
            if path:
                source_paths.append(path)
        validate_output_directory(self.output_dir, source_paths=source_paths,
                                  draft_paths=[self.source_draft] if self.source_draft else [])
        prior = load_report(self.output_dir)
        if prior and prior.get('batch_id') != self.batch_id:
            raise ValueError('输出目录已有其他批次报告，请为本批次选择独立目录')
        report = {"schema_version": "2.0", "batch_id": self.batch_id,
                  "state": "PREPARING", "backend": "FFmpeg external renderer",
                  "ffmpeg": self.tools["ffmpeg"], "ffprobe": self.tools["ffprobe"],
                  "output_dir": str(self.output_dir), "conflict": self.conflict,
                   "started_at": _utc(), "finished_at": None, "items": [],
                   "source_feature_notes": list(self.snapshot.get('external_omitted_groups') or []),
                   "progress": {"completed": 0, "total": 0},
                  "summary": {"success": 0, "failed": 0, "skipped": 0, "cancelled": 0}}
        self._write(report)
        try:
            items = self._items(selected)
        except Exception as exc:
            report["state"] = "FAILED"; report["error"] = str(exc); report["finished_at"] = _utc(); self._write(report); return report
        prior_rows = {row.get("output_id"): row for row in (prior or {}).get("items", []) if isinstance(row, dict)}
        selected_ids = {item["output_id"] for item in items}
        for existing in (prior or {}).get("items", []):
            if existing.get("output_id") not in selected_ids:
                report["items"].append(existing)
                status = existing.get("status")
                if status == "COMPLETED": report["summary"]["success"] += 1
                elif status == "FAILED": report["summary"]["failed"] += 1
                elif status == "SKIPPED": report["summary"]["skipped"] += 1
                elif status == "CANCELLED": report["summary"]["cancelled"] += 1
        for item in items:
            row = {"output_id": item["output_id"], "status": "QUEUED",
                   "input_draft": str(self.source_draft) if self.source_draft else None,
                   "time_range_us": [item["start_us"], item["end_us"]],
                   "output_dir": str(self.output_dir), "output_file": None,
                   "command_summary": None, "started_at": None, "finished_at": None,
                   "file_size": None, "actual_duration_s": None, "error": None,
                   "playback_ok": False, "media_check": None}
            report["items"].append(row)
        report["progress"]["total"] = len(report["items"])
        report["state"] = "EXPORTING"; self._write(report)
        selected_rows = [row for row in report["items"] if row.get("output_id") in selected_ids]
        for item, row in zip(items, selected_rows):
            if self.cancel_event.is_set():
                row["status"] = "CANCELLED"; report["summary"]["cancelled"] += 1
                report["progress"]["completed"] = sum(item.get("status") in ("COMPLETED", "FAILED", "SKIPPED", "CANCELLED") for item in report["items"])
                self._write(report); continue
            row["status"] = "PREPARING"; row["started_at"] = _utc(); self._write(report)
            temp = None
            subtitle_temp = None
            subtitle_images = []
            try:
                if (type(item['start_us']) is not int or type(item['end_us']) is not int
                        or item['start_us'] < 0 or item['start_us'] >= item['end_us']):
                    raise ValueError('成品时间范围无法确定')
                item['snapshot'] = _extract_output(self.snapshot, item['output_id'])
                # Materialize editable text as isolated PNG overlays for the
                # duration of this render. The bundled FFmpeg has no libass or
                # drawtext, so this keeps subtitles visible without touching
                # the draft or source tree.
                if _subtitle_texts(item['snapshot']) or item['snapshot'].get('subtitle_path'):
                    subtitle_temp = Path(tempfile.mkdtemp(prefix='.jianying-export-', dir=self.output_dir))
                    font_path = None
                    if self.source_draft:
                        fonts = []
                        for suffix in ('*.ttf', '*.ttc', '*.otf'):
                            fonts.extend(self.source_draft.glob('fonts/' + suffix))
                        fonts = sorted(fonts)
                        font_path = str(fonts[0]) if fonts else None
                    subtitle_images = _materialize_subtitle_images(item['snapshot'], subtitle_temp,
                                                                    font_path=font_path,ffmpeg_path=self.tools['ffmpeg'])
                command = build_ffmpeg_command(item["snapshot"], self.output_dir / ("." + _safe_name(item["output_id"]) + ".tmp.mp4"), self.tools["ffmpeg"],
                                               subtitle_images=subtitle_images)
                final = self.output_dir / (_safe_name(item["output_id"]) + ".mp4")
                if final.exists() and self.conflict == "skip":
                    row["status"] = "SKIPPED"; row["output_file"] = str(final); report["summary"]["skipped"] += 1; row["finished_at"] = _utc(); self._write(report); continue
                if final.exists() and self.conflict == "rename":
                    index = 2
                    while (self.output_dir / f"{final.stem}-{index}{final.suffix}").exists(): index += 1
                    final = self.output_dir / f"{final.stem}-{index}{final.suffix}"
                temp = self.output_dir / ("." + final.stem + '.' + uuid.uuid4().hex + ".tmp.mp4")
                command[-1] = str(temp)
                row["command_summary"] = " ".join(command)
                row["status"] = "EXPORTING"; self._write(report)
                proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                while True:
                    if self.cancel_event.is_set():
                        proc.terminate()
                        try: proc.wait(timeout=5)
                        except subprocess.TimeoutExpired: proc.kill()
                        raise CancelledError("用户取消导出")
                    try:
                        _, stderr = proc.communicate(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if proc.returncode != 0 or not temp.is_file() or temp.stat().st_size <= 0:
                    raise RuntimeError(stderr[-2000:] or "FFmpeg 未生成有效 MP4")
                media = probe_media(temp, ffmpeg=self.tools["ffmpeg"], ffprobe=self.tools["ffprobe"])
                canvas = item['snapshot']['canvas_config']
                if (abs(media['duration_s'] - (item['end_us'] - item['start_us']) / 1e6) > 0.1
                        or media['width'] != canvas['width'] or media['height'] != canvas['height']
                        or media.get('fps') != item['snapshot']['fps']
                        or not media['has_audio']):
                    raise RuntimeError('输出时长、画幅或音频流检查不通过：' + str(media))
                playback = subprocess.run([self.tools["ffmpeg"], "-v", "error", "-xerror", "-i", str(temp), "-f", "null", "-"],
                                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                                           stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                if playback.returncode != 0 or not media["has_video"]:
                    raise RuntimeError((playback.stderr or "输出播放检查失败")[-2000:])
                row['voice_attribution_file']=write_voice_credits(item['snapshot'],final)
                temp.replace(final)
                row.update({"status": "COMPLETED", "output_file": str(final), "file_size": final.stat().st_size,
                            "actual_duration_s": media["duration_s"], "media_check": media,
                            "playback_ok": True, "finished_at": _utc()})
                report["summary"]["success"] += 1
            except CancelledError as exc:
                row.update({"status": "CANCELLED", "error": str(exc), "finished_at": _utc()}); report["summary"]["cancelled"] += 1
            except UnsupportedExternalRender as exc:
                row.update({"status": "FAILED", "error": f"{exc.code}: {exc}", "finished_at": _utc()}); report["summary"]["failed"] += 1
            except Exception as exc:
                row.update({"status": "FAILED", "error": str(exc), "finished_at": _utc()}); report["summary"]["failed"] += 1
            finally:
                if temp and temp.is_file():
                    temp.unlink()
                if subtitle_temp and subtitle_temp.is_dir():
                    shutil.rmtree(subtitle_temp, ignore_errors=True)
            report["progress"]["completed"] = sum(item.get("status") in ("COMPLETED", "FAILED", "SKIPPED", "CANCELLED") for item in report["items"])
            self._write(report)
        report["finished_at"] = _utc()
        report["summary"] = {"success": sum(row.get("status") == "COMPLETED" for row in report["items"]),
                              "failed": sum(row.get("status") == "FAILED" for row in report["items"]),
                              "skipped": sum(row.get("status") == "SKIPPED" for row in report["items"]),
                              "cancelled": sum(row.get("status") == "CANCELLED" for row in report["items"])}
        report["state"] = "CANCELLED" if report["summary"]["cancelled"] and not report["summary"]["success"] and not report["summary"]["failed"] else ("COMPLETED" if not report["summary"]["failed"] and not report["summary"]["cancelled"] else "FAILED")
        self._write(report)
        return report


class CancelledError(RuntimeError):
    pass


def _extract_output(master, output_id):
    """Local zero-based extraction to keep this module usable without service."""
    rows = [row for row in master.get("batch_outputs") or [] if row.get("output_id") == output_id]
    if len(rows) != 1:
        raise ValueError("未知或重复的成品 ID")
    row = rows[0]; start, end = row["start_us"], row["end_us"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= master['duration']:
        raise ValueError('成品时间范围无法确定')
    child = json.loads(json.dumps(master, ensure_ascii=False))
    child["duration"] = end - start
    child["batch_outputs"] = [{**row, "start_us": 0, "end_us": end - start, "gap_after_us": 0}]
    if child.get('subtitle_path'):
        child['subtitle_time_offset_us'] = child.get('subtitle_time_offset_us', 0) + start
    child['caption_layout_schedule'] = [
        {**scope, 'start_us': max(start, scope['start_us']) - start,
         'end_us': min(end, scope['end_us']) - start}
        for scope in child.get('caption_layout_schedule') or []
        if scope['start_us'] < end and scope['end_us'] > start
    ]
    for track in child.get("tracks") or []:
        kept = []
        for segment in track.get("segments") or []:
            tr = segment.get("target_timerange") or {}; a = tr.get("start", -1); b = a + tr.get("duration", -1)
            if start <= a and b <= end:
                value = json.loads(json.dumps(segment))
                value["target_timerange"]["start"] = a - start
                if 'caption_origin_start_us' in value:
                    value['caption_origin_start_us'] -= start
                kept.append(value)
            elif a < end and b > start:
                raise ValueError("片段跨越成品边界，拒绝导出")
        track["segments"] = kept
    return child


def load_report(output_dir):
    path = Path(output_dir) / REPORT_NAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
