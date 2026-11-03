import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_core.editing_rules import configure_rules, apply_rules, resolve_rules
from agent_core.workbench import Workbench
from agent_core.desktop_workbench import WORKFLOWS, MARKET_PRESETS, default_instruction
from agent_core.commerce import compile_ad, plan_ad
from agent_core.direct_resources import build_direct_media_plan
from agent_core.direct_renderer import snapshot_from_timeline
from tests import test_commerce as fixture_module
from tests import test_task_service as service_fixture


class EditingRuleTests(unittest.TestCase):
    def spec(self, instruction, workflow='custom', country=None):
        return Workbench._prepare_spec(None,{'instruction':instruction,'workflow':workflow,
            'target_country':country,'asset_ids':['asset_a','asset_b']})

    def test_only_three_creation_categories_and_no_custom_default_prompt(self):
        self.assertEqual(list(WORKFLOWS.values()),['crossborder_montage','crossborder_montage','custom','long_video','long_video_no_voice'])
        self.assertEqual(set(MARKET_PRESETS.values()),{'日本','美国'})
        self.assertEqual(default_instruction('custom'),'')

    def test_custom_does_not_inherit_ad_or_voice_requirements(self):
        s=self.spec('按原顺序简单剪辑')
        self.assertFalse(s['formal_ad_policy']['formal_ad'])
        self.assertEqual(s['required_native_categories'],[])
        self.assertEqual(s['narration_mode'],'text_only')
        self.assertFalse(s['constraints']['subtitles'])
        self.assertTrue(s['mute_source_audio'])
        self.assertTrue(s['resolved_editing_options']['transition'])
        self.assertTrue(s['formal_ad_policy']['transitions_required'])
        self.assertNotIn('transition', s['minimum_native_counts'])

    def test_automatic_transition_defaults_apply_without_other_custom_decorations(self):
        from agent_core.visual_presets import visual_plan, caption_motion_selection
        for workflow, country in (('custom', None), ('crossborder_montage', '日本'),
                                  ('crossborder_montage', '美国')):
            with self.subTest(workflow=workflow, country=country):
                spec = self.spec('按原顺序简单剪辑', workflow, country)
                plan = visual_plan(spec, 'job-stable-id')
                transition = plan['categories']['transition']
                self.assertTrue(transition['enabled'])
                self.assertEqual(len(set(transition['presets'])), 3)
                self.assertEqual(plan, visual_plan(spec, 'job-stable-id'))
                self.assertNotIn('transition', spec['required_native_categories'])
                self.assertNotIn('transition', spec['minimum_native_counts'])
                if workflow == 'custom':
                    self.assertFalse(plan['categories']['sticker']['enabled'])
                    self.assertFalse(plan['categories']['effect']['enabled'])
                    self.assertFalse(spec['constraints']['subtitles'])
                    self.assertEqual(spec['narration_mode'], 'text_only')
                selection = caption_motion_selection(spec, 'job-stable-id')
                self.assertEqual(selection, caption_motion_selection(spec, 'job-stable-id'))
                self.assertEqual(selection['loop_probability'], .5)

    def test_unrelated_model_false_and_known_automatic_capabilities_do_not_disable_defaults(self):
        spec = self.spec('按原顺序简单剪辑')
        provider = Mock()
        response = {'schema_version': '1.0', 'overrides': {
            'transition': {'value': False, 'quote': '按原顺序简单剪辑'}},
            'unsupported': ['自动随机转场', '随机选择字幕入场动画', '自动字幕循环动画']}
        provider._json.return_value = copy.deepcopy(response)
        resolution = resolve_rules(provider, spec)
        self.assertNotIn('transition', resolution['overrides'])
        self.assertEqual(resolution['unsupported'], [])
        self.assertEqual(resolution['ignored_unsupported'], response['unsupported'])
        self.assertEqual(resolution['ignored_overrides'][0]['reason'], 'no_explicit_category_switch')
        for result in (resolution, response):  # Also protect previously saved model resolutions.
            with self.subTest(result=result):
                updated = apply_rules(spec, result)
                self.assertTrue(updated['resolved_editing_options']['transition'])
                self.assertFalse(updated['constraints']['subtitles'])
        prompt = provider._json.call_args.args[0]
        self.assertIn('从全部转场中抽取3种不同款式', prompt)
        self.assertIn('文字循环以50%概率启用', prompt)
        self.assertNotIn('只在用户明确指定名称时使用', prompt)
        self.assertNotIn('新广告默认只用轻量整体弹入', prompt)
        for workflow, country in (('custom', None), ('crossborder_montage', '日本')):
            with self.subTest(caption_workflow=workflow):
                original = self.spec('按原顺序简单剪辑', workflow, country)
                wrong = {'schema_version': '1.0', 'overrides': {
                    'text': {'value': workflow == 'custom', 'quote': '按原顺序简单剪辑'}}, 'unsupported': []}
                corrected = apply_rules(original, wrong)
                self.assertEqual(corrected['constraints']['subtitles'], workflow != 'custom')

    def test_known_named_visuals_are_not_unsupported_but_unknown_capabilities_still_fail(self):
        from agent_core.caption_motion import ENTRIES, LOOPS
        from agent_core.visual_presets import TRANSITIONS
        spec = self.spec('添加字幕动画和转场')
        known = ['转场使用'+label for label in TRANSITIONS.values()]
        known += ['字幕使用'+label for catalog in (ENTRIES, LOOPS)
                  for key, label in catalog.items() if key != 'none']
        updated = apply_rules(spec, {'schema_version': '1.0', 'overrides': {}, 'unsupported': known})
        self.assertTrue(updated['resolved_editing_options']['transition'])
        for unsupported in ('自定义3D粒子转场', '字幕沿人物运动轨迹跟踪', '未支持的剪映专属特效'):
            with self.subTest(unsupported=unsupported), self.assertRaisesRegex(ValueError, '不能满足'):
                apply_rules(spec, {'schema_version': '1.0', 'overrides': {}, 'unsupported': [unsupported]})

    def test_explicit_transition_disable_is_local_and_animation_disable_does_not_add_captions(self):
        from agent_core.visual_presets import visual_plan, caption_motion_selection
        empty = {'schema_version': '1.0', 'overrides': {}, 'unsupported': []}
        for instruction in ('不要转场', '不用任何转场', '关闭全部转场',
                            '转场不要', '不要音乐、特效和转场', '只加音乐'):
            with self.subTest(instruction=instruction):
                spec = apply_rules(self.spec(instruction), empty)
                self.assertFalse(spec['resolved_editing_options']['transition'])
                self.assertFalse(visual_plan(spec, 'stable')['categories']['transition']['enabled'])
        for instruction in ('不要字幕', '不要新增字幕', '无字幕', '字幕不要', '只加音乐'):
            with self.subTest(no_captions=instruction):
                spec = apply_rules(self.spec(instruction, 'crossborder_montage', '日本'), empty)
                self.assertFalse(spec['constraints']['subtitles'])
        for workflow, country in (('custom', None), ('crossborder_montage', '日本')):
            for value, quote in ((True, '不要字幕动画'), (False, '不要字幕动画'), (False, '不要字幕')):
                with self.subTest(workflow=workflow, value=value, quote=quote):
                    spec = self.spec('不要字幕动画', workflow, country)
                    provider = Mock()
                    provider._json.return_value = {'schema_version': '1.0', 'overrides': {
                        'text': {'value': value, 'quote': quote}}, 'unsupported': []}
                    spec = apply_rules(spec, resolve_rules(provider, spec))
                    self.assertEqual(spec['constraints']['subtitles'], workflow != 'custom')
                    motion = caption_motion_selection(spec, 'stable')['motion']
                    self.assertEqual((motion['entry'], motion['loop']), ('none', 'none'))

    def test_explicit_transition_request_is_required_but_not_other_elements(self):
        spec = self.spec('添加镜头散焦转场')
        result = {'schema_version': '1.0', 'overrides': {
            'transition': {'value': True, 'quote': '添加镜头散焦转场'}}, 'unsupported': []}
        spec = apply_rules(spec, result)
        self.assertEqual(spec['required_native_categories'], ['transition'])
        self.assertEqual(spec['minimum_native_counts'], {'transition': 1})
        self.assertFalse(spec['constraints']['subtitles'])
        self.assertEqual(spec['narration_mode'], 'text_only')

    def test_crossborder_template_stays_available_but_explicit_omissions_win(self):
        for country in ('日本','美国'):
            s=self.spec('不要音乐，不要字幕，保留原声','crossborder_montage',country)
            self.assertTrue(s['formal_ad_policy']['music_required'])
            resolved={'schema_version':'1.0','overrides':{
                'music':{'value':False,'quote':'不要音乐'},'text':{'value':False,'quote':'不要字幕'},
                'mute_source_audio':{'value':False,'quote':'保留原声'}},'unsupported':[]}
            updated=apply_rules(s,resolved)
            self.assertFalse(updated['formal_ad_policy']['music_required'])
            self.assertNotIn('music',updated['required_native_categories'])
            self.assertFalse(updated['constraints']['subtitles'])
            self.assertFalse(updated['mute_source_audio'])
            self.assertEqual(updated['target_country'],country)

    def test_custom_uses_model_resolved_explicit_requests_without_changing_input(self):
        s=self.spec('加中文配音和音乐，不要字幕')
        original=copy.deepcopy(s);provider=Mock()
        provider._json.return_value={'schema_version':'1.0','overrides':{
            'narration':{'value':'new','quote':'加中文配音'},'music':{'value':True,'quote':'音乐'},
            'text':{'value':False,'quote':'不要字幕'}},'unsupported':[]}
        out=apply_rules(s,resolve_rules(provider,s))
        self.assertEqual(s,original)
        self.assertEqual(out['narration_mode'],'new_native')
        self.assertTrue(out['formal_ad_policy']['music_required'])
        self.assertFalse(out['constraints']['subtitles'])

    def test_explicit_chinese_requests_survive_missing_or_inverted_model_switches(self):
        instruction = '生成中文口播添加中文文案，文字字体使用红色，添加对应的贴纸和字体动效还有转场'
        empty = {'schema_version': '1.0', 'overrides': {}, 'unsupported': []}
        spec = self.spec(instruction)
        # Empty/saved resolutions must both retain the literal requests.
        for resolution in (empty, {'schema_version': '1.0', 'overrides': {
                'narration': {'value': 'none', 'quote': '生成中文口播'},
                'text': {'value': False, 'quote': '添加中文文案'},
                'sticker': {'value': False, 'quote': '添加对应的贴纸'}}, 'unsupported': []}):
            with self.subTest(resolution=resolution):
                updated = apply_rules(spec, resolution)
                self.assertEqual(updated['narration_mode'], 'new_native')
                self.assertTrue(updated['constraints']['subtitles'])
                self.assertTrue(updated['resolved_editing_options']['sticker'])
                self.assertFalse(updated['resolved_editing_options']['music'])
                self.assertFalse(updated['resolved_editing_options']['effect'])
                for entry in updated['explicit_editing_overrides'].values():
                    self.assertIn(entry['quote'], instruction)
        provider = Mock()
        provider._json.return_value = {'schema_version': '1.0'}
        resolved = resolve_rules(provider, spec)
        self.assertEqual(resolved['overrides']['narration']['value'], 'new')
        self.assertTrue(resolved['overrides']['text']['value'])
        self.assertTrue(resolved['overrides']['sticker']['value'])

    def test_explicit_caption_copy_quote_is_not_mistaken_for_an_unrelated_request(self):
        spec = self.spec('添加中文文案')
        provider = Mock()
        provider._json.return_value = {'schema_version': '1.0', 'overrides': {
            'text': {'value': True, 'quote': '添加中文文案'}}, 'unsupported': []}
        resolved = resolve_rules(provider, spec)
        self.assertNotIn('ignored_overrides', resolved)
        self.assertTrue(apply_rules(spec, resolved)['constraints']['subtitles'])

    def test_literal_exclusions_win_over_positive_model_and_fallback_requests(self):
        empty = {'schema_version': '1.0', 'overrides': {}, 'unsupported': []}
        for instruction in ('不要新增中文口播、字幕和贴纸', '不用再生成中文口播，不加字幕或贴纸',
                            '口播不要，字幕不要，贴纸关闭', '只加背景音乐',
                            '添加中文口播和贴纸，不要配音、贴纸和字幕'):
            with self.subTest(instruction=instruction):
                spec = self.spec(instruction, 'crossborder_montage', '日本')
                wrong = {'schema_version': '1.0', 'overrides': {
                    key: {'value': 'new' if key == 'narration' else True, 'quote': instruction}
                    for key in ('narration', 'text', 'sticker')}, 'unsupported': []}
                for resolution in (empty, wrong):
                    updated = apply_rules(spec, resolution)
                    self.assertEqual(updated['narration_mode'], 'text_only')
                    self.assertFalse(updated['constraints']['subtitles'])
                    self.assertFalse(updated['resolved_editing_options']['sticker'])

    def test_reference_script_and_motion_exclusions_do_not_add_unrequested_elements(self):
        for instruction in ('分析中文口播和字幕', '参考原片的贴纸和口播', '使用原有的中文口播',
                            '生成口播文案', '不要字幕动画', '字幕保持静止',
                            '不要生成自然中文口播，不要添加可爱的贴纸'):
            with self.subTest(instruction=instruction):
                spec = apply_rules(self.spec(instruction),
                                   {'schema_version': '1.0', 'overrides': {}, 'unsupported': []})
                self.assertEqual(spec['narration_mode'], 'text_only')
                self.assertFalse(spec['constraints']['subtitles'])
                self.assertFalse(spec['resolved_editing_options']['sticker'])
        spec = apply_rules(self.spec('生成自然中文口播，声音不要太机械，添加中文文案和贴纸'),
                           {'schema_version': '1.0', 'overrides': {}, 'unsupported': []})
        self.assertEqual(spec['narration_mode'], 'new_native')
        self.assertTrue(spec['constraints']['subtitles'])
        self.assertTrue(spec['resolved_editing_options']['sticker'])

    def test_invented_quotes_and_unsupported_voice_do_not_silently_fall_back(self):
        s=self.spec('不要音乐')
        with self.assertRaisesRegex(ValueError,'原文'):
            apply_rules(s,{'schema_version':'1.0','overrides':{'music':{'value':True,'quote':'加音乐'}},'unsupported':[]})
        s=self.spec('添加日语配音','crossborder_montage','日本')
        result=apply_rules(s,{'schema_version':'1.0','overrides':{'narration':{'value':'new','quote':'添加日语配音'}},'unsupported':[]})
        self.assertEqual(result['narration_locale'],'ja-JP')
        with self.assertRaisesRegex(ValueError,'不能满足'):
            apply_rules(s,{'schema_version':'1.0','overrides':{},'unsupported':['未支持的剪映专属特效']})

    def test_disabled_audio_does_not_load_even_configured_resources(self):
        s=self.spec('不添加声音')
        with tempfile.TemporaryDirectory() as tmp, patch('agent_core.direct_resources._cache_audio') as cache:
            result=build_direct_media_plan(s,{'duration_us':3000000,'source_map':[]},{},Path(tmp),
                {'direct_media':{'music':'missing.wav','sfx':'missing.wav','narration':'missing.wav'}})
            cache.assert_not_called()
            self.assertFalse(result['tracks']);self.assertFalse(result['pending'])

    def test_custom_single_shot_compiles_without_ads_subtitles_or_packaging(self):
        f=fixture_module.CommerceTests();f.setUp()
        s=configure_rules({**copy.deepcopy(f.task),'workflow':'custom','render_backend':'direct_ffmpeg'})
        s['allow_single_source']=True
        plan=copy.deepcopy(f.plan);plan['clips']=plan['clips'][:1]
        timeline,_,_=compile_ad(s,f.assets,f.sources,plan)
        self.assertFalse(timeline['captions']);self.assertNotIn('narration_cues',timeline)
        snapshot=snapshot_from_timeline(s,f.assets,timeline)
        self.assertEqual(len(snapshot['tracks']),1)
        self.assertNotIn('transition',snapshot['tracks'][0]['segments'][0])
        self.assertNotIn('clip',snapshot['tracks'][0]['segments'][0])
        provider=Mock();plan_ad(provider,s,f.sources)
        self.assertIn('自定义视频剪辑师',provider._json.call_args.args[0])

    def test_custom_prompt_contract_is_accepted_by_real_task_store(self):
        f=service_fixture.TaskServiceTests();f.setUp()
        try:
            source=f.root/'source.mp4';source.write_bytes(b'isolated-test')
            aid=f.service.register_asset(source)
            spec=Workbench._prepare_spec(None,{'instruction':'简单剪辑，不加字幕或配音',
                                              'workflow':'custom','asset_ids':[aid]})
            job=f.service.create_job(spec)
            stored=f.service.get_job(job)['task_spec']
            self.assertEqual(stored['narration_mode'],'text_only')
            self.assertFalse(stored['constraints']['subtitles'])
            self.assertEqual(stored['editing_rules_version'],'prompt_first_v1')
        finally:f.tearDown()


if __name__=='__main__':unittest.main()
