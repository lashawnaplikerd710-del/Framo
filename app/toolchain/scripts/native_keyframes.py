"""Verified linear motion curves for Jianying 11.5.0.14471 native snapshots.

Only these properties have native cold-reload pixel evidence. A stored enum is
not proof of rendering; do not expose arbitrary property names to the planner.
"""
import copy
import math
import uuid

from native_service import NativeServiceError

PROPERTIES = {
    'alpha': ('KFTypeAlpha', 0, 1),
    'position_x': ('KFTypePositionX', -10, 10),
    'position_y': ('KFTypePositionY', -10, 10),
    'rotation': ('KFTypeRotation', -360, 360),
    'scale': ('KFTypeScaleX', .001, 100),
}
MOTION_PROPERTIES = tuple(name for name in PROPERTIES if name != 'alpha')


def segment_in(snapshot, segment_id):
    rows = [segment for track in snapshot.get('tracks', []) for segment in track.get('segments', [])
            if segment.get('id') == segment_id]
    if len(rows) != 1:
        raise NativeServiceError('Keyframe target is missing or ambiguous')
    return rows[0]


def validate_value(property_name, time_us, value, duration):
    if not isinstance(property_name, str) or property_name not in PROPERTIES:
        raise ValueError('Unverified native keyframe property')
    _, low, high = PROPERTIES[property_name]
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{property_name} requires a finite number within [{low}, {high}]')
    if type(time_us) is not int or not 0 <= time_us <= duration:
        raise ValueError('Keyframe time must be an integer within the segment target duration')


def native_time(snapshot, segment, time_us):
    """Native curves use source time, including trim and constant playback speed.

    Real 11.5 probes with a 2 s source in-point at 1x and 2x confirmed this
    transform. Unmapped reverse, loop and variable-speed cases fail explicitly.
    """
    if segment.get('reverse', False) is not False or segment.get('is_loop', False) is not False:
        raise NativeServiceError('Reverse/loop motion time mapping is not verified')
    speed = segment.get('speed', 1)
    if type(speed) not in (int, float) or not math.isfinite(speed) or speed <= 0:
        raise NativeServiceError('Native motion requires a positive finite constant speed')
    speed_materials = [m for m in snapshot.get('materials', {}).get('speeds', [])
                       if m.get('id') in segment.get('extra_material_refs', [])]
    if len(speed_materials) > 1 or any(m.get('curve_speed') is not None or m.get('mode', 0) != 0 or
            type(m.get('speed')) not in (int, float) or not math.isclose(m['speed'], speed, abs_tol=1e-6)
            for m in speed_materials):
        raise NativeServiceError('Variable or ambiguous native speed cannot map motion times')
    material = next((m for m in snapshot.get('materials', {}).get('videos', [])
                     if m.get('id') == segment.get('material_id')), {})
    source = segment.get('source_timerange')
    if material.get('type') == 'photo':
        if speed != 1 or (source and source.get('start') != 0):
            raise NativeServiceError('Still-image motion requires a zero in-point and 1x speed')
        return time_us
    if (material.get('type') != 'video' or not isinstance(source, dict) or
            type(source.get('start')) is not int or source['start'] < 0 or
            type(source.get('duration')) is not int or source['duration'] <= 0):
        raise NativeServiceError('Native video source-time metadata is missing or unsupported')
    duration = segment['target_timerange']['duration']
    if not math.isclose(source['duration'], duration * speed, rel_tol=1e-6, abs_tol=2):
        raise NativeServiceError('Source and target spans do not match constant-speed motion mapping')
    return source['start'] + round(time_us * speed)


