"""Local, explicitly masked LaMa repair; never modifies the original video."""
from __future__ import annotations

from fractions import Fraction
import hashlib
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

MODEL_SHA256 = '1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6'
ROOT = Path(__file__).resolve().parents[1]
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _candidate_boxes(image):
    """Find horizontal, high-contrast text bands without an OCR service.

    This deliberately returns conservative candidates.  It is a location detector,
    not a claim that every connected component is Chinese text.
    """
    import cv2
    import numpy as np
    if hasattr(image, 'convert'):
        image = np.asarray(image.convert('RGB'))[:, :, ::-1]
    else:
        image = np.asarray(image)
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    height, width = image.shape[:2]
    if width < 32 or height < 32:
        return []
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    masks = [cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 31, 7)]
    # Dark outlines and bright glyphs cover the common outlined-subtitle styles.
    masks.extend((cv2.inRange(gray, 0, 105), cv2.inRange(gray, 180, 255)))
    boxes = []
    for mask in masks:
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                np.ones((3, 3), dtype=np.uint8))
        mask = cv2.dilate(mask, np.ones((3, 3), dtype=np.uint8))
        count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask)
        for x, y, w, h, area in stats[1:]:
            # A subtitle line is wider than it is tall and occupies a modest
            # fraction of the frame.  This rejects furniture edges and borders.
            if (w < max(36, width * .18) or h < max(10, height * .018)
                    or h > height * .12 or w / max(1, h) < 3.2
                    or area < max(80, width * height * .0012)
                    or area > width * height * .24):
                continue
            boxes.append((int(x), int(y), int(x + w), int(y + h)))
    # De-duplicate threshold variants and keep the most stable/widest bands.
    merged = []
    for box in sorted(boxes, key=lambda b: (b[1], -(b[2] - b[0]))):
        x0, y0, x1, y1 = box
        found = None
        for index, other in enumerate(merged):
            ox0, oy0, ox1, oy1 = other
            overlap = max(0, min(x1, ox1) - max(x0, ox0))
            close_y = abs((y0 + y1) / 2 - (oy0 + oy1) / 2) <= max(8, .7 * max(y1-y0, oy1-oy0))
            if close_y and overlap / max(1, min(x1-x0, ox1-ox0)) > .35:
                found = index
                merged[index] = (min(x0, ox0), min(y0, oy0), max(x1, ox1), max(y1, oy1))
                break
        if found is None:
            merged.append(box)
    return sorted(merged, key=lambda b: (-(b[2]-b[0])*(b[3]-b[1]), b[1]))[:8]


def detect_subtitle_regions(frames, *, max_regions=4):
    """Detect subtitle bands from sampled PIL frames and return pixel boxes.

    A box must recur in at least two samples when several frames are available;
    this avoids treating a single bright object as a subtitle.  One frame is
    accepted for short clips and is still shown for user confirmation.
    """
    samples = list(frames)
    if not samples:
        return []
    all_boxes = [_candidate_boxes(frame) for frame in samples]
    height, width = samples[0].height, samples[0].width
    clusters = []
    for boxes in all_boxes:
        for box in boxes:
            x0, y0, x1, y1 = box
            best = None
            best_distance = 10**9
            for index, item in enumerate(clusters):
                bx0, by0, bx1, by1, hits = item
                distance = abs((x0+x1)/2-(bx0+bx1)/2)/width + abs((y0+y1)/2-(by0+by1)/2)/height
                if distance < .12 and distance < best_distance:
                    best, best_distance = index, distance
            if best is None:
                clusters.append([x0, y0, x1, y1, 1])
            else:
                item = clusters[best]
                item[:4] = [min(item[0],x0), min(item[1],y0), max(item[2],x1), max(item[3],y1)]
                item[4] += 1
    minimum_hits = 1 if len(samples) == 1 else max(2, int(len(samples) * .35))
    result = []
    for x0, y0, x1, y1, hits in clusters:
        if hits < minimum_hits:
            continue
        pad_x, pad_y = max(4, round((x1-x0)*.04)), max(3, round((y1-y0)*.18))
        result.append((max(0,x0-pad_x), max(0,y0-pad_y), min(width,x1+pad_x), min(height,y1+pad_y)))
    return sorted(result, key=lambda r: (-(r[2]-r[0])*(r[3]-r[1]), r[1]))[:max_regions]


