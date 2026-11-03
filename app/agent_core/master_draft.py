"""Deterministic multi-output draft composition and extraction.

The composer works on already validated native draft snapshots. It never
pretends that an unsupported exporter exists; extraction produces a fresh
zero-based temporary draft that can be handed to a supported exporter or
opened for manual export.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import uuid

from .batch_model import DEFAULT_GAP_US, frame_align
from .task_store import atomic_json, digest


class MasterDraftError(ValueError):
    pass


def _objects(snapshot):
    for group in snapshot.get('materials', {}).values():
        if isinstance(group, list):
            for item in group:
                if isinstance(item, dict) and item.get('id'):
                    yield item
    for track in snapshot.get('tracks', []):
        yield track
        for segment in track.get('segments', []):
            yield segment


def _id_map(snapshot, prefix):
    mapping = {}
    for item in _objects(snapshot):
        value = item.get('id')
        if isinstance(value, str) and value:
            mapping[value] = f'{prefix}_{value}'
    mapping[snapshot.get('id')] = f'{prefix}_{snapshot.get("id", uuid.uuid4().hex)}'
    return mapping


def _rewrite_ids(value, mapping):
    if isinstance(value, dict):
        return {key: _rewrite_ids(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_ids(item, mapping) for item in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def _rewrite_paths(value, old_root, new_root):
    if isinstance(old_root, (list, tuple)):
        result = value
        for item in old_root:
            result = _rewrite_paths(result, item, new_root)
        return result
    old = str(Path(old_root).resolve()).replace('\\', '/')
    new = str(Path(new_root).resolve()).replace('\\', '/')
    if isinstance(value, dict):
        return {key: _rewrite_paths(item, old_root, new_root) for key, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_paths(item, old_root, new_root) for item in value]
    if isinstance(value, str):
        normalized = value.replace('\\', '/')
        if normalized.casefold() == old.casefold() or normalized.casefold().startswith(old.casefold() + '/'):
            return new + normalized[len(old):]
    return value


def _shift_segment(segment, offset_us, child_start, child_end):
    result = copy.deepcopy(segment)
    timerange = result.get('target_timerange')
    if not isinstance(timerange, dict):
        raise MasterDraftError('segment lacks target_timerange')
    start = timerange.get('start')
    duration = timerange.get('duration')
    if type(start) is not int or type(duration) is not int or duration < 0:
        raise MasterDraftError('segment timerange is invalid')
    end = start + duration
    if start < 0 or end > child_end:
        raise MasterDraftError('segment leaves child output range')
    result['target_timerange'] = {'start': start + offset_us, 'duration': duration}
    return result


def validate_snapshot(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('tracks'), list):
        raise MasterDraftError('native snapshot requires tracks')
    duration = snapshot.get('duration')
    fps = snapshot.get('fps')
    if type(duration) is not int or duration <= 0 or type(fps) is not int or not 1 <= fps <= 120:
        raise MasterDraftError('native snapshot duration/fps is invalid')
    for track in snapshot['tracks']:
        if not isinstance(track, dict) or track.get('type') not in ('video', 'audio', 'text', 'effect', 'sticker', 'filter'):
            raise MasterDraftError('unsupported or malformed track')
        for segment in track.get('segments', []):
            timerange = segment.get('target_timerange') or {}
            start, length = timerange.get('start'), timerange.get('duration')
            if type(start) is not int or type(length) is not int or start < 0 or length <= 0 or start + length > duration:
                raise MasterDraftError('segment lies outside snapshot duration')
    return snapshot


def compose_master(children, *, gap_us=DEFAULT_GAP_US, name='剪映 Agent 批量成品'):
    """Compose ``[(output_id, snapshot), ...]`` into one absolute timeline."""
    if not isinstance(children, list) or not children:
        raise MasterDraftError('at least one child draft is required')
    first = validate_snapshot(children[0][1])
    fps = first['fps']
    gap_us = frame_align(gap_us, fps)
    canvas = first.get('canvas_config', {})
    master = {
        **copy.deepcopy(first),
        'id': 'batch_' + uuid.uuid4().hex,
        'name': name,
        'duration': 0,
        'tracks': [],
        'materials': {},
        'batch_schema': 'jianying-agent-master-draft-v1',
        'batch_outputs': [],
    }
    # Jianying's main-track adsorption is a draft-level setting.  Child
    # drafts produced by the native adapter may carry the default ``true``;
    # leaving that value in a composed master causes Jianying to close the
    # absolute-time gaps while loading/saving the draft.  Set the real native
    # field explicitly so the three-second gaps remain editable and visible.
    master.setdefault('config', {})['maintrack_adsorb'] = False
    master['batch_settings'] = {
        'maintrack_adsorb': False,
        'gap_us': gap_us,
        'gap_seconds': gap_us / 1_000_000,
        'positioning': 'absolute_time_us',
    }
    track_slots = {}
    material_seen = set()
    cursor = 0
    for ordinal, pair in enumerate(children, 1):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise MasterDraftError('child must be (output_id, snapshot)')
        output_id, snapshot = pair
        if not isinstance(output_id, str) or not output_id.strip():
            raise MasterDraftError('output_id must be nonempty')
        snapshot = validate_snapshot(snapshot)
        if snapshot['fps'] != fps or snapshot.get('canvas_config', {}) != canvas:
            raise MasterDraftError('all child drafts must share fps and canvas')
        child_start = cursor
        child_end = child_start + snapshot['duration']
        prefix = f'out{ordinal:02d}_{output_id}'
        mapping = _id_map(snapshot, prefix)
        for key, group in snapshot.get('materials', {}).items():
            if not isinstance(group, list):
                master['materials'].setdefault(key, copy.deepcopy(group)); continue
            target_group = master['materials'].setdefault(key, [])
            for item in group:
                if not isinstance(item, dict):
                    continue
                value = _rewrite_ids(copy.deepcopy(item), mapping)
                identity = value.get('id')
                if identity not in material_seen:
                    target_group.append(value); material_seen.add(identity)
        for track_index, track in enumerate(snapshot.get('tracks', [])):
            key = (track.get('type'), track_index)
            if key not in track_slots:
                new_track = {k: copy.deepcopy(v) for k, v in track.items() if k != 'segments'}
                new_track['id'] = mapping.get(track.get('id'), f'{prefix}_track_{track_index}')
                new_track['segments'] = []
                master['tracks'].append(new_track)
                track_slots[key] = new_track
            target = track_slots[key]
            for segment in track.get('segments', []):
                shifted = _shift_segment(segment, child_start, 0, snapshot['duration'])
                shifted = _rewrite_ids(shifted, mapping)
                target['segments'].append(shifted)
        master['batch_outputs'].append({
            'output_id': output_id, 'ordinal': ordinal,
            'start_us': child_start, 'end_us': child_end,
            'duration_us': snapshot['duration'], 'gap_after_us': gap_us if ordinal < len(children) else 0,
            'source_snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True,
                                                               ensure_ascii=False).encode()).hexdigest(),
        })
        cursor = child_end + (gap_us if ordinal < len(children) else 0)
    master['duration'] = cursor
    for track in master['tracks']:
        track['segments'].sort(key=lambda s: (s.get('target_timerange', {}).get('start', 0), s.get('id', '')))
    report = validate_master(master)
    master['batch_validation'] = report
    return master, report


def validate_master(snapshot):
    validate_snapshot(snapshot)
    outputs = snapshot.get('batch_outputs')
    if not isinstance(outputs, list) or not outputs:
        raise MasterDraftError('master draft has no batch output map')
    issues = []
    for index, item in enumerate(outputs):
        start, end = item.get('start_us'), item.get('end_us')
        if type(start) is not int or type(end) is not int or not start < end:
            issues.append({'code': 'invalid_output_range', 'index': index})
        if index and start < outputs[index-1]['end_us'] + outputs[index-1].get('gap_after_us', 0):
            issues.append({'code': 'output_overlap_or_gap_mismatch', 'index': index})
    for track in snapshot.get('tracks', []):
        for segment in track.get('segments', []):
            timerange = segment['target_timerange']
            start, end = timerange['start'], timerange['start'] + timerange['duration']
            belonging = [row for row in outputs if row['start_us'] <= start and end <= row['end_us']]
            if len(belonging) != 1:
                issues.append({'code': 'cross_output_segment', 'track': track.get('id'),
                               'segment': segment.get('id'), 'range': [start, end]})
    return {'schema_version': '1.0', 'status': 'clear' if not issues else 'issues',
            'issues': issues, 'output_count': len(outputs),
            'empty_gaps_verified': not any(item['gap_after_us'] < 0 for item in outputs)}


def extract_output(master, output_id):
    """Create a fresh zero-based snapshot for one output range."""
    validate_master(master)
    rows = [row for row in master['batch_outputs'] if row['output_id'] == output_id]
    if len(rows) != 1:
        raise MasterDraftError('unknown or duplicate output_id')
    row = rows[0]
    start, end = row['start_us'], row['end_us']
    result = copy.deepcopy(master)
    result['id'] = 'export_' + uuid.uuid4().hex
    result['name'] = f"{master.get('name', '批量成品')} · {output_id}"
    result['duration'] = end - start
    result['batch_outputs'] = [{**row, 'start_us': 0, 'end_us': end - start,
                                'gap_after_us': 0}]
    for track in result.get('tracks', []):
        kept = []
        for segment in track.get('segments', []):
            timerange = segment.get('target_timerange', {})
            a, b = timerange.get('start', -1), timerange.get('start', -1) + timerange.get('duration', -1)
            if start <= a and b <= end:
                value = copy.deepcopy(segment)
                value['target_timerange']['start'] = a - start
                kept.append(value)
            elif a < end and b > start:
                raise MasterDraftError('segment crosses selected output boundary')
        track['segments'] = kept
    result['batch_schema'] = 'jianying-agent-output-draft-v1'
    result['batch_validation'] = validate_snapshot(result) and {'status': 'clear', 'issues': []}
    return result


def write_snapshot_draft(snapshot, destination, *, template=None, templates=None):
    """Write a draft folder for a temporary export or inspection.

    A template draft is copied when available so native sidecar files and local
    dependencies remain present. The content and metadata are always rewritten.
    """
    validate_snapshot(snapshot)
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError('draft destination already exists')
    roots = [Path(item).resolve(strict=True) for item in (templates or ([template] if template else []))]
    copy_warnings = []
    if roots:
        destination.mkdir(parents=True)
        excluded = {'draft_content.json', 'draft_meta_info.json', 'template-2.tmp',
                    'batch-output.json'}
        for root in roots:
            for item in root.iterdir():
                if item.name in excluded:
                    continue
                target = destination / item.name
                if item.is_dir():
                    # Some Jianying resource packages contain stale optional
                    # entries after a cache cleanup.  Copy every existing file
                    # independently so one missing optional shader does not
                    # prevent the whole editable batch draft from being built.
                    for source_file in item.rglob('*'):
                        if not source_file.is_file():
                            continue
                        relative = source_file.relative_to(item)
                        target_file = target / relative
                        try:
                            target_file.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(source_file, target_file)
                        except (OSError, shutil.Error) as exc:
                            copy_warnings.append({'source': str(source_file),
                                                  'error': type(exc).__name__ + ': ' + str(exc)})
                elif not target.exists():
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, target)
                    except (OSError, shutil.Error) as exc:
                        copy_warnings.append({'source': str(item),
                                              'error': type(exc).__name__ + ': ' + str(exc)})
    else:
        destination.mkdir(parents=True)
    content = _rewrite_paths(snapshot, roots, destination) if roots else snapshot
    atomic_json(destination / 'draft_content.json', content)
    # A copied child layout must point at the new composed timeline.
    atomic_json(destination / 'timeline_layout.json', {
        'dockItems': [{'dockIndex': 0, 'ratio': 1,
                       'timelineIds': [content['id']], 'timelineNames': ['Timeline 01']}],
        'layoutOrientation': 1})
    meta_path = destination / 'draft_meta_info.json'
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            meta = {}
    else:
        meta = {}
    now_us = int(__import__('time').time() * 1_000_000)
    meta.update({'draft_id': snapshot.get('id'), 'draft_name': snapshot.get('name'),
                 'draft_fold_path': str(destination), 'draft_root_path': str(destination.parent),
                 'draft_json_file': str(destination / 'draft_content.json'),
                 'tm_duration': snapshot['duration'], 'tm_draft_create': now_us,
                 'tm_draft_modified': now_us})
    atomic_json(meta_path, meta)
    # The native reader accepts this sidecar for temporary extraction. A full
    # homepage registration still goes through the existing verified registrar.
    atomic_json(destination / 'batch-output.json', {
        'schema_version': '1.0', 'draft_id': snapshot.get('id'),
        'name': snapshot.get('name'), 'duration_us': snapshot['duration'],
        'snapshot_sha256': digest(destination / 'draft_content.json'),
        'template_copy_warnings': copy_warnings})
    return destination
