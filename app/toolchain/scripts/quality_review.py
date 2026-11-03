"""Plan whole-timeline QA and enforce evidence scope; never infer listening from meters."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import sys

SCHEMA = 'jianying-quality-plan-v1'
REVIEW = 'jianying-quality-review-v1'
RESULT = 'jianying-quality-result-v1'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def binding(path):
    path = Path(path).resolve(strict=True)
    return {'path': str(path), 'sha256': digest(path)}


def verify(item):
    if digest(item['path']) != item['sha256']:
        raise ValueError('Evidence or final project changed: ' + item['path'])


def positive(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(name + ' must be a positive integer')
    return value


def windows(duration, size=60000000, overlap=3000000):
    positive(duration, 'duration'); positive(size, 'window')
    if type(overlap) is not int or not 0 <= overlap < size:
        raise ValueError('overlap must be smaller than window')
    result = []
    start = 0
    while start < duration:
        end = min(duration, start + size)
        result.append([start, end])
        if end == duration:
            break
        start = end - overlap
    return result


def covers(ranges, start, end):
    cursor = start
    for a, b in sorted(ranges):
        if b <= cursor:
            continue
        if a > cursor:
            return False
        cursor = max(cursor, b)
        if cursor >= end:
            return True
    return False


def interval(value, duration):
    if (not isinstance(value, list) or len(value) != 2 or
        any(type(t) is not int for t in value) or not 0 <= value[0] < value[1] <= duration):
        raise ValueError('Invalid observed interval')
    return value


def assets(snapshot):
    """Bind all existing local path references, including external font/image files."""
    found = {}
    unresolved = []
    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, str) and item and (key == 'path' or key.endswith('_path')):
                    p = Path(item)
                    if p.is_file():
                        found[str(p.resolve()).casefold()] = binding(p)
                    elif p.is_absolute():
                        unresolved.append(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(snapshot.get('materials', {}))
    return list(found.values()), sorted(set(unresolved))


def make_plan(snapshot, draft_file, snapshot_file, chapters=None, window_us=60000000,
              overlap_us=3000000, frame_step_us=None, audio_expected=True):
    duration = positive(snapshot.get('duration'), 'draft duration')
    fps = snapshot.get('fps', 30)
    if type(fps) not in (int, float) or not math.isfinite(fps) or fps <= 0:
        raise ValueError('Invalid frame rate')
    frame = max(1, round(1000000 / fps))
    step = frame_step_us or (1000000 if duration <= 180000000 else 5000000)
    positive(step, 'frame interval')
    if duration // step > 100000:
        raise ValueError('Frame schedule exceeds budget; choose an explicit larger scan interval')
    marked_chapters = bool(chapters)
    chapters = chapters or [{'start_us': 0, 'end_us': duration, 'summary': 'Whole film; semantic chapter map pending'}]
    cursor = 0
    for chapter in chapters:
        a, b = interval([chapter['start_us'], chapter['end_us']], duration)
        if a != cursor or not str(chapter.get('summary', '')).strip():
            raise ValueError('Chapters must be consecutive and have content summaries')
        cursor = b
    if cursor != duration:
        raise ValueError('Chapters must reach final film end')
    boundaries = {0, duration}
    overlays = []
    for track in snapshot.get('tracks', []):
        for segment in track.get('segments', []):
            timerange = segment.get('target_timerange') or {}
            a = timerange.get('start'); d = timerange.get('duration')
            if type(a) is int and type(d) is int and 0 <= a < a+d <= duration:
                boundaries.update([a, a+d])
                if track.get('type') in ('text', 'sticker', 'effect', 'filter'):
                    overlays.append((segment.get('id'), a, a+d))
    for chapter in chapters:
        boundaries.update([chapter['start_us'], chapter['end_us']])
    points = set(range(0, duration, step)) | {max(0, duration-frame)}
    for at in boundaries:
        points.update(t for t in (at-frame, at, at+frame) if 0 <= t < duration)
    for _, a, b in overlays:
        points.add((a+b)//2)
    units = []
    def add(kind, a, b, allowed, note):
        units.append({'id': f'{kind}-{len(units)+1:04d}', 'kind': kind,
                      'range_us': [a, b], 'allowed_evidence': allowed, 'check': note})
    for a, b in windows(duration, window_us, overlap_us):
        add('visual_playback', a, b, ['continuous_av', 'continuous_video'],
            '连续画面：黑闪帧/卡顿/动作连续性/转场；静帧不能替代连续观看')
        if audio_expected:
            add('listening', a, b, ['continuous_av', 'audio_listening'],
                '实听最终混音：词头尾/断句/噪声/音乐遮盖/突变/疲劳；电平不替代')
    for chapter in chapters:
        add('chapter_narrative', chapter['start_us'], chapter['end_us'],
            ['continuous_av'] if audio_expected else ['continuous_av', 'continuous_video'],
            '本段主张、证据、条件否定词、目的与前后承接；' + chapter['summary'])
    add('global_narrative', 0, duration, ['continuous_av'] if audio_expected else ['continuous_av', 'continuous_video'],
        '通篇观点/人物/因果/重复/节奏曲线/开头承诺与结尾兑现；分段记忆需汇总后复核')
    if audio_expected:
        anchors = boundaries | {duration//2} | set(range(0, duration, window_us))
        for at in sorted(anchors):
            add('synchronization', max(0, at-2000000), min(duration, at+2000000), ['continuous_av'],
                '可见发声或动作瞬态的声画关系；记录依据/偏移与方向，结构相等不等于同步')
    for sid, a, b in overlays:
        add('overlay', a, b, ['native_frames', 'continuous_av', 'continuous_video'],
            '检查文字准确、可读、遮挡、起止与画面/语义对应；segment=' + str(sid))
    bound_assets, unresolved = assets(snapshot)
    project_files = [binding(p) for p in Path(draft_file).parent.glob('*.json') if p.is_file()]
    return {'schema': SCHEMA, 'scope': 'editable_native_draft', 'duration_us': duration,
            'draft': binding(draft_file), 'snapshot': binding(snapshot_file),
            'project_files': project_files, 'assets': bound_assets,
            'unresolved_asset_paths': unresolved, 'audio_expected': bool(audio_expected),
            'chapters_semantically_mapped': marked_chapters, 'chapters': chapters,
            'window_us': window_us, 'overlap_us': overlap_us,
            'frame_step_us': step, 'frame_times_us': sorted(points),
            'frame_scan_scope': 'sampled_instants_including_all_known_edit_boundaries; not every decoded frame',
            'sync_policy': {'tolerance_us': frame, 'offset_sign': 'audio minus video',
                            'basis': 'internal one-frame review trigger; not a platform or industry guarantee'},
            'units': units, 'technical_required': ['structure', 'native_reload'],
            'limitations': ['Review records are reviewer attestations, not independent proof of semantic truth.',
                            'Draft acceptance does not certify an exported MP4 or other inputs/versions.']}


def check_plan(plan):
    if plan.get('schema') != SCHEMA:
        raise ValueError('Unknown quality plan')
    for item in [plan['draft'], plan['snapshot']] + plan['project_files'] + plan['assets']:
        verify(item)
    # Reconstruct requirements, so editing the plan cannot silently omit a gate.
    rebuilt = make_plan(read(plan['snapshot']['path']), plan['draft']['path'], plan['snapshot']['path'],
                        plan['chapters'] if plan['chapters_semantically_mapped'] else None,
                        plan['window_us'], plan['overlap_us'], plan['frame_step_us'], plan['audio_expected'])
    for key in ('scope', 'duration_us', 'units', 'frame_times_us', 'technical_required', 'assets', 'project_files', 'unresolved_asset_paths'):
        if rebuilt[key] != plan[key]:
            raise ValueError('Quality plan requirements changed: ' + key)
    if plan.get('sync_policy', rebuilt['sync_policy']) != rebuilt['sync_policy']:
        raise ValueError('Per-event reviewer cannot relax the project sync policy')


def prepare(install, draft, output, chapters=None, window_us=60000000, overlap_us=3000000,
            frame_step_us=None, audio_expected=True):
    from native_service import Service
    draft = Path(draft).resolve(strict=True)
    if draft.is_dir():
        draft = draft/'draft_content.json'
    output = Path(output).resolve()
    if output.exists() or output.is_relative_to(draft.parent):
        raise ValueError('Use a new QA directory outside the final draft')
    before = binding(draft)
    output.mkdir(parents=True)
    with Service(Path(install), output/'readback-service') as service:
        snapshot = json.loads(service.request('draft', 'readFile', {'path': str(draft), 'rootPath': str(draft.parent)})['result']['content'])
    verify(before)
    snap_path = output/'native-snapshot.json'
    write(snap_path, snapshot)
    plan = make_plan(snapshot, draft, snap_path, chapters, window_us, overlap_us, frame_step_us, audio_expected)
    write(output/'quality-plan.json', plan)
    blank = {'schema': REVIEW, 'plan': binding(output/'quality-plan.json'), 'reviewer': 'current Codex session',
             'evidence': [], 'technical': [], 'units': [], 'issues': [],
             'note': 'Populate only after actual review. Unfilled units stay unverified.'}
    write(output/'review-template.json', blank)
    return {'plan': str(output/'quality-plan.json'), 'units': len(plan['units']),
            'frame_points': len(plan['frame_times_us']), 'status': 'planned_not_reviewed'}


def capture(plan_path, install, output, width=640):
    from native_preview import preview
    plan = read(plan_path); check_plan(plan)
    output = Path(output).resolve()
    if output.exists() or output.is_relative_to(Path(plan['draft']['path']).parent):
        raise ValueError('Use a new frame directory outside draft')
    output.mkdir(parents=True)
    frames = []
    for index in range(0, len(plan['frame_times_us']), 20):
        points = plan['frame_times_us'][index:index+20]
        report = preview(install, plan['draft']['path'], output/f'batch-{index//20:04d}', points, width)
        frames.extend(report['frames'])
        write(output/f'batch-{index//20:04d}-binding.json', {'plan': binding(plan_path), 'draft': plan['draft'], 'frames': report['frames']})
        print(json.dumps({'captured': len(frames), 'total': len(plan['frame_times_us'])}), flush=True)
    check_plan(plan)
    result = {'schema': 'jianying-quality-frames-v1', 'plan': binding(plan_path), 'draft': plan['draft'],
              'frames': frames, 'status': 'captured_not_reviewed', 'continuous_playback_verified': False}
    write(output/'frames.json', result)
    return result


def validate(plan_path, review_path, output=None):
    plan = read(plan_path); check_plan(plan)
    review = read(review_path)
    if review.get('schema') != REVIEW or review.get('plan') != binding(plan_path):
        raise ValueError('Review belongs to another plan or revision')
    duration = plan['duration_us']; errors = []; missing = []; evidence = {}
    for row in review.get('evidence', []):
        if not row.get('id') or row['id'] in evidence:
            raise ValueError('Duplicate/missing evidence ID')
        verify(row['artifact'])
        if row.get('draft_sha256') != plan['draft']['sha256']:
            raise ValueError('Evidence is not bound to this draft')
        if not str(row.get('method', '')).strip():
            raise ValueError('Evidence method required')
        for span in row.get('ranges_us', []):
            interval(span, duration)
        if row.get('kind') in ('continuous_av', 'continuous_video', 'audio_listening'):
            if row.get('actually_reviewed') is not True or not str(row.get('observer', '')).strip():
                raise ValueError('Playback/listening needs explicit actual observer attestation')
        evidence[row['id']] = row
    def usable(refs, kinds):
        rows = []
        for ref in refs:
            if ref not in evidence:
                raise ValueError('Unknown evidence reference: ' + str(ref))
            if evidence[ref]['kind'] in kinds:
                rows.append(evidence[ref])
        return rows
    records = {}
    unit_ids = {u['id'] for u in plan['units']}
    for row in review.get('units', []):
        if row.get('id') not in unit_ids or row['id'] in records:
            raise ValueError('Duplicate/unknown review unit')
        if row.get('status') not in ('pass', 'fail', 'unverified'):
            raise ValueError('Required units cannot be removed with not_applicable')
        records[row['id']] = row
    for unit in plan['units']:
        row = records.get(unit['id'], {})
        if row.get('status') == 'fail':
            errors.append(unit['id'] + ': reviewer found failure'); continue
        if row.get('status') != 'pass':
            missing.append(unit['id']); continue
        if not str(row.get('finding', '')).strip():
            errors.append(unit['id'] + ': missing concrete finding'); continue
        if unit['kind'] == 'synchronization':
            sync = row.get('sync_assessment', {})
            if sync.get('mode') == 'event_based':
                valid_numbers = all(type(sync.get(k)) in (int, float) and math.isfinite(sync[k])
                                    for k in ('offset_us', 'resolution_us', 'tolerance_us'))
                if (not valid_numbers or sync.get('resolution_us', 0) <= 0 or sync.get('tolerance_us', 0) <= 0
                    or not all(str(sync.get(k, '')).strip() for k in ('video_event', 'audio_event', 'method'))
                    or type(sync.get('event_time_us')) is not int
                    or not unit['range_us'][0] <= sync['event_time_us'] < unit['range_us'][1]):
                    errors.append(unit['id'] + ': actual synchronization event/method required')
                elif (abs(sync['offset_us']) > sync['tolerance_us'] or sync['resolution_us'] > sync['tolerance_us']
                      or sync['tolerance_us'] > round(1000000 / read(plan['snapshot']['path']).get('fps', 30))):
                    errors.append(unit['id'] + ': sync outside tolerance or measurement too coarse')
            elif sync.get('mode') == 'intentional_voiceover':
                if not str(sync.get('reason', '')).strip() or not str(sync.get('observed_relationship', '')).strip():
                    errors.append(unit['id'] + ': voiceover relation must be observed and justified')
            else:
                errors.append(unit['id'] + ': synchronization cannot be certified by track alignment')
        rows = usable(row.get('evidence_ids', []), unit['allowed_evidence'])
        ranges = [span for e in rows if e['kind'] != 'native_frames' for span in e.get('ranges_us', [])]
        a, b = unit['range_us']
        covered = covers(ranges, a, b)
        if unit['kind'] == 'overlay' and not covered:
            # For static overlays require inspected frames near start, middle and end.
            pts = [t for e in rows if e['kind'] == 'native_frames' and e.get('actually_reviewed') is True
                   for t in e.get('times_us', []) if type(t) is int and a <= t < b]
            margin = min(250000, (b-a)//4)
            covered = bool(pts) and min(pts) <= a+margin and max(pts) >= b-margin-1 and any(abs(t-(a+b)//2) <= margin for t in pts)
        if not covered:
            errors.append(unit['id'] + ': evidence modality/coverage insufficient')
    technical = {}
    for row in review.get('technical', []):
        if row.get('kind') not in plan['technical_required'] or row['kind'] in technical:
            raise ValueError('Unknown/duplicate technical check')
        technical[row['kind']] = row
    for kind in plan['technical_required']:
        row = technical.get(kind, {})
        if row.get('status') == 'fail':
            errors.append('technical:' + kind); continue
        if row.get('status') != 'pass' or not str(row.get('finding', '')).strip() or not usable(row.get('evidence_ids', []), [kind]):
            missing.append('technical:' + kind)
    issue_ids = set()
    for issue in review.get('issues', []):
        if not issue.get('id') or issue['id'] in issue_ids or issue.get('severity') not in ('blocker', 'major', 'minor'):
            raise ValueError('Invalid issue identity/severity')
        issue_ids.add(issue['id']); interval(issue['range_us'], duration)
        if not str(issue.get('finding', '')).strip():
            raise ValueError('Issue must describe actual finding')
        state = issue.get('state', 'open')
        if state == 'resolved':
            refs = issue.get('recheck_units', [])
            if not refs or any(records.get(i, {}).get('status') != 'pass' for i in refs):
                errors.append(issue['id'] + ': repair lacks passing recheck')
            required_kinds = {'audio': {'listening'}, 'sync': {'synchronization'},
                              'visual': {'visual_playback'}, 'overlay': {'overlay', 'visual_playback'},
                              'narrative': {'chapter_narrative', 'global_narrative'}}.get(issue.get('category'), set())
            spans = [u['range_us'] for u in plan['units'] if u['id'] in refs and u['kind'] in required_kinds]
            if not covers(spans, *issue['range_us']):
                errors.append(issue['id'] + ': recheck does not cover defect time and category')
        elif state == 'accepted' and issue['severity'] == 'minor' and str(issue.get('reason', '')).strip():
            pass
        elif state not in ('open', 'accepted'):
            raise ValueError('Unknown issue state')
        elif issue['severity'] in ('blocker', 'major'):
            errors.append(issue['id'] + ': unresolved ' + issue['severity'])
        else:
            missing.append(issue['id'] + ': minor issue needs documented disposition')
    if not plan['chapters_semantically_mapped']:
        missing.append('semantic chapter map')
    elif any(re.fullmatch(r'(?:(?:第.{1,20}章|chapter\s+\d+)\s*[:：-]\s*)?(?:待分析|待核实|未分析|pending|todo|tbd)[.。… ]*',
                          c['summary'].strip(), flags=re.I) for c in plan['chapters']):
        missing.append('semantic chapter map still contains unfinished summaries')
    if plan['unresolved_asset_paths']:
        missing.append('unresolved asset references')
    status = 'fail' if errors else 'partial' if missing else 'pass'
    result = {'schema': RESULT, 'status': status, 'scope': plan['scope'], 'plan': binding(plan_path),
              'review': binding(review_path), 'draft': plan['draft'], 'errors': errors,
              'unverified': missing, 'required_units': len(plan['units']),
              'accepted_units': sum(r.get('status') == 'pass' for r in records.values()) if not errors else None,
              'semantic_truth_verified_by_tool': False, 'generalization_to_other_materials': False,
              'exported_file_verified': False}
    if output:
        write(output, result)
    return result


def verify_certificate(path):
    saved = read(path)
    if saved.get('schema') != RESULT:
        raise ValueError('Quality result certificate required')
    verify(saved['plan']); verify(saved['review'])
    current = validate(saved['plan']['path'], saved['review']['path'])
    if current != saved or current['status'] != 'pass':
        raise ValueError('Current final draft has not passed quality acceptance')
    return current


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    for k in ('install-dir', 'draft', 'out'): p.add_argument('--'+k, required=True)
    p.add_argument('--chapters'); p.add_argument('--window-us', type=int, default=60000000)
    p.add_argument('--overlap-us', type=int, default=3000000); p.add_argument('--frame-step-us', type=int)
    p.add_argument('--silent-film', action='store_true', help='Only for an intentionally silent delivery, not to bypass audio review')
    p = sub.add_parser('capture')
    for k in ('plan', 'install-dir', 'out'): p.add_argument('--'+k, required=True)
    p.add_argument('--width', type=int, default=640)
    p = sub.add_parser('validate')
    for k in ('plan', 'review', 'out'): p.add_argument('--'+k, required=True)
    a = parser.parse_args()
    try:
        if a.command == 'prepare':
            result = prepare(a.install_dir, a.draft, a.out, read(a.chapters) if a.chapters else None,
                             a.window_us, a.overlap_us, a.frame_step_us, not a.silent_film)
        elif a.command == 'capture':
            result = capture(a.plan, a.install_dir, a.out, a.width)
            result = {'status': result['status'], 'frames': len(result['frames'])}
        else:
            result = validate(a.plan, a.review, a.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get('status') == 'fail' else 0
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
