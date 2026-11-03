"""Carry auditable subtitle colors and placement into the actual PNG render."""
from __future__ import annotations
import re
import hashlib
import math

COLORS = {
    '白色':'#FFFFFF','白':'#FFFFFF','white':'#FFFFFF',
    '黑色':'#000000','黑':'#000000','black':'#000000',
    '红色':'#FF0000','红':'#FF0000','red':'#FF0000',
    '黄色':'#FFFF00','黄':'#FFFF00','yellow':'#FFFF00',
    '蓝色':'#0000FF','蓝':'#0000FF','blue':'#0000FF',
    '绿色':'#00FF00','绿':'#00FF00','green':'#00FF00',
    '橙色':'#FF8C00','橙':'#FF8C00','orange':'#FF8C00',
    '粉色':'#FF69B4','粉红色':'#FF69B4','粉红':'#FF69B4','pink':'#FF69B4',
    '紫色':'#A855F7','紫':'#A855F7','purple':'#A855F7',
    '金色':'#FFD700','金':'#FFD700','gold':'#FFD700',
    '银色':'#C0C0C0','silver':'#C0C0C0','灰色':'#808080','gray':'#808080',
    '深灰色':'#404040','深灰':'#404040','浅灰色':'#D3D3D3',
    '青色':'#00FFFF','cyan':'#00FFFF','薄荷绿':'#A3FFD9',
    '奶油色':'#FFEBAE','米白色':'#FFF4DF','深蓝色':'#163B70',
    '深红色':'#B91C1C','浅蓝色':'#93C5FD','浅粉色':'#F9A8D4',
    '透明':'#00000000','transparent':'#00000000',
}
DEFAULT_STYLE = {'fill_color':'#FFFFFF', 'stroke_color':'#000000',
                 'background_color':'#00000000', 'stroke_width':2}
PALETTES = {'cream':'#FFEBAE', 'mint':'#A3FFD9', 'pink':'#FFBDC9'}
POSITIONS = {f'{v}_{h}' for v in ('top','middle','bottom') for h in ('left','center','right')}
COLOR_TOKEN = r'(?:#[0-9a-fA-F]{8}(?![0-9a-fA-F])|#[0-9a-fA-F]{6}(?![0-9a-fA-F])|' + '|'.join(
    re.escape(s) for s in sorted(COLORS,key=len,reverse=True)) + r')'
SUBJECTS = {
    'background_color':r'(?:(?:字幕|花字)背景|(?:字幕|花字)?(?:背景色|背景颜色|底板颜色|底板色|底色|底板|背景板))',
    'stroke_color':r'(?:字幕|花字)?(?:描边颜色|描边色|描边|边框颜色|轮廓色)',
    'fill_color':r'(?:字幕(?:文字颜色|字体颜色|颜色|文字|字体)?|花字(?:颜色)?|字色|文字颜色|字体颜色|(?:subtitle|caption|text)\s*(?:colou?r)?)',
}
CONNECTOR = r'\s*(?:的|颜色)?\s*(?:(?:设置为|调整为|换成|改成|改为|变成|变为|设为|使用|采用|选择|用|为|是|要|[:：=])\s*)*'

def normalize_color(value):
    if not isinstance(value,str):raise ValueError('字幕颜色必须是名称或 #RRGGBB / #RRGGBBAA')
    value=value.strip().casefold()
    if value in COLORS:return COLORS[value]
    if re.fullmatch(r'#[0-9a-f]{6}(?:[0-9a-f]{2})?',value):return value.upper()
    raise ValueError('无法识别字幕颜色：'+value)

def rgba(value):
    value=normalize_color(value)[1:]
    return tuple(int(value[i:i+2],16) for i in (0,2,4)) + (int(value[6:8],16) if len(value)==8 else 255,)

def normalize_style(value):
    if value is None:return {}
    if not isinstance(value,dict):raise ValueError('字幕样式必须是对象')
    result={}
    for key in ('fill_color','stroke_color','background_color'):
        if key in value:result[key]=normalize_color(value[key])
    if 'stroke_width' in value:
        width=value['stroke_width']
        if type(width) is not int or not 0<=width<=12:raise ValueError('字幕描边宽度必须为 0～12')
        result['stroke_width']=width
    if 'position' in value:
        if not isinstance(value['position'],str) or value['position'] not in POSITIONS:
            raise ValueError('字幕位置必须为上、中、下与左、中、右的九宫格位置')
        result['position']=value['position']
    for key,low,high in (('x',0,1),('y',0,1),('font_scale',.5,2),
                         ('wrap_width',.25,.95),('line_spacing',.8,2)):
        if key in value:
            number=value[key]
            if isinstance(number,bool) or not isinstance(number,(int,float)) or not math.isfinite(number) or not low<=number<=high:
                raise ValueError('字幕布局参数 '+key+' 超出允许范围')
            result[key]=float(number)
    for key in ('max_lines','line_count'):
        if key in value:
            if type(value[key]) is not int or not 1<=value[key]<=4:
                raise ValueError('字幕行数必须为1到4行')
            result[key]=value[key]
    return result


