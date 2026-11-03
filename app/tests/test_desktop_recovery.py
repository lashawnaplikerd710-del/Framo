import unittest
from unittest.mock import patch

from agent_core.desktop_recovery import TaskRepairDialog
from tests import test_desktop_workbench as baseline


def diagnostic_fixture():
    return {'job_id':'job_'+'a'*32, 'reason':'第4处屏幕文案未使用英语，请修复目标语言',
        'advice':'修改标记字幕或点击修复字幕语言，保存后重新检查。', 'resume_count':3,
        'locale':'en-US', 'editable':True, 'checkpoint_id':'artifact_'+'b'*32,
        'headings':[{'clip_index':i, 'heading':'商品详情' if i==4 else f'Caption {i}', 'problem':i==4} for i in range(1,5)],
        'narration_script':'Give your pet a little corner. Take a closer look.', 'speech_units':11,
        'source_task':{'workflow':'crossborder_montage', 'target_country':'美国', 'instruction':'黄色字幕，不要底板'},
        'assets':[]}


class DesktopRecoveryTests(unittest.TestCase):
    setUp = baseline.DesktopInteractionTests.setUp
    tearDown = baseline.DesktopInteractionTests.tearDown
    settle = baseline.DesktopInteractionTests.settle

    def open(self):
        self.root.deiconify();self.root.geometry('1120x740');self.ui.show('tasks')
        job = baseline.fixture_job(state='WAITING_FOR_INPUT', render_backend='direct_ffmpeg',
                                   publication=None, error='第4处屏幕文案未使用英语')
        self.snapshot['jobs']=[job];self.ui.render_status(self.snapshot)
        self.ui.task_tree.selection_set(job['job_id']);self.ui.draw_job();self.root.update()
        self.backend.action.return_value=diagnostic_fixture()
        self.ui.action_buttons['diagnose'].invoke();self.settle();self.root.update()
        return self.ui.task_repair_dialog

    def test_button_opens_specific_copy_and_save_continue_dispatches_user_changes(self):
        dialog = self.open()
        self.assertEqual(self.ui.action_buttons['diagnose'].winfo_manager(), 'pack')
        self.assertIn('查看并修复', self.ui.task_problem_var.get())
        self.assertEqual(dialog.current,4)
        self.assertEqual(dialog.heading.get('1.0','end-1c'),'商品详情')
        dialog.heading.delete('1.0','end');dialog.heading.insert('1.0','See details')
        self.backend.action.return_value={'saved':True,'queued':True}
        dialog.continue_button.invoke();self.settle()
        payload=self.backend.action.call_args.args[0]
        self.assertEqual(payload['action'],'save_script_repair')
        self.assertTrue(payload['continue'])
        self.assertEqual(payload['headings'][3]['heading'],'See details')
        self.assertFalse(dialog.window.winfo_exists())

    def test_failed_save_keeps_edits_and_reenables_controls(self):
        dialog=self.open()
        dialog.heading.delete('1.0','end');dialog.heading.insert('1.0','Still editing')
        self.backend.action.side_effect=ValueError('第4处字幕仍需修改')
        dialog.save_button.invoke();self.settle()
        self.assertIn('仍需修改',dialog.message.get())
        self.assertEqual(dialog.heading.get('1.0','end-1c'),'Still editing')
        self.assertFalse(dialog.save_button.instate(['disabled']))
        dialog.close()

    def test_translation_is_only_a_draft_and_generic_errors_have_fallback(self):
        dialog=self.open()
        self.backend.action.return_value={'caption_fixes':[{'clip_index':4,'heading':'See details'}]}
        dialog.translate_button.invoke();self.settle()
        self.assertEqual(dialog.heading.get('1.0','end-1c'),'See details')
        self.assertIn('建议已填入',dialog.message.get())
        self.assertEqual(self.backend.action.call_count,2)
        dialog.close()
        data=diagnostic_fixture();data.update(editable=False,reason='模型版本变化，请新建')
        dialog=TaskRepairDialog(self.ui,data);self.root.update()
        self.assertTrue(dialog.save_button.instate(['disabled']))
        self.assertTrue(dialog.translate_button.instate(['disabled']))
        dialog.new_button.invoke()
        self.assertEqual(self.ui.current_view,'create')
        self.assertEqual(self.ui.workflow.get(),'跨境电商广告 · 美国区')
        self.assertEqual(self.ui.instruction.get('1.0','end-1c'),'黄色字幕，不要底板')
        self.backend.create_job.assert_not_called()

    def test_recovered_narration_opens_editable_and_remains_editable_after_tab_switch(self):
        data = diagnostic_fixture()
        data.update(reason='口播自动精简仍需修订', legacy_narration_recovered=True)
        for row in data['headings']:
            row['problem'] = False
        dialog = TaskRepairDialog(self.ui, data);self.root.update()
        self.assertEqual(dialog.tabs.index('current'), 1)
        self.assertEqual(dialog.narration.cget('state'), 'normal')
        self.assertEqual(dialog.narration.get('1.0','end-1c'), data['narration_script'])
        dialog.tabs.select(0);self.root.update()
        dialog.tabs.select(1);self.root.update()
        dialog.narration.delete('1.0','end');dialog.narration.insert('1.0','A revised complete paragraph.')
        self.assertEqual(dialog.narration.get('1.0','end-1c'), 'A revised complete paragraph.')
        self.assertFalse(dialog.save_button.instate(['disabled']))
        dialog.close()

    def test_readonly_explanation_is_visible_and_reload_can_enable_narration(self):
        data = diagnostic_fixture()
        data.update(editable=False, advice='模型、工具或素材库已变化，当前只读。')
        dialog = TaskRepairDialog(self.ui, data);self.root.update()
        self.assertEqual(dialog.narration.cget('state'), 'disabled')
        self.assertIn('当前只读', dialog.message.get())
        recovered = diagnostic_fixture()
        recovered.update(legacy_narration_recovered=True)
        dialog.load(recovered);self.root.update()
        self.assertEqual(dialog.narration.cget('state'), 'normal')
        self.assertFalse(dialog.continue_button.instate(['disabled']))
        self.assertNotIn('当前只读', dialog.message.get())
        dialog.close()


if __name__ == '__main__': unittest.main()
