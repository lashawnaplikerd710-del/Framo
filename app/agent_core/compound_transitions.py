"""Native, deterministic compound transitions for the direct FFmpeg renderer.

Inputs contain the renderer's held boundary frames. Spatial filters act only
inside the transition window; all branches consume matching timestamps from
the beginning, with no trim that reads far ahead into a future transition.
No additional media, model calls, or source handles are required, and
narration/caption time coordinates stay unchanged.
"""
import math
import re


COMPOUND_TRANSITIONS = {
    'whip_blur_left': '向左甩动模糊',
    'whip_blur_right': '向右甩动模糊',
    'mirror_whip': '镜像甩动',
    'chromatic_zoom': '彩色重影推进',
    'whip_blur_up': '向上甩动模糊',
    'whip_blur_down': '向下甩动模糊',
    'rotate_zoom': '旋转缩放',
    'pullback_bounce': '拉远回弹',
    'flashwhite_zoom': '闪白推进',
    'flashblack_zoom': '闪黑缩放',
    'squeeze_horizontal': '横向挤压',
    'squeeze_vertical': '纵向挤压',
    'glitch_horizontal': '横向故障',
    'defocus': '镜头散焦',
    'warm_light_sweep': '暖光扫入',
}


def _window_effect(label, output, kind, *, incoming, duration, offset, width, height, fps, prefix):
    filters = []
    def tag(suffix):
        return f'[{prefix}_{suffix}]'
    # Incoming starts at zero; the outgoing window may be many seconds into
    # an already joined stream. No branch may trim forward to that timestamp:
    # FFmpeg would buffer the preceding primary frames waiting for its first
    # secondary frame, exhausting the bounded queue on a longer sequence.
    # Sampled transitions contain ceil(duration*fps) frames.  Reach the clean
    # incoming endpoint on the final sampled frame, rather than one frame
    # later (which would produce a visible jump back to an untransformed cut).
    steps = max(1, math.ceil(duration*fps-1e-9)-1)
    p = f'clip((t-{offset:.6f})/{steps/fps:.6f},0,1)'
    # Perspective's input counter starts at one; the timestamp-based filters
    # start at zero. Match their phases so zoom still covers rotation edges
    # during the penultimate transition frames.
    frame_p = f'clip((in-1-{offset*fps:.6f})/{steps},0,1)'
    # The last sampled frame is the exact unmodified endpoint. Exclude it
    # from colour conversion/noise too: a mathematically zero motion can
    # still round a pixel during an RGB conversion or add residual grain.
    # This also covers xfade's inclusive final transition sample.
    active = f'gte(t,{offset:.6f})*lt(t,{offset+steps/fps-1e-7:.9f})'
    peak = f'(sin(PI*({p}))*sin(PI*({p})))'
    frame_peak = f'(sin(PI*({frame_p}))*sin(PI*({frame_p})))'
    def zoom_filter(amount):
        zoom = f'(1+{amount}*{frame_peak})'
        return (f"perspective=x0='W/2-W/2/{zoom}':y0='H/2-H/2/{zoom}':"
                f"x1='W/2+W/2/{zoom}':y1='H/2-H/2/{zoom}':"
                f"x2='W/2-W/2/{zoom}':y2='H/2+H/2/{zoom}':"
                f"x3='W/2+W/2/{zoom}':y3='H/2+H/2/{zoom}':eval=frame:enable='{active}'")
    move = f'(1-{p})' if incoming else p
    blur_size = min(127, max(3, (width//28)|1))
    if kind in ('whip_blur_left', 'whip_blur_right', 'mirror_whip'):
        left, middle, right = tag('left'), tag('middle'), tag('right')
        left_flip, right_flip, wide = tag('lf'), tag('rf'), tag('wide')
        filters += [f'{label}split=3{left}{middle}{right}',
                    f'{left}hflip{left_flip}', f'{right}hflip{right_flip}',
                    f'{left_flip}{middle}{right_flip}hstack=inputs=3{wide}']
        if kind == 'mirror_whip':
            # Output stays three tiles wide. During the effect, widen the
            # source field around its centre, then crop the middle tile.
            # With perspective bypassed outside the window, that crop is the
            # exact original raster (no full-stream resize or colour change).
            scale = f'(1+2*sin(PI*({frame_p}))*sin(PI*({frame_p})))'
            filters.append(f"{wide}perspective=x0='W/2*(1-{scale})':y0=0:x1='W/2*(1+{scale})':y1=0:"
                           f"x2='W/2*(1-{scale})':y2=H:x3='W/2*(1+{scale})':y3=H:eval=frame:enable='{active}',"
                           f'crop={width}:{height}:{width}:0,setsar=1,'
                           f"avgblur=sizeX={blur_size}:sizeY=1:enable='between(t,{offset+duration*.20:.6f},{offset+duration*.80:.6f})'{output}")
        else:
            direction = 1 if kind == 'whip_blur_left' else -1
            if incoming:
                direction *= -1
            shift = f'{direction}*{width}*0.95*({move})*({move})'
            filters.append(f"{wide}crop={width}:{height}:x='{width}+({shift})':y=0,setsar=1,"
                           f"avgblur=sizeX={blur_size}:sizeY=1:enable='between(t,{offset+duration*.15:.6f},{offset+duration*.85:.6f})'{output}")
    elif kind == 'chromatic_zoom':
        # Two slightly different zoom trajectories create radial ghosting.
        # The colour-plane offsets are genuine RGB shifts, not a colour tint.
        normal, ghost, original = tag('normal'), tag('ghost'), tag('original')
        zoom_a, zoom_b = tag('za'), tag('zb')
        filters.append(f'{label}split=3{normal}{ghost}{original}')
        for source, out, amount in ((normal, zoom_a, 1.15), (ghost, zoom_b, 1.35)):
            filters.append(f'{source}{zoom_filter(amount)}{out}')
        separation = min(24, max(2, round(width*.012)))
        filters.append(f"{zoom_a}{zoom_b}blend=all_expr='A*0.65+B*0.35':enable='{active}',format=gbrp,"
                       f"rgbashift=rh={separation}:bh={-separation}:edge=smear:"
                       f"enable='between(t,{offset+duration*.15:.6f},{offset+duration*.85:.6f})',format=yuv444p{tag('effect')}")
        filters.append(f"{original}{tag('effect')}blend=all_expr='B':enable='{active}'{output}")
    elif kind in ('whip_blur_up', 'whip_blur_down'):
        top,middle,bottom=(tag(s) for s in ('top','middle','bottom'))
        filters += [f'{label}split=3{top}{middle}{bottom}',
                    f'{top}vflip{tag("tf")}',f'{bottom}vflip{tag("bf")}',
                    f'{tag("tf")}{middle}{tag("bf")}vstack=inputs=3{tag("tall")}']
        direction=(1 if kind=='whip_blur_up' else -1)*(-1 if incoming else 1)
        shift=f'{direction}*{height}*0.95*({move})*({move})'
        vertical_blur=min(127,max(3,(height//28)|1))
        filters.append(f"{tag('tall')}crop={width}:{height}:x=0:y='{height}+({shift})',setsar=1,"
                       f"avgblur=sizeX=1:sizeY={vertical_blur}:enable='between(t,{offset+duration*.15:.6f},{offset+duration*.85:.6f})'{output}")
    elif kind == 'rotate_zoom':
        angle=(-.23 if incoming else .23)
        # Rotate first, then crop the enlarged centre of that raster. Zooming
        # before rotate would still expose the rotated canvas's black corners.
        filters.append(f"{label}rotate=angle='{angle}*{peak}':ow=iw:oh=ih:"
                       f"fillcolor=black:enable='{active}',{zoom_filter(.9)}{output}")
    elif kind in ('pullback_bounce','squeeze_horizontal','squeeze_vertical'):
        # Unlike zoompan (which clamps zoom >=1), scale+pad can physically
        # shrink a scene. Intermediate dimensions vary but pad always emits
        # the unchanged canvas size and PTS; no future source stream is read.
        if kind=='pullback_bounce':
            factor=f'(1-0.28*{peak}*(1+0.32*sin(4*PI*({p}))))'
            w=f'max(2,trunc({width}*{factor}/2)*2)'
            h=f'max(2,trunc({height}*{factor}/2)*2)'
        elif kind=='squeeze_horizontal':
            w=f'max(2,trunc({width}*(1-0.68*{peak})/2)*2)';h=str(height)
        else:
            w=str(width);h=f'max(2,trunc({height}*(1-0.68*{peak})/2)*2)'
        filters.append(f"{label}scale=w='{w}':h='{h}':eval=frame:flags=lanczos,"
                       f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black:eval=frame,setsar=1{output}')
    elif kind in ('flashwhite_zoom','flashblack_zoom'):
        brightness=.72 if kind=='flashwhite_zoom' else -.72
        filters.append(f"{label}{zoom_filter(.65)},eq=brightness='{brightness}*{peak}*{peak}':"
                       f"eval=frame:enable='{active}'{output}")
    elif kind == 'glitch_horizontal':
        # Alternating horizontal bands tear in opposite directions. Different
        # chroma offsets and deterministic noise produce a real electronic
        # glitch rather than renaming a slide or colour preset.
        tp=f'clip((T-{offset:.6f})/{steps/fps:.6f},0,1)'
        intensity=f'(sin(PI*({tp}))*sin(PI*({tp})))'
        shift=f'W*0.12*{intensity}*if(lt(mod(floor(Y/(H/12))+floor(T*30),4),2),1,-1)'
        filters.append(f"{label}geq=lum='lum(X+({shift}),Y)':"
                       f"cb='cb(X+({shift})+W*0.025*{intensity},Y)':"
                       f"cr='cr(X+({shift})-W*0.025*{intensity},Y)':enable='{active}',"
                       f"noise=alls=8:allf=t+u:all_seed=42:enable='{active}'{output}")
    elif kind == 'defocus':
        original,blurred=tag('original'),tag('blurred')
        filters += [f'{label}split=3{original}{tag("blur_in")}{tag("mask_in")}',
                    f"{tag('blur_in')}gblur=sigma={min(18,max(2,width/45)):.3f}:steps=2:enable='{active}'{blurred}"]
        # The curve is uniform across a frame. Compute it on a tiny mask;
        # native maskedmerge avoids millions of repeated sin evaluations.
        # Derive its clock/EOF from the source; an independent nullsrc is
        # infinite and would extend or desynchronize this three-input filter.
        mask_peak=peak.replace('t-', 'T-')
        curve=f'255*{mask_peak}'
        filters.append(f"{tag('mask_in')}scale=2:2:flags=neighbor,geq=lum='{curve}':cb='{curve}':cr='{curve}',"
                       f'scale={width}:{height}:flags=neighbor{tag("mask")}')
        filters.append(f"{original}{blurred}{tag('mask')}maskedmerge=planes=7:enable='{active}'{output}")
    elif kind == 'warm_light_sweep':
        # A narrow warm exposure band physically crosses the image. Light
        # intensity follows a smooth envelope; outside the window geq is
        # bypassed, preserving source pixels exactly.
        tp=f'clip((T-{offset:.6f})/{steps/fps:.6f},0,1)'
        band=f'(exp(-pow((X/W-(-0.25+1.5*({tp})))/0.14,2))*sin(PI*({tp}))*sin(PI*({tp})))'
        # This band varies only along X. A small continuous light map keeps
        # the same envelope and color offsets without full-size exp calls.
        filters.append(f"{label}split=2{tag('base')}{tag('light_in')}")
        filters.append(f"{tag('light_in')}scale=128:2:flags=neighbor,geq=lum='100*{band}':cb='23*{band}':cr='12*{band}',"
                       f'scale={width}:{height}:flags=bilinear{tag("light")}')
        filters.append(f"{tag('base')}{tag('light')}blend=c0_mode=addition:c1_mode=subtract:c2_mode=addition:enable='{active}'{output}")
    else:
        raise ValueError('组合转场缺少实际渲染实现：'+str(kind))
    return filters


def compound_transition_filters(left_label, right_label, output_label, kind, *,
                                duration, offset, width, height, fps, prefix):
    """Return graph entries with the same duration semantics as FFmpeg xfade.

    ``prefix`` must be unique per boundary.  ``offset`` is the absolute start
    of the transition in the left input; right input starts at local zero.
    Both inputs must already be normalized to the canvas/frame rate and held
    at their boundary by the caller.  Output is yuv444p like stock xfade.
    """
    if kind not in COMPOUND_TRANSITIONS:
        raise ValueError('不支持的组合转场：' + str(kind))
    if not all(isinstance(value, str) and re.fullmatch(r'\[[A-Za-z0-9_:]+\]', value)
               for value in (left_label, right_label, output_label)):
        raise ValueError('组合转场的画面标签格式无效')
    if not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', prefix):
        raise ValueError('组合转场的内部标识格式无效')
    if not all(type(value) is int and value >= 2 for value in (width, height)):
        raise ValueError('组合转场需要有效的画面宽度和高度')
    if type(fps) is not int or fps <= 0:
        raise ValueError('组合转场需要有效的帧率')
    if (not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0
            or not isinstance(offset, (int, float)) or not math.isfinite(offset) or offset < 0):
        raise ValueError('组合转场的时长或开始时间无效')
    def tag(suffix):
        return f'[{prefix}_{suffix}]'
    left_window, right_window = tag('lw'), tag('rw')
    left_out, right_out = tag('lo'), tag('ro')
    filters = [f'{left_label}format=yuv444p{left_window}', f'{right_label}format=yuv444p{right_window}']
    for incoming, label, output, suffix in ((False, left_window, left_out, 'a'), (True, right_window, right_out, 'b')):
        filters += _window_effect(label, output, kind, incoming=incoming, duration=duration,
                                  offset=0 if incoming else offset,
                                  width=width, height=height, fps=fps, prefix=prefix+'_'+suffix)
    # One stock xfade owns duration and PTS. Every spatial branch consumes the
    # same local time, so a later boundary never makes an earlier one buffer
    # an entire multi-second prefix waiting for a future trimmed overlay.
    filters += [f'{left_out}{right_out}xfade=transition=fade:duration={duration:.6f}:offset={offset:.6f},settb=AVTB{output_label}']
    return filters
