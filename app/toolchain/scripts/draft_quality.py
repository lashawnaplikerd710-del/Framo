"""Read-only structural audit of a reloaded native Jianying draft snapshot.

This audits every timeline segment, including embedded compound drafts. It does
not render, listen, certify lip sync, or infer narrative quality from JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sys


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def timerange(value):
    if not isinstance(value, dict):
        return None
    start, duration = value.get('start'), value.get('duration')
    if type(start) is not int or type(duration) is not int or start < 0 or duration <= 0:
        return None
    return start, start + duration


def audit(snapshot, *, base_dir=None, primary_track=None, probe=None):
    """Return a structural report. probe(path) may supply common.media_info data.

    A primary track is explicit, or inferred only when a draft has exactly one
    video track. Other video tracks are overlays, not standalone gap failures.
    Nested compound timestamps remain local to their named draft context.
    """
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('tracks'), list):
        raise ValueError('Expected raw native draft object with tracks array')
    base = Path(base_dir or '.').resolve()
    report = {
        'schema': 'jianying-draft-structural-qc-v1',
        'status': 'checking', 'duration_us': snapshot.get('duration'),
        'issues': [], 'segments': [], 'drafts': [], 'media_probes': [],
        'edit_boundaries_us': [],
        'scope': {
            'all_segments_inspected': True,
            'compound_drafts_recursively_inspected': True,
            'source_bounds_method': 'MediaInfo when enabled, otherwise declared material duration',
            'primary_gap_method': 'declared primary video track; sole video track can be inferred',
            'a_v_mapping_method': 'same-path overlapping audio/video source-time mapping hints only',
            'rendered_frames_checked': False, 'audio_listened': False,
            'narrative_verified': False, 'lip_sync_verified': False,
            'black_frames_verified': False, 'full_quality_accepted': False,
        },
        'unknowns': [
            'Decoded picture, black/frozen frames and effect rendering require native rendered evidence.',
            'Narrative completeness, pacing and claim support require continuous editorial review.',
            'Intelligibility, clicks, distortion, loudness and mixing require actual audio review/measurement.',
            'Source-time alignment is not perceptual lip sync; validate events and continuous playback.',
            'Text correctness, legibility, safe area and existing burned text require rendered review.',
            'Effect/sticker resource presence does not prove successful rendering or online entitlement.',
            'Compound timestamps are local; structural issues are not automatically mapped through nonlinear retiming.',
        ],
    }
    cache = {}
    seen_objects = set()

    def issue(code, severity, message, context='root', track=None, segment=None,
              interval=None, **evidence):
        report['issues'].append({
            'id': f'Q{len(report["issues"])+1:04d}', 'code': code, 'severity': severity,
            'message': message, 'draft_context': context, 'track_id': track,
            'segment_id': segment, 'start_us': interval[0] if interval else None,
            'end_us': interval[1] if interval else None, 'evidence': evidence,
        })

    def path_for(raw):
        if not isinstance(raw, str) or not raw.strip():
            return None
        if '://' in raw or raw.startswith(('\\\\', '//')):
            return None
        path = Path(raw)
        return (path if path.is_absolute() else base / path).resolve()

    def metadata(path):
        key = str(path)
        if key not in cache:
            entry = {'path': key, 'method': 'MediaInfo' if probe else 'not_probed'}
            try:
                stat = path.stat()
                entry.update(size_bytes=stat.st_size, modified_ns=stat.st_mtime_ns)
                if probe:
                    entry['info'] = probe(path)
            except Exception as exc:
                entry['error'] = str(exc)
            cache[key] = entry
            report['media_probes'].append(entry)
        return cache[key]

    def visit(draft, context, requested_primary=None, depth=0):
        if depth > 32 or id(draft) in seen_objects:
            issue('compound_depth_or_cycle', 'error', 'Compound graph exceeds supported safe traversal.', context)
            report['scope']['all_segments_inspected'] = False
            return
        seen_objects.add(id(draft))
        tracks = draft.get('tracks')
        if not isinstance(tracks, list):
            issue('tracks_invalid', 'error', 'Draft tracks must be an array.', context)
            report['scope']['all_segments_inspected'] = False
            return
        duration = draft.get('duration')
        if type(duration) is not int or duration <= 0:
            issue('draft_duration_invalid', 'error', 'Draft duration must be positive integer microseconds.', context)
            duration = None
        fps = draft.get('fps', 30)
        tolerance = max(1, round(1_000_000 / fps)) if finite(fps) and fps > 0 else 33333
        materials = draft.get('materials')
        if not isinstance(materials, dict):
            issue('materials_invalid', 'error', 'Draft materials must be an object.', context)
            materials = {}
        indexed = {}
        embedded = []
        for category, rows in materials.items():
            if not isinstance(rows, list):
                issue('material_category_invalid', 'error', 'Material category must be an array.', context, category=category)
                continue
            for material in rows:
                if not isinstance(material, dict) or not isinstance(material.get('id'), str) or not material['id']:
                    issue('material_invalid', 'error', 'Material requires a nonempty ID.', context, category=category)
                    continue
                mid = material['id']
                if mid in indexed:
                    issue('material_id_duplicate', 'error', 'Material ID appears more than once.', context, material_id=mid)
                indexed[mid] = (category, material)
                if category == 'drafts':
                    child = material.get('draft')
                    if isinstance(child, dict):
                        embedded.append((mid, child))
                    else:
                        issue('compound_unavailable', 'warning', 'Referenced compound has no embedded snapshot; child segments cannot be audited.', context, material_id=mid)
                        report['scope']['all_segments_inspected'] = False
        visual = [t for t in tracks if isinstance(t, dict) and t.get('type') == 'video']
        chosen = requested_primary
        selection = 'explicit' if chosen else 'sole_video_track' if len(visual) == 1 else 'unresolved'
        if not chosen and len(visual) == 1:
            chosen = visual[0].get('id')
        if chosen and not any(t.get('id') == chosen for t in visual):
            issue('primary_track_invalid', 'error', 'Declared primary track is not a video track in this draft.', context, track=chosen)
            chosen = None
        if not chosen:
            issue('primary_track_unresolved', 'warning', 'Primary visual continuity cannot be audited without an unambiguous primary video track.', context)
        report['drafts'].append({'context': context, 'duration_us': duration,
                                 'primary_track_id': chosen, 'primary_selection': selection})
        local = []
        ids = set()
        track_ids = set()
        for ti, track in enumerate(tracks):
            if not isinstance(track, dict):
                issue('track_invalid', 'error', 'Track is not an object.', context, index=ti)
                report['scope']['all_segments_inspected'] = False
                continue
            tid, kind = track.get('id'), track.get('type')
            if not isinstance(tid, str) or not tid or tid in track_ids:
                issue('track_id_invalid', 'error', 'Track IDs must be nonempty and unique.', context, track=tid)
            if isinstance(tid, str):
                track_ids.add(tid)
            segments = track.get('segments')
            if not isinstance(segments, list):
                issue('segments_invalid', 'error', 'Track segments must be an array.', context, track=tid)
                report['scope']['all_segments_inspected'] = False
                continue
            for si, seg in enumerate(segments):
                if not isinstance(seg, dict):
                    issue('segment_invalid', 'error', 'Segment is not an object.', context, track=tid, index=si)
                    report['scope']['all_segments_inspected'] = False
                    continue
                sid = seg.get('id')
                if not isinstance(sid, str) or not sid or sid in ids:
                    issue('segment_id_invalid', 'error', 'Segment IDs must be nonempty and unique within a draft.', context, track=tid, segment=sid)
                if isinstance(sid, str):
                    ids.add(sid)
                target = timerange(seg.get('target_timerange'))
                source = timerange(seg.get('source_timerange'))
                args = {'context': context, 'track': tid, 'segment': sid, 'interval': target}
                row = {'draft_context': context, 'track_id': tid, 'segment_id': sid,
                       'track_type': kind, 'material_id': seg.get('material_id'),
                       'start_us': target[0] if target else None, 'end_us': target[1] if target else None,
                       'source_start_us': source[0] if source else None, 'source_end_us': source[1] if source else None,
                       'speed': seg.get('speed', 1), 'volume': seg.get('volume'),
                       'visible': seg.get('visible', True), 'path': None, 'text': None}
                local.append(row)
                report['segments'].append(row)
                if target is None:
                    issue('target_range_invalid', 'error', 'Target range needs nonnegative integer start and positive integer duration.', **args)
                elif duration and target[1] > duration:
                    issue('segment_beyond_draft', 'error', 'Segment ends beyond declared draft duration.', **args, draft_duration_us=duration)
                if context == 'root' and target:
                    report['edit_boundaries_us'].extend(target)
                mid = seg.get('material_id')
                if not isinstance(mid, str) or mid not in indexed:
                    issue('material_missing', 'error', 'Segment material ID cannot be resolved.', **args, material_id=mid)
                    continue
                category, material = indexed[mid]
                refs = seg.get('extra_material_refs', [])
                if not isinstance(refs, list):
                    issue('extra_refs_invalid', 'error', 'Extra material references must be an array.', **args)
                    refs = []
                for ref in refs:
                    if not isinstance(ref, str) or ref not in indexed:
                        issue('extra_material_missing', 'error', 'Extra material reference cannot be resolved.', **args, reference=ref)
                resolved_refs = [indexed[ref] for ref in refs if isinstance(ref, str) and ref in indexed]
                for dependency_category, dependency in [(category, material)] + resolved_refs:
                    if dependency_category in ('videos', 'audios', 'images', 'drafts'):
                        continue
                    for field in ('path', 'resource_path'):
                        raw_dependency = dependency.get(field)
                        if not isinstance(raw_dependency, str) or not raw_dependency:
                            continue
                        dependency_path = path_for(raw_dependency)
                        if dependency_path is None or not dependency_path.exists():
                            issue('resource_path_unavailable', 'warning', 'Declared effect/sticker/other material resource path is unavailable; native appearance cannot be assumed.', **args, material_id=dependency['id'], field=field, path=raw_dependency)
                compound = next((m['draft'] for c, m in resolved_refs if c == 'drafts' and isinstance(m.get('draft'), dict)), None)
                if category == 'drafts' and isinstance(material.get('draft'), dict):
                    compound = material['draft']
                curved = any(c == 'speeds' and m.get('curve_speed') for c, m in resolved_refs)
                row['nonlinear_mapping'] = bool(curved or seg.get('reverse') or seg.get('is_loop'))
                if kind == 'audio' and category != 'audios':
                    issue('audio_material_type', 'error', 'Native audio track segment must reference an audio material.', **args, category=category)
                if kind == 'video' and category not in ('videos', 'images', 'drafts'):
                    issue('video_material_type', 'error', 'Video track references a nonvisual material.', **args, category=category)
                if kind == 'text' and category not in ('texts', 'text_templates'):
                    issue('text_material_type', 'error', 'Text track references a nontext material.', **args, category=category)
                is_still = category == 'images' or material.get('type') in ('photo', 'image')
                media_segment = kind in ('video', 'audio')
                speed = seg.get('speed', 1)
                if media_segment and (not finite(speed) or speed <= 0):
                    issue('speed_invalid', 'error', 'Playback speed must be finite and positive.', **args)
                if media_segment and not is_still and source is None:
                    issue('source_range_invalid', 'error', 'Media source range needs nonnegative start and positive duration.', **args)
                if source and target and media_segment and not is_still and finite(speed) and speed > 0:
                    if row['nonlinear_mapping']:
                        issue('nonlinear_mapping_review', 'info', 'Curve/reverse/loop timing requires rendered review; linear duration check skipped.', **args)
                    elif abs((source[1] - source[0]) / speed - (target[1] - target[0])) > tolerance:
                        issue('retime_duration_mismatch', 'warning', 'Source duration, constant speed and target duration disagree by more than one timeline frame.', **args, tolerance_us=tolerance)
                volume = seg.get('volume')
                if media_segment and volume is not None and (not finite(volume) or volume < 0):
                    issue('volume_invalid', 'error', 'Volume must be finite and nonnegative.', **args)
                if media_segment and volume == 0:
                    issue('segment_muted', 'info', 'Segment has zero gain; confirm that muting is intentional.', **args)
                if kind == 'video' and seg.get('visible') is False:
                    issue('visual_hidden', 'warning', 'Visual segment is marked invisible.', **args)
                clip = seg.get('clip')
                if kind in ('video', 'text', 'sticker') and isinstance(clip, dict) and clip.get('alpha') == 0:
                    issue('visual_zero_alpha', 'warning', 'Visual segment has zero opacity; confirm that invisibility is intentional.', **args)
                declared_duration = compound.get('duration') if compound else material.get('duration')
                bound = declared_duration if type(declared_duration) is int and declared_duration > 0 else None
                bound_method = 'compound_duration' if compound else 'declared_material_duration'
                path = path_for(material.get('path'))
                if media_segment and compound is None:
                    if path is None:
                        issue('media_path_missing', 'error', 'Media segment has no resolvable local source path.', **args)
                    elif not path.is_file():
                        row['path'] = str(path)
                        issue('media_file_missing', 'error', 'Referenced local media file does not exist.', **args, path=str(path))
                    else:
                        row['path'] = str(path)
                        meta = metadata(path)
                        if meta.get('error'):
                            issue('media_probe_failed', 'warning', 'Source probe failed; declared duration is the fallback.', **args, error=meta['error'])
                        info = meta.get('info') or {}
                        if probe and info:
                            if kind == 'audio' and info.get('has_audio') is False:
                                issue('audio_stream_missing', 'error', 'Audio material source contains no detected audio stream.', **args)
                            if kind == 'video' and info.get('has_video') is False:
                                issue('video_stream_missing', 'error', 'Visual source contains no detected image/video stream.', **args)
                            seconds = info.get('audio_duration' if kind == 'audio' else 'video_duration')
                            if finite(seconds) and seconds > 0:
                                bound = round(seconds * 1_000_000)
                                bound_method = 'MediaInfo_stream_duration'
                if media_segment and not is_still and source:
                    row['source_bound_us'] = bound
                    row['source_bound_method'] = bound_method
                    if bound is None:
                        issue('source_bound_unknown', 'warning', 'No usable source duration; out-of-bounds safety is unverified.', **args)
                    elif source[1] > bound + 1000:
                        issue('source_range_exceeded', 'error', 'Source out-point exceeds known source duration (1 ms metadata rounding allowance).', **args, source_end_us=source[1], source_bound_us=bound, bound_method=bound_method)
                if kind == 'text':
                    content = material.get('content')
                    try:
                        parsed = json.loads(content) if isinstance(content, str) else content
                        text = parsed.get('text') if isinstance(parsed, dict) else None
                    except (ValueError, TypeError):
                        parsed, text = None, None
                    if not isinstance(text, str) or not text.strip():
                        issue('text_content_empty_or_unreadable', 'warning', 'Text content has no readable nonempty text; templates need native review.', **args)
                    else:
                        row['text'] = text
                        row['text_characters'] = len(text)
                    font_paths = [material.get('font_path')]
                    if isinstance(parsed, dict) and isinstance(parsed.get('styles'), list):
                        font_paths += [style.get('font', {}).get('path') for style in parsed['styles']
                                       if isinstance(style, dict) and isinstance(style.get('font'), dict)]
                    for raw in set(p for p in font_paths if isinstance(p, str) and p):
                        font = path_for(raw)
                        if font is None or not font.is_file():
                            issue('font_file_missing', 'warning', 'Declared font path is unavailable; fallback appearance requires native review.', **args, path=raw)
        if chosen:
            spans = sorted((r for r in local if r['track_id'] == chosen and r['start_us'] is not None), key=lambda r: r['start_us'])
            cursor = 0
            for row in spans:
                start, end = row['start_us'], row['end_us']
                if start > cursor:
                    issue('primary_visual_gap', 'warning', 'Declared primary track has an uncovered interval; inspect intended background/other tracks.', context, chosen, interval=(cursor, start))
                elif start < cursor:
                    issue('primary_visual_overlap', 'warning', 'Declared primary segments overlap; verify transition/compositing intent.', context, chosen, row['segment_id'], (start, min(end, cursor)))
                cursor = max(cursor, end)
            if duration and cursor < duration:
                issue('primary_visual_gap', 'warning', 'Declared primary track does not cover the draft tail.', context, chosen, interval=(cursor, duration))
        videos = [r for r in local if r['track_type'] == 'video' and r['path'] and r['source_start_us'] is not None and r['start_us'] is not None]
        for audio in (r for r in local if r['track_type'] == 'audio' and r['path'] and r['source_start_us'] is not None and r['start_us'] is not None):
            for video in videos:
                left = max(audio['start_us'], video['start_us'])
                right = min(audio['end_us'], video['end_us'])
                if audio['path'] != video['path'] or left >= right or audio.get('nonlinear_mapping') or video.get('nonlinear_mapping'):
                    continue
                def source_at(row, when):
                    return row['source_start_us'] + (when - row['start_us']) * (row['source_end_us'] - row['source_start_us']) / (row['end_us'] - row['start_us'])
                offsets = [source_at(audio, t) - source_at(video, t) for t in (left, right)]
                if max(abs(n) for n in offsets) > tolerance:
                    issue('a_v_mapping_offset_hint', 'warning', 'Same-file overlapping audio/video use different source-time mappings; may be intentional, perceptual sync remains unverified.', context, audio['track_id'], audio['segment_id'], (left, right), video_segment_id=video['segment_id'], start_offset_us=round(offsets[0]), end_offset_us=round(offsets[1]), tolerance_us=tolerance)
        for mid, child in embedded:
            visit(child, context + '/compound:' + mid, depth=depth + 1)

    visit(snapshot, 'root', primary_track)
    duration = snapshot.get('duration')
    report['edit_boundaries_us'] = sorted(set(n for n in report['edit_boundaries_us'] if n > 0 and (type(duration) is not int or n < duration)))
    counts = {severity: sum(i['severity'] == severity for i in report['issues']) for severity in ('error', 'warning', 'info')}
    report['summary'] = dict(counts, segment_count=len(report['segments']), draft_count=len(report['drafts']), media_file_count=len(cache))
    report['status'] = 'issues_found' if counts['error'] or counts['warning'] else 'structural_clear_with_limits'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', required=True, help='Raw JSON saved after native draft reload')
    parser.add_argument('--out', help='New report JSON path; existing files are never overwritten')
    parser.add_argument('--primary-track', help='Explicit top-level primary video track ID; required for ambiguous multitrack continuity')
    parser.add_argument('--media-info', action='store_true', help='Probe source stream metadata with installed MediaInfo (no decoding)')
    args = parser.parse_args()
    path = Path(args.snapshot).resolve(strict=True)
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError('Snapshot exceeds the 100 MiB audit input bound; split the audit by embedded draft')
    output = Path(args.out).resolve() if args.out else None
    if output and output.exists():
        raise ValueError('Choose a new report file')
    raw = path.read_bytes()
    def invalid_number(value):
        raise ValueError('Nonfinite JSON number: ' + value)
    snapshot = json.loads(raw.decode('utf-8-sig'), parse_constant=invalid_number)
    probe = None
    if args.media_info:
        from common import media_info
        probe = media_info
    report = audit(snapshot, base_dir=path.parent, primary_track=args.primary_track, probe=probe)
    report['snapshot'] = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
                          'native_reload_verified_by_this_tool': False}
    if hashlib.sha256(path.read_bytes()).hexdigest() != report['snapshot']['sha256']:
        raise ValueError('Snapshot changed during audit')
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as handle:
            handle.write(text)
        print(json.dumps({'status': report['status'], 'summary': report['summary'], 'report': str(output)}, ensure_ascii=False))
    else:
        print(text)
    return 2 if report['summary']['error'] else 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    sys.exit(main())
