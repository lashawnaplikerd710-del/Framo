"""Bounded, seek-safe animated captions with cropped alpha media.

The exporter owns font selection, wrapping and the final static image.  This
module only animates that image and its already positioned glyphs.  Entrance
clips contain at most 0.9 seconds; loops contain at most 2.4 seconds and are
reused by FFmpeg.  Frames are streamed one at a time, never retained as a
full-canvas frame array or a PNG sequence.

``render_caption_motion`` returns absolute-time overlay rows with ``kind``
(``image``/``video``), ``loop``, ``x`` and ``y``.  A video input must start at
local PTS zero and be shifted to ``start_us`` by the caller.  Loop inputs use
``-stream_loop -1``; every overlay must be bounded by ``end_us`` and use
``eof_action=pass:repeatlast=0``.  Static rendering remains the caller's exact
original PNG when neither animation is requested.
"""
from __future__ import annotations

import math
import re
import subprocess
import tempfile
import threading
import unicodedata
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps

ENTRIES = {
    'none': '无', 'fade': '淡入', 'pop': '整体弹入', 'bounce': '逐字弹跳',
    'stagger': '逐字上浮', 'assemble': '错位聚合', 'trail': '文字残影',
    'typewriter': '打字机', 'slideup': '向上滑入',
    'slidedown': '向下滑入', 'slideleft': '从左滑入', 'slideright': '从右滑入',
    'focus': '缩小聚焦', 'flip': '翻转展开', 'wipe': '擦拭显现', 'scatter': '散点汇聚',
}
LOOPS = {
    'none': '无', 'pulse': '呼吸缩放', 'float': '轻柔漂浮',
    'bounce': '逐字波浪', 'sway': '轻轻摇摆', 'shimmer': '逐字闪亮',
    'drift': '左右漂移', 'orbit': '环形浮动', 'stretch': '弹性伸缩',
    'pendulum': '钟摆摆动', 'glyph_pulse': '逐字缩放', 'glyph_sway': '逐字摇摆',
    'jitter': '轻微抖动', 'glow': '柔光呼吸', 'color_wave': '逐字变色', 'echo': '残影摆动',
}
DEFAULT_MOTION = {'entry': 'none', 'loop': 'none', 'entry_duration_ms': 650,
                  'loop_duration_ms': 1600, 'strength': .65}
_ENTRY_ALIASES = {
    **{v: k for k, v in ENTRIES.items()}, '弹入': 'pop', '逐字弹入': 'bounce', '弹跳': 'bounce',
    '逐字蹦跳': 'bounce', '错位组装': 'assemble', '文字聚合': 'assemble',
    '残影': 'trail', '拖尾': 'trail', '逐字出现': 'typewriter', '上滑入场': 'slideup',
    '下滑入场': 'slidedown', '左侧滑入': 'slideleft', '右侧滑入': 'slideright',
    '缩小入场': 'focus', '翻转入场': 'flip', '擦拭入场': 'wipe', '散点入场': 'scatter',
    '关闭': 'none',
}
_LOOP_ALIASES = {
    **{v: k for k, v in LOOPS.items()}, '呼吸': 'pulse', '漂浮': 'float',
    '波浪': 'bounce', '摇摆': 'sway', '闪亮': 'shimmer', '关闭': 'none',
    '横向漂移': 'drift', '圆周浮动': 'orbit', '弹性拉伸': 'stretch',
    '钟摆': 'pendulum', '字符缩放': 'glyph_pulse', '字符摇摆': 'glyph_sway',
    '轻微震动': 'jitter', '光晕呼吸': 'glow', '文字变色': 'color_wave', '残影摇摆': 'echo',
}


def _preset(value, choices, aliases, label):
    if not isinstance(value, str):
        raise ValueError(f'字幕{label}必须是已支持的动画名称')
    value = value.strip().lower()
    value = aliases.get(value, value)
    if value not in choices:
        raise ValueError(f'暂不支持此字幕{label}：{value}')
    return value


