"""Bounded PCM playback on Windows. No player windows, files or global volume changes."""
from __future__ import annotations

import ctypes as C
import threading
import time


class WaveFormat(C.Structure):
    _pack_ = 1
    _fields_ = [('tag', C.c_ushort), ('channels', C.c_ushort), ('rate', C.c_uint32),
                ('byte_rate', C.c_uint32), ('align', C.c_ushort), ('bits', C.c_ushort), ('extra', C.c_ushort)]


class WaveHeader(C.Structure):
    _fields_ = [('data', C.c_void_p), ('length', C.c_uint32), ('recorded', C.c_uint32),
                ('user', C.c_size_t), ('flags', C.c_uint32), ('loops', C.c_uint32),
                ('next', C.c_void_p), ('reserved', C.c_size_t)]


class WaveTime(C.Structure):
    _fields_ = [('type', C.c_uint32), ('value', C.c_uint32), ('padding', C.c_uint32)]


class WaveOutput:
    """Own and retain up to four 50 ms buffers until the driver returns them."""
    rate = 48000
    frame_bytes = 4  # PCM16 stereo
    chunk_bytes = 9600

    def __init__(self):
        self.lock = threading.RLock()
        self.handle = C.c_void_p()
        self.buffers = []
        self.closed = False
        self.reset_requested = False
        self.played = 0.0
        self.submitted_bytes = 0
        self.api = C.WinDLL('winmm')
        prototypes = {
            'waveOutOpen': [C.POINTER(C.c_void_p), C.c_uint, C.POINTER(WaveFormat), C.c_size_t, C.c_size_t, C.c_uint32],
            'waveOutPrepareHeader': [C.c_void_p, C.POINTER(WaveHeader), C.c_uint],
            'waveOutUnprepareHeader': [C.c_void_p, C.POINTER(WaveHeader), C.c_uint],
            'waveOutWrite': [C.c_void_p, C.POINTER(WaveHeader), C.c_uint],
            'waveOutGetPosition': [C.c_void_p, C.POINTER(WaveTime), C.c_uint],
            **{name: [C.c_void_p] for name in ('waveOutPause','waveOutRestart','waveOutReset','waveOutClose')},
        }
        for name, args in prototypes.items():
            fn = getattr(self.api,name);fn.argtypes=args;fn.restype=C.c_uint
        fmt = WaveFormat(1, 2, self.rate, self.rate*self.frame_bytes, self.frame_bytes, 16, 0)
        self._check(self.api.waveOutOpen(C.byref(self.handle), 0xFFFFFFFF, C.byref(fmt), 0, 0, 0))
        try:
            self._check(self.api.waveOutPause(self.handle))
        except Exception:
            self.api.waveOutClose(self.handle)
            raise

    @staticmethod
    def _check(code):
        if code:
            raise OSError(f'Windows 音频设备不可用（{code}），请检查默认输出设备')

    def _collect(self):
        for header, data in self.buffers[:]:
            if header.flags & 1:  # WHDR_DONE
                self._check(self.api.waveOutUnprepareHeader(self.handle,C.byref(header),C.sizeof(header)))
                self.buffers.remove((header,data))

    def write(self, pcm, stopped):
        if len(pcm) % self.frame_bytes:
            raise OSError('音频采样未完整解码')
        while not stopped.is_set():
            with self.lock:
                if self.closed or self.reset_requested:return False
                self._collect()
                if len(self.buffers) < 4:
                    data=C.create_string_buffer(pcm)
                    header=WaveHeader(data=C.cast(data,C.c_void_p),length=len(pcm))
                    self._check(self.api.waveOutPrepareHeader(self.handle,C.byref(header),C.sizeof(header)))
                    self.buffers.append((header,data))
                    self._check(self.api.waveOutWrite(self.handle,C.byref(header),C.sizeof(header)))
                    self.submitted_bytes += len(pcm)
                    return True
            stopped.wait(.005)
        return False

    def start(self):
        with self.lock:
            if not self.closed and not self.reset_requested:
                self._check(self.api.waveOutRestart(self.handle))

    def position(self):
        with self.lock:
            if self.closed or self.reset_requested:return self.played
            pos=WaveTime(type=2)  # TIME_SAMPLES, with documented driver fallback
            self._check(self.api.waveOutGetPosition(self.handle,C.byref(pos),C.sizeof(pos)))
            if pos.type == 2:seconds=pos.value/self.rate
            elif pos.type == 4:seconds=pos.value/(self.rate*self.frame_bytes)
            elif pos.type == 1:seconds=pos.value/1000
            else:raise OSError('音频设备不支持播放位置查询')
            self.played=max(self.played,seconds)
            return self.played

    def pending(self):
        with self.lock:
            if self.closed or self.reset_requested:return False
            self._collect()
            return bool(self.buffers)

    def reset(self):
        with self.lock:
            if not self.closed and not self.reset_requested:
                self.api.waveOutReset(self.handle)
                self.reset_requested=True

    def close(self):
        with self.lock:
            if self.closed:return
            self.reset()
            # waveOutReset synchronously returns pending buffers to the owner.
            for header,data in self.buffers:
                self.api.waveOutUnprepareHeader(self.handle,C.byref(header),C.sizeof(header))
            self.api.waveOutClose(self.handle)
            self.buffers.clear()
            self.closed=True


