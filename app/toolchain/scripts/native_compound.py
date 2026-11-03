"""Wrap an explicit draft snapshot into a real editable nested native clip.

Uses the installed native service for graph loading, encrypted persistence,
reloading, and frame capture. No GUI, FFmpeg, cloud operation or resource lookup.
The verified operation wraps the entire supplied timeline once; it does not
silently select or discard clips. A source already containing nested drafts is
rejected because this service build loses a second nesting level on normalization.
A fixed canvas ratio is required; original-ratio root and nested rendering differ
in this native build, so that mode is rejected instead of changing the picture.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import shutil
import struct
import sys
import time
import uuid
import zlib

from native_service import Service, NativeServiceError
from native_draft_codec import file_digest, validate_json
from native_build import sidecar_metadata


def uid():
    return str(uuid.uuid4()).upper()


def dump(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def all_drafts(snapshot, depth=0):
    if depth > 8:
        raise ValueError('The compound adapter supports at most eight nesting levels')
    yield snapshot
    for material in snapshot.get('materials', {}).get('drafts', []):
        child = material.get('draft')
        if isinstance(child, dict):
            yield from all_drafts(child, depth + 1)


def graph_shape(snapshot):
    return [{'draft_id': draft.get('id'), 'tracks': [
        {'id': track.get('id'), 'type': track.get('type'),
         'segments': [{'id': segment.get('id'), 'material_id': segment.get('material_id'),
                       'target_timerange': segment.get('target_timerange')}
                      for segment in track.get('segments', [])]}
        for track in draft.get('tracks', [])]} for draft in all_drafts(snapshot)]


def copy_media(snapshot, source_json, destination):
    """Copy only the explicit snapshot's local video/audio files; preserve other fields."""
    destination.mkdir(exist_ok=False)
    manifest, cache = [], {}
    for draft in all_drafts(snapshot):
        materials = draft.get('materials', {})
        for kind in ('videos', 'audios'):
            for material in materials.get(kind, []):
                raw = material.get('path') or material.get('media_path')
                if not raw:
                    # Real compound video materials intentionally have an empty path.
                    if kind == 'videos' and material.get('extra_type_option', 0) & 2:
                        continue
                    raise ValueError('A non-compound media material has no local file path')
                if not isinstance(raw, str) or '://' in raw or raw.startswith(('\\\\', '//')):
                    raise ValueError('Compound packaging requires explicit local media files')
                path = Path(raw)
                if not path.is_absolute():
                    path = source_json.parent / path
                # Packaged snapshots may be moved as a unit; check their own media folders.
                if not path.is_file():
                    candidates = [source_json.parent/'draft/media'/Path(raw).name,
                                  source_json.parent/'media'/Path(raw).name]
                    present = [candidate for candidate in candidates if candidate.is_file()]
                    if len(present) != 1:
                        raise FileNotFoundError(f'Explicit source media is unavailable: {raw}')
                    path = present[0]
                path = path.resolve(strict=True)
                if not path.is_file() or str(path).startswith(('\\\\', '//')):
                    raise ValueError('Expected a regular local media file')
                digest = file_digest(path)
                key = (str(path), digest)
                if key not in cache:
                    target = destination / (digest[:24] + path.suffix.lower())
                    if target.exists():
                        if file_digest(target) != digest:
                            raise ValueError('Media filename hash collision')
                    else:
                        shutil.copyfile(path, target)
                    if file_digest(target) != digest:
                        raise NativeServiceError('Copied media hash mismatch')
                    cache[key] = target
                    manifest.append({'source': str(path), 'path': str(target), 'sha256': digest})
                material['path'] = str(cache[key])
                if kind == 'videos' or 'media_path' in material:
                    material['media_path'] = str(cache[key])
    return manifest


