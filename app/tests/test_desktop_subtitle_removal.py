from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
from PIL import Image
from tests import test_desktop_workbench as baseline


class SubtitleRemovalInteractionTests(unittest.TestCase):
    setUp=baseline.DesktopInteractionTests.setUp
    tearDown=baseline.DesktopInteractionTests.tearDown
    settle=baseline.DesktopInteractionTests.settle

    def pump(self,predicate,timeout=3):
        deadline=time.monotonic()+timeout
        while not predicate() and time.monotonic()<deadline:
            self.root.update();time.sleep(.005)
        self.assertTrue(predicate())

    def test_tool_is_singleton_and_uses_own_background_executor(self):
        self.ui.open_subtitle_removal()
        dialog=self.ui.subtitle_removal_dialog
        self.ui.open_subtitle_removal()
        self.assertIs(self.ui.subtitle_removal_dialog,dialog)
        main_thread=threading.get_ident()
        seen=[]
        def inspect(path,tools):
            seen.append(threading.get_ident())
            return {'path':'D:/素材/原片.mp4','width':320,'height':240,'duration_s':8}
        with patch('agent_core.independent_export.discover_tools',return_value={'ffmpeg':'ffmpeg','ffprobe':'ffprobe'}),\
             patch('agent_core.desktop_subtitle_removal.inspect_video',side_effect=inspect),\
             patch('agent_core.desktop_subtitle_removal.read_frame',return_value=Image.new('RGB',(320,240))):
            dialog.load('D:/素材/原片.mp4')
            self.assertFalse(self.ui.busy)
            self.pump(lambda:not dialog.busy)
        self.assertNotEqual(seen[0],main_thread)
        self.assertEqual(dialog.before.size,(320,240))
        self.assertFalse(dialog.export_button.disabled)
        self.assertIsNone(dialog.after)

    def test_mask_uses_original_pixel_coordinates_and_edit_invalidates_preview(self):
        self.ui.open_subtitle_removal();dialog=self.ui.subtitle_removal_dialog
        dialog.info={'width':1080,'height':1920}
        dialog.before=Image.new('RGB',(1080,1920))
        dialog.set_region((100,1500,980,1630))
        self.root.update()
        self.assertEqual(dialog.region,(100,1500,980,1630))
        x,y,sx,sy=dialog.mapping
        class Event:pass
        e=Event();e.x=x+540*sx;e.y=y+960*sy
        self.assertEqual(dialog.point(e),(540,960))
        dialog.after=dialog.before.copy()
        dialog.set_region((100,1490,980,1620))
        self.assertIsNone(dialog.after)

    def test_import_replaces_only_matching_source_and_registration_failure_keeps_it(self):
        original=str(Path(self.tmp.name)/'original.mp4')
        output=str(Path(self.tmp.name)/'fixed.mp4')
        def asset(aid,path):return {'asset_id':aid,'name':Path(path).name,
            'source_path':path,'duration_us':2_000_000,'width':160,'height':240}
        self.ui._added([asset('a',original),asset('other',str(Path(self.tmp.name)/'other.mp4'))])
        self.backend.register_files.side_effect=ValueError('素材读取失败')
        self.ui.import_repaired_source(original,output);self.settle()
        self.assertEqual([a['asset_id'] for a in self.ui.assets],['a','other'])
        self.backend.register_files.side_effect=None
        self.backend.register_files.return_value=[asset('fixed',output)]
        self.ui.import_repaired_source(original,output);self.settle()
        self.assertEqual({a['asset_id'] for a in self.ui.assets},{'fixed','other'})
        self.assertEqual(self.ui.asset_tree.selection(),('fixed',))
        self.backend.create_job.assert_not_called()
        self.assertEqual(self.ui.current_view,'create')

    def test_close_during_work_requests_cancel_and_shutdown_closes_tool(self):
        self.ui.open_subtitle_removal();dialog=self.ui.subtitle_removal_dialog
        started=threading.Event();release=threading.Event()
        def work():started.set();release.wait(2)
        dialog.submit('测试后台任务',work,lambda _:None)
        try:
            self.assertTrue(started.wait(1))
            dialog.close()
            self.assertTrue(dialog.cancel.is_set())
            self.assertFalse(dialog.closed)
            release.set();self.pump(lambda:not dialog.busy)
            self.ui.shutdown()
            self.assertTrue(dialog.closed)
        finally:release.set()


if __name__=='__main__':unittest.main()
