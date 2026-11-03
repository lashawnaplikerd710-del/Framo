import json
import hashlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

from agent_core.caption_motion import (ENTRIES, LOOPS, graphemes, glyph_layer,
    motion_from_instruction, normalize_motion, render_caption_motion)


def _font():
    for path in ('C:/Windows/Fonts/arial.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'):
        if Path(path).is_file():
            return ImageFont.truetype(path, 30)
    return ImageFont.load_default()


def _fixture():
    font = _font()
    image = Image.new('RGBA', (320, 180))
    background = Image.new('RGBA', image.size)
    ImageDraw.Draw(background).rounded_rectangle((35, 55, 285, 115), radius=10, fill=(255, 230, 80, 240))
    image.alpha_composite(background)
    draw = ImageDraw.Draw(image)
    origin = (54, 64)
    text = 'Cute CAT'
    draw.text(origin, text, font=font, fill='white', stroke_width=2, stroke_fill='black')
    glyphs = []
    prefix = ''
    for cluster in graphemes(text):
        layer, box = glyph_layer(cluster, font, fill='white', stroke_width=2, stroke_fill='black')
        glyphs.append({'image': layer, 'x': origin[0]+draw.textlength(prefix, font=font)+box[0], 'y': origin[1]+box[1]})
        prefix += cluster
    return image, background, glyphs


