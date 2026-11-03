import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from agent_core.desktop_workbench import (DesktopWorkbench, allowed_actions,
    guard_runtime_change, wake_existing, default_instruction)
from agent_core.workbench import Workbench


def fixture_job(**changes):
    result = dict(job_id='job_'+'a'*32, state='READY_FOR_REVIEW', instruction='宠物碗带货混剪',
        created_at='2026-09-28T13:00:00+00:00', worker_running=False, cancel_requested=False,
        error=None, events=[], native_progress=None, review={'issues': [{'problem': '字幕和文案重叠'}]},
        publication={'registered_draft_path':'D:/drafts/test','full_quality_accepted':False})
    result.update(changes)
    return result


class DesktopInteractionTests(unittest.TestCase):
    """Exercise real Tk widgets and service dispatch without model calls or editing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.backend = Mock()
        self.backend.root = Path(self.tmp.name)
        self.backend.stop = threading.Event()
        self.backend.busy_action = None
        self.backend.safe_error.side_effect = lambda e: Workbench.safe_error(None, e)
        self.snapshot = {'settings': {'gateway_url':'https://work.example/v1'}, 'jobs': [],
                         'checks':[{'ok':True,'name':'运行环境','detail':'正常'}], 'limitations':['需检查音画'], 'state_dir': self.tmp.name}
        self.backend.status.side_effect = lambda: self.snapshot
        self.root = tk.Tk(); self.root.withdraw()
        self.ui = DesktopWorkbench(self.root, self.backend, poll=False)
        self.ui.render_status(self.snapshot)
        self.root.update_idletasks()

    def tearDown(self):
        if not self.ui.closed:
            self.ui.shutdown()
        self.tmp.cleanup()

    def settle(self):
        deadline = time.monotonic()+4
        while self.ui.busy or self.ui.refresh_pending or not self.ui.inbox.empty():
            self.root.update()
            if time.monotonic()>deadline:
                self.fail('Desktop service did not settle')
            time.sleep(.01)

    def test_progress_follows_latest_on_selection_and_refresh(self):
        self.root.deiconify()
        self.root.geometry('1366x768')
        self.ui.show('tasks')
        job = fixture_job(state='WAITING_FOR_INPUT', publication=None, review=None,
                          render_backend='direct_ffmpeg', events=[
            {'timestamp':'2026-10-13T14:00:00+00:00', 'stage':'PLANNING',
             'type':'progress', 'payload':{'message':f'进度记录 {i}'}} for i in range(100)])
        self.snapshot['jobs'] = [job]
        self.ui.render_status(self.snapshot)
        self.ui.task_tree.selection_set(job['job_id'])
        self.ui.draw_job(); self.root.update()
        self.assertGreater(self.ui.detail.yview()[1], .99)
        self.ui.detail.yview_moveto(.3)
        job['events'].append({'timestamp':'2026-10-13T14:01:00+00:00', 'stage':'PLANNING',
                             'type':'progress', 'payload':{'message':'最新记录'}})
        self.ui.draw_job(); self.root.update()
        self.assertGreater(self.ui.detail.yview()[1], .99)
        self.assertIsNotNone(self.ui.detail.dlineinfo(self.ui.detail.index('end-2l')))

    def test_runtime_mismatch_explains_new_task_without_altering_saved_error(self):
        error = 'Runtime differs from the saved task; do not resume with different tools'
        job = fixture_job(state='WAITING_FOR_INPUT', error=error, publication=None)
        self.snapshot['jobs'] = [job]
        self.ui.render_status(self.snapshot)
        self.ui.task_tree.selection_set(job['job_id']); self.ui.draw_job()
        self.assertIn('按原素材和要求新建任务', self.ui.task_problem_var.get())
        self.assertEqual(job['error'], error)
        self.assertNotIn(error, self.ui.detail.get('1.0', 'end'))
        self.assertIn('程序版本已更新', self.ui.detail.get('1.0', 'end'))

    def test_real_widgets_add_unicode_media_off_ui_thread_and_create_job(self):
        main_thread = threading.get_ident()
        def register(paths):
            self.assertNotEqual(threading.get_ident(), main_thread)
            self.assertEqual(paths, ['D:/素材/猫碗.mp4'])
            return [{'asset_id':'asset_a','name':'猫碗.mp4','duration_us':22800000,'width':1080,'height':1920}]
        self.backend.register_files.side_effect = register
        self.ui.add_files(['D:/素材/猫碗.mp4']); self.settle()
        self.assertEqual(self.ui.asset_tree.item('asset_a')['values'][0], '猫碗.mp4')
        self.ui.instruction.delete('1.0','end'); self.ui.instruction.insert('1.0','28 秒带货草稿')
        self.backend.create_job.return_value = {'job_id':'job_new'}
        self.ui.create_job(); self.settle()
        payload = self.backend.create_job.call_args.args[0]
        self.assertEqual(payload, {'workflow':'crossborder_montage','target_country':'日本','instruction':'28 秒带货草稿','asset_ids':['asset_a']})
        self.assertEqual(self.ui.current_view, 'tasks')
        self.assertFalse(self.ui.assets)

    def test_failed_create_keeps_materials_and_redacts_error(self):
        self.ui._added([{'asset_id':'asset_a','name':'猫碗.mp4','duration_us':22800000,'width':1080,'height':1920}])
        token = 'jyg_'+'x'*43
        self.backend.create_job.side_effect = ValueError('invalid '+token)
        self.ui.create_job(); self.settle()
        self.assertEqual(len(self.ui.assets), 1)
        self.assertNotIn(token, self.ui.notice_var.get())
        self.assertIn('隐藏', self.ui.notice_var.get())

    def test_historical_failure_reason_and_render_report_display_in_chinese(self):
        reason = 'No single bundled font covers this text and locale; revise symbols or add a font'
        job = fixture_job(state='FAILED', error=reason, publication=None,
                          direct_render_report={'status':'FAILED','error':reason})
        self.snapshot['jobs'] = [job]
        self.ui.render_status(self.snapshot)
        self.ui.task_tree.selection_set(job['job_id'])
        self.ui.draw_job()
        self.assertIn('字体', self.ui.task_problem_var.get())
        detail = self.ui.detail.get('1.0','end')
        self.assertNotIn('No single bundled', detail)
        self.assertIn('内置字体无法完整显示', detail)
        self.assertEqual(job['error'], reason)  # Historical evidence is untouched.

    def test_change_output_directory_uses_picker_and_dedicated_setting_without_moving_output(self):
        self.root.deiconify();self.ui.show('tasks')
        job = fixture_job(render_backend='direct_ffmpeg', state='COMPLETED', publication=None,
                          delivery={'output_file':'D:/old-output/old.mp4'})
        self.snapshot['jobs']=[job];self.snapshot['direct_output_dir']='D:/old-output'
        self.ui.render_status(self.snapshot)
        self.ui.task_tree.selection_set(job['job_id']);self.ui.draw_job();self.root.update()
        self.assertEqual(self.ui.action_buttons['change_output_dir'].winfo_manager(), 'grid')
        self.backend.save_output_directory.return_value={'saved':True,'output_dir':'D:/new-output'}
        with patch('agent_core.desktop_workbench.filedialog.askdirectory',return_value='D:/new-output') as chooser:
            self.ui.job_action('change_output_dir');self.settle()
        self.assertEqual(chooser.call_args.kwargs['initialdir'],'D:/old-output')
        self.backend.save_output_directory.assert_called_once_with('D:/new-output')
        self.backend.save_settings.assert_not_called();self.backend.action.assert_not_called()
        self.assertEqual(job['delivery']['output_file'],'D:/old-output/old.mp4')
        self.assertIn('已有任务和成品位置不变',self.ui.notice_var.get())
        self.backend.save_output_directory.reset_mock()
        with patch('agent_core.desktop_workbench.filedialog.askdirectory',return_value=''):
            self.ui.job_action('change_output_dir')
        self.backend.save_output_directory.assert_not_called()

    def test_voice_library_language_columns_and_selection_passed_to_task(self):
        self.root.deiconify(); self.root.update()
        root=Path(self.tmp.name)/'voices';root.mkdir()
        (root/'catalog.json').write_text(json.dumps({'schema_version':1,
            'defaults':{'ja-JP':'jp','en-US':'us'},'voices':[
                {'id':'jp','locale':'ja-JP','label':'日语 Alpha'},
                {'id':'jp2','locale':'ja-JP','label':'日语 Kumo'},
                {'id':'us','locale':'en-US','label':'英语 Heart'}]}),encoding='utf-8')
        self.backend.service.config={'direct_media':{'local_voice_library':str(root)}}
        self.ui.open_voice_library();self.root.update_idletasks()
        dialog=next(w for w in self.root.winfo_children() if isinstance(w,tk.Toplevel))
        dialog.transient(''); dialog.deiconify()
        def widgets(w):
            for child in w.winfo_children():
                yield child
                yield from widgets(child)
        all_widgets=list(widgets(dialog))
        trees=[w for w in all_widgets if isinstance(w, __import__('tkinter.ttk',fromlist=['Treeview']).Treeview)]
        self.assertEqual([tree.get_children() for tree in trees],[('jp','jp2'),('us',)])
        jp_tree,us_tree=trees
        with patch('winsound.PlaySound') as play:
            self.root.update()
            play.assert_not_called()
        self.assertEqual(jp_tree.selection(),('jp',))
        self.assertEqual(us_tree.selection(),())
        (root/'samples').mkdir()
        for voice_id in ('jp','jp2','us'):
            (root/'samples'/f'{voice_id}.wav').write_bytes(b'fixture')
        buttons={w.cget('text'):w for w in all_widgets if w.winfo_class()=='TButton'}
        self.assertEqual(set(buttons), {'停止'})
        def click(tree, row):
            x,y,w,h=tree.bbox(row, 'voice')
            tree.event_generate('<Button-1>', x=x+10, y=y+h//2)
            self.root.update()
        with patch('winsound.PlaySound') as play:
            click(jp_tree,'jp2')
            self.assertEqual(Path(play.call_args.args[0]),root/'samples/jp2.wav')
            self.assertEqual(play.call_count,2)  # stop previous, then async play
            self.assertEqual(jp_tree.set('jp','chosen'),'')
            self.assertEqual(jp_tree.set('jp2','chosen'),'✓')
            self.assertEqual(self.ui.selected_voice_ids['ja-JP'],'jp2')
            click(jp_tree,'jp2')  # same row can be replayed without a button
            self.assertEqual(play.call_count,4)
            click(us_tree,'us')
            self.assertEqual(jp_tree.selection(),())
            self.assertEqual(Path(play.call_args.args[0]),root/'samples/us.wav')
            self.assertEqual(self.ui.selected_voice_ids['en-US'],'us')
            self.assertEqual(jp_tree.set('jp2','chosen'),'✓')
            click(jp_tree,'jp')
            self.assertEqual(us_tree.selection(),())
            self.assertEqual(jp_tree.set('jp','chosen'),'✓')
            self.assertEqual(jp_tree.set('jp2','chosen'),'')
            self.assertTrue(dialog.winfo_exists())
            buttons['停止'].invoke(); play.assert_called_with(None,0)
            self.root.tk.call(dialog.protocol('WM_DELETE_WINDOW'))
            play.assert_called_with(None,0)
            self.ui.open_voice_library();self.root.update()
            play.assert_called_with(None,0)  # reopening is silent
            reopened=next(w for w in self.root.winfo_children() if isinstance(w,tk.Toplevel))
            new_trees=[w for w in widgets(reopened) if w.winfo_class()=='Treeview']
            self.assertEqual(new_trees[0].set('jp','chosen'),'✓')
            self.root.tk.call(reopened.protocol('WM_DELETE_WINDOW'))
        self.ui._added([{'asset_id':'asset_a','name':'猫碗.mp4','duration_us':22800000,'width':1080,'height':1920}])
        self.backend.create_job.return_value={'job_id':'job_new'}
        self.ui.create_job();self.settle()
        self.assertEqual(self.backend.create_job.call_args.args[0]['direct_media'],{'voice_id':'jp'})

    def test_voice_click_keeps_custom_choice_when_sample_unavailable(self):
        self.root.deiconify(); self.root.update()
        root=Path(self.tmp.name)/'voices';root.mkdir()
        (root/'catalog.json').write_text(json.dumps({'schema_version':1,
            'defaults':{'ja-JP':'jp'},'voices':[
                {'id':'jp','locale':'ja-JP','label':'日语 Alpha'}]}),encoding='utf-8')
        self.backend.service.config={'direct_media':{'local_voice_library':str(root)}}
        self.ui.workflow.set('自定义剪辑')
        self.ui.open_voice_library();self.root.update()
        dialog=next(w for w in self.root.winfo_children() if isinstance(w,tk.Toplevel))
        dialog.transient(''); dialog.deiconify(); self.root.update()
        def walk(w):
            for child in w.winfo_children():
                yield child
                yield from walk(child)
        tree=next(w for w in walk(dialog) if w.winfo_class()=='Treeview')
        x,y,w,h=tree.bbox('jp','voice')
        with patch('winsound.PlaySound') as play:
            tree.event_generate('<Button-1>',x=x+10,y=y+h//2);self.root.update()
            play.assert_called_once_with(None,0)
            self.assertEqual(self.ui.custom_voice_id,'jp')
            self.assertEqual(tree.set('jp','chosen'),'✓')
            self.assertIn('试听失败',self.ui.notice_var.get())
            self.assertTrue(dialog.winfo_exists())
            self.root.tk.call(dialog.protocol('WM_DELETE_WINDOW'))
        self.ui._added([{'asset_id':'asset_a','name':'猫碗.mp4','duration_us':22800000,'width':1080,'height':1920}])
        self.backend.create_job.return_value={'job_id':'job_new'}
        self.ui.create_job();self.settle()
        self.assertEqual(self.backend.create_job.call_args.args[0]['direct_media'],{'voice_id':'jp'})

    def test_export_poll_keeps_folder_edits_and_selected_retry(self):
        self.ui.export_batch.set('batch_test')
        self.ui.snapshot['external_exports'] = [{'batch_id':'batch_test','output_dir':'D:/old',
            'items':[{'output_id':'one','status':'FAILED','time_range_us':[0,1000000]}]}]
        self.ui.refresh_export_items()
        self.ui.export_output.set('D:/new')
        self.ui.export_tree.selection_set('one')
        self.ui.refresh_export_items()
        self.assertEqual(self.ui.export_output.get(), 'D:/new')
        self.assertEqual(self.ui.export_tree.selection(), ('one',))

    def test_task_details_never_label_registered_draft_as_quality_accepted(self):
        self.snapshot['jobs'] = [fixture_job()]
        self.ui.render_status(self.snapshot)
        details = self.ui.detail.get('1.0','end')
        self.assertIn('完整视听验收尚未通过', details)
        self.assertIn('字幕和文案重叠', details)
        self.assertFalse(self.ui.action_buttons['open_folder'].instate(['disabled']))
        self.assertTrue(self.ui.action_buttons['resume'].instate(['disabled']))
        self.ui.job_action('open_folder'); self.settle()
        self.backend.action.assert_called_once_with({'action':'open_folder','job_id':'job_'+'a'*32})

    def test_access_code_control_is_cleared_after_save(self):
        self.ui.settings_vars['access_token'].set('jyg_'+'x'*43)
        self.ui.save_settings(); self.settle()
        data = self.backend.save_settings.call_args.args[0]
        self.assertEqual(data['access_token'], 'jyg_'+'x'*43)
        self.assertEqual(self.ui.settings_vars['access_token'].get(), '')
        self.assertNotIn(data['access_token'], self.ui.notice_var.get())

    def test_waiting_registration_is_not_shown_as_already_in_jianying(self):
        self.snapshot['jobs'] = [fixture_job(publication=None,
            registration={'status':'waiting_editor','message':'等待退出剪映后自动登记；请保持工作台运行。'})]
        self.ui.render_status(self.snapshot)
        details = self.ui.detail.get('1.0','end')
        self.assertIn('待登记 · 请退出剪映', details)
        self.assertIn('自动登记', details)
        self.assertTrue(self.ui.action_buttons['open_folder'].instate(['disabled']))
        self.assertNotIn('已登记 · 待视听检查', details)

    def test_registering_disables_duplicate_action_and_failure_offers_retry(self):
        job = fixture_job(publication=None, registration={'status':'registering'})
        self.snapshot['jobs'] = [job]; self.ui.render_status(self.snapshot)
        self.assertTrue(self.ui.action_buttons['publish'].instate(['disabled']))
        job['registration'] = {'status':'failed','message':'校验失败，草稿已保留'}
        self.ui.render_status(self.snapshot)
        self.assertEqual(self.ui.action_buttons['publish'].cget('text'), '重试登记')
        self.assertIn('草稿已保留', self.ui.detail.get('1.0','end'))

    def test_closing_with_pending_registration_explains_resume_on_reopen(self):
        with patch('agent_core.desktop_workbench.messagebox.askyesno', return_value=False) as ask:
            self.ui._finish_close({'publishing':False,'running':False,'queued':False,'registration_pending':True})
        self.assertFalse(self.ui.closed)
        self.assertIn('自动登记将在下次启动', ask.call_args.args[1])

    def test_workflow_switch_updates_default_but_keeps_custom_requirements(self):
        self.ui.workflow.set('自定义剪辑'); self.ui.update_hint()
        self.assertEqual(self.ui.instruction.get('1.0','end').strip(), '')
        self.ui.instruction.delete('1.0','end'); self.ui.instruction.insert('1.0','保留所有产品细节')
        self.ui.workflow.set('跨境电商广告 · 美国区'); self.ui.update_hint()
        self.assertEqual(self.ui.instruction.get('1.0','end').strip(), '保留所有产品细节')

    def test_blank_custom_prompt_does_not_start_a_job(self):
        self.ui.workflow.set('自定义剪辑');self.ui.update_hint()
        self.ui.create_job()
        self.backend.create_job.assert_not_called()
        self.assertIn('请先填写',self.ui.notice_var.get())

    def test_cancelled_close_does_not_stop_active_worker(self):
        with patch('agent_core.desktop_workbench.messagebox.askyesno', return_value=False):
            self.ui._finish_close({'publishing':False,'running':True,'queued':False})
        self.assertFalse(self.backend.stop.is_set())
        self.assertFalse(self.ui.closed)
        self.backend.action.assert_not_called()

    def test_publishing_prevents_exit(self):
        self.ui._finish_close({'publishing':True,'running':True,'queued':False})
        self.assertFalse(self.ui.closed)
        self.assertIn('登记', self.ui.notice_var.get())

    def test_idle_close_ends_scheduler_without_canceling_job(self):
        self.ui._finish_close({'publishing':False,'running':False,'queued':False})
        self.assertTrue(self.ui.closed)
        self.assertTrue(self.backend.stop.is_set())
        self.backend.action.assert_not_called()


class DesktopLifecycleTests(unittest.TestCase):
    def test_no_resume_or_publish_while_worker_is_running(self):
        actions = allowed_actions(fixture_job(state='BUILDING_DRAFT', worker_running=True, publication=None))
        self.assertEqual(actions, {'cancel'})

    def test_wake_validates_pid_create_time_and_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'desktop-status.json').write_text(json.dumps({'pid':100,'process_created':1}))
            process = Mock(); process.create_time.return_value=2.5
            process.cmdline.return_value=['pythonw','D:/app/Launch-Desktop.pyw']
            with patch('psutil.Process',return_value=process):
                self.assertFalse(wake_existing(root))
                process.create_time.return_value=1
                self.assertTrue(wake_existing(root))
            self.assertTrue((root/'desktop-wake.json').is_file())

    def test_new_package_cannot_overwrite_live_worker_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'runtime-config.json').write_text(json.dumps({'toolchain_root':'D:/older/toolchain'}))
            job = root/'tasks/jobs/job_existing'; job.mkdir(parents=True)
            (job/'background-latest.json').write_text(json.dumps({'pid':100,'worker_id':'worker_123'}))
            process = Mock(); process.cmdline.return_value=['python','-m','agent_core.background','job_existing','worker_123']
            with patch('psutil.Process',return_value=process):
                with self.assertRaisesRegex(RuntimeError,'仍有剪辑任务'):
                    guard_runtime_change(root)


if __name__=='__main__':
    unittest.main()
