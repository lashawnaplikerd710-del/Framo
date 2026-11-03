"""Versioned Japan/US category defaults distilled from the approved ad session.

Profiles are copied into new tasks. Existing saved tasks are never migrated.
The example product, voice files and exact durations are not reusable rules.
"""
from copy import deepcopy
import re

VERSION = 'tiktok_market_v1'
SOURCE_THREAD = '01a13009-afb2-70a0-b254-b2553e05e010'

COMMON = (
    '用户本次指令优先，模板只补全未指定部分。先识别商品与可用画面，按吸引注意、'
    '可见特点及使用演示、商品详情引导组织，不随机轮流拼接，不重复开场。'
    '原声默认全部去除，原口播仅作资料；重新创作目标语言口播和卖点字幕，'
    '原烧录字幕暂时保留，新文字避开主体、旧字幕和平台按钮区。'
    '口播使用短句、自然停顿和轻重音，避免匀速朗读、翻译腔和剪辑标签。'
    '口播按整条成品安排，允许完整句子延续到后续画面，不要求逐镜头一一对应；'
    '连续镜头须共同支持文案，不混淆商品、物种或适用对象。'
    '只能使用可见结构、明确使用演示和有依据的信息，不凭拉链推断可水洗，'
    '不编造价格、优惠、健康功效、适用品种或保证。'
    '结尾自然引导查看商品页尺寸与详情，不虚构链接或促销。'
    '以完整口播与画面证据决定时长，不照搬样片秒数；字幕保留阅读时间。'
    '配音需核对发音、漏词和句尾完整性，语速变化不得损害自然感；'
    '未听音频不得声称自然度已验收，缺合适配音保留任务待补资源。'
    '默认竖屏1080×1920、30fps；检查时长、完整解码、黑帧、音画对应和原声移除。'
)
PROFILES = {
    '日本': {'id': 'jp', 'locale': 'ja-JP', 'language': '日语',
        'tone': '温柔、自然的居家分享口吻，先用真实使用状态引起兴趣，再展示细节；克制推销，避免生硬翻译和夸张催单。',
        'voice_style': '自然日语口播，读音稳定，柔和、有停顿，避免机械播报。'},
    '美国': {'id': 'us', 'locale': 'en-US', 'language': '英语',
        'tone': '轻快直接的美式英语口语，开场用真实反应或具体使用场景吸引注意，逐项演示可见功能，再自然引导了解。',
        'voice_style': '自然美式英语口播，像向朋友分享，语气有起伏，避免整段匀速朗读。'},
}


def profile_for(country):
    if country not in PROFILES:
        raise ValueError('新版跨境类别仅支持日本和美国')
    return {**deepcopy(PROFILES[country]), 'version': VERSION,
            'source_thread': SOURCE_THREAD, 'country': country, 'common': COMMON}


def active(task):
    return (task.get('workflow') == 'crossborder_montage'
            and task.get('render_backend') == 'direct_ffmpeg'
            and (task.get('market_rules') or {}).get('version') == VERSION)


def prompt(task):
    if not active(task):
        return ''
    p = task['market_rules']
    return f"目标市场：{p['country']}；新增文案和口播使用{p['language']}（{p['locale']}）。{p['tone']}{p['voice_style']}{COMMON}"


def default_instruction(country):
    p = profile_for(country)
    return (f"制作{country}区 TikTok 电商带货广告，{p['tone']}去除原声，"
            f"重新创作自然{p['language']}口播与卖点字幕；"
            '按真实画面展示商品和细节，结尾引导查看商品详情。原字幕暂时保留，按原素材比例输出。')


def speech_budget(task, seconds, *, planning=False):
    """English is budgeted in words, Japanese in visible characters, not Latin letters."""
    locale = task['market_rules']['locale']
    if locale == 'en-US':
        return max(1, int(seconds * (2.1 if planning else 2.8))), 'words'
    return max(1, int(seconds * (4.0 if planning else 5.5))), 'characters'


def speech_units(task, text):
    if task['market_rules']['locale'] == 'en-US':
        return len(re.findall(r"[A-Za-z]+(?:['’\-][A-Za-z]+)*|\d+", text))
    return len(re.findall(r'[\u3040-\u30ff\u3400-\u9fffA-Za-z0-9]', text))


def validate_language(task, text):
    locale = task['market_rules']['locale']
    if locale == 'en-US':
        return bool(re.search('[A-Za-z]', text)) and not re.search(r'[\u3040-\u30ff\u3400-\u9fff]', text)
    return bool(re.search(r'[\u3040-\u30ff]', text))
