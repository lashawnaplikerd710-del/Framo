"""Independent selling copy, with an explicit opt-in to verbatim subtitles."""
import re

VERSION = 'independent_highlights_v1'
RULE = ('屏幕文案与口播独立创作：默认heading只显示当前镜头有依据的关键卖点，'
        '英语通常2到6词，中文或日语通常4到12字，避免完整口播长句。'
        '可留空让展示镜头呼吸，不强制每个镜头都有文字，不连续重复同一卖点。'
        'voiceover讲完整连贯内容，可跨连续镜头；部分镜头voiceover可为空字符串表示纯展示，'
        '不要为每个镜头硬写一句口播；整片保留至少一段完整口播。屏幕文案可概括或补充，不要求逐字相同。'
        '用户明确要求逐字字幕时才将口播作为字幕。验收看事实依据、可读性与用户要求，'
        '不因短文案与口播不同、跨镜头说话或合理无字画面而拒绝。')


def configure(task):
    if task.get('render_backend') != 'direct_ffmpeg' or task.get('workflow') not in ('custom','product_ad','crossborder_montage'):
        return task
    instruction = task.get('instruction', '')
    mode = task.get('caption_mode', 'highlights')
    for match in re.finditer(r'(?:不要|不用|无需)?(?:逐字字幕|完整口播字幕|字幕与口播一致|字幕跟随口播)|只显示关键(?:词|卖点)|只显示重点|卖点短字幕', instruction):
        mode = 'highlights' if match.group().startswith(('不要','不用','无需','只显示','卖点')) else 'transcript'
    if mode not in ('highlights','transcript'):
        raise ValueError('字幕模式必须是 highlights 或 transcript')
    return {**task, 'caption_policy_version':VERSION, 'caption_mode':mode}


def highlights(task):
    return task.get('caption_policy_version') == VERSION and task.get('caption_mode') == 'highlights'


def acceptance(task, snapshot, media_plan):
    """Machine checks only; semantic review remains a separate model gate."""
    checks=[]
    texts = snapshot.get('materials',{}).get('texts',[])
    total=snapshot['duration']
    spans=[s['target_timerange'] for t in snapshot['tracks'] if t['type']=='text' for s in t.get('segments',[])]
    checks.append({'name':'caption_intervals','passed':all(0 <= s['start'] < s['start']+s['duration'] <= total for s in spans)})
    options=task.get('resolved_editing_options',{})
    if 'text' in options:
        checks.append({'name':'user_text_switch','passed':bool(texts)==options['text']})
    if 'mute_source_audio' in options:
        checks.append({'name':'user_source_audio_switch','passed':snapshot['source_audio_muted']==options['mute_source_audio']})
    for kind in ('music','sound','sticker'):
        if kind in options:
            exists=any(t['type']==kind and t.get('segments') for t in snapshot['tracks'])
            checks.append({'name':'user_'+kind+'_switch','passed':exists==options[kind]})
    if 'narration' in options:
        exists=any(t['type']=='audio' and t.get('segments') for t in snapshot['tracks'])
        checks.append({'name':'user_narration_switch','passed':exists==(options['narration']=='new')})
    from .subtitle_styles import parse_color_instruction, parse_position_instruction
    from .caption_layout import schedule_from_task, global_layout
    schedule=schedule_from_task(task,total)
    requested={**parse_color_instruction(task.get('instruction'))['values'],
               **parse_position_instruction(task.get('instruction'))['values'],**global_layout(task.get('instruction'))}
    for key,value in requested.items():
        present=bool(texts) or options.get('text') is False
        if not any(key in r['style'] for r in schedule):
            checks.append({'name':'user_subtitle_'+key,'passed':present and all(
                t.get('subtitle_style',{}).get(key)==value for t in texts)})
    if schedule and options.get('text') is not False:
        by_id={t['id']:t for t in texts}
        valid=bool(texts)
        for track in snapshot['tracks']:
            if track['type']!='text':continue
            for segment in track.get('segments',[]):
                span=segment['target_timerange'];start=span['start'];end=start+span['duration']
                expected=dict(requested)
                for row in schedule:
                    if start<row['start_us']<end or start<row['end_us']<end:valid=False
                    if row['start_us']<=start<row['end_us']:expected.update(row['style'])
                for source in (by_id.get(segment.get('material_id'),{}),segment):
                    valid=valid and all(source.get('subtitle_style',{}).get(k)==v for k,v in expected.items())
        checks.append({'name':'user_timed_subtitle_layout','passed':valid})
    observations=[]
    cues=media_plan.get('narration_cues') or []
    if cues and total-max(c['end_us'] for c in cues)>2_000_000:
        observations.append('末段留有超过2秒的无口播展示；允许留白，需按创意意图复核。')
    return {'schema_version':'1.0','status':'passed' if all(c['passed'] for c in checks) else 'failed',
            'caption_mode':task.get('caption_mode','legacy'), 'checks':checks,
            'editorial_observations':observations,'requires_verbatim_match':task.get('caption_mode')=='transcript',
            'semantic_review':'separate_pre_render_model_review','audio_listened':False,
            'limits':'自动检查不代表审美或配音听感每次都合格；不强制字幕与口播逐字一致或全程有字。'}
