import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from agent_core import native_export as adapter


class NativeExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.install=self.root/'install';self.install.mkdir()
        (self.install/'lyra-cli.exe').write_bytes(b'fixture-not-executable')
        pin=patch.object(adapter,'LYRA_SHA256',hashlib.sha256(b'fixture-not-executable').hexdigest())
        pin.start();self.addCleanup(pin.stop)
        bridge=patch.object(adapter,'check',return_value={'status':'blocked','reason':'discovery_missing'})
        bridge.start();self.addCleanup(bridge.stop)
        self.source=self.root/'source';self.source.mkdir()
        (self.source/'draft_content.json').write_bytes(b'original-encrypted-draft')

    def test_unauthorized_host_has_no_fallback_no_leaked_output_and_preserves_input(self):
        runner=SimpleNamespace(returncode=1,stdout=b'{"err_code":-100015,"err_msg":"SECRET"}',stderr=b'SECRET')
        with patch.object(adapter.subprocess,'run',return_value=runner) as call,\
                patch.dict(os.environ,{'DEEPSEEK_API_KEY':'SECRET','UNRELATED_TOKEN':'SECRET'}):
            report=adapter.export(self.install,self.source,self.root/'out')
        self.assertEqual(report['status'],'HOST_AUTH_REQUIRED')
        self.assertFalse(report['native_export_verified']);self.assertFalse(report['automatic_retry'])
        self.assertNotIn('SECRET',json.dumps(report))
        self.assertTrue(report['source_preserved']);self.assertFalse(report['output_exists'])
        call.assert_called_once();args=call.call_args
        self.assertNotIn('shell',args.kwargs)
        self.assertNotIn('DEEPSEEK_API_KEY',args.kwargs['env'])
        self.assertNotIn('UNRELATED_TOKEN',args.kwargs['env'])
        self.assertEqual(args.args[0][1:5],['draft','export','--ref','local://draft-copy'])
        self.assertEqual(args.kwargs['env']['LYRA_CLI_ROOT_WORKSPACE'],str(self.root/'out'))

    def test_cli_success_without_mp4_never_accepted(self):
        with patch.object(adapter.subprocess,'run',return_value=SimpleNamespace(
                returncode=0,stdout=b'{"err_code":0}',stderr=b'')):
            report=adapter.export(self.install,self.source,self.root/'out')
        self.assertEqual(report['status'],'invalid_export')
        self.assertFalse(report['native_export_verified']);self.assertFalse(report['delivery_accepted'])

    def test_timeout_keeps_report_does_not_retry(self):
        with patch.object(adapter.subprocess,'run',side_effect=adapter.subprocess.TimeoutExpired('lyra',55)) as call:
            report=adapter.export(self.install,self.source,self.root/'out')
        self.assertEqual(report['status'],'export_timeout');call.assert_called_once()
        self.assertTrue((self.root/'out/native-export-report.json').is_file())

    def test_existing_destination_and_untrusted_binary_rejected_before_process(self):
        with patch.object(adapter.subprocess,'run') as call:
            with self.assertRaises(ValueError):adapter.export(self.install,self.source,self.source)
            (self.install/'lyra-cli.exe').write_bytes(b'changed')
            with self.assertRaises(ValueError):adapter.export(self.install,self.source,self.root/'out')
            call.assert_not_called()
        self.assertFalse((self.root/'out').exists())

    def test_authenticated_missing_command_is_not_reported_as_auth_failure(self):
        seen=[]
        def native_runner(args,**kwargs):
            seen.append(args)
            return SimpleNamespace(returncode=2,stdout=b'{"err_code":-100003,"err_msg":"ExtrasError: export"}')
        with patch.object(adapter.subprocess,'run') as standalone:
            report=adapter.export(self.install,self.source,self.root/'out',runner=native_runner)
        self.assertEqual(seen,[['draft','export','--help']]);standalone.assert_not_called()
        self.assertEqual(report['status'],'EXPORT_COMMAND_UNAVAILABLE')
        self.assertTrue(report['source_preserved'])
        self.assertFalse(report['native_export_verified']);self.assertFalse(report['output_exists'])
