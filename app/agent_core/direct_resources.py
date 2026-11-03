"""Read-only media resources for the direct FFmpeg backend.

The direct path must not need a Jianying process to add a voice, music or a
sound effect.  This module resolves explicitly configured local/bundled files,
copies them into a job-local immutable cache, and records hashes and provenance
before a renderer sees them.  Optional Windows SAPI narration is generated in
the job cache; it never writes to a source or a Jianying directory.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import uuid
import wave

from .task_store import atomic_json
from . import market_rules, sound_design


AUDIO_EXTENSIONS = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".wav", ".wma"}


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _audio_duration_us(source, config):
    from .independent_export import discover_tools
    ffprobe = discover_tools(config.get('direct_ffmpeg_dir')).get('ffprobe')
    if not ffprobe:
        raise ValueError('缺少 ffprobe，无法读取自定义音效时长')
    result = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'default=nw=1:nk=1', str(source)], capture_output=True, text=True, timeout=15,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise ValueError('无法读取自定义音效')
    duration = round(float(result.stdout.strip())*1e6)
    if duration <= 0:
        raise ValueError('自定义音效为空')
    return duration


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (str, Path)):
        return [str(value)]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, Path))]
    return []


def _find_audio(value, *, fallback_dirs=()):
    candidates = []
    for raw in _as_list(value):
        path = Path(raw).expanduser()
        if path.is_file():
            candidates.append(path)
        elif path.is_dir():
            candidates.extend(sorted(item for item in path.iterdir()
                                     if item.is_file() and item.suffix.casefold() in AUDIO_EXTENSIONS))
    if candidates:
        return candidates[0].resolve()
    for raw in fallback_dirs:
        path = Path(raw).expanduser()
        if path.is_dir():
            items = sorted(item for item in path.rglob("*")
                           if item.is_file() and item.suffix.casefold() in AUDIO_EXTENSIONS)
            if items:
                return items[0].resolve()
    return None


def _cache_audio(source: Path, kind: str, folder: Path, *, provenance=None):
    source = source.resolve(strict=True)
    if source.suffix.casefold() not in AUDIO_EXTENSIONS:
        raise ValueError(f"不支持的音频资源格式: {source.suffix}")
    source_sha = _sha256(source)
    target_dir = folder / "direct-resources" / kind
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{source_sha[:16]}{source.suffix.lower()}"
    if not target.exists():
        shutil.copy2(source, target)
    if _sha256(target) != source_sha:
        raise ValueError("资源缓存指纹校验失败")
    if provenance is None:
        sidecars = [source.with_suffix(source.suffix + ".provenance.json"),
                    source.parent / (source.stem + ".provenance.json")]
        for sidecar in sidecars:
            if sidecar.is_file():
                try:
                    provenance = json.loads(sidecar.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    provenance = None
                break
    result = {
        "resource_id": f"{kind}_{source_sha[:16]}",
        "kind": kind,
        "source_path": str(source),
        "path": str(target),
        "sha256": source_sha,
        "bytes": target.stat().st_size,
        "cached_at": _utc(),
        "provenance": provenance or {"source": "explicit_local_or_builtin", "verified": False},
    }
    atomic_json(target.with_suffix(target.suffix + ".provenance.json"), result)
    return result


def _powershell_executable():
    return shutil.which("powershell") or shutil.which("pwsh")


def _sapi_script():
    # The script is static; all user/model text is passed through a JSON file,
    # so no text is interpolated into a shell command. The request path travels
    # through a task-specific environment variable because PowerShell's
    # -EncodedCommand mode does not accept positional arguments.
    return r'''$RequestPath = $env:DIRECT_SAPI_REQUEST
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Speech
$request = Get-Content -Raw -LiteralPath $RequestPath | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  if ($request.voice) { $synth.SelectVoice([string]$request.voice) }
  $synth.SetOutputToWaveFile([string]$request.output)
  $synth.Speak([string]$request.text)
} finally {
  $synth.Dispose()
}
'''


def synthesize_windows_sapi(text: str, output: Path, *, work_dir: Path, voice=None):
    """Generate a WAV with the OS speech engine, or raise a truthful error."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("口播文案为空")
    executable = _powershell_executable()
    if not executable:
        raise RuntimeError("未找到 Windows SAPI PowerShell，无法生成直接口播")
    work_dir.mkdir(parents=True, exist_ok=True)
    request = work_dir / ("sapi-request-" + uuid.uuid4().hex + ".json")
    output = output.resolve()
    # Windows PowerShell 5.1 treats a UTF-8 file without BOM as the legacy
    # code page.  Use UTF-8 with BOM so Chinese narration survives parsing.
    request.write_text(json.dumps({"text": text.strip(), "output": str(output),
                                   "voice": voice or ""}, ensure_ascii=False),
                       encoding="utf-8-sig")
    encoded = base64.b64encode(_sapi_script().encode("utf-16le")).decode("ascii")
    try:
        env = dict(__import__("os").environ)
        env["DIRECT_SAPI_REQUEST"] = str(request)
        process = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", encoded], env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
            stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    finally:
        request.unlink(missing_ok=True)
    if process.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        detail = (process.stderr or process.stdout or "SAPI 未生成音频")[-2000:]
        raise RuntimeError("Windows SAPI 口播失败: " + detail)
    return output