def parse_position_instruction(instruction, *, allow_timed=False):
    """Resolve scoped global placement, with later positive instructions winning.

    Only subtitle clauses and direct continuations may set position. A product,
    person, camera or sticker position must never move the captions.
    """
    subject = r'(?:字幕|花字|标题文字|caption[s]?|subtitle[s]?)'
    token = re.compile(
        r'左上(?:角|方|侧)?|右上(?:角|方|侧)?|左下(?:角|方|侧)?|右下(?:角|方|侧)?|'
        r'top[ -](?:left|right|center)|bottom[ -](?:left|right|center)|'
        r'上方|上面|顶部|顶端|上部|靠上|偏上|上边|上侧|'
        r'下方|下面|底部|底端|下部|靠下|偏下|下边|下侧|'
        r'靠左|偏左|左侧|左边|左方|靠右|偏右|右侧|右边|右方|'
        r'正中央|正中间|中间|中央|中心|居中|中部|正中|'
        r'(?<![a-z])(?:top|bottom|left|right|middle|center)(?![a-z])|'
        r'(?<=[在到放置于为])(?:上|下|左|右)(?![一二三四五六七八九十个条帧秒])|'
        r'[上下左右](?=$|\s)', re.I)
    x=y=None; quote=None; scoped=False
    for clause in re.split(r'[，,。；;\n]', str(instruction or '')):
        from .caption_layout import temporal_clause
        if not allow_timed and temporal_clause(clause):
            scoped=False
            continue
        match=re.search(subject,clause,re.I)
        if match:
            # Original burned-in subtitles are source pixels, not a new layer.
            if re.search(r'(?:原有|原片|原始|原)字幕',clause):
                scoped=False;continue
            scoped=True
            part=clause[match.end():]
            # "字幕上方的人物" describes the scene, not text placement.
            if re.search(r'(?:人物|商品|产品|贴纸|镜头|相机)',part):
                scoped=False;continue
        elif scoped and re.match(r'\s*(?:并|再|然后|最后|而是)?\s*(?:要放|请放|应放|放|置|移|改|靠|偏|居中|不要|别|不放)',clause):
            part=clause
        else:
            scoped=False;continue
        found=list(token.finditer(part))
        if not found:continue
        cx=cy=None
        for item in found:
            prefix=(clause[:match.end()] if match else '')+part[:item.start()]
            if re.search(r'(?:不要|不放|不在|不用|别|避免|禁止|不是|不能)',prefix) and not re.search(
                    r'(?:改为|改成|改到|而是|应放|(?<!不)要放|请放)',prefix):
                continue
            word=item.group().lower().replace(' ','_').replace('-','_')
            if word.startswith(('左上','右上','左下','右下')):
                cx='left' if word.startswith('左') else 'right'
                cy='top' if '上' in word else 'bottom'
            elif word.startswith(('top_','bottom_')):
                cy,cx=word.split('_')
            elif word in ('上','top') or re.search(r'上|顶',word):cy='top'
            elif word in ('下','bottom') or re.search(r'下|底',word):cy='bottom'
            elif word in ('左','left') or '左' in word:cx='left'
            elif word in ('右','right') or '右' in word:cx='right'
            else:
                if cx is None and cy is None:cx=cy='middle'
                elif cx is None:cx='center'
                elif cy is None:cy='middle'
        if cx is None and cy is None:continue
        if match:x,y=cx or 'center',cy or 'middle'
        else:x,y=cx or x or 'center',cy or y or 'middle'
        if x=='middle':x='center'
        quote=clause.strip()
    return {'values':{'position':f'{y}_{x}'} if quote else {},
            'evidence':{'position':quote} if quote else {}}

