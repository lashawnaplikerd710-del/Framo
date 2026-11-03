"""Select local fonts/stickers from timestamped visual observations and user rules."""
from functools import lru_cache
from pathlib import Path
import hashlib,json,re,shutil
from .font_library import FontLibrary
from .framo_supplements import observation_box

ROOT=Path(__file__).resolve().parents[1]/'desktop-assets/media-library'
def domestic_commerce(task):
    """Use the Chinese commerce collection only in relevant local edits."""
    if task.get('target_locale','').startswith(('ja','en')) or task.get('target_country') in ('日本','美国'):
        return False
    return bool(re.search(r'电商|带货|优惠券|红包|加购',str(task.get('instruction',''))))

def commerce_style(task,catalog):
    """Prefer the dimensional pack, while keeping explicitly requested flat art."""
    instruction=str(task.get('instruction',''))
    if re.search(r'扁平|IconPark',instruction,re.I):return 'cn_commerce'
    return 'premium_3d' if any(r.get('style')=='premium_3d' for r in catalog) else 'cn_commerce'

KEYWORDS={
 'fashion':('衣服','服装','穿搭','衬衫','裤','鞋','包包','裙','shirt','clothing','fashion','シャツ'),
 'pet':('宠物','猫','狗','犬','小动物','cat','dog','pet','ねこ'),
 'food':('食物','食品','咖啡','蛋糕','水果','饮料','餐','food','coffee','cake','料理'),
 'travel':('旅行','旅游','风景','行李','海滩','飞机','travel','beach','suitcase','旅行'),
 'fitness':('运动','健身','篮球','跑步','球场','sport','fitness','basketball','ランニング'),
 'technology':('电脑','手机','耳机','游戏','电子','computer','phone','gaming','keyboard','スマホ'),
 'education':('书本','书籍','学习','课堂','知识','教程','book','study','education','本を'),
 'nature':('植物','花草','树','森林','自然','盆栽','plant','tree','nature','forest'),
 'beauty':('美妆','化妆','护肤','口红','香水','beauty','makeup','lipstick','cosmetic'),
 'music':('音乐','乐器','吉他','钢琴','music','guitar','piano'),
 'celebration':('生日','礼物','庆祝','派对','birthday','gift','party'),
}
THEME_TAGS={'fashion':['elegant','clean'],'pet':['cute','soft'],'food':['lifestyle','soft'],
 'travel':['lifestyle','casual'],'fitness':['sport','bold'],'technology':['technology','clean'],
 'education':['professional','clean'],'nature':['lifestyle','soft'],'beauty':['elegant','soft'],
 'music':['energetic'],'celebration':['playful','cute'],'general':['clean']}

@lru_cache(maxsize=2)
def fonts(root=str(ROOT)):
    path=Path(root)/'font-library'
    return FontLibrary(path) if (path/'manifest.json').is_file() else None

