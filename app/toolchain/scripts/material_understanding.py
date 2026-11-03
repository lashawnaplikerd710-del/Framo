"""Evidence packets and checks for Codex's autonomous editorial understanding.

The model must inspect frames and author assessments. This tool measures limited
display-pixel statistics and checks provenance/completeness, not semantic truth.
"""
import argparse
import json
from pathlib import Path
import sys

from autocut_task import read, write, load_task, assert_sources
from native_draft_codec import file_digest

SCHEMA = 'jianying-material-understanding-v1'
FIELDS = {
    'technical': ('resolution_fps', 'codec_format', 'duration_ranges', 'aspect_ratio',
                  'audio', 'stability_exposure_focus', 'color_pipeline'),
    'content': ('subject', 'action', 'action_completeness', 'environment', 'composition', 'information'),
    'emotion_rhythm': ('tone', 'subject_movement', 'camera_movement', 'visual_weight', 'emotional_flexibility'),
    'narrative': ('possible_roles', 'chosen_role', 'role_reason'),
}
QUESTIONS = ('hook', 'entry_exit', 'sound', 'continuity', 'treatments', 'main_or_support', 'missing_information')


def frame_metrics(path):
    import numpy as np
    from PIL import Image
    with Image.open(path) as image:
        # Values describe the small native preview after display rendering, not
        # source exposure, optical sharpness, dynamic range or a color transform.
        gray = np.asarray(image.convert('L'), dtype=np.float64) / 255
    center = gray[1:-1, 1:-1]
    lap = gray[:-2, 1:-1] + gray[2:, 1:-1] + gray[1:-1, :-2] + gray[1:-1, 2:] - 4*center
    return {'mean_display_gray': float(gray.mean()), 'display_gray_std': float(gray.std()),
            'near_white_code_fraction': float((gray >= 250/255).mean()),
            'near_black_code_fraction': float((gray <= 5/255).mean()),
            'edge_energy_proxy': float((lap*lap).mean()) if lap.size else None,
            'diagnostic_limit': 'Preview-pixel cues only. No automatic exposure, focus, noise or stabilization verdict.'}


