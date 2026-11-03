import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agent_core import native_host as host
from agent_core.__main__ import _native_call
from agent_core.isolated_desktop import child_environment


class FakeJob:
    def __init__(self):
        self.closed=False;self.job=1
        self.kernel=SimpleNamespace(TerminateJobObject=lambda *args:True)
    def start(self,*args):return 123
    def members(self):return {123}
    def inspect(self):
        return {'job_pids':[123],'input_desktop_unchanged':True,'input_window_count':0,'isolated_window_count':2}
    def close(self):
        self.closed=True
        return {'owned_processes_ended':True,'pending_owned_pids':[]}


class NativeHostTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.install=self.root/'install';self.install.mkdir()
        self.data=self.root/'data';self.index=self.data/'Projects/com.lveditor.draft/root_meta_info.json'
        self.index.parent.mkdir(parents=True);self.index.write_text('{"all_draft_store":[{"id":"keep"}]}')
        self.fakes=[]
        def factory():
            item=FakeJob();self.fakes.append(item);return item
        for target,opts in [('editor_pids',{'return_value':[]}),('signature_valid',{'return_value':True}),
                            ('DesktopJob',{'side_effect':factory})]:
            pin=patch.object(host,target,**opts);pin.start();self.addCleanup(pin.stop)
        pin=patch.object(host.bridge,'verify_host_image');pin.start();self.addCleanup(pin.stop)

    def make(self,**kwargs):
        return host.IsolatedNativeHost(self.install,self.root/'out',allow_isolated_host=True,
                                       user_data=self.data,startup_timeout=6,lifetime_timeout=10,**kwargs)

    def test_no_implicit_launch_or_output_without_opt_in(self):
        with self.assertRaisesRegex(host.HostFailure,'opt_in_required'):
            host.IsolatedNativeHost(self.install,self.root/'out')
        self.assertFalse((self.root/'out').exists());self.assertFalse(self.fakes)

    def test_existing_editor_or_bad_signature_never_creates_owned_job(self):
        for function,result,reason in [('editor_pids',[7],'existing_editor'),('signature_valid',False,'signature_invalid')]:
            with patch.object(host,function,return_value=result),self.assertRaisesRegex(host.HostFailure,reason):
                with self.make():pass
        self.assertFalse(self.fakes)

    def test_child_environment_drops_model_and_host_credentials(self):
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'PRIVATE','CUSTOM_AGENT_HOST_API_TOKEN':'PRIVATE',
                                    'LYRA_CLI_UNTRUSTED_SETTING':'PRIVATE','SYSTEMROOT':'C:/Windows'}):
            result=child_environment()
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertEqual(result['SYSTEMROOT'],'C:/Windows')

    def test_owned_health_checks_identity_before_sending_credentials(self):
        instance=self.make();instance.job=FakeJob()
        with patch.object(host.bridge,'read_discovery',return_value={'pid':789,'port':10,'token':'PRIVATE'}),\
                patch.object(host.bridge,'check') as check:
            self.assertEqual(instance._owned_health()['reason'],'discovery_not_owned_by_managed_host')
            check.assert_not_called()
        with patch.object(host.bridge,'read_discovery',return_value={'pid':123,'port':10,'token':'PRIVATE'}),\
                patch.object(host.bridge,'check',return_value={'status':'healthy','pid':123}) as check:
            instance._owned_health();self.assertEqual(check.call_args.kwargs,{'expected_pid':123})

    def test_exited_editor_does_not_count_as_an_unowned_live_host(self):
        instance=self.make();instance.job=FakeJob()
        with patch.object(host,'editor_pids',return_value=[123,789]),\
                patch.object(host.bridge,'process_image',side_effect=host.bridge.ConnectionFailure('discovery_stale')):
            instance._check_other_editors()
        with patch.object(host,'editor_pids',return_value=[123,789]),\
                patch.object(host.bridge,'process_image',return_value=self.install/'JianyingPro.exe'),\
                self.assertRaisesRegex(host.HostFailure,'unowned_editor_appeared'):
            instance._check_other_editors()

    def test_healthy_context_closes_job_after_operation_error_and_preserves_index(self):
        before=self.index.read_bytes();instance=self.make()
        with patch.object(instance,'_owned_health',return_value={'status':'healthy','pid':123}),\
                self.assertRaisesRegex(RuntimeError,'operation_failed'):
            with instance:raise RuntimeError('operation_failed')
        self.assertTrue(self.fakes[0].closed)
        self.assertEqual(self.index.read_bytes(),before)
        self.assertEqual((self.root/'out/root_meta_info.before.json').read_bytes(),before)
        report=json.loads((self.root/'out/host-lifecycle.json').read_text())
        self.assertEqual(report['status'],'closed_after_healthy_connection')
        self.assertEqual(len(report['health_observations']),2)
        self.assertFalse(report['resource_authorization_verified'])

    def test_index_change_is_reported_without_overwriting_user_state(self):
        instance=self.make()
        with patch.object(instance,'_owned_health',return_value={'status':'healthy','pid':123}),\
                self.assertRaisesRegex(host.HostFailure,'review_preserved_backup'):
            with instance:self.index.write_text('{"all_draft_store":[]}')
        self.assertEqual(json.loads(self.index.read_text())['all_draft_store'],[])
        self.assertEqual(instance.report['status'],'homepage_index_changed')
        self.assertTrue(self.fakes[0].closed)

    def test_health_timeout_cleans_job_without_restart(self):
        instance=host.IsolatedNativeHost(self.install,self.root/'out',allow_isolated_host=True,
                  user_data=self.data,startup_timeout=1,lifetime_timeout=3)
        with patch.object(instance,'_owned_health',return_value={'status':'blocked'}),\
                self.assertRaisesRegex(host.HostFailure,'health_timeout'):
            with instance:pass
        self.assertEqual(len(self.fakes),1);self.assertTrue(self.fakes[0].closed)
        self.assertFalse(instance.report['automatic_restart'])

    def test_monitor_isolation_failure_never_accepts_healthy_reply(self):
        instance=self.make()
        with patch.object(FakeJob,'inspect',return_value={'job_pids':[123],
              'input_desktop_unchanged':True,'input_window_count':1,'isolated_window_count':0}),\
              patch.object(instance,'_owned_health',return_value={'status':'healthy','pid':123}),\
              self.assertRaisesRegex(host.HostFailure,'desktop_isolation_lost'):
            with instance:pass
        self.assertTrue(self.fakes[0].closed)

    def test_cli_without_flag_never_constructs_host(self):
        args=SimpleNamespace(isolated_host=False,host_out=None)
        with patch.object(host,'IsolatedNativeHost') as constructor:
            self.assertEqual(_native_call(args,{},lambda:{'value':1}),{'value':1})
            constructor.assert_not_called()
        for args in (SimpleNamespace(isolated_host=True,host_out=None),
                     SimpleNamespace(isolated_host=False,host_out=str(self.root/'out'))):
            with self.assertRaises(ValueError):_native_call(args,{},lambda:{})

    def test_cli_rejects_backup_inside_source_draft(self):
        args=SimpleNamespace(isolated_host=True,host_out=str(self.root/'draft/host'),draft=str(self.root/'draft'))
        with self.assertRaisesRegex(ValueError,'outside the input draft'):
            _native_call(args,{},lambda:{})


if __name__=='__main__':unittest.main()
