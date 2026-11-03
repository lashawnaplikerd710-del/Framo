"""Versioned prompt-first rules for newly created desktop jobs.

Old saved jobs keep their original contract. Model-resolved overrides must cite
the user's prompt; an unsupported request never silently becomes a default.
"""
from copy import deepcopy
import re
from . import market_rules

VERSION = 'prompt_first_v1'
FLAGS = {'music': 'music_required', 'sound': 'sound_effects_required',
         'sticker': 'sticker_required', 'transition': 'transitions_required',
         'effect': 'effects_required', 'filter': 'filters_required',
         'text': 'selling_copy_required', 'zoom': 'zoom_required'}

_NEGATIVE = r'不要|不用|无需|不需要|不加|不添加|不使用|取消|去掉|关闭|禁止'

_EXPLICIT_CATEGORIES = {
    'narration': r'(?:中文|普通话|汉语|日语|英语)?(?:口播|配音|旁白)',
    'text': r'(?:中文|日文|英文|日语|英语)?(?:字幕|屏幕文案|屏幕文字|文案|花字|文字|字体|标题)',
    'sticker': r'贴纸',
}
_REQUEST = r'重新生成|重新配|重新制作|生成|添加|增加|加入|配上|加上|需要|启用|使用|制作|加'


def _explicit_request_fallback(task, result):
    """Recover only unambiguous requests omitted or inverted by extraction.

    A custom task has deliberately empty defaults. These three categories must
    not depend on the model remembering a switch, but a mention/reference is
    not authorization to add one. Keep a genuine, continuous source quote and
    let a category-wide prohibition win over any positive/model request.
    """
    clauses = re.split(r'[,，。；;!！\n]', task.get('instruction', ''))
    all_names = '|'.join(_EXPLICIT_CATEGORIES.values())
    modifiers = rf'(?:再|新增|新|新的|生成|添加|任何|所有|全部|对应的|和|及|与|还有|、|或|{all_names})*'
    found = {}
    for key, names in _EXPLICIT_CATEGORIES.items():
        for clause in clauses:
            disabled = re.search(
                rf'(?:{_NEGATIVE}){modifiers}(?:{names})(?=$|[和及与、或])'
                rf'|(?:{names})(?:都)?(?:{_NEGATIVE})(?=$|[和及与、或])', clause)
            if disabled:
                found[key] = {'value': 'none' if key == 'narration' else False,
                              'quote': disabled.group()}
                break
        if key in found:
            continue
        for clause in clauses:
            if key == 'text' and _motion_disable_only(clause):
                continue
            for noun in re.finditer(names, clause):
                before = clause[:noun.start()]
                # Script writing and keeping existing media do not request
                # screen captions or a new synthesized voice.
                if ((key == 'text' and re.search(r'(?:口播|旁白|配音)$', before)) or
                        (key == 'narration' and re.match(r'(?:文案|稿|脚本)', clause[noun.end():])) or
                        re.search(r'(?:保留|原有|现有|已有|原)(?:的)?$', before)):
                    continue
                directives = list(re.finditer(
                    rf'(?:{_NEGATIVE})(?:再|重新|生成|添加|增加|加入|使用|制作)*|{_REQUEST}', before))
                if directives and not re.match(_NEGATIVE, directives[-1].group()):
                    found[key] = {'value': 'new' if key == 'narration' else True,
                                  'quote': clause[directives[-1].start():noun.end()]}
                    break
                if (key == 'text' and not (directives and re.match(_NEGATIVE, directives[-1].group()))
                        and re.match(r'(?:的)?(?:字体|字色|颜色|位置|字号)?(?:使用|用|设为|改为|放在)',
                                     clause[noun.end():])):
                    found[key] = {'value': True, 'quote': clause[noun.start():]}
                    break
            if key in found:
                break
    # A music-only instruction is an explicit exclusion of these categories.
    for clause in clauses:
        only_music = re.search(r'只(?:添加|加|要|保留|使用)?(?:背景)?音乐(?:即可|就好|就行)?$', clause)
        if only_music:
            found.update({key: {'value': 'none' if key == 'narration' else False,
                                'quote': only_music.group()} for key in _EXPLICIT_CATEGORIES})
    if not found:
        return result
    result = deepcopy(result)
    result['overrides'].update(found)
    return result


