"""Read-only, bounded-memory A/V preview driven by the Windows audio device clock."""
from __future__ import annotations

import queue
import subprocess
import threading
import time
import json


def read_exact(pipe, count, stopped):
    raw=bytearray()
    while len(raw)<count and not stopped.is_set():
        chunk=pipe.read(min(65536,count-len(raw)))
        if not chunk:break
        raw.extend(chunk)
    return bytes(raw)


def close_decoder(process):
    if process is None:return
    try:
        if process.poll() is None:process.terminate()
        try:process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill();process.wait(timeout=2)
    except (OSError,subprocess.SubprocessError):pass
    finally:
        if process.stdout:process.stdout.close()


class OutputPreviewStream:
    """Own video/audio decoders and bounded buffers, isolated from later sessions."""

    size = (360, 360)
    fps = 30

    def __init__(self, path, position, install_dir, *, size=None):
        self.path, self.position, self.install_dir = path, position, install_dir
        if size is not None:
            if (not isinstance(size, tuple) or len(size) != 2 or
                    any(type(value) is not int or not 2 <= value <= 960 or value % 2 for value in size)):
                raise ValueError('Preview dimensions must be even integers in 2..960')
            self.size = size
        self.frames = queue.Queue(maxsize=2)
        self.stopped = threading.Event()
        self.process = None
        self.audio = None
        self.audio_status = '准备声音'
        self.error = None
        self.thread = threading.Thread(target=self._run, name='desktop-preview-stream', daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        # Never wait for a decoder on the Tk thread. The worker reaps its own
        # process; checking this event after Popen also covers an early stop.
        self.stopped.set()
        if self.audio is not None:self.audio.stop()
        process = self.process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def _send(self, kind, value=None):
        if self.stopped.is_set():
            return
        if kind == 'frame':
            # Drop stale frames rather than slowing playback to drain a backlog.
            while True:
                try:
                    self.frames.get_nowait()
                except queue.Empty:
                    break
        try:
            self.frames.put_nowait((kind, value))
        except queue.Full:
            pass

    def _run(self):
        from PIL import Image
        from .independent_export import discover_tools
        process = None
        try:
            tools=discover_tools(self.install_dir)
            ffmpeg = tools.get('ffmpeg')
            if self.stopped.is_set():
                return
            if not ffmpeg or not tools.get('ffprobe'):
                raise OSError('没有可用的 FFmpeg / FFprobe')
            probe=subprocess.run([tools['ffprobe'],'-v','error','-select_streams','a:0',
                                  '-show_entries','stream=index','-of','json',str(self.path)],
                                 capture_output=True,timeout=8,stdin=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if probe.returncode:raise OSError('无法读取素材音轨信息')
            has_audio=bool(json.loads(probe.stdout).get('streams'))
            if self.stopped.is_set():return
            if has_audio:
                from .preview_audio import AudioPlayback
                self.audio=AudioPlayback(ffmpeg,self.path,self.position,self.stopped)
                self.audio.thread.start()
            self.audio_status='声音开启' if has_audio else '无音轨'
            width, height = self.size
            args = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin',
                    '-threads', '2', '-ss', f'{max(0.0, float(self.position)):.3f}',
                    '-i', str(self.path), '-map', '0:v:0', '-an', '-sn', '-dn',
                    '-filter_threads', '1', '-vf',
                    f'fps={self.fps},scale={width}:{height}:force_original_aspect_ratio=decrease,'
                    f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,format=rgb24',
                    '-threads', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1']
            process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       bufsize=0,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.process = process
            if self.stopped.is_set():
                return
            frame_bytes = width * height * 3
            frame_index = 0
            started=None
            last_clock,last_change=0.0,time.monotonic()
            while not self.stopped.is_set():
                # Unbuffered Windows pipes may return part of a frame. Read
                # exactly one frame, never wait for a multi-frame pipe buffer.
                raw = read_exact(process.stdout,frame_bytes,self.stopped)
                if self.stopped.is_set():
                    return
                if len(raw) != frame_bytes:
                    if raw or process.wait(timeout=3) != 0 or frame_index == 0:
                        raise OSError('视频帧读取失败')
                    if self.audio is not None:
                        while not self.audio.finished.wait(.02):
                            if self.stopped.is_set():return
                        if self.audio.error:raise OSError(self.audio.error)
                    self._send('ended')
                    return
                if started is None:
                    if self.audio is not None:
                        deadline=time.monotonic()+8
                        while not self.audio.ready.wait(.02):
                            if self.stopped.is_set():return
                            if time.monotonic()>deadline:raise OSError('音频准备超时，请重试预览')
                        if self.audio.error:raise OSError(self.audio.error)
                        if self.stopped.is_set():return
                        self.audio.sink.start()
                    started=time.monotonic();last_change=started
                target=frame_index/self.fps
                while not self.stopped.is_set():
                    clock=self.audio.clock() if self.audio is not None else time.monotonic()-started
                    if clock>last_clock:last_clock,last_change=clock,time.monotonic()
                    if target<=clock+.005:break
                    if time.monotonic()-last_change>5:raise OSError('音频设备播放停滞，请检查输出设备')
                    self.stopped.wait(min(.01,max(.001,target-clock)))
                if self.stopped.is_set():return
                if target<clock-.1:
                    frame_index+=1
                    continue  # catch up to device clock rather than slowing sound
                frame = Image.frombytes('RGB', self.size, bytes(raw))
                self._send('frame', (self.position + frame_index / self.fps, frame))
                frame_index += 1
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            self.error=str(exc)
            self.audio_status='播放失败'
            self._send('error')
        finally:
            if self.audio is not None:
                self.stopped.set()
                self.audio.stop()
                self.audio.thread.join(timeout=5)
            close_decoder(process)