def make_subtitle_tracker(initial_region, width, height):
    """Return a lightweight frame tracker for a detected subtitle band."""
    previous = tuple(initial_region)

    def select(frame, _fallback=None):
        nonlocal previous
        candidates = _candidate_boxes(frame)
        if candidates:
            px = (previous[0] + previous[2]) / 2
            py = (previous[1] + previous[3]) / 2
            pw, ph = previous[2] - previous[0], previous[3] - previous[1]
            ranked = sorted(candidates, key=lambda b:
                abs((b[0]+b[2])/2-px)/width + abs((b[1]+b[3])/2-py)/height
                + .15 * abs((b[2]-b[0])/max(1,pw)-1)
                + .15 * abs((b[3]-b[1])/max(1,ph)-1))
            candidate = ranked[0]
            distance = abs((candidate[0]+candidate[2])/2-px)/width + abs((candidate[1]+candidate[3])/2-py)/height
            # Do not jump to a distant background edge when the caption
            # detector is uncertain; keep the last verified caption band.
            if distance < .10:
                pad_x, pad_y = max(4, round((candidate[2]-candidate[0])*.04)), max(3, round((candidate[3]-candidate[1])*.12))
                target = (max(0,candidate[0]-pad_x), max(0,candidate[1]-pad_y),
                          min(width,candidate[2]+pad_x), min(height,candidate[3]+pad_y))
                # Exponential smoothing keeps a noisy detector from making the
                # mask visibly jump while still following a moving caption.
                alpha = .28
                previous = tuple(round(old*(1-alpha)+new*alpha)
                                 for old,new in zip(previous,target))
        return previous
    return select


def quick_repair(image, region):
    """Fast local OpenCV repair used by automatic mode.

    The mask is derived inside the detected subtitle band, so nearby product
    pixels are not replaced wholesale.  This is intentionally a fast mode;
    LaMa remains available for difficult, manually verified regions.
    """
    import cv2
    import numpy as np
    from PIL import Image
    x0, y0, x1, y1 = checked_region(region, image.width, image.height)
    pad = max(8, min(36, round((y1-y0)*.7)))
    bounds = (max(0,x0-pad), max(0,y0-pad), min(image.width,x1+pad), min(image.height,y1+pad))
    crop = np.asarray(image.crop(bounds).convert('RGB'))[:, :, ::-1]
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    local_x0, local_y0 = x0-bounds[0], y0-bounds[1]
    local_x1, local_y1 = x1-bounds[0], y1-bounds[1]
    roi = gray[local_y0:local_y1, local_x0:local_x1]
    hsv = cv2.cvtColor(crop[local_y0:local_y1, local_x0:local_x1], cv2.COLOR_BGR2HSV)
    # Keep glyph-sized connected components. Large blanket edges and other
    # scenery are intentionally rejected before the inpaint mask is expanded.
    candidates = [cv2.inRange(hsv, np.array([80, 100, 55]), np.array([145, 255, 255])),
                  cv2.inRange(hsv, np.array([0, 0, 210]), np.array([179, 85, 255]))]
    mask = np.zeros(roi.shape, np.uint8)
    roi_area = roi.shape[0] * roi.shape[1]
    for source_mask in candidates:
        count, labels, stats, _ = cv2.connectedComponentsWithStats(source_mask)
        for label in range(1, count):
            x, y, w, h, area = stats[label]
            if (area >= 18 and area <= roi_area*.08 and w >= 3 and h >= 5
                    and w <= roi.shape[1]*.25 and h <= roi.shape[0]*.75):
                mask[labels == label] = 255
    mask = cv2.dilate(mask, np.ones((7,7), np.uint8), iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5,5), np.uint8))
    full = np.zeros(gray.shape, np.uint8)
    full[local_y0:local_y1, local_x0:local_x1] = mask
    repaired = cv2.inpaint(crop, full, 3, cv2.INPAINT_TELEA)
    result = image.convert('RGB').copy()
    patch = Image.fromarray(cv2.cvtColor(repaired, cv2.COLOR_BGR2RGB)).crop(
        (local_x0, local_y0, local_x1, local_y1)).resize((x1-x0,y1-y0), Image.Resampling.LANCZOS)
    result.paste(patch, (x0, y0))
    return result


