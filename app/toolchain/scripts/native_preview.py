"""Capture real Jianying rendered frames without launching the desktop editor."""
import argparse
import json
from pathlib import Path
import sys

from native_service import Service
from native_draft_codec import file_digest
from native_compound import write_png
from native_frames import capture_at, prime


def preview(install, draft_file, output, positions=None, width=640):
    source = Path(draft_file).resolve(strict=True)
    if source.is_dir():
        source = source / 'draft_content.json'
    output = Path(output).resolve()
    install = Path(install).resolve(strict=True)
    if output.exists() or output.is_symlink():
        raise FileExistsError('Preview output directory must be new')
    if output.is_relative_to(source.parent) or output.is_relative_to(install):
        raise ValueError('Preview output must be outside the draft and installation')
    if type(width) is not int or not 16 <= width <= 1920:
        raise ValueError('Preview width must be 16..1920')
    digest = file_digest(source)
    output.mkdir(parents=True, exist_ok=False)
    report = {'draft_file': str(source), 'frames': [], 'renderer': 'Jianying native captureFrame',
              'desktop_ui_opened': False, 'ffmpeg_invoked': False}
    with Service(install, output / 'service') as service:
        snapshot = json.loads(service.request('draft', 'readFile', {'path': str(source), 'rootPath': str(source.parent)})['result']['content'])
        duration = snapshot.get('duration')
        if type(duration) is not int or duration <= 0:
            raise ValueError('A positive native draft duration is required')
        positions = positions if positions is not None else [duration // 2]
        if (not isinstance(positions, list) or not positions or len(positions) > 20
                or any(type(t) is not int or not 0 <= t < duration for t in positions)):
            raise ValueError('Choose 1..20 integer timestamps inside the draft duration')
        canvas = snapshot.get('canvas_config', {})
        if not canvas.get('width') or not canvas.get('height'):
            raise ValueError('Native draft has no canvas dimensions')
        height = max(16, round(width * canvas['height'] / canvas['width']))
        if height > 1920:
            width, height = max(16, round(width * 1920 / height)), 1920
        session = service.request('player', 'create', {'kind': 'draft', 'serviceGeneration': 1})['result']['sessionId']
        try:
            service.request('player', 'load', {'draftPath': str(source), 'workspacePath': str(output)}, session)
            prime(service, session, duration, width, height)
            for index, position in enumerate(positions):
                frame, pts = capture_at(service, session, position, width, height,
                    tolerance_us=round(1_000_000 / snapshot.get('fps', 30)) + 1)
                path = output / f'{index:02d}-{position}us.png'
                write_png(path, frame)
                report['frames'].append({'path': str(path), 'requested_us': position, 'native_pts_us': pts,
                    'width': frame[0], 'height': frame[1], 'sha256': file_digest(path)})
        finally:
            service.request('player', 'destroy', {}, session)
    if file_digest(source) != digest:
        raise RuntimeError('Native source draft changed during preview')
    report.update(status='native_frames_captured', source_file_preserved=True, native_pixels_settled=True, service=service.report)
    with (output / 'preview-report.json').open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return report


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', required=True)
    parser.add_argument('--draft', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--time-us', type=int, action='append')
    parser.add_argument('--width', type=int, default=640)
    args = parser.parse_args()
    try:
        result = preview(args.install_dir, args.draft, args.out, args.time_us, args.width)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def preview_batches(install,draft_file,output,positions,width=640):
    """Keep every requested review position while respecting native batch limits."""
    if not isinstance(positions,list) or not 1<=len(positions)<=200:
        raise ValueError('Choose 1..200 bounded review timestamps')
    if len(positions)<=20:return preview(install,draft_file,output,positions,width)
    root=Path(output).resolve();root.mkdir(parents=True,exist_ok=False)
    frames=[];reports=[]
    for offset in range(0,len(positions),20):
        child=root/f'batch-{offset//20+1:02d}'
        report=preview(install,draft_file,child,positions[offset:offset+20],width)
        frames.extend(report['frames'])
        reports.append({'path':str(child/'preview-report.json'),'sha256':file_digest(child/'preview-report.json')})
    result={'status':'native_frames_captured','frames':frames,'batches':reports,
        'renderer':'Jianying native captureFrame','desktop_ui_opened':False,
        'source_file_preserved':True,'native_pixels_settled':True}
    (root/'preview-report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__ == '__main__':
    raise SystemExit(main())