def normalize_motion(value=None):
    """Validate entry/loop settings; reject unknown presets and nonfinite data."""
    if value is None:
        return dict(DEFAULT_MOTION)
    if isinstance(value, str):
        value = {'entry': value}
    if not isinstance(value, dict):
        raise ValueError('字幕动画必须是对象或已支持的动画名称')
    result = dict(DEFAULT_MOTION)
    if 'entry' in value:
        result['entry'] = _preset(value['entry'], ENTRIES, _ENTRY_ALIASES, '入场动画')
    if 'loop' in value:
        result['loop'] = _preset(value['loop'], LOOPS, _LOOP_ALIASES, '循环动画')
    for key, lower, upper in (('entry_duration_ms', 120, 900), ('loop_duration_ms', 800, 2400)):
        if key in value:
            item = value[key]
            if type(item) is not int or not lower <= item <= upper:
                raise ValueError(f'字幕动画时长必须在 {lower}～{upper} 毫秒之间')
            result[key] = item
    if 'strength' in value:
        item = value['strength']
        if isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(item) or not 0 <= item <= 1:
            raise ValueError('字幕动画强度必须是 0～1 之间的有限数值')
        result['strength'] = float(item)
    return result


def motion_from_instruction(instruction):
    """Parse only explicit caption animation clauses, with later choices winning.

    This returns *overrides*, so an empty instruction never enables animation.
    A sticker or camera animation does not accidentally animate the captions.
    """
    result, scoped = {}, False
    subject = r'(?:字幕|花字|文字|标题|caption|subtitle|text)'
    continuation = r'\s*(?:并|再|然后|之后|保持|循环|持续|一直|入场|出现|改成|改为|换成|不要|不用|别|loop\b|entrance\b|entry\b)'
    catalogs = [('entry', ENTRIES, _ENTRY_ALIASES), ('loop', LOOPS, _LOOP_ALIASES)]
    for clause in re.split(r'[，,。；;\n]', str(instruction or '')):
        if re.search(subject, clause, re.I):
            scoped = True
        elif not scoped or not re.match(continuation, clause, re.I):
            scoped = False
            continue
        if re.search(r'(?:贴纸|相机|镜头|商品|人物|转场)', clause) and not re.search(subject, clause, re.I):
            scoped = False
            continue
        if re.search(r'保持静止|固定不动|静态字幕|字幕(?:保持)?不动', clause):
            result.update(entry='none', loop='none')
            continue
        disabled = re.search(r'(?:不要|不用|取消|关闭|禁止|无)(?:任何|全部|所有)?(?:字幕|花字|文字|标题)?(?:的)?(?:(入场|出现|循环))?(?:动画|动效)', clause)
        if disabled:
            if disabled[1] == '循环': result['loop'] = 'none'
            elif disabled[1]: result['entry'] = 'none'
            else: result.update(entry='none', loop='none')
            continue
        # Match both classes before resolving overlaps: the old short alias
        # “残影” must not steal the new loop “残影摆动”, nor may “摇摆”
        # override “逐字摇摆”. Equal spans retain both possible categories
        # only for shared English keys such as bounce.
        hits = []
        for key, choices, aliases in catalogs:
            names = {**{name: name for name in choices if name != 'none'},
                     **{name: code for name, code in aliases.items() if code != 'none'}}
            for name, code in names.items():
                pattern = re.escape(name) if not name.isascii() else rf'(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])'
                for match in re.finditer(pattern, clause, re.I):
                    hits.append((match.start(), match.end(), key, code))
        spans = sorted({(a,b) for a,b,_,_ in hits}, key=lambda ab: (-(ab[1]-ab[0]), ab[0]))
        accepted = []
        for a,b in spans:
            if not any(a < y and b > x for x,y in accepted): accepted.append((a,b))
        for a,b in sorted(accepted):
            candidates = {(key,code) for x,y,key,code in hits if (x,y)==(a,b)}
            before = clause[:a]
            markers = list(re.finditer(r'循环|持续|一直|入场|出现|(?<![a-z])(?:loop|entry|entrance)(?![a-z])', before, re.I))
            category = None
            if markers:
                category = 'loop' if re.fullmatch(r'循环|持续|一直|loop',markers[-1].group(),re.I) else 'entry'
            if category and any(key==category for key,_ in candidates):
                candidates = {(key,code) for key,code in candidates if key==category}
            elif category and len(candidates)==1:
                # Explicitly asking an entrance-only name to loop cannot be
                # silently converted into a different preset or category.
                continue
            elif len(candidates)>1:
                candidates = {(key,code) for key,code in candidates if key=='entry'}
            negative_tokens = list(re.finditer(r'不要|不用|取消|关闭|禁止|避免|不使用|不采用|别',before))
            positive_tokens = list(re.finditer(r'改成|改为|而是|换成|使用|采用|选择',before))
            negative = bool(negative_tokens) and not any(
                token.start() >= negative_tokens[-1].end() for token in positive_tokens)
            for key,code in candidates:
                if not negative: result[key] = code
                elif key not in result or result[key] == code: result[key] = 'none'
    return result