class RemovalCancelled(Exception):
    pass


class FastPainter:
    """Small adapter so the existing export worker can use automatic mode."""
    def load(self):
        return None

    def repair(self, image, region):
        return quick_repair(image, region)

    def close(self):
        return None


def stabilize_repair(previous_frame, previous_repaired, frame, repaired, region):
    """Reduce frame-to-frame LaMa texture shimmer without smearing cuts."""
    if previous_frame is None or previous_repaired is None:
        return repaired
    import cv2
    import numpy as np
    from PIL import Image
    previous = np.asarray(previous_frame.convert('RGB'))
    current = np.asarray(frame.convert('RGB'))
    repaired_array = np.asarray(repaired.convert('RGB'))
    previous_fixed = np.asarray(previous_repaired.convert('RGB'))
    prev_gray = cv2.cvtColor(previous, cv2.COLOR_RGB2GRAY)
    current_gray = cv2.cvtColor(current, cv2.COLOR_RGB2GRAY)
    # A low-resolution flow is enough for subtitle-sized texture and is much
    # cheaper than another neural inference pass.
    flow = cv2.calcOpticalFlowFarneback(prev_gray, current_gray, None,
                                        .45, 2, 16, 2, 5, 1.1, 0)
    height, width = current_gray.shape
    grid_x, grid_y = np.meshgrid(np.arange(width, dtype=np.float32),
                                 np.arange(height, dtype=np.float32))
    map_x = grid_x - flow[..., 0]
    map_y = grid_y - flow[..., 1]
    warped = cv2.remap(previous_fixed, map_x, map_y, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_REFLECT)
    # Reset temporal history at cuts or when the flow is clearly unreliable.
    cut_score = float(np.mean(cv2.absdiff(prev_gray, current_gray)))
    if cut_score > 42:
        return repaired
    x0, y0, x1, y1 = region
    mask = np.zeros((height, width), dtype=np.float32)
    mask[y0:y1, x0:x1] = 1.0
    mask = cv2.GaussianBlur(mask, (0, 0), 2.0)[..., None]
    alpha = .28 * mask
    blended = repaired_array.astype(np.float32) * (1-alpha) + warped.astype(np.float32) * alpha
    result = repaired_array.copy()
    result[y0:y1, x0:x1] = np.clip(blended[y0:y1, x0:x1], 0, 255).astype(np.uint8)
    # The inpaint result is authoritative outside the current mask.
    return Image.fromarray(result)


def checked_region(region, width, height):
    if (not isinstance(region, (list, tuple)) or len(region) != 4
            or any(type(x) is not int for x in region)):
        raise ValueError('请在原片预览上框选字幕区域')
    x0, y0, x1, y1 = region
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise ValueError('字幕区域必须位于画面内部')
    if x1-x0 < 4 or y1-y0 < 4:
        raise ValueError('字幕区域太小，请重新框选')
    if (x1-x0)*(y1-y0) > width*height*.4:
        raise ValueError('字幕区域超过画面的 40%，请缩小到实际文字及描边范围')
    return tuple(region)