def _transition_disable_quote(instruction):
    """A category switch is different from excluding one named transition."""
    for clause in re.split(r'[,，。；;!！\n]', instruction or ''):
        match = re.search(
            rf'(?:{_NEGATIVE})(?:任何|所有|全部|新增)?'
            r'(?:音乐|音效|贴纸|字幕|特效|滤镜|和|及|与|、)*'
            r'(?:转场|过渡)(?:动画|效果)?(?=$|[和及与、])'
            rf'|(?:转场|过渡)(?:动画|效果)?(?:都)?(?:{_NEGATIVE})(?=$|[和及与、])', clause)
        if match:
            return match.group()
        match = re.search(r'只(?:添加|加|要|保留|使用)?(?:背景)?音乐(?:即可|就好|就行)?$', clause)
        if match:
            return match.group()
    return None


def _motion_disable_only(quote):
    return bool(re.fullmatch(
        rf'(?:{_NEGATIVE})(?:任何|全部|所有)?(?:字幕|花字|文字|标题)(?:的)?(?:入场|循环)?(?:动画|动效)'
        r'|(?:字幕|花字|文字|标题)(?:保持静止|固定不动|保持不动|不动)', quote.strip()))


def _motion_only_quote(task, quote):
    if _motion_disable_only(quote):
        return True
    # A model may quote only “不要字幕” from “不要字幕动画”. The
    # complete source clause must determine which switch the user meant.
    clauses = [clause.strip() for clause in re.split(r'[,，。；;!！\n]', task.get('instruction', ''))
               if quote in clause]
    return bool(clauses) and all(_motion_disable_only(clause) for clause in clauses)


def _text_disable_quote(instruction):
    for clause in re.split(r'[,，。；;!！\n]', instruction or ''):
        match = re.search(
            rf'(?:{_NEGATIVE}|无)(?:任何|所有|全部|新增)?(?:字幕|花字|屏幕文字|文字|标题)(?=$|[和及与、])'
            rf'|(?:字幕|花字|屏幕文字|文字|标题)(?:都)?(?:{_NEGATIVE})(?=$|[和及与、])', clause)
        if match:
            return match.group()
        match = re.search(r'只(?:添加|加|要|保留|使用)?(?:背景)?音乐(?:即可|就好|就行)?$', clause)
        if match:
            return match.group()
    return None


def _supported_visual_selection(value):
    """Recognize only the renderer's known names or automatic-selection policy.

    An unsupported proprietary effect or a new kind of animation must still
    fail explicitly.  Keep evidence when the model mislabels a known preset.
    """
    from .caption_motion import ENTRIES, LOOPS
    from .visual_presets import TRANSITIONS
    text = re.sub(r'[\s，,。.!！；;：:、]', '', value)
    text = re.sub(r'^(?:当前|目前|暂时|暂)?(?:不支持|未支持|无法实现|无法)(?:自动实现)?', '', text)
    verbs = r'(?:使用|添加|增加|加入|启用|选择|应用)?'
    sticker_rows = _available_commerce_stickers()
    if sticker_rows:
        labels = '|'.join(re.escape(row['label'].split(' · ')[-1]) for row in sticker_rows)
        styles = []
        if any(row.get('style')=='premium_3d' for row in sticker_rows):
            styles.append(r'(?:高级立体|黑金|金属质感|立体)(?:电商)?贴纸')
        if any(row.get('style')=='cn_commerce' for row in sticker_rows):
            styles.append(r'(?:国内电商|IconPark)(?:电商)?贴纸')
        if re.fullmatch(rf'{verbs}(?:新增的)?(?:{"|".join(styles)}|(?:{labels})(?:贴纸)?)(?:素材)?',text,re.I):
            return True
    names = '|'.join(re.escape(label) for label in TRANSITIONS.values())
    if re.fullmatch(rf'(?:转场)?{verbs}(?:{names})(?:转场|效果)?', text):
        return True
    names = '|'.join(re.escape(label) for catalog in (ENTRIES, LOOPS)
                     for key, label in catalog.items() if key != 'none')
    if re.fullmatch(rf'(?:字幕|文字)(?:入场|循环)?{verbs}(?:{names})(?:动画|动效)?', text):
        return True
    # All tokens in a default-policy complaint must be known policy words;
    # a sentence about unsupported tracking/3D/templates cannot match this.
    policy_words = (r'(?:软件|当前|默认|自动|随机|从|所有|全部|种|不同|转场|字幕|文字|入场|循环|动画|动效|'
                    r'选择|挑选|抽取|选取|使用|添加|分配|布置|安排|搭配|启用|关闭|全片|统一|'
                    r'独立|每个|每条|每视频|视频|成品|一|二|三|个|以|概率|[0-9%])+')
    return bool(re.search(r'自动|随机|默认', text) and
                re.search(r'转场|(?:字幕|文字)(?:入场|循环|动画|动效)', text) and
                re.fullmatch(policy_words, text))