def parse_color_instruction(instruction, *, allow_timed=False):
    """Recognize scoped positive color instructions, never product/scene colors.

    Unsupported nuanced requests are not guessed. Supports solid fill, outline
    and background; precise colors use six/eight-digit hexadecimal notation.
    """
    instruction=str(instruction or '')
    if not allow_timed:
        from .caption_layout import temporal_clause
        instruction='；'.join(c for c in re.split(r'[，,。；;\n]',instruction) if not temporal_clause(c))
    events=[]
    for field,subject in SUBJECTS.items():
        patterns=[rf'(?P<subject>{subject}){CONNECTOR}(?P<color>{COLOR_TOKEN})',
                  rf'(?P<color>{COLOR_TOKEN})(?:的)?\s*(?P<subject>{subject})']
        for pattern in patterns:
            for match in re.finditer(pattern,instruction,re.I):
                prefix=re.split(r'[，,。；;\n]',instruction[:match.start()])[-1][-16:]
                if re.search(r'(?:不要|不需要|无需|不用|不使用|避免|禁止|别用|别|不是)\s*(?:添加|增加|加上|加|使用)?\s*(?:把|将)?\s*$',prefix):continue
                # Do not treat "黄色字幕背景" as a yellow foreground.
                if field=='fill_color' and re.match(r'(?:背景|底板|底色|描边)',instruction[match.end():]):continue
                events.append((match.start(),field,normalize_color(match['color']),match.group()))
    for match in re.finditer(rf'(?P<color>{COLOR_TOKEN})字(?![幕体号])', instruction):
        prefix=re.split(r'[，,。；;\n]',instruction[:match.start()])[-1]
        if not re.search(r'(?:不要|不用|别用|不是)\s*$',prefix):
            events.append((match.start(),'fill_color',normalize_color(match['color']),match.group()))
    removal = r'(?:不要|不需要|无需|不用|禁止|别|无)\s*(?:再)?\s*(?:添加|增加|加上|加|使用|保留|带|有)?|去掉|移除|取消|不(?:添加|增加|加上|加)'
    # A plain scene background is not a subtitle backplate. Only bare
    # 底板/背景板 is unambiguous without a subtitle subject.
    background = r'(?:(?:字幕|花字)\s*(?:的)?\s*(?:底板|背景板|背景颜色|背景色|背景|底色)|底板|背景板)'
    end = r'(?=$|[\s，,。；;！!])'
    patterns = [rf'(?:{removal})\s*(?:{background})(?!音乐|音效)',
                rf'(?:字幕|花字)\s*(?:{removal})\s*(?:底板|背景板|背景颜色|背景色|背景|底色)(?!音乐|音效)',
                rf'(?:{background})\s*(?:(?:不要|不需要|无需|不用|禁止|别)\s*(?:添加|增加|加上|加|使用|保留)|去掉|移除|取消|不要|不需要|无需|不用){end}',
                r'(?:字幕|花字)?(?:底板|背景板)(?:设为)?透明']
    for match in re.finditer('|'.join(patterns),instruction):
        prefix=re.split(r'[，,。；;\n]',instruction[:match.start()])[-1]
        if re.search(r'(?:不要|不需要|无需|不用|别)\s*(?:把|将)?\s*$',prefix):continue
        events.append((match.start(),'background_color','#00000000',match.group()))
    for match in re.finditer(r'(?:不要|去掉|移除|取消|不加)(?:字幕|花字)?描边',instruction):
        events.append((match.start(),'stroke_width',0,match.group()))
    values={};evidence={}
    for _,field,value,quote in sorted(events):
        values[field]=value;evidence[field]=quote
    return {'values':values,'evidence':evidence}

def resolve_style(task,cue=None):
    cue=cue or {}
    style=dict(DEFAULT_STYLE)
    # One choice per video, stable across captions and render retries. Most
    # videos use clean outlined text; a quarter retain the dark backplate.
    seed=task.get('subtitle_style_seed')
    auto_evidence={}
    if seed:
        backed=int.from_bytes(hashlib.sha256(str(seed).encode('utf-8')).digest()[:4],'big') % 4 == 0
        style['background_color']='#000000A5' if backed else '#00000000'
        auto_evidence['background_color']='auto_per_video_25pct_backplate' if backed else 'auto_per_video_no_backplate'
    # Existing planned palettes and structured per-cue color survive snapshotting.
    palette=(cue.get('ad_style') or {}).get('palette')
    if palette in PALETTES:style['fill_color']=PALETTES[palette]
    style.update(normalize_style(task.get('subtitle_style')))
    style.update(normalize_style(cue.get('subtitle_style')))
    if cue.get('color'):style['fill_color']=normalize_color(cue['color'])
    requested=parse_color_instruction(task.get('instruction'))
    style.update(requested['values'])
    placement=parse_position_instruction(task.get('instruction'))
    style.update(placement['values'])
    from .caption_layout import global_layout
    style.update(normalize_style(global_layout(task.get('instruction'))))
    style.update(normalize_style(cue.get('timed_subtitle_style')))
    return style,{**auto_evidence,**requested['evidence'],**placement['evidence']}