class AudioPlayback:
    """A PCM decoder and device clock belonging to exactly one preview session."""
    def __init__(self, ffmpeg, path, position, stopped):
        self.ffmpeg,self.path,self.position=ffmpeg,path,position
        self.stopped=stopped
        self.ready=threading.Event();self.finished=threading.Event()
        self.process=None;self.sink=None;self.error=None
        self.final_position=0.0;self.finished_at=None
        self.thread=threading.Thread(target=self._run,name='desktop-preview-audio',daemon=True)

    def stop(self):
        if self.sink is not None:self.sink.reset()
        if self.process is not None and self.process.poll() is None:
            try:self.process.terminate()
            except OSError:pass

    def clock(self):
        if self.error:raise OSError(self.error)
        if self.finished_at is not None:
            return self.final_position+time.monotonic()-self.finished_at
        return self.sink.position() if self.sink is not None else 0.0

    def _run(self):
        import subprocess
        from .output_preview import close_decoder, read_exact
        try:
            self.sink=WaveOutput()
            if self.stopped.is_set():return
            args=[self.ffmpeg,'-hide_banner','-loglevel','error','-nostdin','-threads','2',
                  '-ss',f'{max(0.0,float(self.position)):.3f}','-i',str(self.path),
                  '-map','0:a:0','-vn','-sn','-dn','-af','aresample=48000:async=1:first_pts=0',
                  '-ac','2','-ar','48000','-f','s16le','pipe:1']
            self.process=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0,
                                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            if self.stopped.is_set():return
            while not self.stopped.is_set():
                pcm=read_exact(self.process.stdout,WaveOutput.chunk_bytes,self.stopped)
                if not pcm:break
                if not self.sink.write(pcm,self.stopped):return
                self.ready.set()
            if self.stopped.is_set():return
            if self.process.wait(timeout=3):raise OSError('音轨解码失败')
            self.ready.set()  # also supports seeking past a shorter audio stream
            last_position,last_change=0.0,time.monotonic()
            while self.sink.pending() and not self.stopped.is_set():
                pos=self.sink.position()
                if pos>last_position:last_position,last_change=pos,time.monotonic()
                if time.monotonic()-last_change>5:raise OSError('音频设备播放停滞')
                self.stopped.wait(.01)
            self.final_position=self.sink.position()
            self.finished_at=time.monotonic()
        except (OSError,ValueError,AttributeError,subprocess.SubprocessError) as exc:
            self.error=str(exc)
        finally:
            self.ready.set()
            close_decoder(self.process)
            if self.sink is not None:self.sink.close()
            self.finished.set()