def graphemes(text):
    """Small dependency-free cluster split for CJK, combining marks and emoji."""
    result = []
    regional_run = 0
    for char in str(text):
        code = ord(char)
        regional = 0x1F1E6 <= code <= 0x1F1FF
        combining = unicodedata.combining(char) or unicodedata.category(char) in ('Mc', 'Me')
        extension = combining or code in (0xFE0E, 0xFE0F, 0x200D) or 0x1F3FB <= code <= 0x1F3FF
        if result and (extension or result[-1].endswith('\u200d') or (regional and regional_run % 2)):
            result[-1] += char
        else:
            result.append(char)
        regional_run = regional_run + 1 if regional else 0
    return result


def glyph_layer(text, font, *, fill, stroke_width=0, stroke_fill=None):
    """Return a tight glyph RGBA and Pillow's textbbox at the caller's origin."""
    draw = ImageDraw.Draw(Image.new('RGBA', (1, 1)))
    box = draw.textbbox((0, 0), str(text), font=font, stroke_width=stroke_width)
    image = Image.new('RGBA', (max(1, box[2]-box[0]), max(1, box[3]-box[1])), (0, 0, 0, 0))
    ImageDraw.Draw(image).text((-box[0], -box[1]), str(text), font=font, fill=fill,
                               stroke_width=stroke_width, stroke_fill=stroke_fill)
    return image, box


def _alpha(image, opacity):
    if opacity >= .999:
        return image
    result = image.copy()
    result.putalpha(image.getchannel('A').point(lambda a: round(a * max(0, min(1, opacity)))))
    return result


def _place(frame, image, x, y, *, scale=1, rotation=0, opacity=1, scale_x=1, scale_y=1):
    if opacity <= 0 or scale <= 0:
        return
    original_w, original_h = image.size
    if abs(scale*scale_x-1) > .001 or abs(scale*scale_y-1) > .001:
        image = image.resize((max(1, round(original_w*scale*scale_x)), max(1, round(original_h*scale*scale_y))), Image.Resampling.LANCZOS)
    if abs(rotation) > .01:
        image = image.rotate(rotation, Image.Resampling.BICUBIC, expand=True)
    image = _alpha(image, opacity)
    frame.alpha_composite(image, (round(x+(original_w-image.width)/2), round(y+(original_h-image.height)/2)))


def _ease(progress):
    return 1-(1-max(0, min(1, progress)))**3


