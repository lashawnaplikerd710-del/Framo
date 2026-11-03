"""Deterministic single-source speech cuts and subtitle remapping.

All intervals are integer microseconds and half-open. Model output supplies only
candidate IDs; source times and captions always come from inspected evidence.
"""
from __future__ import annotations

from fractions import Fraction


class PlanIssue(ValueError):
    pass


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise PlanIssue(f'{name} must be an integer >= {minimum}')


def _floor_frame(us: int, fps: int) -> int:
    return (us * fps // 1_000_000) * 1_000_000 // fps


def _ceil_frame(us: int, fps: int) -> int:
    return ((us * fps + 999_999) // 1_000_000) * 1_000_000 // fps


def candidates_from_cues(asset_id: str, cues: list[dict], duration_us: int, *, max_group_us=7_000_000):
    """Group adjacent transcript cues without rewriting any speech."""
    _integer(duration_us, 'duration_us', 1)
    if not cues:
        raise PlanIssue('No timed transcript cues; semantic selection is unavailable')
    groups, current = [], []
    previous_end = 0
    for index, cue in enumerate(cues, 1):
        start, end = cue.get('start_us'), cue.get('end_us')
        _integer(start, f'cue {index} start_us')
        _integer(end, f'cue {index} end_us', 1)
        if not start < end <= duration_us or start < previous_end:
            raise PlanIssue(f'Cue {index} overlaps another cue or leaves source duration')
        if not isinstance(cue.get('text'), str) or not cue['text'].strip():
            raise PlanIssue(f'Cue {index} lacks spoken text')
        if current and (start - current[-1]['end_us'] > 350_000 or
                        end - current[0]['start_us'] > max_group_us):
            groups.append(current); current = []
        current.append({**cue, 'cue_id': f'cue_{index:04d}'})
        previous_end = end
    if current:
        groups.append(current)
    return [{'schema_version': '1.0', 'segment_id': f'segment_{index:04d}',
             'evidence_id': f'evidence_{index:04d}', 'asset_id': asset_id,
             'start_us': group[0]['start_us'], 'end_us': group[-1]['end_us'],
             'text': ''.join(cue['text'] for cue in group),
             'cue_ids': [cue['cue_id'] for cue in group],
             'source': 'timed_local_transcript', 'uncertainty': 'ASR text and word timing require review'}
            for index, group in enumerate(groups, 1)]


def validate_edit_plan(plan: dict, candidates: list[dict], target: dict, *, max_clips=80):
    if not isinstance(plan, dict) or plan.get('schema_version') != '1.0':
        raise PlanIssue('EditPlan schema_version must be 1.0')
    clips = plan.get('clips')
    if not isinstance(clips, list) or not 1 <= len(clips) <= max_clips:
        raise PlanIssue('EditPlan must contain 1..80 clips')
    known = {row['segment_id']: row for row in candidates}
    seen = set()
    duration = 0
    for index, clip in enumerate(clips, 1):
        if not isinstance(clip, dict) or set(clip) - {'source_segment_id', 'role', 'reason', 'evidence_ids'}:
            raise PlanIssue(f'Clip {index} contains unsupported fields')
        sid = clip.get('source_segment_id')
        if sid not in known:
            raise PlanIssue(f'Clip {index} references nonexistent segment ID {sid!r}')
        if sid in seen:
            raise PlanIssue(f'Clip {index} repeats segment ID {sid!r}; first version does not repeat speech')
        seen.add(sid)
        if not isinstance(clip.get('reason'), str) or not clip['reason'].strip():
            raise PlanIssue(f'Clip {index} needs an editorial reason')
        if not isinstance(clip.get('evidence_ids'), list) or known[sid]['evidence_id'] not in clip['evidence_ids']:
            raise PlanIssue(f'Clip {index} must cite its actual transcript evidence')
        duration += known[sid]['end_us'] - known[sid]['start_us']
    desired = target.get('duration_ms')
    tolerance = target.get('tolerance_ms', 3000)
    if desired is not None:
        _integer(desired, 'target.duration_ms', 1)
        _integer(tolerance, 'target.tolerance_ms')
        # Source cue durations are estimates. The compiler makes the final check
        # after frame quantization; this catches obviously impossible proposals.
        if abs(duration - desired * 1000) > (tolerance + len(clips) * 34) * 1000:
            raise PlanIssue(f'Selected speech totals {duration / 1e6:.3f}s, outside target {desired / 1000:.3f}s ± {tolerance / 1000:.3f}s')
    return [known[clip['source_segment_id']] for clip in clips]


def compile_timeline(task: dict, asset: dict, transcript: dict, candidates: list[dict], plan: dict):
    """Return native plan, mapped captions and a source-to-output map."""
    if task.get('schema_version') != '1.0' or asset.get('schema_version') != '1.0' or transcript.get('schema_version') != '1.0':
        raise PlanIssue('All input contracts require schema_version 1.0')
    if transcript.get('asset_id') != asset.get('asset_id'):
        raise PlanIssue('Transcript and media asset identity differ')
    target = task['target']
    fps = target.get('fps', 30)
    _integer(fps, 'fps', 1)
    if fps > 120:
        raise PlanIssue('fps exceeds 120')
    if asset.get('frame_rate_mode') == 'VFR':
        raise PlanIssue('Variable frame rate requires a timestamp-preserving proxy not yet implemented')
    if task.get('render_backend') != 'direct_ffmpeg' and asset.get('rotation') not in (0, 0.0, None):
        raise PlanIssue('Rotated video has no verified adaptation')
    width, height = asset['width'], asset['height']
    ratio = target.get('aspect_ratio')
    if ratio == '9:16':
        canvas = {'width': 1080, 'height': 1920, 'fps': fps}
    elif ratio == '16:9':
        canvas = {'width': 1920, 'height': 1080, 'fps': fps}
    elif ratio == 'source':
        canvas = {'width': width, 'height': height, 'fps': fps}
    else:
        raise PlanIssue('Unsupported target aspect_ratio')
    if task.get('render_backend') == 'direct_ffmpeg':
        from .video_geometry import canvas_for
        canvas = {**canvas_for(target, asset), 'fps':fps}
    if task.get('render_backend') != 'direct_ffmpeg' and Fraction(width, height) != Fraction(canvas['width'], canvas['height']):
        raise PlanIssue('Source and target aspect ratios differ; verified contain/pan-and-scan is unavailable')
    selected = validate_edit_plan(plan, candidates, target)
    cue_by_id = {f'cue_{index:04d}': cue for index, cue in enumerate(transcript['cues'], 1)}
    native = {'schema': 'jianying-native-timeline-v1', 'name': task.get('title') or 'Agent 口播精剪',
              'canvas': canvas, 'steps': []}
    mapping, captions, cursor_frames = [], [], 0
    stream_limit = asset['video_duration_us']
    for index, segment in enumerate(selected, 1):
        first_frame = segment['start_us'] * fps // 1_000_000
        last_frame = (segment['end_us'] * fps + 999_999) // 1_000_000
        source_start = first_frame * 1_000_000 // fps
        frame_count = last_frame - first_frame
        cursor = cursor_frames * 1_000_000 // fps
        target_end = (cursor_frames + frame_count) * 1_000_000 // fps
        duration = target_end - cursor
        source_end = source_start + duration
        if not 0 <= source_start < source_end <= stream_limit:
            raise PlanIssue(f'{segment["segment_id"]} leaves video stream or collapses after quantization')
        audio_limit = asset.get('audio_duration_us')
        if type(audio_limit) is int and source_end > audio_limit + (1_000_000 // fps + 1):
            raise PlanIssue(f'{segment["segment_id"]} extends beyond the audio stream')
        native['steps'].append({'action': 'video.add', 'id': f'clip_{index:03d}',
            'path': asset['path'], 'track': 0, 'start_us': cursor,
            'duration_us': duration, 'source_start_us': source_start})
        mapping.append({'schema_version': '1.0', 'source_segment_id': segment['segment_id'],
            'asset_id': asset['asset_id'], 'source_start_us': source_start,
            'source_end_us': source_end, 'timeline_start_us': cursor,
            'timeline_end_us': cursor + duration})
        for cue_id in segment['cue_ids']:
            cue = cue_by_id[cue_id]
            if not segment['start_us'] <= cue['start_us'] < cue['end_us'] <= segment['end_us']:
                raise PlanIssue(f'{cue_id} does not fit entirely within {segment["segment_id"]}')
            start = cursor + cue['start_us'] - source_start
            end = cursor + cue['end_us'] - source_start
            # Native subtitle writer rounds upward to frame boundaries. Apply
            # the same rule now so overlaps/collapses cannot appear after build.
            start = _ceil_frame(max(cursor, start), fps)
            end = min(cursor + duration, _ceil_frame(end, fps))
            if captions and start < captions[-1]['end_us']:
                start = captions[-1]['end_us']
            if not start < end:
                raise PlanIssue(f'{cue_id} collapses/overlaps after output-frame alignment')
            captions.append({'start_us': start, 'end_us': end, 'text': cue['text'],
                'source_cue_id': cue_id, 'source_segment_id': segment['segment_id'],
                'source_start_us': cue['start_us'], 'source_end_us': cue['end_us']})
        cursor_frames += frame_count
    cursor = cursor_frames * 1_000_000 // fps
    desired = target.get('duration_ms')
    tolerance = target.get('tolerance_ms', 3000)
    if desired is not None and abs(cursor - desired * 1000) > tolerance * 1000:
        raise PlanIssue(f'Compiled timeline is {cursor / 1e6:.3f}s, outside target {desired / 1000:.3f}s ± {tolerance / 1000:.3f}s')
    return {'schema_version': '1.0', 'revision_id': plan.get('revision_id', 'revision_001'),
            'duration_us': cursor, 'native_plan': native, 'captions': captions,
            'source_map': mapping, 'effects': [], 'transition_overlap_us': 0,
            'quality_limits': ['Transcript and cut semantics still need human review',
                               'No perceptual audio/lip-sync acceptance in compiler']}