def _visual_preferences(task, result):
    if not isinstance(result, dict) or not isinstance(result.get('overrides'), dict):
        return result
    result = deepcopy(result)
    ignored = list(result.get('ignored_overrides') or [])
    for key, entry in list((result.get('overrides') or {}).items()):
        if not isinstance(entry, dict) or not isinstance(entry.get('quote'), str):
            continue
        quote = entry['quote']
        # Quoting an unrelated sentence is not an explicit request to turn
        # off the automatic visual policy, even when the quote is genuine.
        unrelated_disable = (key == 'transition' and entry.get('value') is False
                             and not _transition_disable_quote(quote))
        caption_clauses = [clause for clause in re.split(r'[,，。；;!！\n]', task.get('instruction', ''))
                           if quote in clause]
        unrelated_caption = key == 'text' and (
            (entry.get('value') is False and not _text_disable_quote(quote)) or
            (entry.get('value') is True and not any(re.search(
                r'字幕|花字|文字|字体|文案|标题|caption|subtitle|text', clause, re.I) for clause in caption_clauses)))
        animation_only = key == 'text' and _motion_only_quote(task, quote)
        if unrelated_disable or unrelated_caption or animation_only:
            del result['overrides'][key]
            ignored.append({'key': key, 'quote': quote,
                            'reason': 'no_explicit_category_switch'})
    if ignored:
        result['ignored_overrides'] = ignored
    if isinstance(result.get('unsupported'), list):
        supported = [value for value in result['unsupported']
                     if isinstance(value, str) and _supported_visual_selection(value)]
        if supported:
            result['unsupported'] = [value for value in result['unsupported'] if value not in supported]
            result['ignored_unsupported'] = [*result.get('ignored_unsupported', []), *supported]
    return result


def _voice_preferences(result):
    """Keep subjective voice goals for listening, not as absent capabilities.

    Only recognize whole, ordinary style requests. Voice cloning, actual human
    recording and proprietary effects remain unsupported if the model says so.
    Also applies to already hash-checked, saved requirement resolutions.
    """
    if not isinstance(result, dict) or not isinstance(result.get('unsupported'), list):
        return result
    preferences, unsupported = [], []
    for value in result['unsupported']:
        text = re.sub(r'[\s，,。.!！；;：:、]', '', value) if isinstance(value, str) else ''
        style = re.fullmatch(
            r'(?:声音|配音|口播|语音)(?:要|需要|应|请)?(?:不要|不应|避免)(?:太|过于)?机械(?:化|音|播报|感)?'
            r'|(?:声音|配音|口播|语音)(?:要|需要|应)?自然(?:流畅)?'
            r'|自然(?:流畅)?(?:的)?(?:日语|英语|中文)?(?:声音|配音|口播|语音)', text)
        if style:
            preferences.append(value)
        else:
            unsupported.append(value)
    if not preferences:
        return result
    existing = result.get('voice_quality_preferences', [])
    existing = [p for p in existing if isinstance(p, str)] if isinstance(existing, list) else []
    return {**result, 'unsupported': unsupported,
            'voice_quality_preferences': list(dict.fromkeys([*existing, *preferences]))}


