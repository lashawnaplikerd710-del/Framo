import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_core.compound_transitions import COMPOUND_TRANSITIONS, compound_transition_filters


class CompoundTransitionTests(unittest.TestCase):
    def graph(self, kind, **changes):
        options = dict(duration=0.4, offset=1.8, width=160, height=284, fps=30, prefix='boundary1')
        options.update(changes)
        return compound_transition_filters('[a]', '[b]', '[out]', kind, **options)

    def test_all_fifteen_are_real_spatial_compounds(self):
        graphs = [self.graph(kind) for kind in COMPOUND_TRANSITIONS]
        self.assertEqual(len(graphs), 15)
        self.assertEqual(len({';'.join(value) for value in graphs}), 15)
        for graph in graphs:
            joined = ';'.join(graph)
            self.assertNotIn('trim=', joined)
            self.assertNotIn('overlay=', joined)
            self.assertIn('duration=0.400000:offset=1.800000', joined)
            self.assertIn('settb=AVTB[out]', graph[-1])
        self.assertIn('hflip', ';'.join(graphs[0]))
        self.assertIn('avgblur', ';'.join(graphs[1]))
        self.assertIn('perspective', ';'.join(graphs[2]))
        self.assertIn("enable='gte(t,1.800000)*lt(t,2.166666567)'", ';'.join(graphs[2]))
        self.assertIn('rgbashift', ';'.join(graphs[3]))
        markers={'whip_blur_up':'vstack','whip_blur_down':'vstack','rotate_zoom':'rotate=',
                 'pullback_bounce':'pad=','flashwhite_zoom':'eq=brightness=',
                 'flashblack_zoom':'eq=brightness=','squeeze_horizontal':'scale=w=',
                 'squeeze_vertical':'scale=w=','glitch_horizontal':'geq=lum=',
                 'defocus':'gblur=','warm_light_sweep':'exp('}
        for kind,marker in markers.items():self.assertIn(marker,';'.join(self.graph(kind)))

    def test_rejects_unknown_and_unsafe_inputs_in_chinese(self):
        for kind, options in [('bad', {}), ('mirror_whip', {'duration': float('nan')}),
                              ('mirror_whip', {'offset': -1}), ('mirror_whip', {'width': 0}),
                              ('mirror_whip', {'fps': 0}), ('mirror_whip', {'prefix': 'x;evil'})]:
            with self.subTest(kind=kind, options=options):
                with self.assertRaisesRegex(ValueError, '[\u4e00-\u9fff]'):
                    self.graph(kind, **options)
        with self.assertRaises(ValueError):
            compound_transition_filters('[a];null', '[b]', '[out]', 'mirror_whip',
                                        duration=.4, offset=1.8, width=160, height=284, fps=30, prefix='c')

    def test_uniform_blur_curve_and_horizontal_light_use_bounded_maps(self):
        # A canvas-sized geq/blend expression used to repeat the same sin/exp
        # millions of times per frame. Enlarging the canvas must not enlarge
        # the mathematical masks; FFmpeg scales and combines them natively.
        for width, height in ((160, 284), (1080, 1920)):
            blur = ';'.join(self.graph('defocus', width=width, height=height))
            self.assertEqual(blur.count('scale=2:2:flags=neighbor,geq='), 2)
            self.assertEqual(blur.count('maskedmerge=planes=7:'), 2)
            self.assertNotIn('blend=all_expr=', blur)
            self.assertNotIn('nullsrc=', blur)
            light = ';'.join(self.graph('warm_light_sweep', width=width, height=height))
            self.assertEqual(light.count('scale=128:2:flags=neighbor,geq='), 2)
            self.assertEqual(light.count('c0_mode=addition:c1_mode=subtract:c2_mode=addition'), 2)
            self.assertNotIn('lum(X,Y)', light)
            self.assertNotIn('nullsrc=', light)

    @unittest.skipUnless(os.environ.get('FRAMO_FFMPEG'), '需要 FRAMO_FFMPEG 才运行实际渲染验证')
    def test_optimized_maps_preserve_original_envelopes_and_exact_clean_endpoints(self):
        ffmpeg = os.environ['FRAMO_FFMPEG']
        width, height, fps = 64, 112, 30
        def reference_window(label, output, kind, offset, prefix):
            p = f'clip((t-{offset:.6f})/0.266667,0,1)'
            peak = f'(sin(PI*({p}))*sin(PI*({p})))'
            active = f'gte(t,{offset:.6f})*lt(t,{offset+8/30-1e-7:.9f})'
            if kind == 'defocus':
                blend_peak = peak.replace('t-', 'T-')
                return [f'{label}split=2[{prefix}o][{prefix}i]',
                        f"[{prefix}i]gblur=sigma=2.000:steps=2:enable='{active}'[{prefix}b]",
                        f"[{prefix}o][{prefix}b]blend=all_expr='A*(1-{blend_peak})+B*{blend_peak}':enable='{active}'{output}"]
            tp = f'clip((T-{offset:.6f})/0.266667,0,1)'
            band = f'(exp(-pow((X/W-(-0.25+1.5*({tp})))/0.14,2))*sin(PI*({tp}))*sin(PI*({tp})))'
            return [f"{label}geq=lum='clip(lum(X,Y)+100*{band},0,255)':"
                    f"cb='clip(cb(X,Y)-23*{band},0,255)':cr='clip(cr(X,Y)+12*{band},0,255)':"
                    f"enable='{active}'{output}"]
        def render(kind, reference):
            graph = ['[0:v]format=yuv444p,tpad=stop_mode=clone:stop_duration=0.15,settb=AVTB[a]',
                     '[1:v]hue=h=80,format=yuv444p,tpad=start_mode=clone:start_duration=0.15,settb=AVTB[b]']
            if reference:
                graph += reference_window('[a]', '[ao]', kind, 1.35, 'a')
                graph += reference_window('[b]', '[bo]', kind, 0, 'b')
                graph += ['[ao][bo]xfade=transition=fade:duration=0.3:offset=1.35,trim=duration=3,format=yuv444p[out]']
            else:
                graph += compound_transition_filters('[a]', '[b]', '[joined]', kind,
                             duration=.3, offset=1.35, width=width, height=height, fps=fps, prefix='map')
                graph += ['[joined]trim=duration=3,format=yuv444p[out]']
            command = [ffmpeg, '-v', 'error', '-filter_complex_threads', '1']
            for _ in range(2):
                command += ['-f', 'lavfi', '-i', f'testsrc2=s={width}x{height}:r={fps}:d=1.5']
            command += ['-filter_complex', ';'.join(graph), '-map', '[out]', '-an',
                        '-pix_fmt', 'yuv444p', '-f', 'rawvideo', '-']
            run = subprocess.run(command, capture_output=True, timeout=30)
            self.assertEqual(run.returncode, 0, run.stderr.decode('utf-8', 'replace'))
            return run.stdout
        size = width*height*3
        for kind in ('defocus', 'warm_light_sweep'):
            with self.subTest(kind=kind):
                expected, actual = render(kind, True), render(kind, False)
                self.assertEqual(len(expected), 90*size)
                self.assertEqual(len(actual), len(expected))
                inside_error = []
                for index in range(90):
                    a, b = expected[index*size:(index+1)*size], actual[index*size:(index+1)*size]
                    if index/30 < 1.35 or index/30 >= 1.35+8/30-1e-7:
                        self.assertEqual(a, b, f'{kind} 修改了第 {index} 帧的原始画面或干净结束帧')
                    else:
                        inside_error.append(sum(abs(x-y) for x, y in zip(a, b))/size)
                self.assertLess(max(inside_error), 1.5,
                                f'{kind} 优化不能改变散焦/暖光的实际运动包络')

    @unittest.skipUnless(os.environ.get('FRAMO_FFMPEG'), '需要 FRAMO_FFMPEG 才运行实际渲染验证')
    def test_actual_render_mixed_and_consecutive_transitions_preserve_duration(self):
        ffmpeg = Path(os.environ['FRAMO_FFMPEG'])
        ffprobe = ffmpeg.with_name('ffprobe.exe' if ffmpeg.suffix == '.exe' else 'ffprobe')
        with tempfile.TemporaryDirectory() as tmp:
            inputs, graph = [], []
            # Four 2-second clips: compound -> compound -> normal.  This also
            # checks that RGB transition output composes with stock xfade.
            for index in range(4):
                inputs += ['-f', 'lavfi', '-i', f'testsrc2=s=160x284:r=30:d=2']
                before, after = (0 if index == 0 else .2), (0 if index == 3 else .2)
                graph.append(f'[{index}:v]hue=h={index*75},tpad=start_mode=clone:start_duration={before}:'
                             f'stop_mode=clone:stop_duration={after},fps=30,settb=AVTB[v{index}]')
            graph += compound_transition_filters('[v0]', '[v1]', '[j1]', 'mirror_whip',
                        duration=.4, offset=1.8, width=160, height=284, fps=30, prefix='c1')
            graph += compound_transition_filters('[j1]', '[v2]', '[j2]', 'chromatic_zoom',
                        duration=.4, offset=3.8, width=160, height=284, fps=30, prefix='c2')
            graph += ['[j2][v3]xfade=transition=fade:duration=0.4:offset=5.8[j3]',
                      '[j3]tpad=stop_mode=clone:stop_duration=0.033334,trim=duration=8,setpts=PTS-STARTPTS[out]']
            output = Path(tmp)/'combined.mp4'
            command = [str(ffmpeg), '-v', 'error', '-y', '-filter_complex_threads', '1', *inputs,
                       '-filter_complex', ';'.join(graph), '-map', '[out]', '-an', '-c:v', 'libx264',
                       '-threads', '1', '-pix_fmt', 'yuv420p', str(output)]
            run = subprocess.run(command, capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)
            probe = subprocess.run([str(ffprobe), '-v', 'error', '-count_frames', '-show_streams',
                                    '-of', 'json', str(output)], capture_output=True, text=True, timeout=30)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            stream = json.loads(probe.stdout)['streams'][0]
            self.assertEqual(int(stream['nb_read_frames']), 240)
            self.assertAlmostEqual(float(stream['duration']), 8, places=3)
            decode = subprocess.run([str(ffmpeg), '-v', 'error', '-i', str(output), '-f', 'null', '-'],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(decode.returncode, 0, decode.stderr)

    @unittest.skipUnless(os.environ.get('FRAMO_FFMPEG'), '需要 FRAMO_FFMPEG 才运行实际渲染验证')
    def test_fractional_boundaries_keep_stock_pts_and_untouched_frames(self):
        ffmpeg = os.environ['FRAMO_FFMPEG']
        def render(compound):
            inputs, graph = [], []
            for i in range(3):
                inputs += ['-f', 'lavfi', '-i', 'testsrc2=s=64x112:r=30:d=2']
                before, after = (0 if i == 0 else .15), (0 if i == 2 else .15)
                graph.append(f'[{i}:v]hue=h={i*70},tpad=start_mode=clone:start_duration={before}:'
                             f'stop_mode=clone:stop_duration={after},fps=30,settb=AVTB[v{i}]')
            previous = '[v0]'
            for i, kind in enumerate(('mirror_whip', 'chromatic_zoom'), 1):
                output = f'[joined{i}]'
                if compound:
                    graph.extend(compound_transition_filters(previous, f'[v{i}]', output, kind,
                                 duration=.3, offset=i*2-.15, width=64, height=112, fps=30, prefix=f't{i}'))
                else:
                    graph.append(f'{previous}[v{i}]xfade=transition=fade:duration=0.3:offset={i*2-.15}{output}')
                previous = output
            graph.append(f'{previous}format=yuv444p,trim=duration=6[out]')
            command = [ffmpeg, '-v', 'error', '-filter_complex_threads', '1', *inputs,
                       '-filter_complex', ';'.join(graph), '-map', '[out]', '-an', '-f', 'framemd5', '-']
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            return [row.split(',') for row in result.stdout.splitlines() if not row.startswith('#')]
        stock, compound = render(False), render(True)
        self.assertEqual(len(stock), len(compound))
        self.assertEqual([r[:5] for r in stock], [r[:5] for r in compound])
        changed = []
        for i, (a, b) in enumerate(zip(stock, compound)):
            second = i/30
            in_transition = 1.85 <= second <= 2.15 or 3.85 <= second <= 4.15
            if not in_transition:
                self.assertEqual(a[-1], b[-1], f'转场外的第 {i} 帧被修改')
            elif a[-1] != b[-1]:
                changed.append(i)
        self.assertGreater(len(changed), 4)

    @unittest.skipUnless(os.environ.get('FRAMO_FFMPEG'), '需要 FRAMO_FFMPEG 才运行实际渲染验证')
    def test_fifteen_seconds_all_four_transitions_keep_queue_bounded(self):
        # The old future-trim overlay architecture passed short demos but
        # queued >256 frames before the fourth transition at 11.85 seconds.
        # Keep the real 3-second shot duration; low resolution is sufficient
        # to prove this is bounded scheduling, not a larger-memory workaround.
        ffmpeg = Path(os.environ['FRAMO_FFMPEG'])
        ffprobe = ffmpeg.with_name('ffprobe.exe' if ffmpeg.suffix == '.exe' else 'ffprobe')
        inputs, graph = [], []
        for i in range(5):
            inputs += ['-f', 'lavfi', '-i', 'testsrc2=s=96x170:r=30:d=3']
            before, after = (0 if i == 0 else .15), (0 if i == 4 else .15)
            graph.append(f'[{i}:v]hue=h={i*60},tpad=start_mode=clone:start_duration={before}:'
                         f'stop_mode=clone:stop_duration={after},fps=30,settb=AVTB[v{i}]')
        previous='[v0]'
        for i, kind in enumerate(list(COMPOUND_TRANSITIONS)[:4], 1):
            joined=f'[j{i}]'
            graph.extend(compound_transition_filters(previous,f'[v{i}]',joined,kind,
                duration=.3,offset=i*3-.15,width=96,height=170,fps=30,prefix=f'q{i}'))
            previous=joined
        graph.append(f'{previous}tpad=stop_mode=clone:stop_duration=0.033334,trim=duration=15,setpts=PTS-STARTPTS[out]')
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'bounded.mp4'
            command=[str(ffmpeg),'-v','error','-y','-filter_threads','1','-filter_complex_threads','1',
                     '-filter_buffered_frames','256',*inputs,'-f','lavfi','-i','sine=sample_rate=48000:duration=15',
                     '-filter_complex',';'.join(graph),'-map','[out]','-map','5:a','-c:v','libx264',
                     '-threads','1','-pix_fmt','yuv420p','-c:a','aac','-t','15',str(target)]
            result=subprocess.run(command,capture_output=True,text=True,timeout=45)
            self.assertEqual(result.returncode,0,result.stderr)
            probe=subprocess.run([str(ffprobe),'-v','error','-count_frames','-show_streams','-of','json',str(target)],
                                 capture_output=True,text=True,timeout=30)
            self.assertEqual(probe.returncode,0,probe.stderr)
            streams=json.loads(probe.stdout)['streams']
            video=next(s for s in streams if s['codec_type']=='video')
            audio=next(s for s in streams if s['codec_type']=='audio')
            self.assertEqual(int(video['nb_read_frames']),450)
            self.assertAlmostEqual(float(video['duration']),15,places=3)
            self.assertAlmostEqual(float(audio['duration']),15,places=3)

    @unittest.skipUnless(os.environ.get('FRAMO_FFMPEG'), '需要 FRAMO_FFMPEG 才运行实际渲染验证')
    def test_each_preset_is_not_fade_and_preserves_unaffected_raster_and_pts(self):
        ffmpeg=os.environ['FRAMO_FFMPEG']
        def render(kind):
            graph=['[0:v]format=yuv444p,tpad=stop_mode=clone:stop_duration=0.2,settb=AVTB[a]',
                   '[1:v]hue=h=80,format=yuv444p,tpad=start_mode=clone:start_duration=0.2,settb=AVTB[b]']
            if kind=='fade':graph.append('[a][b]xfade=transition=fade:duration=0.4:offset=1.3[out]')
            else:graph.extend(compound_transition_filters('[a]','[b]','[out]',kind,
                     duration=.4,offset=1.3,width=64,height=112,fps=30,prefix='allkinds'))
            command=[ffmpeg,'-v','error','-filter_threads','1','-filter_complex_threads','1','-filter_buffered_frames','256']
            for _ in range(2):command+=['-f','lavfi','-i','testsrc2=s=64x112:r=30:d=1.5']
            command+=['-filter_complex',';'.join(graph),'-map','[out]','-an','-f','framemd5','-']
            result=subprocess.run(command,capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,f'{kind}: {result.stderr}')
            return [row.split(',') for row in result.stdout.splitlines() if not row.startswith('#')]
        baseline=render('fade');signatures=set()
        for kind in COMPOUND_TRANSITIONS:
            with self.subTest(kind=kind):
                rows=render(kind)
                self.assertEqual([r[:5] for r in rows],[r[:5] for r in baseline])
                affected=[]
                for i,(base,row) in enumerate(zip(baseline,rows)):
                    if i/30<1.3 or i/30>=1.7:
                        self.assertEqual(base[-1],row[-1],f'{kind} 修改转场外第{i}帧')
                    elif base[-1]!=row[-1]:affected.append(row[-1])
                self.assertGreater(len(affected),2,f'{kind} 不能只是普通叠化别名')
                signatures.add(tuple(affected))
        self.assertEqual(len(signatures),15,'每种组合转场应有不同的实际画面')

    @unittest.skipUnless(os.environ.get('FRAMO_FFMPEG'), '需要 FRAMO_FFMPEG 才运行实际渲染验证')
    def test_rotation_zoom_does_not_expose_black_canvas_corners(self):
        # A spatially uniform scene must stay uniform throughout rotation.
        # Rotating an already zoomed, fixed-size raster used to reveal black
        # triangles at its corners, despite zoomed source content.
        ffmpeg=os.environ['FRAMO_FFMPEG']
        for width,height in ((64,112),(112,64)):
            with self.subTest(width=width,height=height):
                graph=compound_transition_filters('[0:v]','[1:v]','[out]','rotate_zoom',
                    duration=.4,offset=1.3,width=width,height=height,fps=30,prefix='corners')
                command=[ffmpeg,'-v','error','-filter_complex_threads','1']
                for _ in range(2):
                    command+=['-f','lavfi','-i',f'color=c=gray:s={width}x{height}:r=30:d=1.7']
                command+=['-filter_complex',';'.join(graph),'-map','[out]','-an',
                          '-pix_fmt','gray','-f','rawvideo','-']
                result=subprocess.run(command,capture_output=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stderr.decode('utf-8','replace'))
                self.assertTrue(result.stdout)
                self.assertGreater(min(result.stdout),100,'旋转缩放不能露出黑色画布边角')

    @unittest.skipUnless(os.environ.get('FRAMO_FFMPEG'), '需要 FRAMO_FFMPEG 才运行实际渲染验证')
    def test_all_fifteen_in_fortyeight_second_sequence_keep_queue_bounded(self):
        ffmpeg=Path(os.environ['FRAMO_FFMPEG'])
        ffprobe=ffmpeg.with_name('ffprobe.exe' if ffmpeg.suffix=='.exe' else 'ffprobe')
        inputs,graph=[],[]
        for i in range(16):
            inputs+=['-f','lavfi','-i','testsrc2=s=64x112:r=30:d=3']
            before,after=(0 if i==0 else .15),(0 if i==15 else .15)
            graph.append(f'[{i}:v]hue=h={i*27},tpad=start_mode=clone:start_duration={before}:'
                         f'stop_mode=clone:stop_duration={after},fps=30,settb=AVTB[v{i}]')
        previous='[v0]'
        for i,kind in enumerate(COMPOUND_TRANSITIONS,1):
            joined=f'[j{i}]'
            graph.extend(compound_transition_filters(previous,f'[v{i}]',joined,kind,
                duration=.3,offset=i*3-.15,width=64,height=112,fps=30,prefix=f'bounded{i}'))
            previous=joined
        graph.append(f'{previous}tpad=stop_mode=clone:stop_duration=0.033334,trim=duration=48,setpts=PTS-STARTPTS[out]')
        with tempfile.TemporaryDirectory() as tmp:
            target=Path(tmp)/'all-fifteen.mp4'
            command=[str(ffmpeg),'-v','error','-y','-filter_threads','1','-filter_complex_threads','1',
                     '-filter_buffered_frames','256',*inputs,'-f','lavfi','-i','sine=sample_rate=48000:duration=48',
                     '-filter_complex',';'.join(graph),'-map','[out]','-map','16:a','-c:v','libx264',
                     '-threads','1','-pix_fmt','yuv420p','-c:a','aac','-t','48',str(target)]
            result=subprocess.run(command,capture_output=True,text=True,timeout=120)
            self.assertEqual(result.returncode,0,result.stderr)
            probe=subprocess.run([str(ffprobe),'-v','error','-count_frames','-show_streams','-of','json',str(target)],
                                 capture_output=True,text=True,timeout=30)
            self.assertEqual(probe.returncode,0,probe.stderr)
            streams=json.loads(probe.stdout)['streams'];video=next(s for s in streams if s['codec_type']=='video')
            audio=next(s for s in streams if s['codec_type']=='audio')
            self.assertEqual(int(video['nb_read_frames']),1440)
            self.assertAlmostEqual(float(video['duration']),48,places=3)
            self.assertAlmostEqual(float(audio['duration']),48,places=3)
            decoded=subprocess.run([str(ffmpeg),'-v','error','-i',str(target),'-f','null','-'],capture_output=True,text=True,timeout=30)
            self.assertEqual(decoded.returncode,0,decoded.stderr)


if __name__ == '__main__':
    unittest.main()
