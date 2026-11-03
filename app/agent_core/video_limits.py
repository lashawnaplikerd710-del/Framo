"""Shared import, output and long-form editing limits."""
import re

MAX_SOURCE_US = 420_000_000
MAX_OUTPUT_MS = 600_000
MAX_ASSETS = 20
LONG_POLICY = 'long_video_v1'


def long_form(task):
    return task.get('creation_mode') == LONG_POLICY


def duration_from_instruction(text):
    numbers = {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}
    pattern = r'(?<![\d.])(?P<m>\d{1,3}(?:\.\d+)?|[一二两三四五六七八九十])\s*(?:分钟|分(?!镜))\s*(?:(?P<s>\d{1,2})\s*秒)?|(?<![\d.])(?P<seconds>\d{1,4}(?:\.\d+)?)\s*(?:秒|s(?:ec(?:onds?)?)?\b)'
    values = []
    for match in re.finditer(pattern,text,re.I):
        before = text[:match.start()]
        after = text[match.end():]
        # Editing clocks describe local intervals, never total output length.
        # Preserve a genuine pair of conflicting total-duration requests.
        if (re.search(r'(?:前|后|开头|结尾|最后|末尾|每段|每个镜头|单个镜头|转场|淡入|淡出|停顿|音效|口播|字幕显示|贴纸显示)\s*$', before)
                or re.search(r'\d+(?:\.\d+)?\s*(?:秒|分钟|s)?\s*[-~～—–到至]\s*$', before,re.I)
                or re.match(r'\s*(?:之后|以后|后|起|之前|以前|处|时|[-~～—–到至])',after)):
            continue
        if match['m']:
            value = float(match['m']) if re.fullmatch(r'\d+(?:\.\d+)?',match['m']) else numbers[match['m']]
            seconds = value*60 + int(match['s'] or 0)
        else:
            seconds = float(match['seconds'])
        values.append(round(seconds*1000))
    if len(set(values)) > 1:
        raise ValueError('剪辑要求包含互相冲突的成片时长，请只指定一个目标时长')
    value = values[0] if values else None
    if value is not None and not 1_000 <= value <= MAX_OUTPUT_MS:
        raise ValueError('成片时长应为 1 秒至 10 分钟')
    return value


def configure(task):
    task = dict(task)
    if task.get('workflow') in ('long_video','long_video_no_voice'):
        if task.get('render_backend') != 'direct_ffmpeg':
            raise ValueError('长视频请使用 MP4 直接导出')
        if task['workflow']=='long_video_no_voice':
            task['original_montage_policy']='semantic_original_montage_v1'
            task['opening_policy']='source_prefix_40s_v1'
        task.update(workflow='custom',creation_mode=LONG_POLICY)
    return task