def configure_rules(spec):
    from .caption_policy import configure
    spec = configure(spec)
    spec = deepcopy(spec)
    spec['editing_rules_version'] = VERSION
    spec['instruction_priority'] = 'user_instruction_then_category_defaults'
    custom = spec['workflow'] == 'custom'
    defaults = {key: not custom for key in FLAGS}
    # Automatic transitions decorate real boundaries; they do not request
    # additional captions, audio, source cuts, stickers or visual effects.
    defaults['transition'] = True
    defaults.update(narration='none', mute_source_audio=True)
    if spec.get('creation_mode') == 'long_video_v1':
        defaults.update(narration='preserve', mute_source_audio=False, transition=False)
    from .original_montage import active as original_montage
    if original_montage(spec):
        defaults.update(narration='none',mute_source_audio=False,text=False,music=False,
                        transition=True,sticker=True,sound=True,effect=True,filter=False,zoom=False)
    if not custom:
        spec['market_rules'] = market_rules.profile_for(spec['target_country'])
        spec['allow_single_source'] = True
        spec['transcription_language'] = 'auto'  # Input speech is independent of output language.
        defaults['narration'] = 'new'
        spec['defaults_applied'] = [s for s in spec.get('defaults_applied', []) if 'no narration' not in s]
        spec['defaults_applied'].append(market_rules.VERSION)
    spec['editing_defaults'] = defaults
    return apply_rules(spec, {'schema_version':'1.0', 'overrides':{}, 'unsupported':[]},
                       _explicit_fallback=False)


def _available_commerce_stickers():
    """Describe only locally present, fingerprint-verified downloaded assets."""
    import hashlib,json
    from .auto_decorations import ROOT
    root=ROOT/'sticker-library'
    try:
        rows=json.loads((root/'manifest.json').read_text(encoding='utf-8'))['stickers']
    except (OSError,ValueError,KeyError):return []
    available=[]
    for row in rows:
        if row.get('collection') not in ('premium_commerce_3d_v1','iconpark_cn_commerce'):continue
        path=(root/row.get('file','')).resolve()
        try:
            if path.is_relative_to(root.resolve()) and hashlib.sha256(path.read_bytes()).hexdigest()==row.get('sha256'):
                available.append(row)
        except OSError:continue
    return available


def _sticker_capability_prompt():
    rows=_available_commerce_stickers()
    if not rows:return ''
    premium=[row for row in rows if row.get('style')=='premium_3d']
    flat=[row for row in rows if row.get('style')=='cn_commerce']
    text=f'软件内已接入{len(premium)}款高级立体电商贴纸和{len(flat)}款国内电商扁平贴纸，按画面语境自动选用。'
    if premium:
        text+='高级立体、黑金、金属质感表示现成贴纸的材质风格，已支持，不是要求生成新3D模型，不能列入unsupported。'
        text+='高级立体款包含：'+'、'.join(row['label'].split(' · ')[-1] for row in premium)+'。'
    return text+'要求多种贴纸或大量贴纸表示在适合的镜头槽位反复使用现有素材与动画，不是新功能。未提供的指定IP角色、新建3D模型或主体跟踪贴纸仍不能伪装成已支持。'


