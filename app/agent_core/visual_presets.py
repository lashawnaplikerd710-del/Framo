"""Portable, deterministic visual treatments for the direct FFmpeg renderer.

Names are a closed allowlist: a prompt never becomes an FFmpeg expression.
Automatic choices use task-scoped deterministic randomness, so retries keep
the same design while independent outputs can use different designs.
"""
import hashlib
import json
import math
import random
import re

VERSION = 'direct_visual_library_v4'
from .compound_transitions import COMPOUND_TRANSITIONS
STANDARD_TRANSITIONS = {
    'fade': '叠化', 'fadeblack': '淡黑', 'fadewhite': '闪白',
    'wipeleft': '向左擦除', 'wiperight': '向右擦除',
    'wipeup': '向上擦除', 'wipedown': '向下擦除',
    'slideleft': '向左推移', 'slideright': '向右推移',
    'slideup': '向上推移', 'slidedown': '向下推移',
    'smoothleft': '向左平滑', 'smoothright': '向右平滑',
    'smoothup': '向上平滑', 'smoothdown': '向下平滑',
    'circleopen': '圆形展开', 'circleclose': '圆形收拢',
    'horzopen': '水平展开', 'vertopen': '垂直展开',
    'diagtl': '左上斜切', 'diagbr': '右下斜切',
    'pixelize': '像素化', 'hblur': '模糊过渡', 'zoomin': '推进转场',
}
TRANSITIONS = {**STANDARD_TRANSITIONS, **COMPOUND_TRANSITIONS}
# These GEQ-heavy treatments stay reachable but cannot multiply automatic
# whole-film rendering cost by being repeated at every shuffled bag.
AUTOMATIC_ONCE_ONLY = frozenset(('glitch_horizontal', 'warm_light_sweep', 'defocus'))
ANIMATIONS = {
    'fade': '渐显', 'pop': '弹入', 'bounce': '弹跳',
    'slideleft': '从左滑入', 'slideright': '从右滑入',
    'slideup': '从上滑入', 'slidedown': '从下滑入',
    'float': '漂浮', 'swing': '摇摆', 'spin': '旋转入场',
    'pulse': '呼吸缩放', 'tilt': '倾斜入场',
}
EFFECTS = {
    'soft_sharpen': '柔和锐化', 'crisp': '清晰增强', 'soft_glow': '柔光',
    'vignette': '暗角', 'film_grain': '胶片颗粒', 'cinematic': '电影质感',
    'warm': '暖色', 'cool': '冷色', 'vivid': '鲜艳', 'mono': '黑白',
    'vintage': '复古', 'fade_film': '褪色胶片',
}
CATALOGS = {'transition': TRANSITIONS, 'sticker': ANIMATIONS, 'effect': EFFECTS}
FLAGS = {'transition': 'transitions_required', 'sticker': 'sticker_required',
         'effect': 'effects_required'}
ALIASES = {'transition': {'溶解': 'fade', '淡入淡出': 'fade', '闪黑': 'fadeblack',
                          '交叉淡化': 'fade', '左推': 'slideleft', '右推': 'slideright',
                          '甩动模糊':'whip_blur_left','向左甩动':'whip_blur_left',
                          '向右甩动':'whip_blur_right','镜像重复':'mirror_whip',
                          '镜像甩动':'mirror_whip','彩色重影':'chromatic_zoom','缩放重影':'chromatic_zoom',
                          '向上甩动':'whip_blur_up','向下甩动':'whip_blur_down',
                          '旋转推进':'rotate_zoom','拉远弹回':'pullback_bounce',
                          '白闪推进':'flashwhite_zoom','黑闪缩放':'flashblack_zoom',
                          '横挤压':'squeeze_horizontal','竖挤压':'squeeze_vertical',
                          '故障转场':'glitch_horizontal','失焦转场':'defocus',
                          '散焦过渡':'defocus','暖光转场':'warm_light_sweep'},
           'sticker': {'弹出': 'pop', '摆动': 'swing', '放大弹入': 'pop'},
           'effect': {'怀旧': 'vintage', '柔焦': 'soft_glow', '去色': 'mono'}}
DEFAULTS = {'transition': tuple(TRANSITIONS),
            'sticker': ('pop', 'fade', 'float', 'slideup'),
            'effect': ('soft_sharpen', 'crisp', 'warm')}


