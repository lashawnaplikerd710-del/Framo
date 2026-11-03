from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from agent_core import native_cli as cli


class NativeCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'lyra-cli.exe').write_bytes(b'pinned')
        self.host = SimpleNamespace(install=self.root, lock=nullcontext(),
            job=SimpleNamespace(members=lambda:{10,20}))
        self.native = Mock()
        self.native.request.side_effect = [
            (200, {'authContext':{'principal':{'status':'authenticated'}},'hostContext':{'context':{'nativeHost':True}}}),
            (200, {'session_id':'test-session'}),
            (200, {'code':1,'data':{'running':True,'starting':False,'port':99,'localServerToken':'PRIVATE'}}),
            (200, {}),
        ]
        for target, options in [
            ('OwnedBridge', {'return_value':self.native}),
            ('check_server', {'return_value':{'owner_pid':20}}),
            ('LYRA_SHA256', {'new':hashlib.sha256(b'pinned').hexdigest()}),
        ]:
            p=patch.object(cli,target,**options);p.start();self.addCleanup(p.stop)
        p=patch.object(cli.bridge,'listener_owned',return_value=True);p.start();self.addCleanup(p.stop)
        self.environment=(200,{'cliCommands':[{'id':'lyra-cli','executablePath':str(self.root/'lyra-cli.exe')}]})

    def test_real_runner_scope_no_context_fabrication_and_session_cleanup(self):
        with patch.object(cli,'request_json',side_effect=[self.environment,
                (200,{'exitCode':0,'stdout':'{"err_code":0,"result":{"items":[]}}','timedOut':False})]) as send:
            with cli.NativeCLI(self.host) as client:
                result=client.run(['material','search','--prompt','宠物 "小碗"; literal','--type','sticker'])
                self.assertEqual(result.returncode,0)
                body=send.call_args.kwargs['body']
                self.assertEqual(shlex.split(body['commandLine'])[4], '宠物 "小碗"; literal')
                self.assertNotIn('PRIVATE',json.dumps(body))
                self.assertEqual(send.call_args.kwargs['token'],'PRIVATE')
            self.assertIsNone(client.token)
        self.native.request.assert_called_with('/infinite-canvas/session/test-session','DELETE')

    def test_login_failure_never_bootstraps_server(self):
        self.native.request.side_effect=[(200, {'authContext':{'principal':{'status':'anonymous'}},'hostContext':{'context':{}}})]
        with patch.object(cli,'request_json') as send, self.assertRaisesRegex(cli.DiagnosticFailure,'official_login_required'):
            with cli.NativeCLI(self.host):pass
        send.assert_not_called();self.assertEqual(self.native.request.call_count,1)

    def test_cli_path_mismatch_closes_session_before_any_command(self):
        with patch.object(cli,'request_json',return_value=(200,{'cliCommands':[{'id':'lyra-cli','executablePath':str(self.root)}]})), \
             self.assertRaisesRegex(cli.DiagnosticFailure,'resolution_unverified'):
            with cli.NativeCLI(self.host):pass
        self.native.request.assert_called_with('/infinite-canvas/session/test-session','DELETE')

    def test_changed_server_never_receives_token(self):
        with patch.object(cli,'request_json',return_value=self.environment) as send:
            with cli.NativeCLI(self.host) as client:
                with patch.object(cli.bridge,'listener_owned',return_value=False), \
                     self.assertRaisesRegex(cli.DiagnosticFailure,'native_server_changed'):
                    client.run(['material','--help'])
            self.assertEqual(send.call_count,1)

    def test_timeout_and_malformed_result_do_not_return_success(self):
        for payload, exception in [({'timedOut':True,'stdout':'PRIVATE'},subprocess.TimeoutExpired),
                                   ({'exitCode':None,'stdout':'PRIVATE'},cli.DiagnosticFailure)]:
            client=cli.NativeCLI(self.host);client.closed=False
            with patch.object(client,'request',return_value=(200,payload)), self.assertRaises(exception):
                client.run(['material','--help'])

    def test_unrelated_commands_and_windows_escape_paths_are_rejected(self):
        for args in (['auth','login'], ['media','compress'], ['draft','--ref',r'local://C:\draft']):
            with self.assertRaises(ValueError):cli.command_line(args)
        self.assertEqual(cli.command_line(['media','--help']), 'lyra-cli media --help')


if __name__=='__main__':unittest.main()
