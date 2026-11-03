"""Bounded local execution of an explicit model request to remove a repeated ending.

This does not represent a second model review. It preserves the original version
and requires independent ASR evidence for the replacement post-speech tail.
"""
import copy
import math

from .montage import compile_montage
from .timeline_compiler import PlanIssue


def replace_repeated_ending(task, assets, candidates, plan, review, transcripts, tail_probe):
    issues = review.get('issues', [])
    if review.get('decision') != 'revise' or len(issues) != 1:
        raise PlanIssue('This repair handles exactly one explicit model revision issue')
    issue = issues[0]
    last = plan['clips'][-1]
    if (issue.get('source_segment_id') != last['source_segment_id'] or
            '重复' not in issue.get('problem', '') or
            '删除结尾段' not in issue.get('suggested_fix', '')):
        raise PlanIssue('Model review did not explicitly request removal of the repeated ending')
    original = next(row for row in candidates if row['segment_id'] == last['source_segment_id'])
    asset = next(a for a in assets if a['asset_id'] == original['asset_id'])
    cues = transcripts[asset['asset_id']]['cues']
    if not cues:
        raise PlanIssue('Original ending has no timed speech evidence')
    fps = task['target']['fps']
    start_frame = math.ceil((max(c['end_us'] for c in cues) + 500_000) * fps / 1_000_000)
    end_frame = asset['video_duration_us'] * fps // 1_000_000 - 1
    earliest, end = start_frame * 1_000_000 // fps, end_frame * 1_000_000 // fps
    interval = tail_probe.get('range_us')
    if (not isinstance(interval, list) or len(interval) != 2 or
            any(type(t) is not int for t in interval) or interval[1] != end or interval[0] < earliest):
        raise PlanIssue('Tail probe is not beyond the complete detected speech and safety margin')
    start = interval[0]
    aligned = ((start * fps + 500_000) // 1_000_000) * 1_000_000 // fps
    if abs(start - aligned) > 1:
        raise PlanIssue('Replacement tail must start on the project frame grid')
    if end - start < 1_000_000:
        raise PlanIssue('No sufficiently long post-speech tail is available')
    if (tail_probe.get('asset_id') != asset['asset_id'] or
            tail_probe.get('source_sha256') != asset['sha256'] or
            tail_probe.get('range_us') != [start, end] or
            tail_probe.get('engine') != 'local_sensevoice_tail_probe' or
            not isinstance(tail_probe.get('raw_result'), dict) or
            not isinstance(tail_probe['raw_result'].get('text'), str) or
            any(char.isalnum() for char in tail_probe['raw_result']['text'])):
        raise PlanIssue('Replacement tail lacks matching independent no-speech-detected evidence')
    sid = original['segment_id'] + '_post_speech_tail'
    row = {'schema_version': '1.0', 'segment_id': sid, 'evidence_id': sid + '_evidence',
           'asset_id': asset['asset_id'], 'start_us': start, 'end_us': end,
           'duration_ms': (end - start) // 1000, 'kind': 'post_speech_tail', 'text': '',
           'source': 'local_sensevoice_tail_probe',
           'uncertainty': 'ASR non-recognition is not proof of silence; music and continuous playback need review'}
    revised = copy.deepcopy(plan)
    revised['revision_id'] = 'revision_002'
    revised['clips'][-1] = {'source_segment_id': sid, 'role': 'ending',
        'reason': 'Local execution of DeepSeek review: remove the repeated ending speech while retaining this source post-roll picture and embedded sound',
        'evidence_ids': [row['evidence_id']]}
    revised['repair_origin'] = 'local_program_applied_prior_deepseek_review'
    revised['second_model_review_performed'] = False
    revised_candidates = candidates + [row]
    added_visual_id = None
    try:
        timeline = compile_montage(task, assets, revised_candidates, revised)
    except PlanIssue as original_error:
        # The reviewer also permits extending other visual material. Try one
        # already-evidenced, unused non-speech window; never invent source times.
        timeline = None
        for candidate in candidates:
            if candidate.get('kind') != 'visual_window_no_speech_detected':
                continue
            trial = copy.deepcopy(revised)
            trial['clips'].insert(-1, {'source_segment_id': candidate['segment_id'], 'role': 'detail',
                'reason': 'Local execution of DeepSeek review: extend an existing visual candidate to meet duration after removing repeated speech',
                'evidence_ids': [candidate['evidence_id']]})
            try:
                timeline = compile_montage(task, assets, revised_candidates, trial)
            except PlanIssue:
                continue
            revised, added_visual_id = trial, candidate['segment_id']
            break
        if timeline is None:
            raise original_error
    timeline['revision_id'] = 'revision_002'
    timeline['native_plan']['name'] = '宠物马甲_DeepSeek混剪_修订版'
    resolution = {'schema_version': '1.0', 'original_issue': issue,
        'action': 'replace_repeated_ending_speech_with_verified_post_speech_tail',
        'removed_speech_segment_id': original['segment_id'], 'replacement_segment_id': sid,
        'source_asset_id': asset['asset_id'], 'replacement_source_range_us': [start, end],
        'added_visual_segment_id': added_visual_id,
        'known_repeated_speech_ranges_excluded': all(c['end_us'] <= start for c in cues),
        'second_model_review_performed': False, 'continuous_av_reviewed': False}
    return revised, revised_candidates, timeline, resolution