def _visual_capability_prompt():
    """Describe the renderer's registered presets, not a drifting copied list."""
    from .caption_motion import ENTRIES, LOOPS
    from .compound_transitions import COMPOUND_TRANSITIONS
    from .visual_presets import STANDARD_TRANSITIONS, ANIMATIONS, EFFECTS
    entries = [label for key, label in ENTRIES.items() if key != 'none']
    loops = [label for key, label in LOOPS.items() if key != 'none']
    return (
        f"直接渲染已支持{len(STANDARD_TRANSITIONS)}种标准转场：{'、'.join(STANDARD_TRANSITIONS.values())}；"
        f"{len(COMPOUND_TRANSITIONS)}种组合转场：{'、'.join(COMPOUND_TRANSITIONS.values())}；"
        '每个新成品默认从全部转场中抽取3种不同款式，在已有相邻镜头边界随机布置；'
        '少于3处边界时按实际边界使用，单镜头不增加切镜。'
        + _sticker_capability_prompt()+
        f"贴纸动画支持{'、'.join(ANIMATIONS.values())}；"
        f"画面特效支持{'、'.join(EFFECTS.values())}。"
        f"文字入场支持{len(entries)}种：{'、'.join(entries)}；"
        f"文字循环支持{len(loops)}种：{'、'.join(loops)}。"
        '已有字幕时，每视频从全部文字入场中随机选1种并全片统一；'
        '文字循环以50%概率启用，启用后随机选1种并全片统一。'
        '显式指定或关闭优先于自动选择，重试同一任务保持稳定。'
        '这些自动能力不能写为unsupported，未提到转场时不能将transition设为false。'
        '文字入场与文字循环可以组合；明确要求添加文字动效可开启text；'
        '仅关闭字幕动画不代表新增或删除字幕。自动文字动效不强制新增本来没有的字幕，'
        '自定义任务未要求时不新增字幕、口播、贴纸或特效。')


def _unrequested_voice_capabilities(task, result):
    """A classifier's copied capability examples are not user requirements."""
    if not isinstance(result,dict) or not isinstance(result.get('unsupported'),list):
        return result
    instruction=task.get('instruction','')
    intent=re.search(r'克隆|复刻.{0,8}(?:声音|音色)|模仿.{0,12}(?:声音|音色)|真人(?:录音|录制|配音)|指定.{0,8}(?:人物|明星|本人|声优).{0,8}(?:声音|音色)|(?:用|使用|我的|本人).{0,4}(?:自己的声音|录制的声音)',instruction)
    if intent:return result
    kept=[];ignored=[]
    for value in result['unsupported']:
        if isinstance(value,str) and re.fullmatch(r'(?:(?:真人录音|真人配音|指定人物声音克隆|声音克隆|音色克隆|指定人物音色)[、，,；;和及与\s]*)+',value):
            ignored.append({'value':value,'reason':'voice_capability_not_requested_by_user'})
        else:kept.append(value)
    return {**result,'unsupported':kept,**({'ignored_unrequested_capabilities':ignored} if ignored else {})}


