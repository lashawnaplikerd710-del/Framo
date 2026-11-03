"""Build an isolated draft using the installed Jianying native editing service."""
import argparse
import copy
import json
import math
from pathlib import Path
import shutil
import sys
import time
import uuid

from native_service import Service, NativeServiceError, validate_player_state
from native_draft_codec import file_digest
from common import media_info
from native_keyframes import MOTION_PROPERTIES, add_point, validate_added_point, validate_value

MAX_TIME_US = 86_400_000_000

ALLOWED = {
    'video.add': {'path', 'duration', 'sourceDuration', 'sourceStartTime', 'startTime', 'trackIndex', 'needInsertTargetTrack'},
    'text.add': {'content', 'duration', 'startTime', 'trackIndex', 'needInsertTargetTrack'},
}
ALLOWED_EDITS = {
    'video.setOpacity': {'action', 'segment', 'value'},
    'keyframe.add': {'action', 'segment', 'time_us', 'value'},
    'keyframe.motion': {'action', 'segment', 'property', 'time_us', 'value'},
    'segment.translate': {'action', 'segment', 'x', 'y'},
    'segment.scale': {'action', 'segment', 'x', 'y'},
    'segment.rotate': {'action', 'segment', 'rotation'},
}


def dump(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def checked_plan(path):
    path = Path(path).resolve(strict=True)
    def unique_pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'Duplicate JSON field: {key}')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('Non-finite JSON numbers are not accepted')
    data = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=unique_pairs,
                      parse_constant=invalid_constant)
    if not isinstance(data, dict) or set(data) - {'schema', 'name', 'canvas', 'operations', 'edits'}:
        raise ValueError('Unknown native plan fields; unsupported requested features cannot be omitted')
    if data.get('schema') != 'jianying-native-plan-v1':
        raise ValueError('Expected schema jianying-native-plan-v1')
    if not isinstance(data.get('name'), str) or not data['name'].strip():
        raise ValueError('A nonempty name is required')
    canvas = data.get('canvas', {})
    if not isinstance(canvas, dict) or set(canvas) != {'width', 'height', 'fps'}:
        raise ValueError('canvas requires width, height, fps only')
    for key in ('width', 'height', 'fps'):
        value = canvas[key]
        if type(value) is not int or not 1 <= value <= (120 if key == 'fps' else 8192):
            raise ValueError(f'Invalid canvas.{key}')
    rows = data.get('operations')
    if not isinstance(rows, list) or not rows or len(rows) > 500:
        raise ValueError('operations must contain 1..500 native operations')
    seen = set()
    occupied = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'id', 'action', 'params'}:
            raise ValueError('Each operation requires exactly id, action, params')
        alias, action, params = row['id'], row['action'], row['params']
        if not isinstance(alias, str) or not alias or alias in seen:
            raise ValueError('Operation ids must be unique nonempty strings')
        seen.add(alias)
        if not isinstance(action, str) or action not in ALLOWED:
            raise ValueError(f'Native operation is not yet verified by this adapter: {action}')
        if not isinstance(params, dict) or set(params) != ALLOWED[action]:
            raise ValueError(f'{action} requires exactly {sorted(ALLOWED[action])}')
        for key in ('duration', 'sourceDuration', 'sourceStartTime', 'startTime'):
            if key in params and (type(params[key]) is not int or
                    not (1 if key in ('duration', 'sourceDuration') else 0) <= params[key] <= MAX_TIME_US):
                raise ValueError(f'{alias}.{key} must be an integer within 24 hours; durations positive, units microseconds')
        if type(params['trackIndex']) is not int or not 0 <= params['trackIndex'] <= 100:
            raise ValueError('trackIndex must be an integer in 0..100')
        if type(params['needInsertTargetTrack']) is not bool:
            raise ValueError('needInsertTargetTrack must be a boolean')
        if params['startTime'] + params['duration'] > MAX_TIME_US:
            raise ValueError('Target interval ends after the 24-hour adapter limit')
        if action == 'text.add':
            if not isinstance(params['content'], str) or not params['content']:
                raise ValueError('Text content must be nonempty')
        else:
            if (not isinstance(params['path'], str) or not params['path'].strip()
                    or '://' in params['path'] or params['path'].startswith(('\\\\', '//'))):
                raise ValueError('Source must be an explicit local file, not a URL or network share')
            source = Path(params['path'])
            if not source.is_absolute():
                source = path.parent / source
            source = source.resolve(strict=True)
            if not source.is_file():
                raise ValueError('A regular local source file is required')
            if str(source).startswith(('\\\\', '//')):
                raise ValueError('Resolved network source paths are not supported')
            if params['sourceStartTime'] + params['sourceDuration'] > MAX_TIME_US:
                raise ValueError('Source interval ends after the 24-hour adapter limit')
            if params['sourceDuration'] != params['duration']:
                raise ValueError('This adapter currently requires sourceDuration == duration; speed changes are not verified')
            info = media_info(source)
            if not info['has_video'] or info['width'] <= 0 or info['height'] <= 0:
                raise ValueError('video.add requires readable image or video media')
            if info['is_image']:
                if params['sourceStartTime'] != 0:
                    raise ValueError('Still images require sourceStartTime=0')
            else:
                if not math.isfinite(info['duration']) or info['duration'] <= 0:
                    raise ValueError('Video duration could not be verified with MediaInfo')
                video_duration = info.get('video_duration') or info['duration']
                if params['sourceStartTime'] + params['sourceDuration'] > round(video_duration * 1_000_000):
                    raise ValueError('Requested source interval exceeds the detected video stream duration')
            params['path'] = str(source)
        # Conservative acceptance: require one add per new track. This avoids silently
        # accepting native insert/replace semantics until those are tested separately.
        track = params['trackIndex']
        if not params['needInsertTargetTrack'] or track != len(occupied):
            raise ValueError('This adapter currently requires sequential new tracks, starting at index 0')
        occupied[track] = alias
    edits = data.get('edits', [])
    if not isinstance(edits, list) or len(edits) > 500:
        raise ValueError('edits must be an array containing at most 500 edits')
    aliases = {row['id']: row for row in rows}
    keyframe_positions = set()
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError('Each edit must be an object')
        action = edit.get('action')
        if not isinstance(action, str) or action not in ALLOWED_EDITS:
            raise ValueError('Edit action is not supported by the verified native adapter')
        if set(edit) != ALLOWED_EDITS[action]:
            raise ValueError(f'{action} edit requires exactly {sorted(ALLOWED_EDITS[action])}')
        alias = edit['segment']
        if not isinstance(alias, str) or alias not in aliases or aliases[alias]['action'] != 'video.add':
            raise ValueError('Property edits require the id of a video.add operation')
        if action in ('video.setOpacity', 'keyframe.add'):
            value = edit['value']
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError('Opacity/alpha values must be finite numbers in [0, 1]')
        elif action == 'keyframe.motion':
            if edit['property'] not in MOTION_PROPERTIES:
                raise ValueError('Only verified position_x, position_y, rotation, scale motion is supported')
            validate_value(edit['property'], edit['time_us'], edit['value'], aliases[alias]['params']['duration'])
        else:
            fields = ('rotation',) if action == 'segment.rotate' else ('x', 'y')
            low, high = ((-360, 360) if action == 'segment.rotate' else
                         (0.001, 100) if action == 'segment.scale' else (-10, 10))
            for field in fields:
                value = edit[field]
                if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(f'{action}.{field} requires a finite number within [{low}, {high}]')
        if action in ('keyframe.add', 'keyframe.motion'):
            position = edit['time_us']
            if type(position) is not int or not 0 <= position <= aliases[alias]['params']['duration']:
                raise ValueError('Keyframe time_us must be an integer from 0 through segment duration')
            property_name = 'alpha' if action == 'keyframe.add' else edit['property']
            if (alias, property_name, position) in keyframe_positions:
                raise ValueError('Duplicate keyframe positions for the same property are not supported')
            keyframe_positions.add((alias, property_name, position))
    return data