def inspect_video(path, tools):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('请选择本地视频文件')
    if not tools.get('ffmpeg') or not tools.get('ffprobe'):
        raise ValueError('去字幕需要软件内置的 FFmpeg 和 FFprobe，请恢复工具组件')
    result = subprocess.run([tools['ffprobe'], '-v', 'error', '-show_streams',
        '-show_format', '-of', 'json', str(path)], capture_output=True,
        timeout=25, creationflags=HIDDEN)
    if result.returncode:
        raise ValueError('无法读取视频，请确认素材可以正常播放')
    data = json.loads(result.stdout)
    video = next((s for s in data['streams'] if s.get('codec_type') == 'video'
                  and not s.get('disposition', {}).get('attached_pic')), None)
    if not video:
        raise ValueError('文件中没有视频画面')
    if video.get('color_transfer') in ('smpte2084', 'arib-std-b67'):
        raise ValueError('当前去字幕支持 SDR 视频；请先将 HDR 素材转换为 SDR')
    width, height = int(video['width']), int(video['height'])
    rotation = next((float(s['rotation']) for s in video.get('side_data_list', [])
                     if 'rotation' in s), float(video.get('tags', {}).get('rotate', 0)))
    if round(rotation) % 180 in (90,):
        width, height = height, width
    rate = Fraction(0)
    for value in (video.get('avg_frame_rate'),video.get('r_frame_rate')):
        try:
            candidate=Fraction(value or '0')
            if candidate > 0:
                rate=candidate;break
        except (ValueError,ZeroDivisionError):continue
    duration = float(video.get('duration') or data.get('format', {}).get('duration') or 0)
    if not (0 < float(rate) <= 120 and 0 < duration and width >= 16 and height >= 16):
        raise ValueError('视频时长、尺寸或帧率无法用于去字幕')
    try:sar=Fraction(video.get('sample_aspect_ratio','1:1').replace(':','/'))
    except (ValueError,ZeroDivisionError):sar=Fraction(1)
    if sar <= 0:sar=Fraction(1)
    if round(rotation) % 180 == 90:sar=1/sar
    return {'path':str(path), 'width':width, 'height':height, 'fps':str(rate),
            'duration_s':duration, 'has_audio':any(s.get('codec_type') == 'audio' for s in data['streams']),
            'sar':str(sar)}


def read_frame(info, seconds, tools):
    from PIL import Image
    seconds = max(0, min(float(seconds), max(0, info['duration_s']-.1)))
    result = subprocess.run([tools['ffmpeg'], '-v', 'error', '-nostdin', '-ss', str(seconds),
        '-i', info['path'], '-map', '0:v:0', '-frames:v', '1', '-f', 'image2pipe',
        '-vcodec', 'png', '-'], capture_output=True, timeout=35, creationflags=HIDDEN)
    if result.returncode or not result.stdout:
        raise ValueError('该时间点无法读取画面，请换一个时间点')
    return Image.open(io.BytesIO(result.stdout)).convert('RGB')


class LamaInpainter:
    def __init__(self, model_path=None):
        self.model_path = Path(model_path or ROOT/'models/subtitle-removal/lama_fp32.onnx')
        self.session = None

    def load(self):
        if self.session is not None:
            return
        if not self.model_path.is_file():
            raise ValueError('本地去字幕模型缺失，请恢复 models/subtitle-removal 模型组件')
        digest = hashlib.sha256()
        with self.model_path.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024), b''):
                digest.update(block)
        if digest.hexdigest() != MODEL_SHA256:
            raise ValueError('去字幕模型校验失败，请恢复原始模型文件')
        try:
            import onnxruntime as ort
        except ImportError:
            raise ValueError('缺少本地去字幕运行组件 onnxruntime，请修复软件组件') from None
        options = ort.SessionOptions()
        options.intra_op_num_threads = 2
        options.inter_op_num_threads = 1
        self.session = ort.InferenceSession(str(self.model_path), options,
                                           providers=['CPUExecutionProvider'])

    def repair(self, image, region):
        """Only paste predictions inside the user's mask; outside stays exact."""
        import numpy as np
        from PIL import Image, ImageFilter
        width, height = image.size
        x0,y0,x1,y1 = checked_region(region, width, height)
        self.load()
        # Include nearby context while keeping small lettering legible at 512.
        pad = max(48, (y1-y0)*2)
        bounds = (max(0,x0-pad), max(0,y0-pad), min(width,x1+pad), min(height,y1+pad))
        crop = image.crop(bounds).convert('RGB')
        cw,ch = crop.size
        scale = min(512/cw,512/ch)
        sw,sh = max(1,round(cw*scale)),max(1,round(ch*scale))
        resized = np.asarray(crop.resize((sw,sh),Image.Resampling.LANCZOS))
        pixels = np.pad(resized, ((0,512-sh),(0,512-sw),(0,0)),mode='reflect')
        mask = np.zeros((512,512),dtype=np.float32)
        mx0,my0 = math.floor((x0-bounds[0])*scale),math.floor((y0-bounds[1])*scale)
        mx1,my1 = math.ceil((x1-bounds[0])*scale),math.ceil((y1-bounds[1])*scale)
        mask[my0:my1,mx0:mx1] = 1
        image_input = np.ascontiguousarray(pixels.transpose(2,0,1)[None],dtype=np.float32)/255
        mask_input = mask[None,None]
        output = self.session.run(None, {'image':image_input,'mask':mask_input})[0]
        output = np.clip(output[0].transpose(1,2,0),0,255).astype(np.uint8)
        predicted = Image.fromarray(output[:sh,:sw]).resize((cw,ch),Image.Resampling.LANCZOS)
        # Feather inward only; never alter pixels beyond the selected box.
        local = predicted.crop((x0-bounds[0],y0-bounds[1],x1-bounds[0],y1-bounds[1]))
        alpha = Image.new('L',(x1-x0,y1-y0),255)
        from PIL import ImageDraw
        ImageDraw.Draw(alpha).rectangle((0,0,alpha.width-1,alpha.height-1),outline=0,width=1)
        alpha = alpha.filter(ImageFilter.GaussianBlur(.6))
        result = image.convert('RGB').copy()
        result.paste(local,(x0,y0),alpha)
        return result


