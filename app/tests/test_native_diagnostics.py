import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import nullcontext

from agent_core import native_diagnostics as diag
from agent_core.__main__ import _native_call


CONTEXT = {'authContext': {'principal': {'status': 'anonymous', 'userId': 'PRIVATE'},
                           'credential': {'cookie': 'PRIVATE'}},
           'hostContext': {'context': {'nativeHost': True, 'tdid': 'PRIVATE', 'pf': '4', 'appvr': '11.5'}}}


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.host = SimpleNamespace(output=Path(self.tmp.name), install=Path(self.tmp.name),
                                    report={'healthy_host_pid': 7}, lock=nullcontext(),
                                    job=SimpleNamespace(members=lambda: {7}), ensure_running=lambda: None)
        self.calls = []

    def run_probe(self, replies):
        def request(path, method='GET', body=None):
            self.calls.append((path, method, body))
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply
        client = SimpleNamespace(request=request)
        with patch.object(diag, 'OwnedBridge', return_value=client):
            result = diag.diagnose(self.host, attempts=2, interval=0)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(json.loads((self.host.output / 'native-diagnostics.json').read_text()), result)
        return result

    def test_login_and_startup_failures_are_separate_and_session_is_deleted(self):
        pending = (200, {'code': 1, 'data': {'running': False, 'starting': True, 'localServerToken': 'PRIVATE'}})
        result = self.run_probe(iter([(200, CONTEXT), (200, {'session_id': 'probe-1'}), pending, pending, (200, {})]))
        self.assertEqual(result['blockers'], ['official_login_required', 'native_server_startup_not_ready'])
        self.assertTrue(result['session_deleted'])
        self.assertEqual(len(result['server_observations']), 2)
        self.assertEqual(self.calls[-1], ('/infinite-canvas/session/probe-1', 'DELETE', None))

    def test_server_error_is_sanitized_and_cleanup_runs(self):
        result = self.run_probe(iter([(200, CONTEXT), (200, {'session_id': 'probe-1'}),
                                     RuntimeError('PRIVATE'), (200, {})]))
        self.assertEqual(result['reason'], 'native_diagnostic_failed')
        self.assertTrue(result['session_deleted'])

    def test_cleanup_failure_blocks_even_a_healthy_server(self):
        authenticated = {**CONTEXT, 'authContext': {'principal': {'status': 'authenticated'}}}
        with patch.object(diag, 'check_server', return_value={'status': 'healthy'}):
            result = self.run_probe(iter([(200, authenticated), (200, {'session_id': 'probe-1'}),
                (200, {'code': 1, 'data': {'running': True, 'starting': False}}), RuntimeError('PRIVATE')]))
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['reason'], 'native_session_cleanup_failed')
        self.assertFalse(result['resource_authorization_verified'])

    def test_unknown_auth_state_is_not_called_authenticated(self):
        context = {**CONTEXT, 'authContext': {'principal': {'status': 'PRIVATE'}}}
        result = diag.summarize_context(context)
        self.assertEqual(result['principal_status'], 'unknown')
        self.assertNotIn('PRIVATE', json.dumps(result))
        with self.assertRaises(diag.DiagnosticFailure):
            diag.summarize_context({'authContext': []})

    def test_unowned_server_receives_no_token(self):
        exe = self.host.install / 'bun.exe'
        exe.write_bytes(b'pinned')
        with patch.object(diag, 'BUN_SHA256', diag.hashlib.sha256(b'pinned').hexdigest()), \
             patch.object(diag.bridge, 'process_image', return_value=exe), \
             patch.object(diag.bridge, 'listener_owned', return_value=False), \
             patch.object(diag, 'request_json') as send:
            with self.assertRaisesRegex(diag.DiagnosticFailure, 'listener_not_owned'):
                diag.check_server(self.host, {'port': 9, 'localServerToken': 'PRIVATE'})
            send.assert_not_called()

    def test_changed_native_discovery_sends_no_request(self):
        client = object.__new__(diag.OwnedBridge)
        client.host, client.path = self.host, self.host.output / 'discovery'
        client.context = {'pid': 7, 'port': 9, 'token': 'PRIVATE'}
        with patch.object(diag.bridge, 'read_discovery', return_value={'pid': 8}), \
             patch.object(diag, 'request_json') as send:
            with self.assertRaisesRegex(diag.DiagnosticFailure, 'native_host_changed'):
                client.request('/custom-agent/context')
            send.assert_not_called()

    def test_unknown_jsb_reply_does_not_look_ready(self):
        result = self.run_probe(iter([(200, CONTEXT), (200, {'session_id': 'probe-1'}),
                                     (200, {'code': -2, 'msg': 'PRIVATE'}), (200, {})]))
        self.assertEqual(result['reason'], 'native_bootstrap_rejected')
        self.assertEqual(result['status'], 'blocked')

    def test_diagnose_does_not_start_without_explicit_cli_option(self):
        args = SimpleNamespace(isolated_host=False, host_out=None)
        with self.assertRaisesRegex(ValueError, 'requires --isolated-host'):
            _native_call(args, {}, lambda host: None, with_host=True)


if __name__ == '__main__':
    unittest.main()
