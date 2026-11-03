"""Narration belongs to one output, not to individual video cuts."""
import re

from . import market_rules

POLICY = 'continuous_narration_v1'
RULE = ('口播按整条成品统一安排，允许一句跨越连续镜头，不要求一句在单个镜头内说完。'
        'clip_index仅用于文案追踪，不是声音结束边界。保留叙事顺序和真实商品依据，'
        '不把不同商品或矛盾动作混说；不能因为跨镜头本身拒绝文案。'
        '全部口播需在成品总时长内以自然语速说完并预留停顿，超出总预算才精简整段文案。'
        '逐字字幕跟随实际配音；关键卖点文案按画面独立安排，不要求与口播逐字相同。'
        '不截断句尾、不强行加速、不跨成品或空白间隙。')


class NarrationDurationIssue(ValueError):
    """Measured PCM overflow: repair the script automatically, not the files."""
    def __init__(self, measurement):
        super().__init__('整段旁白过早结束，正在自动展开连贯叙述并重新配音' if measurement.get('kind') == 'underfilled'
                         else '实际配音超出成品可用时间，正在自动精简文案并重新配音')
        self.measurement = measurement


def planning_rate(task):
    locale = (task.get('market_rules') or {}).get('locale')
    return 1.7 if locale == 'en-US' else 3.2 if locale == 'ja-JP' else 2.8


def planning_hint(task):
    duration = (task.get('target') or {}).get('duration_ms')
    return {'units_per_second': planning_rate(task),
            'unit': 'words' if (task.get('market_rules') or {}).get('locale') == 'en-US' else 'characters',
            'pause_per_sentence_ms': 120, 'tail_ms': 100,
            'target_duration_ms': duration,
            'instruction': '先加总所选镜头实际时长，扣除每句间停顿，再计算整片口播预算；不要填满硬上限'}


def script_locked(task):
    text = task.get('instruction', '')
    return bool(re.search(r'(?:口播|文案|台词|旁白).{0,16}(?:一字不改|逐字保留|不得删改|不能改|原样保留)'
                          r'|(?:一字不改|逐字保留|不得删改|原样保留).{0,16}(?:口播|文案|台词|旁白)', text))


def enabled(task):
    return task.get('render_backend') == 'direct_ffmpeg' and task.get('narration_mode') == 'new_native'


def total_duration(timeline):
    return int(timeline.get('duration_us') or max(
        (r['timeline_end_us'] for r in timeline.get('source_map', [])), default=0))


def units(task, text):
    if market_rules.active(task):
        return market_rules.speech_units(task, text)
    return len(re.findall(r'[\u3040-\u30ff\u3400-\u9fffA-Za-z0-9]', text))


def budget(task, timeline, count, *, planning=False):
    # Reserve sentence breaks and an unspoken tail inside this output only.
    seconds = max(0, (total_duration(timeline) - 120_000 * max(0, count-1) - 100_000) / 1e6)
    if planning:
        return max(1, int(seconds * planning_rate(task))), planning_hint(task)['unit']
    if market_rules.active(task):
        return market_rules.speech_budget(task, seconds, planning=planning)
    return max(1, int(seconds * (3.4 if planning else 4.5))), 'characters'


def schedule(durations, total, *, estimated=False, anchors=None):
    """Absolute, sequential cues. Exact PCM durations are never shortened."""
    if not durations or any(type(d) is not int or d <= 0 for d in durations):
        raise ValueError('口播时长无效')
    pause, tail = 120_000, 100_000
    available = total - pause * (len(durations)-1) - tail
    if available <= 0:
        raise ValueError('成品时长不足以容纳口播停顿')
    if estimated:
        weight = sum(durations)
        edges = [0]
        for d in durations:
            edges.append(edges[-1] + d)
        durations = [available*edges[i+1]//weight - available*edges[i]//weight
                     for i in range(len(durations))]
    elif sum(durations) > available:
        raise ValueError(f'整条成品口播总时长超出可用时间：{sum(durations)/1e6:.2f}秒 > '
                         f'{available/1e6:.2f}秒；请精简整段文案或延长成品，已保留配音缓存')
    if anchors is not None and (len(anchors) != len(durations) or any(type(a) is not int or a < 0 for a in anchors)):
        raise ValueError('口播视觉锚点无效')
    # Place measured speech near its evidence shot when room permits, while
    # reserving enough time for every later sentence. No audio is accelerated.
    latest = total-tail
    starts = [0]*len(durations)
    for i in range(len(durations)-1,-1,-1):
        starts[i] = latest-durations[i]
        latest = starts[i]-pause
    result, cursor = [], 0
    for i,d in enumerate(durations):
        if anchors is not None:
            cursor = max(cursor, min(anchors[i], starts[i]))
        result.append({'start_us': cursor, 'end_us': cursor+d})
        cursor += d + pause
    return result


def retime(cues, captions, ranges):
    if len(cues) != len(ranges):
        raise ValueError('口播时间与文案数量不一致')
    updated = [{**cue, **span} for cue, span in zip(cues, ranges)]
    by_id = {c.get('source_cue_id'): c for c in updated if c.get('source_cue_id')}
    subtitles = []
    for caption in captions:
        cue = by_id.get(caption.get('narration_cue_id'))
        subtitles.append({**caption, **({'start_us': cue['start_us'], 'end_us': cue['end_us']}
                                       if cue else {})})
    return updated, subtitles
