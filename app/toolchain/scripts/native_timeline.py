"""Build new native video/audio timelines; edits only affect a new output copy.

Verified actions: append video/audio, split plain video, normal video speed,
audio/video volume and audio fades. Native speed edits ripple the same track.
No GUI, export, online assets or arbitrary native operation forwarding.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
import shutil
import sys
import time
import uuid

from common import media_info
from native_build import dump, sidecar_metadata
from native_draft_codec import file_digest
from native_service import Service, NativeServiceError, validate_player_state

MAX_TIME = 86_400_000_000
FIELDS = {
    'video.add': {'action', 'id', 'path', 'track', 'start_us', 'duration_us', 'source_start_us'},
    'audio.add': {'action', 'id', 'path', 'track', 'start_us', 'duration_us', 'source_start_us'},
    'segment.split': {'action', 'segment', 'at_us', 'right_id'},
    'video.setSpeed': {'action', 'segment', 'speed'},
    'video.setVolume': {'action', 'segment', 'volume'},
    'audio.setVolume': {'action', 'segment', 'volume'},
    'video.setFade': {'action', 'segment', 'fade_in_us', 'fade_out_us'},
    'audio.setFade': {'action', 'segment', 'fade_in_us', 'fade_out_us'},
}


def fail(message):
    raise NativeServiceError(message)


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label} must be an integer in [{low}, {high}]')


def number(value, low, high, label):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label} must be a finite number in [{low}, {high}]')


def load_plan(path):
    path = Path(path).resolve(strict=True)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'Duplicate JSON field: {key}')
            result[key] = value
        return result
    def constant(value):
        raise ValueError(f'Non-finite JSON number: {value}')
    plan = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=pairs, parse_constant=constant)
    if not isinstance(plan, dict) or set(plan) != {'schema', 'name', 'canvas', 'steps'}:
        raise ValueError('Plan requires exactly schema, name, canvas, steps')
    if plan['schema'] != 'jianying-native-timeline-v1':
        raise ValueError('Expected schema jianying-native-timeline-v1')
    if not isinstance(plan['name'], str) or not plan['name'].strip():
        raise ValueError('name must be nonempty')
    canvas = plan['canvas']
    if not isinstance(canvas, dict) or set(canvas) != {'width', 'height', 'fps'}:
        raise ValueError('canvas requires exactly width, height, fps')
    for field in canvas:
        integer(canvas[field], 1, 120 if field == 'fps' else 8192, 'canvas.' + field)
    if not isinstance(plan['steps'], list) or not 1 <= len(plan['steps']) <= 500:
        raise ValueError('steps must contain 1..500 operations')
    for step in plan['steps']:
        if not isinstance(step, dict) or not isinstance(step.get('action'), str) or step['action'] not in FIELDS:
            raise ValueError('Unsupported native timeline action')
        if set(step) != FIELDS[step['action']]:
            raise ValueError(f"{step['action']} requires exactly {sorted(FIELDS[step['action']])}")
        if step['action'].endswith('.add'):
            raw = step['path']
            if not isinstance(raw, str) or not raw or '://' in raw or raw.startswith(('\\\\', '//')):
                raise ValueError('Media must be an explicit local file')
            source = Path(raw)
            if not source.is_absolute():
                source = path.parent / source
            source = source.resolve(strict=True)
            if not source.is_file() or str(source).startswith(('\\\\', '//')):
                raise ValueError('Media must resolve to a regular local file')
            info = media_info(source)
            if step['action'] == 'video.add' and (not info['has_video'] or info['is_image']):
                raise ValueError('Timeline video.add currently requires video, not a still image')
            if step['action'] == 'audio.add' and not info['has_audio']:
                raise ValueError('audio.add requires detected audio')
            if not math.isfinite(info['duration']) or info['duration'] <= 0:
                raise ValueError('Source duration must be verifiable with MediaInfo')
            for field in ('start_us', 'duration_us', 'source_start_us'):
                integer(step[field], 1 if field == 'duration_us' else 0, MAX_TIME, field)
            stream_key = 'video_duration' if step['action'] == 'video.add' else 'audio_duration'
            stream_duration = info.get(stream_key) or info['duration']
            if step['source_start_us'] + step['duration_us'] > round(stream_duration * 1_000_000):
                raise ValueError(f'Requested source range exceeds detected {stream_key}; container and stream lengths can differ')
            step['path'] = str(source)
    # Validate all structural and temporal constraints before invoking native code.
    model, tracks = {}, []
    for step in plan['steps']:
        advance(model, tracks, step)
    return plan


def advance(model, tracks, step):
    """Explicit model for the supported native semantics, including ripple speed."""
    action = step['action']
    if action.endswith('.add'):
        alias, track = step['id'], step['track']
        if not isinstance(alias, str) or not alias or alias in model:
            raise ValueError('New segment aliases must be unique nonempty strings')
        integer(track, 0, 100, 'track')
        kind = action.split('.')[0]
        if track > len(tracks):
            raise ValueError('New tracks must be sequential from track 0')
        if track == len(tracks):
            tracks.append({'kind': kind, 'aliases': []})
        target = tracks[track]
        if target['kind'] != kind:
            raise ValueError('Track media kinds cannot be mixed')
        if kind == 'audio' and target['aliases']:
            raise ValueError('This adapter currently requires a new track for each audio.add')
        if target['aliases']:
            previous = model[target['aliases'][-1]]
            if step['start_us'] != previous['start'] + previous['duration']:
                raise ValueError('Existing video tracks only support contiguous appends')
        model[alias] = {'kind': kind, 'track': track, 'path': step['path'],
            'start': step['start_us'], 'duration': step['duration_us'],
            'source_start': step['source_start_us'], 'source_duration': step['duration_us'],
            'speed': 1, 'volume': 1, 'fade': None, 'native_id': None}
        target['aliases'].append(alias)
    else:
        alias = step['segment']
        if not isinstance(alias, str) or alias not in model:
            raise ValueError('segment must name an earlier segment alias')
        item = model[alias]
        if action.startswith(('video.', 'audio.')) and not action.startswith(item['kind'] + '.'):
            raise ValueError('Action and segment media kind differ')
        if action == 'segment.split':
            right = step['right_id']
            if not isinstance(right, str) or not right or right in model:
                raise ValueError('right_id must be a new nonempty alias')
            if item['kind'] != 'video' or item['speed'] != 1 or item['fade'] is not None:
                raise ValueError('Split is limited to video before speed/fade edits')
            integer(step['at_us'], item['start'] + 1, item['start'] + item['duration'] - 1, 'at_us')
            left_duration = step['at_us'] - item['start']
            new = copy.deepcopy(item)
            new.update(start=step['at_us'], duration=item['duration'] - left_duration,
                       source_start=item['source_start'] + left_duration,
                       source_duration=item['source_duration'] - left_duration, native_id=None)
            item.update(duration=left_duration, source_duration=left_duration)
            model[right] = new
            aliases = tracks[item['track']]['aliases']
            aliases.insert(aliases.index(alias) + 1, right)
        elif action == 'video.setSpeed':
            number(step['speed'], 0.1, 10, 'speed')
            if item['fade'] is not None:
                raise ValueError('Set speed before fades')
            duration = item['source_duration'] / step['speed']
            if not duration.is_integer() or duration < 1:
                raise ValueError('This adapter requires a whole-microsecond speed result')
            delta = int(duration) - item['duration']
            item.update(speed=step['speed'], duration=int(duration))
            aliases = tracks[item['track']]['aliases']
            for subsequent in aliases[aliases.index(alias) + 1:]:
                model[subsequent]['start'] += delta
        elif action.endswith('.setVolume'):
            number(step['volume'], 0, 4, 'volume')
            item['volume'] = step['volume']
        elif action.endswith('.setFade'):
            for field in ('fade_in_us', 'fade_out_us'):
                integer(step[field], 0, item['duration'], field)
            if step['fade_in_us'] + step['fade_out_us'] > item['duration']:
                raise ValueError('Fade intervals cannot overlap')
            item['fade'] = (step['fade_in_us'], step['fade_out_us'])
    if any(row['start'] + row['duration'] > MAX_TIME for row in model.values()):
        raise ValueError('Timeline cannot exceed 24 hours')


def all_segments(snapshot):
    return [segment for track in snapshot.get('tracks', []) for segment in track.get('segments', [])]


def by_id(values):
    result = {}
    for value in values:
        identity = value.get('id')
        if not isinstance(identity, str) or not identity or identity.casefold() in result:
            fail('Native object identity is missing or duplicated')
        result[identity.casefold()] = value
    return result


def normalized(value):
    if isinstance(value, dict):
        return {key: normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        items = [normalized(item) for item in value]
        # Native material vectors can reorder while preserving their identities.
        return sorted(items, key=lambda item: item['id']) if items and all(isinstance(item, dict) and 'id' in item for item in items) else items
    return value


def equal(left, right, message):
    if normalized(left) != normalized(right):
        fail(message)


def verify(snapshot, model, tracks, initial):
    for field in ('id', 'name', 'fps'):
        if snapshot.get(field) != initial[field]:
            fail('Native draft identity/name/fps changed')
    for field in ('width', 'height'):
        if snapshot.get('canvas_config', {}).get(field) != initial['canvas_config'][field]:
            fail('Native canvas changed')
    actual_tracks = snapshot.get('tracks', [])
    if len(actual_tracks) != len(tracks):
        fail('Unexpected native track count')
    by_id(all_segments(snapshot))
    materials = snapshot.get('materials', {})
    audio_source_durations = {}
    for index, track in enumerate(tracks):
        native_track = actual_tracks[index]
        if native_track['type'] != track['kind'] or len(native_track['segments']) != len(track['aliases']):
            fail('Native track kind or segment count differs')
        native_segments = by_id(native_track['segments'])
        for alias in track['aliases']:
            expected = model[alias]
            segment = native_segments.get(expected['native_id'].casefold())
            if segment is None:
                fail('Expected native segment missing')
            for field, expected_range in [('target_timerange', {'start': expected['start'], 'duration': expected['duration']}),
                                          ('source_timerange', {'start': expected['source_start'], 'duration': expected['source_duration']})]:
                if segment.get(field) != expected_range:
                    fail(f'{alias}: native {field} differs')
            for field in ('speed', 'volume'):
                if not math.isclose(segment.get(field, -999), expected[field], rel_tol=0, abs_tol=1e-6):
                    fail(f'{alias}: native {field} differs')
            material = by_id(materials.get(expected['kind'] + 's', [])).get(segment['material_id'].casefold())
            if material is None or Path(material.get('path', '')).resolve() != Path(expected['path']).resolve():
                fail(f'{alias}: native media path differs')
            if expected['kind'] == 'audio' and material.get('type') != 'music':
                fail(f'{alias}: native audio material must be music, not an empty/none placeholder')
            if expected['kind'] == 'audio':
                key = str(Path(expected['path']).resolve()).casefold()
                if key not in audio_source_durations:
                    audio_source_durations[key] = round(media_info(expected['path'])['duration'] * 1_000_000)
                if material.get('duration') != audio_source_durations[key]:
                    fail(f'{alias}: native audio material duration is not the complete source duration')
            if expected['fade'] is not None:
                refs = segment.get('extra_material_refs', [])
                fades = [fade for fade in materials.get('audio_fades', []) if fade['id'] in refs]
                if len(fades) != 1 or (fades[0]['fade_in_duration'], fades[0]['fade_out_duration']) != expected['fade']:
                    fail(f'{alias}: native fade material differs')
    expected_duration = max(item['start'] + item['duration'] for item in model.values())
    if snapshot.get('duration') != expected_duration:
        fail('Native overall duration differs')


def verify_change(before, after, action, native_id, old_model, new_model):
    """Compare full snapshots for properties, plus retained objects for adds/split."""
    if not before.get('tracks'):
        return  # First add creates the native default schema; verify() checks fields.
    previous, current = copy.deepcopy(before), copy.deepcopy(after)
    old_segments, new_segments = by_id(all_segments(previous)), by_id(all_segments(current))
    if action.endswith('.add') or action == 'segment.split':
        for identity, old in old_segments.items():
            actual = new_segments.get(identity)
            if actual is None:
                fail('Native add/split removed an earlier segment')
            if action == 'segment.split' and identity == native_id.casefold():
                for field in ('source_timerange', 'target_timerange'):
                    old[field] = actual[field]
            equal(old, actual, 'Native add/split unexpectedly changed an earlier segment')
        for category, old_items in previous.get('materials', {}).items():
            if isinstance(old_items, list) and old_items:
                new_items = by_id(current['materials'].get(category, []))
                for identity, old in by_id(old_items).items():
                    equal(old, new_items.get(identity), 'Native add/split changed an existing material')
        return
    old_segment, new_segment = old_segments[native_id.casefold()], new_segments[native_id.casefold()]
    if action.endswith('.setVolume'):
        old_segment['volume'] = new_segment['volume']
        if action == 'video.setVolume':
            old_segment['last_nonzero_volume'] = new_segment['last_nonzero_volume']
    elif action == 'video.setSpeed':
        old_segment['speed'] = new_segment['speed']
        for alias, expected in new_model.items():
            sid = expected['native_id'].casefold()
            old_segments[sid]['target_timerange'] = {'start': expected['start'], 'duration': expected['duration']}
        speed_refs = old_segment.get('extra_material_refs', [])
        speed_materials = [item for item in previous['materials']['speeds'] if item['id'] in speed_refs]
        if len(speed_materials) != 1:
            fail('Native speed material is missing/ambiguous')
        speed_materials[0]['speed'] = new_segment['speed']
        previous['duration'] = current['duration']
    elif action.endswith('.setFade'):
        old_refs = set(old_segment.get('extra_material_refs', []))
        new_refs = set(new_segment.get('extra_material_refs', []))
        fades_old = previous['materials'].get('audio_fades', [])
        fades_new = current['materials'].get('audio_fades', [])
        related_old = [item for item in fades_old if item['id'] in old_refs]
        related_new = [item for item in fades_new if item['id'] in new_refs]
        if len(related_new) != 1 or len(related_old) > 1:
            fail('Native fade change is ambiguous')
        old_nonfade = old_refs - {item['id'] for item in related_old}
        new_nonfade = new_refs - {item['id'] for item in related_new}
        if old_nonfade != new_nonfade:
            fail('Fade operation changed unrelated material references')
        old_segment['extra_material_refs'] = new_segment['extra_material_refs']
        previous['materials']['audio_fades'] = [item for item in fades_old if item not in related_old]
        current['materials']['audio_fades'] = [item for item in fades_new if item not in related_new]
        if action == 'video.setFade':
            old_video = next(item for item in previous['materials']['videos'] if item['id'] == old_segment['material_id'])
            new_video = next(item for item in current['materials']['videos'] if item['id'] == new_segment['material_id'])
            if new_video.get('audio_fade') != related_new[0]:
                fail('Video inline audio fade differs from its referenced material')
            old_video['audio_fade'] = new_video['audio_fade']
    equal(previous, current, 'Native edit unexpectedly changed unrelated draft content')


def native_params(step, model, before_tracks, draft_id):
    action = step['action']
    if action.endswith('.add'):
        params = {'segmentId': str(uuid.uuid4()).upper(), 'duration': step['duration_us'],
            'sourceDuration': step['duration_us'], 'startTime': step['start_us'],
            'sourceStartTime': step['source_start_us'], 'needInsertTargetTrack': step['track'] == len(before_tracks)}
        if action == 'video.add':
            params.update(path=step['path'], trackIndex=step['track'])
        else:
            # Material duration describes the complete source file. The clip
            # interval is sourceStartTime/sourceDuration, independently.
            params['duration'] = round(media_info(step['path'])['duration'] * 1_000_000)
            params.update(trackId=str(uuid.uuid4()).upper(), audioPath=step['path'],
                audioTitle=Path(step['path']).name, targetTrackIndex=step['track'],
                # The native dispatcher expects the numeric MetaType enum.
                # The bundled JS adapter maps music -> 4 before invoking it;
                # sending the string directly silently creates type=none.
                targetTrackName='Audio', volume=1, type=4)
        return params
    sid = model[step['segment']]['native_id']
    if action == 'segment.split':
        return {'draftId': draft_id, 'segmentId': sid, 'splitTime': step['at_us']}
    if action == 'video.setSpeed':
        # Keep both identities: segId alone has produced accepted no-ops.
        return {'draftId': draft_id, 'segmentId': sid, 'segId': sid, 'normalSpeed': float(step['speed'])}
    if action.endswith('.setVolume'):
        return {'draftId': draft_id, 'segmentIds': [sid], 'volume': step['volume'],
                'isKeyframe': False, 'isRecordAction': True}
    return {'draftId': draft_id, 'segIds': [sid], 'fadeInDuration': step['fade_in_us'],
            'fadeOutDuration': step['fade_out_us']}


def build(plan_path, install, out):
    plan = load_plan(plan_path)
    install = Path(install).resolve(strict=True)
    raw_out = Path(out)
    if raw_out.exists() or raw_out.is_symlink():
        raise FileExistsError('Output must be a new directory')
    out = raw_out.resolve()
    if out == install or out.is_relative_to(install):
        raise ValueError('Output must be outside the installed application')
    out.mkdir(parents=True, exist_ok=False)
    draft = out / 'draft'
    media = draft / 'media'
    media.mkdir(parents=True)
    manifest = []
    for step in plan['steps']:
        if 'path' in step:
            source = Path(step['path'])
            digest = file_digest(source)
            dest = media / (digest[:16] + source.suffix.lower())
            if not dest.exists():
                shutil.copyfile(source, dest)
            if file_digest(dest) != digest:
                fail('Copied media hash differs')
            step['path'] = str(dest)
            manifest.append({'source': str(source), 'copy': str(dest), 'sha256': digest})
    dump(out / 'native-timeline-plan.json', plan)
    dump(out / 'manifest.json', manifest)
    now_us = time.time_ns() // 1000
    initial = {'id': str(uuid.uuid4()).upper(), 'name': plan['name'], 'fps': plan['canvas']['fps'],
        'canvas_config': {'width': plan['canvas']['width'], 'height': plan['canvas']['height'],
            'ratio': f"{plan['canvas']['width']//math.gcd(plan['canvas']['width'],plan['canvas']['height'])}:{plan['canvas']['height']//math.gcd(plan['canvas']['width'],plan['canvas']['height'])}"},
        'duration': 0, 'tracks': [], 'create_time': now_us // 1000, 'update_time': now_us // 1000}
    report = {'status': 'in_progress', 'steps': [], 'ffmpeg_invoked': False,
        'native_editor_ui_acceptance': 'not_checked', 'homepage_registration': 'not_performed',
        'desktop_sidecar_compatibility': 'not_checked', 'all_non_member_features_verified': False,
        'validation_scope': 'Requested timeline fields, prior object preservation, native file roundtrip and reload ready; no GUI display or rendered pixels/audio checked',
        'relocation': 'absolute_paths_require_rebuild_or_explicit_rebase_after_moving'}
    service = None
    try:
        with Service(install, out / 'service') as service:
            def write(path, value):
                if path.exists():
                    raise FileExistsError('Refusing to replace an existing native file')
                service.request('draft', 'writeFile', {'path': str(path), 'rootPath': str(out), 'content': json.dumps(value, ensure_ascii=False, allow_nan=False)})
                decoded = service.request('draft', 'readFile', {'path': str(path), 'rootPath': str(out)})['result']
                if not decoded.get('exists') or json.loads(decoded['content']) != value:
                    fail('Native file roundtrip differs')
            seed = out / 'initial-content.json'
            write(seed, initial)
            session = service.request('player', 'create', {'kind': 'draft', 'serviceGeneration': 1})['result']['sessionId']
            try:
                service.request('player', 'load', {'draftPath': str(seed), 'workspacePath': str(out)}, session)
                snapshot, model, tracks = initial, {}, []
                for index, step in enumerate(plan['steps']):
                    previous, old_model = snapshot, copy.deepcopy(model)
                    params = native_params(step, model, tracks, initial['id'])
                    dump(out / f'{index:03d}-request.json', {'action': step['action'], 'params': params})
                    advance(model, tracks, step)
                    result = service.request('edit', step['action'], params, session)['result']
                    dump(out / f'{index:03d}-{step["action"]}.json', result)
                    if result.get('accepted') is not True or not isinstance(result.get('draftSnapshot'), dict):
                        fail('Native operation did not return an accepted snapshot')
                    snapshot = result['draftSnapshot']
                    new_ids = set(by_id(all_segments(snapshot))) - set(by_id(all_segments(previous)))
                    if step['action'].endswith('.add') or step['action'] == 'segment.split':
                        if len(new_ids) != 1:
                            fail('Native operation must add exactly one new segment')
                        new_id = by_id(all_segments(snapshot))[next(iter(new_ids))]['id']
                        model[step.get('id', step.get('right_id'))]['native_id'] = new_id
                    elif new_ids:
                        fail('Native property edit unexpectedly added a segment')
                    target = model[step.get('segment', step.get('id'))]['native_id']
                    verify(snapshot, model, tracks, initial)
                    verify_change(previous, snapshot, step['action'], target, old_model, model)
                    report['steps'].append({'action': step['action'], 'segment_id': target,
                        'native_params': params, 'requested_fields_verified': True, 'prior_objects_preserved': True})
                write(draft / 'draft_content.json', snapshot)
                report['native_write_read_equal'] = True
            finally:
                service.request('player', 'destroy', {}, session)
            second = service.request('player', 'create', {'kind': 'draft', 'serviceGeneration': 2})['result']['sessionId']
            try:
                loaded = service.request('player', 'load', {'draftPath': str(draft / 'draft_content.json'), 'workspacePath': str(out)}, second)['result']
                state = service.request('player', 'getState', {}, second)['result']
                report['native_player_state'] = state
                if loaded.get('accepted') is not True:
                    fail('Native reload not ready with expected duration/canvas')
                report['native_preview_dimensions'] = validate_player_state(state, snapshot['duration'], plan['canvas'])
                report['native_reload_ready_duration_canvas_verified'] = True
            finally:
                service.request('player', 'destroy', {}, second)
            write(draft / 'draft_meta_info.json', sidecar_metadata(snapshot, draft, out, now_us))
        report['service'] = service.report
        for name in ('draft_content.json', 'draft_meta_info.json'):
            shutil.copyfile(draft / name, draft / (name + '.bak'))
        shutil.copyfile(draft / 'draft_content.json', draft / 'template-2.tmp')
        dump(draft / 'timeline_layout.json', {'dockItems': [{'dockIndex': 0, 'ratio': 1, 'timelineIds': [snapshot['id']], 'timelineNames': ['Timeline 01']}], 'layoutOrientation': 1})
        dump(draft / 'draft_agency_config.json', {'is_auto_agency_enabled': False, 'is_auto_agency_popup': False, 'is_single_agency_mode': False, 'marterials': None, 'use_converter': False, 'video_resolution': 720})
        (draft / 'draft_biz_config.json').touch(exist_ok=False)
        with (draft / 'draft_settings').open('x', encoding='utf-8') as stream:
            stream.write(f'[General]\ndraft_create_time={now_us // 1_000_000}\ndraft_last_edit_time={now_us // 1_000_000}\nreal_edit_seconds=0\nreal_edit_keys=0\n')
        report['source_hashes_preserved'] = all(file_digest(Path(item['source'])) == item['sha256'] for item in manifest)
        if not report['source_hashes_preserved']:
            fail('Source media hash changed')
        report.update(status='native_timeline_created', duration_us=snapshot['duration'],
                      tracks=len(tracks), segments=len(model), aliases={alias: value['native_id'] for alias, value in model.items()})
        dump(out / 'draft-snapshot.json', snapshot)
        dump(out / 'native-timeline-report.json', report)
        return report
    except Exception as exc:
        report.update(status='failed', error=str(exc))
        if service is not None:
            report['service'] = service.report
        dump(out / 'native-timeline-failure.json', report)
        raise


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', required=True)
    parser.add_argument('--install-dir', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        report = build(args.plan, args.install_dir, args.out)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
