"""Evidence-bound, frame-exact opening for newly created original-audio jobs."""
import hashlib
import re
from difflib import SequenceMatcher

from .timeline_compiler import PlanIssue

POLICY = 'source_prefix_40s_v1'
MIN_US = 40_000_000
DEFAULT_BRIEF = ('从上传素材中挑选一条，成片前四十秒完整使用该素材开头的四十秒，'
    '连续保留画面顺序、原口播、原音乐和已有字幕，不加速、不跳段；若切点在句中，'
    '继续保留到完整语句结束。开头也在适当位置加入大量、多样的短转场、贴纸、动效、'
    '轻特效和低音量音效，避开文字、人脸和产品关键区域。之后按完整语句、问答和动作'
    '拆分素材，再按内容主题交叉重排，各素材内也调整可独立表达的顺序；不重复已经'
    '使用的开头区间。全片保留原声和画面已有字幕，不生成新配音，不添加字幕，'
    '保持讲述和画面衔接，并持续安排上述装饰元素。至少一条素材须达到四十秒。')


def active(task):
    return task.get('opening_policy') == POLICY


def _opening_features(source):
    text=re.sub(r'[^\w]', '', ''.join(u.get('text','') for u in source.get('speech_units',[])
        if u['start_us']<MIN_US)).lower()[:160]
    hashes=[]
    try:
        from PIL import Image
        frames=[f for f in source.get('frames',[]) if f.get('path') and f['requested_us']<MIN_US]
        if frames:
            for when in (0,10_000_000,20_000_000,30_000_000,39_000_000):
                frame=min(frames,key=lambda f:abs(f['requested_us']-when))
                with Image.open(frame['path']) as image:
                    pixels=list(image.convert('L').resize((9,8)).getdata())
                value=0
                for y in range(8):
                    for x in range(8):value=(value<<1)|int(pixels[y*9+x]>pixels[y*9+x+1])
                hashes.append(value)
    except (OSError,ValueError):
        hashes=[]
    return text,hashes


def _batch_opening_order(task, eligible):
    """Prefer distinct actual files and visually/textually different openings.

    Opt in only for an explicit batch-diversity request, so existing saved
    jobs retain their original deterministic opening. This is a diversity
    preference, not a claim of platform deduplication or perfect video identity.
    """
    identity='|'.join(task['asset_ids'])+task.get('instruction','')
    candidates=sorted(eligible,key=lambda s:hashlib.sha256((identity+s['asset_id']).encode()).digest())
    unique=[];seen=set()
    for source in candidates:
        fingerprint=(source.get('transcript') or {}).get('source_sha256') or source['asset_id']
        if fingerprint in seen:continue
        seen.add(fingerprint);unique.append(source)
    features={s['asset_id']:_opening_features(s) for s in unique}
    def distance(a,b):
        at,ah=features[a['asset_id']];bt,bh=features[b['asset_id']]
        text=1-SequenceMatcher(None,at,bt,autojunk=False).ratio() if at and bt else 0
        if ah and bh:
            visual=sum((x^y).bit_count()/64 for x,y in zip(ah,bh))/min(len(ah),len(bh))
            return .75*visual+.25*text
        return text
    selected=unique[:1];pending=unique[1:]
    while pending:
        source=max(pending,key=lambda s:min(distance(s,p) for p in selected))
        selected.append(source);pending.remove(source)
    return selected


