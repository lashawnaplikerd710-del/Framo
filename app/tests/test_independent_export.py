import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from agent_core.independent_export import (IndependentExport, UnsupportedExternalRender,
    build_ffmpeg_command, validate_output_directory, _subtitle_rows)


def snapshot(source, *, effect=False):
    tracks = [{'id': 'track', 'type': 'video', 'segments': [{
        'id': 'segment', 'material_id': 'material',
        'target_timerange': {'start': 0, 'duration': 1_000_000},
        'source_timerange': {'start': 0, 'duration': 1_000_000}}]}]
    if effect:
        tracks.append({'id': 'effect', 'type': 'effect', 'segments': [{
            'id': 'effect-segment', 'target_timerange': {'start': 0, 'duration': 1_000_000}}]})
    return {'id': 'draft', 'duration': 1_000_000, 'fps': 30,
            'canvas_config': {'width': 720, 'height': 1280},
            'materials': {'videos': [{'id': 'material', 'path': str(source), 'has_audio': False}]},
            'tracks': tracks,
            'batch_outputs': [{'output_id': 'one', 'start_us': 0, 'end_us': 1_000_000,
                               'gap_after_us': 0}]}


class IndependentExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source.mp4'
        self.source.write_bytes(b'fixture')

    def tearDown(self):
        self.tmp.cleanup()

    def test_output_directory_must_be_outside_source_tree(self):
        with self.assertRaises(ValueError):
            validate_output_directory(self.root / 'exports', source_paths=[self.source])
        target = validate_output_directory(self.root.parent / 'exports-independent', source_paths=[self.source])
        self.assertTrue(target.is_dir())

    def test_loose_drive_root_source_does_not_block_dedicated_output_folder(self):
        drive=Path(self.root.anchor)
        loose_source=drive/'framo-directory-validation-input.mp4'
        target=validate_output_directory(self.root/'outputs',source_paths=[loose_source])
        self.assertTrue(target.is_dir())
        with self.assertRaises(ValueError):
            validate_output_directory(drive,source_paths=[loose_source])

    def test_drive_root_exception_does_not_relax_explicit_source_or_draft_trees(self):
        drive=Path(self.root.anchor)
        with self.assertRaises(ValueError):
            validate_output_directory(self.root/'outputs',source_paths=[drive])
        with self.assertRaises(ValueError):
            validate_output_directory(self.root/'draft'/'outputs',
                source_paths=[drive/'loose-source.mp4'],draft_paths=[self.root/'draft'])

    def test_command_uses_browser_compatible_avc_with_fast_start(self):
        command = build_ffmpeg_command(snapshot(self.source), self.root / 'out.mp4', 'ffmpeg.exe')
        self.assertEqual(command[command.index('-c:v')+1], 'libx264')
        self.assertEqual(command[command.index('-pix_fmt')+1], 'yuv420p')
        self.assertIn('+faststart', command)
        self.assertNotIn('mpeg4', command)
        self.assertIn('-filter_complex', command)

    def test_mixed_resolution_inputs_are_normalized_and_volume_is_preserved(self):
        value = snapshot(self.source)
        value['materials']['videos'][0]['has_audio'] = True
        value['tracks'][0]['segments'][0]['volume'] = .25
        command = build_ffmpeg_command(value, self.root / 'out.mp4', 'ffmpeg.exe')
        graph = command[command.index('-filter_complex') + 1]
        self.assertIn('scale=720:1280', graph)
        self.assertIn('setsar=1,fps=30', graph)
        self.assertEqual(command[command.index('-r') + 1], '30')
        self.assertIn('volume=0.25', graph)

    def test_source_audio_can_be_muted_while_direct_tracks_remain(self):
        value = snapshot(self.source)
        value['materials']['videos'][0]['has_audio'] = True
        value['source_audio_muted'] = True
        command = build_ffmpeg_command(value, self.root / 'out.mp4', 'ffmpeg.exe')
        graph = command[command.index('-filter_complex') + 1]
        self.assertIn('anullsrc=r=48000:cl=stereo', graph)
        self.assertNotIn('[0:a]aresample=', graph)

    def test_adjacent_transitions_chain_unique_filter_labels(self):
        value = snapshot(self.source)
        segments = []
        for index in range(3):
            segments.append({
                'id': f'segment-{index}', 'material_id': 'material',
                'target_timerange': {'start': index * 1_000_000, 'duration': 1_000_000},
                'source_timerange': {'start': 0, 'duration': 1_000_000},
                'transition_after': {'type': 'fade', 'duration_us': 200_000}
            } if index < 2 else {
                'id': f'segment-{index}', 'material_id': 'material',
                'target_timerange': {'start': index * 1_000_000, 'duration': 1_000_000},
                'source_timerange': {'start': 0, 'duration': 1_000_000}
            })
        value['duration'] = 3_000_000
        value['tracks'][0]['segments'] = segments
        value['batch_outputs'][0]['end_us'] = 3_000_000
        graph = build_ffmpeg_command(value, self.root / 'out.mp4', 'ffmpeg.exe')
        graph = graph[graph.index('-filter_complex') + 1]
        self.assertEqual(graph.count('xfade=transition=fade'), 2)
        self.assertIn('offset=0.900000', graph)
        self.assertIn('offset=1.900000', graph)
        import re
        labels = [re.search(r'(\[[^\]]+\])$', chain).group(1) for chain in graph.split(';')]
        self.assertEqual(len(labels), len(set(labels)))

    def test_jianying_json_text_material_uses_visible_text_only(self):
        value = snapshot(self.source)
        value['materials']['texts'] = [{'id': 'caption', 'content':
            '{"styles":[{"font":{"path":"D:/private/font.ttf"}}],"text":"卖点字幕"}'}]
        value['tracks'].append({'type': 'text', 'segments': [{
            'material_id': 'caption', 'target_timerange': {'start': 0, 'duration': 500_000}}]})
        rows = _subtitle_rows(value)
        self.assertEqual(rows, [(0, 500_000, '卖点字幕', 0)])

    def test_stripped_effect_snapshot_is_not_accepted_as_complete(self):
        value = snapshot(self.source)
        value['external_omitted_groups'] = ['effects']
        with self.assertRaises(UnsupportedExternalRender):
            build_ffmpeg_command(value, self.root/'out.mp4', 'ffmpeg.exe')

    def test_native_unknown_transition_is_not_mapped_to_fade(self):
        value = snapshot(self.source)
        value['materials']['transitions'] = [{'id':'unknown','type':'transition','resource_id':'123','duration':100000}]
        value['tracks'][0]['segments'][0]['extra_material_refs'] = ['unknown']
        with self.assertRaises(UnsupportedExternalRender):
            build_ffmpeg_command(value, self.root/'out.mp4', 'ffmpeg.exe')

    def test_zoom_motion_preserves_keyframe_times(self):
        value = snapshot(self.source)
        value['tracks'][0]['segments'][0]['zoom_keyframes'] = [
            {'time_us':0,'scale':1.0}, {'time_us':1_000_000,'scale':1.1}]
        cmd = build_ffmpeg_command(value, self.root/'out.mp4', 'ffmpeg.exe')
        graph = cmd[cmd.index('-filter_complex')+1]
        self.assertIn('zoompan=', graph)
        self.assertIn('on-', graph)

    def test_native_audio_fade_is_read_from_material_reference(self):
        value = snapshot(self.source)
        value['materials']['videos'][0]['has_audio'] = True
        value['materials']['audio_fades'] = [{'id':'fade','type':'audio_fade','fade_in_duration':100000,'fade_out_duration':200000}]
        value['tracks'][0]['segments'][0]['extra_material_refs'] = ['fade']
        cmd = build_ffmpeg_command(value, self.root/'out.mp4', 'ffmpeg.exe')
        graph = cmd[cmd.index('-filter_complex')+1]
        self.assertIn('afade=t=in:d=0.100000', graph)
        self.assertIn('afade=t=out:st=0.800000:d=0.200000', graph)

    def test_builtin_ad_visual_primitives_are_rendered(self):
        value = snapshot(self.source)
        segment = value['tracks'][0]['segments'][0]
        segment['filter'] = {'type': 'ad_color', 'contrast': 1.03, 'saturation': 1.08, 'brightness': 0.01}
        segment['effect'] = {'type': 'soft_sharpen', 'strength': 0.35}
        value['materials']['stickers'] = [{'id': 'badge', 'type': 'sticker', 'path': str(self.source)}]
        value['tracks'].append({'id': 'sticker-track', 'type': 'sticker', 'segments': [{
            'material_id': 'badge', 'target_timerange': {'start': 0, 'duration': 1_000_000},
            'position': {'x': 'main_w-overlay_w-40', 'y': '40'}}]})
        command = build_ffmpeg_command(value, self.root / 'out.mp4', 'ffmpeg.exe')
        graph = command[command.index('-filter_complex') + 1]
        self.assertIn('eq=contrast=1.0300:saturation=1.0800:brightness=0.0100', graph)
        self.assertIn('unsharp=lx=5:ly=5:la=0.3500', graph)
        self.assertIn("overlay=x='main_w-overlay_w-40':y='40'", graph)

    def test_extra_audio_is_mixed_and_transform_is_not_silently_dropped(self):
        value = snapshot(self.source)
        value['tracks'].append({'type': 'audio', 'segments': [{'id': 'music'}]})
        # The fixture has no material for the audio segment, so the exporter
        # must report the missing source rather than silently drop the track.
        with self.assertRaises(ValueError):
            build_ffmpeg_command(value, self.root/'out.mp4', 'ffmpeg.exe')
        value = snapshot(self.source)
        value['tracks'][0]['segments'][0]['clip'] = {'scale': {'x': 2, 'y': 1}}
        with self.assertRaises(UnsupportedExternalRender):
            build_ffmpeg_command(value, self.root/'out.mp4', 'ffmpeg.exe')

    def test_bad_range_does_not_prevent_other_item_attempt(self):
        value = snapshot(self.source, effect=True)
        value['batch_outputs'].append({'output_id': 'bad', 'start_us': None, 'end_us': 2})
        with tempfile.TemporaryDirectory() as out:
            with patch('agent_core.independent_export.discover_tools', return_value={'ffmpeg': 'ffmpeg.exe', 'ffprobe': None}):
                report = IndependentExport(batch_id='batch_ranges', master_snapshot=value, output_dir=out).run()
        self.assertEqual(len(report['items']), 2)
        self.assertIn('UNSUPPORTED_EXTERNAL_RENDER', report['items'][0]['error'])
        self.assertIn('时间范围', report['items'][1]['error'])

    def test_retry_failure_clears_old_success_evidence(self):
        import json
        with tempfile.TemporaryDirectory() as out:
            Path(out, 'independent-export-report.json').write_text(json.dumps({
                'batch_id': 'b', 'items': [{'output_id':'one', 'status':'COMPLETED',
                    'output_file':'stale.mp4', 'playback_ok':True}]}))
            with patch('agent_core.independent_export.discover_tools', return_value={'ffmpeg':'ffmpeg.exe','ffprobe':None}):
                report = IndependentExport(batch_id='b', master_snapshot=snapshot(self.source,effect=True),output_dir=out).run()
        self.assertFalse(report['items'][0]['playback_ok'])
        self.assertIsNone(report['items'][0]['output_file'])

    def test_effect_is_reported_without_running_ffmpeg(self):
        output = self.root.parent / 'export-effect'
        with patch('agent_core.independent_export.discover_tools', return_value={'ffmpeg': 'ffmpeg.exe', 'ffprobe': None}):
            report = IndependentExport(batch_id='batch_effect', master_snapshot=snapshot(self.source, effect=True),
                                       output_dir=output, source_draft=self.root / 'draft').run()
        self.assertEqual(report['state'], 'FAILED')
        self.assertIn('UNSUPPORTED_EXTERNAL_RENDER', report['items'][0]['error'])
        self.assertFalse(list(output.glob('*.mp4')))


if __name__ == '__main__':
    unittest.main()
