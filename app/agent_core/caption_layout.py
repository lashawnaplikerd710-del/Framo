"""Resolve prompt-scoped caption layouts on an absolute, half-open timeline."""
from copy import deepcopy
import re

VERSION = 'timed_caption_layout_v1'
TIME_SCOPE = re.compile(r'(?:\d+(?:\.\d+)?\s*(?:秒|s\b|[-~～—–到至]))|(?:开头|结尾|前半|后半|最后\s*\d)', re.I)


def temporal_clause(text):
    return bool(TIME_SCOPE.search(text))


def layout_values(clause):
    from .subtitle_styles import parse_position_instruction, parse_color_instruction
    # The temporal scope is resolved here; the existing position parser sees
    # only the placement, never a global last-position-wins instruction.
    clean = re.sub(r'开头|结尾|前半(?:段|部分)?|后半(?:段|部分)?|最后', '', clause)
    values = {**parse_position_instruction(clean, allow_timed=True)['values'],
              **parse_color_instruction(clause,allow_timed=True)['values']}
    for pattern in (r'(?:横向|水平|x)\s*[:：=]?\s*(\d+(?:\.\d+)?)\s*%',):
        found = re.search(pattern, clause, re.I)
        if found: values['x'] = float(found[1]) / 100
    for pattern in (r'(?:纵向|垂直|y|距顶部)\s*[:：=]?\s*(\d+(?:\.\d+)?)\s*%',):
        found = re.search(pattern, clause, re.I)
        if found: values['y'] = float(found[1]) / 100
    if re.search(r'(?:两行|双行|2行)', clause):
        values['max_lines'] = 2
        if not re.search(r'最多|不超过',clause):values['line_count']=2
    elif re.search(r'(?:单行|一行|1行)', clause): values['max_lines'] = 1
    elif re.search(r'(?:三行|3行)', clause):
        values['max_lines'] = 3
        if not re.search(r'最多|不超过',clause):values['line_count']=3
    found = re.search(r'(?:字号|字体大小)\s*(?:为|用|设为|设置为)?\s*(\d+(?:\.\d+)?)\s*倍', clause)
    if found: values['font_scale'] = float(found[1])
    found = re.search(r'(?:文字宽度|字幕宽度|排版宽度)\s*(?:为|用|设为)?\s*(\d+(?:\.\d+)?)\s*%', clause)
    if found: values['wrap_width'] = float(found[1])/100
    found = re.search(r'行距\s*(?:为|用|设为)?\s*(\d+(?:\.\d+)?)\s*倍', clause)
    if found: values['line_spacing'] = float(found[1])
    return values


def _interval(clause, duration):
    number = r'(\d+(?:\.\d+)?)'
    pair = re.search(number+r'\s*(?:秒|s)?\s*[-~～—–到至]\s*'+number+r'\s*(?:秒|s)', clause, re.I)
    if pair: return int(float(pair[1])*1e6), int(float(pair[2])*1e6)
    found = re.search(r'(?:前|开头)\s*'+number+r'\s*(?:秒|s)',clause,re.I)
    if found: return 0,int(float(found[1])*1e6)
    found = re.search(r'(?:最后|结尾|末尾|后)\s*'+number+r'\s*(?:秒|s)(?!后|以)',clause,re.I)
    if found: return max(0,duration-int(float(found[1])*1e6)),duration
    found = re.search(number+r'\s*(?:秒|s)\s*(?:之后|以后|后|起)',clause,re.I)
    if found:return int(float(found[1])*1e6),duration
    if re.search(r'前半',clause):return 0,duration//2
    if re.search(r'后半',clause):return duration//2,duration
    if '开头' in clause:return 0,min(3_000_000,duration)
    if '结尾' in clause:return max(0,duration-3_000_000),duration
    raise ValueError('字幕时间段无法识别，请使用“前5秒”或“5-10秒”等明确范围')