def validate_snapshot(snapshot, plan, applied, draft_id):
    """Verify each requested edit, including every previously accepted segment."""
    if snapshot.get('id') != draft_id or snapshot.get('name') != plan['name']:
        raise NativeServiceError('Native draft identity/name differs from the plan')
    canvas = snapshot.get('canvas_config', {})
    if (canvas.get('width') != plan['canvas']['width'] or
            canvas.get('height') != plan['canvas']['height'] or
            snapshot.get('fps') != plan['canvas']['fps']):
        raise NativeServiceError('Native canvas or frame rate differs from the plan')
    tracks = snapshot.get('tracks')
    if not isinstance(tracks, list) or len(tracks) != len(applied):
        raise NativeServiceError('Native track count differs from the requested new tracks')
    ids = set()
    materials = snapshot.get('materials', {})
    for expected, native_id in applied:
        params, action = expected['params'], expected['action']
        track = tracks[params['trackIndex']]
        if track.get('type') != ('video' if action == 'video.add' else 'text'):
            raise NativeServiceError('Native track type differs from the requested action')
        segments = track.get('segments', [])
        if len(segments) != 1 or segments[0].get('id') != native_id or native_id.casefold() in ids:
            raise NativeServiceError('Native segment identity/uniqueness or previous segment preservation failed')
        ids.add(native_id.casefold())
        segment = segments[0]
        if segment.get('target_timerange') != {'start': params['startTime'], 'duration': params['duration']}:
            raise NativeServiceError('Native target interval differs from the requested interval')
        group = 'videos' if action == 'video.add' else 'texts'
        matches = [item for item in materials.get(group, []) if item.get('id') == segment.get('material_id')]
        if len(matches) != 1:
            raise NativeServiceError('Native segment material reference is missing or ambiguous')
        material = matches[0]
        if action == 'video.add':
            if segment.get('source_timerange') != {'start': params['sourceStartTime'], 'duration': params['sourceDuration']}:
                raise NativeServiceError('Native source interval differs from the requested interval')
            if segment.get('speed') != 1:
                raise NativeServiceError('Native segment unexpectedly changed playback speed')
            for field in ('path', 'media_path'):
                native_path = material.get(field)
                if not isinstance(native_path, str) or Path(native_path).resolve() != Path(params['path']).resolve():
                    raise NativeServiceError('Native video material does not reference the isolated media copy')
        else:
            text_data = json.loads(material.get('content', 'null'))
            if not isinstance(text_data, dict) or text_data.get('text') != params['content']:
                raise NativeServiceError('Native text material differs from requested text')
    expected_duration = max(row['params']['startTime'] + row['params']['duration'] for row, _ in applied)
    if snapshot.get('duration') != expected_duration:
        raise NativeServiceError('Native timeline duration differs from requested intervals')


