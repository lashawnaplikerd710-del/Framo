"""Presentation integration tests: no real service, model or production tasks."""
import hashlib
import json
import queue
from pathlib import Path
import tkinter as tk
import threading
import time
from tkinter import font as tkfont, ttk
import unittest
from unittest.mock import Mock, patch

from tests import test_desktop_workbench as baseline
from agent_core import desktop_studio
from agent_core.desktop_studio import BG, CutButton, read_sections


class StudioTests(unittest.TestCase):
    setUp = baseline.DesktopInteractionTests.setUp
    tearDown = baseline.DesktopInteractionTests.tearDown
    settle = baseline.DesktopInteractionTests.settle

    def test_remove_undo_preserves_order_and_clears_after_creation(self):
        with patch.object(self.ui, '_load_preview', return_value=None):
            self.ui._added([self.source_asset(i) for i in (1,2,3)])
            original=[a['asset_id'] for a in self.ui.assets]
            self.ui.remove_files(asset_ids=[original[1]])
            self.assertFalse(self.ui.undo_assets_button.instate(['disabled']))
            self.ui._set_busy(True)
            self.ui.undo_remove_files()
            self.assertEqual(len(self.ui.assets),2)
            self.ui._set_busy(False)
            self.ui.undo_assets_button.invoke()
            self.assertEqual([a['asset_id'] for a in self.ui.assets],original)
            self.assertTrue(self.ui.undo_assets_button.instate(['disabled']))
            self.ui.remove_files(asset_ids=[original[1]])
            self.ui._created({'job_id':'test'})
            self.ui.undo_remove_files()
            self.assertFalse(self.ui.assets)
            self.assertFalse(self.ui._removed_assets)
        self.backend.action.assert_not_called()

    def test_task_summary_uses_actual_states_and_error_is_visible(self):
        self.batch_fixture()
        for job,state in zip(self.snapshot['jobs'],('COMPLETED','FAILED','WAITING_FOR_INPUT')):
            job['state']=state
        self.snapshot['jobs'][1]['error']='配音资源缺失'
        self.ui.render_status(self.snapshot)
        self.ui.task_tree.selection_set('j1');self.ui.draw_job()
        self.assertIn('完成 1',self.ui.batch_summary.get())
        self.assertIn('失败 1',self.ui.batch_summary.get())
        self.assertIn('待处理 1',self.ui.batch_summary.get())
        self.assertIn('有失败项',self.ui.group_tree.item('batch_a')['values'][0])
        self.assertEqual(str(self.ui.task_state_label.cget('foreground')),desktop_studio.RED)
        self.assertIn('配音资源缺失',self.ui.task_problem_var.get())
        self.snapshot.update(batches=[],jobs=[]);self.ui.render_status(self.snapshot)
        self.assertEqual(self.ui.timeline_panel.winfo_manager(),'')
        self.assertEqual(self.ui.task_state_var.get(),'未选择任务')

    def test_output_preview_redraw_retains_canvas_items(self):
        from PIL import Image
        self.root.deiconify();self.ui.show('tasks');self.root.update()
        self.ui._output_preview_image=Image.new('RGB',(180,320),'blue')
        self.ui.paint_output_preview();items=self.ui.output_preview_canvas.find_all()
        for _ in range(3):
            self.ui._output_preview_image=Image.new('RGB',(180,320),'red')
            self.ui.paint_output_preview()
            self.assertEqual(items,self.ui.output_preview_canvas.find_all())
        self.ui._output_preview_image=None;self.ui.paint_output_preview()
        self.assertEqual(items,self.ui.output_preview_canvas.find_all())

    def batch_fixture(self):
        path = Path(self.tmp.name)/'snapshot.json'
        rows = [{'output_id': f'o{i}', 'start_us': i*18_000_000, 'end_us': i*18_000_000+15_000_000}
                for i in range(3)]
        path.write_text(json.dumps({'batch_outputs': rows}), encoding='utf-8')
        jobs = [baseline.fixture_job(job_id=f'j{i}', publication=None,
                                    batch={'batch_id': 'batch_a', 'output_id': f'o{i}', 'ordinal': i+1})
                for i in range(3)]
        batch = {'batch_id': 'batch_a', 'gap_us': 3_000_000,
                 'children': [{'job_id': f'j{i}', 'output_id': f'o{i}'} for i in range(3)],
                 'counts': {'success': 3}, 'finalization': {'master_snapshot': str(path)}}
        self.snapshot.update(jobs=jobs, batches=[batch])
        self.ui.render_status(self.snapshot)
        return batch, path

    def test_five_pages_navigation_uses_dark_theme_without_service_dispatch(self):
        for view in ('create', 'templates', 'tasks', 'export', 'settings', 'create'):
            self.ui.show(view)
            self.root.update_idletasks()
            self.assertEqual(self.ui.current_view, view)
        self.assertEqual(len(self.ui.template_families), 6)
        self.assertEqual(sum(len(f['styles']) for f in self.ui.template_families), 11)
        self.assertEqual(self.root.cget('background'), BG)
        self.backend.create_job.assert_not_called()
        self.backend.action.assert_not_called()
        self.backend.save_settings.assert_not_called()

    def test_template_library_has_no_external_new_template_button(self):
        self.ui.show('templates')
        labels=[button.cget('text') for button in self.ui.buttons]
        self.assertNotIn('查看新版模板',labels)
        self.assertIn('导入 .motion 包',labels)

    def test_first_three_template_previews_never_open_author_demos(self):
        self.ui.show('templates')
        for family_id in ('01','02','03'):
            self.ui.template_tree.selection_set(family_id)
            self.ui._select_template_family()
            for choice in self.ui.template_styles.cget('values'):
                self.ui.template_style.set(choice)
                self.ui._template_style_changed()
                self.root.update_idletasks()
                self.assertTrue(self.ui.template_demo_button.instate(['disabled']))
                self.assertIn('无人脸',self.ui.template_preview_caption.get())
                self.assertNotIn('image',[self.ui.template_poster.type(item)
                                          for item in self.ui.template_poster.find_all()])
                with patch('os.startfile') as open_file:
                    self.ui._play_template_demo()
                    open_file.assert_not_called()
        self.ui.template_tree.selection_set('04')
        self.ui._select_template_family()
        self.assertFalse(self.ui.template_demo_button.instate(['disabled']))

    def test_group_selection_filters_children_and_preserves_poll_selection(self):
        self.batch_fixture()
        self.snapshot['jobs'].append(baseline.fixture_job(job_id='single', batch=None))
        self.ui.render_status(self.snapshot)
        self.assertEqual(self.ui.task_tree.get_children(), ('j0', 'j1', 'j2'))
        self.ui.task_tree.selection_set('j1')
        self.ui.render_status(self.snapshot)
        self.assertEqual(self.ui.selected_job()['job_id'], 'j1')
        self.ui.group_tree.selection_set('single')
        self.ui.select_group()
        self.assertEqual(self.ui.task_tree.get_children(), ('single',))

    def test_groups_mix_batches_and_singles_newest_first_and_keep_selection(self):
        batch, _ = self.batch_fixture()
        batch['created_at'] = '2026-10-13T10:00:00Z'
        self.snapshot['jobs'].extend([
            baseline.fixture_job(job_id='older', created_at='2026-10-13T09:00:00Z'),
            baseline.fixture_job(job_id='newer', created_at='2026-10-13T19:00:00+08:00')])
        self.ui.render_status(self.snapshot)
        self.assertEqual(self.ui.group_tree.get_children(), ('newer', 'batch_a', 'older'))
        self.ui.group_tree.selection_set('older'); self.ui.select_group()
        self.snapshot['jobs'][-2]['updated_at'] = '2026-10-14T10:00:00Z'
        self.snapshot['jobs'].append(baseline.fixture_job(job_id='latest', created_at='2026-10-13T12:00:00Z'))
        self.ui.render_status(self.snapshot)
        self.assertEqual(self.ui.group_tree.get_children(), ('latest', 'newer', 'batch_a', 'older'))
        self.assertEqual(self.ui.group_tree.selection(), ('older',))
        self.assertEqual(self.ui.selected_job()['job_id'], 'older')

    def test_timeline_reads_absolute_ranges_and_never_writes_snapshot(self):
        batch, path = self.batch_fixture()
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(self.ui._sections[1]['start_us'], 18_000_000)
        self.assertEqual(self.ui._sections[1]['start_us']-self.ui._sections[0]['end_us'], 3_000_000)
        for _ in range(3):
            self.ui.render_status(self.snapshot)
            self.ui.paint_timeline()
        self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
        self.backend.action.assert_not_called()

    def test_invalid_timeline_is_never_guessed(self):
        batch, path = self.batch_fixture()
        path.write_text(json.dumps({'batch_outputs': [{'output_id': 'bad', 'start_us': 10, 'end_us': 2}]}))
        rows, error = read_sections(batch)
        self.assertEqual(rows, [])
        self.assertIn('无效', error)
        self.ui.render_status(self.snapshot)
        self.assertEqual(self.ui._sections, [])
        self.assertIn('尚未确定', str(self.ui.export_tree.item('o0')['values']))

    def test_export_has_rows_before_start_and_requires_chosen_folder(self):
        self.batch_fixture()
        self.assertEqual(len(self.ui.export_tree.get_children()), 3)
        self.assertTrue(self.ui.export_start_button.instate(['disabled']))
        self.ui.export_start_button.invoke()
        self.backend.action.assert_not_called()
        self.ui.export_output.set('D:/chosen')
        self.assertFalse(self.ui.export_start_button.instate(['disabled']))

    def test_export_directories_are_independent_per_batch_and_keep_edits(self):
        self.ui.export_batch.set('a'); self.ui.refresh_export_items()
        self.ui.export_output.set('D:/a')
        self.ui.export_batch.set('b'); self.ui.refresh_export_items()
        self.assertEqual(self.ui.export_output.get(), '')
        self.ui.export_output.set('D:/b')
        self.ui.export_batch.set('a'); self.ui.refresh_export_items()
        self.assertEqual(self.ui.export_output.get(), 'D:/a')
        self.ui.refresh_export_items()
        self.assertEqual(self.ui.export_output.get(), 'D:/a')

    def test_export_retry_routes_only_selected_failed_output_and_keeps_conflict(self):
        self.batch_fixture()
        self.snapshot['external_exports'] = [{'batch_id': 'batch_a', 'state': 'FAILED', 'items': [
            {'output_id': 'o0', 'status': 'COMPLETED'}, {'output_id': 'o1', 'status': 'FAILED', 'error': 'missing source'}]}]
        self.ui.render_status(self.snapshot)
        self.ui.export_tree.selection_set('o1'); self.ui.draw_export_error()
        self.assertIn('文件不存在', self.ui.export_error.get('1.0', 'end'))
        self.assertNotIn('missing source', self.ui.export_error.get('1.0', 'end'))
        self.assertFalse(self.ui.export_retry_button.instate(['disabled']))
        self.ui.export_conflict.set('跳过')
        self.assertEqual(self.ui._export_conflict_value(), 'skip')
        self.ui.export_retry_button.invoke(); self.settle()
        self.backend.action.assert_called_once_with({'action': 'external_export_retry', 'batch_id': 'batch_a', 'output_id': 'o1'})

    def test_export_busy_refresh_and_batch_switch_clear_stale_errors(self):
        self.ui.export_batch.set('a')
        self.snapshot['external_exports'] = [{'batch_id': 'a', 'state': 'EXPORTING', 'items': [
            {'output_id': 'one', 'status': 'EXPORTING'}]}]
        self.ui.snapshot = self.snapshot
        self.ui.refresh_export_items(); self.ui.export_output.set('D:/out')
        self.assertTrue(self.ui.export_start_button.instate(['disabled']))
        self.assertFalse(self.ui.export_cancel_button.instate(['disabled']))
        self.ui.export_batch.set('empty'); self.ui.refresh_export_items()
        self.assertIn('选择成品', self.ui.export_error.get('1.0', 'end'))
        self.assertTrue(self.ui.export_cancel_button.instate(['disabled']))

    def test_market_editor_and_batch_hint_removed_preserving_user_prompt(self):
        self.ui.workflow.set('自定义剪辑'); self.ui.update_hint()
        self.assertFalse(hasattr(self.ui, 'country_row'))
        self.ui.workflow.set('跨境电商广告 · 日本区')
        self.ui.update_hint()
        self.assertFalse(hasattr(self.ui, 'target_country_entry'))
        def labels(widget):
            result = [str(widget.cget('text'))] if 'text' in widget.keys() else []
            return result + [text for child in widget.winfo_children() for text in labels(child)]
        self.assertNotIn('目标国家', labels(self.ui.window))
        self.assertFalse(any('成品间隙' in text for text in labels(self.ui.window)))
        self.assertIn('缺配音待补资源', self.ui.packaging_var.get())
        self.ui.instruction.delete('1.0', 'end'); self.ui.instruction.insert('1.0', '面向日本')
        self.ui.workflow.set('自定义剪辑'); self.ui.update_hint()
        self.assertEqual(self.ui.instruction.get('1.0', 'end').strip(), '面向日本')

    def test_crossborder_generic_label_is_removed_while_market_presets_remain(self):
        labels = list(self.ui.workflow_select.cget('values'))
        self.assertNotIn('跨境素材混剪 · 仅支持文案（不支持口播）', labels)
        self.assertIn('跨境电商广告 · 日本区', labels)
        self.assertIn('跨境电商广告 · 美国区', labels)

    def test_crossborder_market_presets_fill_country_and_use_ad_copy_hint(self):
        for label, country in (
            ('跨境电商广告 · 日本区', '日本'),
            ('跨境电商广告 · 美国区', '美国'),
        ):
            self.ui.workflow.set(label)
            self.ui.update_hint()
            self.assertEqual(self.ui.target_country.get(), country)
            self.assertIn('用户指令优先', self.ui.packaging_var.get())
            self.assertIn('缺配音待补资源', self.ui.packaging_var.get())
            self.assertIn(country + '区', self.ui.workflow_hint.get())

    def test_cut_buttons_support_disabled_state_and_count_limits(self):
        self.ui.batch_count.set(15); self.ui.change_count(1)
        self.assertEqual(self.ui.batch_count.get(), 15)
        self.ui.batch_count.set(1); self.ui.change_count(-1)
        self.assertEqual(self.ui.batch_count.get(), 1)
        b = self.ui.export_start_button
        self.assertIsInstance(b, CutButton)
        b.state(['disabled']); b.invoke()
        self.backend.action.assert_not_called()

    def test_missing_preview_does_not_fail_asset_import(self):
        with patch.object(self.ui, '_load_preview', return_value=None):
            self.backend.register_files.return_value = [{'asset_id': 'one', 'name': '不存在.mp4',
                'duration_us': 1_000_000, 'width': 1080, 'height': 1920}]
            self.ui.add_files(['D:/missing/不存在.mp4']); self.settle()
            self.root.update()
        self.assertEqual(len(self.ui.assets), 1)
        self.assertEqual(self.ui._asset_paths['one'], 'D:/missing/不存在.mp4')
        self.backend.create_job.assert_not_called()

    def wait_for_preview(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate():
            self.root.update()
            if time.monotonic() > deadline:
                self.fail('Material preview did not settle')
            time.sleep(.01)
        self.root.update()

    @staticmethod
    def source_asset(index):
        return dict(asset_id=f'source{index}', name=f'素材{index}.mp4',
                    source_path=f'D:/fixture/素材{index}.mp4', duration_us=2_000_000,
                    width=1080, height=1920)

    def test_import_displays_source_frame_and_thumbnail_without_legacy_setting(self):
        from PIL import Image
        self.root.deiconify(); self.root.update()
        self.assertNotIn('install_dir', self.ui.settings_vars)
        with patch.object(self.ui, '_load_preview', return_value=Image.new('RGB', (90, 160), 'red')) as load:
            self.backend.register_files.return_value = [self.source_asset(1)]
            self.ui.add_files(['D:/fixture/素材1.mp4'])
            self.wait_for_preview(lambda: self.ui.preview_image is not None)
        self.assertEqual(Path(load.call_args.args[1]), Path(desktop_studio.__file__).resolve().parents[1]/'toolchain')
        self.assertEqual(len(self.ui.preview_canvas.find_withtag('source_frame')), 1)
        self.assertEqual(len(self.ui.asset_strip.find_withtag('asset_thumbnail')), 1)
        self.backend.create_job.assert_not_called()
        self.backend.action.assert_not_called()

    def test_preview_does_not_wait_for_serialized_backend_executor(self):
        from PIL import Image
        release = threading.Event()
        started = threading.Event()
        def backend_wait():
            started.set()
            release.wait(8)
        work = self.ui.executor.submit(backend_wait)
        self.assertTrue(started.wait(1))
        try:
            with patch.object(self.ui, '_load_preview', return_value=Image.new('RGB', (90, 160))):
                self.ui._added([self.source_asset(1)])
                self.wait_for_preview(lambda: self.ui.preview_image is not None)
                self.assertFalse(work.done())
        finally:
            release.set()
            work.result(timeout=1)

    def test_thumbnail_switch_keyboard_selection_and_remove_clear_images(self):
        from PIL import Image
        self.root.deiconify(); self.root.update()
        with patch.object(self.ui, '_load_preview', side_effect=lambda path, _: Image.new('RGB', (90, 160),
                          'red' if path.endswith('1.mp4') else 'blue')):
            self.ui._added([self.source_asset(1), self.source_asset(2)])
            self.wait_for_preview(lambda: len(self.ui._preview_cache) == 2)
            self.ui.asset_strip.event_generate('<Button-1>', x=190, y=50)
            self.root.update()
            self.assertEqual(self.ui.preview_name.get(), '素材2.mp4')
            self.assertEqual(self.ui.preview_image.getpixel((0, 0)), (0, 0, 255))
            self.ui._step_asset(-1)
            self.assertEqual(self.ui.preview_name.get(), '素材1.mp4')
            self.ui.asset_strip.event_generate('<Button-1>', x=190, y=50, state=0x0004)
            self.root.update()
            self.assertEqual(set(self.ui.asset_tree.selection()), {'source1', 'source2'})
            self.ui.draw_assets()
            self.assertEqual(set(self.ui.asset_tree.selection()), {'source1', 'source2'})
            self.ui.remove_files(); self.root.update()
            self.assertFalse(self.ui.assets)
            self.assertIsNone(self.ui.preview_image)
            self.assertFalse(self.ui.preview_canvas.find_withtag('source_frame'))
            self.assertFalse(self.ui.asset_strip.find_withtag('asset_thumbnail'))

    def test_failed_thumbnail_can_retry_without_dropping_material(self):
        from PIL import Image
        self.root.deiconify(); self.root.update()
        with patch.object(self.ui, '_load_preview', side_effect=[None, Image.new('RGB', (90, 160))]) as load:
            self.ui._added([self.source_asset(1)])
            self.wait_for_preview(lambda: bool(self.ui._preview_failed))
            self.assertEqual(len(self.ui.assets), 1)
            self.assertEqual(self.ui.preview_retry_button.winfo_manager(), 'pack')
            self.ui.preview_retry_button.invoke()
            self.wait_for_preview(lambda: self.ui.preview_image is not None)
            self.assertEqual(load.call_count, 2)
            self.assertEqual(self.ui.preview_retry_button.winfo_manager(), '')

    def test_late_frame_for_removed_material_cannot_replace_current_preview(self):
        from PIL import Image
        release, started = threading.Event(), threading.Event()
        def load(path, _):
            if path.endswith('1.mp4'):
                started.set(); release.wait(6)
                return Image.new('RGB', (90, 160), 'red')
            return Image.new('RGB', (90, 160), 'blue')
        try:
            with patch.object(self.ui, '_load_preview', side_effect=load):
                self.ui._added([self.source_asset(1)])
                self.assertTrue(started.wait(1))
                self.ui.remove_files()
                self.ui._added([self.source_asset(2)])
                release.set()
                self.wait_for_preview(lambda: self.ui.preview_image is not None)
                self.assertEqual(self.ui.preview_name.get(), '素材2.mp4')
                self.assertEqual(self.ui.preview_image.getpixel((0, 0)), (0, 0, 255))
        finally:
            release.set()

    def test_go_export_selects_the_current_batch(self):
        self.batch_fixture()
        self.ui.go_export()
        self.assertEqual(self.ui.current_view, 'export')
        self.assertEqual(self.ui.export_batch.get(), 'batch_a')

    def test_empty_preview_click_opens_picker_and_imports_without_creating_task(self):
        self.root.deiconify(); self.root.update()
        self.backend.register_files.return_value = [{'asset_id': 'added', 'name': '饰品.mp4',
            'duration_us': 2_000_000, 'width': 1080, 'height': 1920}]
        with patch('agent_core.desktop_workbench.filedialog.askopenfilenames', return_value=('D:/素材/饰品.mp4',)) as picker, \
             patch.object(self.ui, '_load_preview', return_value=None):
            canvas = self.ui.preview_canvas
            canvas.event_generate('<Button-1>', x=canvas.winfo_width()//2, y=canvas.winfo_height()//2)
            self.settle()
        picker.assert_called_once()
        self.backend.register_files.assert_called_once_with(['D:/素材/饰品.mp4'])
        self.assertTrue(self.ui.asset_tree.exists('added'))
        self.backend.create_job.assert_not_called()
        self.backend.action.assert_not_called()

    def test_cancelled_picker_can_be_reopened_and_busy_preview_cannot_import(self):
        self.root.deiconify(); self.root.update()
        c = self.ui.preview_canvas
        with patch('agent_core.desktop_workbench.filedialog.askopenfilenames', return_value=()) as picker:
            c.event_generate('<Button-1>', x=60, y=60)
            c.event_generate('<Button-1>', x=80, y=80)
            self.assertEqual(picker.call_count, 2)
            self.ui._set_busy(True)
            c.event_generate('<Button-1>', x=60, y=60)
            self.assertEqual(picker.call_count, 2)
            self.assertEqual(c.cget('cursor'), 'arrow')
            self.ui._set_busy(False)
        self.assertFalse(self.ui.assets)
        self.assertEqual(c.cget('cursor'), 'hand2')
        self.backend.register_files.assert_not_called()

    def test_empty_preview_keyboard_activation_and_existing_media_are_separate(self):
        self.root.deiconify(); self.root.update()
        c = self.ui.preview_canvas
        c.focus_force(); self.root.update()
        with patch('agent_core.desktop_workbench.filedialog.askopenfilenames', return_value=()) as picker:
            c.event_generate('<Return>'); c.event_generate('<space>')
            self.assertEqual(picker.call_count, 2)
            self.ui.assets = [{'asset_id': 'existing'}]
            self.ui.paint_preview()
            c.event_generate('<Button-1>', x=60, y=60)
            self.assertEqual(picker.call_count, 2)
        self.assertEqual(c.cget('cursor'), 'arrow')
        texts = [c.itemcget(i, 'text') for i in c.find_all() if c.type(i) == 'text']
        self.assertNotIn('＋', texts)

    def test_native_ui_family_is_used_by_all_text_surfaces(self):
        self.assertNotIn('975', self.ui.ui_font_family)
        expected = self.ui.ui_font_family
        for widget in (self.ui.instruction, self.ui.detail, self.ui.export_error, self.ui.checks_text):
            self.assertEqual(tkfont.Font(root=self.root, font=widget.cget('font')).actual('family'), expected)
        style = ttk.Style(self.root)
        for name in ('.', 'Metric.TLabel', 'Treeview', 'Treeview.Heading', 'TEntry', 'TCombobox'):
            self.assertEqual(tkfont.Font(root=self.root, font=style.lookup(name, 'font')).actual('family'), expected)
        for name in ('TkDefaultFont', 'TkTextFont', 'TkFixedFont'):
            self.assertEqual(tkfont.nametofont(name, root=self.root).actual('family'), expected)
        b = self.ui.export_start_button
        b._draw()
        item = next(i for i in b.find_all() if b.type(i) == 'text')
        self.assertEqual(tkfont.Font(root=self.root, font=b.itemcget(item, 'font')).actual('family'), desktop_studio.CONTROL_FONT)
        self.assertEqual(tkfont.Font(root=self.root, font=b.itemcget(item, 'font')).actual('weight'), 'bold')
        for name in ('Title.TLabel', 'Heading.TLabel'):
            actual = tkfont.Font(root=self.root, font=style.lookup(name, 'font')).actual()
            self.assertEqual(actual['family'], desktop_studio.CONTROL_FONT)
            self.assertEqual(actual['weight'], 'bold')

    def test_thumbnail_x_removes_one_material_without_deleting_source_or_other_selections(self):
        from PIL import Image
        self.root.deiconify();self.root.update()
        paths=[Path(self.tmp.name)/f'original-{i}.mp4' for i in (1,2)]
        for path in paths:path.write_bytes(b'original')
        assets=[{**self.source_asset(i),'source_path':str(p)} for i,p in zip((1,2),paths)]
        with patch.object(self.ui,'_load_preview',return_value=Image.new('RGB',(90,160))):
            self.ui._added(assets)
            self.wait_for_preview(lambda:len(self.ui._preview_cache)==2)
            self.ui.asset_tree.selection_set(['source1','source2']);self.ui.preview_asset()
            self.ui._set_busy(True)
            # Event generation already dispatches the click synchronously.
            # Drain layout, rather than waiting on a running progress timer.
            self.ui.asset_strip.event_generate('<Button-1>',x=127,y=15);self.root.update_idletasks()
            self.assertEqual(len(self.ui.assets),2)
            self.ui._set_busy(False)
            self.ui.asset_strip.event_generate('<Button-1>',x=127,y=15);self.root.update()
        self.assertEqual([a['asset_id'] for a in self.ui.assets],['source2'])
        self.assertEqual(self.ui.asset_tree.selection(),('source2',))
        self.assertEqual([p.read_bytes() for p in paths],[b'original',b'original'])
        self.backend.create_job.assert_not_called();self.backend.action.assert_not_called()

    def test_failed_rows_are_red_in_tasks_groups_and_exports_including_selection(self):
        from agent_core.desktop_studio import RED
        self.snapshot['jobs']=[baseline.fixture_job(job_id='failed',state='FAILED',publication=None,error='failed'),
                               baseline.fixture_job(job_id='pending',state='WAITING_FOR_INPUT',publication=None)]
        self.snapshot['external_exports']=[{'batch_id':'export_failed','items':[
            {'output_id':'one','status':'FAILED','error':'render failed'}]}]
        self.ui.render_status(self.snapshot)
        self.assertEqual(self.ui.group_tree.item('failed','tags'),('bad',))
        self.assertEqual(self.ui.task_tree.item('failed','tags'),('bad',))
        self.assertEqual(self.ui.group_tree.tag_configure('bad')['foreground'],RED)
        self.assertEqual(self.ui.group_tree.item('pending','tags'),('warning',))
        self.ui.export_batch.set('export_failed');self.ui.refresh_export_items()
        self.assertEqual(self.ui.export_tree.item('one','tags'),('bad',))
        self.assertEqual(ttk.Style(self.root).map('Treeview','foreground'),[])

    def add_playable_source(self):
        from PIL import Image
        self.root.deiconify();self.root.update()
        path=Path(self.tmp.name)/'source.mp4';path.write_bytes(b'fixture')
        with patch.object(self.ui,'_load_preview',return_value=Image.new('RGB',(90,160),'red')):
            self.ui._added([{**self.source_asset(1),'source_path':str(path)}])
            self.wait_for_preview(lambda:self.ui.preview_image is not None)
        return path

    @staticmethod
    def fake_stream():
        return Mock(frames=queue.Queue(maxsize=2), error=None, audio_status='声音开启')

    def test_large_canvas_play_pause_and_clip_removal_stop_decoder_without_mutating_media(self):
        from PIL import Image
        path=self.add_playable_source();canvas=self.ui.preview_canvas
        ids=canvas.find_all();first,second=self.fake_stream(),self.fake_stream()
        with patch('agent_core.desktop_studio.OutputPreviewStream',side_effect=[first,second]) as factory:
            canvas.event_generate('<Button-1>',x=canvas.winfo_width()//2,y=canvas.winfo_height()//2)
            self.assertTrue(self.ui._source_preview_playing);first.start.assert_called_once()
            token=self.ui._source_preview_token
            first.frames.put(('frame',(0.4,Image.new('RGB',(90,160),'blue'))))
            self.ui._poll_source_preview_stream(token)
            self.assertEqual(self.ui.preview_image.getpixel((0,0)),(0,0,255))
            self.assertEqual(canvas.find_all(),ids)
            canvas.event_generate('<Button-1>',x=40,y=40)
            self.assertFalse(self.ui._source_preview_playing);first.stop.assert_called_once()
            self.assertEqual(self.ui._source_preview_position,0.4)
            self.ui.toggle_source_preview();second.start.assert_called_once()
            self.assertEqual(factory.call_args.args[1],0.4)
            self.ui.remove_files(asset_ids=['source1'])
            second.stop.assert_called_once();self.assertIsNone(self.ui._source_preview_stream)
            self.ui._poll_source_preview_stream(token)
            self.assertIsNone(self.ui.preview_image)
        self.assertEqual(path.read_bytes(),b'fixture')
        self.backend.action.assert_not_called()

    def test_playback_end_restarts_and_error_keeps_material(self):
        self.add_playable_source();first,second,third=[self.fake_stream() for _ in range(3)]
        with patch('agent_core.desktop_studio.OutputPreviewStream',side_effect=[first,second,third]) as factory:
            self.ui.toggle_source_preview();first.frames.put(('ended',None))
            self.ui._poll_source_preview_stream(self.ui._source_preview_token)
            self.assertFalse(self.ui._source_preview_playing)
            self.assertEqual(self.ui._source_preview_position,2.0)
            self.ui.toggle_source_preview();self.assertEqual(factory.call_args.args[1],0.0)
            second.frames.put(('error',None));self.ui._poll_source_preview_stream(self.ui._source_preview_token)
            self.assertEqual(len(self.ui.assets),1);self.assertIn('读取失败',self.ui.notice_var.get())
            self.ui.toggle_source_preview();self.ui.shutdown()
            third.stop.assert_called_once()

    def test_rapid_navigation_keeps_widgets_and_images_and_pauses_hidden_preview(self):
        self.add_playable_source();stream=self.fake_stream()
        ids={key:widget.winfo_id() for key,widget in self.ui.views.items()}
        frame_id=self.ui.preview_canvas.find_withtag('source_frame')
        with patch('agent_core.desktop_studio.OutputPreviewStream',return_value=stream):
            self.ui.toggle_source_preview();self.ui.show('tasks')
        self.assertFalse(self.ui._source_preview_playing);stream.stop.assert_called_once()
        for view in ['export','create','tasks','export','create']*5:self.ui.show(view)
        self.wait_for_preview(lambda:abs(self.ui._nav_indicator_y-(self.ui.nav_buttons['create'].winfo_y()+10))<0.1)
        self.assertEqual(ids,{key:widget.winfo_id() for key,widget in self.ui.views.items()})
        self.assertEqual(frame_id,self.ui.preview_canvas.find_withtag('source_frame'))
        self.assertTrue(self.ui.nav_buttons['create'].selected)
        self.assertFalse(self.ui.nav_buttons['export'].selected)
        self.assertEqual(self.ui._nav_indicator.winfo_y(),0)
        self.backend.create_job.assert_not_called();self.backend.action.assert_not_called()

    def test_packaging_preserves_both_rounded_fonts_and_license(self):
        from package_workbench import copy_studio_fonts, ROOT
        destination = Path(self.tmp.name)/'bundle'
        copy_studio_fonts(destination)
        folder = destination/'desktop-assets/fonts'
        self.assertEqual({p.name for p in folder.iterdir()},
                         {'LXGW975YuanSC-400W.ttf', 'LXGW975YuanSC-700W.ttf', 'OFL-975Yuan.txt', 'ORIGIN.md'})
        for path in folder.iterdir():
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),
                             hashlib.sha256((ROOT/'desktop-assets/fonts'/path.name).read_bytes()).digest())


if __name__ == '__main__':
    unittest.main()