def resolve_rules(provider, task):
    result = provider._json(
        '读取用户剪辑提示词，提取用户明确要求，不为用户补写要求。只返回JSON：'
        '{"schema_version":"1.0","overrides":{"music":{"value":false,"quote":"用户原句"}},'
        '"unsupported":[]}。overrides只包含有明确要求的字段；未提到的字段省略。'
        '允许字段：music音乐、sound音效、sticker贴纸、transition转场、effect特效、filter滤镜、'
        'text新增字幕文案、zoom画面缩放、mute_source_audio原声静音，值为布尔；'
        'narration值为none不生成新配音或new生成新配音。quote必须是提示词中连续出现的原文。'
        '保留原声应mute_source_audio=false，不代表生成新配音；去掉原声/静音为true。'
        '不要配音则narration=none；只加音乐时关闭其他新增元素、字幕和口播。'
        '“按模板”不代表将模板字段当作用户明确指定。自定义不强制广告结构或销售话术。'
        '日区美区沿用当地语言文案模板，但用户明确要求可覆盖元素。'
        '有market_rules的日区美区默认生成对应日语或英语新口播；不要将默认要求写为unsupported。'
        '没有market_rules的旧日区美区任务仍只支持文案；自定义新配音目前只支持中文系统语音。'
        '若用户明确要求未支持的语音能力或剪映专属模板/特效，将具体要求写入unsupported。'
        + _visual_capability_prompt() +
        '用户将这些画面特效称为滤镜时也应开启effect，'
        'quote引用对应原文。这些已有能力不能写入unsupported；具体名称由渲染器按原文解析。'
        '“声音不要机械”“自然口播”是音质与表达目标，不是缺少功能，不能写入unsupported；'
        '它们应通过音色、自然停顿和最终试听检查落实，不宣称已保证真人效果。'
        '真人录音、指定人物声音克隆等实际未接入能力仍应明确列出。'
        '字幕纯色字色、描边色和底板色支持常用颜色名称或#RRGGBB/#RRGGBBAA，'
        '不要底板、不要描边也受支持；它们由渲染器按用户原文解析，不要写为unsupported。'
        '字幕支持整片统一放在上方、下方、中间、左侧、右侧、左上、右上、左下、右下；'
        '指定字幕位置表示需要新增字幕，应开启text并引用原句，除非用户明确不要新增字幕。'
        '字幕位置由渲染器按用户原文决定，已有位置能力不能写为unsupported；'
        '字幕还支持明确秒数的分时段位置和布局，例如前5秒字幕上方居中、5-10秒字幕下方两行；'
        '可用横向50%、纵向25%设置文字中心，字号1.2倍、字幕宽度80%、行距1.2倍设置布局。'
        '这些已有能力不要写为unsupported；竖排文字仍不支持，不要将竖屏画幅误当竖排字幕。'
        '不要改时长、素材顺序等其他要求，它们会原文交给剪辑规划器。',
        {'instruction':task['instruction'],'workflow':task['workflow'],
         'market':task.get('target_country'),'market_rules':task.get('market_rules'),
         'defaults':task['editing_defaults']})
    # The model is told to quote the user's prompt, but it can occasionally
    # copy a sentence from ``market_rules.common`` into an override.  Such an
    # override is not user authorization and must never silently change the
    # editing contract. Preserve the evidence as an ignored entry, then keep
    # the category default already present in ``task``.
    if isinstance(result, dict):
        # ``unsupported`` is an empty list in the requested schema. Treat an
        # omitted field as the empty list; the model's omission does not mean
        # it found an unsupported capability.
        if 'unsupported' not in result:
            result = {**result, 'unsupported': []}
        if 'overrides' not in result:
            result = {**result, 'overrides': {}}
    if isinstance(result, dict) and isinstance(result.get('overrides'), dict):
        ignored = []
        accepted = {}
        for key, entry in result['overrides'].items():
            quote = entry.get('quote') if isinstance(entry, dict) else None
            if isinstance(quote, str) and quote.strip() and quote in task['instruction']:
                accepted[key] = entry
            else:
                ignored.append({'key': key, 'quote': quote,
                                'reason': 'quote_not_in_user_instruction'})
        result = {**result, 'overrides': accepted}
        if ignored:
            result['ignored_overrides'] = ignored
    result = _visual_preferences(task, _voice_preferences(_unrequested_voice_capabilities(task,result)))
    validate_resolution(task, result)
    result = _explicit_request_fallback(task, result)
    return result


def validate_resolution(task, result):
    if (not isinstance(result,dict) or result.get('schema_version') != '1.0'
            or not isinstance(result.get('overrides'),dict)
            or not isinstance(result.get('unsupported'),list)
            or any(not isinstance(s,str) or not s.strip() for s in result['unsupported'])):
        raise ValueError('剪辑要求解析格式无效')
    allowed=set(FLAGS)|{'narration','mute_source_audio'}
    for key, entry in result['overrides'].items():
        if key not in allowed or not isinstance(entry,dict):
            raise ValueError('剪辑要求包含未知字段')
        quote=entry.get('quote');value=entry.get('value')
        if not isinstance(quote,str) or not quote.strip() or quote not in task['instruction']:
            raise ValueError('剪辑规则覆盖缺少用户提示词原文依据')
        if (key == 'narration' and value not in ('none','new')) or (key != 'narration' and type(value) is not bool):
            raise ValueError('剪辑规则覆盖值无效')


