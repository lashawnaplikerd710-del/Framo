import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_core.commerce import (compile_ad, validate_ad_script, rewrite_direct_voiceover,
                                 NarrationPlanIssue, ScriptRevisionPending)
from agent_core.task_service import TaskService
from agent_core.task_store import TaskStore
from tests import test_market_rules


class BatchScriptRepairTests(unittest.TestCase):
    def fixture(self):
        fixture, task, plan = test_market_rules.MarketRuleTests().fixture('日本')
        timeline, _, _ = compile_ad(task, fixture.assets, fixture.sources, plan)
        timeline['source_map'][0]['timeline_end_us'] = 1_933_333
        plan['clips'][0]['voiceover'] = '猫ちゃんがゆっくりくつろげるこちらの小さなお部屋をご覧ください。'
        return fixture, task, plan, timeline

    def test_short_shot_no_longer_rejects_sentence_when_output_has_room(self):
        fixture, task, plan, timeline = self.fixture()
        validate_ad_script(task, plan, timeline)

    def test_whole_output_overflow_is_repaired_with_original_indices(self):
        fixture, task, plan, timeline = self.fixture()
        plan['clips'][0]['voiceover'] = 'こちらのお部屋をご覧ください。' * 12
        with self.assertRaisesRegex(NarrationPlanIssue, '整条成品'):
            validate_ad_script(task, plan, timeline)
        provider = Mock()
        response = {'schema_version':'1.0', 'clips':[
            {'clip_index':i+1, 'voiceover': ('見て、この寝顔。' if i==0 else c['voiceover'])}
            for i,c in enumerate(plan['clips'])]}
        provider._json.side_effect = [
            {'schema_version':'1.0', 'clips':[{'clip_index':1,'voiceover':'見て。'}]}, response]
        revised, report = rewrite_direct_voiceover(provider, task, fixture.sources, plan,
            timeline, NarrationPlanIssue('整条成品口播总量过长', timeline))
        validate_ad_script(task, revised, timeline)
        self.assertEqual(provider._json.call_count, 2)
        self.assertEqual(len(report['responses']), 2)
        self.assertEqual(provider._json.call_args.args[1]['required_clip_indices'], [1,2,3,4])
        for old, new in zip(plan['clips'], revised['clips']):
            self.assertEqual({k:v for k,v in old.items() if k!='voiceover'},
                             {k:v for k,v in new.items() if k!='voiceover'})

    def test_exhausted_repairs_preserve_checkpoint_and_resume_without_full_replan(self):
        fixture, task, plan, timeline = self.fixture()
        task['max_revision_rounds'] = 2
        with tempfile.TemporaryDirectory() as tmp:
            service = TaskService.__new__(TaskService); service.store = TaskStore(tmp)
            job = service.store.put_job(task)
            with patch('agent_core.commerce.plan_ad', return_value=plan) as planner, \
                 patch('agent_core.commerce.compile_ad', side_effect=NarrationPlanIssue('口播过长', timeline)), \
                 patch('agent_core.commerce.rewrite_direct_voiceover', return_value=(plan, {})) as repair:
                with self.assertRaises(ScriptRevisionPending):
                    service._plan_direct_ad(job, task, fixture.assets, fixture.sources, Mock())
                self.assertEqual(planner.call_count, 1)
                self.assertEqual(repair.call_count, 2)
            artifact = service.store.list_artifacts(job)[0]
            checkpoint = Path(artifact['path']); before = checkpoint.read_bytes()
            self.assertFalse(json.loads(before)['approved'])
            self.assertFalse((service.store.job_dir(job)/'revisions/001/ad-plan.json').exists())
            accepted = {'decision':'accept','issues':[]}
            with patch('agent_core.commerce.plan_ad') as planner, \
                 patch('agent_core.commerce.compile_ad', return_value=(timeline, [], {})), \
                 patch('agent_core.commerce.review_direct_script', return_value=accepted):
                result = service._plan_direct_ad(job, task, fixture.assets, fixture.sources, Mock())
                planner.assert_not_called()
                self.assertEqual(result[0], plan)
            self.assertEqual(checkpoint.read_bytes(), before)

    def test_pending_checkpoint_hash_is_enforced_on_resume(self):
        fixture, task, plan, timeline = self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            service = TaskService.__new__(TaskService); service.store = TaskStore(tmp)
            job = service.store.put_job(task)
            path = service.store.job_dir(job)/'revisions/001/script-pending/example.json'
            path.parent.mkdir(parents=True); path.write_text(json.dumps({'plan':plan}), encoding='utf-8')
            service.store.put_artifact(job,'ad_script_pending','example',path)
            path.write_text('{}',encoding='utf-8')
            with patch('agent_core.commerce.plan_ad') as planner:
                with self.assertRaisesRegex(ValueError,'changed'):
                    service._plan_direct_ad(job,task,fixture.assets,fixture.sources,Mock())
                planner.assert_not_called()


if __name__ == '__main__': unittest.main()