class ProcessInpainter:
    """Keep ORT session construction (which holds the GIL) outside the UI process."""
    def __init__(self):
        self.process=None
        self.stopped=threading.Event()
        self.log=None

    def _request(self,command,raw=b''):
        if self.stopped.is_set():raise RemovalCancelled('已取消去字幕，原片保留')
        if self.process is None:
            self.log=tempfile.TemporaryFile()
            try:
                self.process=subprocess.Popen([str(ROOT/'python/python.exe'),'-B','-X','utf8',
                    '-m','agent_core.subtitle_removal','--worker'],cwd=str(ROOT),
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,creationflags=HIDDEN)
            except OSError:
                self.log.close()
                raise ValueError('本地去字幕进程无法启动，请检查软件内置 Python 组件') from None
            if self.stopped.is_set():
                self.process.terminate()
                raise RemovalCancelled('已取消去字幕，原片保留')
        process=self.process
        try:
            process.stdin.write(json.dumps(command).encode('utf-8')+b'\n')
            if raw:process.stdin.write(raw)
            process.stdin.flush()
            line=process.stdout.readline()
            if not line:raise ValueError('本地去字幕进程已退出，请关闭去字幕窗口后重新打开')
            response=json.loads(line)
            if not response.get('ok'):
                raise ValueError(response.get('error') or '本地去字幕处理失败')
            return response,process.stdout
        except (OSError,json.JSONDecodeError):
            raise ValueError('本地去字幕进程通信失败，请重新打开去字幕工具') from None

    def load(self):
        self._request({'action':'load'})

    def repair(self,image,region):
        from PIL import Image
        image=image.convert('RGB')
        response,stream=self._request({'action':'repair','width':image.width,'height':image.height,
                                      'region':list(region)},image.tobytes())
        size=image.width*image.height*3
        if response.get('bytes') != size:raise ValueError('本地去字幕结果尺寸不匹配')
        raw=_read_exact(stream,size)
        if len(raw)!=size:raise ValueError('本地去字幕结果不完整')
        return Image.frombytes('RGB',image.size,bytes(raw))

    def close(self):
        self.stopped.set()
        process=self.process
        if process is not None:
            try:process.terminate()
            except OSError:pass
            def reap():
                try:process.wait(timeout=5)
                except subprocess.TimeoutExpired:process.kill();process.wait()
                for stream in (process.stdin,process.stdout,self.log):
                    if stream:
                        try:stream.close()
                        except (OSError,ValueError):pass
            threading.Thread(target=reap,daemon=True).start()