def _entry_pose(kind, progress, i, count, glyph_h, strength):
    if kind == 'typewriter':
        shown = progress*max(1, count)
        return (0, 0, 1, 0, max(0, min(1, (shown-i)*2)))
    stagger = .38 if kind in ('bounce', 'stagger', 'assemble', 'trail') else .25 if kind=='scatter' else 0
    local = max(0, min(1, (progress-stagger*i/max(1, count-1))/(1-stagger)))
    eased = _ease(local)
    remaining = 1-eased
    opacity = max(0, min(1, local*4))
    if kind == 'bounce':
        dy = -glyph_h*(.30+.22*strength)*remaining
        dy += math.sin(local*math.pi*3)*math.exp(-local*5)*glyph_h*.35*strength
        return 0, dy, .55+.45*eased+math.sin(local*math.pi)*.10*strength, 0, opacity
    if kind in ('assemble', 'trail'):
        direction = -1 if i % 2 else 1
        dx = direction*glyph_h*(.25+.35*strength)*remaining
        dy = ((i % 3)-1)*glyph_h*(.40+.25*strength)*remaining
        return dx, dy, .50+.50*eased, direction*14*strength*remaining, opacity
    if kind == 'stagger':
        return 0, glyph_h*.55*remaining, .8+.2*eased, 0, opacity
    if kind == 'scatter':
        radial = i*2.399963229728653
        distance = glyph_h*(.75+.30*strength)*remaining
        return (math.cos(radial)*distance, math.sin(radial)*distance,
                .35+.65*eased, math.sin(radial)*42*strength*remaining, opacity)
    return 0, 0, 1, 0, eased


