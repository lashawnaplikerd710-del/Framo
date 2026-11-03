import copy
import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import Mock, patch

from agent_core import narration_timing as timing
from agent_core.commerce import ScriptRevisionPending, compile_ad, NarrationPlanIssue
from agent_core.direct_resources import build_direct_media_plan
from agent_core.task_service import TaskService
from agent_core.task_store import TaskStore
from tests import test_market_rules, test_narration_timing


class AutoNarrationTests(unittest.TestCase):
    def fixture(self, raw):
        f, task, plan = test_market_rules.MarketRuleTests().fixture('日本')
        timeline, _, _ = compile_ad(task, f.assets, f.sources, plan)
        timeline['ad_content_review'] = {'decision':'accept','issues':[]}
        s=TaskService.__new__(TaskService);s.store=TaskStore(raw)
        job=s.store.put_job(task)
        return s, job, f, task, plan, timeline

    def test_conservative_initial_budget_keeps_natural_pause_room(self):
        for market in ('日本','美国'):
            t=test_market_rules.spec(market)
            planned,_=timing.budget(t,{'duration_us':15_000_000},4,planning=True)
            hard,_=timing.budget(t,{'duration_us':15_000_000},4)
            self.assertLess(planned,hard*.7)

    def test_measured_overflow_auto_repairs_and_completes_same_job(self):
        with tempfile.TemporaryDirectory() as raw:
            s,job,f,task,plan,timeline=self.fixture(raw)
            changed=copy.deepcopy(plan);changed['clips'][0]['voiceover']='見て、この小さなお部屋。'
            overflow={'available_us':15_500_000,'measured_us':20_000_000,'cue_durations_us':[5_000_000]*4}
            calls=[]
            def deliver(*args):
                calls.append(copy.deepcopy(args[4]))
                if len(calls)==1:raise timing.NarrationDurationIssue(overflow)
                s.store.set_state(job,'COMPLETED',message='isolated verified delivery')
                return s.store.get_job(job)
            with patch.object(s,'_run_direct_delivery',side_effect=deliver),\
                 patch('agent_core.commerce.rewrite_direct_voiceover',return_value=(changed,{})) as rewrite,\
                 patch('agent_core.commerce.review_direct_script',return_value={'decision':'accept','issues':[]}):
                result=s._deliver_direct_ad(job,task,f.assets,f.sources,timeline,plan,Mock())
            self.assertEqual(result['state'],'COMPLETED');self.assertEqual(len(calls),2)
            self.assertNotEqual(calls[0],calls[1])
            self.assertLess(rewrite.call_args.args[1]['_narration_repair_max_units'],sum(timing.units(task,c['voiceover']) for c in plan['clips']))
            self.assertFalse(any(e['type']=='waiting_input' for e in s.store.read_events(job)))
            saved=s.store.list_artifacts(job)
            self.assertTrue(any(a['kind']=='ad_auto_script' for a in saved))
            for before,after in zip(plan['clips'],changed['clips']):
                self.assertEqual({k:v for k,v in before.items() if k!='voiceover'},
                                 {k:v for k,v in after.items() if k!='voiceover'})
            with patch.object(s,'_run_direct_delivery',return_value={'state':'COMPLETED'}) as delivery,\
                 patch('agent_core.commerce.rewrite_direct_voiceover') as rewrite:
                s._deliver_direct_ad(job,task,f.assets,f.sources,timeline,plan,Mock())
                rewrite.assert_not_called()
                self.assertEqual(delivery.call_args.args[4],changed)

    def test_explicit_verbatim_script_is_not_silently_changed(self):
        with tempfile.TemporaryDirectory() as raw:
            s,job,f,task,plan,timeline=self.fixture(raw)
            task['instruction']='口播文案必须一字不改'
            with patch.object(s,'_run_direct_delivery',side_effect=timing.NarrationDurationIssue({})),\
                 patch('agent_core.commerce.rewrite_direct_voiceover') as rewrite:
                with self.assertRaisesRegex(ScriptRevisionPending,'原样'):
                    s._deliver_direct_ad(job,task,f.assets,f.sources,timeline,plan,Mock())
                rewrite.assert_not_called()

    def test_persistently_invalid_model_is_bounded_and_preserves_measurement(self):
        with tempfile.TemporaryDirectory() as raw:
            s,job,f,task,plan,timeline=self.fixture(raw)
            measurement={'available_us':15_000_000,'measured_us':25_000_000}
            with patch.object(s,'_run_direct_delivery',side_effect=timing.NarrationDurationIssue(measurement)),\
                 patch('agent_core.commerce.rewrite_direct_voiceover',side_effect=NarrationPlanIssue('模型未返回完整文案')) as rewrite:
                with self.assertRaisesRegex(ScriptRevisionPending,'自动精简'):
                    s._deliver_direct_ad(job,task,f.assets,f.sources,timeline,plan,Mock())
            self.assertEqual(rewrite.call_count,3)
            self.assertTrue(any(a['kind']=='narration_measurement' for a in s.store.list_artifacts(job)))

    def test_repaired_audio_never_overwrites_previous_sentence_cache(self):
        task,timeline=test_narration_timing.NarrationTimingTests().fixture()
        with tempfile.TemporaryDirectory() as raw:
            folder=Path(raw)
            def synth(text,output,**kwargs):
                with wave.open(str(output),'wb') as f:
                    f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000)
                    f.writeframes(b'\1\0'*16_000*(3 if '给毛' in text else 1))
            with patch('agent_core.direct_resources.synthesize_windows_sapi',side_effect=synth) as tts:
                first=build_direct_media_plan(task,timeline,{},folder,{})
                old={p:p.read_bytes() for p in folder.glob('direct-resources/narration/*.wav')}
                new=copy.deepcopy(timeline);new['narration_cues'][0]['text']='柔软的小窝。'
                second=build_direct_media_plan(task,new,{},folder,{})
            for p,data in old.items():self.assertEqual(p.read_bytes(),data)
            self.assertEqual(tts.call_count,3)  # Only the changed sentence was synthesized again.
            self.assertFalse(first['pending']);self.assertFalse(second['pending'])

    def test_overflow_report_contains_actual_pcm_measurements(self):
        task,timeline=test_narration_timing.NarrationTimingTests().fixture()
        with tempfile.TemporaryDirectory() as raw:
            def synth(text,output,**kwargs):
                with wave.open(str(output),'wb') as f:
                    f.setnchannels(1);f.setsampwidth(2);f.setframerate(16000);f.writeframes(b'\1\0'*48_000)
            with patch('agent_core.direct_resources.synthesize_windows_sapi',side_effect=synth):
                media=build_direct_media_plan(task,timeline,{},Path(raw),{})
            measured=media['report']['narration_overflow']
            self.assertEqual(measured['measured_us'],6_000_000)
            self.assertEqual(measured['available_us'],4_780_000)
            self.assertFalse(media['tracks'])


if __name__=='__main__':unittest.main()