def local_bin_entries(snapshot):
    """Index each local source once, using its full measured media duration.

    Timeline materials and the desktop import bin use separate metadata.
    The type=0 local bucket uses file_Path and metetype, including for audio.
    Its schema follows the attributed pyJianYingDraft registration implementation.
    """
    entries, seen, pending = [], set(), [snapshot]
    while pending:
        graph = pending.pop()
        materials = graph.get('materials', {})
        for kind in ('videos', 'audios'):
            for material in materials.get(kind, []):
                if kind=='audios' and material.get('type') in ('extract_music','text_to_audio'):
                    continue  # Native extraction/TTS belongs on the timeline, not the local import bin.
                # Native online audio retains its resource ID and native type.
                # Its downloaded cache is not a user import-bin item.
                if (kind == 'audios' and material.get('type') in ('music','sound')
                        and str(material.get('resource_id','')).isdigit()):
                    continue
                raw_path = material.get('path')
                if not raw_path:
                    continue  # A compound wrapper has no independent media file.
                path = Path(raw_path).resolve(strict=True)
                key = str(path).casefold()
                if key in seen:
                    continue
                info = media_info(path)
                duration = round(info['duration'] * 1_000_000)
                if info['is_image']:
                    metetype = 'photo'
                elif info['has_video']:
                    metetype = 'video'
                elif info['has_audio']:
                    metetype = 'music'
                else:
                    raise NativeServiceError('Imported source has no supported media stream')
                entries.append({'duration': duration, 'height': info['height'], 'width': info['width'],
                    'md5': '', 'metetype': metetype, 'type': 0, 'create_time': 0,
                    'extra_info': path.name, 'file_Path': str(path), 'import_time': 0,
                    'import_time_ms': 0, 'item_source': 1,
                    'roughcut_time_range': {'duration': duration, 'start': 0},
                    'sub_time_range': {'duration': -1, 'start': -1}})
                seen.add(key)
        for child in materials.get('drafts', []):
            if isinstance(child.get('draft'), dict):
                pending.append(child['draft'])
    return entries