def _entry_frame(static, background, glyphs, kind, progress, strength):
    if progress >= 1:
        return static.copy()
    frame = Image.new('RGBA', static.size, (0, 0, 0, 0))
    if kind in ('fade', 'pop', 'slideup', 'slidedown', 'slideleft', 'slideright'):
        eased = _ease(progress)
        scale = (.72+.28*eased+math.sin(progress*math.pi)*.10*strength) if kind == 'pop' else 1
        dy = static.height*.22*(1-eased) if kind=='slideup' else -static.height*.22*(1-eased) if kind=='slidedown' else 0
        # Use the glyph height as a bounded distance even for very long lines.
        distance=max((g['image'].height for g in glyphs),default=48)*(.45+.2*strength)
        dx = -distance*(1-eased) if kind=='slideleft' else distance*(1-eased) if kind=='slideright' else 0
        _place(frame, static, dx, dy, scale=scale, opacity=min(1, progress*4))
        return frame
    if kind=='focus':
        eased=_ease(progress)
        softened=static.filter(ImageFilter.GaussianBlur((1-eased)*3.5*strength))
        _place(frame,softened,0,0,scale=1+.20*(1-eased),opacity=min(1,progress*4))
        return frame
    if kind=='flip':
        eased=_ease(progress)
        _place(frame,static,0,0,scale_y=max(.02,math.sin(eased*math.pi/2)),
               scale_x=1+.08*(1-eased),opacity=min(1,progress*4))
        return frame
    if kind=='wipe':
        softness=max(4,min(24,static.width//12))
        mask=Image.new('L',static.size,0)
        edge=progress*(static.width+softness)
        boundary=round(edge-softness)
        if boundary>0:ImageDraw.Draw(mask).rectangle((0,0,boundary,static.height),fill=255)
        gradient=Image.frombytes('L',(softness,1),bytes(round(255*(1-i/(softness-1))) for i in range(softness)))
        mask.paste(gradient.resize((softness,static.height)),(boundary,0))
        revealed=static.copy();revealed.putalpha(ImageChops.multiply(static.getchannel('A'),mask))
        frame.alpha_composite(revealed)
        return frame
    if background:
        frame.alpha_composite(_alpha(background, min(1, progress*5)))
    for i, glyph in enumerate(glyphs):
        dx, dy, scale, angle, opacity = _entry_pose(kind, progress, i, len(glyphs), glyph['image'].height, strength)
        if kind == 'trail' and progress < .98:
            for delay, alpha in ((.16, .10), (.08, .18)):
                ghost = _entry_pose(kind, max(0, progress-delay), i, len(glyphs), glyph['image'].height, strength)
                _place(frame, glyph['image'], glyph['x']+ghost[0], glyph['y']+ghost[1],
                       scale=ghost[2], rotation=ghost[3], opacity=ghost[4]*alpha*(1-progress))
        _place(frame, glyph['image'], glyph['x']+dx, glyph['y']+dy,
               scale=scale, rotation=angle, opacity=opacity)
    return frame


def _loop_frame(static, background, glyphs, kind, phase, strength):
    frame = Image.new('RGBA', static.size, (0, 0, 0, 0))
    # Modulo avoids precision drift on very late timeline ranges and gives
    # exact periodic identity at phase and phase+integer.
    phase = phase % 1
    angle = phase*2*math.pi
    if kind in ('pulse', 'float', 'sway'):
        # Start at the exact static pose. Sinusoidal paths remain continuous
        # across the encoded cycle boundary when stream_loop reuses the clip.
        scale = 1+math.sin(angle)*.025*strength if kind == 'pulse' else 1
        dy = math.sin(angle)*6*strength if kind == 'float' else 0
        rotation = math.sin(angle)*1.5*strength if kind == 'sway' else 0
        _place(frame, static, 0, dy, scale=scale, rotation=rotation)
        return frame
    if kind in ('drift','orbit','stretch','pendulum','jitter'):
        if kind=='drift':
            _place(frame,static,math.sin(angle)*8*strength,0)
        elif kind=='orbit':
            _place(frame,static,math.sin(angle)*7*strength,(math.cos(angle)-1)*4*strength)
        elif kind=='stretch':
            _place(frame,static,0,0,scale_x=1+math.sin(angle)*.065*strength,
                   scale_y=1-math.sin(angle)*.045*strength)
        elif kind=='pendulum':
            degrees=math.sin(angle)*4.5*strength
            radians=math.radians(degrees)
            # Pivot above the text instead of the center used by “sway”.
            bbox=static.getbbox() or (0,0,static.width,static.height)
            arm=static.height/2-bbox[1]+max(6,(bbox[3]-bbox[1])*.5)
            _place(frame,static,-math.sin(radians)*arm,(math.cos(radians)-1)*arm,rotation=-degrees)
        else:
            _place(frame,static,(math.sin(3*angle)+.4*math.sin(7*angle))*1.8*strength,
                   (math.cos(5*angle)+.3*math.sin(9*angle))*1.1*strength)
        return frame
    if kind in ('glow','echo'):
        ink=Image.new('RGBA',static.size)
        for glyph in glyphs:_place(ink,glyph['image'],glyph['x'],glyph['y'])
        if kind=='glow':
            glow=Image.new('RGBA',static.size,(210,235,255,0))
            alpha=ink.getchannel('A').filter(ImageFilter.GaussianBlur(3+2*strength))
            glow.putalpha(alpha.point(lambda value:round(value*(.12+.42*(1+math.sin(angle))/2)*strength)))
            frame.alpha_composite(glow);frame.alpha_composite(static)
        else:
            for delay,opacity in ((.48,.10),(.24,.18)):
                _place(frame,ink,math.sin(angle-delay)*9*strength,math.cos(angle-delay)*2.5*strength,
                       rotation=math.sin(angle-delay)*2*strength,opacity=opacity)
            _place(frame,static,math.sin(angle)*2*strength,0)
        return frame
    if background:
        frame.alpha_composite(background)
    for i, glyph in enumerate(glyphs):
        wave = math.sin(angle-i*.65)
        dy = wave*glyph['image'].height*.065*strength if kind == 'bounce' else 0
        opacity = 1-(.25*strength*(1-wave)/2) if kind == 'shimmer' else 1
        scale=1+wave*.09*strength if kind=='glyph_pulse' else 1
        rotation=wave*6*strength if kind=='glyph_sway' else 0
        image=glyph['image']
        if kind=='color_wave':
            hue=angle-i*.72
            rgb=tuple(round(170+75*math.cos(hue+shift)) for shift in (0,2*math.pi/3,4*math.pi/3))
            tinted=ImageOps.colorize(image.convert('L'),black=(0,0,0),white=rgb).convert('RGBA')
            tinted.putalpha(image.getchannel('A'));image=tinted
        _place(frame, image, glyph['x'], glyph['y']+dy, opacity=opacity,scale=scale,rotation=rotation)
    return frame


def _write_alpha_video(path, frames, *, size, fps, ffmpeg_path):
    command = [str(ffmpeg_path), '-hide_banner', '-loglevel', 'error', '-y',
               '-f', 'rawvideo', '-pixel_format', 'rgba', '-video_size', f'{size[0]}x{size[1]}',
               '-framerate', str(fps), '-i', 'pipe:0', '-an', '-c:v', 'qtrle', '-pix_fmt', 'argb', str(path)]
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as exc:
            raise ValueError('无法启动字幕动画渲染器，请检查 FFmpeg 路径') from exc
        watchdog = threading.Timer(90, process.kill)
        watchdog.daemon = True
        watchdog.start()
        try:
            try:
                for frame in frames:
                    process.stdin.write(frame.tobytes())
                process.stdin.close()
                code = process.wait(timeout=30)
            except (OSError, subprocess.SubprocessError) as exc:
                process.kill()
                process.wait()
                raise ValueError('字幕动画渲染中断，请检查渲染器与可用磁盘空间') from exc
            if code:
                errors.seek(0)
                detail = errors.read(2048).decode('utf-8', 'replace').strip()
                raise ValueError('字幕动画渲染失败，请检查渲染器与可用磁盘空间') from RuntimeError(detail)
        finally:
            watchdog.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            if process.stdin and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass


def render_caption_motion(static_image, glyphs, output_dir, *, name, start_us, end_us,
                          motion, ffmpeg_path=None, fps=30, background_image=None, phase_us=0):
    """Produce one cropped entrance clip plus a static/loop hold overlay.

    Glyph records contain ``image`` (tight RGBA), ``x`` and ``y`` (absolute
    canvas position).  ``background_image`` contains only the background
    panel; it must have the same dimensions as ``static_image``.  Animated
    entry frames settle on the exact original static raster, preserving
    Pillow's whole-line kerning and the original hold appearance.
    """
    config = normalize_motion(motion)
    for value in (start_us, end_us, phase_us):
        if type(value) is not int or value < 0:
            raise ValueError('字幕动画时间必须是非负整数微秒')
    if end_us <= start_us:
        raise ValueError('字幕动画结束时间必须晚于开始时间')
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 1 <= fps <= 120:
        raise ValueError('字幕动画帧率必须是 1～120 之间的有限数值')
    fps = min(30, max(12, round(fps)))
    static_image = static_image.convert('RGBA')
    if background_image is not None and background_image.size != static_image.size:
        raise ValueError('字幕动画背景尺寸与字幕画幅不一致')
    bounds = static_image.getbbox()
    if not bounds:
        return []
    glyphs = list(glyphs or [])
    if len(glyphs) > 320:
        raise ValueError('单条动态字幕超过 320 字，请拆分为较短的字幕')
    for glyph in glyphs:
        if not isinstance(glyph, dict) or not isinstance(glyph.get('image'), Image.Image):
            raise ValueError('字幕动画字形资源不完整')
        for key in ('x', 'y'):
            if isinstance(glyph.get(key), bool) or not isinstance(glyph.get(key), (int, float)) or not math.isfinite(glyph[key]):
                raise ValueError('字幕动画字形位置必须是有限数值')
    active = config['entry'] != 'none' or config['loop'] != 'none'
    if active and not glyphs and (config['entry'] in ('bounce', 'stagger', 'assemble', 'trail', 'typewriter','scatter') or
                                 config['loop'] in ('bounce','shimmer','glyph_pulse','glyph_sway','glow','color_wave','echo')):
        raise ValueError('逐字字幕动画缺少已排版字形，无法准确生成')
    if active and not ffmpeg_path:
        raise ValueError('字幕动画需要可用的 FFmpeg 渲染器')
    # Include only the text region plus bounded movement allowance.  No
    # full-canvas per-frame allocation even for 4K source video.
    pad = max(16, round(max((g['image'].height for g in glyphs), default=48)*.85)) if active else 0
    if config['entry']=='scatter':
        pad=max(pad,round(max((g['image'].height for g in glyphs),default=48)*1.15))
    elif config['entry'] in ('focus','flip'):
        pad=max(pad,min(128,math.ceil((bounds[2]-bounds[0])*.12)))
    left, top = max(0, bounds[0]-pad), max(0, bounds[1]-pad)
    right, bottom = min(static_image.width, bounds[2]+pad), min(static_image.height, bounds[3]+pad)
    region = (left, top, right, bottom)
    static = static_image.crop(region)
    if static.width*static.height > 2_500_000:
        raise ValueError('动态字幕区域过大，请缩小字号或减少行数')
    background = background_image.convert('RGBA').crop(region) if background_image is not None else None
    placed = [{**glyph, 'image': glyph['image'].convert('RGBA'), 'x': glyph['x']-left, 'y': glyph['y']-top} for glyph in glyphs]
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r'[^0-9A-Za-z._-]', '_', str(name)).strip('._') or 'caption'
    slug = slug[:90]
    result = []
    hold_start = start_us
    entry = config['entry']
    duration_us = end_us-start_us
    if entry != 'none' and duration_us >= 150_000:
        desired = min(duration_us, config['entry_duration_ms']*1000)
        frames = max(2, min(math.ceil(desired/1_000_000*fps), math.floor(.9*fps)))
        actual = min(duration_us, round(frames/fps*1_000_000))
        path = output_dir/f'.{slug}-entry.mov'
        _write_alpha_video(path, (_entry_frame(static, background, placed, entry, i/(frames-1), config['strength'])
                                  for i in range(frames)), size=static.size, fps=fps, ffmpeg_path=ffmpeg_path)
        result.append({'path': str(path), 'kind': 'video', 'loop': False, 'start_us': start_us,
                       'end_us': start_us+actual, 'x': left, 'y': top, 'motion': dict(config),
                       'resource_duration_us': round(frames/fps*1_000_000)})
        hold_start += actual
    if hold_start < end_us:
        loop = config['loop']
        if loop != 'none' and duration_us >= 150_000:
            frames = max(2, min(round(config['loop_duration_ms']/1000*fps), math.floor(2.4*fps)))
            cycle_us = round(frames/fps*1_000_000)
            # Anchor every range to the original cue's elapsed time.  Layout
            # splits suppress replaying the entrance; their loop must still
            # match the phase already visible immediately before the split.
            phase = ((phase_us+hold_start-start_us) % cycle_us)/cycle_us
            path = output_dir/f'.{slug}-loop.mov'
            _write_alpha_video(path, (_loop_frame(static, background, placed, loop, phase+i/frames, config['strength'])
                                      for i in range(frames)), size=static.size, fps=fps, ffmpeg_path=ffmpeg_path)
            kind, looping = 'video', True
        else:
            path = output_dir/f'.{slug}-hold.png'
            static.save(path, 'PNG')
            kind, looping = 'image', False
            cycle_us = None
        result.append({'path': str(path), 'kind': kind, 'loop': looping, 'start_us': hold_start,
                       'end_us': end_us, 'x': left, 'y': top, 'motion': dict(config),
                       'resource_duration_us': cycle_us})
    return result