def _cue_text(plan, timeline, *, required=False):
    # Direct product ads keep the new voice script in its own track.  Captions
    # may contain visual labels for compatibility, but they are never a safe
    # fallback for TTS (that was the source of the previous bad output).
    captions = (timeline.get("narration_cues") if timeline.get("narration_cues") is not None
                else timeline.get("captions") or [])
    formal = timeline.get("formal_ad_policy") or {}
    if (timeline.get("narration_cues") is None and
            (required or plan.get("narration_mode") == "new_native" or formal.get("new_narration_required"))):
        raise ValueError("正式广告缺少独立新口播脚本，拒绝把字幕标签当作配音")
    rows = []
    for cue in captions:
        text = str(cue.get("text") or "").strip()
        start, end = cue.get("start_us"), cue.get("end_us")
        if text and type(start) is int and type(end) is int and 0 <= start < end <= timeline['duration_us']:
            if required:
                from .commerce import GENERIC_AD_LABELS, UNSUPPORTED_CLAIMS
                if not timeline.get('custom_user_prompt') and (any(label in text for label in GENERIC_AD_LABELS) or UNSUPPORTED_CLAIMS.search(text)):
                    raise ValueError('口播脚本含剪辑标签或未经支持的承诺')
                if rows and start < rows[-1]['end_us']:
                    raise ValueError('口播时间重叠')
            rows.append({"start_us": start, "end_us": end, "text": text})
        elif required:
            raise ValueError('口播脚本存在空白或无效时间段')
    if rows:
        return rows
    if required or plan.get("narration_mode") == "new_native" or formal.get("new_narration_required"):
        raise ValueError("正式广告口播脚本为空")
    title = str(plan.get("title") or "商品展示").strip()
    return [{"start_us": 0, "end_us": int(timeline["duration_us"]), "text": title}]


def _localized_voice_inputs(task, rows, settings):
    """Require script-bound reviewed audio; never use the Chinese SAPI fallback.

    Generation in the reference conversation used external services. Their
    access/session is not an embedded capability of this desktop application.
    Explicitly supplied audio can be reused only for the exact current script.
    """
    locale = task['narration_locale']
    entries = (settings.get('localized_voiceovers') or {}).get(locale)
    if not isinstance(entries, list) or len(entries) != len(rows):
        raise ValueError(f'缺少 {locale} 自然配音：需为当前每句脚本提供已核对的音频；外部配音服务尚未接入，禁止使用默认系统语音代替')
    for index, (row, entry) in enumerate(zip(rows, entries), 1):
        if (not isinstance(entry, dict) or entry.get('text') != row['text']
                or entry.get('locale') != locale):
            raise ValueError(f'{locale} 配音第 {index} 句与本次脚本或语言不一致')
        review = entry.get('review') or {}
        if (not isinstance(review, dict) or not isinstance(review.get('reviewer'), str)
                or not review['reviewer'].strip()
                or any(review.get(key) is not True for key in
                       ('naturalness_passed', 'pronunciation_passed', 'sentence_complete'))):
            raise ValueError(f'{locale} 配音第 {index} 句缺少自然度、发音和完整性核对记录')
        source = Path(entry.get('path') or '')
        if not source.is_file() or entry.get('sha256') != _sha256(source):
            raise ValueError(f'{locale} 配音第 {index} 句文件缺失或指纹不一致')
    return entries