def build(task, sources):
    """Choose once from eligible sources; reconstruct identically at compilation."""
    if not active(task):
        return None
    target = task['target'].get('duration_ms')
    if target is not None and target < 40_000:
        raise PlanIssue('长视频（无口播）固定保留开头四十秒，目标时长不能少于四十秒')
    eligible = []
    for source in sources:
        duration = source.get('video_duration_us') or 0
        if source.get('has_audio'):
            duration = min(duration, source.get('audio_duration_us') or 0)
        if duration >= MIN_US and source.get('frames'):
            eligible.append(source)
    if not eligible:
        raise PlanIssue('至少需要一条画面及原声达到四十秒的素材，才能保留完整开头')
    identity = '|'.join(task['asset_ids']) + task.get('instruction', '')
    ordinal=task.get('batch_output_index')
    diverse=(type(ordinal) is int and ordinal>0 and re.search(
        r'本批[^\n]*(?:开头[^\n]*(?:不同|不重复|不要重复)|不同[^\n]*开头)',task.get('instruction','')))
    if diverse:
        ordered=_batch_opening_order(task,eligible)
        source=ordered[(ordinal-1)%len(ordered)]
    else:
        source = eligible[int(hashlib.sha256(identity.encode()).hexdigest(), 16) % len(eligible)]
    fps = task['target']['fps']
    to_frame = lambda us: (us * fps + 500_000) // 1_000_000
    end_frame = 40 * fps
    crossing = next((u for u in source['speech_units']
                     if u['start_us'] < MIN_US < u['end_us']), None)
    # A requested forty-second output has no room for a sentence extension.
    if crossing and (target is None or crossing['end_us'] <= target * 1000):
        end_frame = to_frame(crossing['end_us'])
    boundaries = [0]
    ends = sorted({to_frame(u['end_us']) for u in source['speech_units']
                   if fps <= to_frame(u['end_us']) < end_frame} | {end_frame})
    for end in ends:
        if end - boundaries[-1] < fps:
            continue
        # Splits provide decoration anchors, without omitting/reordering any
        # frames or audio. Prefer ASR boundaries; subdivide unusually long spans.
        while end - boundaries[-1] > 6 * fps:
            boundaries.append(boundaries[-1] + 4 * fps)
        boundaries.append(end)
    if boundaries[-1] != end_frame:
        boundaries[-1] = end_frame
    parts = []
    for i, (a, b) in enumerate(zip(boundaries, boundaries[1:])):
        start, end = a * 1_000_000 // fps, b * 1_000_000 // fps
        frame = min(source['frames'], key=lambda f: abs(f['requested_us'] - (start + end)//2))
        parts.append({'asset_id': source['asset_id'], 'start_us': start, 'end_us': end,
            'frames': b-a, 'locked_opening_index': i,
            'first_unit': f'opening_{i:03d}', 'last_unit': f'opening_{i:03d}',
            'frame_times_us': [frame['requested_us']], 'heading': '', 'palette': 'cream',
            'placement': 'lower', 'role': 'locked_opening',
            'reason': '连续保留同一素材的完整开头，切点仅用于装饰，原声连续'})
    return {'policy': POLICY, 'asset_id': source['asset_id'],
            'end_us': end_frame * 1_000_000 // fps, 'frames': end_frame, 'parts': parts}


def validate_plan(task, sources, plan):
    opening = build(task, sources)
    if opening is None:
        return None
    clips = plan.get('clips') or []
    parts = opening['parts']
    if len(clips) < len(parts):
        raise PlanIssue('方案缺少完整的四十秒开头，不允许跳过固定开头')
    for actual, expected in zip(clips, parts):
        if any(actual.get(k) != expected[k] for k in
               ('asset_id', 'first_unit', 'last_unit', 'locked_opening_index')):
            raise PlanIssue('固定开头的素材、顺序或区间被修改，请重新生成方案')
    if any('locked_opening_index' in c for c in clips[len(parts):]):
        raise PlanIssue('固定开头只能出现一次，后半段不得重复开头')
    return opening


def validate_timeline(task, timeline, opening):
    if opening is None:
        return
    cursor = 0
    for row in timeline['source_map'][:len(opening['parts'])]:
        if (row['asset_id'] != opening['asset_id'] or
                row['source_start_us'] != cursor or row['timeline_start_us'] != cursor or
                row['source_end_us'] != row['timeline_end_us']):
            raise PlanIssue('固定开头校验失败：前四十秒必须逐帧连续保留同一原片开头')
        cursor = row['source_end_us']
    if abs(cursor - opening['end_us']) > 1 or cursor < MIN_US:
        raise PlanIssue('固定开头不足四十秒或未完整保留')
    for row in timeline['source_map'][len(opening['parts']):]:
        if row['asset_id'] == opening['asset_id'] and row['source_start_us'] < cursor:
            raise PlanIssue('后半段重复了已保留的开头画面或口播')
    timeline['locked_opening'] = {k: opening[k] for k in ('policy', 'asset_id', 'end_us')}
    timeline['locked_opening']['clip_count'] = len(opening['parts'])
