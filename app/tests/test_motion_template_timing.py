"""Template planning must use the same exact frame clock as the real importer."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_core.motion_templates import TemplateLibrary, TemplateRunner


class TemplateTimingTests(unittest.TestCase):
    def test_container_audio_tail_does_not_extend_video_plan(self):
        info={'format':{'duration':'6.154667','start_time':'0'},'streams':[
            {'codec_type':'video','duration':'6.133333','start_time':'0'},
            {'codec_type':'audio','duration':'6.154667'}]}
        with tempfile.TemporaryDirectory() as temp:
            runner=TemplateRunner(TemplateLibrary(temp))
            with patch('agent_core.motion_templates.shutil.which',return_value='ffprobe'), \
                    patch('agent_core.motion_templates.subprocess.run',return_value=
                          subprocess.CompletedProcess([],0,json.dumps(info).encode())):
                self.assertEqual(runner.probe('unchanged.mp4'),368/60)

    def test_video_origin_and_half_frame_rounding_follow_importer(self):
        # Container starts at 4 s; video starts 1/120 s later and lasts 1/30 s.
        # End is 2.5 project frames; importer rounds upward, never bankers' round.
        info={'format':{'duration':'1','start_time':'4'},'streams':[
            {'codec_type':'video','start_time':str(4+1/120),'duration':str(1/30)},
            {'codec_type':'audio','duration':'1'}]}
        with tempfile.TemporaryDirectory() as temp:
            runner=TemplateRunner(TemplateLibrary(temp))
            with patch('agent_core.motion_templates.shutil.which',return_value='ffprobe'), \
                    patch('agent_core.motion_templates.subprocess.run',return_value=
                          subprocess.CompletedProcess([],0,json.dumps(info).encode())):
                self.assertEqual(runner.probe('offset.mp4'),3/60)

    def test_invalid_stream_durations_are_chinese_errors(self):
        with tempfile.TemporaryDirectory() as temp:
            runner=TemplateRunner(TemplateLibrary(temp))
            for duration in ('NaN','inf','-1','0','601','0.001','not-a-duration'):
                info={'format':{},'streams':[{'codec_type':'video','duration':duration},
                                            {'codec_type':'audio'}]}
                with self.subTest(duration=duration), \
                        patch('agent_core.motion_templates.shutil.which',return_value='ffprobe'), \
                        patch('agent_core.motion_templates.subprocess.run',return_value=
                              subprocess.CompletedProcess([],0,json.dumps(info).encode())):
                    with self.assertRaisesRegex(ValueError,'模板口播需在'):
                        runner.probe('invalid.mp4')

    def test_actual_mp4_with_21ms_longer_audio_matches_video_frames_without_input_changes(self):
        root=Path(__file__).resolve().parents[1]
        tools=root/'toolchain'
        ffmpeg=str(tools/'ffmpeg.exe') if (tools/'ffmpeg.exe').is_file() else shutil.which('ffmpeg')
        ffprobe=str(tools/'ffprobe.exe') if (tools/'ffprobe.exe').is_file() else shutil.which('ffprobe')
        if not ffmpeg or not ffprobe:
            self.skipTest('Standalone FFmpeg/FFprobe required for real MP4 frame-clock check')
        with tempfile.TemporaryDirectory() as temp:
            source=Path(temp)/'longer-audio.mp4'
            subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y',
                '-f','lavfi','-i','testsrc2=size=160x90:rate=30:duration=6.154667',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=6.154667',
                '-c:v','libx264','-preset','ultrafast','-c:a','aac','-shortest',str(source)],
                check=True,capture_output=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            before=source.read_bytes()
            result=subprocess.run([ffprobe,'-v','error','-show_streams','-show_format',
                '-of','json',str(source)],check=True,capture_output=True,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            info=json.loads(result.stdout)
            video=next(s for s in info['streams'] if s['codec_type']=='video')
            audio=next(s for s in info['streams'] if s['codec_type']=='audio')
            self.assertAlmostEqual(float(audio['duration'])-float(video['duration']),.021334,places=5)
            runner=TemplateRunner(TemplateLibrary(Path(temp)/'state',root))
            duration=runner.probe(source)
            self.assertEqual(round(duration*60),368)
            self.assertNotEqual(round(float(info['format']['duration'])*60),368)
            self.assertEqual(source.read_bytes(),before)


if __name__=='__main__':
    unittest.main()