def wrap_snapshot(child, name):
    """Persisted form used by the shipped MaterialDraft/VideoDocument serializer."""
    duration = child.get('duration')
    if not isinstance(name, str) or not name.strip():
        raise ValueError('A nonempty compound name is required')
    if any(isinstance(material.get('draft'), dict)
           for material in child.get('materials', {}).get('drafts', [])):
        raise ValueError('Second-level nesting is not verified in this native service build; input is preserved')
    if type(duration) is not int or not 0 < duration <= 86_400_000_000:
        raise ValueError('Source draft requires a positive duration within 24 hours')
    videos = [segment for track in child.get('tracks', []) if track.get('type') == 'video'
              for segment in track.get('segments', [])]
    if not videos:
        raise ValueError('This verified wrapper requires a source video/image track')
    canvas = child.get('canvas_config', {})
    if not isinstance(canvas.get('ratio'), str) or canvas['ratio'] in ('', 'original'):
        raise ValueError('Compound wrapping requires an explicit fixed canvas ratio; original-ratio rendering is not verified')
    width, height = canvas.get('width'), canvas.get('height')
    if any(type(value) is not int or not 1 <= value <= 8192 for value in (width, height)):
        raise ValueError('Source canvas dimensions are invalid')
    outer = copy.deepcopy(child)
    outer.update(id=uid(), name=name, create_time=int(time.time()*1000000),
                 update_time=int(time.time()*1000000))
    nested_id, video_id, segment_id = uid(), uid(), uid()
    nested = {'id': nested_id, 'type': 'combination', 'name': name,
              'category_id': '', 'category_name': '', 'formula_id': '',
              'combination_id': uid(), 'combination_type': 'none',
              'precompile_combination': False, 'draft_file_path': '',
              'draft_cover_path': '', 'draft_config_path': '', 'draft': copy.deepcopy(child)}
    segment = copy.deepcopy(videos[0])
    segment.update(id=segment_id, material_id=video_id, extra_material_refs=[nested_id],
                   target_timerange={'start': 0, 'duration': duration},
                   source_timerange={'start': 0, 'duration': duration},
                   common_keyframes=[], keyframe_refs=[], group_id='',
                   template_id='', template_scene='default', speed=1, reverse=False,
                   is_loop=False, volume=1, visible=True, render_index=0,
                   track_render_index=0, track_attribute=0, is_placeholder=False,
                   clip={'alpha': 1, 'flip': {'horizontal': False, 'vertical': False},
                         'rotation': 0, 'scale': {'x': 1, 'y': 1},
                         'transform': {'x': 0, 'y': 0}})
    # Empty-path video + IsCombinationVideo (2) is the shipped native wrapper contract.
    video = {'id': video_id, 'type': 'video', 'path': '', 'media_path': '',
             'material_name': name, 'duration': duration, 'extra_type_option': 2,
             'width': width, 'height': height, 'has_audio': any(
                 track.get('type') == 'audio' or any(material.get('has_audio') for material in
                 draft.get('materials', {}).get('videos', []))
                 for draft in all_drafts(child) for track in draft.get('tracks', [])),
             'crop': {'lower_left_x': 0, 'lower_left_y': 1, 'lower_right_x': 1,
                      'lower_right_y': 1, 'upper_left_x': 0, 'upper_left_y': 0,
                      'upper_right_x': 1, 'upper_right_y': 0},
             'crop_ratio': 'free', 'crop_scale': 1}
    track = copy.deepcopy(next(track for track in child['tracks'] if track.get('type') == 'video'))
    track.update(id=uid(), segments=[segment], name=name, flag=0, attribute=0)
    outer['tracks'] = [track]
    outer['materials'] = {key: [] if isinstance(value, list) else copy.deepcopy(value)
                          for key, value in child['materials'].items()}
    outer['materials']['videos'] = [video]
    outer['materials']['drafts'] = [nested]
    list(all_drafts(outer))
    return outer, {'nested_material_id': nested_id, 'wrapper_segment_id': segment_id,
                   'child_draft_id': child['id']}


