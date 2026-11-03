"""Regression coverage for bugs reproduced in the installed render package."""
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from agent_core.direct_renderer import render_snapshot
from agent_core.independent_export import build_ffmpeg_command, _materialize_subtitle_images


def still_snapshot(source):
    return {'duration': 4_000_000, 'fps': 15,
            'canvas_config': {'width': 320, 'height': 568},
            'materials': {'videos': [{'id': 'base', 'path': str(source), 'has_audio': False}]},
            'tracks': [{'type': 'video', 'segments': [{'material_id': 'base',
                'target_timerange': {'start': 0, 'duration': 4_000_000},
                'source_timerange': {'start': 0, 'duration': 4_000_000}}]}]}


class MediaRenderRegressionTests(unittest.TestCase):
    def test_music_loop_trim_uses_target_length_but_one_shot_keeps_source_length(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root/'base.png'; Image.new('RGB', (320, 568), '#224466').save(source)
            music = root/'music.wav'; music.touch()
            snapshot = still_snapshot(source)
            snapshot['materials']['audio'] = [{'id': 'music', 'path': str(music)}]
            segment = {'material_id': 'music', 'loop': True,
                       'target_timerange': {'start': 0, 'duration': 4_000_000},
                       'source_timerange': {'start': 0, 'duration': 1_000_000}}
            snapshot['tracks'].append({'type': 'music', 'segments': [segment]})
            command = build_ffmpeg_command(snapshot, root/'out.mp4', 'ffmpeg')
            graph = command[command.index('-filter_complex')+1]
            self.assertIn('[1:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration=4.000000', graph)
            self.assertIn('-stream_loop', command)
            segment['loop'] = False
            command = build_ffmpeg_command(snapshot, root/'out.mp4', 'ffmpeg')
            self.assertIn('[1:a]aresample=48000,aformat=channel_layouts=stereo,atrim=duration=1.000000',
                          command[command.index('-filter_complex')+1])

    def test_explicit_srt_font_path_is_not_replaced_by_automatic_font_selection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            srt = root/'captions.srt'
            srt.write_text('1\n00:00:00,000 --> 00:00:04,000\nCaption\n', encoding='utf-8')
            from agent_core.font_library import bundled_font_library
            selected = bundled_font_library().select('Caption', 'en')
            snapshot = {'duration': 4_000_000, 'fps': 15, 'subtitle_path': str(srt),
                        'canvas_config': {'width': 320, 'height': 568}}
            with patch('agent_core.auto_decorations.select_caption_font') as automatic:
                rows = _materialize_subtitle_images(snapshot, root/'subtitles', font_path=selected['path'])
            automatic.assert_not_called()
            self.assertEqual([row['text'] for row in rows], ['Caption'])

    @unittest.skipUnless(os.environ.get('FRAMO_FFMPEG'), '需要FRAMO_FFMPEG进行真实媒体验证')
    def test_actual_loop_music_is_audible_after_original_source_ends(self):
        ffmpeg = Path(os.environ['FRAMO_FFMPEG'])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root/'base.png'; Image.new('RGB', (320, 568), '#224466').save(source)
            music = root/'short.wav'
            subprocess.run([str(ffmpeg), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                'sine=frequency=880:sample_rate=48000:duration=1', str(music)],
                check=True, capture_output=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            snapshot = still_snapshot(source)
            snapshot['materials']['audio'] = [{'id': 'music', 'path': str(music)}]
            snapshot['tracks'].append({'type': 'music', 'segments': [{'material_id': 'music', 'loop': True,
                'target_timerange': {'start': 0, 'duration': 4_000_000},
                'source_timerange': {'start': 0, 'duration': 1_000_000}, 'volume': .6}]})
            output = root/'loop.mp4'
            report = render_snapshot(snapshot, output, install_dir=ffmpeg.parent, work_dir=root/'work')
            self.assertEqual(report['status'], 'COMPLETED')
            decoded = subprocess.run([str(ffmpeg), '-v', 'error', '-ss', '2.5', '-i', str(output),
                '-t', '0.3', '-vn', '-ar', '8000', '-ac', '1', '-f', 'f32le', '-'],
                check=True, capture_output=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
            samples = struct.unpack('<'+'f'*(len(decoded)//4), decoded)
            rms = math.sqrt(sum(value*value for value in samples)/len(samples))
            self.assertGreater(rms, .04, '循环音乐在源音乐长度后变为静音')

    @unittest.skipUnless(os.environ.get('FRAMO_FFMPEG'), '需要FRAMO_FFMPEG进行真实媒体验证')
    def test_actual_chinese_srt_renders_without_text_material_or_external_font(self):
        ffmpeg = Path(os.environ['FRAMO_FFMPEG'])
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root/'base.png'; Image.new('RGB', (320, 568), '#224466').save(source)
            srt = root/'captions.srt'
            srt.write_text('1\n00:00:00,000 --> 00:00:04,000\n中文字幕\n', encoding='utf-8')
            snapshot = {**still_snapshot(source), 'subtitle_path': str(srt)}
            output = root/'srt.mp4'
            report = render_snapshot(snapshot, output, install_dir=ffmpeg.parent, work_dir=root/'work')
            self.assertEqual(report['status'], 'COMPLETED')
            self.assertEqual([row['text'] for row in report['subtitle_styles']], ['中文字幕'])
            self.assertEqual(report['subtitle_fonts'][0]['coverage_policy'], 'complete_text_verified')
            raw = subprocess.run([str(ffmpeg), '-v', 'error', '-ss', '1', '-i', str(output),
                '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                check=True, capture_output=True, timeout=15,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
            self.assertGreater(sum(min(raw[i:i+3]) > 170 for i in range(0, len(raw), 3)), 40)


if __name__ == '__main__':
    unittest.main()
