"""Native whole-timeline visual sampling; a scan is not continuous viewing."""
import argparse
import json
from pathlib import Path
import sys

from autocut_task import read, write, load_task, assert_sources, sample_source
from native_draft_codec import file_digest


def grid(duration_us, interval_us):
    if type(duration_us) is not int or duration_us<=0 or type(interval_us) is not int or interval_us<=0:
        raise ValueError('Positive integer duration and interval required')
    if (duration_us+interval_us-1)//interval_us>10000:
        raise ValueError('Split a large review into bounded tasks')
    points=list(range(0,duration_us,interval_us))
    if points[-1]!=duration_us-1:points.append(duration_us-1)
    return points


def sheets(frames, output, sid):
    from PIL import Image, ImageDraw, ImageFont
    output.mkdir()
    font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf',22)
    pages=[]
    for page,start in enumerate(range(0,len(frames),12)):
        group=frames[start:start+12];tw,th=360,684
        canvas=Image.new('RGB',(tw*3,th*((len(group)+2)//3)),(22,24,29));draw=ImageDraw.Draw(canvas)
        for i,row in enumerate(group):
            with Image.open(row['path']) as source:
                thumb=source.convert('RGB');thumb.thumbnail((tw,640))
            x,y=(i%3)*tw,(i//3)*th
            canvas.paste(thumb,(x+(tw-thumb.width)//2,y+40))
            draw.text((x+8,y+8),sid+'  '+f"{row['native_pts_us']/1e6:.3f}s",font=font,fill='white')
        path=output/f'{sid}-page-{page+1:02d}.jpg';canvas.save(path,quality=94)
        pages.append({'path':str(path),'sha256':file_digest(path),'frame_paths':[r['path'] for r in group]})
    return pages


def scan(task, source_id, install, output, interval_us=1000000, width=360):
    folder,state,inventory=load_task(task);assert_sources(inventory)
    row=next(r for r in inventory['sources'] if r['id']==source_id)
    meta=row['metadata']
    if not meta['has_video'] or meta['is_image']:raise ValueError('Choose video')
    duration=(int(meta['video_duration']*30)-1)*1000000//30
    points=grid(duration,interval_us)
    output=Path(output).resolve()
    if output.exists():raise FileExistsError('Scan output must be new')
    output.mkdir(parents=True)
    existing={}
    for entry in state['observations']:
        if entry['source']!=source_id:continue
        path=Path(entry['report'])
        if file_digest(path)!=entry['sha256']:raise ValueError('Observation changed')
        obs=read(path)
        if obs['source_sha256']!=row['sha256']:raise ValueError('Observation/source mismatch')
        for f in obs['frames']:
            if file_digest(Path(f['path']))!=f['sha256']:raise ValueError('Frame changed')
            t=f['requested_us']
            if t not in existing or f['width']>existing[t]['width']:existing[t]=f
    missing=[t for t in points if t not in existing]
    for offset in range(0,len(missing),20):
        result=sample_source(task,source_id,install,missing[offset:offset+20],width=width)
        for f in result['frames']:existing[f['requested_us']]=f
        print(json.dumps({'source':source_id,'new_frames_ready':min(offset+20,len(missing)),
                          'new_frames_total':len(missing)},ensure_ascii=False),flush=True)
    frames=[existing[t] for t in points]
    report={'schema':'jianying-material-scan-v1','source_id':source_id,'source_path':row['path'],
            'source_sha256':row['sha256'],'source_duration_us':round(meta['video_duration']*1000000),
            'observation_duration_us':duration,'interval_us':interval_us,'frames':frames,
            'largest_requested_gap_us':max(b-a for a,b in zip(points,points[1:])) if len(points)>1 else 0,
            'pages':sheets(frames,output/'pages',source_id),'status':'captured_not_yet_reviewed',
            'continuous_viewing':False,'audio_content_verified':False,
            'limits':'Inter-sample events and last observation-tail margin are not fully observed. Inspect originals for fine text/focus.'}
    assert_sources(inventory)
    write(output/'scan.json',report)
    return {'report':str(output/'scan.json'),'source_id':source_id,'frames':len(frames),'new_frames':len(missing),'pages':len(report['pages'])}


def validate_map(scan_path, map_path):
    report,mapping=read(scan_path),read(map_path)
    errors=[]
    if mapping.get('source_id')!=report['source_id']:errors.append('Source identity mismatch')
    if mapping.get('scan_sha256')!=file_digest(Path(scan_path)):errors.append('Scan hash mismatch')
    if file_digest(Path(report['source_path']))!=report['source_sha256']:errors.append('Source changed')
    for frame in report['frames']:
        if file_digest(Path(frame['path']))!=frame['sha256']:errors.append('Frame changed')
    for page in report['pages']:
        if file_digest(Path(page['path']))!=page['sha256']:errors.append('Contact sheet changed')
    planned=grid(report['observation_duration_us'],report['interval_us'])
    expected_duration=(report['source_duration_us']*30//1000000-1)*1000000//30
    if abs(report['observation_duration_us']-expected_duration)>1:
        errors.append('Observation duration does not match the source and native tail margin')
    actual=[f['requested_us'] for f in report['frames']]
    if actual!=planned:errors.append('Scan frames do not match the full planned temporal grid')
    if any(abs(f['native_pts_us']-f['requested_us'])>33335 for f in report['frames']):
        errors.append('Native frame timestamp exceeds observation-frame tolerance')
    if any(not 0<=f['native_pts_us']<report['observation_duration_us'] for f in report['frames']):
        errors.append('Native frame timestamp outside observation duration')
    expected={f['path'] for f in report['frames']}
    times={f['path']:f['native_pts_us'] for f in report['frames']}
    reviewed=mapping.get('reviewed_frame_paths',[])
    if set(reviewed)!=expected or len(reviewed)!=len(set(reviewed)):
        errors.append('Every scan frame must be actually reviewed and listed exactly once')
    if mapping.get('continuous_viewing') is not False or mapping.get('audio_content_verified') is not False:
        errors.append('Visual sampling cannot certify continuous viewing or audio semantics')
    sections=mapping.get('sections',[])
    if not sections:errors.append('Timeline map needs sections')
    end=0
    for section in sections:
        start,next_end=section.get('start_us'),section.get('end_us')
        if type(start) is not int or type(next_end) is not int or start!=end or next_end<=start:
            errors.append('Timeline sections must cover full source consecutively')
        for key in ('content','editorial_use','limits'):
            if not isinstance(section.get(key),str) or not section[key].strip():errors.append('Section missing '+key)
        refs=section.get('evidence_frame_paths',[])
        if not refs or any(p not in expected for p in refs):errors.append('Section lacks real source frame evidence')
        if type(start) is int and type(next_end) is int and not any(
                p in times and start<=times[p]<next_end for p in refs):
            errors.append('Section requires evidence from within its own time range')
        end=next_end
    if end!=report['source_duration_us']:errors.append('Timeline map must cover the full source; tail limits must be explicit')
    return {'status':'map_structure_valid' if not errors else 'invalid','errors':errors,
            'full_timeline_mapped':not errors,'continuous_viewing':False,'semantic_truth_verified_by_tool':False}


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='command',required=True)
    s=sub.add_parser('scan');s.add_argument('--task',required=True);s.add_argument('--source-id',required=True)
    s.add_argument('--install-dir',required=True);s.add_argument('--out',required=True)
    s.add_argument('--interval-us',type=int,default=1000000);s.add_argument('--width',type=int,default=360)
    s=sub.add_parser('validate-map');s.add_argument('--scan',required=True);s.add_argument('--map',required=True);s.add_argument('--out',required=True)
    a=p.parse_args()
    try:
        if a.command=='scan':result=scan(a.task,a.source_id,a.install_dir,a.out,a.interval_us,a.width)
        else:result=validate_map(a.scan,a.map);write(a.out,result)
        print(json.dumps(result,ensure_ascii=False,indent=2));return 2 if result.get('errors') else 0
    except Exception as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr);return 2


if __name__=='__main__':raise SystemExit(main())