def _worker_main():
    from PIL import Image
    painter=LamaInpainter()
    reader,writer=sys.stdin.buffer,sys.stdout.buffer
    while line:=reader.readline():
        raw=b''
        try:
            command=json.loads(line)
            if command.get('action')=='load':
                painter.load()
            elif command.get('action')=='repair':
                width,height=command['width'],command['height']
                if not (type(width) is int and type(height) is int and 16<=width<=16384 and 16<=height<=16384):
                    raise ValueError('去字幕画面尺寸不受支持')
                pixels=_read_exact(reader,width*height*3)
                image=Image.frombytes('RGB',(width,height),bytes(pixels))
                raw=painter.repair(image,command['region']).tobytes()
            else:raise ValueError('无法识别去字幕操作')
            result={'ok':True,'bytes':len(raw)}
        except Exception as exc:
            result={'ok':False,'error':str(exc) if isinstance(exc,ValueError) else
                    '本地 AI 修补失败，请重新打开去字幕工具并检查模型组件'}
        writer.write(json.dumps(result,ensure_ascii=False).encode('utf-8')+b'\n')
        if result['ok'] and raw:writer.write(raw)
        writer.flush()


def _read_exact(pipe, count):
    data = bytearray()
    while len(data) < count:
        block = pipe.read(count-len(data))
        if not block:
            break
        data.extend(block)
    if data and len(data) != count:
        raise ValueError('视频解码帧不完整，请检查原素材')
    return data


