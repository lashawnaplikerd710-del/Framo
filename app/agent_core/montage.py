"""Evidence-bound multi-source montage, preserving original language and sound."""
from fractions import Fraction

from .timeline_compiler import PlanIssue, validate_edit_plan


def sample_positions(duration_us, count=8):
    # Match the native observation clip, which itself leaves one frame of
    # decoder tail margin. Every requested instant must be strictly inside it.
    native_end = (duration_us * 30 // 1_000_000 - 1) * 1_000_000 // 30
    if native_end <= 0:
        raise PlanIssue('Source is too short for native sampling')
    if type(count) is not int or not 2 <= count <= 108:
        raise PlanIssue('画面抽样数量应为 2 至 108 张')
    return sorted({min(native_end - 1, native_end * index // (count-1)) for index in range(count)})


def montage_candidates(asset, transcript, fps=30):
    """Speech remains in one protected block; no phrase cuts from character ASR."""
    end_frame = asset['video_duration_us'] * fps // 1_000_000 - 1
    cues = transcript['cues']
    if any(not 0 <= c['start_us'] < c['end_us'] <= asset['video_duration_us'] for c in cues):
        raise PlanIssue('Transcript exceeds source stream')
    ranges = [(0, end_frame, 'whole_source')]
    if cues:
        # Keep the complete detected utterance and surrounding lead-in/tail.
        speech_end = min(end_frame, (max(c['end_us'] for c in cues) * fps + 999_999) // 1_000_000 + fps // 2)
        if speech_end < end_frame:
            ranges.append((0, speech_end, 'complete_detected_speech_with_tail'))
    else:
        # Non-recognition is not proof of silence. Preserve embedded audio and
        # expose the uncertainty to the model and the delivery report.
        step = max(1, fps * 5 // 2)
        for start in range(0, end_frame, step):
            end = min(end_frame, start + step)
            if end - start >= fps:
                ranges.append((start, end, 'visual_window_no_speech_detected'))
    rows = []
    for index, (first, last, kind) in enumerate(ranges):
        start, end = first * 1_000_000 // fps, last * 1_000_000 // fps
        if start >= end:
            continue
        sid = asset['asset_id'] + f'_segment_{index:03d}'
        rows.append({'schema_version': '1.0', 'segment_id': sid, 'evidence_id': sid + '_evidence',
                     'asset_id': asset['asset_id'], 'start_us': start, 'end_us': end,
                     'duration_ms': (end - start) // 1000, 'kind': kind,
                     'text': ''.join(c['text'] for c in cues), 'source': 'local_asr_and_native_stills',
                     'uncertainty': 'ASR needs proofreading; empty ASR does not prove absence of speech; static frames do not prove continuous motion'})
    return rows


def validate_montage_plan(plan, candidates, task):
    from .original_montage import active, MAX_CLIPS
    selected = validate_edit_plan(plan, candidates, task['target'],
                                  max_clips=MAX_CLIPS if active(task) else 40 if task.get('creation_mode') == 'long_video_v1' else 12)
    if len({row['asset_id'] for row in selected}) < 2 and not task.get('allow_single_source'):
        raise PlanIssue('A montage must use at least two different supplied sources')
    for index, left in enumerate(selected):
        for right in selected[index + 1:]:
            if (left['asset_id'] == right['asset_id'] and
                    max(left['start_us'], right['start_us']) < min(left['end_us'], right['end_us'])):
                raise PlanIssue('Selected alternatives overlap in the same source; do not repeat the same footage or speech')
    if not task['constraints']['allow_reorder']:
        order = {aid: i for i, aid in enumerate(task['asset_ids'])}
        keys = [(order[row['asset_id']], row['start_us']) for row in selected]
        if keys != sorted(keys):
            raise PlanIssue('Selected order violates the original-source order constraint')
    text = ''.join(row['text'] for row in selected)
    if any(phrase not in text for phrase in task['constraints']['must_keep']):
        raise PlanIssue('Montage omits a must_keep phrase')
    if any(phrase in text for phrase in task['constraints']['must_remove']):
        raise PlanIssue('Montage includes a must_remove phrase')
    return selected


def compile_montage(task, assets, candidates, plan):
    selected = validate_montage_plan(plan, candidates, task)
    known = {row['asset_id']: row for row in assets}
    if set(known) != set(task['asset_ids']):
        raise PlanIssue('Montage source manifest does not match the task')
    fps = task['target']['fps']
    first = assets[0]
    ratio = task['target']['aspect_ratio']
    direct = task.get('render_backend') == 'direct_ffmpeg'
    from .video_geometry import canvas_for
    canvas = canvas_for(task['target'], first)
    width, height = canvas['width'], canvas['height']
    for asset in assets:
        if asset['frame_rate_mode'] == 'VFR' or (not direct and asset.get('rotation', 0) != 0):
            raise PlanIssue('Montage requires unrotated CFR media')
        if not direct and Fraction(asset['width'], asset['height']) != Fraction(width, height):
            raise PlanIssue('Montage sources must match the target aspect ratio')
    native = {'schema': 'jianying-native-timeline-v1', 'name': task.get('title') or 'Agent 多素材混剪',
              'canvas': {'width': width, 'height': height, 'fps': fps}, 'steps': []}
    mapping, cursor_frames = [], 0
    for index, row in enumerate(selected, 1):
        asset = known.get(row['asset_id'])
        if asset is None:
            raise PlanIssue('Selected candidate references an unregistered source')
        # Validate the requested source range before quantization.  Duration
        # metadata may have millisecond precision, while frame boundaries do
        # not (15.933s versus 478/30s). Round inward at the physical tail.
        if not 0 <= row['start_us'] < row['end_us'] <= asset['video_duration_us']:
            raise PlanIssue('Montage source interval leaves the video stream')
        start_frame = (row['start_us'] * fps + 500_000) // 1_000_000
        end_frame = min((row['end_us'] * fps + 500_000) // 1_000_000,
                        asset['video_duration_us'] * fps // 1_000_000)
        frames = end_frame - start_frame
        start = start_frame * 1_000_000 // fps
        target_start = cursor_frames * 1_000_000 // fps
        target_end = (cursor_frames + frames) * 1_000_000 // fps
        duration = target_end - target_start
        # Global and source frame clocks can differ by one microsecond.
        if frames > 0 and start + duration > asset['video_duration_us']:
            frames -= 1
            target_end = (cursor_frames + frames) * 1_000_000 // fps
            duration = target_end - target_start
        if not 0 <= start < start + duration <= asset['video_duration_us']:
            raise PlanIssue('Montage source interval leaves the video stream')
        # When the task explicitly mutes source audio, the embedded audio
        # stream is not part of the rendered timeline and must not constrain a
        # visual-only interval that is still inside the video stream.
        if (asset.get('has_audio') and not task.get('mute_source_audio')
                and start + duration > (asset.get('audio_duration_us') or 0) + 34_000):
            raise PlanIssue('Montage interval extends beyond its embedded audio stream')
        clip = f'clip_{index:03d}'
        native['steps'].append({'action': 'video.add', 'id': clip, 'path': asset['path'], 'track': 0,
                               'start_us': target_start, 'duration_us': duration, 'source_start_us': start})
        # A short audio envelope reduces hard clicks; it is not a music crossfade.
        if asset.get('has_audio'):
            fade = min(50_000, duration // 4)
            native['steps'].append({'action': 'video.setFade', 'segment': clip,
                                   'fade_in_us': fade, 'fade_out_us': fade})
        mapping.append({'schema_version': '1.0', 'source_segment_id': row['segment_id'],
                        'asset_id': row['asset_id'], 'source_start_us': start,
                        'source_end_us': start + duration, 'timeline_start_us': target_start,
                        'timeline_end_us': target_end})
        cursor_frames += frames
    duration = cursor_frames * 1_000_000 // fps
    if duration > 600_000_000:
        raise PlanIssue('成片总时长不能超过 10 分钟，请减少选段')
    if duration < task['target'].get('minimum_duration_ms',0)*1000:
        raise PlanIssue('Compiled montage is below the required minimum duration')
    desired = task['target']['duration_ms']
    if desired is not None and abs(duration - desired * 1000) > task['target']['tolerance_ms'] * 1000:
        raise PlanIssue('Compiled montage duration is outside the requested tolerance')
    return {'schema_version': '1.0', 'revision_id': 'revision_001', 'duration_us': duration,
            'native_plan': native, 'source_map': mapping, 'captions': [],
            'subtitle_policy': 'preserve_source', 'effects': [], 'transition_overlap_us': 0,
            'audio_strategy': 'embedded_source_audio_with_50ms_edge_fades',
            'quality_limits': ['Original burned text is retained; no new ASR subtitle layer',
                               'Music continuity, ASR accuracy and continuous audiovisual quality require review']}
