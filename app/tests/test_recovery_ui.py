import copy,unittest,tkinter as tk
from unittest.mock import Mock
from agent_core.desktop_studio import StudioLayout
from agent_core.desktop_recovery import TaskRepairDialog
from agent_core.task_recovery import repair_guide

class Owner(StudioLayout):
 def __init__(self):
  self.window=tk.Tk();self._style();self.window.geometry('1000x760');self.busy=False;self.backend=Mock();self.notice=Mock()
 def run(self,title,fn,callback):callback(fn())

class RecoveryUIHelpersTests(unittest.TestCase):
 def test_problem_quotes_and_readonly_next_step_are_explicit(self):
  guide=repair_guide({'editable':False,'advice':'模型已变更','headings':[]})
  self.assertEqual(guide['focus'],'details')
  self.assertIn('带入原素材',guide['steps'][1])
  guide=repair_guide({'editable':True,'narration_script':'文案','reason':'内容不实','headings':[]},{'issues':['无依据承诺'],'findings':[{'problem':'无依据承诺','quote':'加厚保暖'}]})
  self.assertEqual(guide['problems'][0]['quote'],'加厚保暖')

class RecoveryUIInteractionTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  try:cls.owner=Owner()
  except tk.TclError as e:raise unittest.SkipTest(str(e))
 @classmethod
 def tearDownClass(cls):cls.owner.window.destroy()
 def setUp(self):
  self.data={'job_id':'qa','checkpoint_id':'cp','editable':True,'locale':'ja-JP','reason':'旁白过长','advice':'精简','resume_count':0,'target_units':8,'headings':[{'clip_index':1,'heading':'入口のカーテン','problem':False,'max_length':36},{'clip_index':2,'heading':'ストライプ柄','problem':False,'max_length':36}],'narration_script':'ペットハウスです。'}
  self.dialog=TaskRepairDialog(self.owner,self.data);self.owner.backend.reset_mock();self.owner.window.update()
 def tearDown(self):self.dialog.close();self.owner.window.update()
 def test_editor_is_writable_and_updates_live_budget(self):
  d=self.dialog;d.narration.insert('end','入口が開いています。');self.owner.window.update()
  self.assertIn('建议再删',d.narration_meta.get())
  self.assertEqual(d.narration.cget('state'),'normal')
 def test_empty_narration_explains_action_without_submitting(self):
  d=self.dialog;d.narration.delete('1.0','end');d.save(True)
  self.owner.backend.action.assert_not_called();self.assertIn('AI 帮我改写',d.message.get())
 def test_caption_switch_does_not_lose_unsaved_edit(self):
  d=self.dialog;d.tree.selection_set('1');d.select_caption();d.heading.delete('1.0','end');d.heading.insert('1.0','入口です。');d.tree.selection_set('2');d.select_caption()
  self.assertEqual(d.headings[1]['heading'],'入口です。')
 def test_ai_suggestion_fills_both_editors_without_saving_or_queuing(self):
  d=self.dialog;self.owner.backend.action.return_value={'headings':[{'clip_index':1,'heading':'新しい入口'},{'clip_index':2,'heading':'新しい形'}],'narration_script':'入口です。','target_units':12,'approved':False}
  d.rewrite();d.flush()
  self.assertEqual(d.headings[1]['heading'],'新しい入口')
  self.assertEqual(d.narration.get('1.0','end-1c'),'入口です。')
  self.assertEqual(self.owner.backend.action.call_count,1)
  self.assertEqual(self.owner.backend.action.call_args.args[0]['action'],'suggest_script_rewrite')
  self.assertIn('尚未保存',d.message.get())
 def test_readonly_case_has_explicit_route_and_disabled_editors(self):
  d=self.dialog;data={**self.data,'editable':False,'advice':'没有可编辑检查点'};d.load(data)
  self.assertEqual(d.narration.cget('state'),'disabled');self.assertTrue(d.continue_button.disabled)
  self.assertIn('不能直接修改',d.guide_title.get());self.assertFalse(d.new_button.disabled)
 def test_actions_and_editors_remain_visible_at_minimum_size(self):
  d=self.dialog;d.window.geometry('840x560');self.owner.window.update()
  self.assertGreater(d.narration.winfo_height(),45)
  self.assertLess(d.continue_button.winfo_rooty()+d.continue_button.winfo_height(),d.window.winfo_rooty()+d.window.winfo_height()+1)