def sidecar_metadata(snapshot, draft, out, now_us):
    """Write fresh project metadata; do not reuse any prior draft identity/path."""
    return {
        'cloud_package_completed_time': '', 'draft_cloud_capcut_purchase_info': '',
        'draft_cloud_last_action_download': False, 'draft_cloud_materials': [],
        'draft_cloud_purchase_info': '', 'draft_cloud_template_id': '',
        'draft_cloud_tutorial_info': '', 'draft_cloud_videocut_purchase_info': '',
        'draft_cover': '', 'draft_deeplink_url': '',
        'draft_enterprise_info': {'draft_enterprise_extra': '', 'draft_enterprise_id': '',
                                  'draft_enterprise_name': '', 'enterprise_material': []},
        'draft_fold_path': str(draft), 'draft_id': snapshot['id'],
        'draft_is_ai_packaging_used': False, 'draft_is_ai_shorts': False,
        'draft_is_ai_translate': False, 'draft_is_article_video_draft': False,
        'draft_is_from_deeplink': 'false', 'draft_is_invisible': False,
        'draft_materials': [{'type': kind, 'value': local_bin_entries(snapshot) if kind == 0 else []}
                            for kind in (0, 1, 2, 3, 6, 7, 8)],
        'draft_materials_copied_info': [], 'draft_name': snapshot['name'],
        'draft_new_version': snapshot.get('new_version', ''),
        'draft_removable_storage_device': '', 'draft_root_path': str(out),
        'draft_segment_extra_info': [], 'draft_type': '',
        'tm_draft_cloud_completed': '', 'tm_draft_cloud_modified': 0,
        'tm_draft_removed': 0, 'tm_duration': snapshot['duration'],
        'draft_json_file': str(draft / 'draft_content.json'),
        'tm_draft_create': now_us, 'tm_draft_modified': now_us,
    }


def segment_in(snapshot, native_id):
    matches = [segment for track in snapshot.get('tracks', [])
               for segment in track.get('segments', []) if segment.get('id') == native_id]
    if len(matches) != 1:
        raise NativeServiceError('Property edit segment identity is missing or ambiguous')
    return matches[0]


def same_alpha(actual, expected):
    return (type(actual) in (int, float) and math.isfinite(actual)
            and math.isclose(actual, expected, rel_tol=0, abs_tol=1e-6))


def alpha_keyframe_snapshot(snapshot, native_id, time_us, value):
    """Submit Alpha through the same verified source-time mapping as motion."""
    return add_point(snapshot, native_id, 'alpha', time_us, value)