def collect_visual_context(timeline,work_dir):
    """Use only accepted image observations within selected source windows."""
    provided=timeline.get('visual_style_context')
    if isinstance(provided,list):return provided
    if not work_dir:return []
    cache={};result=[]
    for index,mapping in enumerate(timeline.get('source_map',[])):
        aid=mapping.get('asset_id','')
        if not isinstance(aid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',aid):continue
        if aid not in cache:
            rows=[]
            for path in sorted((Path(work_dir)/'sources'/aid).glob('visual-observations-*.json')):
                report=json.loads(path.read_text(encoding='utf-8-sig'))
                if report.get('image_input_accepted') is True and report.get('asset_id')==aid:
                    rows.extend(report.get('frames',[]))
            cache[aid]=rows
        start,end=mapping['source_start_us'],mapping['source_end_us']
        for row in cache[aid]:
            if (row.get('confidence') in ('high','medium') and type(row.get('requested_us')) is int
                    and start<=row['requested_us']<end
                    and re.fullmatch(r'[a-fA-F0-9]{64}',str(row.get('frame_sha256','')))):
                result.append({**row,'clip_index':index,'timeline_start_us':mapping['timeline_start_us'],
                               'timeline_end_us':mapping['timeline_end_us']})
    return result

def theme_for(context):
    scores={theme:0 for theme in KEYWORDS}
    for row in context:
        if row.get('confidence') not in ('high','medium'):continue
        for field,weight in [('subject',6),('description',2),('visible_features',1)]:
            text=row.get(field,'');text=' '.join(text) if isinstance(text,list) else str(text)
            text=text.casefold()
            for theme,words in KEYWORDS.items():
                scores[theme]+=weight*sum(
                    bool(re.search(r'(?<![a-z])'+re.escape(word)+r's?(?![a-z])',text))
                    if word.isascii() else word in text for word in words)
    best=max(scores,key=lambda k:scores[k])
    return (best,scores[best]) if scores[best] else ('general',0)

def tags_for(context,instruction=''):
    theme,_=theme_for(context);tags=list(THEME_TAGS[theme])
    style_words={'可爱':'cute','柔和':'soft','国风':'traditional','手写':'handwritten',
                 '科技':'technology','运动':'sport','活泼':'playful','复古':'traditional',
                 '高级':'elegant','简洁':'clean'}
    for word,tag in style_words.items():
        if word in instruction and tag not in tags:tags.append(tag)
    return tags

def select_caption_font(task,cue,context):
    library=fonts()
    if library is None:return None
    text=cue['text']
    hint=str(task.get('target_locale') or '')
    if hint in ('','auto'):hint=str(task.get('narration_locale') or '')
    if hint.startswith('ja'):locale='ja'
    elif hint in ('zh-TW','zh-Hant'):locale='zh-TW'
    elif hint.startswith('en') and not re.search(r'[\u3400-\u9fff]',text):locale='en'
    elif re.search(r'[\u3040-\u30ff]',text):locale='ja'
    elif re.search(r'[\u3400-\u9fff]',text):locale='zh-CN'
    else:locale='en'
    role='body' if cue.get('narration_cue_id') else ('heading' if cue.get('source_cue_id','').startswith('heading_') else 'body')
    preferred=task.get('subtitle_font_id') or cue.get('font_id')
    instruction=str(task.get('instruction',''))
    if not preferred:
        matches=[(len(name),family['id']) for family in library.families.values()
                 for name in (family['label'].split(' / ')[0],family['id'])
                 if name.casefold() in instruction.casefold()]
        if matches:preferred=max(matches)[1]
    if preferred and preferred in library.families and role not in library.families[preferred]['roles']:
        role='heading'  # An explicit requested display font may style the whole subtitle.
    choice=library.select(text,locale,role,tags=tags_for(context,instruction),preferred=preferred)
    choice.update(library_version=library.manifest['version'],style_basis='selected_visual_observations_and_user_instruction')
    return choice

def load_selected_font(choice,pixels):
    library=fonts()
    if library is None:raise ValueError('已选字体库不存在，请恢复软件内置字体库')
    family=library.families.get(choice.get('font_id'))
    if not family:raise ValueError('快照引用了不存在的字体')
    record=next((r for r in family['files'] if str(library.root/r['file'])==choice.get('path')),None)
    if not record or record['sha256']!=choice.get('sha256'):raise ValueError('字体快照指纹与内置字体库不一致')
    library._path(record)
    return library.load_pillow_font(choice,pixels)

def _safe_sticker_position(evidence,canvas,size,index,y_ratio=.035):
    """Choose the least-overlapping upper corner when detector boxes exist."""
    candidates=[(int(canvas['width']*.07),int(canvas['height']*y_ratio)),
                (int(canvas['width']*.93)-size,int(canvas['height']*y_ratio))]
    boxes=[observation_box(row) for row in evidence]
    boxes=[box for box in boxes if box]
    if not boxes:return candidates[index%len(candidates)],False
    width,height=float(canvas['width']),float(canvas['height'])
    scored=[]
    for x,y in candidates:
        left,top,right,bottom=x/width,y/height,(x+size)/width,(y+size)/height
        overlap=0.0
        for box in boxes:
            overlap+=max(0.0,min(right,box['x']+box['w'])-max(left,box['x']))*max(
                0.0,min(bottom,box['y']+box['h'])-max(top,box['y']))
        scored.append((overlap,x,y))
    _,x,y=min(scored,key=lambda row:(row[0],row[1]))
    return (x,y),True

def plan_stickers(task,timeline,canvas,work_dir,context):
    path=ROOT/'sticker-library/manifest.json'
    if not work_dir or not path.is_file():return [],{'status':'library_unavailable','selections':[]}
    instruction=str(task.get('instruction',''))
    override=(task.get('explicit_editing_overrides') or {}).get('sticker')
    disabled=(override and override.get('value') is False) or (task.get('resolved_editing_options') or {}).get('sticker') is False
    if disabled or re.search(r'(?:不要|不加|不添加|不需要|去掉|取消)(?:任何)?贴纸|不添加任何元素|只剪辑',instruction):
        return [],{'status':'disabled_by_user','selections':[]}
    from .original_montage import active as original_montage, stickers
    if original_montage(task):
        return stickers(task,timeline,canvas,work_dir,context,ROOT)
    catalog=json.loads(path.read_text(encoding='utf-8'))
    domestic=domestic_commerce(task)
    preferred_style=commerce_style(task,catalog['stickers']) if domestic else '3d'
    duration=timeline['duration_us'];limit=min(6,max(1,duration//8_000_000))
    size=max(40,min(180,int(canvas['width']*.10)))
    selections=[];seen=set();next_start=0
    for index,mapping in enumerate(timeline['source_map']):
        start,end=mapping['timeline_start_us'],mapping['timeline_end_us']
        if start<next_start or end-start<1_000_000 or len(selections)>=limit:continue
        evidence=[r for r in context if r.get('clip_index')==index]
        theme,score=theme_for(evidence)
        if domestic:theme='commerce'
        if theme=='general' and any(r['theme']=='general' for r in selections):continue
        candidates=[r for r in catalog['stickers'] if r.get('style')==preferred_style
                    and theme in r['themes'] and r['design'] not in seen]
        if theme=='general':candidates=[r for r in candidates if r.get('neutral') is True]
        if not candidates:continue
        subject=' '.join(str(r.get('subject',''))+' '+str(r.get('description','')) for r in evidence
                         if r.get('confidence') in ('high','medium')).casefold()
        def rank(item):
            matching=sum(bool(re.search(r'(?<![a-z])'+re.escape(k.casefold())+r's?(?![a-z])',subject))
                         if k.isascii() else k in subject for k in item.get('keywords',[]))
            return matching*10+(item.get('style')==preferred_style)
        highest=max(map(rank,candidates))
        if not domestic and theme!='general' and highest<10:
            generic_ids={'fluent_paw_prints','fluent_musical_notes','fluent_light_bulb'}
            generic=[r for r in candidates if r['id'] in generic_ids]
            candidates=generic or [r for r in catalog['stickers'] if r.get('neutral') is True
                        and r['design'] not in seen and r.get('style')=='3d']
            if not candidates:continue
            highest=max(map(rank,candidates))
        candidates=[r for r in candidates if rank(r)==highest]
        chosen=candidates[index%len(candidates)]
        size=(max(48,min(460,chosen['width'],chosen['height'],int(canvas['width']*.24),int(canvas['height']*.22))) if domestic
              else max(40,min(180,int(canvas['width']*.10))))
        source=ROOT/'sticker-library'/chosen['file']
        if hashlib.sha256(source.read_bytes()).hexdigest()!=chosen['sha256']:raise ValueError('贴纸素材指纹不匹配')
        cache=Path(work_dir)/'direct-resources/stickers';cache.mkdir(parents=True,exist_ok=True)
        target=cache/f"{chosen['id']}-{size}.png"
        from PIL import Image
        with Image.open(source) as image:
            image.resize((size,size),Image.Resampling.LANCZOS).save(target)
        position,occlusion_aware=_safe_sticker_position(evidence,canvas,size,index,.10 if domestic else .035)
        selected={**chosen,'path':str(target.resolve()),'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
          'source_sha256':chosen['sha256'],'start_us':start,'end_us':min(end,start+1_800_000),
          'x':position[0],'y':position[1],'theme':theme,'theme_score':score,'clip_index':index,
          'occlusion_aware':occlusion_aware,
          'reason':'匹配已识别画面主题' if score else '主题不明确，使用中性装饰',
          'frame_sha256':[r['frame_sha256'] for r in evidence if r.get('frame_sha256')]}
        selections.append(selected);seen.add(chosen['design']);next_start=start+6_000_000
    return selections,{'status':'selected' if selections else 'no_suitable_slot','selections':selections,
                       'placement_policy':'large_commerce_corner' if domestic else 'small_upper_corner','max_per_video':limit,
                       'subject_occlusion_detection':('bbox_aware_heuristic' if any(item.get('occlusion_aware') for item in selections) else 'not_available')}
