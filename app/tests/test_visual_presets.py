import copy
from collections import Counter
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

from agent_core.direct_renderer import snapshot_from_timeline
from agent_core.independent_export import build_ffmpeg_command, UnsupportedExternalRender
from agent_core.visual_presets import (visual_plan, choose, TRANSITIONS, STANDARD_TRANSITIONS,
    ANIMATIONS, EFFECTS, DEFAULTS, AUTOMATIC_ONCE_ONLY,
    default_caption_motion, caption_motion_selection)


class VisualPresetsTests(unittest.TestCase):
    def test_all_fifteen_compound_names_are_selectable_without_short_name_collisions(self):
        from agent_core.compound_transitions import COMPOUND_TRANSITIONS
        self.assertEqual(len(COMPOUND_TRANSITIONS), 15)
        self.assertEqual(len(STANDARD_TRANSITIONS), 24)
        self.assertEqual(len(TRANSITIONS), 39)
        self.assertFalse(set(STANDARD_TRANSITIONS) & set(COMPOUND_TRANSITIONS))
        for key, label in COMPOUND_TRANSITIONS.items():
            for requested in (label, key):
                with self.subTest(requested=requested):
                    plan = visual_plan({'instruction': '转场使用'+requested,
                        'resolved_editing_options': {'transition': True}})
                    self.assertEqual(plan['categories']['transition']['presets'], [key])
        plan = visual_plan({'instruction': '转场闪白推进、闪黑缩放、向上甩动模糊、向下甩动模糊',
            'resolved_editing_options': {'transition': True}})
        self.assertEqual(plan['categories']['transition']['presets'],
            ['flashwhite_zoom', 'flashblack_zoom', 'whip_blur_up', 'whip_blur_down'])

    def test_all_fifteen_compound_names_can_be_excluded_without_disabling_requested_standard(self):
        from agent_core.compound_transitions import COMPOUND_TRANSITIONS
        for key, label in COMPOUND_TRANSITIONS.items():
            with self.subTest(excluded=label):
                plan = visual_plan({'instruction': '转场不使用'+label+'，使用叠化',
                    'resolved_editing_options': {'transition': True}})
                self.assertEqual(plan['categories']['transition']['presets'], ['fade'])
                self.assertIn(key, plan['categories']['transition']['excluded'])
        for instruction in ('不用任何转场', '关闭全部转场', '不要转场', '无转场',
                            '无需转场', '不做转场', 'no transitions', "don't add transitions"):
            with self.subTest(instruction=instruction):
                plan = visual_plan({'instruction': instruction,
                    'resolved_editing_options': {'transition': True}})
                self.assertFalse(plan['categories']['transition']['enabled'])
                self.assertIsNone(choose(plan, 'transition', 0))

    def test_added_aliases_are_resolved_in_user_order(self):
        plan = visual_plan({'instruction': '转场使用向上甩动、向下甩动、旋转推进、拉远弹回、白闪推进、'
            '黑闪缩放、横挤压、竖挤压、故障转场、失焦转场和暖光转场',
            'resolved_editing_options': {'transition': True}})
        self.assertEqual(plan['categories']['transition']['presets'], [
            'whip_blur_up', 'whip_blur_down', 'rotate_zoom', 'pullback_bounce',
            'flashwhite_zoom', 'flashblack_zoom', 'squeeze_horizontal', 'squeeze_vertical',
            'glitch_horizontal', 'defocus', 'warm_light_sweep'])

    def test_every_workflow_defaults_to_three_transitions_and_one_film_motion(self):
        timeline = {'duration_us': 2_000_000, 'captions': [
            {'start_us': 0, 'end_us': 2_000_000, 'text': 'A simple caption'}],
            'source_map': [{'asset_id': 'a', 'source_start_us': 0,
                'source_end_us': 2_000_000, 'timeline_start_us': 0,
                'timeline_end_us': 2_000_000}]}
        asset = {'asset_id': 'a', 'path': 'source.mp4', 'width': 320,
                 'height': 568, 'has_audio': False}
        with patch('agent_core.direct_renderer.select_caption_font', return_value={}):
            for workflow in ('product_ad', 'crossborder_montage', 'custom'):
                with self.subTest(workflow=workflow):
                    task = {'workflow': workflow, 'job_id': 'job_defaults',
                            'instruction': '', 'target': {'fps': 30},
                            'resolved_editing_options': {'transition': False}}
                    plan = visual_plan(task)
                    self.assertEqual(len(set(plan['categories']['transition']['presets'])), 3)
                    snapshot = snapshot_from_timeline(task, [asset], timeline)
                    text_track = next(t for t in snapshot['tracks'] if t['type'] == 'text')
                    motion = text_track['segments'][0]['caption_motion']
                    self.assertEqual(motion, default_caption_motion(task))
                    self.assertNotEqual(motion['entry'], 'none')
        self.assertEqual(set(DEFAULTS['transition']), set(TRANSITIONS))

    def test_model_and_guide_include_the_registered_animation_and_transition_names(self):
        from agent_core.caption_motion import ENTRIES, LOOPS
        from agent_core.compound_transitions import COMPOUND_TRANSITIONS
        from agent_core.editing_rules import resolve_rules, _visual_capability_prompt
        self.assertEqual((len(ENTRIES)-1, len(LOOPS)-1), (15, 15))
        provider = Mock()
        provider._json.return_value = {'schema_version': '1.0', 'overrides': {}, 'unsupported': []}
        resolve_rules(provider, {'instruction': '字幕散点汇聚，然后循环逐字缩放',
            'workflow': 'custom', 'editing_defaults': {}})
        prompt = provider._json.call_args.args[0]
        guide = (Path(__file__).resolve().parents[1]/'docs'/'caption-motion-and-japanese-voices.md').read_text(encoding='utf-8')
        for catalog in (ENTRIES, LOOPS, COMPOUND_TRANSITIONS):
            for key, label in catalog.items():
                if key == 'none':
                    continue
                with self.subTest(preset=key, label=label):
                    self.assertIn(label, prompt)
                    self.assertIn(label, guide)
        self.assertIn('随机', prompt)
        # The capability prompt must reflect registration at call time so a
        # later added preset cannot silently remain labelled unsupported.
        with patch.dict(ENTRIES, {'future_entry': '测试新入场'}):
            self.assertIn('测试新入场', _visual_capability_prompt())

    def test_combined_caption_and_transition_requests_stay_in_their_categories(self):
        from agent_core.caption_motion import motion_from_instruction
        instruction = '字幕翻转展开，循环柔光呼吸；转场使用旋转缩放和暖光扫入。'
        task = {'workflow': 'custom', 'instruction': instruction, 'target': {'fps': 30},
            'resolved_editing_options': {'text': True, 'transition': True,
                'sticker': False, 'effect': False}}
        plan = visual_plan(task)
        self.assertEqual(motion_from_instruction(instruction), {'entry': 'flip', 'loop': 'glow'})
        self.assertEqual(plan['categories']['transition']['presets'], ['rotate_zoom', 'warm_light_sweep'])
        self.assertFalse(plan['categories']['sticker']['enabled'])
        self.assertFalse(plan['categories']['effect']['enabled'])
        timeline = {'duration_us': 6_000_000, 'captions': [
            {'start_us': 0, 'end_us': 6_000_000, 'text': 'A cute new home'}],
            'source_map': [{'asset_id': 'a', 'source_start_us': 0,
                'source_end_us': 2_000_000, 'timeline_start_us': index*2_000_000,
                'timeline_end_us': (index+1)*2_000_000} for index in range(3)]}
        asset = {'asset_id': 'a', 'path': 'source.mp4', 'width': 320,
                 'height': 568, 'has_audio': False}
        with patch('agent_core.direct_renderer.select_caption_font', return_value={}):
            snapshot = snapshot_from_timeline(task, [asset], timeline)
        videos = next(t['segments'] for t in snapshot['tracks'] if t['type'] == 'video')
        self.assertEqual([s.get('transition', {}).get('type') for s in videos],
            ['rotate_zoom', 'warm_light_sweep', None])
        captions = next(t['segments'] for t in snapshot['tracks'] if t['type'] == 'text')
        self.assertTrue(all(s['caption_motion']['entry'] == 'flip' and
            s['caption_motion']['loop'] == 'glow' for s in captions))
        self.assertFalse(any(t['type'] == 'sticker' for t in snapshot['tracks']))
        self.assertFalse(any('effect' in s for s in videos))

    def test_compound_direction_is_not_replaced_by_short_alias(self):
        plan=visual_plan({'instruction':'转场用向右甩动模糊，镜像甩动和彩色重影推进',
                          'resolved_editing_options':{'transition':True}})
        self.assertEqual(plan['categories']['transition']['presets'],['whip_blur_right','mirror_whip','chromatic_zoom'])

    def test_named_preferences_and_negations(self):
        task = {'instruction': '转场用闪白、向左推移；贴纸弹入或摇摆；特效黑白',
                'formal_ad_policy': {'transitions_required': True, 'sticker_required': True, 'effects_required': True}}
        plan = visual_plan(task, 'job1')
        self.assertEqual(plan['categories']['transition']['presets'], ['fadewhite', 'slideleft'])
        self.assertEqual(plan['categories']['sticker']['presets'], ['pop', 'swing'])
        self.assertEqual(choose(plan, 'effect', 0), 'mono')
        plan = visual_plan({**task, 'instruction': '不要暖色特效；不要贴纸；转场不要闪白'})
        self.assertNotIn('warm', plan['categories']['effect']['presets'])
        self.assertIsNone(choose(plan, 'sticker', 0))
        self.assertNotIn('fadewhite', plan['categories']['transition']['presets'])

    def test_retry_is_stable_and_only_explicit_transition_off_wins(self):
        task = {'instruction': '', 'workflow': 'custom', 'resolved_editing_options':
                {'transition': False, 'sticker': False, 'effect': False}}
        self.assertEqual(visual_plan(task, 'job1'), visual_plan(task, 'job1'))
        self.assertEqual(len(visual_plan(task)['categories']['transition']['presets']), 3)
        self.assertTrue(all(not visual_plan(task)['categories'][key]['presets']
                            for key in ('sticker', 'effect')))
        task['formal_ad_policy'] = {'transitions_required': True}
        self.assertIsNotNone(choose(visual_plan(task), 'transition', 0))
        task['explicit_editing_overrides'] = {'transition': {'value': False,
            'quote': '不要转场'}}
        self.assertIsNone(choose(visual_plan(task), 'transition', 0))

    def test_automatic_transitions_cover_all_presets_without_subtle_only_bias(self):
        sampled = Counter()
        for number in range(3000):
            row = visual_plan({'job_id': f'job_{number}', 'workflow': 'custom',
                'resolved_editing_options': {'transition': False}})['categories']['transition']
            self.assertTrue(row['enabled'])
            self.assertEqual(len(row['presets']), 3)
            self.assertEqual(len(set(row['presets'])), 3)
            sampled.update(row['presets'])
        self.assertEqual(set(sampled), set(TRANSITIONS))
        # This deterministic sample is broad enough to catch a modulo/first-
        # three bias while leaving ordinary random sampling plenty of room.
        self.assertTrue(all(150 < count < 320 for count in sampled.values()), sampled)

    def test_shuffled_bags_are_index_stable_unique_and_never_repeat_neighbors(self):
        for number in range(40):
            task = {'job_id': f'job_bag_{number}', 'instruction': ''}
            plan = visual_plan(task)
            values = [choose(plan, 'transition', index) for index in range(90)]
            self.assertTrue(all(a != b for a, b in zip(values, values[1:])))
            row = plan['categories']['transition']
            self.assertEqual(set(values[:3]), set(row['presets']))
            if row['automatic_once_only']:
                for key in row['automatic_once_only']:
                    self.assertEqual(values.count(key), 1)
                self.assertEqual(set(values[3:]), set(row['presets']) - set(row['automatic_once_only']))
            else:
                for start in range(0, len(values), 3):
                    self.assertEqual(set(values[start:start+3]), set(row['presets']))
            # Looking up late boundaries first cannot advance shared RNG state.
            for index in (89, 4, 0, 40, 1, 3, 87, 2):
                self.assertEqual(choose(plan, 'transition', index), values[index])
            if not row['automatic_once_only']:
                self.assertGreater(len({tuple(values[i:i+3]) for i in range(0, 90, 3)}), 1)
            self.assertEqual(values, [choose(visual_plan(task), 'transition', i)
                                      for i in range(90)])

    def test_costly_automatic_transitions_run_at_most_once_but_explicit_requests_are_unlimited(self):
        reached = set()
        for number in range(300):
            plan = visual_plan({'job_id': f'job_cost_{number}'})
            row = plan['categories']['transition']
            costly = set(row['presets']) & AUTOMATIC_ONCE_ONLY
            self.assertLessEqual(len(costly), 1)
            self.assertEqual(set(row['automatic_once_only']), costly)
            values = [choose(plan, 'transition', index) for index in range(150)]
            for key in costly:
                self.assertEqual(values.count(key), 1)
            reached.update(costly)
        self.assertEqual(reached, AUTOMATIC_ONCE_ONLY)
        explicit = ['glitch_horizontal', 'defocus', 'warm_light_sweep']
        plan = visual_plan({'instruction': '转场横向故障、镜头散焦、暖光扫入'})
        self.assertEqual(plan['categories']['transition']['automatic_once_only'], [])
        self.assertEqual([choose(plan, 'transition', index) for index in range(9)], explicit * 3)
        # All permitted lightweight presets can be explicitly excluded. In
        # that corner case never silently reintroduce one just to fill a bag.
        instruction = '；'.join('转场不要'+name for key, name in TRANSITIONS.items()
                               if key not in AUTOMATIC_ONCE_ONLY)
        plan = visual_plan({'instruction': instruction})
        self.assertEqual(len(plan['categories']['transition']['presets']), 1)
        self.assertIn(choose(plan, 'transition', 0), AUTOMATIC_ONCE_ONLY)
        self.assertIsNone(choose(plan, 'transition', 1))

    def test_transition_exclusions_shrink_the_sample_and_user_order_is_not_shuffled(self):
        for available_count in (0, 1, 2, 3):
            keep = list(TRANSITIONS)[:available_count]
            excluded = [label for key, label in TRANSITIONS.items() if key not in keep]
            task = {'job_id': 'job_excluded', 'instruction':
                '；'.join('转场不要'+label for label in excluded)}
            plan = visual_plan(task)
            row = plan['categories']['transition']
            self.assertEqual(set(row['presets']), set(keep))
            values = [choose(plan, 'transition', i) for i in range(20)]
            if available_count >= 2:
                self.assertTrue(all(a != b for a, b in zip(values, values[1:])))
            elif available_count == 0:
                self.assertEqual(values, [None] * 20)
        task = {'job_id': 'job_order', 'instruction':
            '转场使用彩色重影推进、叠化、向右甩动模糊'}
        plan = visual_plan(task)
        expected = ['chromatic_zoom', 'fade', 'whip_blur_right']
        self.assertEqual(plan['categories']['transition']['presets'], expected)
        self.assertEqual([choose(plan, 'transition', i) for i in range(12)], expected * 4)
        for instruction in ('不要闪白', '不要向右甩动模糊'):
            plan = visual_plan({'instruction': instruction})
            self.assertTrue(plan['categories']['transition']['excluded'])
        plan = visual_plan({'instruction': '字幕不要闪白'})
        self.assertEqual(plan['categories']['transition']['excluded'], [])

    def test_stable_identity_beats_temporary_seed_and_batch_children_use_independent_draws(self):
        task = {'job_id': 'job_stable', 'batch_id': 'batch_one', 'output_id': 'output_01',
                'instruction': '制作广告', 'workflow': 'custom'}
        self.assertEqual(visual_plan(task, 'first-temp'), visual_plan(task, 'retry-temp'))
        self.assertEqual(default_caption_motion(task, 'first-temp'),
                         default_caption_motion(task, 'retry-temp'))
        # Changing category flags cannot consume another category's random draw.
        changed = {**task, 'instruction': '特效黑白；贴纸弹入',
                   'resolved_editing_options': {'effect': True, 'sticker': True}}
        self.assertEqual(visual_plan(task)['categories']['transition'],
                         visual_plan(changed)['categories']['transition'])
        self.assertEqual(default_caption_motion(task), default_caption_motion(changed))
        children = [{**task, 'job_id': f'job_child_{i}', 'output_id': f'output_{i}'}
                    for i in range(40)]
        self.assertEqual(len({caption_motion_selection(t)['selection_seed']
                              for t in children}), 40)
        self.assertGreater(len({tuple(visual_plan(t)['categories']['transition']['presets'])
                                for t in children}), 30)
        # Standalone render calls without task ids use their stable job seed.
        self.assertNotEqual(caption_motion_selection({}, 'job1')['selection_seed'],
                            caption_motion_selection({}, 'job2')['selection_seed'])
        batch_task = {'batch_id': 'batch_real', 'batch_output_id': 'output_01',
                      'batch_output_index': 1, 'instruction': '制作广告'}
        sibling = {**batch_task, 'batch_output_id': 'output_02', 'batch_output_index': 2}
        first = caption_motion_selection(batch_task, 'job_real_child_1')
        second = caption_motion_selection(sibling, 'job_real_child_2')
        self.assertNotEqual(first['selection_seed'], second['selection_seed'])
        self.assertEqual(first, caption_motion_selection(batch_task, 'job_real_child_1'))
        self.assertEqual(visual_plan(batch_task, 'job_real_child_1'),
                         visual_plan(batch_task, 'job_real_child_1'))
        self.assertNotEqual(visual_plan(batch_task, 'job_real_child_1')['categories']['transition']['selection_seed'],
                            visual_plan(sibling, 'job_real_child_2')['categories']['transition']['selection_seed'])
        # Even a legacy spec with only its batch id must keep the supplied
        # child job directory seed; the whole batch cannot share one draw.
        self.assertNotEqual(caption_motion_selection({'batch_id': 'batch_real'}, 'job_child_1')['selection_seed'],
                            caption_motion_selection({'batch_id': 'batch_real'}, 'job_child_2')['selection_seed'])
        self.assertNotEqual(caption_motion_selection(batch_task)['selection_seed'],
                            caption_motion_selection(sibling)['selection_seed'])

    def test_caption_random_defaults_have_half_loop_probability_and_all_presets_reachable(self):
        from agent_core.caption_motion import ENTRIES, LOOPS
        entry_counts, loop_counts = Counter(), Counter()
        for number in range(3000):
            task = {'job_id': f'job_caption_{number}'}
            metadata = caption_motion_selection(task)
            motion = metadata['motion']
            self.assertEqual(motion, default_caption_motion(task))
            self.assertEqual(metadata['default_motion'], motion)
            self.assertEqual(metadata['selection'], {'entry': 'automatic', 'loop': 'automatic'})
            entry_counts[motion['entry']] += 1
            loop_counts[motion['loop']] += 1
        self.assertEqual(set(entry_counts), set(ENTRIES) - {'none'})
        self.assertEqual(set(loop_counts), set(LOOPS))
        self.assertTrue(1350 <= loop_counts['none'] <= 1650, loop_counts)
        self.assertTrue(all(130 < count < 280 for count in entry_counts.values()), entry_counts)
        self.assertTrue(all(55 < loop_counts[key] < 150 for key in LOOPS if key != 'none'), loop_counts)

    def test_caption_explicit_motion_and_closures_override_random_defaults(self):
        for number in range(30):
            base = {'job_id': f'job_override_{number}'}
            task = {**base, 'caption_motion': {'entry': 'flip', 'loop': 'glow',
                'entry_duration_ms': 420}, 'instruction': '字幕从左滑入，循环逐字缩放'}
            motion = default_caption_motion(task)
            self.assertEqual((motion['entry'], motion['loop']), ('slideleft', 'glyph_pulse'))
            self.assertEqual(motion['entry_duration_ms'], 420)
            metadata = caption_motion_selection(task)
            self.assertEqual(metadata['selection'], {'entry': 'user', 'loop': 'user'})
            for instruction in ('不要字幕动画', '字幕保持静止'):
                motion = default_caption_motion({**task, 'instruction': instruction})
                self.assertEqual((motion['entry'], motion['loop']), ('none', 'none'))
            motion = default_caption_motion({**base, 'instruction': '不要字幕循环动画'})
            self.assertNotEqual(motion['entry'], 'none')
            self.assertEqual(motion['loop'], 'none')

    def test_snapshot_applies_the_same_random_motion_to_every_existing_cue_only(self):
        task = {'job_id': 'job_same_film', 'workflow': 'custom', 'instruction': '',
                'target': {'fps': 30}}
        timeline = {'duration_us': 9_000_000, 'captions': [
            {'start_us': i * 3_000_000, 'end_us': (i+1) * 3_000_000, 'text': f'Caption {i}'}
            for i in range(3)], 'source_map': [
            {'asset_id': 'a', 'source_start_us': 0, 'source_end_us': 3_000_000,
             'timeline_start_us': i * 3_000_000, 'timeline_end_us': (i+1) * 3_000_000}
            for i in range(3)]}
        asset = {'asset_id': 'a', 'path': 'source.mp4', 'width': 320,
                 'height': 568, 'has_audio': False}
        with patch('agent_core.direct_renderer.select_caption_font', return_value={}):
            snapshot = snapshot_from_timeline(task, [asset], timeline)
            captions = next(t['segments'] for t in snapshot['tracks'] if t['type'] == 'text')
            self.assertEqual(len(captions), 3)
            self.assertTrue(all(c['caption_motion'] == default_caption_motion(task) for c in captions))
            timeline['captions'] = []
            empty = snapshot_from_timeline(task, [asset], timeline)
            self.assertFalse(any(t['type'] == 'text' for t in empty['tracks']))

    def test_snapshot_carries_user_choices_without_changing_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            asset = {'asset_id': 'a', 'path': str(Path(tmp)/'a.mp4'), 'width': 320, 'height': 568, 'has_audio': False}
            timeline = {'duration_us': 4_000_000, 'source_map': [
                {'asset_id': 'a', 'timeline_start_us': i*2_000_000, 'timeline_end_us': (i+1)*2_000_000,
                 'source_start_us': 0, 'source_end_us': 2_000_000} for i in range(2)]}
            original = copy.deepcopy(timeline)
            result = snapshot_from_timeline({'instruction': '转场向上擦除；特效黑白；不要贴纸',
                'target': {'fps': 30}, 'formal_ad_policy': {'transitions_required': True, 'effects_required': True}},
                [asset], timeline, work_dir=tmp)
            rows = result['tracks'][0]['segments']
            self.assertEqual(rows[0]['transition']['type'], 'wipeup')
            self.assertEqual(rows[0]['effect'], {'type': 'mono', 'strength': 1.0})
            self.assertEqual(timeline, original)
            self.assertEqual(result['duration'], 4_000_000)
            self.assertFalse(any(t['type']=='sticker' for t in result['tracks']))

    def test_late_animated_sticker_has_local_entrance_and_absolute_placement(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'video.mp4'; source.touch()
            sticker = root/'sticker.png'; Image.new('RGBA', (40,40), 'red').save(sticker)
            value = {'duration': 4_000_000, 'fps': 30, 'canvas_config': {'width': 320,'height':568},
                'materials': {'videos': [{'id':'v','path':str(source),'has_audio':False}],
                              'stickers':[{'id':'s','path':str(sticker)}]},
                'tracks':[{'type':'video','segments':[{'material_id':'v',
                    'target_timerange':{'start':0,'duration':4_000_000}}]},
                    {'type':'sticker','segments':[{'material_id':'s','animation':'pop','position':{'x':30,'y':40},
                    'target_timerange':{'start':2_000_000,'duration':1_000_000}}]}]}
            command = build_ffmpeg_command(value, root/'out.mp4','ffmpeg')
            graph = command[command.index('-filter_complex')+1]
            self.assertIn('setpts=PTS+2.000000/TB', graph)
            self.assertIn('eof_action=pass:repeatlast=0',graph)
            self.assertIn('t/0.280000',graph)
            value['tracks'][1]['segments'][0]['animation']='unknown'
            with self.assertRaises(UnsupportedExternalRender):
                build_ffmpeg_command(value, root/'out.mp4','ffmpeg')

    def test_animated_caption_repeats_settled_frame_at_hold_handoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root/'video.mp4'; source.touch()
            value = {'duration': 1_000_000, 'fps': 30,
                'canvas_config': {'width': 320, 'height': 568},
                'materials': {'videos': [{'id': 'v', 'path': str(source),
                                          'has_audio': False}]},
                'tracks': [{'type': 'video', 'segments': [{'material_id': 'v',
                    'target_timerange': {'start': 0, 'duration': 1_000_000}}]}]}
            subtitle_images = [
                {'kind': 'video', 'loop': False, 'path': str(root/'entry.mov'),
                 'start_us': 0, 'end_us': 366_667, 'x': 0, 'y': 0},
                {'kind': 'image', 'loop': False, 'path': str(root/'hold.png'),
                 'start_us': 366_667, 'end_us': 1_000_000, 'x': 0, 'y': 0},
            ]
            command = build_ffmpeg_command(value, root/'out.mp4', 'ffmpeg',
                                           subtitle_images=subtitle_images)
            graph = command[command.index('-filter_complex') + 1]
            self.assertIn('eof_action=repeat:repeatlast=1', graph)
            self.assertNotIn('eof_action=pass:repeatlast=0', graph)

    def test_bad_effect_strength_and_unknown_preset_fail_truthfully(self):
        from agent_core.visual_presets import effect_filters
        for bad in (float('nan'), float('inf'), -1, 2):
            with self.assertRaises(ValueError): effect_filters({'type':'warm','strength':bad})
        with self.assertRaises(ValueError): effect_filters({'type':'unknown'})
        self.assertEqual((len(TRANSITIONS), len(ANIMATIONS), len(EFFECTS)), (39,12,12))


if __name__ == '__main__':
    unittest.main()
