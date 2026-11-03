import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

from agent_core.task_service import TaskService,task_spec
from agent_core.workbench import Workbench
from agent_core.editing_rules import apply_rules
from agent_core.commerce import compile_ad,editing_units,plan_ad
from agent_core.video_limits import duration_from_instruction
from agent_core.timeline_compiler import PlanIssue


def fixture(duration_ms=600_000,count=20):
    assets=[{'asset_id':f'asset_{i:024x}','path':f'C:/{i}.mp4','width':320,'height':180,
        'video_duration_us':420_000_000,'duration_us':420_000_000,'audio_duration_us':420_000_000,
        'has_audio':True,'frame_rate_mode':'CFR','rotation':0} for i in range(count)]
    task=Workbench._prepare_spec(None,{'workflow':'long_video','instruction':'保留原声，不要字幕，按原顺序剪辑',
        'asset_ids':[a['asset_id'] for a in assets],'target_duration_ms':duration_ms})
    task=apply_rules(task,{'schema_version':'1.0','overrides':{},'unsupported':[]})
    sources=[];clips=[]
    for asset in assets:
        units,_=editing_units(task,asset,{},[{'requested_us':0}])
        sources.append({'asset_id':asset['asset_id'],'speech_mode':'visual','speech_units':units,
            'frames':[{'requested_us':0},{'requested_us':20_000_000}],'observations':[]})
        clips.append({'asset_id':asset['asset_id'],'first_unit':units[0]['unit_id'],
            'last_unit':units[2]['unit_id'],'frame_times_us':[0,20_000_000],
            'role':'chapter','reason':'素材内容','heading':'','palette':'cream','placement':'lower'})
    return task,assets,sources,{'schema_version':'1.0','title':'长视频测试','story':'按主题分章','clips':clips}


class LongVideoTests(unittest.TestCase):
    def test_minute_and_second_target_limits(self):
        for text in ('制作10分钟长视频','制作十分钟长视频','生成600秒成片'):
            self.assertEqual(duration_from_instruction(text),600000)
        self.assertEqual(duration_from_instruction('制作1分30秒成片'),90000)
        for text in ('制作601秒视频','制作11分钟成片'):
            with self.assertRaises(ValueError):duration_from_instruction(text)

    def test_long_profile_is_separate_and_reuses_prompt_first(self):
        task,*_=fixture()
        self.assertEqual(task['workflow'],'custom')
        self.assertEqual(task['creation_mode'],'long_video_v1')
        self.assertEqual(task['narration_mode'],'preserve_source')
        self.assertEqual(task['target']['duration_ms'],600000)
        self.assertFalse(task['constraints']['subtitles'])
        self.assertNotIn('creation_mode',task_spec('自定义视频',['a'],'custom'))

    def test_twenty_real_source_ranges_compile_to_ten_minutes(self):
        task,assets,sources,plan=fixture()
        timeline,_,_=compile_ad(task,assets,sources,plan)
        self.assertEqual(timeline['duration_us'],600000000)
        self.assertEqual(len(timeline['source_map']),20)
        self.assertEqual(len({m['asset_id'] for m in timeline['source_map']}),20)

    def test_overlap_and_output_cap_are_not_bypassed(self):
        task,assets,sources,plan=fixture()
        plan['clips'][-1]=copy.deepcopy(plan['clips'][0])
        with self.assertRaises(PlanIssue):compile_ad(task,assets,sources,plan)
        task,assets,sources,plan=fixture();task['target']['duration_ms']=None
        plan['clips'][0]['last_unit']='l0004'
        with self.assertRaisesRegex(PlanIssue,'10 分钟'):compile_ad(task,assets,sources,plan)

    def test_long_planner_receives_real_ranges_and_does_not_use_ad_rules(self):
        task,assets,sources,plan=fixture()
        provider=Mock();provider._json.return_value=plan
        self.assertEqual(plan_ad(provider,task,sources),plan)
        prompt,payload=provider._json.call_args.args
        self.assertIn('长视频剪辑师',prompt);self.assertIn('600秒',prompt)
        self.assertEqual(len(payload['sources']),20)

    def test_seven_minute_import_limit(self):
        service=TaskService.__new__(TaskService);service.inspector=Mock();service.store=Mock()
        service.inspector.inspect.return_value={'video_duration_us':420000000}
        service.register_asset('clip.mp4');service.store.put_asset.assert_called_once()
        service.inspector.inspect.return_value={'video_duration_us':420000001}
        with self.assertRaisesRegex(ValueError,'7 分钟'):service.register_asset('clip.mp4')

    def test_long_narration_requests_one_complete_chapter_script(self):
        from agent_core import ad_narration
        from agent_core.commerce import rewrite_direct_voiceover
        task,assets,sources,plan=fixture()
        task=ad_narration.configure({**task,'narration_mode':'new_native'})
        timeline,_,_=compile_ad({**task,'narration_mode':'preserve_source'},assets,sources,plan)
        provider=Mock();provider._json.return_value={'narration_script':'这是完整的中文讲解，按章节介绍画面中的内容。'}
        rewrite_direct_voiceover(provider,task,sources,plan,timeline,'需要完整旁白')
        prompt,payload=provider._json.call_args.args
        self.assertIn('长视频旁白编剧',prompt)
        self.assertIn('不返回alternatives',prompt)
        self.assertIn('target_speech_units',payload)

    def test_chunk_recognition_covers_full_seven_minutes(self):
        import numpy as np
        path=Path(__file__).resolve().parents[1]/'toolchain/scripts/local_asr.py'
        spec=importlib.util.spec_from_file_location('long_asr_under_test',path)
        module=importlib.util.module_from_spec(spec)
        import sys
        with patch.dict(sys.modules,{'common':Mock()}):spec.loader.exec_module(module)
        recognizer=Mock()
        recognizer.create_stream.side_effect=lambda:Mock(result=json.dumps({'tokens':['内容'], 'timestamps':[2.0]}))
        raw=module.recognize_chunks(recognizer,np.zeros(420*16000,dtype='float32'))
        self.assertEqual(len(raw['chunks']),14)
        self.assertEqual(raw['chunks'][-1]['end_s'],420)
        self.assertTrue(all(a<b for a,b in zip(raw['timestamps'],raw['timestamps'][1:])))


if __name__=='__main__':unittest.main()
