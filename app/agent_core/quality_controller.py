"""Independent readback checks for the exact native draft we deliver."""
from __future__ import annotations

import json
from pathlib import Path

from .task_store import digest


def check_native_draft(snapshot: dict, timeline: dict, structural_audit: dict,
                       *, editable_captions: bool, source_sha256: str | None = None,
                       source_has_audio: bool = False, source_manifest: dict | None = None,
                       packaging_report: dict | None = None):
    issues = list(structural_audit.get('issues', []))
    intentional = []
    native_packaging = bool(packaging_report and packaging_report.get('schema') == 'jianying-native-task-packaging-v1')
    separated_native = native_packaging and packaging_report.get('source_audio_mode')=='separated_by_native_cli'
    if separated_native:
        muted_ids={r['source_segment_id'] for r in packaging_report.get('narration_segments',[])}
        intentional=[i for i in issues if i['code']=='segment_muted' and i.get('segment_id') in muted_ids]
        issues=[i for i in issues if i not in intentional]
    if packaging_report and not native_packaging:
        muted_ids = {s['id'] for t in packaging_report['original_tracks'] if t['type']=='video' for s in t['segments']}
        transition_ids = {r['segment_id'] for r in packaging_report['items'] if r['role']=='transitions'}
        muted_ids.update(r['segment_id'] for r in packaging_report['items'] if r['role'] in ('stickers','transitions'))
        intentional = [i for i in issues if
            (i['code']=='segment_muted' and i.get('segment_id') in muted_ids) or
            (i['code']=='a_v_mapping_offset_hint' and i.get('evidence',{}).get('video_segment_id') in transition_ids)]
        issues = [i for i in issues if i not in intentional]
    duration = snapshot.get('duration')
    expected_duration = timeline['duration_us']
    if type(duration) is not int or abs(duration - expected_duration) > 34_000:
        issues.append({'code': 'duration_mismatch', 'severity': 'blocking',
                       'expected_us': expected_duration, 'actual_us': duration})
    main_id = (next(t['id'] for t in packaging_report['original_tracks'] if t['type']=='video')
               if packaging_report else None)
    video = sorted((segment for track in snapshot.get('tracks', []) if track.get('type') == 'video'
                    and (main_id is None or track['id'] == main_id)
                    for segment in track.get('segments', [])),
                   key=lambda segment: segment['target_timerange']['start'])
    video_materials = {item['id']: item for item in snapshot.get('materials', {}).get('videos', [])}
    referenced_materials = {item.get('material_id') for item in video}
    missing_materials = referenced_materials - video_materials.keys()
    if missing_materials:
        issues.append({'code': 'video_material_missing', 'severity': 'blocking',
                       'material_ids': sorted(missing_materials, key=str)})
    if source_sha256 is not None:
        for material_id in sorted(referenced_materials & video_materials.keys(), key=str):
            material = video_materials[material_id]
            source_path = material.get('path') or material.get('media_path')
            actual_hash = (digest(Path(source_path)) if source_path and
                           Path(source_path).is_file() else None)
            if actual_hash != source_sha256:
                issues.append({'code': 'video_material_hash_mismatch', 'severity': 'blocking',
                               'material_id': material_id, 'expected_sha256': source_sha256,
                               'actual_sha256': actual_hash})
    if source_has_audio and (not packaging_report or native_packaging) and not separated_native:
        for index, segment in enumerate(video, 1):
            material = video_materials.get(segment.get('material_id'), {})
            if material.get('has_audio') is not True or segment.get('volume', 1) <= 0:
                issues.append({'code': 'embedded_audio_missing_or_muted',
                               'severity': 'blocking', 'clip_index': index})
    expected = timeline['source_map']
    if len(video) != len(expected):
        issues.append({'code': 'video_segment_count_mismatch', 'severity': 'blocking',
                       'expected': len(expected), 'actual': len(video)})
    else:
        for index, (actual, mapped) in enumerate(zip(video, expected), 1):
            if source_manifest is not None:
                expected_asset = source_manifest.get(mapped.get('asset_id'))
                material = video_materials.get(actual.get('material_id'), {})
                source_path = material.get('path') or material.get('media_path')
                actual_hash = digest(Path(source_path)) if source_path and Path(source_path).is_file() else None
                if not expected_asset or actual_hash != expected_asset['sha256']:
                    issues.append({'code': 'video_material_hash_mismatch', 'severity': 'blocking',
                                   'clip_index': index, 'asset_id': mapped.get('asset_id'),
                                   'actual_sha256': actual_hash})
                if (not packaging_report or native_packaging) and not separated_native and expected_asset and expected_asset.get('has_audio') and (
                        material.get('has_audio') is not True or actual.get('volume', 1) <= 0):
                    issues.append({'code': 'embedded_audio_missing_or_muted', 'severity': 'blocking',
                                   'clip_index': index})
            target = actual.get('target_timerange') or {}
            source = actual.get('source_timerange') or {}
            actual_end = target.get('start', -1) + target.get('duration', -1)
            source_end = source.get('start', -1) + source.get('duration', -1)
            if (target.get('start') != mapped['timeline_start_us'] or
                    actual_end != mapped['timeline_end_us'] or
                    source.get('start') != mapped['source_start_us'] or
                    source_end != mapped['source_end_us']):
                issues.append({'code': 'video_time_mapping_mismatch', 'severity': 'blocking',
                               'clip_index': index, 'source_segment_id': mapped['source_segment_id'],
                               'expected': mapped, 'actual_target': target, 'actual_source': source})
    native = sorted((segment for track in snapshot.get('tracks', []) if track.get('type') == 'text'
                     for segment in track.get('segments', [])),
                    key=lambda segment: segment['target_timerange']['start'])
    if native_packaging:
        if separated_native:
            audio={s['id']:s for t in snapshot['tracks'] if t['type']=='audio' for s in t['segments']}
            records=packaging_report.get('narration_segments',[])
            if len(records)!=len(video) or {r['source_segment_id'] for r in records}!={s['id'] for s in video}:
                issues.append({'code':'separated_narration_count_mismatch','severity':'blocking'})
            for record in records:
                seg=audio.get(record['segment_id'],{}); time=record['target_timerange']; target=seg.get('target_timerange',{})
                if (target.get('start',0)!=time.get('start',0) or target.get('duration')!=time['duration']
                        or abs(seg.get('volume',0)-record['volume'])>1e-6):
                    issues.append({'code':'separated_narration_mapping_mismatch','severity':'blocking'})
            if any(s.get('volume',1)!=0 for s in video):
                issues.append({'code':'double_source_audio','severity':'blocking'})
        # These added headings are verified against the typed native request;
        # they are not transcript captions and must not alter their count.
        added = {r['segment_id'] for r in packaging_report['items'] if r['kind']=='text'}
        native = [s for s in native if s['id'] not in added]
        all_segments = {s['id']:s for t in snapshot['tracks'] for s in t['segments']}
        all_materials = {m['id']:m for group in snapshot['materials'].values() if isinstance(group,list)
                         for m in group if isinstance(m,dict) and m.get('id')}
        for item in packaging_report['items']:
            seg = all_segments.get(item['segment_id']); row = item['request']; kind = item['kind']
            valid = bool(seg)
            if valid and kind == 'transition':
                found = [m for m in snapshot['materials'].get('transitions',[])
                    if m['id'] in seg.get('extra_material_refs',[]) and m.get('resource_id')==row['resource_id']
                    and m.get('duration')==row['duration_us']]
                valid = len(found)==1
            elif valid:
                valid = seg['target_timerange'] == {'start':row['start_us'],'duration':row['duration_us']}
                mat = all_materials.get(seg.get('material_id'),{})
                if kind == 'text':
                    try: valid = valid and json.loads(mat['content'])['text']==row['text']
                    except (KeyError,ValueError,TypeError): valid = False
                elif kind in ('sticker','effect','sound','music','filter'):
                    valid = valid and str(mat.get('resource_id'))==row['resource_id']
                if kind in ('sound','music'):
                    valid = valid and abs(seg.get('volume',1)-row['volume'])<1e-6
            if not valid:
                issues.append({'code':'native_packaging_object_mismatch','severity':'blocking',
                               'segment_id':item['segment_id'],'kind':kind})
    if not editable_captions and native:
        issues.append({'code': 'unexpected_text_segments', 'severity': 'blocking',
                       'actual': len(native)})
    if editable_captions:
        materials = {item['id']: item for item in snapshot.get('materials', {}).get('texts', [])}
        captions = timeline['captions']
        if len(native) != len(captions):
            issues.append({'code': 'caption_count_mismatch', 'severity': 'blocking',
                           'expected': len(captions), 'actual': len(native)})
        else:
            for index, (segment, cue) in enumerate(zip(native, captions), 1):
                material = materials.get(segment.get('material_id'))
                try:
                    actual_text = json.loads(material['content'])['text']
                except (TypeError, KeyError, ValueError):
                    actual_text = None
                target = segment.get('target_timerange') or {}
                if (target.get('start') != cue['start_us'] or
                        target.get('duration') != cue['end_us'] - cue['start_us'] or
                        actual_text != cue['text']):
                    issues.append({'code': 'caption_mapping_mismatch', 'severity': 'blocking',
                                   'caption_index': index, 'source_cue_id': cue['source_cue_id'],
                                   'expected_text': cue['text'], 'actual_text': actual_text,
                                   'expected_range': [cue['start_us'], cue['end_us']],
                                   'actual_range': target})
    if packaging_report and not native_packaging:
        # Cross-check independent sound against the compiled mapping, not merely presence of a track.
        sound = [row for row in packaging_report['items'] if row['role']=='original_audio']
        native_audio = {s['id']:s for t in snapshot.get('tracks',[]) if t.get('type')=='audio'
                        for s in t.get('segments',[])}
        expected_sound = [mapped for actual,mapped in zip(video,expected)
            if (source_manifest.get(mapped.get('asset_id'),{}).get('has_audio') if source_manifest is not None
                else source_has_audio or video_materials.get(actual.get('material_id'),{}).get('has_audio'))]
        if len(sound) != len(expected_sound):
            issues.append({'code':'separated_audio_count_mismatch','severity':'blocking'})
        for row, mapped in zip(sound, expected_sound):
            seg = native_audio.get(row['segment_id'], {})
            if (seg.get('target_timerange') != {'start':mapped['timeline_start_us'], 'duration':mapped['timeline_end_us']-mapped['timeline_start_us']} or
                seg.get('source_timerange') != {'start':mapped['source_start_us'], 'duration':mapped['source_end_us']-mapped['source_start_us']} or
                seg.get('volume',0) <= 0):
                issues.append({'code':'separated_audio_mapping_mismatch','severity':'blocking'})
        if any(s.get('volume',1) != 0 for s in video):
            issues.append({'code':'double_source_audio','severity':'blocking'})
    return {'schema_version': '1.0', 'status': 'technical_clear_with_limits' if not issues else 'technical_issues',
            'issues': issues, 'duration_match': isinstance(duration, int) and
            abs(duration - expected_duration) <= 34_000,
            'video_mapping_match': not any(item['code'].startswith('video_') for item in issues),
            'caption_mapping_match': not any(item['code'].startswith('caption_') for item in issues),
            'structural_scope': structural_audit.get('scope'),
            'intentional_packaging_observations': intentional,
            'embedded_audio_metadata_checked': source_has_audio or source_manifest is not None,
            'narrative_verified': False, 'continuous_video_reviewed': False,
            'final_audio_listened': False, 'lip_sync_verified': False}
