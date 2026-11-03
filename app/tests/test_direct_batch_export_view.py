"""Direct deliveries share the export page without pretending to re-export."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent_core.workbench import Workbench
from tests import test_desktop_workbench as baseline


class DirectExportPageTests(unittest.TestCase):
    setUp = baseline.DesktopInteractionTests.setUp
    tearDown = baseline.DesktopInteractionTests.tearDown

    def direct_fixture(self, states=('COMPLETED', 'COMPLETED'), missing=()):
        jobs, children, outputs = [], [], []
        for i, state in enumerate(states):
            output = Path(self.tmp.name)/f'成品-{i}.mp4'
            if i not in missing:
                output.write_bytes(b'UI fixture; decoding covered by actual runtime QA')
            record = {'output_id':f'o{i}', 'output_file':str(output), 'status':'COMPLETED',
                      'duration_us':8_000_000-i*2_000_000}
            outputs.append(record)
            children.append({'job_id':f'j{i}', 'output_id':f'o{i}', 'state':state})
            jobs.append(baseline.fixture_job(job_id=f'j{i}', state=state, render_backend='direct_ffmpeg',
                delivery=record, batch={'batch_id':'direct', 'output_id':f'o{i}', 'ordinal':i+1}))
        batch = {'batch_id':'direct', 'children':children, 'finalization':{
            'render_backend':'direct_ffmpeg', 'status':'direct_outputs_ready',
            'master_snapshot':None, 'master_draft':None, 'outputs':outputs}}
        self.snapshot.update(jobs=jobs, batches=[batch])
        self.ui.render_status(self.snapshot)
        self.ui.export_batch.set('direct');self.ui.refresh_export_items()
        return batch

    def test_actual_existing_deliveries_are_visible_without_fake_export_receipt(self):
        batch = self.direct_fixture()
        self.assertEqual(self.ui.export_metric.get(), '02 / 02')
        self.assertIn('已自动生成，无需再次导出', self.ui.export_report_var.get())
        self.assertEqual(self.ui.export_tree.item('o0')['values'][1:], ['已生成', '00:08.00', '成品-0.mp4'])
        self.assertEqual(self.ui.export_tree.heading('range')['text'], '视频时长')
        for control in (self.ui.export_start_button, self.ui.export_retry_button,
                        self.ui.export_cancel_button, self.ui.export_directory_button,
                        self.ui.export_directory_entry, self.ui.export_conflict_select):
            self.assertTrue(control.instate(['disabled']))
        self.ui.export_output.set('D:/ignored')
        self.ui.start_export()
        self.backend.action.assert_not_called()
        self.assertIsNone(self.ui._selected_export_report())
        self.assertNotIn('external_exports', self.snapshot)
        self.ui.export_tree.selection_set('o1');self.ui.draw_export_error()
        self.assertIn(batch['finalization']['outputs'][1]['output_file'],self.ui.export_error.get('1.0','end'))
        with patch('os.startfile') as opener:
            self.ui.open_export_directory()
            opener.assert_called_once_with(self.tmp.name)

    def test_completed_with_missing_file_is_not_counted_as_success(self):
        self.direct_fixture(missing=(1,))
        self.assertEqual(self.ui.export_metric.get(), '01 / 02')
        self.assertEqual(self.ui.export_tree.item('o1')['values'][1], '文件缺失')
        self.ui.export_tree.selection_set('o1');self.ui.draw_export_error()
        self.assertIn('成品文件缺失',self.ui.export_error.get('1.0','end'))
        self.assertIn('成品-1.mp4',self.ui.export_error.get('1.0','end'))
        self.assertTrue(self.ui.export_retry_button.instate(['disabled']))
        self.assertTrue(self.ui.export_start_button.instate(['disabled']))

    def test_failed_waiting_cancelled_residual_files_never_become_success(self):
        self.direct_fixture(states=('FAILED','WAITING_FOR_INPUT','CANCELLED'))
        self.assertEqual(self.ui.export_metric.get(), '00 / 03')
        self.assertEqual([self.ui.export_tree.item(f'o{i}')['values'][1] for i in range(3)],
                         ['执行失败','需要处理','已取消'])
        self.assertTrue(self.ui.export_start_button.instate(['disabled']))

    def test_old_master_draft_export_recovers_its_original_controls(self):
        self.direct_fixture()
        master=Path(self.tmp.name)/'master.json'
        master.write_text(json.dumps({'batch_outputs':[{'output_id':'legacy-output',
            'start_us':0, 'end_us':4_000_000}]}),encoding='utf-8')
        self.snapshot['batches'].append({'batch_id':'legacy','children':[{'output_id':'legacy-output'}],
            'finalization':{'master_snapshot':str(master),'render_backend':'native_jianying'}})
        self.ui.render_status(self.snapshot)
        self.ui.export_batch.set('legacy');self.ui.refresh_export_items()
        self.assertIsNone(self.ui._direct_export_view())
        self.assertEqual(self.ui.export_tree.heading('range')['text'], '绝对时间范围')
        self.assertFalse(self.ui.export_directory_button.instate(['disabled']))
        self.assertFalse(self.ui.export_directory_entry.instate(['disabled']))
        self.assertEqual(str(self.ui.export_conflict_select.cget('state')), 'readonly')
        self.ui.export_output.set('D:/legacy-output')
        self.assertFalse(self.ui.export_start_button.instate(['disabled']))
        self.assertIn('尚未开始导出', self.ui.export_report_var.get())


class ExportBackendGuardTests(unittest.TestCase):
    def test_direct_no_master_has_chinese_guard_and_never_touches_receipts(self):
        with tempfile.TemporaryDirectory() as raw:
            app=Workbench.__new__(Workbench);app.service=Mock()
            app.service.store.batch_dir.return_value=Path(raw)
            final=Path(raw)/'batch-finalization.json'
            text=json.dumps({'render_backend':'direct_ffmpeg','master_snapshot':None,'master_draft':None})
            final.write_text(text,encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'自动生成独立 MP4，无需再次导出'):
                app._export_snapshot('direct')
            self.assertEqual(final.read_text('utf-8'),text)
            app.service.finalize_batch.assert_not_called()

    def test_legacy_master_snapshot_still_loads(self):
        with tempfile.TemporaryDirectory() as raw:
            app=Workbench.__new__(Workbench);app.service=Mock()
            app.service.store.batch_dir.return_value=Path(raw)
            master=Path(raw)/'master.json';master.write_text('{"id":"legacy"}',encoding='utf-8')
            (Path(raw)/'batch-finalization.json').write_text(json.dumps({'master_snapshot':str(master),
                'master_draft':'draft','validation':{'status':'clear'}}),encoding='utf-8')
            snapshot,draft=app._export_snapshot('legacy')
            self.assertEqual((snapshot,draft),({'id':'legacy'},'draft'))


if __name__=='__main__':unittest.main()