def _prepare_localized_voice(entry, output, config):
    source = Path(entry['path'])
    if _sha256(source) != entry['sha256']:
        raise ValueError('配音资源读取前指纹发生变化')
    if source.suffix.lower() == '.wav':
        with wave.open(str(source), 'rb') as wav:
            if wav.getnframes() <= 0:
                raise ValueError('配音文件为空')
        shutil.copyfile(source, output)
    else:
        from .independent_export import discover_tools
        ffmpeg = discover_tools(config.get('direct_ffmpeg_dir')).get('ffmpeg')
        if not ffmpeg:
            raise ValueError('缺少 FFmpeg，无法读取配音')
        result = subprocess.run([ffmpeg, '-y', '-v', 'error', '-i', str(source),
            '-vn', '-ar', '48000', '-ac', '1', '-c:a', 'pcm_s16le', str(output)],
            capture_output=True, timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise ValueError('配音转码失败')
    if _sha256(source) != entry['sha256']:
        raise ValueError('配音资源读取期间指纹发生变化')


def build_direct_media_plan(task, timeline, plan, folder: Path, config: dict, *,
                            report_dir: Path | None = None):
    """Resolve optional direct audio and return tracks plus an auditable report.

    Missing optional resources are reported and source audio remains intact. A
    caller may set ``require_direct_audio_resources`` to make missing required
    resources a resumable BLOCKED_RESOURCES checkpoint instead.
    """
    folder = Path(folder).resolve()
    configured = config.get("direct_media") or {}
    requested = task.get("direct_media") or {}
    if not isinstance(configured, dict):
        configured = {}
    if not isinstance(requested, dict):
        requested = {}
    merged = {**configured, **requested}
    auto_packaging = task.get("workflow") in ("product_ad", "product_montage", "custom", "crossborder_montage")
    fallback_root = merged.get("root") or config.get("direct_resources_root")
    fallback_dirs = []
    if fallback_root:
        root = Path(fallback_root).expanduser()
        fallback_dirs.extend([root / "music", root / "sfx", root / "voice"])
    materials = {"audio": []}
    tracks = []
    resources = []
    pending = []

    def add_track(kind, source, start, duration, *, volume=1.0, fade_in_us=0,
                  fade_out_us=0, loop=False, source_duration=None, label=None,
                  provenance=None):
        if not source:
            return None
        cached = _cache_audio(Path(source), kind, folder, provenance=provenance)
        aid = cached["resource_id"]
        materials["audio"].append({"id": aid, "type": kind, "path": cached["path"],
                                    "sha256": cached["sha256"], "has_audio": True,
                                    "label": label or kind})
        segment = {"id": f"direct_{kind}_{len(tracks)+1:03d}", "material_id": aid,
                   "target_timerange": {"start": int(start), "duration": int(duration)},
                   "source_timerange": {"start": 0, "duration": int(source_duration or duration)},
                   "volume": float(volume), "fade_in_us": int(fade_in_us),
                   "fade_out_us": int(fade_out_us), "loop": bool(loop)}
        tracks.append({"id": f"direct_{kind}_track_{len(tracks)+1:03d}",
                       "type": "audio" if kind == "narration" else kind,
                       "segments": [segment]})
        resources.append(cached)
        return cached

    duration = int(timeline["duration_us"])
    policy = task.get('formal_ad_policy') or {}
    enabled = lambda kind: ('enabled_direct_resources' not in task or kind in task['enabled_direct_resources'])
    fade = min(80_000, duration // 20)
    music = (_find_audio(merged.get("music"), fallback_dirs=[Path(fallback_root) / "music"]
                        if fallback_root and auto_packaging else []) if enabled('music') else None)
    if music:
        add_track("music", music, 0, duration, volume=float(merged.get("music_volume", 0.10 if task.get("ad_narration_policy") == "whole_ad_v1" else 0.16)),
                  fade_in_us=fade, fade_out_us=fade, loop=True, label="background_music",
                  provenance=merged.get("music_provenance"))
    elif enabled('music') and (merged.get("music") or task.get("require_direct_audio_resources") or policy.get('music_required')):
        pending.append({"kind": "music", "reason": "没有可验证的 BGM 资源"})

    sound_report = None
    effects = _as_list(merged.get("sound_effects") or merged.get("sfx")) if enabled('sound') else []
    if enabled('sound') and task.get('sound_design_version') == sound_design.POLICY:
        candidates = None
        if effects:
            candidates = []
            for value in effects:
                source = _find_audio(value)
                if not source:
                    continue
                # Probe supplied files, retaining their exact identity and duration.
                try:
                    audio_us = _audio_duration_us(source, config)
                except (ValueError, OSError, subprocess.SubprocessError):
                    continue
                candidates.append({'id':source.stem, 'path':str(source), 'family':'provided',
                    'duration_us':min(audio_us, 1_000_000), 'sha256':_sha256(source),
                    'recommended_gain':float(merged.get('sfx_volume', .16)),
                    **(merged.get('sfx_provenance') or {})})
        sound_report = sound_design.arrange(task, timeline, plan, seed=str(folder), candidates=candidates)
        for event in sound_report['events']:
            row = event['sound']
            provenance = {key:row[key] for key in ('id','family','license','license_file','source_page','source_url','sha256') if key in row}
            add_track('sound', row['path'], event['start_us'], event['duration_us'],
                volume=event['volume'], fade_in_us=min(5_000,event['duration_us']//10),
                fade_out_us=min(40_000,event['duration_us']//5), label=event['event'], provenance=provenance)
        if not sound_report['events'] and (task.get('require_direct_audio_resources') or policy.get('sound_effects_required')):
            pending.append({'kind':'sound', 'reason':'没有可验证的事件音效资源'})
        effects = []

    if enabled('sound') and sound_report is None and not effects and fallback_root and auto_packaging:
        candidate = _find_audio(None, fallback_dirs=[Path(fallback_root) / "sfx"])
        if candidate:
            effects = [str(candidate)]
    transition_points = [int(row["timeline_end_us"]) for row in timeline.get("source_map", [])[:-1]
                         if type(row.get("timeline_end_us")) is int]
    if effects:
        for index, point in enumerate(transition_points):
            source = _find_audio(effects[index % len(effects)])
            if source:
                add_track("sound", source, max(0, point - 100_000), 1_000_000,
                          volume=float(merged.get("sfx_volume", 0.45)), fade_in_us=20_000,
                          fade_out_us=120_000, label="transition_sfx",
                          provenance=merged.get("sfx_provenance"))
    elif enabled('sound') and sound_report is None and (task.get("require_direct_audio_resources") or policy.get('sound_effects_required')):
        pending.append({"kind": "sound", "reason": "没有可验证的音效资源"})

    narration_mode = task.get("narration_mode")
    narration_path = _find_audio(merged.get("narration") or merged.get("voiceover"))
    if task.get('editing_rules_version') and narration_mode != 'new_native':
        narration_path = None
    narration_rows = _cue_text(plan, timeline, required=True) if narration_mode == 'new_native' else []
    from . import narration_timing
    continuous = timeline.get('narration_timing_policy') == narration_timing.POLICY
    actual_narration = None
    actual_captions = None
    narration_overflow = None
    tts_report = None
    localized = (market_rules.active(task) or bool(merged.get('voice_id'))) and narration_mode == 'new_native'
    localized_entries = None
    local_generated = False
    if localized:
        narration_path = None  # Unbound global audio could be in the wrong language or script.
        try:
            if not market_rules.active(task):
                from .local_voice import catalog
                try:
                    voice = next(v for v in catalog(merged['local_voice_library'])['voices'] if v['id'] == merged['voice_id'])
                    task = {**task, 'narration_locale':voice['locale']}
                except (KeyError, OSError, ValueError, StopIteration) as exc:
                    raise ValueError('所选本地音色不可用') from exc
            if task['narration_locale'] in (merged.get('localized_voiceovers') or {}):
                localized_entries = _localized_voice_inputs(task, narration_rows, merged)
            elif merged.get('local_voice_library') and merged.get('tts', True) is not False:
                from .local_voice import generate
                localized_entries = generate(merged['local_voice_library'],
                    [row['text'] for row in narration_rows], task['narration_locale'],
                    folder / 'direct-resources' / 'tts', voice_id=merged.get('voice_id'))
                local_generated = True
            else:
                localized_entries = _localized_voice_inputs(task, narration_rows, merged)
        except (ValueError, OSError, TypeError, AttributeError) as exc:
            pending.append({'kind': 'narration', 'reason': str(exc)})
            tts_report = {'status': 'PENDING_RESOURCES', 'locale': task.get('narration_locale'),
                          'provider': None, 'system_voice_fallback': False}
    if narration_mode == "new_native" and not narration_path and (
            localized_entries is not None if localized else merged.get("tts", True) is not False):
        voice_dir = folder / "direct-resources" / "narration"
        voice_dir.mkdir(parents=True, exist_ok=True)
        generated = []
        for index, row in enumerate(narration_rows, 1):
            signature = hashlib.sha256((row["text"] + "\0" +
                                        (json.dumps({k:localized_entries[index-1].get(k) for k in
                                                     ('text','locale','sha256','voice_id','review')}, ensure_ascii=False, sort_keys=True)
                                         if localized else str(merged.get("tts_voice") or "system_default"))).encode("utf-8")).hexdigest()
            # A repaired sentence gets its own file. Earlier checkpoints and
            # unchanged sentence caches must remain readable and hash-correct.
            output = voice_dir / f"narration-{index:03d}-{signature[:20]}.wav"
            receipt = output.with_suffix(".json")
            try:
                reusable = False
                if output.is_file() and output.stat().st_size > 0 and receipt.is_file():
                    try:
                        saved = json.loads(receipt.read_text(encoding="utf-8-sig"))
                        reusable = saved.get("signature") == signature
                        if localized:
                            reusable = reusable and saved.get('sha256') == _sha256(output)
                    except (OSError, ValueError):
                        reusable = False
                if not reusable:
                    if localized:
                        _prepare_localized_voice(localized_entries[index-1], output, config)
                    else:
                        synthesize_windows_sapi(row["text"], output,
                                                work_dir=folder / "direct-resources" / "tts",
                                                voice=merged.get("tts_voice"))
                    atomic_json(receipt, {"signature": signature, "text": row["text"],
                                          "sha256": _sha256(output),
                                          "voice": task['narration_locale'] if localized else merged.get("tts_voice") or "system_default"})
            except Exception as exc:
                pending.append({"kind": "narration", "reason": str(exc)})
                break
            # Never truncate a spoken sentence at the visual slot boundary.
            # A bounded tempo correction is allowed and recorded; larger
            # overruns require a shorter script instead of dropping words.
            try:
                with wave.open(str(output), 'rb') as wav:
                    audio_us = round(wav.getnframes()/wav.getframerate()*1e6)
                if continuous:
                    if audio_us <= 0:
                        raise ValueError('配音音频时长无效')
                    generated.append((output, audio_us, 1.0))
                    continue
                slot_us = row['end_us']-row['start_us']
                ratio = audio_us/max(1, slot_us-60_000)
                used = output
                if ratio > 1:
                    if ratio > (1.08 if localized else 1.25):
                        raise ValueError(f'口播第{index}句长于镜头：{audio_us/1e6:.2f}秒 > {slot_us/1e6:.2f}秒，需缩短文案')
                    from .independent_export import discover_tools
                    ffmpeg = discover_tools(config.get('direct_ffmpeg_dir')).get('ffmpeg')
                    if not ffmpeg:
                        raise ValueError('缺少 FFmpeg，无法校准口播时长')
                    used = output.with_name(output.stem+f'-fit-{slot_us}.wav')
                    result = subprocess.run([ffmpeg,'-y','-v','error','-i',str(output),'-af',f'atempo={ratio:.6f}',str(used)],
                        capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    if result.returncode:
                        raise ValueError('口播时长校准失败')
                    with wave.open(str(used),'rb') as wav:
                        audio_us = round(wav.getnframes()/wav.getframerate()*1e6)
                if not 0 < audio_us <= slot_us:
                    raise ValueError('口播时长校准后仍越界')
                generated.append((used, audio_us, max(1.0,ratio)))
            except (ValueError, OSError, wave.Error, subprocess.TimeoutExpired) as exc:
                pending.append({'kind':'narration','reason':str(exc)})
                break
        if len(generated) == len(narration_rows) and continuous:
            try:
                from .caption_policy import highlights
                anchors = [c.get('visual_anchor_us',c['start_us']) for c in timeline['narration_cues']] if highlights(task) else None
                # Narration estimates may already be distributed by speech
                # length; visual source-map anchors preserve the intended scene.
                if anchors is not None and not any('visual_anchor_us' in c for c in timeline['narration_cues']) and len(timeline.get('source_map',[])) == len(anchors):
                    anchors = [r['timeline_start_us'] for r in timeline['source_map']]
                ranges = narration_timing.schedule([g[1] for g in generated], duration, anchors=anchors)
                actual_narration, actual_captions = narration_timing.retime(
                    timeline['narration_cues'], timeline.get('captions', []), ranges)
                narration_rows = [{**row, **span} for row, span in zip(narration_rows, ranges)]
            except ValueError as exc:
                pending.append({'kind': 'narration', 'reason': str(exc)})
                narration_overflow = {'measured_us': sum(g[1] for g in generated),
                    'available_us': max(0, duration-120_000*max(0,len(generated)-1)-100_000),
                    'cue_durations_us': [g[1] for g in generated],
                    'speech_units': sum(narration_timing.units(task, row['text']) for row in narration_rows),
                    'reason': str(exc)}
        if len(generated) == len(narration_rows) and (not continuous or actual_narration is not None):
            for index, (row, (source, audio_us, tempo)) in enumerate(zip(narration_rows, generated), 1):
                cached = add_track("narration", source, row["start_us"],
                                   audio_us, volume=1.0,
                                   fade_in_us=30_000, fade_out_us=50_000,
                                   label=("local_neural_voiceover" if local_generated else "reviewed_localized_voiceover") if localized else "new_sapi_voiceover",
                                   provenance=({'source': 'local_neural_tts' if local_generated else 'provided_reviewed_audio', 'locale': task['narration_locale'],
                                                'review': localized_entries[index-1]['review'],
                                                'voice_id': localized_entries[index-1].get('voice_id'),
                                                'attribution':localized_entries[index-1].get('attribution'),
                                                'attribution_required':localized_entries[index-1].get('attribution_required',False),
                                                'terms_url':localized_entries[index-1].get('terms_url'),
                                                'license': localized_entries[index-1].get('license'),
                                                'model_source': localized_entries[index-1].get('source'),
                                                'source_sha256': localized_entries[index-1]['sha256']}
                                               if localized else {"source": "windows_sapi", "voice": merged.get("tts_voice")}))
                if cached:
                    resources[-1]["script_text"] = row["text"]
                    resources[-1].update(duration_us=audio_us, tempo=tempo, slot_end_us=row['end_us'])
            tts_report = {"status": "PROVIDED" if localized and not local_generated else "COMPLETED", "cue_count": len(generated),
                          "voice": task['narration_locale'] if localized else merged.get("tts_voice") or "system_default"}
            if local_generated:
                tts_report.update(provider='local_neural_tts', voice_id=localized_entries[0]['voice_id'],
                    naturalness_review='试听待确认', human_listened=False,
                    cache_hits=sum(bool(row.get('cache_hit')) for row in localized_entries))
        else:
            tts_report = {"status": "FAILED", "cue_count": len(generated),
                          "expected": len(narration_rows)}
    elif narration_path:
        add_track("narration", narration_path, 0, duration, volume=1.0,
                  fade_in_us=fade, fade_out_us=fade, label="provided_voiceover",
                  provenance=merged.get("narration_provenance"))
        tts_report = {"status": "PROVIDED", "cue_count": 1}
    elif narration_mode == "new_native" and not localized:
        pending.append({"kind": "narration", "reason": "未配置配音且已禁用直接 TTS"})

    report = {"schema_version": "1.0", "backend": "direct_ffmpeg",
              "created_at": _utc(), "resources": resources, "pending": pending,
              "tts": tts_report,
              "source_audio_retained": not bool(task.get("mute_source_audio",
                                                       task.get("narration_mode") == "new_native")),
              "quality_downgraded": bool(pending and task.get("require_direct_audio_resources"))}
    if sound_report is not None:
        report['sound_design'] = sound_report
    if task.get('ad_narration_policy'):
        report['ad_narration_policy'] = task['ad_narration_policy']
    if continuous:
        report.update(narration_timing_policy=narration_timing.POLICY,
                      narration_cues=actual_narration, captions=actual_captions,
                      timing_basis='measured_pcm_duration', cross_shot_allowed=True)
    if narration_overflow:
        report['narration_overflow'] = narration_overflow
    # Keep each retry's resource report immutable.  The cache remains shared
    # within the job, while the auditable plan belongs to its revision.
    report_path = (Path(report_dir).resolve() if report_dir else folder) / "direct-resource-plan.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    result = {"materials": materials, "tracks": tracks, "resources": resources,
              "pending": pending, "report": report, "report_path": str(report_path)}
    credits=[]
    for resource in resources:
        evidence=resource.get('provenance') or {}
        if evidence.get('attribution_required'):
            credit={k:evidence.get(k) for k in ('voice_id','attribution','terms_url')}
            if credit not in credits:credits.append(credit)
    result['voice_attributions']=credits
    report['voice_attributions']=credits
    atomic_json(report_path,report)
    if actual_narration is not None:
        result.update(narration_cues=actual_narration, captions=actual_captions)
    return result

