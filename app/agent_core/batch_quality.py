"""Batch-level structural quality checks.

The JSON checks are deterministic. Black-frame and continuous dropped-frame
claims require native preview/audio or decoded export evidence and therefore
remain explicitly pending when that evidence is absent.
"""
from __future__ import annotations

from pathlib import Path


def inspect_batch_snapshot(snapshot, *, strict_files=False):
    issues = []
    outputs = snapshot.get('batch_outputs') or []
    duration = snapshot.get('duration')
    if type(duration) is not int or duration <= 0:
        issues.append({'code': 'invalid_master_duration', 'severity': 'blocking'})
    for track in snapshot.get('tracks', []):
        for segment in track.get('segments', []):
            timerange = segment.get('target_timerange') or {}
            start, length = timerange.get('start'), timerange.get('duration')
            if type(start) is not int or type(length) is not int or length <= 0 or start < 0 or start + length > (duration or 0):
                issues.append({'code': 'segment_outside_master', 'severity': 'blocking',
                               'track': track.get('id'), 'segment': segment.get('id')})
            if track.get('type') in ('audio', 'text') and not any(
                    row['start_us'] <= start and start + length <= row['end_us'] for row in outputs):
                issues.append({'code': 'audio_or_caption_crosses_output_gap', 'severity': 'blocking',
                               'track': track.get('id'), 'segment': segment.get('id')})
    materials = snapshot.get('materials', {})
    missing = []
    for group in materials.values():
        if not isinstance(group, list):
            continue
        for material in group:
            if not isinstance(material, dict):
                continue
            path = material.get('path') or material.get('media_path')
            if strict_files and path and not Path(path).is_file():
                missing.append(path)
    if missing:
        issues.append({'code': 'material_missing', 'severity': 'blocking',
                       'paths': sorted(set(missing))})
    # These flags are intentionally not inferred from structural JSON.
    evidence = {
        'draft_readable': not issues,
        'material_files_present': not missing,
        'audio_video_mapping_checked': not any(i['code'].startswith(('audio_', 'caption_')) for i in issues),
        'caption_narration_correspondence': 'pending_native_review',
        'black_frame_scan': 'pending_native_preview',
        'dropped_frame_scan': 'pending_native_preview',
        'cross_output_content_scan': 'structural_only',
        'continuous_av_reviewed': False,
    }
    return {'schema_version': '1.0', 'status': 'clear' if not issues else 'issues',
            'issues': issues, 'evidence': evidence}