def assert_nested(snapshot, child, identities):
    roots = [segment for track in snapshot.get('tracks', []) for segment in track.get('segments', [])]
    if len(roots) != 1 or roots[0].get('id') != identities['wrapper_segment_id']:
        raise NativeServiceError('Native wrapper did not contain exactly one compound segment')
    matches = [material for material in snapshot.get('materials', {}).get('drafts', [])
               if material.get('id') == identities['nested_material_id']]
    if len(matches) != 1 or matches[0].get('type') != 'combination':
        raise NativeServiceError('Native compound material is missing or ambiguous')
    if identities['nested_material_id'] not in roots[0].get('extra_material_refs', []):
        raise NativeServiceError('Native wrapper does not reference its nested timeline')
    if graph_shape(matches[0]['draft']) != graph_shape(child):
        error = NativeServiceError('Native nested tracks, clips, identities or intervals changed')
        error.expected_graph = graph_shape(child)
        error.actual_graph = graph_shape(matches[0]['draft'])
        raise error
    return matches[0]['draft']


def capture(service, session, snapshot):
    canvas = snapshot['canvas_config']
    scale = min(320/canvas['width'], 320/canvas['height'])
    width, height = max(2, round(canvas['width']*scale)), max(2, round(canvas['height']*scale))
    when = max(0, min(snapshot['duration']//2, snapshot['duration']-1))
    from native_frames import prime, capture_at
    prime(service, session, snapshot['duration'], width, height)
    frame, _ = capture_at(service, session, when, width, height,
                          round(1_000_000 / snapshot.get('fps', 30)) + 1)
    return frame



def write_png(path, frame):
    """Lossless encoding of an existing native RGBA frame; no external rendering."""
    width, height, pixels = frame
    def chunk(kind, payload):
        return struct.pack('>I', len(payload))+kind+payload+struct.pack('>I', zlib.crc32(kind+payload)&0xffffffff)
    rows = b''.join(b'\0'+pixels[y*width*4:(y+1)*width*4] for y in range(height))
    data = b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,6,0,0,0))
    data += chunk(b'IDAT',zlib.compress(rows))+chunk(b'IEND',b'')
    with path.open('xb') as stream:
        stream.write(data)


