import unittest
from agent_core.desktop_workbench import WORKFLOWS
from tests.test_desktop_workbench import DesktopInteractionTests


class LongVideoUITests(unittest.TestCase):
    setUp=DesktopInteractionTests.setUp
    tearDown=DesktopInteractionTests.tearDown
    settle=DesktopInteractionTests.settle

    def test_long_settings_visibility_and_target_survive_creation(self):
        self.root.deiconify();self.root.geometry('1120x740');self.root.update()
        self.assertIn('长视频',WORKFLOWS)
        self.ui.workflow.set('长视频');self.ui.update_hint();self.root.update()
        self.assertEqual(self.ui.long_settings.winfo_manager(),'pack')
        self.ui.long_duration.set('10分钟')
        self.ui.custom_voice_id='kokoro_zh_male_01'
        self.ui.run=lambda _label,_fn,_callback:None
        self.ui.create_job()
        self.assertEqual(self.ui._creating_payload['workflow'],'long_video')
        self.assertEqual(self.ui._creating_payload['target_duration_ms'],600000)
        self.assertEqual(self.ui._creating_payload['direct_media']['voice_id'],'kokoro_zh_male_01')
        self.ui.workflow.set('自定义剪辑');self.ui.update_hint();self.root.update()
        self.assertFalse(self.ui.long_settings.winfo_manager())

    def test_original_long_mode_disables_voice_and_font_and_sends_duration(self):
        self.root.deiconify();self.root.geometry('1120x740');self.root.update()
        self.assertEqual(list(WORKFLOWS)[-1],'长视频（无口播）')
        self.ui.workflow.set('长视频（无口播）');self.ui.update_hint();self.root.update()
        self.assertEqual(self.ui.long_settings.winfo_manager(),'pack')
        self.ui.long_duration.set('3分钟')
        self.assertTrue(self.ui.voice_library_button.instate(['disabled']))
        self.assertTrue(self.ui.font_library_button.instate(['disabled']))
        self.ui.custom_voice_id='kokoro_zh_female_01'
        self.ui.run=lambda _label,_fn,_callback:None
        self.ui.create_job()
        self.assertEqual(self.ui._creating_payload['workflow'],'long_video_no_voice')
        self.assertEqual(self.ui._creating_payload['target_duration_ms'],180000)
        self.assertNotIn('direct_media',self.ui._creating_payload)


if __name__=='__main__':unittest.main()
