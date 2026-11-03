"""Source-shaped MP4 canvases; contain mixed inputs without stretching."""


def source_size(asset):
    width, height = int(asset['width']), int(asset['height'])
    if min(width, height) <= 0:
        raise ValueError('素材画幅信息无效，请重新添加素材')
    rotation = float(asset.get('rotation') or 0) % 360
    if abs(rotation - 90) < .01 or abs(rotation - 270) < .01:
        width, height = height, width
    return width, height


def canvas_for(target, first):
    ratio = target.get('aspect_ratio', 'source')
    if ratio == '9:16':
        width, height = 1080, 1920
    elif ratio == '16:9':
        width, height = 1920, 1080
    elif ratio == 'source':
        width, height = source_size(first)
        # H.264/yuv420p needs even dimensions. Uniform doubling preserves an
        # odd-sized source's exact aspect ratio instead of rounding one side.
        if width % 2 or height % 2:
            width, height = width * 2, height * 2
    else:
        raise ValueError('不支持的输出画幅，请选择原素材比例、横屏或竖屏')
    return {'width': width, 'height': height}
