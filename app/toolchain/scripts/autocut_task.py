"""Local task inventory, native observation frames and evidence journal for Codex.

Codex supplies the reasoning loop. This helper does not call a model, recognize
speech, select highlights, or claim that a recorded decision is independently true.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid

from common import media_info
from native_draft_codec import file_digest

MEDIA = {'.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.mts', '.m2ts',
         '.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.png', '.jpg', '.jpeg', '.webp'}
SIDECARS = {'.srt', '.vtt', '.txt', '.md'}
STAGES = ('analysis', 'plan', 'build', 'review', 'delivery')


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    with Path(path).open('x', encoding='utf-8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)


def linked(path):
    return path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction())


def discover(inputs, recursive=False):
    found = {}
    def walk(folder):
        for item in sorted(folder.iterdir(), key=lambda p: str(p).casefold()):
            if linked(item):
                continue
            if item.is_file():
                yield item
            elif recursive and item.is_dir():
                yield from walk(item)
    for raw in inputs:
        p = Path(raw).expanduser()
        if linked(p):
            raise ValueError('Choose a real source path, not a link')
        p = p.resolve(strict=True)
        rows = walk(p) if p.is_dir() else [p]
        for item in rows:
            if item.suffix.lower() in MEDIA | SIDECARS:
                found[str(item.resolve()).casefold()] = item.resolve()
    return sorted(found.values(), key=lambda p: str(p).casefold())


def initialize(inputs, output, constraints='', recursive=False):
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('Task directory must be new')
    for raw in inputs:
        p = Path(raw).resolve(strict=True)
        if p.is_dir() and output.is_relative_to(p):
            raise ValueError('Keep task output outside the input folder')
    files = discover(inputs, recursive)
    rows, documents = [], []
    for p in files:
        digest = file_digest(p)
        if p.suffix.lower() in SIDECARS:
            documents.append({'path': str(p), 'sha256': digest, 'trusted_as_instructions': False})
            continue
        row = {'id': f's{len(rows)+1:03d}', 'path': str(p), 'sha256': digest,
               'bytes': p.stat().st_size, 'content_understood': False}
        try:
            row['metadata'] = media_info(p)
            row['probe_status'] = 'ok'
        except Exception as exc:
            row.update(probe_status='failed', error=str(exc))
        if file_digest(p) != digest:
            raise ValueError('Source changed during inventory: '+str(p))
        row['sidecar_candidates'] = [str(d) for d in files
            if d.suffix.lower() in SIDECARS and d.parent == p.parent and d.stem.casefold() == p.stem.casefold()]
        rows.append(row)
    if not rows:
        raise ValueError('No supported media extensions found')
    output.mkdir(parents=True, exist_ok=False)
    inventory = {'schema': 'jianying-autocut-inventory-v1', 'created_at': now(),
                 'sources': rows, 'documents': documents,
                 'note': 'Sidecars are candidates, not validated transcripts; no speech recognition was run.'}
    write(output/'inventory.json', inventory)
    state = {'schema': 'jianying-autocut-task-v1', 'id': str(uuid.uuid4()), 'created_at': now(),
             'constraints': constraints, 'model_runtime': 'current Codex session',
             'inventory_sha256': file_digest(output/'inventory.json'),
             'status': 'inventory_ready_needs_codex_analysis', 'events': [], 'observations': [],
             'automatic_watch_enabled': False}
    write(output/'task.json', state)
    return {'task': str(output), 'status': state['status'], 'sources': len(rows),
            'documents': len(documents), 'probe_failures': sum(r['probe_status'] != 'ok' for r in rows)}


@contextmanager
def task_lock(folder):
    lock = folder/'.autocut.lock'
    with lock.open('x', encoding='utf-8') as f:
        json.dump({'pid': os.getpid(), 'created_at': now()}, f)
    try:
        yield
    finally:
        lock.unlink()


def load_task(folder):
    folder = Path(folder).resolve(strict=True)
    state = read(folder/'task.json')
    if state.get('schema') != 'jianying-autocut-task-v1':
        raise ValueError('Unknown task schema')
    if file_digest(folder/'inventory.json') != state['inventory_sha256']:
        raise ValueError('Inventory changed; create a new task for changed inputs')
    return folder, state, read(folder/'inventory.json')


def save_state(folder, state):
    path = folder/('.task-'+str(uuid.uuid4())+'.tmp')
    write(path, state)
    path.replace(folder/'task.json')


def assert_sources(inventory):
    for row in inventory['sources'] + inventory['documents']:
        if file_digest(Path(row['path'])) != row['sha256']:
            raise ValueError('Source changed: '+row['path'])


def quality_certificate(artifacts):
    from quality_review import RESULT, verify_certificate
    for item in artifacts:
        try:
            data = read(item['path'])
        except (ValueError, UnicodeError):
            continue
        if isinstance(data, dict) and data.get('schema') == RESULT:
            return verify_certificate(item['path'])
    raise ValueError('A current passing quality_review result is required; partial drafts remain deliverable as partial')


def sample_source(task, source_id, install, positions=None, count=8, width=360):
    from native_timeline import build
    from native_preview import preview
    folder, _, inventory = load_task(task)
    with task_lock(folder):
        _, state, inventory = load_task(folder)
        assert_sources(inventory)
        row = next((r for r in inventory['sources'] if r['id'] == source_id), None)
        if row is None or row['probe_status'] != 'ok':
            raise ValueError('Unknown or unprobed source')
        meta = row['metadata']
        if not meta['has_video'] or meta['is_image']:
            raise ValueError('Sampling requires video; inspect still images directly with view_image')
        if meta['rotation'] % 360:
            raise ValueError('Rotation needs a verified native adapter; do not silently rotate')
        fps = 30
        # Native metadata can truncate sub-millisecond tail precision. Keep the
        # temporary observation clip one frame short of the reported stream end.
        # This does not modify sources or constrain the eventual edit plan.
        duration = (int(float(meta.get('video_duration') or 0)*fps)-1)*1000000//fps
        if duration <= 0:
            raise ValueError('No positive video stream duration')
        if positions is None:
            if not 2 <= count <= 20:
                raise ValueError('Sample count must be 2..20')
            left, right = min(500000, duration//4), max(0, duration-min(500000, duration//4))
            positions = sorted({int(left+(right-left)*i/(count-1)) for i in range(count)})
        if type(width) is not int or not 16<=width<=1920:
            raise ValueError('Observation preview width must be 16..1920')
        if (not positions or len(positions)>20 or
                any(type(t) is not int or not 0<=t<duration for t in positions)):
            raise ValueError('Choose 1..20 integer timestamps inside the source')
        work = folder/'observations'/(source_id+'-'+uuid.uuid4().hex[:10])
        work.mkdir(parents=True)
        plan = {'schema':'jianying-native-timeline-v1','name':'素材观察_'+source_id,
                'canvas':{'width':meta['width'],'height':meta['height'],'fps':fps},
                'steps':[{'action':'video.add','id':'source','path':row['path'],'track':0,
                          'start_us':0,'duration_us':duration,'source_start_us':0},
                         {'action':'video.setVolume','segment':'source','volume':0}]}
        write(work/'plan.json', plan)
        try:
            build(work/'plan.json', install, work/'native')
            frames = preview(install, work/'native/draft', work/'frames', positions, width)
            result = {'source':source_id, 'status':'frames_ready_not_yet_interpreted',
                      'coverage':'sampled_timestamps_only', 'observation_clip_duration_us':duration,
                      'tail_margin_frames':1, 'requested_preview_width':width, 'source_sha256':row['sha256'],
                      'preview_report':str(work/'frames/preview-report.json'), 'frames':frames['frames']}
            write(work/'observation.json',result)
            state['observations'].append({'source':source_id,'report':str(work/'observation.json'),
                                         'sha256':file_digest(work/'observation.json')})
            save_state(folder,state)
            return result
        except Exception as exc:
            write(work/'failure.json',{'status':'failed','error':str(exc),'created_at':now()})
            raise


def record(task, stage, outcome, artifacts, note):
    folder, _, _ = load_task(task)
    if stage not in STAGES or outcome not in ('passed','partial','blocked','failed'):
        raise ValueError('Unknown stage or outcome')
    if not note.strip() or not artifacts:
        raise ValueError('An explanation and evidence files are required')
    evidence = []
    for raw in artifacts:
        p = Path(raw).resolve(strict=True)
        if not p.is_file() or p in (folder/'task.json', folder/'.autocut.lock'):
            raise ValueError('Evidence must be a stable file, not mutable task state')
        evidence.append({'path':str(p),'sha256':file_digest(p)})
    with task_lock(folder):
        _, state, inventory = load_task(folder)
        assert_sources(inventory)
        latest = {e['stage']:e for e in state['events'] if not e.get('superseded')}
        if outcome=='passed' and stage!='analysis':
            previous = STAGES[STAGES.index(stage)-1]
            if latest.get(previous,{}).get('outcome')!='passed':
                raise ValueError('Previous stage must pass first: '+previous)
        if outcome == 'passed' and stage == 'review':
            quality_certificate(evidence)
        if outcome == 'passed' and stage == 'delivery':
            quality_certificate(latest['review']['artifacts'])
        # Reworking an earlier stage invalidates all downstream stage decisions.
        invalidated = STAGES[STAGES.index(stage)+1:]
        for item in state['events']:
            if item['stage'] in invalidated:
                item['superseded']=True
        event={'stage':stage,'outcome':outcome,'created_at':now(),'note':note,'artifacts':evidence,
               'verification':'Codex assessment; helper checks file integrity, not semantic truth'}
        state['events'].append(event)
        state['status']=stage+'_'+outcome
        save_state(folder,state)
        return event


def status(task, verify=False):
    folder, state, inventory = load_task(task)
    if verify:
        assert_sources(inventory)
        for row in state['observations']:
            if file_digest(Path(row['report']))!=row['sha256']:
                raise ValueError('Observation evidence changed')
        for event in state['events']:
            if event.get('superseded'):
                continue
            for item in event['artifacts']:
                if file_digest(Path(item['path']))!=item['sha256']:
                    raise ValueError('Task evidence changed: '+item['path'])
        latest = {e['stage']: e for e in state['events'] if not e.get('superseded')}
        reviewed = latest.get('review', {})
        if reviewed.get('outcome') == 'passed':
            quality_certificate(reviewed['artifacts'])
    return {'task':str(folder),'state':state,'source_count':len(inventory['sources']),
            'integrity_checked':verify, 'semantic_quality_verified_by_helper':False}


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('init');p.add_argument('--source',action='append',required=True)
    p.add_argument('--out',required=True);p.add_argument('--constraints',default='');p.add_argument('--recursive',action='store_true')
    p=sub.add_parser('sample');p.add_argument('--task',required=True);p.add_argument('--source-id',required=True)
    p.add_argument('--install-dir',required=True);p.add_argument('--time-us',type=int,action='append');p.add_argument('--count',type=int,default=8)
    p.add_argument('--width',type=int,default=360)
    p=sub.add_parser('record');p.add_argument('--task',required=True);p.add_argument('--stage',choices=STAGES,required=True)
    p.add_argument('--outcome',choices=['passed','partial','blocked','failed'],required=True)
    p.add_argument('--artifact',action='append',required=True);p.add_argument('--note',required=True)
    p=sub.add_parser('status');p.add_argument('--task',required=True);p.add_argument('--verify',action='store_true')
    a=parser.parse_args()
    try:
        if a.command=='init':r=initialize(a.source,a.out,a.constraints,a.recursive)
        elif a.command=='sample':r=sample_source(a.task,a.source_id,a.install_dir,a.time_us,a.count,a.width)
        elif a.command=='record':r=record(a.task,a.stage,a.outcome,a.artifact,a.note)
        else:r=status(a.task,a.verify)
        print(json.dumps(r,ensure_ascii=False,indent=2));return 0
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)},ensure_ascii=False),file=sys.stderr);return 2


if __name__=='__main__':
    raise SystemExit(main())