def validate_property_change(before, after, edit, native_id):
    """Allow only requested geometry/alpha changes or a new point; preserve all else."""
    if edit['action'] in ('keyframe.motion', 'keyframe.add'):
        property_name = edit['property'] if edit['action'] == 'keyframe.motion' else 'alpha'
        validate_added_point(before, after, native_id, property_name, edit['time_us'], edit['value'])
        return
    previous = copy.deepcopy(before)
    current = copy.deepcopy(after)
    old_segment, new_segment = segment_in(previous, native_id), segment_in(current, native_id)
    if edit['action'] == 'video.setOpacity':
        if not same_alpha(new_segment.get('clip', {}).get('alpha'), edit['value']):
            raise NativeServiceError('Native opacity does not match the requested value')
        if 'alpha' not in old_segment.get('clip', {}):
            raise NativeServiceError('Previous native segment has no comparable alpha field')
        # Ignore precisely the property whose modification was requested.
        new_segment['clip']['alpha'] = old_segment['clip']['alpha']
    elif edit['action'].startswith('segment.'):
        field = {'segment.translate': 'transform', 'segment.scale': 'scale', 'segment.rotate': 'rotation'}[edit['action']]
        actual = new_segment.get('clip', {}).get(field)
        if field == 'rotation':
            valid = same_alpha(actual, edit['rotation'])
        else:
            valid = (isinstance(actual, dict) and set(actual) == {'x', 'y'} and
                     all(same_alpha(actual[axis], edit[axis]) for axis in ('x', 'y')))
        if not valid or field not in old_segment.get('clip', {}):
            raise NativeServiceError('Native geometry does not match the requested value')
        new_segment['clip'][field] = old_segment['clip'][field]
    else:
        raise NativeServiceError('Unsupported property validation action')
    if current != previous:
        raise NativeServiceError('Native property edit unexpectedly changed other draft content')


