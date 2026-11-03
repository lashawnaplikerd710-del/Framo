"""Direct MP4 delivery without a Jianying host.

This module turns the deterministic timeline/source map produced by the agent
into an immutable external-render snapshot.  It deliberately reuses the
validated FFmpeg command builder from :mod:`independent_export`; no draft is
created, registered, or modified.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid

from .independent_export import (
    UnsupportedExternalRender,
    build_ffmpeg_command,
    discover_tools,
    probe_media,
    _materialize_subtitle_images,
    write_voice_credits,
)
from .task_store import atomic_json, digest
from .subtitle_styles import resolve_style
from .auto_decorations import collect_visual_context, select_caption_font, plan_stickers
from .visual_presets import visual_plan, choose, caption_motion_selection
from .caption_layout import schedule_from_task, expand_cues
from .caption_motion import normalize_motion, motion_from_instruction


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _material(asset, material_id):
    return {
        "id": material_id,
        "type": "video",
        "path": asset["path"],
        "sha256": asset.get("sha256"),
        "width": int(asset.get("width") or 0),
        "height": int(asset.get("height") or 0),
        "has_audio": bool(asset.get("has_audio")),
    }


def _builtin_ad_sticker(task, canvas, work_dir):
    """Create a small auditable badge for formal direct-render ads.

    This is a deterministic built-in visual primitive, rather than a claimed
    Jianying sticker resource.  It gives the direct renderer a real sticker
    layer that can be checked and overlaid by FFmpeg without touching sources.
    """
    if not work_dir:
        return None
    policy = task.get("formal_ad_policy") or {}
    if not policy.get("sticker_required") and "sticker" not in (task.get("required_native_categories") or []):
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise ValueError("直接广告贴纸需要 Pillow") from exc
    width, height = int(canvas.get("width", 0)), int(canvas.get("height", 0))
    if width <= 0 or height <= 0:
        raise ValueError("贴纸缺少明确画幅")
    font_path = Path(__file__).resolve().parents[1] / "desktop-assets" / "fonts" / "LXGW975YuanSC-700W.ttf"
    if not font_path.is_file():
        raise ValueError("直接广告贴纸缺少已校验的内置字体")
    sticker_dir = Path(work_dir) / "direct-resources" / "stickers"
    sticker_dir.mkdir(parents=True, exist_ok=True)
    path = sticker_dir / "builtin-ad-badge.png"
    badge_w, badge_h = max(180, width // 4), max(72, height // 18)
    image = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    country = str(task.get("target_country") or "").strip()
    fill = (255, 104, 104, 235) if country == "日本" else (104, 181, 255, 235) if country == "美国" else (245, 232, 208, 235)
    draw.rounded_rectangle((0, 0, badge_w - 1, badge_h - 1), radius=max(12, badge_h // 4), fill=fill)
    font = ImageFont.truetype(str(font_path), max(24, badge_h // 2))
    label = str(task.get('direct_badge_label') or 'NEW')[:12]
    while draw.textlength(label,font=font) > badge_w-20 and font.size > 16:
        font = ImageFont.truetype(str(font_path),font.size-1)
    box = draw.textbbox((0, 0), label, font=font)
    draw.text(((badge_w - (box[2] - box[0])) / 2, (badge_h - (box[3] - box[1])) / 2 - box[1]),
               label, font=font, fill=(8, 12, 14, 255))
    image.save(path, "PNG")
    return {"resource_id": "builtin_sticker_ad_badge_v1", "kind": "sticker",
            "path": str(path.resolve()), "source_path": str(path.resolve()),
            "sha256": digest(path), "bytes": path.stat().st_size,
            "provenance": {"source": "agent_builtin", "verified": True,
                           "profile": "formal_ad_badge_v1"}}


def snapshot_from_timeline(task, assets, timeline, media_plan=None, work_dir=None):
    """Create a renderer snapshot from evidence-bound source-map rows.

    All target and source intervals remain integer microseconds.  The source
    map is the only authority for placement; no magnetic-track behaviour or
    inferred gaps are used.
    """
    if not isinstance(timeline, dict) or not isinstance(timeline.get("source_map"), list):
        raise ValueError("timeline 缺少可渲染的 source_map")
    if not assets:
        raise ValueError("direct render requires at least one asset")
    # The job folder supplies a stable, different seed for each new video.
    # Copy the task so snapshot construction never mutates the saved spec.
    if work_dir and not task.get('subtitle_style_seed'):
        task = {**task, 'subtitle_style_seed': Path(work_dir).name}
    fps = (task.get("target") or {}).get("fps")
    ratio = (task.get("target") or {}).get("aspect_ratio", "source")
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ValueError("任务帧率无效")
    first = assets[0]
    from .video_geometry import canvas_for
    canvas = canvas_for(task.get('target') or {}, first)
    by_id = {item["asset_id"]: item for item in assets}
    materials = {"videos": []}
    segments = []
    policy = task.get("formal_ad_policy") or {}
    visual_features = []
    layout_tracks = {row.get("source_segment_id"): row
                     for row in (timeline.get("layout_tracks") or [])
                     if isinstance(row, dict) and row.get("source_segment_id")}
    # Actual task delivery uses its persistent job directory here.  Its name
    # distinguishes batch children and survives retry/resume; never seed from
    # a temporary render/output filename.
    selection_seed = Path(work_dir).name if work_dir else task.get('title', '')
    treatments = visual_plan(task, selection_seed)
    for index, row in enumerate(timeline["source_map"], 1):
        aid = row.get("asset_id")
        asset = by_id.get(aid)
        start = row.get("timeline_start_us")
        end = row.get("timeline_end_us")
        source_start = row.get("source_start_us")
        source_end = row.get("source_end_us")
        if asset is None:
            raise ValueError(f"时间线引用了未登记素材: {aid}")
        if any(type(v) is not int for v in (start, end, source_start, source_end)):
            raise ValueError("source_map 含有非整数时间")
        if not 0 <= start < end or not 0 <= source_start < source_end:
            raise ValueError("source_map 时间范围无效")
        duration = end - start
        if source_end - source_start != duration:
            raise ValueError("源区间与成品区间长度不一致")
        material_id = f"direct_video_{index:04d}"
        materials["videos"].append(_material(asset, material_id))
        segment = {
            "id": f"direct_segment_{index:04d}",
            "material_id": material_id,
            "target_timerange": {"start": start, "duration": duration},
            "source_timerange": {"start": source_start, "duration": duration},
            "volume": 1,
        }
        layout = layout_tracks.get(row.get("source_segment_id"))
        if isinstance(layout, dict) and ratio != 'source':
            # Keep the evidence-linked layout in the immutable snapshot. The
            # exporter validates the track and falls back safely when it has
            # no verified subject box.
            segment["layout"] = layout
        # Bounded portable treatments preserve source trims and audio timing.
        policy = task.get("formal_ad_policy") or {}
        transition = choose(treatments, 'transition', index - 1)
        if transition and index < len(timeline["source_map"]):
            next_duration = timeline["source_map"][index].get("timeline_end_us", 0) - timeline["source_map"][index].get("timeline_start_us", 0)
            transition_us = min(300_000, duration // 4, int(next_duration) // 4)
            if transition_us > 0:
                segment["transition"] = {"type": transition, "duration_us": transition_us}
                if 'transition' not in visual_features:
                    visual_features.append('transition')
        if policy.get("zoom_required"):
            segment["clip"] = {"zoom": 1.04 if index % 2 else 1.06}
        if policy.get("filters_required") or policy.get("filter_required"):
            segment["filter"] = {"type": "ad_color", "contrast": 1.03,
                                  "saturation": 1.08, "brightness": 0.01}
            if "filter" not in visual_features:
                visual_features.append("filter")
        from .original_montage import active as original_montage
        effect_index = index - 1 if original_montage(task) or treatments['categories']['effect']['selection'] == 'user' else 0
        effect = choose(treatments, 'effect', effect_index)
        if effect:
            segment["effect"] = {"type": effect, "strength": 1.0 if effect == 'mono' else .17 if original_montage(task) else 0.35}
            if "effect" not in visual_features:
                visual_features.append("effect")
        segments.append(segment)
    duration = timeline.get("duration_us")
    if type(duration) is not int or duration <= 0:
        raise ValueError("timeline duration_us 无效")
    if sorted((s["target_timerange"]["start"] for s in segments)) != [
        s["target_timerange"]["start"] for s in sorted(segments, key=lambda x: x["target_timerange"]["start"])
    ]:
        raise ValueError("时间线片段排序失败")
    ordered = sorted(segments, key=lambda x: x["target_timerange"]["start"])
    cursor = 0
    for segment in ordered:
        timerange = segment["target_timerange"]
        if timerange["start"] != cursor:
            raise ValueError("成品内部存在未定义空白区段，拒绝猜测导出")
        cursor += timerange["duration"]
    if cursor != duration:
        raise ValueError("视频轨道未覆盖成品完整时长")

    tracks = [{"id": "direct_video_track", "type": "video", "segments": ordered}]
    media_plan = media_plan or {}
    if timeline.get('narration_timing_policy') and any(
            item.get('kind') == 'narration' for item in media_plan.get('pending', [])):
        raise ValueError('口播尚未完整排入成品，保留缓存等待修订，拒绝静默缺失声音')
    captions = media_plan.get("captions", timeline.get("captions")) or []
    caption_schedule = schedule_from_task(task, duration)
    motion_request = motion_from_instruction(task.get('instruction', ''))
    motion_selection = caption_motion_selection(task, selection_seed)
    decorated_captions = []
    for index, cue in enumerate(captions):
        motion = dict(motion_selection['motion'])
        motion.update(cue.get('caption_motion') or {})
        motion.update(motion_request)
        decorated_captions.append({**cue, 'caption_motion':normalize_motion(motion)})
    captions = expand_cues(decorated_captions, caption_schedule)
    visual_context = collect_visual_context(timeline, work_dir)
    if captions:
        text_materials = []
        text_segments = []
        for index, cue in enumerate(captions, 1):
            start, end, text = cue.get("start_us"), cue.get("end_us"), cue.get("text")
            if type(start) is not int or type(end) is not int or not start < end <= duration:
                raise ValueError("字幕时间范围无效")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("字幕内容为空")
            mid = f"direct_text_{index:04d}"
            subtitle_style, style_evidence = resolve_style(task, cue)
            nearby_context = [r for r in visual_context if r.get('timeline_start_us',0) < end
                              and r.get('timeline_end_us',duration) > start]
            font_selection = select_caption_font(task, cue, nearby_context)
            text_materials.append({"id": mid, "type": "text", "content": text.strip(),
                                   "font_selection": font_selection,
                                   "caption_motion":cue['caption_motion'],
                                   "subtitle_style": subtitle_style})
            text_segments.append({"id": f"direct_text_segment_{index:04d}", "material_id": mid,
                                  "text": text.strip(),
                                  "subtitle_style": subtitle_style,
                                  "subtitle_style_evidence": style_evidence,
                                  "font_selection": font_selection,
                                  "caption_motion":cue['caption_motion'],
                                  "caption_origin_start_us":cue['caption_origin_start_us'],
                                  "target_timerange": {"start": start, "duration": end - start}})
        materials["texts"] = text_materials
        tracks.append({"id": "direct_text_track", "type": "text", "segments": text_segments})

    # Source audio is carried by the video inputs.  Direct resource stages add
    # immutable narration/music/SFX materials and absolute-time audio tracks.
    # The renderer never resolves a path by guessing; every resource must be
    # present in the media plan and is copied into the job cache first.
    media_plan = media_plan or {}
    for item in (media_plan.get("materials") or {}).get("audio", []):
        path = Path(item.get("path", "")).resolve()
        if not path.is_file():
            raise ValueError("直接音频资源不存在: " + str(path))
        materials.setdefault("audio", []).append(dict(item))
    for track in media_plan.get("tracks") or []:
        if track.get("type") not in ("audio", "music", "sound"):
            raise ValueError("直接资源轨道类型不受支持: " + str(track.get("type")))
        for segment in track.get("segments") or []:
            timerange = segment.get("target_timerange") or {}
            start, length = timerange.get("start"), timerange.get("duration")
            if type(start) is not int or type(length) is not int or length <= 0 or start < 0 or start + length > duration:
                raise ValueError("直接音频轨道绝对时间范围无效")
            source = segment.get("source_timerange") or {}
            if type(source.get("start", 0)) is not int or type(source.get("duration", length)) is not int:
                raise ValueError("直接音频轨道源时间范围无效")
        tracks.append(dict(track))
    headings = (timeline.get('selling_copy') or {}).get('headings') or []
    sticker_task = {**task, 'direct_badge_label': headings[0]} if timeline.get('narration_cues') and headings else task
    selected_stickers, sticker_report = plan_stickers(task, timeline, canvas, work_dir, visual_context)
    if not treatments['categories']['sticker']['enabled']:
        selected_stickers = []
        sticker_report = {'status': 'disabled_by_user', 'selections': []}
    sticker = None if sticker_report['status'] != 'library_unavailable' else _builtin_ad_sticker(sticker_task, canvas, work_dir)
    if sticker:
        from PIL import Image
        with Image.open(sticker['path']) as image:
            badge_width, badge_height = image.size
        sticker_id = sticker["resource_id"]
        materials.setdefault("stickers", []).append({"id": sticker_id, "type": "sticker",
                                                       "path": sticker["path"], "sha256": sticker["sha256"]})
        tracks.append({"id": "direct_sticker_track", "type": "sticker", "segments": [{
            "id": "direct_sticker_segment", "material_id": sticker_id,
            "target_timerange": {"start": 0, "duration": duration},
            "position": {"x": str(canvas['width']-badge_width-40), "y": "40"},
            "animation": choose(treatments, 'sticker', 0) or 'none'}]})
        visual_features.append("sticker")
    builtin_resources = list(media_plan.get("resources") or [])
    if sticker:
        builtin_resources.append(sticker)
    if selected_stickers:
        materials.setdefault('stickers',[])
        sticker_track={'id':'auto_stickers','type':'sticker','segments':[]}
        for index, selected in enumerate(selected_stickers):
            selected['animation'] = choose(treatments, 'sticker', index) or 'none'
            mid=f'auto_sticker_{index}'
            materials['stickers'].append({'id':mid,'type':'sticker','path':selected['path'],'sha256':selected['sha256']})
            sticker_track['segments'].append({'id':mid+'_segment','material_id':mid,
                'target_timerange':{'start':selected['start_us'],'duration':selected['end_us']-selected['start_us']},
                'position':{'x':selected['x'],'y':selected['y']}, 'animation': selected['animation']})
            builtin_resources.append({'resource_id':selected['id'],'kind':'sticker','path':selected['path'],
                'sha256':selected['sha256'],'provenance':{'source':'builtin_sticker_library','theme':selected['theme'],
                'license':selected['license'],'reason':selected['reason'],'frame_sha256':selected['frame_sha256'],
                'animation':selected['animation']}})
        tracks.append(sticker_track)
        if 'sticker' not in visual_features:visual_features.append('sticker')
    for feature in visual_features:
        if feature not in {item.get("kind") for item in builtin_resources}:
            builtin_resources.append({"resource_id": f"builtin_{feature}_ad_v1", "kind": feature,
                                      "provenance": {"source": "agent_builtin", "verified": True,
                                          "presets": treatments['categories'].get(feature, {}).get('presets', [])}})
    snapshot = {
        "schema_version": "1.0",
        "id": "direct_" + uuid.uuid4().hex,
        "name": task.get("title") or "Agent 直接成片",
        "duration": duration,
        "fps": fps,
        "canvas_config": {**canvas, "fps": fps},
        "materials": materials,
        "tracks": tracks,
        "batch_outputs": [],
        "render_backend": "direct_ffmpeg",
        "visual_treatment_plan": treatments,
        "layout_tracks": timeline.get('layout_tracks') or [],
        "supplemental_analysis": timeline.get('supplemental_analysis') or {},
        "narration_timing_policy": timeline.get('narration_timing_policy'),
        "narration_cues": media_plan.get('narration_cues', timeline.get('narration_cues', [])),
        "source_policy": "online_or_builtin",
        "source_asset_manifest": [{"asset_id": a["asset_id"], "sha256": a.get("sha256"),
                                    "path": a["path"]} for a in assets],
        "source_audio_muted": bool(task.get("mute_source_audio",
                                             task.get("narration_mode") == "new_native")),
        "audio_strategy": (("direct_resources_only"
                            if task.get("mute_source_audio",
                                        task.get("narration_mode") == "new_native")
                            else "embedded_source_audio_plus_direct_resources")
                           if media_plan.get("tracks") else
                           ("muted_source_audio" if task.get("mute_source_audio",
                                                              task.get("narration_mode") == "new_native")
                            else timeline.get("audio_strategy", "embedded_source_audio"))),
        "subtitle_policy": timeline.get("subtitle_policy", "none"),
        "caption_mode": task.get('caption_mode','legacy'),
        "creation_mode": task.get('creation_mode'),
        "caption_layout_schedule":caption_schedule,
        "caption_design_version":"caption_motion_layout_v1",
        "caption_motion_selection":motion_selection,
        "subtitle_style": resolve_style(task)[0],
        "subtitle_style_evidence": resolve_style(task)[1],
        "subtitle_placement": "upper" if timeline.get('narration_cues') else "default",
        "direct_resource_manifest": builtin_resources,
        "direct_visual_features": sorted(set(visual_features)),
        "direct_resource_pending": media_plan.get("pending", []),
        "direct_resource_report": media_plan.get("report_path"),
        "voice_attributions":media_plan.get('voice_attributions',[]),
        "sticker_selection_report": sticker_report,
    }
    from .original_montage import decorate
    if timeline.get('locked_opening'):
        snapshot['locked_opening'] = dict(timeline['locked_opening'])
    return decorate(task,snapshot)


def render_snapshot(snapshot, output_path, *, install_dir=None, work_dir=None,
                    cancelled=None, font_path=None, progress_callback=None):
    """Render one immutable snapshot and return a truthful result report."""
    if snapshot.get('original_montage_policy') and not snapshot.get('_original_montage_chunk'):
        from .original_montage_render import render
        return render(snapshot,output_path,install_dir=install_dir,work_dir=work_dir,
                       cancelled=cancelled,font_path=font_path,progress_callback=progress_callback)
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    work_dir = Path(work_dir or output_path.parent / ".direct-render").resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    tools = discover_tools(install_dir)
    if not tools.get("ffmpeg"):
        raise RuntimeError("未找到 FFmpeg，无法直接生成 MP4")
    if cancelled and cancelled():
        raise RuntimeError("任务已取消")
    temp = work_dir / ("." + output_path.stem + "." + uuid.uuid4().hex + ".tmp.mp4")
    subtitle_temp = None
    started = _utc()
    report = {"schema_version": "1.0", "backend": "direct_ffmpeg",
              "status": "EXPORTING", "output_file": str(output_path),
              "started_at": started, "ffmpeg": tools["ffmpeg"],
              "ffprobe": tools.get("ffprobe"), "input_sha256": [
                  {"path": row["path"], "sha256": row.get("sha256")}
                  for row in snapshot.get("source_asset_manifest", [])],
              "command": None, "media": None, "error": None}
    try:
        if snapshot.get("materials", {}).get("texts") or snapshot.get('subtitle_path'):
            subtitle_temp = work_dir / ("subtitles-" + uuid.uuid4().hex)
            subtitle_temp.mkdir(parents=True, exist_ok=True)
            subtitle_images = _materialize_subtitle_images(snapshot, subtitle_temp,
                                                            font_path=font_path,ffmpeg_path=tools['ffmpeg'])
        else:
            subtitle_images = []
        report["subtitle_styles"] = [{"text": c["text"], "style": c.get("style")}
                                     for c in subtitle_images]
        report['subtitle_layout'] = [{k:c[k] for k in ('text','lines','font_size','bounds','position','start_us','end_us')} for c in subtitle_images]
        report['subtitle_fonts'] = [c.get('font_selection') for c in subtitle_images]
        report['caption_motion']=[{k:c.get(k) for k in ('text','start_us','end_us','kind','motion','resource_duration_us')} for c in subtitle_images]
        report['caption_layout_schedule']=snapshot.get('caption_layout_schedule',[])
        report['caption_motion_selection']=snapshot.get('caption_motion_selection',{})
        report['sticker_selection'] = snapshot.get('sticker_selection_report')
        report['visual_treatment_plan'] = snapshot.get('visual_treatment_plan')
        command = build_ffmpeg_command(snapshot, temp, tools["ffmpeg"], subtitle_images=subtitle_images)
        if snapshot.get('_original_montage_chunk'):
            index=command.index('-filter_complex')
            graph=work_dir/'filters.ffscript';graph.write_text(command[index+1],encoding='utf-8')
            command[index:index+2]=['-/filter_complex',str(graph)]
        report["command"] = " ".join(command)
        from .independent_export import run_ffmpeg_progress
        execution=run_ffmpeg_progress(command,progress_path=work_dir/'ffmpeg-progress.log',
            duration_s=snapshot['duration']/1e6,timeout_s=max(120,int(snapshot['duration']/1e6*12)),
            cancelled=cancelled,progress_callback=progress_callback,phase='视频编码')
        stderr=execution['stderr']
        if execution['returncode'] != 0 or not temp.is_file() or temp.stat().st_size <= 0:
            raise RuntimeError((stderr or "FFmpeg 未生成有效 MP4")[-4000:])
        media = probe_media(temp, ffmpeg=tools["ffmpeg"], ffprobe=tools.get("ffprobe"))
        expected = snapshot["duration"] / 1_000_000
        if abs(media.get("duration_s", 0) - expected) > 0.15:
            raise RuntimeError(f"输出时长检查失败: expected={expected:.3f}, actual={media.get('duration_s')}")
        if media.get("width") != snapshot["canvas_config"]["width"] or media.get("height") != snapshot["canvas_config"]["height"]:
            raise RuntimeError("输出画幅检查失败")
        if not media.get("has_video"):
            raise RuntimeError("输出缺少视频流")
        if progress_callback:
            progress_callback({'phase':'分段编码结束，正在完整解码检查','processed_s':expected,'duration_s':expected,'percent':None})
        check = subprocess.run([tools["ffmpeg"], "-v", "error", "-xerror", "-i", str(temp), "-f", "null", "-"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=max(120,int(expected*3)),
                               stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if check.returncode != 0:
            raise RuntimeError((check.stderr or "完整解码检查失败")[-4000:])
        report['voice_attribution_file']=write_voice_credits(snapshot,output_path)
        try:
            temp.replace(output_path)
        except OSError as exc:
            # Workbench checkpoints commonly live on C: while the user's
            # selected export folder is on D:.  ``os.replace`` is atomic only
            # within one volume; bridge the volumes through a destination-side
            # temporary file and keep the same no-partial-output contract.
            if getattr(exc, 'winerror', None) != 17 and getattr(exc, 'errno', None) != 18:
                raise
            bridge = output_path.with_name('.' + output_path.name + '.' + uuid.uuid4().hex + '.tmp')
            try:
                shutil.copyfile(temp, bridge)
                bridge.replace(output_path)
            finally:
                bridge.unlink(missing_ok=True)
            temp.unlink(missing_ok=True)
        report.update({"status": "COMPLETED", "finished_at": _utc(),
                       "file_size": output_path.stat().st_size, "media": media,
                       "playback_ok": True})
        return report
    except Exception as exc:
        report.update({"status": "FAILED", "finished_at": _utc(),
                       "error": f"{type(exc).__name__}: {exc}"})
        if temp.is_file():
            temp.unlink()
        raise
    finally:
        if subtitle_temp and subtitle_temp.is_dir():
            shutil.rmtree(subtitle_temp, ignore_errors=True)


def write_snapshot(path, snapshot):
    path = Path(path)
    atomic_json(path, snapshot)
    return {"path": str(path), "sha256": digest(path)}

