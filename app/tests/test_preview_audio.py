"""Real short decoders/device clocks, isolated generated fixtures, no production jobs."""
from pathlib import Path
import queue
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from agent_core.output_preview import OutputPreviewStream
from agent_core.preview_audio import WaveOutput


class PreviewAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools=Path(__import__('agent_core.output_preview',fromlist=['x']).__file__).resolve().parent.parent/'toolchain'
        if not (cls.tools/'ffmpeg.exe').is_file():raise unittest.SkipTest('Bundled FFmpeg unavailable')
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        cls.av=cls.root/'av.mp4';cls.silent=cls.root/'silent.mp4';cls.short=cls.root/'short-audio.mp4'
        cls.delayed=cls.root/'delayed.mp4'
        for path,audio_duration in ((cls.av,1.2),(cls.silent,0),(cls.short,.3)):
            args=[str(cls.tools/'ffmpeg.exe'),'-v','error','-f','lavfi','-i','testsrc2=size=96x128:rate=30:duration=1.2']
            if audio_duration:
                args+=['-f','lavfi','-i',f'sine=frequency=660:sample_rate=48000:duration={audio_duration}','-c:a','aac']
            args+=['-c:v','libx264','-preset','ultrafast','-y',str(path)]
            subprocess.run(args,check=True,capture_output=True,creationflags=0x08000000,timeout=20)
        subprocess.run([str(cls.tools/'ffmpeg.exe'),'-v','error','-f','lavfi','-i',
                        'testsrc2=size=96x128:rate=30:duration=1.2','-itsoffset','0.4','-f','lavfi','-i',
                        'sine=frequency=660:sample_rate=48000:duration=0.7','-c:v','libx264','-preset','ultrafast',
                        '-c:a','aac','-y',str(cls.delayed)],check=True,capture_output=True,creationflags=0x08000000,timeout=20)

    @classmethod
    def tearDownClass(cls):cls.tmp.cleanup()

    def consume(self,stream,stop_at=None):
        points=[];drift=[];terminal=None;deadline=time.monotonic()+10
        stream.start()
        try:
            while time.monotonic()<deadline:
                try:kind,value=stream.frames.get(timeout=.1)
                except queue.Empty:continue
                if kind=='frame':
                    points.append(value[0])
                    if stream.audio is not None:
                        drift.append(abs(value[0]-stream.position-stream.audio.clock()))
                    if stop_at is not None and value[0]>=stop_at:break
                else:terminal=kind;break
        finally:
            if stop_at is not None or terminal is None:stream.stop()
            stream.thread.join(5)
        self.assertFalse(stream.thread.is_alive())
        if stream.process is not None:self.assertIsNotNone(stream.process.poll())
        if stream.audio is not None:
            self.assertFalse(stream.audio.thread.is_alive())
            if stream.audio.sink is not None:self.assertTrue(stream.audio.sink.closed)
            if stream.audio.process is not None:self.assertIsNotNone(stream.audio.process.poll())
        return terminal,points,drift

    def test_audio_device_clock_drives_video_and_hidden_decoders_are_reaped(self):
        real=subprocess.Popen;flags=[]
        def popen(*a,**kw):flags.append(kw.get('creationflags',0));return real(*a,**kw)
        stream=OutputPreviewStream(self.av,0,self.tools,size=(96,128))
        with patch('subprocess.Popen',side_effect=popen):terminal,points,drift=self.consume(stream)
        self.assertEqual(terminal,'ended',stream.error)
        self.assertGreater(len(points),20)
        self.assertLess(max(drift),.15)
        self.assertGreater(stream.audio.final_position,1.1)
        self.assertGreater(stream.audio.sink.submitted_bytes,200000)
        self.assertTrue(all(f & 0x08000000 for f in flags))

    def test_silent_video_uses_video_clock_without_opening_audio_device(self):
        stream=OutputPreviewStream(self.silent,0,self.tools,size=(96,128))
        with patch('agent_core.preview_audio.WaveOutput',side_effect=AssertionError('must not open')):
            terminal,points,_=self.consume(stream)
        self.assertEqual(terminal,'ended',stream.error)
        self.assertIsNone(stream.audio);self.assertEqual(stream.audio_status,'无音轨')
        self.assertGreater(points[-1],1)

    def test_short_audio_does_not_stall_longer_video(self):
        stream=OutputPreviewStream(self.short,0,self.tools,size=(96,128))
        terminal,points,_=self.consume(stream)
        self.assertEqual(terminal,'ended',stream.error)
        self.assertGreater(points[-1],1)

    def test_seek_and_stop_reset_device_and_new_session_uses_new_origin(self):
        first=OutputPreviewStream(self.av,0,self.tools,size=(96,128))
        self.consume(first,stop_at=.2)
        self.assertTrue(first.audio.sink.reset_requested)
        second=OutputPreviewStream(self.av,.6,self.tools,size=(96,128))
        terminal,points,drift=self.consume(second)
        self.assertEqual(terminal,'ended',second.error)
        self.assertAlmostEqual(points[0],.6,places=2)
        self.assertLess(max(drift),.15)

    def test_device_error_is_reported_instead_of_silent_success(self):
        stream=OutputPreviewStream(self.av,0,self.tools,size=(96,128))
        with patch('agent_core.preview_audio.WaveOutput',side_effect=OSError('测试设备不可用')):
            terminal,_,_=self.consume(stream)
        self.assertEqual(terminal,'error')
        self.assertIn('测试设备不可用',stream.error)

    def test_early_stop_does_not_leave_audio_device_open(self):
        stream=OutputPreviewStream(self.av,0,self.tools,size=(96,128))
        stream.start();stream.stop();stream.thread.join(5)
        self.assertFalse(stream.thread.is_alive())
        self.assertTrue(stream.frames.empty())

    def test_delayed_audio_retains_leading_silence(self):
        import array
        chunks=[]
        class Capture(WaveOutput):
            def write(self,pcm,stopped):
                chunks.append(pcm)
                return super().write(pcm,stopped)
        stream=OutputPreviewStream(self.delayed,0,self.tools,size=(96,128))
        with patch('agent_core.preview_audio.WaveOutput',Capture):terminal,_,_=self.consume(stream)
        self.assertEqual(terminal,'ended',stream.error)
        samples=array.array('h',b''.join(chunks))
        first=next(i for i,x in enumerate(samples) if abs(x)>100)/96000
        self.assertAlmostEqual(first,.4,delta=.035)