class CaptionMotionTests(unittest.TestCase):
    def test_exact_fifteen_per_category_preserve_existing_names(self):
        self.assertEqual(len(ENTRIES)-1,15)
        self.assertEqual(len(LOOPS)-1,15)
        original_entries={'fade':'淡入','pop':'整体弹入','bounce':'逐字弹跳','stagger':'逐字上浮',
            'assemble':'错位聚合','trail':'文字残影','typewriter':'打字机','slideup':'向上滑入'}
        original_loops={'pulse':'呼吸缩放','float':'轻柔漂浮','bounce':'逐字波浪','sway':'轻轻摇摆','shimmer':'逐字闪亮'}
        for key,label in original_entries.items():self.assertEqual(ENTRIES[key],label)
        for key,label in original_loops.items():self.assertEqual(LOOPS[key],label)

    def test_every_chinese_name_and_english_key_positive_and_negative(self):
        for field,catalog,scope in (('entry',ENTRIES,'入场'),('loop',LOOPS,'循环')):
            for key,label in catalog.items():
                if key=='none':continue
                for name in (key,label):
                    with self.subTest(field=field,name=name):
                        self.assertEqual(normalize_motion({field:name})[field],key)
                        self.assertEqual(motion_from_instruction(f'字幕{scope}使用{name}'),{field:key})
                        self.assertEqual(motion_from_instruction(f'字幕不要{scope}{name}'),{field:'none'})
        self.assertEqual(motion_from_instruction('字幕从左滑入，然后循环逐字缩放'),{'entry':'slideleft','loop':'glyph_pulse'})
        self.assertEqual(motion_from_instruction('字幕散点汇聚，然后循环残影摆动'),{'entry':'scatter','loop':'echo'})
        self.assertEqual(motion_from_instruction('字幕循环逐字摇摆'),{'loop':'glyph_sway'})
        self.assertEqual(motion_from_instruction('字幕循环柔光呼吸'),{'loop':'glow'})
        self.assertEqual(motion_from_instruction('字幕循环残影摆动，不要文字残影'),{'loop':'echo','entry':'none'})
        self.assertEqual(motion_from_instruction('字幕入场从左滑入且循环逐字缩放'),{'entry':'slideleft','loop':'glyph_pulse'})
        self.assertEqual(motion_from_instruction('字幕不要逐字弹跳而是改成翻转展开'),{'entry':'flip'})
        self.assertEqual(motion_from_instruction('字幕整体弹入，不要字幕循环动画'),{'entry':'pop','loop':'none'})
        self.assertEqual(motion_from_instruction('贴纸残影摆动，转场从左滑入'),{})

    def test_scoped_instruction_names_and_negation(self):
        self.assertEqual(motion_from_instruction('贴纸弹入，镜头摇摆'), {})
        self.assertEqual(motion_from_instruction('字幕逐字弹跳，循环轻柔漂浮'), {'entry': 'bounce', 'loop': 'float'})
        self.assertEqual(motion_from_instruction('字幕错位聚合，文字残影'), {'entry': 'trail'})
        self.assertEqual(motion_from_instruction('不要字幕动画'), {'entry': 'none', 'loop': 'none'})
        self.assertEqual(motion_from_instruction('字幕不要残影'), {'entry': 'none'})
        self.assertEqual(normalize_motion('逐字弹跳')['entry'], 'bounce')

    def test_invalid_settings_are_chinese_and_no_guessing(self):
        for value in ({'entry': 'unknown'}, {'loop': 'unknown'}, {'strength': float('nan')},
                      {'strength': True}, {'entry_duration_ms': 10000}, {'loop_duration_ms': 100}, []):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '字幕|支持'):
                normalize_motion(value)

    def test_excluding_another_animation_keeps_positive_choice_and_static_overrides_default(self):
        self.assertEqual(motion_from_instruction('字幕逐字弹跳，不要文字残影'), {'entry':'bounce'})
        self.assertEqual(motion_from_instruction('字幕循环呼吸缩放，不要漂浮'), {'loop':'pulse'})
        self.assertEqual(motion_from_instruction('字幕保持静止'), {'entry':'none','loop':'none'})

    def test_cluster_split_keeps_combining_and_emoji_together(self):
        self.assertEqual(graphemes('猫A\u0301👩\u200d💻🇯🇵'), ['猫', 'A\u0301', '👩\u200d💻', '🇯🇵'])

    def test_static_has_exact_pixels_and_unchanged_absolute_timing(self):
        image, background, glyphs = _fixture()
        with tempfile.TemporaryDirectory() as tmp:
            rows = render_caption_motion(image, glyphs, tmp, name='static', start_us=1_250_000,
                end_us=4_330_000, motion=None, background_image=background)
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual((row['kind'], row['loop'], row['start_us'], row['end_us']), ('image', False, 1_250_000, 4_330_000))
            recreated = Image.new('RGBA', image.size)
            recreated.alpha_composite(Image.open(row['path']).convert('RGBA'), (row['x'], row['y']))
            self.assertIsNone(ImageChops.difference(image, recreated).getbbox())

    def test_nan_geometry_and_missing_glyphs_fail_before_encoder(self):
        image, background, glyphs = _fixture()
        with tempfile.TemporaryDirectory() as tmp:
            for bad in (float('nan'), float('inf'), True):
                with self.assertRaisesRegex(ValueError, '位置'):
                    render_caption_motion(image, [{**glyphs[0], 'x': bad}], tmp, name='bad',
                        start_us=0, end_us=1_000_000, motion='bounce', ffmpeg_path='not-a-binary')
            with self.assertRaisesRegex(ValueError, '缺少已排版字形'):
                render_caption_motion(image, [], tmp, name='bad', start_us=0, end_us=1_000_000,
                    motion='bounce', ffmpeg_path='not-a-binary')

    def test_layout_continuation_keeps_original_cue_loop_phase(self):
        from agent_core.caption_motion import _loop_frame
        image, background, glyphs = _fixture()
        captured = {}
        def capture(path, frames, **kwargs):
            # Only the first frame is necessary to verify the timeline anchor;
            # the separate real-render test covers the actual video encoder.
            captured[Path(path).name] = next(iter(frames))
        with tempfile.TemporaryDirectory() as tmp, patch('agent_core.caption_motion._write_alpha_video', capture):
            first = render_caption_motion(image, glyphs, tmp, name='first', start_us=700_000,
                end_us=2_000_000, motion={'entry':'trail','loop':'float'},
                ffmpeg_path='mock', background_image=background, fps=30)
            continuation = render_caption_motion(image, glyphs, tmp, name='continuation', start_us=2_000_000,
                end_us=4_000_000, motion={'entry':'none','loop':'float'}, phase_us=1_300_000,
                ffmpeg_path='mock', background_image=background, fps=30)
            for rows, phase_us in ((first, 0), (continuation, 1_300_000)):
                row=rows[-1]
                left, top = row['x'], row['y']
                actual=captured[Path(row['path']).name]
                static=image.crop((left,top,left+actual.width,top+actual.height))
                bg=background.crop((left,top,left+actual.width,top+actual.height))
                placed=[{**g,'x':g['x']-left,'y':g['y']-top} for g in glyphs]
                elapsed=phase_us+row['start_us']-rows[0]['start_us']
                expected=_loop_frame(static,bg,placed,'float',elapsed/row['resource_duration_us'],.65)
                self.assertEqual(actual.tobytes(),expected.tobytes())

    def test_real_alpha_video_settles_on_exact_static_and_loop_is_bounded(self):
        ffmpeg = next((str(p) for p in (Path('D:/JianyingAgentWork/Toolchain/ffmpeg.exe'),)
                       if p.is_file()), shutil.which('ffmpeg'))
        ffprobe = str(Path(ffmpeg).with_name('ffprobe.exe' if Path(ffmpeg).suffix == '.exe' else 'ffprobe')) if ffmpeg else None
        if not ffmpeg or not Path(ffprobe).is_file():
            self.skipTest('FFmpeg/FFprobe 不可用')
        image, background, glyphs = _fixture()
        with tempfile.TemporaryDirectory() as tmp:
            rows = render_caption_motion(image, glyphs, tmp, name='render', start_us=2_100_000,
                end_us=22_100_000, motion={'entry': 'trail', 'loop': 'float'},
                ffmpeg_path=ffmpeg, fps=24, background_image=background)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['end_us'], rows[1]['start_us'])
            self.assertEqual(rows[1]['end_us'], 22_100_000)
            self.assertFalse(rows[0]['loop'])
            self.assertTrue(rows[1]['loop'])
            for row in rows:
                probe = subprocess.run([ffprobe, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', row['path']],
                    capture_output=True, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                info = json.loads(probe.stdout)
                self.assertEqual(info['streams'][0]['codec_name'], 'qtrle')
                self.assertEqual(info['streams'][0]['pix_fmt'], 'argb')
                self.assertLess(int(info['streams'][0]['height']), image.height)
                self.assertLessEqual(float(info['format']['duration']), 2.4)
            entry = rows[0]
            decoded = subprocess.run([ffmpeg, '-v', 'error', '-i', entry['path'], '-f', 'rawvideo', '-pix_fmt', 'rgba', 'pipe:1'],
                capture_output=True, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
            encoded = Image.open(rows[1]['path']) if rows[1]['kind'] == 'image' else None
            width = info['streams'][0]['width']
            height = info['streams'][0]['height']
            frame_bytes = width*height*4
            self.assertGreater(len(decoded), frame_bytes*2)
            first = Image.frombytes('RGBA', (width, height), decoded[:frame_bytes])
            last = Image.frombytes('RGBA', (width, height), decoded[-frame_bytes:])
            expected = image.crop((entry['x'], entry['y'], entry['x']+width, entry['y']+height))
            self.assertEqual(last.tobytes(), expected.tobytes())
            self.assertNotEqual(first.tobytes(), last.tobytes())
            self.assertIsNone(first.getbbox())
            # Entry frames are composited with a real FFmpeg video background;
            # no opaque black rectangle may hide the surrounding source.
            preview = Path(tmp)/'overlay.mp4'
            command = [ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=24:d=1',
                '-i', entry['path'], '-filter_complex',
                f'[0:v][1:v]overlay={entry["x"]}:{entry["y"]}:eof_action=pass:repeatlast=0,format=yuv420p[v]',
                '-map', '[v]', '-an', '-c:v', 'libx264', str(preview)]
            subprocess.run(command, capture_output=True, check=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            raw = subprocess.run([ffmpeg, '-v', 'error', '-i', str(preview), '-frames:v', '1',
                                  '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'],
                capture_output=True, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout
            corner = tuple(raw[:3])
            self.assertGreater(corner[2], 230)
            self.assertLess(max(corner[:2]), 20)

    def test_all_motion_kinds_have_a_real_render_path(self):
        # Exercise pose math for every advertised preset without multiplying
        # external processes. A separate test above verifies the media encoder.
        from agent_core.caption_motion import _entry_frame, _loop_frame
        image, background, glyphs = _fixture()
        for kind in ENTRIES:
            if kind == 'none':
                continue
            with self.subTest(entry=kind):
                first = _entry_frame(image, background, glyphs, kind, 0, .65)
                middle = _entry_frame(image, background, glyphs, kind, .5, .65)
                last = _entry_frame(image, background, glyphs, kind, 1, .65)
                self.assertIsNone(first.getbbox())
                self.assertIsNotNone(middle.getbbox())
                self.assertEqual(last.tobytes(), image.tobytes())
        for kind in LOOPS:
            if kind == 'none':
                continue
            with self.subTest(loop=kind):
                at_a = _loop_frame(image, background, glyphs, kind, .1, .65)
                at_b = _loop_frame(image, background, glyphs, kind, .6, .65)
                self.assertNotEqual(at_a.tobytes(), at_b.tobytes())

    def test_all_presets_are_distinct_and_loops_have_exact_period(self):
        from agent_core.caption_motion import _entry_frame,_loop_frame
        image,background,glyphs=_fixture()
        for catalog,render in ((ENTRIES,_entry_frame),(LOOPS,_loop_frame)):
            signatures={}
            for code in catalog:
                if code=='none':continue
                frames=[render(image,background,glyphs,code,p,.65) for p in (.17,.38,.63,.89)]
                signature=hashlib.sha256(b''.join(frame.tobytes() for frame in frames)).hexdigest()
                self.assertNotIn(signature,signatures,(code,signatures.get(signature)))
                signatures[signature]=code
        for code in LOOPS:
            if code=='none':continue
            with self.subTest(loop=code):
                first=_loop_frame(image,background,glyphs,code,0,.65)
                repeated=_loop_frame(image,background,glyphs,code,1,.65)
                self.assertEqual(first.tobytes(),repeated.tobytes())
                # The cycle seam is a normal frame step, not a special jump.
                import numpy as np
                def distance(a,b):return float(np.abs(np.asarray(a,dtype=float)-np.asarray(b,dtype=float)).mean())
                previous=first;largest_step=0
                for index in range(1,48):
                    current=_loop_frame(image,background,glyphs,code,index/48,.65)
                    largest_step=max(largest_step,distance(previous,current));previous=current
                self.assertLessEqual(distance(previous,first),largest_step+.05)

    def test_every_new_preset_encodes_real_alpha_media(self):
        ffmpeg=next((str(p) for p in (Path('D:/JianyingAgentWork/Toolchain/ffmpeg.exe'),) if p.is_file()),shutil.which('ffmpeg'))
        ffprobe=str(Path(ffmpeg).with_name('ffprobe.exe' if Path(ffmpeg).suffix=='.exe' else 'ffprobe')) if ffmpeg else None
        if not ffmpeg or not Path(ffprobe).is_file():self.skipTest('FFmpeg/FFprobe 不可用')
        original_entries={'none','fade','pop','bounce','stagger','assemble','trail','typewriter','slideup'}
        original_loops={'none','pulse','float','bounce','sway','shimmer'}
        image,background,glyphs=_fixture()
        with tempfile.TemporaryDirectory() as tmp:
            for field,names in (('entry',set(ENTRIES)-original_entries),('loop',set(LOOPS)-original_loops)):
                for code in sorted(names):
                    with self.subTest(field=field,preset=code):
                        rows=render_caption_motion(image,glyphs,tmp,name=field+'-'+code,start_us=300000,end_us=3300000,
                            motion={field:code},ffmpeg_path=ffmpeg,fps=12,background_image=background)
                        videos=[row for row in rows if row['kind']=='video'];self.assertEqual(len(videos),1)
                        row=videos[0]
                        info=json.loads(subprocess.run([ffprobe,'-v','error','-show_streams','-show_format','-of','json',row['path']],
                            capture_output=True,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout)
                        stream=info['streams'][0];self.assertEqual((stream['codec_name'],stream['pix_fmt']),('qtrle','argb'))
                        self.assertLessEqual(float(info['format']['duration']),.9 if field=='entry' else 2.4)
                        raw=subprocess.run([ffmpeg,'-v','error','-i',row['path'],'-f','rawvideo','-pix_fmt','rgba','pipe:1'],
                            capture_output=True,check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)).stdout
                        size=stream['width']*stream['height']*4
                        self.assertGreater(len(raw),size*2)
                        self.assertNotEqual(raw[size:2*size],raw[3*size:4*size])
                        if field=='entry':
                            expected=image.crop((row['x'],row['y'],row['x']+stream['width'],row['y']+stream['height']))
                            self.assertEqual(raw[-size:],expected.tobytes())


if __name__ == '__main__':
    unittest.main()
