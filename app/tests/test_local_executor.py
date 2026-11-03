from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from agent_core.local_executor import LocalExecutor, RecoveryRequired, ToolFailed, JianyingAdapter
from agent_core.task_store import TaskStore


class LocalExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.scripts = self.root / 'scripts'; self.scripts.mkdir()
        self.store = TaskStore(self.root / 'state')
        self.job = self.store.put_job({'schema_version': '1.0'})
        self.executor = LocalExecutor(sys.executable, self.scripts, self.root,
                                      self.store, self.job)
    def tearDown(self):
        self.temp.cleanup()

    def test_completed_report_is_recovered_without_second_process(self):
        script = self.scripts / 'native_preview.py'
        script.write_text('''
import json, pathlib, sys
out = pathlib.Path(sys.argv[1]); out.mkdir()
counter = out.parent / 'counter.txt'
count = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(count))
(out / 'preview-report.json').write_text(json.dumps({'status': 'native_frames_captured', 'count': count}))
''', encoding='utf-8')
        output = self.store.job_dir(self.job) / 'test-preview'
        first = self.executor.run('render_preview', 'native_preview.py', [output],
                                  output_dir=output, report_file=output / 'preview-report.json',
                                  timeout_seconds=10)
        second = self.executor.run('render_preview', 'native_preview.py', [output],
                                   output_dir=output, report_file=output / 'preview-report.json',
                                   timeout_seconds=10)
        self.assertEqual(first, second)
        self.assertEqual((output.parent / 'counter.txt').read_text(), '1')

    def test_partial_output_is_not_replayed(self):
        script = self.scripts / 'native_preview.py'
        script.write_text('# fixture', encoding='utf-8')
        output = self.store.job_dir(self.job) / 'partial'; output.mkdir()
        with self.assertRaises(RecoveryRequired):
            self.executor.run('render_preview', 'native_preview.py', [output],
                              output_dir=output, report_file=output / 'preview-report.json')

    def test_internal_packaging_uses_its_recovery_and_revalidates_completed_work(self):
        script=self.scripts/'native_internal_packaging.py'
        script.write_text('''
import json,pathlib,sys
out=pathlib.Path(sys.argv[1]);out.mkdir(exist_ok=True)
counter=out/'checks';count=int(counter.read_text())+1 if counter.exists() else 1
counter.write_text(str(count))
if count==1:
 (out/'packaging-report.json').write_text(json.dumps({'status':'needs_resume_or_plan_revision'}))
 sys.exit(2)
(out/'packaging-report.json').write_text(json.dumps({'status':'native_packaging_ready_for_review','delivery_accepted':False}))
''',encoding='utf-8')
        out=self.store.job_dir(self.job)/'native-package'
        def call():return self.executor.run('package_internal',script.name,[out],output_dir=out,
            report_file=out/'packaging-report.json',timeout_seconds=10)
        with self.assertRaises(ToolFailed):call()
        self.assertFalse(call()['delivery_accepted'])
        with patch('agent_core.native_packaging.validate_completed',return_value={'status':'native_packaging_ready_for_review'}) as check:
            call();check.assert_called_once_with(out)
        self.assertEqual((out/'checks').read_text(),'2')
        with self.assertRaises(RecoveryRequired):
            self.executor.run('package_internal',script.name,[out,'changed-plan'],output_dir=out,
                report_file=out/'packaging-report.json',timeout_seconds=10)

    def test_internal_packaging_needs_host_opt_in(self):
        with self.assertRaisesRegex(ValueError,'authorization'):
            JianyingAdapter(self.executor).package_internal('draft','plan','out')

    def test_registration_recovery_is_specific_to_evidence_checked_adapter(self):
        script=self.scripts/'native_review_register.py'
        script.write_text('''
import json,pathlib,sys
out=pathlib.Path(sys.argv[1]);out.mkdir(exist_ok=True)
counter=out/'attempts';n=int(counter.read_text())+1 if counter.exists() else 1;counter.write_text(str(n))
if n==1:sys.exit(2)
(out/'registration-report.json').write_text(json.dumps({'status':'native_draft_registered'}))
''',encoding='utf-8')
        out=self.store.job_dir(self.job)/'register'
        def run():return self.executor.run('register_native_review',script.name,[out],output_dir=out,
            report_file=out/'registration-report.json',timeout_seconds=10)
        with self.assertRaises(ToolFailed):run()
        self.assertEqual(run()['status'],'native_draft_registered')
        with patch('agent_core.native_review_registration.validate_registered',return_value={'checked':True}) as check:
            self.assertTrue(run()['checked']);check.assert_called_once_with(out)
        self.assertEqual((out/'attempts').read_text(),'2')