def schedule_from_task(task, duration):
    from .subtitle_styles import normalize_style
    if type(duration) is not int or duration<=0:raise ValueError('字幕时间线时长无效')
    result=[]
    structured=task.get('caption_layout_schedule') or []
    if not isinstance(structured,list):raise ValueError('字幕分段布局必须是列表')
    for row in structured:
        if not isinstance(row,dict):raise ValueError('字幕分段布局格式无效')
        start,end=row.get('start_us'),row.get('end_us')
        if type(start) is not int or type(end) is not int or start<0 or end<=start:
            raise ValueError('字幕分段布局的起止时间无效')
        if start<duration:
            result.append({'start_us':start,'end_us':min(end,duration),
                           'style':normalize_style(row.get('style')),'quote':row.get('quote','结构化布局')})
    scoped=False
    for clause in re.split(r'[，,。；;\n]',str(task.get('instruction') or '')):
        if re.search(r'(?:原有|原片|原始|原)字幕',clause):scoped=False;continue
        subject=re.search(r'字幕|花字|标题文字|captions?|subtitles?',clause,re.I)
        if subject:scoped=True
        elif not (scoped and temporal_clause(clause)):continue
        if not temporal_clause(clause):continue
        # Negated placements are not positive coordinates or silent defaults.
        if re.search(r'不要|不放|禁止|避免|别放',clause):continue
        if re.search(r'人物|产品|商品|镜头|贴纸',clause):continue
        values=layout_values(clause if subject else '字幕'+clause)
        if not values:continue
        start,end=_interval(clause,duration)
        if start<0 or end<=start:raise ValueError('字幕时间段结束时间必须晚于开始时间')
        if start>=duration:continue
        result.append({'start_us':start,'end_us':min(end,duration),
                       'style':normalize_style(values),'quote':clause.strip()})
    return result


def global_layout(instruction):
    values={}
    scoped=False
    for clause in re.split(r'[，,。；;\n]',str(instruction or '')):
        if temporal_clause(clause):scoped=False;continue
        subject=bool(re.search(r'字幕|花字|标题文字|captions?|subtitles?',clause,re.I))
        continuation=bool(re.match(r'\s*(?:横向|纵向|水平|垂直|距顶部|[xy]\s*[:：=]|字号|字体大小|行距|两行|三行|单行)',clause,re.I))
        if not subject and not (scoped and continuation):scoped=False;continue
        scoped=True
        if not re.search(r'不要|不放|禁止|原字幕|人物|产品|商品|镜头|贴纸',clause):
            values.update(layout_values(clause if subject else '字幕'+clause))
    return values


def expand_cues(captions, schedule):
    """Split spans at layout boundaries without changing text/audio timing.

    Later overlapping instructions win per field. A continuation never replays
    an entrance; its continuous loop retains the original cue phase.
    """
    result=[]
    for cue in captions:
        start,end=cue['start_us'],cue['end_us']
        cuts=sorted({start,end}|{r[k] for r in schedule for k in ('start_us','end_us') if start<r[k]<end})
        for a,b in zip(cuts,cuts[1:]):
            item=deepcopy(cue);style=dict(item.get('subtitle_style') or {})
            for row in schedule:
                if row['start_us']<=a<row['end_us']:style.update(row['style'])
            item.update(start_us=a,end_us=b,subtitle_style=style)
            # The resolved per-range style must win over an earlier global
            # placement when direct_renderer applies user-wide typography.
            item['timed_subtitle_style']={k:v for row in schedule if row['start_us']<=a<row['end_us'] for k,v in row['style'].items()}
            item['caption_origin_start_us']=cue.get('caption_origin_start_us',start)
            if a>start:
                item['caption_motion']={**(item.get('caption_motion') or {}),'entry':'none'}
            result.append(item)
    return result


def has_layout_request(instruction):
    return bool(re.search(r'字幕|花字|标题文字|captions?|subtitles?',str(instruction or ''),re.I) and
                re.search(r'上方|下方|居中|左|右|中间|两行|单行|行距|字号|\d\s*%',str(instruction or '')))