def _selection_seed(task, seed, category):
    """Use persistent job/output identity, never mutable retry state or RNG."""
    unique_key = next((key for key in ('job_id', 'task_id', 'id')
                       if task.get(key) not in (None, '')), None)
    if unique_key:
        identity = {unique_key: task[unique_key]}
    else:
        # A batch id alone identifies many outputs. Keep the caller's stable
        # child job seed and the actual batch child fields in that situation.
        identity = {key: task[key] for key in
                    ('batch_id', 'batch_output_id', 'batch_output_index', 'output_id')
                    if task.get(key) not in (None, '')}
    # The renderer normally supplies its persistent job directory as seed.
    # Standalone callers without an id still get a stable, useful fallback.
    fallback = {} if identity or seed else {
        key: task[key] for key in ('instruction', 'workflow', 'title', 'target')
        if key in task}
    payload = json.dumps({'identity': identity, 'seed': '' if unique_key else str(seed),
                          'fallback': fallback, 'category': category},
                         ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                         default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _rng(task, seed, category):
    return random.Random(_selection_seed(task, seed, category))


def caption_motion_selection(task, seed=''):
    """Choose one whole-film entry and an independently optional loop.

    ``motion`` is a regular ``normalize_motion`` dictionary; selection metadata
    remains separate so it cannot accidentally become a rendering parameter.
    Explicit task settings and instruction names/closures override defaults.
    This helper only selects styling and never creates a caption.
    """
    from .caption_motion import ENTRIES, LOOPS, normalize_motion, motion_from_instruction
    defaults = normalize_motion()
    defaults['entry'] = _rng(task, seed, 'caption_entry').choice(
        [key for key in ENTRIES if key != 'none'])
    if _rng(task, seed, 'caption_loop_enabled').random() < .5:
        defaults['loop'] = _rng(task, seed, 'caption_loop_preset').choice(
            [key for key in LOOPS if key != 'none'])
    explicit = task.get('caption_motion') or {}
    if isinstance(explicit, str):
        explicit = {'entry': explicit}
    if not isinstance(explicit, dict):
        # Reuse the renderer's Chinese validation error for malformed settings.
        normalize_motion(explicit)
    instruction = motion_from_instruction(task.get('instruction'))
    return {'motion': normalize_motion({**defaults, **explicit, **instruction}),
            'default_motion': defaults,
            'selection_seed': _selection_seed(task, seed, 'caption_motion'),
            'loop_probability': .5,
            'selection': {key: 'user' if key in explicit or key in instruction
                          else 'automatic' for key in ('entry', 'loop')}}


def default_caption_motion(task, seed=''):
    """Return task-stable entry/loop defaults without generating any text."""
    return caption_motion_selection(task, seed)['motion']


def _category_disabled(instruction, noun):
    return bool(re.search(
        r'(?:不要|不加|不添加|不用|不使用|不需要|不做|无需|无|取消|去掉|关闭|禁止)'
        r'(?:添加|使用)?(?:任何|所有|全部)?(?:的)?' + noun,
        instruction, re.I))


def _requested(instruction, category, *, negative=False):
    """Match names only in a clause discussing that element; ignore negations."""
    marker = {'transition': r'转场|过渡|transition',
              'sticker': r'贴纸|sticker', 'effect': r'特效|滤镜|质感|effect|filter'}[category]
    names = {**{v: k for k, v in CATALOGS[category].items()},
             **{k: k for k in CATALOGS[category]}, **ALIASES[category]}
    found = []
    clause_offset=0
    for clause in re.split(r'[。；;\n]', instruction.casefold()):
        # A standalone "不要闪白" still explicitly excludes that transition;
        # captions/stickers/effects keep their own namespace when mentioned.
        standalone_exclusion = (category == 'transition' and negative and
            re.search(r'不要|不加|不添加|不用|不使用|取消|去掉|关闭|禁止|no\s|without\s', clause) and
            not re.search(r'字幕|花字|文字|标题|贴纸|特效|滤镜|caption|subtitle|sticker|effect|filter', clause))
        if not re.search(marker, clause) and not standalone_exclusion:
            clause_offset+=len(clause)+1
            continue
        matches=[]
        for label, key in names.items():
            pattern = (r'(?<![a-z_])' + re.escape(label) + r'(?![a-z_])'
                       if label.isascii() else re.escape(label))
            for match in re.finditer(pattern, clause):
                prefix = clause[:match.start()]
                # A comma begins another independent positive/negative request.
                fragment = re.split(r'[,，、]', prefix)[-1]
                negated = bool(re.search(r'不要|不加|不添加|不用|不使用|取消|去掉|关闭|禁止|no\s|without\s', fragment))
                if negated != negative:
                    continue
                matches.append((match.start(),match.end(),key))
        accepted=[]
        # A short alias inside “向右甩动模糊” must not also select the
        # generic/left preset. Prefer the longest matching name at each span.
        for start,end,key in sorted(matches,key=lambda r:(-(r[1]-r[0]),r[0])):
            if any(start<stop and begin<end for begin,stop,_ in accepted):continue
            accepted.append((start,end,key))
        found.extend((clause_offset+start,key) for start,end,key in accepted)
        clause_offset+=len(clause)+1
    return list(dict.fromkeys(key for _, key in sorted(found)))


def visual_plan(task, seed=''):
    from .original_montage import active as original_montage, TRANSITION_PRESETS, EFFECT_PRESETS
    instruction = str(task.get('instruction') or '')
    options = task.get('resolved_editing_options') or {}
    overrides = task.get('explicit_editing_overrides') or {}
    policy = task.get('formal_ad_policy') or {}
    result = {'version': VERSION, 'categories': {}}
    for category, flag in FLAGS.items():
        requested = _requested(instruction, category)
        excluded = _requested(instruction, category, negative=True)
        # Preserve legacy implicit decoration; new custom jobs always carry
        # explicit resolved options and do not inherit this legacy default.
        legacy = category == 'sticker' and 'workflow' not in task
        enabled = options.get(category, policy.get(flag, policy.get('effect_required', False)
            if category == 'effect' else bool(requested) or legacy))
        # All new workflows, including custom jobs, get transitions unless the
        # user explicitly closes them. Resolved defaults are not user intent.
        if category == 'transition':
            enabled = True
        override = overrides.get(category)
        if isinstance(override, dict):
            enabled = override.get('value', enabled)
        noun = {'transition': '转场', 'sticker': '贴纸', 'effect': '特效'}[category]
        if _category_disabled(instruction, noun) or (category == 'transition' and
                re.search(r'\b(?:no\s+(?:transitions?|crossfades?)|without\s+transitions?|'
                          r'don[\'’]t\s+(?:use|add)\s+transitions?|'
                          r'(?:disable|turn\s+off)\s+transitions?)\b', instruction, re.I)):
            enabled = False
        pool = [p for p in (requested or list(DEFAULTS[category])) if p not in excluded]
        selection_seed = _selection_seed(task, seed, category)
        if not requested:
            if pool:
                if category == 'transition':
                    candidates = list(TRANSITION_PRESETS if original_montage(task) else pool)
                    _rng(task, seed, category).shuffle(candidates)
                    pool = []
                    for candidate in candidates:
                        if candidate in AUTOMATIC_ONCE_ONLY and any(
                                key in AUTOMATIC_ONCE_ONLY for key in pool):
                            continue
                        pool.append(candidate)
                        if len(pool) == (12 if original_montage(task) else 3):
                            break
                else:
                    if category=='effect' and original_montage(task):pool=list(EFFECT_PRESETS)
                    offset = int(selection_seed[:8], 16) % len(pool)
                    pool = pool[offset:] + pool[:offset]
        result['categories'][category] = {'enabled': bool(enabled),
            'presets': pool if enabled else [], 'selection': 'user' if requested else 'automatic',
            'excluded': excluded, 'selection_seed': selection_seed,
            'automatic_once_only': [key for key in pool if enabled and category == 'transition'
                                    and not requested and key in AUTOMATIC_ONCE_ONLY],
            'ordering': 'shuffled_bag' if category == 'transition' and not requested else 'cycle'}
    return result


def choose(plan, category, index):
    row = plan['categories'][category]
    values = row['presets']
    if not values:
        return None
    once_only = row.get('automatic_once_only', [])
    if row.get('ordering') != 'shuffled_bag' or (len(values) == 1 and not once_only):
        return values[index % len(values)]
    if type(index) is not int or index < 0:
        raise ValueError('视觉效果序号必须是非负整数')
    cycle, offset = divmod(index, len(values))

    def bag(number):
        ordered = list(values)
        random.Random(row['selection_seed'] + ':bag:' + str(number)).shuffle(ordered)
        return ordered

    if once_only:
        first = bag(0)
        if index < len(first):
            return first[index]
        repeatable = [key for key in values if key not in once_only]
        if not repeatable:
            return None
        random.Random(row['selection_seed'] + ':repeat').shuffle(repeatable)
        if len(repeatable) > 1 and repeatable[0] == first[-1]:
            repeatable[0], repeatable[1] = repeatable[1], repeatable[0]
        return repeatable[(index - len(first)) % len(repeatable)]

    # Two presets have only one legal ordering if adjacent repetition across
    # bag boundaries is forbidden. Reuse the initial shuffled pair.
    ordered = bag(0 if len(values) == 2 else cycle)
    if len(values) > 2 and cycle and ordered[0] == bag(cycle - 1)[-1]:
        # Swapping the first two leaves the last preset unchanged, allowing an
        # arbitrary indexed lookup without replaying all preceding boundaries.
        ordered[0], ordered[1] = ordered[1], ordered[0]
    return ordered[offset]


def effect_filters(effect):
    kind = effect.get('type')
    if kind not in EFFECTS:
        raise ValueError('不支持的画面特效：' + str(kind))
    strength = float(effect.get('strength', .35))
    if not math.isfinite(strength) or not 0 <= strength <= 1:
        raise ValueError('画面特效强度必须在 0 到 1 之间')
    s = strength
    return {
        'soft_sharpen': [f'unsharp=lx=5:ly=5:la={s:.4f}'],
        'crisp': [f'unsharp=lx=3:ly=3:la={s*.7:.4f}', f'eq=contrast={1+s*.05:.4f}'],
        'soft_glow': [f'gblur=sigma={.1+s*.65:.4f}', f'eq=brightness={s*.025:.4f}:saturation={1+s*.06:.4f}'],
        'vignette': [f'vignette=angle={s*.65:.4f}'],
        'film_grain': [f'noise=alls={s*10:.4f}:allf=t+u:all_seed=42'],
        'cinematic': [f'eq=contrast={1+s*.12:.4f}:saturation={1-s*.12:.4f}', f'colorbalance=bs={s*.025:.4f}'],
        'warm': [f'colorbalance=rs={s*.045:.4f}:bs={-s*.035:.4f}'],
        'cool': [f'colorbalance=rs={-s*.025:.4f}:bs={s*.045:.4f}'],
        'vivid': [f'eq=saturation={1+s*.3:.4f}:contrast={1+s*.04:.4f}'],
        'mono': [f'hue=s={1-s:.4f}'],
        'vintage': [f'eq=saturation={1-s*.3:.4f}:contrast={1-s*.08:.4f}',
                    f'colorbalance=rs={s*.06:.4f}:bs={-s*.05:.4f}'],
        'fade_film': [f'eq=contrast={1-s*.15:.4f}:brightness={s*.035:.4f}:saturation={1-s*.15:.4f}'],
    }[kind]


def sticker_motion(row, fps):
    """Return local-time image animation plus absolute-time overlay coordinates."""
    kind = row.get('animation', 'none')
    if kind == 'none':
        return '', row['x'], row['y']
    if kind not in ANIMATIONS:
        raise ValueError('不支持的贴纸动画：' + str(kind))
    start = row['target_start'] / 1e6
    duration = row['target_duration'] / 1e6
    entrance = min(.28, duration / 3)
    tail = min(.2, duration / 3)
    # t in image filters is local; overlay t is absolute. Never animate a late
    # sticker with global time (it would already have finished its entrance).
    u = f'min(1,max(0,t/{entrance:.6f}))'
    at = f'max(0,t-{start:.6f})'
    v = f'min(1,({at})/{entrance:.6f})'
    chain = f'format=rgba,fps={fps},setpts=PTS-STARTPTS'
    scale = None
    if kind == 'pop':
        scale = f'.65+.35*({u})+.12*sin(PI*({u}))'
    elif kind == 'pulse':
        scale = '.94+.06*cos(2*PI*t/1.1)'
    elif kind == 'spin':
        scale = f'.75+.25*({u})'
    if scale:
        chain += f",scale=w='max(2,trunc(iw*({scale})/2)*2)':h='max(2,trunc(ih*({scale})/2)*2)':eval=frame"
    if kind in ('swing', 'spin', 'tilt'):
        # Fixed transparent padding prevents rotating corners from being cut.
        angle = {'swing': '.13*sin(2*PI*t/1.1)',
                 'spin': f'-.65*(1-({u}))',
                 'tilt': f'-.2*(1-({u}))'}[kind]
        chain += f",pad=iw*2:ih*2:iw/2:ih/2:color=0x00000000,rotate='{angle}':ow=iw:oh=ih:c=none"
    chain += (f',fade=t=in:st=0:d={min(.12,entrance):.6f}:alpha=1'
              f',fade=t=out:st={duration-tail:.6f}:d={tail:.6f}:alpha=1'
              f',setpts=PTS+{start:.6f}/TB')
    x = f"({row['x']})+{row['width']}/2-overlay_w/2"
    y = f"({row['y']})+{row['height']}/2-overlay_h/2"
    if kind in ('slideleft', 'slideright'):
        x += f"+{'-' if kind=='slideleft' else ''}36*(1-({v}))"
    if kind in ('slideup', 'slidedown'):
        y += f"+{'-' if kind=='slideup' else ''}36*(1-({v}))"
    if kind == 'bounce':
        y += f'-18*abs(sin(3*PI*({v})))*(1-({v}))'
    if kind == 'float':
        y += f'-5*sin(2*PI*({at})/1.5)'
    return chain, x, y
