"""Real font picker widgets and saved task/render integration, no model calls."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tests import test_desktop_workbench as baseline
from tests import test_workbench as workbench_tests
from tests import test_task_service as service_tests
from agent_core.desktop_fonts import AUTO
from agent_core.auto_decorations import ROOT, select_caption_font
from agent_core.direct_renderer import snapshot_from_timeline
from agent_core.independent_export import _materialize_subtitle_images
from agent_core.task_service import task_spec


class FontPickerTests(unittest.TestCase):
    setUp = baseline.DesktopInteractionTests.setUp
    tearDown = baseline.DesktopInteractionTests.tearDown
    settle = baseline.DesktopInteractionTests.settle

    def open_picker(self):
        self.root.deiconify()
        self.root.geometry('1120x740')
        self.root.update()
        self.ui.font_library_button.invoke()
        self.root.update()
        self.assertTrue(self.ui.font_picker.window.winfo_exists())
        return self.ui.font_picker

    def choose(self, picker, font_id):
        picker.tree.selection_set(font_id)
        picker.choose()
        self.root.update_idletasks()
        picker.paint_preview()

    def test_real_catalog_preview_search_language_and_compact_layout(self):
        picker = self.open_picker()
        self.assertEqual(len(picker.tree.get_children()), 55)
        self.assertEqual(picker.window.grab_current(), picker.window)
        picker.query.set('手写')
        rows = picker.tree.get_children()
        self.assertIn('yomogi', rows)
        self.assertNotIn('poppins', rows)
        picker.query.set('')
        picker.category_buttons['英语'].invoke()
        self.assertEqual(picker.language.get(), '英语')
        self.assertIn('poppins', picker.tree.get_children())
        self.assertNotIn('yomogi', picker.tree.get_children())
        self.assertNotIn('noto_sans_sc', picker.tree.get_children())
        self.choose(picker, 'bangers')
        self.assertIsNotNone(picker._image)
        self.assertIn('英语', picker.coverage.get())
        picker.sample_text.set('中文试字')
        picker.paint_preview()
        self.assertIn('不支持', picker.coverage.get())
        self.assertFalse(picker.apply_button.instate(['disabled']))
        picker.query.set('no_font_matches_this_name')
        self.assertEqual(picker.tree.get_children(), (AUTO,))
        self.assertIn('没有匹配', picker.count.get())
        for widget in (picker.apply_button, picker.sample_entry, picker.tree, picker.canvas):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),
                                 picker.window.winfo_rooty()+picker.window.winfo_height())
        button = self.ui.font_library_button
        self.assertTrue(button.winfo_ismapped())
        self.assertLess(button.winfo_rooty()+button.winfo_height(),
                        self.ui.instruction.winfo_rooty())
        picker.close()
        self.backend.create_job.assert_not_called()

    def test_three_language_categories_are_complete_and_do_not_overlap(self):
        picker = self.open_picker()
        self.assertEqual(tuple(picker.category_buttons), ('全部', '中文', '英语', '日语'))
        grouped = {}
        for category, count in (('中文', 14), ('英语', 27), ('日语', 13)):
            picker.category_buttons[category].invoke()
            self.root.update_idletasks()
            grouped[category] = set(picker.tree.get_children()) - {AUTO}
            self.assertEqual(len(grouped[category]), count)
            self.assertIn(str(count), picker.count.get())
            self.assertTrue(picker.category_buttons[category].selected)
            self.assertTrue(picker.category_buttons[category].winfo_ismapped())
        self.assertIn('noto_sans_sc', grouped['中文'])
        self.assertIn('noto_sans_tc', grouped['中文'])
        self.assertIn('yomogi', grouped['日语'])
        self.assertFalse(grouped['中文'] & grouped['英语'])
        self.assertFalse(grouped['中文'] & grouped['日语'])
        self.assertFalse(grouped['日语'] & grouped['英语'])
        self.assertEqual(set.union(*grouped.values()), set(picker.library.families))
        picker.query.set('手写')
        self.assertIn('yomogi', picker.tree.get_children())
        self.assertNotIn('rocknroll_one', picker.tree.get_children())
        self.choose(picker, 'yomogi')
        self.assertIn('日语', picker.coverage.get())
        picker.apply()
        self.assertEqual(self.ui.selected_subtitle_font_id, 'yomogi')

    def test_cancel_does_not_change_font_apply_batch_and_auto_reset(self):
        picker = self.open_picker()
        self.choose(picker, 'bangers')
        picker.close()
        self.assertIsNone(self.ui.selected_subtitle_font_id)
        picker = self.open_picker()
        self.choose(picker, 'bangers')
        picker.apply_button.invoke()
        self.assertEqual(self.ui.selected_subtitle_font_id, 'bangers')
        self.assertIn('Bangers', self.ui.font_library_button.cget('text'))
        self.ui.workflow.set('跨境电商广告 · 美国区')
        self.ui.update_hint()
        self.ui.batch_count.set(3)
        self.ui.assets = [{'asset_id':'asset_a'}]
        self.backend.create_job.return_value = {'batch_id':'batch_fonts'}
        self.ui.create_job()
        self.settle()
        payload = self.backend.create_job.call_args.args[0]
        self.assertEqual(payload['subtitle_font_id'], 'bangers')
        self.assertEqual(payload['output_count'], 3)
        self.assertEqual(payload['target_country'], '美国')
        self.ui.show('create')
        picker = self.open_picker()
        self.assertEqual(picker.draft_id, 'bangers')
        self.choose(picker, AUTO)
        picker.apply()
        self.assertIsNone(self.ui.selected_subtitle_font_id)

    def test_keyboard_cancel_and_busy_button(self):
        picker = self.open_picker()
        picker.window.focus_force()
        picker.window.event_generate('<Escape>')
        self.root.update()
        self.assertFalse(picker.window.winfo_exists())
        self.assertIsNone(self.ui.selected_subtitle_font_id)
        self.ui._set_busy(True)
        self.ui.font_library_button.invoke()
        self.assertTrue(self.ui.font_library_button.instate(['disabled']))
        self.assertFalse(self.ui.font_picker.window.winfo_exists())


class FontTaskTests(unittest.TestCase):
    setUp = workbench_tests.WorkbenchTests.setUp
    tearDown = workbench_tests.WorkbenchTests.tearDown

    def test_font_flows_through_single_and_every_batch_spec(self):
        self.app.service = Mock()
        data = {'workflow':'crossborder_montage', 'target_country':'美国',
                'instruction':'字幕采用 Poppins', 'asset_ids':['asset_a'], 'subtitle_font_id':'bangers'}
        with patch.object(self.app, 'checks', return_value=[{'ok':True}]):
            self.app.create_job(data)
            spec = self.app.service.create_job.call_args.args[0]
            self.assertEqual(spec['subtitle_font_id'], 'bangers')
            self.app.create_batch({**data, 'output_count':3})
            self.assertEqual([s['subtitle_font_id'] for s in self.app.service.create_batch.call_args.args[0]],
                             ['bangers']*3)
        del data['subtitle_font_id']
        self.assertNotIn('subtitle_font_id', self.app._prepare_spec(data))
        for invalid in ('../../font.ttf', 'unknown', ['bangers'], True):
            with self.assertRaisesRegex(ValueError, '内置字体库'):
                self.app._prepare_spec({**data, 'subtitle_font_id':invalid})


class FontPersistenceTests(unittest.TestCase):
    setUp = service_tests.TaskServiceTests.setUp
    tearDown = service_tests.TaskServiceTests.tearDown

    def test_task_store_keeps_selected_font_and_rejects_unknown_ids(self):
        source = self.root/'clip.mp4'
        source.write_bytes(b'media')
        asset_id = self.service.register_asset(source)
        spec = task_spec('剪成60秒竖屏，添加中文字幕', [asset_id], 'interview')
        spec['subtitle_font_id'] = 'zcool_kuaile'
        job_id = self.service.create_job(spec)
        self.assertEqual(self.service.get_job(job_id)['task_spec']['subtitle_font_id'], 'zcool_kuaile')
        with self.assertRaisesRegex(ValueError, '内置字体库'):
            self.service.create_job({**spec, 'subtitle_font_id':'made_up_font'})


class FontRenderTests(unittest.TestCase):
    def test_manual_choice_overrides_prompt_and_cue_and_reaches_png(self):
        task = {'instruction':'字幕使用 Poppins，不要贴纸', 'subtitle_font_id':'bangers',
                'target_locale':'en-US', 'target':{'fps':30},
                'caption_motion':{'entry':'none', 'loop':'none'}}
        cue = {'text':'COMFORT EVERY DAY', 'font_id':'poppins'}
        choice = select_caption_font(task, cue, [])
        self.assertEqual(choice['font_id'], 'bangers')
        self.assertTrue(choice['requested_preference_applied'])
        timeline = {'duration_us':3_000_000, 'source_map':[
            {'asset_id':'asset_a', 'source_start_us':0, 'source_end_us':3_000_000,
             'timeline_start_us':0, 'timeline_end_us':3_000_000}],
            'captions':[{**cue, 'start_us':0, 'end_us':3_000_000}]}
        with tempfile.TemporaryDirectory() as raw:
            snapshot = snapshot_from_timeline(task, [
                {'asset_id':'asset_a', 'path':str(ROOT), 'width':720, 'height':1280,
                 'has_audio':False}], timeline, work_dir=raw)
            images = _materialize_subtitle_images(snapshot, Path(raw)/'subtitles')
            self.assertEqual(images[0]['font_selection']['font_id'], 'bangers')
            self.assertTrue(Path(images[0]['path']).is_file())

    def test_unsupported_glyphs_fall_back_to_complete_font(self):
        choice = select_caption_font({'subtitle_font_id':'bangers'}, {'text':'舒适生活每一天'}, [])
        self.assertNotEqual(choice['font_id'], 'bangers')
        self.assertFalse(choice['requested_preference_applied'])
        self.assertEqual(choice['coverage_policy'], 'complete_text_verified')


if __name__ == '__main__':
    unittest.main()
