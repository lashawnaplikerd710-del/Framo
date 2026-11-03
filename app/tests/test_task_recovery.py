import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from agent_core.task_service import TaskService
from agent_core.task_store import TaskStore, atomic_json, digest
from agent_core.workbench import Workbench
from agent_core import task_recovery
from agent_core.commerce import ScriptRevisionPending
from tests import test_ad_audio as audio


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fixture, self.task, self.plan = audio.AdAudioTests().fixture()
        self.plan['narration_script'] = 'Give your pet a cozy little corner. Open the curtains, and let them settle in. Take a closer look.'
        self.plan['clips'][3]['heading'] = '商品详情'
        self.service = TaskService.__new__(TaskService)
        self.service.store = TaskStore(self.root/'tasks')
        self.service.provider_factory = Mock(return_value=Mock())
        self.service._runtime_binding = Mock(return_value={'schema_version':'1.0', 'model':'same',
                                                        'core_sha256':{'task_service.py':'new'}})
        for i, asset in enumerate(self.fixture.assets):
            path = self.root/f'source{i}.mp4';path.write_bytes(b'isolated source')
            asset.update(path=str(path), sha256=digest(path))
            self.service.store.put_asset(asset)
        self.job = self.service.store.put_job(self.task)
        self.path = self.service.store.job_dir(self.job)/'revisions/001/script-pending/original.json'
        atomic_json(self.path, {'plan':self.plan, 'approved':False, 'reason':'第4处屏幕文案未使用英语，请仅修复heading目标语言'})
        self.original = self.path.read_bytes()
        self.record = self.service.store.put_artifact(self.job, 'ad_script_pending', 'original', self.path)
        self.service.store.set_state(self.job, 'WAITING_FOR_INPUT', error='口播待修订：第4处屏幕文案未使用英语')

    def tearDown(self):
        self.temp.cleanup()

    def correction(self):
        data = task_recovery.diagnose(self.service, self.job)
        data['headings'][3]['heading'] = 'Take a closer look'
        return data

    def save(self, data=None):
        data = data or self.correction()
        return task_recovery.save_correction(self.service, self.job, data['checkpoint_id'],
                                             data['headings'], data['narration_script'])

    def test_diagnostics_shows_actual_bad_heading_and_usable_next_action(self):
        data = task_recovery.diagnose(self.service, self.job)
        self.assertTrue(data['editable'])
        self.assertEqual([r['clip_index'] for r in data['headings'] if r['problem']], [4])
        self.assertEqual(data['headings'][3]['heading'], '商品详情')
        self.assertIn('尚未生成', data['video_status'])
        self.assertIn('修复字幕语言', data['advice'])
        self.assertEqual(data['narration_script'], self.plan['narration_script'])

    def test_ai_rewrite_uses_unsaved_draft_but_keeps_checkpoint_unapproved_and_stopped(self):
        self.service.config = {'provider':{}}
        self.service.executor_factory = Mock()
        self.service._prepare_direct_ad_sources = Mock(return_value=self.fixture.sources)
        provider = self.service.provider_factory.return_value
        provider.usage = {'model_calls':1}
        candidate = copy.deepcopy(self.plan)
        candidate['clips'][3]['heading'] = 'See the details'
        candidate['narration_script'] = 'A pet house with an open doorway.'
        draft = self.correction()
        draft['narration_script'] = 'My current unsaved revision.'
        with patch('agent_core.model_recovery.bind_recovery', side_effect=lambda p,root:p), \
             patch('agent_core.commerce.rewrite_direct_voiceover',return_value=(candidate,{'max_speech_units':12})) as rewrite:
            result = task_recovery.suggest_script_rewrite(self.service,self.job,self.record['artifact_id'],draft)
        self.assertEqual(rewrite.call_args.args[3]['narration_script'],draft['narration_script'])
        self.assertFalse(result['approved'])
        self.assertLessEqual(result['target_units'],12)
        self.assertEqual(self.service.get_job(self.job)['state'],'WAITING_FOR_INPUT')
        self.assertEqual(task_recovery.latest_plan(self.service,self.job)[0]['artifact']['artifact_id'],self.record['artifact_id'])
        self.assertEqual(self.path.read_bytes(),self.original)

    def test_saved_revision_preserves_evidence_and_is_used_on_resume(self):
        result = self.save()
        self.assertEqual(self.path.read_bytes(), self.original)
        latest = self.service._narration_checkpoint(self.job)
        self.assertTrue(latest['user_edited']);self.assertFalse(latest['approved'])
        for old, new in zip(self.plan['clips'], latest['plan']['clips']):
            self.assertEqual({k:v for k,v in old.items() if k!='heading'},
                             {k:v for k,v in new.items() if k!='heading'})
        with patch('agent_core.commerce.plan_ad') as planner:
            plan, timeline, _, _ = self.service._plan_direct_ad(self.job, self.task,
                self.fixture.assets, self.fixture.sources, Mock())
            planner.assert_not_called()
        self.assertEqual(plan['clips'][3]['heading'], 'Take a closer look')
        events = []
        self.service._run_direct_delivery = Mock(side_effect=lambda *args: events.append('render') or {'done':True})
        with patch('agent_core.commerce.review_direct_script', side_effect=lambda *args:
                   events.append('review') or {'decision':'accept', 'issues':[]}):
            delivered = self.service._deliver_direct_ad(self.job, self.task, self.fixture.assets,
                self.fixture.sources, timeline, plan, Mock())
        self.assertEqual(delivered, {'done':True})
        self.assertEqual(events, ['review', 'render'])
        self.assertTrue(self.service._narration_checkpoint(self.job)['approved'])

    def test_rejected_manual_copy_cannot_reach_render(self):
        self.save()
        plan, timeline, _, _ = self.service._plan_direct_ad(self.job, self.task,
                self.fixture.assets, self.fixture.sources, Mock())
        self.service._run_direct_delivery = Mock()
        with patch('agent_core.commerce.review_direct_script', return_value={
                'decision':'revise', 'issues':['结尾文案需要调整']}):
            with self.assertRaisesRegex(ScriptRevisionPending, '结尾文案'):
                self.service._deliver_direct_ad(self.job, self.task, self.fixture.assets,
                                               self.fixture.sources, timeline, plan, Mock())
        self.service._run_direct_delivery.assert_not_called()
        self.assertFalse(self.service._narration_checkpoint(self.job)['approved'])

    def test_continue_after_saving_does_not_require_an_extra_edit(self):
        self.save()
        data = task_recovery.diagnose(self.service, self.job)
        result = task_recovery.save_correction(self.service, self.job, data['checkpoint_id'],
                                               data['headings'], data['narration_script'])
        self.assertEqual(result['checkpoint_id'], data['checkpoint_id'])
        self.assertFalse(self.service._narration_checkpoint(self.job)['approved'])

    def test_stale_running_bad_language_and_changed_source_are_rejected(self):
        data = task_recovery.diagnose(self.service, self.job)
        with self.assertRaisesRegex(ValueError, '仍未使用目标语言'):
            self.save(data)
        fixed = self.correction()
        self.service.store.set_state(self.job, 'PLANNING')
        with self.assertRaisesRegex(ValueError, '正在执行'):
            self.save(fixed)
        self.service.store.set_state(self.job, 'WAITING_FOR_INPUT')
        source = Path(self.fixture.assets[0]['path']);source.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'source changed'):
            self.save(fixed)
        source.write_bytes(b'isolated source')
        self.save(fixed)
        with self.assertRaisesRegex(ValueError, '检查点已更新'):
            self.save(fixed)

    def test_corrupt_checkpoint_still_has_diagnostics_and_cannot_be_saved(self):
        correction = self.correction()
        self.path.write_text('{}', encoding='utf-8')
        data = task_recovery.diagnose(self.service, self.job)
        self.assertFalse(data['editable'])
        self.assertIn('文件校验', data['advice'])
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.save(correction)

    def test_language_suggestion_changes_only_requested_headings(self):
        provider = self.service.provider_factory.return_value
        provider._json.return_value = {'caption_fixes':[{'clip_index':4, 'heading':'See details'}],
                                       'narration_script':'This unexpected script is ignored.'}
        result = task_recovery.suggest_language_fix(self.service, self.job, self.record['artifact_id'])
        self.assertEqual(result['caption_fixes'], [{'clip_index':4, 'heading':'See details'}])
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(task_recovery.latest_plan(self.service, self.job)[0]['plan'], self.plan)
        provider._json.return_value = {'caption_fixes':[{'clip_index':1, 'heading':'Wrong row'}]}
        with self.assertRaisesRegex(ValueError, '修复建议仍未满足'):
            task_recovery.suggest_language_fix(self.service, self.job, self.record['artifact_id'])

    def test_workbench_can_save_then_queue_without_overwriting_old_checkpoint(self):
        app = Workbench.__new__(Workbench)
        app.service = self.service;app.mutex=threading.RLock();app.live_worker=Mock(return_value=False)
        data = app.action({'action':'diagnose', 'job_id':self.job})
        data['headings'][3]['heading'] = 'See details'
        out = app.action({'action':'save_script_repair', 'job_id':self.job,
            'checkpoint_id':data['checkpoint_id'], 'headings':data['headings'],
            'narration_script':data['narration_script'], 'continue':True})
        self.assertTrue(out['queued'])
        self.assertEqual(self.service.get_job(self.job)['state'], 'CREATED')
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_current_core_repair_can_recheck_old_script_but_model_changes_require_new_job(self):
        task = {**self.task, 'runtime_binding':{'schema_version':'1.0', 'model':'same',
                                             'core_sha256':{'task_service.py':'old'}}}
        with self.service.store._connect() as db:
            db.execute('UPDATE jobs SET spec_json=? WHERE job_id=?', (json.dumps(task), self.job))
        data = self.correction()
        self.assertTrue(data['editable'])
        self.save(data)
        self.assertEqual(self.service.get_job(self.job)['task_spec']['runtime_binding'], self.service._runtime_binding())
        self.service._runtime_binding.return_value['model'] = 'changed'
        data = task_recovery.diagnose(self.service, self.job)
        self.assertFalse(data['editable'])
        self.assertIn('按原要求新建', data['advice'])

    def legacy_job(self):
        task = {**self.task, 'workflow':'custom', 'editing_rules_version':'prompt_first_v1',
                'instruction':'生成英语口播，添加英文文案', 'narration_mode':'text_only',
                'separate_narration':False, 'resolved_editing_options':{'narration':'none'}}
        task.pop('ad_narration_policy', None)
        plan = copy.deepcopy(self.plan)
        plan['voiceover'] = plan.pop('narration_script')
        for clip in plan['clips']:
            clip['voiceover'] = 'Do not prefer this old per-shot draft.'
        plan['clips'][3]['heading'] = 'Take a closer look'
        job = self.service.store.put_job(task)
        path = self.service.store.job_dir(job)/'auto-narration/pending.json'
        atomic_json(path, {'plan':plan, 'measurement':{'measured_us':13_810_000, 'available_us':9_210_000},
                           'target_units':21, 'approved':False, 'reason':'口播精简后仍未通过内容检查'})
        self.service.store.put_artifact(job, 'ad_auto_pending', 'legacy', path)
        self.service.store.set_state(job, 'WAITING_FOR_INPUT', error='口播精简后仍未通过内容检查')
        return job, task, plan, path

    def test_legacy_generated_paragraph_is_visible_editable_and_saved_as_unapproved_revision(self):
        job, task, plan, path = self.legacy_job()
        original = path.read_bytes()
        data = task_recovery.diagnose(self.service, job)
        self.assertTrue(data['editable'])
        self.assertTrue(data['legacy_narration_recovered'])
        self.assertEqual(data['narration_script'], plan['voiceover'])
        self.assertGreater(data['speech_units'], 0)
        self.assertEqual(self.service.get_job(job)['task_spec'], task)  # diagnosis is read-only
        task_recovery.save_correction(self.service, job, data['checkpoint_id'],
                                      data['headings'], data['narration_script'])
        saved = self.service.get_job(job)['task_spec']
        self.assertEqual(saved['narration_mode'], 'new_native')
        self.assertEqual(saved['ad_narration_policy'], 'whole_ad_v1')
        self.assertEqual(saved['resolved_editing_options']['narration'], 'new')
        self.assertEqual(path.read_bytes(), original)
        checkpoint = self.service._narration_checkpoint(job)
        self.assertTrue(checkpoint['user_edited'])
        self.assertFalse(checkpoint['approved'])
        self.assertEqual(checkpoint['plan']['narration_script'], plan['voiceover'])
        self.assertEqual(checkpoint['plan']['clips'], plan['clips'])

    def test_legacy_compatibility_requires_direct_request_and_generated_checkpoint(self):
        _, task, plan, _ = self.legacy_job()
        for kind in ('ad_plan', None, 'unknown'):
            self.assertIsNone(task_recovery.compatible_task(task, plan, checkpoint_kind=kind))
        for instruction in ('只添加音乐', '不要生成口播', '保留原声，再生成字幕', '不需要重新配音'):
            self.assertIsNone(task_recovery.compatible_task({**task, 'instruction':instruction}, plan,
                                                           checkpoint_kind='ad_auto_pending'))
        self.assertIsNone(task_recovery.compatible_task(task, {'clips':[{'heading':'Not narration'}]},
                                                       checkpoint_kind='ad_auto_pending'))
        self.assertIsNone(task_recovery.compatible_task({**task, 'render_backend':'native'}, plan,
                                                       checkpoint_kind='ad_auto_pending'))

    def test_legacy_repair_keeps_integrity_and_tool_model_binding_guards(self):
        job, task, _, path = self.legacy_job()
        binding = self.service._runtime_binding()
        task = {**task, 'runtime_binding':{**binding, 'model':'other'}}
        with self.service.store._connect() as db:
            db.execute('UPDATE jobs SET spec_json=? WHERE job_id=?', (json.dumps(task), job))
        data = task_recovery.diagnose(self.service, job)
        self.assertFalse(data['editable'])
        self.assertIn('模型、工具或素材库版本已变化', data['advice'])
        with self.assertRaisesRegex(ValueError, '模型、工具'):
            task_recovery.save_correction(self.service, job, data['checkpoint_id'],
                                          data['headings'], data['narration_script'])
        path.write_text('{}', encoding='utf-8')
        self.assertFalse(task_recovery.diagnose(self.service, job)['editable'])

    def test_readonly_length_failure_does_not_instruct_unavailable_save_action(self):
        job, task, _, _ = self.legacy_job()
        task['instruction'] = '只添加音乐'
        with self.service.store._connect() as db:
            db.execute('UPDATE jobs SET spec_json=? WHERE job_id=?', (json.dumps(task), job))
        data = task_recovery.diagnose(self.service, job)
        self.assertFalse(data['editable'])
        self.assertIn('当前只读', data['advice'])
        self.assertNotIn('再保存并继续', data['advice'])


if __name__ == '__main__': unittest.main()