def build(snapshot_path, install, output, name):
    source_path = Path(snapshot_path).resolve(strict=True)
    if not source_path.is_file() or source_path.stat().st_size > 64*1024*1024:
        raise ValueError('Supply one draft-snapshot JSON file no larger than 64 MiB')
    child = validate_json(source_path.read_bytes())
    list(all_drafts(child))
    # Reject unsupported graph structure before creating any output or starting native code.
    wrap_snapshot(child, name)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError('Output directory already exists; choose a new directory')
    output = output.resolve()
    install = Path(install).resolve(strict=True)
    if output == install or output.is_relative_to(install):
        raise ValueError('Output must be outside the installed application')
    output.mkdir(parents=True, exist_ok=False)
    draft = output/'draft'; draft.mkdir()
    report = {'status': 'in_progress', 'operation': 'wrap_entire_timeline',
              'editor_ui_acceptance': 'not_checked', 'source_snapshot': str(source_path),
              'source_snapshot_sha256': file_digest(source_path)}
    service = None
    try:
        manifest = copy_media(child, source_path, draft/'media')
        dump(output/'manifest.json', {'media': manifest})
        source_native = output/'source-native.json'
        target = draft/'draft_content.json'
        with Service(install, output/'service') as service:
            def write(path, value):
                return service.request('draft', 'writeFile', {'path': str(path), 'rootPath': str(output),
                    'content': json.dumps(value, ensure_ascii=False, allow_nan=False)})
            def load(path):
                session = service.request('player', 'create', {'kind': 'draft', 'serviceGeneration': 1})['result']['sessionId']
                try:
                    service.request('player','load',{'draftPath':str(path),'workspacePath':str(output)},session)
                except Exception:
                    service.request('player','destroy',{},session)
                    raise
                return session
            write(source_native, child)
            session = load(source_native)
            try:
                baseline = capture(service, session, child)
                write_png(output/'native-source-preview.png',baseline)
            finally:
                service.request('player','destroy',{},session)
            outer, identities = wrap_snapshot(child, name)
            report.update(identities)
            write(target, outer)
            session = load(target)
            try:
                # Native setDraft imports and normalizes the complete nested graph.
                snapshot = service.request('edit','draft.setDraft',{'draftSnapshot':outer},session)['result']['draftSnapshot']
                assert_nested(snapshot, child, identities)
                write(target, snapshot)
            finally:
                service.request('player','destroy',{},session)
            session = load(target)
            try:
                state=service.request('player','getState',{},session)['result']
                if state.get('status')!='ready' or state.get('durationUs')!=child['duration']:
                    raise NativeServiceError('Reloaded compound is not ready with expected duration')
                preview=capture(service,session,snapshot)
                write_png(output/'native-compound-preview.png',preview)
                # A neutral wrapper must preserve the native-rendered result, including nested text.
                if preview[:2]!=baseline[:2] or preview[2]!=baseline[2]:
                    changed=sum(a!=b for a,b in zip(preview[2],baseline[2]))
                    report['render_changed_components']=changed
                    raise NativeServiceError('Native frame changed after neutral compound wrapping')
                report['native_render_identical_before_after']=True
                # Verify the actual saved native graph, not only the supplied JSON.
                saved=service.request('draft','readFile',{'path':str(target),'rootPath':str(output)})['result']
                persisted=json.loads(saved['content'])
                assert_nested(persisted,child,identities)
                report['encrypted']=saved.get('encrypted') is True
                if not report['encrypted']:
                    raise NativeServiceError('Native encrypted persistence was not confirmed')
                report['native_reload_ready']=True
            finally:
                service.request('player','destroy',{},session)
            write(draft/'draft_meta_info.json', sidecar_metadata(snapshot,draft,output,int(time.time()*1000000)))
        report['native_frame_sha256']=hashlib.sha256(preview[2]).hexdigest()
        report['nested_graphs']=graph_shape(child)
        report['source_hashes_preserved']=file_digest(source_path)==report['source_snapshot_sha256'] and all(file_digest(Path(m['source']))==m['sha256'] for m in manifest)
        if not report['source_hashes_preserved']:
            raise NativeServiceError('A source file changed during native processing')
        dump(output/'draft-snapshot.json',snapshot)
        for filename in ('draft_content.json','draft_meta_info.json'):
            shutil.copyfile(draft/filename,draft/(filename+'.bak'))
        shutil.copyfile(target,draft/'template-2.tmp')
        dump(draft/'timeline_layout.json',{'dockItems':[{'dockIndex':0,'ratio':1,'timelineIds':[snapshot['id']],'timelineNames':['时间线01']}],'layoutOrientation':1})
        dump(draft/'draft_agency_config.json',{'is_auto_agency_enabled':False,
             'is_auto_agency_popup':False,'is_single_agency_mode':False,
             'marterials':None,'use_converter':False,'video_resolution':720})
        (draft/'draft_biz_config.json').touch(exist_ok=False)
        now=int(time.time())
        (draft/'draft_settings').write_text('[General]\ndraft_create_time='+str(now)+
            '\ndraft_last_edit_time='+str(now)+'\nreal_edit_seconds=0\nreal_edit_keys=0\n',encoding='utf-8')
        report.update(status='native_compound_verified',service=service.report)
        dump(output/'native-compound-report.json',report)
        return report
    except Exception as exc:
        report.update(status='failed',error=str(exc))
        if hasattr(exc, 'expected_graph'):
            report['expected_graph'] = exc.expected_graph
            report['actual_graph'] = exc.actual_graph
        if service is not None:
            report['service']=service.report
        dump(output/'native-compound-failure.json',report)
        raise


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['wrap'])
    parser.add_argument('--snapshot',required=True)
    parser.add_argument('--install-dir',required=True)
    parser.add_argument('--out',required=True)
    parser.add_argument('--name',default='CLI 复合片段')
    args=parser.parse_args()
    try:
        report=build(args.snapshot,args.install_dir,args.out,args.name)
        print(json.dumps({'status':report['status'],'report':str(Path(args.out).resolve()/'native-compound-report.json'),'native_render_identical_before_after':True,'editor_ui_acceptance':'not_checked'},ensure_ascii=False,indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())
