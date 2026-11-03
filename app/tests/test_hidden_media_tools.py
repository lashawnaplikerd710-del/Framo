"""Verify quiet subprocess launch still returns media evidence and diagnostics."""
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent_core import direct_resources, independent_export


class HiddenMediaToolsTests(unittest.TestCase):
    def test_sapi_is_hidden_and_keeps_json_text_and_error_diagnostics(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);output=root/'speech.wav'
            def synth(command, **options):
                self.assertIn('-WindowStyle',command)
                self.assertEqual(command[command.index('-WindowStyle')+1],'Hidden')
                self.assertEqual(options['creationflags'],getattr(subprocess,'CREATE_NO_WINDOW',0))
                self.assertEqual(options['stdin'],subprocess.DEVNULL)
                self.assertTrue(options['capture_output'])
                request=Path(options['env']['DIRECT_SAPI_REQUEST'])
                self.assertEqual(json.loads(request.read_text('utf-8-sig'))['text'],'测试口播')
                output.write_bytes(b'audio')
                return SimpleNamespace(returncode=0,stdout='',stderr='')
            with patch.object(direct_resources,'_powershell_executable',return_value='powershell.exe'), \
                 patch.object(direct_resources.subprocess,'run',side_effect=synth):
                self.assertEqual(direct_resources.synthesize_windows_sapi('测试口播',output,work_dir=root),output)
            self.assertFalse(list(root.glob('sapi-request-*.json')))
            with patch.object(direct_resources,'_powershell_executable',return_value='powershell.exe'), \
                 patch.object(direct_resources.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='voice unavailable')):
                with self.assertRaisesRegex(RuntimeError,'voice unavailable'):
                    direct_resources.synthesize_windows_sapi('测试口播',output,work_dir=root)
            self.assertFalse(list(root.glob('sapi-request-*.json')))

    def test_hidden_ffprobe_still_reads_duration_streams_and_frame_rate(self):
        result={'format':{'duration':'2.000'},'streams':[
            {'codec_type':'video','width':720,'height':1280,'avg_frame_rate':'30/1'},
            {'codec_type':'audio'}]}
        with patch.object(independent_export.subprocess,'run',return_value=SimpleNamespace(
                returncode=0,stdout=json.dumps(result))) as run:
            media=independent_export._probe_with_ffprobe(Path('video.mp4'),'ffprobe.exe')
        self.assertEqual(media['duration_s'],2.0)
        self.assertEqual(media['fps'],30.0)
        self.assertTrue(media['has_audio'] and media['has_video'])
        self.assertEqual(run.call_args.kwargs['creationflags'],getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.assertTrue(run.call_args.kwargs['capture_output'])

    def test_hidden_ffmpeg_fallback_preserves_banner_evidence(self):
        banner='Duration: 00:00:02.00\nStream #0:0: Video: h264, yuv420p, 720x1280, 30 fps\nStream #0:1: Audio: aac'
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw)/'video.mp4';path.write_bytes(b'fixture')
            with patch.object(independent_export.subprocess,'run',return_value=SimpleNamespace(
                    returncode=1,stdout='',stderr=banner)) as run:
                media=independent_export.probe_media(path,ffmpeg='ffmpeg.exe')
            self.assertEqual(media['probe_backend'],'ffmpeg-banner')
            self.assertEqual((media['width'],media['height']),(720,1280))
            self.assertTrue(media['has_audio'] and media['has_video'])
            self.assertEqual(run.call_args.kwargs['creationflags'],getattr(subprocess,'CREATE_NO_WINDOW',0))


if __name__=='__main__': unittest.main()