def add_point(snapshot, segment_id, property_name, time_us, value):
    changed = copy.deepcopy(snapshot)
    segment = segment_in(changed, segment_id)
    validate_value(property_name, time_us, value, segment['target_timerange']['duration'])
    native_time_us = native_time(changed, segment, time_us)
    groups = segment.get('common_keyframes')
    if not isinstance(groups, list):
        raise NativeServiceError('Native segment has no comparable keyframe list')
    if property_name == 'scale':
        scale = segment.get('clip', {}).get('scale', {})
        if (segment.get('uniform_scale', {}).get('on') is not True or
                set(scale) != {'x', 'y'} or scale['x'] != scale['y'] or
                any(g.get('property_type') == 'KFTypeScaleY' for g in groups)):
            raise NativeServiceError('Scale motion requires uniform scaling and no independent Y curve')
    native_property = PROPERTIES[property_name][0]
    matching = [g for g in groups if g.get('property_type') == native_property]
    if len(matching) > 1:
        raise NativeServiceError('Native keyframe property group is ambiguous')
    if matching:
        group = matching[0]
    else:
        group = {'id': str(uuid.uuid4()).upper(), 'keyframe_list': [],
                 'material_id': '', 'property_type': native_property}
        groups.append(group)
    points = group.get('keyframe_list')
    if not isinstance(points, list) or any(p.get('time_offset') == native_time_us for p in points):
        raise NativeServiceError('Keyframe point list is invalid or target time is occupied')
    points.append({'curveType': 'Line', 'graphID': '', 'id': str(uuid.uuid4()).upper(),
                   'left_control': {'x': 0, 'y': 0}, 'right_control': {'x': 0, 'y': 0},
                   'string_value': '', 'time_offset': native_time_us, 'values': [float(value)]})
    points.sort(key=lambda p: p['time_offset'])
    return changed


def validate_added_point(before, after, segment_id, property_name, time_us, value):
    """Verify exactly one new linear point and byte-equivalent remaining JSON values."""
    previous, current = copy.deepcopy(before), copy.deepcopy(after)
    old, new = segment_in(previous, segment_id), segment_in(current, segment_id)
    validate_value(property_name, time_us, value, old['target_timerange']['duration'])
    native_time_us = native_time(previous, old, time_us)
    native_property = PROPERTIES[property_name][0]
    old_groups, new_groups = old.get('common_keyframes', []), new.get('common_keyframes', [])
    old_matches = [g for g in old_groups if g.get('property_type') == native_property]
    new_matches = [g for g in new_groups if g.get('property_type') == native_property]
    if len(old_matches) > 1 or len(new_matches) != 1:
        raise NativeServiceError('Native keyframe property group is missing or ambiguous')
    old_points = old_matches[0].get('keyframe_list', []) if old_matches else []
    new_points = new_matches[0].get('keyframe_list', [])
    old_ids = {p.get('id') for p in old_points}
    new_ids = [p.get('id') for p in new_points]
    if (any(not isinstance(i, str) or not i for i in new_ids) or
            len(new_ids) != len(set(new_ids)) or len(new_points) != len(old_points) + 1):
        raise NativeServiceError('Native edit did not add exactly one uniquely identified point')
    added = [p for p in new_points if p['id'] not in old_ids]
    if len(added) != 1 or any(p not in new_points for p in old_points):
        raise NativeServiceError('Native edit changed an existing keyframe')
    point = added[0]
    expected = {'curveType': 'Line', 'graphID': '', 'id': point['id'],
                'left_control': {'x': 0, 'y': 0}, 'right_control': {'x': 0, 'y': 0},
                'string_value': '', 'time_offset': native_time_us, 'values': [float(value)]}
    if point != expected:
        raise NativeServiceError('Native keyframe point differs from the request')
    new_matches[0]['keyframe_list'] = [p for p in new_points if p['id'] != point['id']]
    if not old_matches:
        # A new group must not introduce hidden resource references/extra fields.
        if (set(new_matches[0]) != {'id', 'keyframe_list', 'material_id', 'property_type'} or
                new_matches[0]['material_id'] != '' or not new_matches[0]['id']):
            raise NativeServiceError('Unexpected fields in the new native keyframe group')
        new['common_keyframes'] = [g for g in new_groups if g is not new_matches[0]]
    if current != previous:
        raise NativeServiceError('Native keyframe edit changed other draft content')