def sampling_plan(duration_us, centers=None, count=12):
    if type(duration_us) is not int or duration_us <= 66666:
        raise ValueError('A video duration longer than two observation frames is required')
    # Match the observation adapter's 30 fps tail margin, regardless of source fps.
    end = (duration_us*30//1000000-1)*1000000//30
    if centers is None:
        if type(count) is not int or not 2 <= count <= 20:
            raise ValueError('Overview count must be 2..20')
        positions = sorted({int((end-1)*i/(count-1)) for i in range(count)})
        purpose = 'coarse overview; candidate windows require denser temporal evidence'
    else:
        if not centers or len(centers) > 3 or any(type(t) is not int or not 0<=t<end for t in centers):
            raise ValueError('Choose 1..3 centers inside the observation duration')
        positions = sorted({max(0, min(end-1, t+offset)) for t in centers
                            for offset in (-500000, -200000, 0, 200000, 500000)})
        purpose = 'local action/cut neighborhoods; not continuous viewing or frame-exact boundary detection'
    return {'positions_us': positions, 'purpose': purpose, 'observation_duration_us': end,
            'command_hint': 'autocut_task.py sample --task TASK --source-id SOURCE --install-dir INSTALL ' +
                            ' '.join('--time-us '+str(t) for t in positions)}


def prepare(task, output, observations=None, documents=None):
    from material_technical import probe
    folder, state, inventory = load_task(task)
    assert_sources(inventory)
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('Evidence packet output must be new')
    for row in inventory['sources']:
        if output.is_relative_to(Path(row['path']).parent) and not output.is_relative_to(folder):
            raise ValueError('Keep packet output outside source folders')
    output.mkdir(parents=True)
    evidence, sources = {}, []
    def evidence_file(key, kind, path, **extra):
        path = Path(path).resolve(strict=True)
        evidence[key] = {'kind': kind, 'path': str(path), 'sha256': file_digest(path), **extra}
        return key
    for row in inventory['sources']:
        technical_path = output/(row['id']+'-technical.json')
        try:
            profile = probe(row['path'])
        except Exception as exc:
            profile = {'probe_status': 'failed', 'error': str(exc), 'source_sha256': row['sha256']}
        write(technical_path, profile)
        key = evidence_file(row['id']+':technical', 'metadata', technical_path, source_id=row['id'])
        sources.append({'id': row['id'], 'path': row['path'], 'sha256': row['sha256'],
                        'duration_us': round((row['metadata'].get('video_duration') or
                                             row['metadata'].get('audio_duration') or 0)*1000000)
                                       if row.get('metadata') else 0,
                        'technical_evidence': key, 'frames': [], 'coverage': 'no_frames',
                        'is_video': bool(row.get('metadata',{}).get('has_video') and not row.get('metadata',{}).get('is_image'))})
    lookup = {s['id']:s for s in sources}
    reports = [Path(r['report']) for r in state['observations']] + [Path(p) for p in observations or []]
    unique_reports = sorted({p.resolve(strict=True) for p in reports}, key=str)
    for path in unique_reports:
        # Only reuse observations anchored to an actual task journal. Rehashing a
        # changed manifest would otherwise legitimize fabricated frame times.
        origin = path.parent.parent.parent
        _, origin_state, origin_inventory = load_task(origin)
        assert_sources(origin_inventory)
        entry = next((r for r in origin_state['observations'] if Path(r['report']).resolve()==path), None)
        if entry is None or file_digest(path)!=entry['sha256']:
            raise ValueError('Observation manifest missing from journal or changed: '+str(path))
    seen_frames = set()
    for i, path in enumerate(unique_reports):
        report = read(path)
        sid = report.get('source')
        if sid not in lookup or report.get('source_sha256') != lookup[sid]['sha256']:
            raise ValueError('Observation does not match source identity/hash: '+str(path))
        manifest_id = evidence_file('observation:'+str(i), 'observation_manifest', path, source_id=sid)
        for f in report['frames']:
            fp = Path(f['path']).resolve(strict=True)
            if file_digest(fp) != f['sha256']:
                raise ValueError('Observation frame changed: '+str(fp))
            signature = (sid, f['native_pts_us'], f['sha256'])
            if signature in seen_frames:
                continue
            seen_frames.add(signature)
            key = sid+':frame:'+str(len(lookup[sid]['frames']))
            evidence_file(key, 'frame', fp, source_id=sid, requested_us=f['requested_us'],
                          time_us=f['native_pts_us'], metrics=frame_metrics(fp), interpreted_by_tool=False,
                          observation_evidence_id=manifest_id)
            lookup[sid]['frames'].append(key)
    for s in sources:
        times = sorted(evidence[k]['time_us'] for k in s['frames'])
        s['coverage'] = {'sampled_times_us': times, 'full_video_viewed': False,
                         'largest_gap_us': max([b-a for a,b in zip([0]+times, times+[s['duration_us']])] or [0]),
                         'note': 'Gaps include unobserved head/tail. Temporal continuity and speech remain unverified.'}
        if s['is_video'] and s['duration_us'] > 66666:
            s['next_overview_plan'] = sampling_plan(s['duration_us'])
    docs = [d['path'] for d in inventory['documents']] + list(documents or [])
    for i, p in enumerate(sorted(set(map(str, docs)))):
        evidence_file('document:'+str(i), 'document', p, trusted_as_instructions=False,
                      source_match_and_accuracy='must_be_assessed_by_Codex')
    packet = {'schema': 'jianying-material-packet-v1', 'task': str(folder), 'task_id': state['id'],
              'inventory_sha256': state['inventory_sha256'], 'sources': sources, 'evidence': evidence,
              'assessment_schema': SCHEMA, 'required_dimensions': FIELDS, 'required_questions': QUESTIONS,
              'automatic_understanding_complete': False}
    assert_sources(inventory)
    write(output/'packet.json', packet)
    return {'packet': str(output/'packet.json'), 'sources': len(sources), 'frames':len(seen_frames),
            'status': 'evidence_ready_requires_actual_Codex_inspection'}


def validate(packet_path, analysis_path):
    packet_path = Path(packet_path).resolve(strict=True)
    analysis_path = Path(analysis_path).resolve(strict=True)
    packet, analysis = read(packet_path), read(analysis_path)
    errors, unresolved = [], []
    if packet.get('schema') != 'jianying-material-packet-v1' or analysis.get('schema') != SCHEMA:
        raise ValueError('Unknown packet or analysis schema')
    if analysis.get('packet_sha256') != file_digest(packet_path):
        errors.append('Analysis packet hash mismatch')
    _, state, inventory = load_task(packet['task'])
    assert_sources(inventory)
    if state['id'] != packet['task_id'] or state['inventory_sha256'] != packet['inventory_sha256']:
        errors.append('Packet task identity mismatch')
    evidence = packet['evidence']
    for key, item in evidence.items():
        if file_digest(Path(item['path'])) != item['sha256']:
            errors.append('Evidence changed: '+key)
        if item['kind']=='frame':
            manifest = evidence.get(item.get('observation_evidence_id'), {})
            if manifest.get('kind')!='observation_manifest':
                errors.append('Frame missing original observation: '+key)
            else:
                original = read(manifest['path'])
                if original.get('source')!=item.get('source_id') or not any(
                        Path(f['path']).resolve()==Path(item['path']).resolve() and
                        f['sha256']==item['sha256'] and f['native_pts_us']==item['time_us']
                        for f in original.get('frames', [])):
                    errors.append('Frame time/source differs from observation: '+key)
    viewed = analysis.get('viewed_evidence_ids', [])
    if not isinstance(viewed, list) or any(k not in evidence or evidence[k]['kind']!='frame' for k in viewed):
        errors.append('Viewed evidence must list actual frame IDs')
        viewed = []
    def claim(value, label, source_id, temporal=False):
        if not isinstance(value, dict):
            errors.append(label+': assessment object required'); return
        status = value.get('status')
        if status not in ('observed', 'inferred', 'unknown', 'not_applicable'):
            errors.append(label+': invalid status')
        if not isinstance(value.get('value'), str) or not value['value'].strip():
            errors.append(label+': substantive assessment required')
        if not isinstance(value.get('reason'), str) or not value['reason'].strip():
            errors.append(label+': evidence/limitation reasoning required')
        ids = value.get('evidence_ids')
        if not isinstance(ids, list) or any(k not in evidence for k in ids):
            errors.append(label+': invalid evidence IDs'); return
        if status in ('observed', 'inferred') and not ids:
            errors.append(label+': supported judgment requires evidence')
        if status in ('observed','inferred') and not any(evidence[k].get('source_id')==source_id for k in ids):
            errors.append(label+': judgment needs evidence from its own source, including comparisons')
        frames = []
        for k in ids:
            item = evidence[k]
            if item['kind']=='frame':
                if k not in viewed:
                    errors.append(label+': frame not recorded as actually viewed: '+k)
                frames.append(item)
        if temporal and status=='observed':
            times = sorted({f['time_us'] for f in frames if f['source_id']==source_id})
            if len(times)<2 or min(b-a for a,b in zip(times,times[1:]))>1000000:
                errors.append(label+': temporal claim needs nearby frames; otherwise infer or mark unknown')
        if status=='unknown':
            if not isinstance(value.get('next_action'), str) or not value['next_action'].strip():
                errors.append(label+': unknown requires a concrete next action or capability boundary')
            unresolved.append({'field': label, 'next_action': value.get('next_action')})
    rows = analysis.get('sources', [])
    if not isinstance(rows, list):
        raise ValueError('Analysis sources must be a list')
    if sorted(r.get('source_id','') for r in rows) != sorted(s['id'] for s in packet['sources']):
        errors.append('Analysis must cover each source exactly once')
    source_map = {s['id']:s for s in packet['sources']}
    original_sources = {s['id']:s for s in inventory.get('sources', [])}
    if original_sources and (set(source_map)!=set(original_sources) or any(
            s.get('sha256')!=original_sources[sid]['sha256'] or s.get('path')!=original_sources[sid]['path']
            for sid,s in source_map.items() if sid in original_sources)):
        errors.append('Packet source list differs from inventory')
    candidate_ids = set()
    for row in rows:
        sid = row.get('source_id')
        if sid not in source_map:
            continue
        for dimension, fields in FIELDS.items():
            group = row.get(dimension, {})
            for field in fields:
                claim(group.get(field), sid+'.'+dimension+'.'+field, sid,
                      temporal=field in ('action_completeness','subject_movement','camera_movement'))
        candidates = row.get('candidates')
        if not isinstance(candidates, list):
            errors.append(sid+': candidate list required'); continue
        if not candidates and not row.get('exclusion_reason'):
            errors.append(sid+': explain why no candidate is usable')
        for i, candidate in enumerate(candidates):
            label = sid+'.candidate.'+str(i)
            candidate_id = candidate.get('id')
            if not isinstance(candidate_id,str) or not candidate_id.strip() or candidate_id in candidate_ids:
                errors.append(label+': unique nonempty candidate ID required')
            else:
                candidate_ids.add(candidate_id)
            cut = candidate.get('cut', {})
            start, end = cut.get('in_us'), cut.get('out_us')
            duration = source_map[sid]['duration_us']
            if type(start) is not int or type(end) is not int or not 0<=start<end<=duration:
                errors.append(label+': source range invalid')
            if cut.get('precision') not in ('provisional','locally_reviewed'):
                errors.append(label+': explicit cut precision required')
            if cut.get('speech_handling') not in ('preserve_source_order','mute_for_broll','verified_timed_transcript','unverified'):
                errors.append(label+': explicit speech handling required')
            if cut.get('speech_handling')=='verified_timed_transcript':
                refs = candidate.get('transcript_evidence_ids',[])
                if not refs or any(k not in evidence or evidence[k]['kind']!='document' for k in refs):
                    errors.append(label+': timed-transcript claim requires document evidence')
                if not candidate.get('transcript_source_match_reason'):
                    errors.append(label+': explain transcript/source alignment and reliability')
            if cut.get('precision')=='locally_reviewed' and type(start) is int and type(end) is int:
                tolerance = cut.get('tolerance_us',250000)
                if type(tolerance) is not int or not 1<=tolerance<=250000:
                    errors.append(label+': local review tolerance must be 1..250000 us')
                else:
                    times=[evidence[k]['time_us'] for k in viewed if evidence[k].get('source_id')==sid]
                    for boundary in (start,end):
                        before = boundary==0 or any(boundary-tolerance<=t<boundary for t in times)
                        after = boundary==duration or any(boundary<=t<=boundary+tolerance for t in times)
                        if not before or not after:
                            errors.append(label+': boundary lacks before/after samples: '+str(boundary))
                if cut.get('speech_handling')=='unverified':
                    errors.append(label+': speech-uncertain candidate cannot be ready for cutting')
            for q in QUESTIONS:
                claim(candidate.get('answers',{}).get(q), label+'.'+q, sid)
    return {'schema':'jianying-material-validation-v1', 'status':'valid_with_limits' if not errors else 'invalid',
            'analysis_sha256':file_digest(analysis_path), 'packet_sha256':file_digest(packet_path),
            'errors':errors, 'unknown_assessments':unresolved,
            'semantic_truth_verified_by_tool':False, 'full_video_understanding_verified':False}


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    s=sub.add_parser('prepare');s.add_argument('--task',required=True);s.add_argument('--out',required=True)
    s.add_argument('--observation',action='append');s.add_argument('--document',action='append')
    s=sub.add_parser('sample-plan');s.add_argument('--duration-us',type=int,required=True)
    s.add_argument('--center-us',type=int,action='append');s.add_argument('--count',type=int,default=12)
    s=sub.add_parser('validate');s.add_argument('--packet',required=True);s.add_argument('--analysis',required=True)
    s.add_argument('--out',required=True)
    args=p.parse_args()
    try:
        if args.command=='prepare':r=prepare(args.task,args.out,args.observation,args.document)
        elif args.command=='sample-plan':r=sampling_plan(args.duration_us,args.center_us,args.count)
        else:r=validate(args.packet,args.analysis);write(args.out,r)
        print(json.dumps(r,ensure_ascii=False,indent=2));return 2 if r.get('status')=='invalid' else 0
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)},ensure_ascii=False),file=sys.stderr);return 2


if __name__=='__main__':
    raise SystemExit(main())