def apply_rules(task, result, *, _explicit_fallback=True):
    result = _voice_preferences(result)
    validate_resolution(task,result)
    result = _visual_preferences(task, result)
    if _explicit_fallback:
        result = _explicit_request_fallback(task, result)
    if result['unsupported']:
        raise ValueError('当前剪辑能力不能满足：'+'；'.join(result['unsupported']))
    spec=deepcopy(task)
    if result.get('voice_quality_preferences'):
        spec['voice_quality_preferences'] = result['voice_quality_preferences'][:]
        spec['voice_quality_verification'] = 'final_listening_required'
    options={**spec['editing_defaults'], **{k:v['value'] for k,v in result['overrides'].items()}}
    # Global placement is implemented locally, so custom workflows must not
    # depend on a model remembering to enable text for a positioning request.
    from .subtitle_styles import parse_position_instruction
    placement=parse_position_instruction(spec.get('instruction'))
    from .caption_layout import has_layout_request
    from .caption_motion import motion_from_instruction
    motion=motion_from_instruction(spec.get('instruction'))
    positive_motion = any(value != 'none' for key, value in motion.items() if key in ('entry', 'loop'))
    if (placement['values'] or has_layout_request(spec.get('instruction')) or positive_motion) and 'text' not in result['overrides']:
        options['text']=True
    transition_disabled = _transition_disable_quote(spec.get('instruction'))
    if transition_disabled:
        options['transition'] = False
    text_disabled = _text_disable_quote(spec.get('instruction'))
    if text_disabled:
        options['text'] = False
    if (spec.get('creation_mode') == 'long_video_v1' and options['narration'] == 'new'
            and 'mute_source_audio' not in result['overrides']):
        options['mute_source_audio'] = True
    if spec['workflow'] == 'crossborder_montage' and not market_rules.active(spec) and options['narration'] != 'none':
        raise ValueError('日区、美区当前仅支持文案，暂不支持生成配音')
    spec['resolved_editing_options']=options
    spec['explicit_editing_overrides']=deepcopy(result['overrides'])
    if transition_disabled:
        spec['explicit_editing_overrides']['transition'] = {'value': False, 'quote': transition_disabled}
    if text_disabled:
        spec['explicit_editing_overrides']['text'] = {'value': False, 'quote': text_disabled}
    policy=spec['formal_ad_policy']
    policy.update({v:options[k] for k,v in FLAGS.items()})
    policy.update(formal_ad=spec['workflow'] != 'custom', new_narration_required=options['narration']=='new',
                  rhythm_changes_required=False if spec['workflow']=='custom' else policy.get('rhythm_changes_required',True),
                  mode=VERSION)
    # An automatic preset pool is not a minimum number of native resources
    # or a reason to invent extra cuts in a short/single-shot video.
    explicit_transition = bool(result['overrides'].get('transition', {}).get('value'))
    categories=[k for k in FLAGS if k != 'zoom' and options[k]
                and (k != 'transition' or explicit_transition)]
    spec['required_native_categories']=categories
    spec['minimum_native_counts']={k:(2 if k in ('sound','text') else 1) for k in categories}
    policy['required_native_categories']=categories[:]
    policy['minimum_native_counts']=spec['minimum_native_counts'].copy()
    spec['narration_mode']='new_native' if options['narration']=='new' else (
        'preserve_source' if not options['mute_source_audio'] else 'text_only')
    spec['separate_narration']=options['narration']=='new'
    if market_rules.active(spec):
        spec['copy_only'] = options['narration'] == 'none'
        spec['narration_locale'] = spec['market_rules']['locale']
        spec['voice_quality_policy'] = 'reviewed_localized_voice_required'
    spec['mute_source_audio']=options['mute_source_audio']
    spec['constraints']['subtitles']=options['text']
    spec['subtitle_policy']=('editable_localized_selling_copy_plus_original_burned_text'
                            if spec['workflow']=='crossborder_montage' else
                            'user_prompt_text' if options['text'] else 'none')
    spec['enabled_direct_resources']=categories
    spec['require_direct_audio_resources']=False  # Per-kind flags remain authoritative.
    spec['require_online_font']=options['text']
    spec['minimum_online_fonts']=1 if options['text'] else 0
    from .original_montage import enforce
    return enforce(spec)
