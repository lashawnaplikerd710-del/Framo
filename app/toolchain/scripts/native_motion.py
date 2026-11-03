"""Apply verified motion curves to video segments in a new native draft copy."""
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

from native_build import dump, sidecar_metadata
from native_draft_codec import file_digest
from native_keyframes import MOTION_PROPERTIES, add_point, validate_added_point, validate_value
from native_preview import preview
from native_register import rebase
from native_service import Service, NativeServiceError, validate_player_state


def checked_request(data, snapshot):
    if (not isinstance(data, dict) or set(data) != {'schema', 'curves'} or
            data['schema'] != 'jianying-native-motion-v1' or
            not isinstance(data['curves'], list) or not 1 <= len(data['curves']) <= 100):
        raise ValueError('Expected jianying-native-motion-v1 with 1..100 curves')
    video_ids = [s['id'] for t in snapshot.get('tracks', []) if t.get('type') == 'video'
                 for s in t.get('segments', [])]
    if len(video_ids) != len(set(video_ids)):
        raise ValueError('Ambiguous video segment IDs')
    segments = {s['id']: s for t in snapshot.get('tracks', []) if t.get('type') == 'video'
                for s in t.get('segments', [])}
    seen = set()
    staged = copy.deepcopy(snapshot)
    for curve in data['curves']:
        if (not isinstance(curve, dict) or set(curve) != {'segment_id', 'property', 'points'} or
                not isinstance(curve['segment_id'], str) or curve['segment_id'] not in segments or
                curve['property'] not in MOTION_PROPERTIES or
                not isinstance(curve['points'], list) or not 2 <= len(curve['points']) <= 100):
            raise ValueError('Each curve needs a video segment ID, verified property and 2..100 points')
        identity = curve['segment_id'], curve['property']
        if identity in seen:
            raise ValueError('Duplicate curve for the same segment/property')
        seen.add(identity)
        duration = segments[curve['segment_id']]['target_timerange']['duration']
        previous = -1
        for point in curve['points']:
            if not isinstance(point, dict) or set(point) != {'time_us', 'value'}:
                raise ValueError('Each point needs only time_us and value')
            validate_value(curve['property'], point['time_us'], point['value'], duration)
            if point['time_us'] <= previous:
                raise ValueError('Curve points must have strictly increasing times')
            previous = point['time_us']
            staged = add_point(staged, curve['segment_id'], curve['property'], point['time_us'], point['value'])
    return data


def apply_motion(install, source, request_path, output):
    install, source = Path(install).resolve(strict=True), Path(source).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or output.is_relative_to(install):
        raise ValueError('Choose a new output outside source and installation')
    request = json.loads(Path(request_path).read_text(encoding='utf-8-sig'))
    content = source/'draft_content.json'
    before_hash = file_digest(content)
    output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'in_progress', 'gui_opened': False, 'ffmpeg_invoked': False,
              'native_action': 'draft.setDraft', 'homepage_registration': 'not_performed',
              'keyframe_time_mapping': 'source_start + target_relative_time * constant_speed',
              'complete_editorial_acceptance': False, 'catalog_resources_added': False}
    try:
        draft = output/'draft'; draft.mkdir()
        with Service(install, output/'service') as service:
            snap = json.loads(service.request('draft', 'readFile',
                {'path': str(content), 'rootPath': str(source)})['result']['content'])
            checked_request(request, snap)
            # Copy media beside the source and retain absolute external references.
            # Homepage publication separately owns media rebasing/registration.
            skip = {'draft_content.json', 'draft_content.json.bak', 'draft_meta_info.json',
                    'draft_meta_info.json.bak', 'template-2.tmp'}
            for path in source.iterdir():
                if path.name in skip:
                    continue
                if path.is_symlink():
                    raise ValueError('Source draft symlinks are not supported')
                if path.is_dir():
                    shutil.copytree(path, draft/path.name)
                else:
                    shutil.copyfile(path, draft/path.name)
            snap = rebase(snap, source, draft, snap['id'], str(uuid.uuid4()).upper())
            def write(path, value):
                if path.exists():
                    raise FileExistsError(path)
                service.request('draft', 'writeFile', {'path': str(path), 'rootPath': str(output),
                    'content': json.dumps(value, ensure_ascii=False, allow_nan=False)})
                saved = service.request('draft', 'readFile', {'path': str(path), 'rootPath': str(output)})['result']
                if not saved.get('encrypted') or json.loads(saved['content']) != value:
                    raise NativeServiceError('Native encrypted save/readback mismatch')
            seed=output/'seed.json'; write(seed, snap)
            session=service.request('player','create',{'kind':'draft','serviceGeneration':1})['result']['sessionId']
            times = set()
            try:
                service.request('player','load',{'draftPath':str(seed),'workspacePath':str(output)},session)
                expected = copy.deepcopy(snap)
                for material in expected.get('materials', {}).get('sound_channel_mappings', []):
                    if material.get('type') == '':
                        material['type'] = 'none'
                prepared = service.request('edit','draft.setDraft',{'draftSnapshot':snap},session)['result']
                if prepared.get('accepted') is not True or prepared.get('draftSnapshot') != expected:
                    raise NativeServiceError('Unexpected native preparation changes')
                snap = prepared['draftSnapshot']
                for curve in request['curves']:
                    segment = next(s for t in snap['tracks'] for s in t['segments'] if s['id']==curve['segment_id'])
                    span = segment['target_timerange']
                    for fraction in (.2, .5, .8):
                        times.add(span['start'] + min(span['duration'] - 1, int(span['duration'] * fraction)))
                    for point in curve['points']:
                        changed=add_point(snap,curve['segment_id'],curve['property'],point['time_us'],point['value'])
                        result=service.request('edit','draft.setDraft',{'draftSnapshot':changed},session)['result']
                        if result.get('accepted') is not True or result.get('draftSnapshot') != changed:
                            raise NativeServiceError('Native service did not preserve the requested curve')
                        validate_added_point(snap,result['draftSnapshot'],curve['segment_id'],curve['property'],
                                             point['time_us'],point['value'])
                        snap=result['draftSnapshot']
                write(draft/'draft_content.json',snap)
                write(draft/'draft_meta_info.json',sidecar_metadata(snap,draft,output,time.time_ns()//1000))
                validate_player_state(service.request('player','getState',{},session)['result'],
                                      snap['duration'],snap['canvas_config'])
            finally:
                service.request('player','destroy',{},session)
        dump(output/'draft-snapshot.json',snap)
        dump(output/'motion-request.json',request)
        # A fresh process cold-loads the saved encrypted draft before rendering.
        frames=preview(install,draft,output/'preview',sorted(times),360)
        if file_digest(content)!=before_hash:
            raise NativeServiceError('Input draft changed during the operation')
        report.update(status='native_motion_created', source_preserved=True, source_sha256=before_hash,
                      curve_count=len(request['curves']), frames=frames['frames'],
                      native_cold_reload_and_frames_verified=True, draft=str(draft),
                      relocation='Use native registration to gather/rebase absolute media references')
        dump(output/'native-motion-report.json',report)
        return report
    except Exception:
        report['status']='failed'
        dump(output/'native-motion-report.json',report)
        raise


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for key in ('install-dir','draft','request','out'):
        parser.add_argument('--'+key,required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(apply_motion(args.install_dir,args.draft,args.request,args.out),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)},ensure_ascii=False));sys.exit(2)