def export_repaired(info, region, destination, tools, painter, *, cancel=None, progress=None,
                    region_selector=None, temporal_stabilize=True):
    """Bounded-memory decode/inpaint/encode; audio retained, no subtitle streams."""
    from PIL import Image
    cancel = cancel or threading.Event()
    progress = progress or (lambda value:None)
    source = Path(info['path']).resolve(strict=True)
    source_stat=source.stat()
    dest = Path(destination).expanduser().resolve()
    if dest == source or dest.exists():
        raise ValueError('请使用新的 MP4 文件名，不能覆盖原片或已有文件')
    if dest.suffix.lower() != '.mp4':
        raise ValueError('去字幕成品请保存为 .mp4 文件')
    if region_selector is None:
        checked_region(region, info['width'], info['height'])
    dest.parent.mkdir(parents=True,exist_ok=True)
    temp = dest.parent/('.framo-removal-'+uuid.uuid4().hex+'.mp4')
    processes=[]
    def stop_processes():
        for process in list(processes):
            if process.poll() is None:
                try:process.terminate()
                except OSError:pass
    done = threading.Event()
    def watch_cancel():
        while not done.wait(.1):
            if cancel.is_set():
                stop_processes()
                return
    watcher=threading.Thread(target=watch_cancel,daemon=True)
    watcher.start()
    started=time.monotonic(); frames=0
    rate=info['fps'];width,height=info['width'],info['height']
    progress({'phase':'加载本地 AI 模型','percent':0})
    try:
        painter.load()
        if cancel.is_set():raise RemovalCancelled('已取消去字幕，原片保留')
        with tempfile.TemporaryFile() as decode_log, tempfile.TemporaryFile() as encode_log:
            decoder = subprocess.Popen([tools['ffmpeg'],'-v','error','-nostdin','-i',str(source),
                '-map','0:v:0','-an','-sn','-vf','fps='+rate,'-f','rawvideo','-pix_fmt','rgb24','-'],
                stdout=subprocess.PIPE,stderr=decode_log,stdin=subprocess.DEVNULL,creationflags=HIDDEN)
            processes.append(decoder)
            encoder = subprocess.Popen([tools['ffmpeg'],'-v','error','-nostdin','-n',
                '-f','rawvideo','-pixel_format','rgb24','-video_size',f'{width}x{height}',
                '-framerate',rate,'-i','-', '-i',str(source),'-map','0:v:0','-map','1:a:0?',
                '-sn','-dn','-vf','setsar='+info.get('sar','1'),
                '-c:v','libx264','-preset','fast','-crf','18','-pix_fmt',
                'yuv420p' if width%2==0 and height%2==0 else 'yuv444p',
                '-c:a','aac','-b:a','192k','-t',str(info['duration_s']),
                '-map_metadata','-1','-movflags','+faststart',str(temp)],
                stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=encode_log,creationflags=HIDDEN)
            processes.append(encoder)
            last_update=0
            previous_frame = None
            previous_repaired = None
            while not cancel.is_set():
                raw=_read_exact(decoder.stdout,width*height*3)
                if not raw:break
                frame=Image.frombytes('RGB',(width,height),bytes(raw))
                current_region = region_selector(frame, region) if region_selector else region
                current_region = checked_region(current_region, width, height)
                repaired=painter.repair(frame,current_region)
                if temporal_stabilize:
                    repaired=stabilize_repair(previous_frame,previous_repaired,
                                              frame,repaired,current_region)
                if cancel.is_set():break
                encoder.stdin.write(repaired.tobytes())
                previous_frame,previous_repaired = frame,repaired
                frames+=1
                now=time.monotonic()
                if now-last_update>.25:
                    progress({'phase':'正在去字幕','percent':min(98,frames/float(Fraction(rate))/info['duration_s']*100),
                              'frames':frames,'elapsed_s':now-started,
                              'remaining_s':max(0,(info['duration_s']*float(Fraction(rate))-frames)*(now-started)/frames)})
                    last_update=now
            if cancel.is_set():raise RemovalCancelled('已取消去字幕，原片保留')
            encoder.stdin.close()
            decoder.stdout.close()
            if decoder.wait(timeout=30) or encoder.wait(timeout=90) or not frames:
                raise ValueError('去字幕视频解码或编码失败，请检查素材及可用磁盘空间')
        progress({'phase':'检查导出画面与声音','percent':99})
        output=inspect_video(temp,tools)
        if ((output['width'],output['height']) != (width,height)
                or output['has_audio'] != info['has_audio']
                or abs(output['duration_s']-info['duration_s']) > max(.15,2/float(Fraction(rate)))):
            raise ValueError('导出时长、画幅或声音检查失败，未发布成品')
        if cancel.is_set():raise RemovalCancelled('已取消去字幕，原片保留')
        after_stat=source.stat()
        if (source_stat.st_size,source_stat.st_mtime_ns) != (after_stat.st_size,after_stat.st_mtime_ns):
            raise ValueError('处理期间原素材发生变化，请重新导入后再去字幕')
        # On Windows rename refuses an existing file, including a late collision.
        if os.name == 'nt':temp.rename(dest)
        else:
            os.link(temp,dest)
            temp.unlink()
        report={'status':'COMPLETED','output_file':str(dest),'source':str(source),
                'region':list(region) if isinstance(region,(list,tuple)) else None,
                'method':'opencv_telea_fast' if isinstance(painter,FastPainter) else 'local_lama_fp32_cpu_temporal',
                'model_sha256':MODEL_SHA256 if not isinstance(painter,FastPainter) else None,
                'frames':frames,'elapsed_s':time.monotonic()-started,'media':output,
                'quality_note':'字幕区域由 AI 修补，复杂纹理、人物及快速运动可能有残影或闪烁，请预览确认。'}
        report['media']['path']=str(dest)
        # The sidecar is optional metadata; a successful MP4 stays successful
        # if permissions prevent creating the report beside it.
        try:
            with Path(str(dest)+'.removal.json').open('x',encoding='utf-8') as stream:
                json.dump(report,stream,ensure_ascii=False,indent=2)
        except OSError:pass
        progress({'phase':'已导出','percent':100})
        return report
    except Exception as exc:
        if cancel.is_set():raise RemovalCancelled('已取消去字幕，原片保留') from None
        if isinstance(exc,subprocess.TimeoutExpired):
            raise ValueError('去字幕编码或检查超时，未发布成品，请重试或先处理较短素材') from None
        if isinstance(exc,OSError):
            raise ValueError('去字幕读写失败，请检查磁盘空间、文件占用和输出目录权限') from None
        raise
    finally:
        done.set()
        stop_processes()
        for process in processes:
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill();process.wait(timeout=5)
            for stream in (process.stdin,process.stdout):
                if stream:
                    try:stream.close()
                    except (OSError,ValueError):pass
        temp.unlink(missing_ok=True)


if __name__=='__main__' and '--worker' in sys.argv:
    _worker_main()
