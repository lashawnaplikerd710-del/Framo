"""Compile curated speech/story intervals into native timeline plans and SRT.

This compiles already selected content; it does not transcribe, rank highlights,
render subtitles, start Jianying, register drafts, or export video.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re
import sys
import tempfile

from common import PlanError, local_file, media_info
from compile_keep import parse_srt
from native_draft_codec import file_digest
from native_timeline import load_plan as validate_native_plan


def read_strict(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PlanError(f'Duplicate field: {key}')
            result[key] = value
        return result
    def constant(value):
        raise PlanError(f'Non-finite JSON value: {value}')
    return json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=pairs,
                      parse_constant=constant)


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) or set(value) - set(required) - set(optional):
        raise PlanError(f'Expected fields {sorted(required)}, optional {sorted(optional)}')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,60}', value):
        raise PlanError('IDs must contain 1..60 ASCII letters, digits, underscores or hyphens')
    return value


def timestamp(milliseconds):
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}'


def compile_story(selection_path, output_path):
    selection = Path(selection_path).resolve(strict=True)
    output = Path(output_path).resolve()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Choose a new output directory')
    spec = read_strict(selection)
    fields(spec, {'schema', 'canvas', 'sources', 'edits'})
    if spec['schema'] != 'jianying-story-v1':
        raise PlanError('Expected schema jianying-story-v1')
    if not isinstance(spec['sources'], list) or not 1 <= len(spec['sources']) <= 100:
        raise PlanError('Choose 1..100 source videos')
    if not isinstance(spec['edits'], list) or not 1 <= len(spec['edits']) <= 100:
        raise PlanError('Choose 1..100 edits')
    sources, source_ids, source_hashes = {}, set(), {}
    for item in spec['sources']:
        fields(item, {'id', 'path'}, {'srt'})
        identity = identifier(item['id'])
        if identity.casefold() in source_ids:
            raise PlanError('Source IDs must be unique, including case-insensitive matches')
        source_ids.add(identity.casefold())
        path = Path(local_file(item['path'], selection.parent))
        if str(path).startswith(('\\\\', '//')):
            raise PlanError('Resolved network sources are unsupported')
        metadata = media_info(path)
        if not metadata['has_video'] or metadata['is_image'] or not math.isfinite(metadata['duration']) or metadata['duration'] <= 0:
            raise PlanError('Every source must be a video with a measured positive duration')
        if metadata['rotation']:
            raise PlanError('Rotated source metadata needs an explicitly verified adaptation')
        duration = round((metadata.get('video_duration') or metadata['duration']) * 1_000_000)
        source_hashes[str(path)] = file_digest(path)
        cues = []
        if item.get('srt') is not None:
            subtitle = Path(local_file(item['srt'], selection.parent))
            if str(subtitle).startswith(('\\\\', '//')):
                raise PlanError('Resolved network subtitles are unsupported')
            source_hashes[str(subtitle)] = file_digest(subtitle)
            for cue in parse_srt(subtitle.read_text(encoding='utf-8-sig')):
                start, end = round(cue['start'] * 1_000_000), round(cue['end'] * 1_000_000)
                if start < 0 or end > duration:
                    raise PlanError(f'{identity}: subtitle cue {cue["cue"]} exceeds source duration')
                cues.append(dict(cue, start_us=start, end_us=end))
        sources[identity] = {'path': str(path), 'duration_us': duration, 'cues': cues,
                             'transcript_provided': item.get('srt') is not None,
                             'has_audio': metadata['has_audio']}
    compiled, edit_ids = [], set()
    for edit in spec['edits']:
        fields(edit, {'id', 'title', 'segments'})
        identity = identifier(edit['id'])
        if identity.casefold() in edit_ids:
            raise PlanError('Edit IDs must be unique, including case-insensitive matches')
        edit_ids.add(identity.casefold())
        if not isinstance(edit['title'], str) or not edit['title'].strip():
            raise PlanError('Each edit needs a nonempty title')
        if not isinstance(edit['segments'], list) or not 1 <= len(edit['segments']) <= 500:
            raise PlanError('Each edit needs 1..500 selected intervals')
        native = {'schema': 'jianying-native-timeline-v1', 'name': edit['title'],
                  'canvas': spec['canvas'], 'steps': []}
        mapping, mapped_cues, cursor, used_sources = [], [], 0, set()
        for index, segment in enumerate(edit['segments'], 1):
            fields(segment, {'source', 'in_us', 'out_us'})
            sid = segment['source']
            if not isinstance(sid, str) or sid not in sources:
                raise PlanError('Segment references an unknown source ID')
            src = sources[sid]
            start, end = segment['in_us'], segment['out_us']
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= src['duration_us']:
                raise PlanError(f'{identity}/{index}: source interval is invalid or out of bounds')
            duration = end - start
            native['steps'].append({'action': 'video.add', 'id': f'clip_{index:03d}',
                'path': src['path'], 'track': 0, 'start_us': cursor,
                'duration_us': duration, 'source_start_us': start})
            mapping.append({'clip': index, 'source': sid, 'path': src['path'],
                'in_us': start, 'out_us': end, 'timeline_start_us': cursor, 'duration_us': duration})
            used_sources.add(sid)
            for cue in src['cues']:
                left, right = max(start, cue['start_us']), min(end, cue['end_us'])
                if right <= left:
                    continue
                if left != cue['start_us'] or right != cue['end_us']:
                    raise PlanError(f'{identity}/{index} cuts through {sid} SRT cue {cue["cue"]}; '
                                    'adjust to sentence boundaries or supply corrected word-aligned subtitles')
                target_start, target_end = cursor + left - start, cursor + right - start
                if round(target_end / 1000) <= round(target_start / 1000):
                    raise PlanError('Subtitle would disappear at SRT millisecond precision')
                mapped_cues.append({'source': sid, 'source_cue': cue['cue'], 'clip': index,
                                    'start_us': target_start, 'end_us': target_end, 'text': cue['text']})
            cursor += duration
        mapped_cues.sort(key=lambda c: (c['start_us'], c['end_us'], c['clip']))
        occurrence = Counter((c['source'], c['source_cue']) for c in mapped_cues)
        mapped_srt = '\n\n'.join(f'{n}\n{timestamp(round(c["start_us"]/1000))} --> '
            f'{timestamp(round(c["end_us"]/1000))}\n{c["text"]}'
            for n, c in enumerate(mapped_cues, 1))
        compiled.append({'id': identity, 'plan': native, 'mapping': mapping,
            'captions': mapped_cues, 'srt': mapped_srt + ('\n' if mapped_srt else ''),
            'duration_us': cursor,
            'sources_without_transcript': sorted(sid for sid in used_sources if not sources[sid]['transcript_provided']),
            'sources_without_audio': sorted(sid for sid in used_sources if not sources[sid]['has_audio']),
            'repeated_cues': [{'source': sid, 'cue': cue, 'occurrences': count}
                              for (sid, cue), count in occurrence.items() if count > 1]})
    # Validate every native plan before creating any final output. The temporary
    # directory is exclusively created/owned by tempfile and contains only plans.
    with tempfile.TemporaryDirectory(prefix='jianying-story-validation-') as temp:
        for item in compiled:
            test = Path(temp) / (item['id'] + '.json')
            test.write_text(json.dumps(item['plan'], ensure_ascii=False), encoding='utf-8')
            validate_native_plan(test)
    if any(file_digest(Path(path)) != digest for path, digest in source_hashes.items()):
        raise PlanError('Source video or subtitles changed during compilation')
    output.mkdir(parents=True, exist_ok=False)
    def write_json(path, value):
        with path.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
    manifest = {'schema': 'jianying-story-batch-v1', 'status': 'plans_compiled_not_rendered',
                'selection': str(selection), 'source_sha256': source_hashes, 'edits': [],
                'starts_editor': False, 'ffmpeg_invoked': False,
                'subtitles_written_to_native_drafts': False}
    for item in compiled:
        folder = output / ('edit-' + item['id'])
        folder.mkdir()
        write_json(folder/'timeline.json', item['plan'])
        write_json(folder/'source-map.json', item['mapping'])
        write_json(folder/'captions.json', item['captions'])
        if item['captions']:
            (folder/'captions.srt').write_text(item['srt'], encoding='utf-8')
        manifest['edits'].append({'id': item['id'], 'title': item['plan']['name'],
            'native_plan': str(folder/'timeline.json'), 'duration_us': item['duration_us'],
            'clip_count': len(item['mapping']), 'caption_count': len(item['captions']),
            'srt': str(folder/'captions.srt') if item['captions'] else None,
            'sources_without_transcript': item['sources_without_transcript'],
            'sources_without_audio': item['sources_without_audio'], 'repeated_cues': item['repeated_cues'],
            'semantic_review': 'Required: sentence-complete cuts do not prove faithful context',
            'caption_render_status': 'not_applied_to_native_draft'})
    write_json(output/'batch-manifest.json', manifest)
    return manifest


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--selection', required=True, help='Curated jianying-story-v1 JSON')
    parser.add_argument('--out', required=True, help='New directory for native plans and remapped SRT')
    args = parser.parse_args()
    try:
        report = compile_story(args.selection, args.out)
        print(json.dumps({'status': report['status'], 'edits': len(report['edits']),
                          'manifest': str(Path(args.out).resolve()/'batch-manifest.json')}, ensure_ascii=False))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