def build(plan_path, install, out):
    plan = checked_plan(plan_path)
    install = Path(install).resolve(strict=True)
    raw_out = Path(out)
    if raw_out.exists() or raw_out.is_symlink():
        raise FileExistsError('Choose a new output directory; existing paths are preserved')
    out = raw_out.resolve()
    if out == install or out.is_relative_to(install):
        raise ValueError('Output must be outside the editor installation')
    out.mkdir(parents=True, exist_ok=False)
    draft = out / 'draft'
    media = draft / 'media'
    media.mkdir(parents=True)
    draft_id = str(uuid.uuid4()).upper()
    now_us = time.time_ns() // 1000
    canvas_gcd = math.gcd(plan['canvas']['width'], plan['canvas']['height'])
    initial = {'id': draft_id, 'name': plan['name'], 'canvas_config': {
        'width': plan['canvas']['width'], 'height': plan['canvas']['height'],
        'ratio': f"{plan['canvas']['width'] // canvas_gcd}:{plan['canvas']['height'] // canvas_gcd}"},
        'duration': 0, 'fps': plan['canvas']['fps'], 'tracks': [],
        'create_time': now_us // 1000, 'update_time': now_us // 1000}
    manifest = []
    for row in plan['operations']:
        if 'path' not in row['params']:
            continue
        source = Path(row['params']['path'])
        digest = file_digest(source)
        dest = media / (digest[:16] + source.suffix.lower())
        if not dest.exists():
            shutil.copyfile(source, dest)
        if file_digest(dest) != digest:
            raise ValueError('Copied asset hash mismatch')
        row['params']['path'] = str(dest)
        manifest.append({'source': str(source), 'copy': str(dest), 'sha256': digest})
    dump(out / 'native-plan.json', plan)
    dump(out / 'manifest.json', manifest)
    report = {'status': 'in_progress', 'native_service_used': True,
        'native_editor_ui_acceptance': 'not_checked', 'homepage_registration': 'not_performed',
        'desktop_sidecar_compatibility': 'not_checked',
        'relocation': 'absolute_paths_require_rebuild_or_explicit_rebase_after_moving',
        'validation_scope': 'Native editing snapshot, file roundtrip, reload and player ready/duration/canvas; GUI display/rendering not checked',
        'ffmpeg_invoked': False, 'all_non_member_features_verified': False, 'operations': [], 'edits': []}
    content_path = draft / 'draft_content.json'
    service = None
    try:
        with Service(install, out / 'service') as service:
            def write_file(path, value):
                if path.exists():
                    raise ValueError(f'Refusing to replace an existing file: {path}')
                result = service.request('draft', 'writeFile', {
                    'path': str(path), 'rootPath': str(out),
                    'content': json.dumps(value, ensure_ascii=False, allow_nan=False)})
                decoded = service.request('draft', 'readFile', {'path': str(path), 'rootPath': str(out)})['result']
                if not decoded.get('exists') or json.loads(decoded['content']) != value:
                    raise NativeServiceError('Native write/read roundtrip differs from requested JSON')
                return result['result']

            start_path = out / 'initial-content.json'
            write_file(start_path, initial)
            session = service.request('player', 'create', {'kind': 'draft', 'serviceGeneration': 1})['result']['sessionId']
            try:
                service.request('player', 'load', {'draftPath': str(start_path), 'workspacePath': str(out)}, session)
                snapshot = initial
                applied = []
                for row in plan['operations']:
                    params = dict(row['params'], segmentId=str(uuid.uuid4()).upper())
                    result = service.request('edit', row['action'], params, session)['result']
                    if result.get('accepted') is not True or not isinstance(result.get('draftSnapshot'), dict):
                        raise NativeServiceError('Native editing returned no draft snapshot')
                    snapshot = result['draftSnapshot']
                    native_id = result.get('segmentId')
                    if not isinstance(native_id, str) or not native_id:
                        raise NativeServiceError('Native editing did not return its actual segment identity')
                    if (result.get('targetDurationUs') != params['duration'] or
                            result.get('targetStartUs') != params['startTime']):
                        raise NativeServiceError('Native result interval differs from the requested interval')
                    applied.append((row, native_id))
                    validate_snapshot(snapshot, plan, applied, draft_id)
                    report['operations'].append({'id': row['id'], 'action': row['action'],
                        'segment_id': native_id, 'requested_segment_id': params['segmentId'],
                        'accepted': True, 'plan_fields_verified': True,
                        'previous_segments_verified': True,
                        'revision': result.get('diff', {}).get('revision')})
                aliases = {row['id']: (row, native_id) for row, native_id in applied}
                if any(edit['action'] in ('keyframe.add', 'keyframe.motion') for edit in plan.get('edits', [])):
                    # Loading/replacing a freshly added draft normalizes the
                    # native no-op sound mapping enum. Account for this in an
                    # explicit preparation step, never as an ignored keyframe
                    # side effect. Everything else must stay exactly equal.
                    expected = copy.deepcopy(snapshot)
                    normalized_count = 0
                    for material in expected.get('materials', {}).get('sound_channel_mappings', []):
                        if material.get('type') == '':
                            material['type'] = 'none'
                            normalized_count += 1
                    prepared = service.request('edit', 'draft.setDraft', {'draftSnapshot': snapshot}, session)['result']
                    if prepared.get('accepted') is not True or prepared.get('draftSnapshot') != expected:
                        raise NativeServiceError('Native draft preparation changed content beyond the verified sound mapping normalization')
                    snapshot = prepared['draftSnapshot']
                    validate_snapshot(snapshot, plan, applied, draft_id)
                    report['keyframe_preparation'] = {'native_action': 'draft.setDraft',
                        'sound_mapping_empty_to_none': normalized_count, 'other_content_preserved': True}
                for edit in plan.get('edits', []):
                    source_row, native_id = aliases[edit['segment']]
                    native_action = edit['action']
                    if edit['action'] == 'video.setOpacity':
                        edit_params = {'segId': native_id, 'value': edit['value'], 'isKeyframe': False}
                    elif edit['action'].startswith('segment.'):
                        edit_params = {'draftId': draft_id, 'segmentId': native_id, 'isKeyframe': False}
                        edit_params.update({key: value for key, value in edit.items() if key not in ('action', 'segment')})
                    elif edit['action'] == 'keyframe.motion':
                        native_action = 'draft.setDraft'
                        edit_params = {'draftSnapshot': add_point(snapshot, native_id, edit['property'],
                                                                 edit['time_us'], edit['value'])}
                    else:
                        native_action = 'draft.setDraft'
                        edit_params = {'draftSnapshot': alpha_keyframe_snapshot(
                            snapshot, native_id, edit['time_us'], edit['value'])}
                    edited = service.request('edit', native_action, edit_params, session)['result']
                    if edited.get('accepted') is not True or not isinstance(edited.get('draftSnapshot'), dict):
                        raise NativeServiceError('Native property edit was not accepted with a snapshot')
                    changed = edited['draftSnapshot']
                    validate_snapshot(changed, plan, applied, draft_id)
                    validate_property_change(snapshot, changed, edit, native_id)
                    snapshot = changed
                    report['edits'].append(dict(edit, native_action=native_action, segment_id=native_id, accepted=True,
                        requested_property_verified=True, other_draft_content_preserved=True,
                        revision=edited.get('diff', {}).get('revision')))
                report['content_encoding'] = write_file(content_path, snapshot)
                report['native_write_read_equal'] = True
            finally:
                service.request('player', 'destroy', {}, session)
            # Load the saved native file in a second engine session, not just JSON checks.
            second = service.request('player', 'create', {'kind': 'draft', 'serviceGeneration': 2})['result']['sessionId']
            try:
                loaded = service.request('player', 'load', {'draftPath': str(content_path), 'workspacePath': str(out)}, second)['result']
                if loaded.get('accepted') is not True:
                    raise NativeServiceError('Saved draft reload was not explicitly accepted')
                report['native_saved_draft_reload_request_accepted'] = True
                report['native_player_state'] = service.request('player', 'getState', {}, second)['result']
                state = report['native_player_state']
                report['native_preview_dimensions'] = validate_player_state(state, snapshot['duration'], plan['canvas'])
                report['native_reload_ready_duration_canvas_verified'] = True
            finally:
                service.request('player', 'destroy', {}, second)
            meta = sidecar_metadata(snapshot, draft, out, now_us)
            write_file(draft / 'draft_meta_info.json', meta)
        report['service'] = service.report
        report['status'] = 'native_draft_created'
        report['tracks'] = len(snapshot.get('tracks', []))
        report['segments'] = sum(len(t.get('segments', [])) for t in snapshot.get('tracks', []))
        report['alpha_keyframes'] = sum(len(group.get('keyframe_list', []))
            for track in snapshot.get('tracks', []) for segment in track.get('segments', [])
            for group in segment.get('common_keyframes', []) if group.get('property_type') == 'KFTypeAlpha')
        report['duration_us'] = snapshot.get('duration', 0)
        dump(out / 'draft-snapshot.json', snapshot)
        for name in ('draft_content.json', 'draft_meta_info.json'):
            shutil.copyfile(draft / name, draft / (name + '.bak'))
        shutil.copyfile(content_path, draft / 'template-2.tmp')
        dump(draft / 'timeline_layout.json', {'dockItems': [{'dockIndex': 0, 'ratio': 1,
             'timelineIds': [snapshot['id']], 'timelineNames': ['时间线01']}], 'layoutOrientation': 1})
        dump(draft / 'draft_agency_config.json', {'is_auto_agency_enabled': False,
             'is_auto_agency_popup': False, 'is_single_agency_mode': False,
             'marterials': None, 'use_converter': False, 'video_resolution': 720})
        (draft / 'draft_biz_config.json').touch(exist_ok=False)
        with (draft / 'draft_settings').open('x', encoding='utf-8') as settings:
            settings.write('[General]\ndraft_create_time=' + str(now_us // 1_000_000) +
                           '\ndraft_last_edit_time=' + str(now_us // 1_000_000) +
                           '\nreal_edit_seconds=0\nreal_edit_keys=0\n')
        # Original source files must remain identical after the native engine has run.
        report['source_hashes_preserved'] = all(file_digest(Path(m['source'])) == m['sha256'] for m in manifest)
        if not report['source_hashes_preserved']:
            raise NativeServiceError('Source hash changed during native build')
        dump(out / 'native-build-report.json', report)
        return report
    except Exception as exc:
        report.update(status='failed', error=str(exc))
        if service is not None:
            report['service'] = service.report
        dump(out / 'native-build-failure.json', report)
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
        result = build(args.plan, args.install_dir, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'status':'error','error':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
