import json
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from unittest.mock import patch

from agent_core import native_connection as bridge


class NativeConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.file = self.root / bridge.DISCOVERY_NAME
        self.context = {'pid': 17, 'port': 23456, 'token': 'PRIVATE-BRIDGE-CREDENTIAL'}
        self.file.write_text(json.dumps(self.context), encoding='utf-8')

    def run_check(self, **kwargs):
        return bridge.check(self.root, discovery_path=self.file, **kwargs)

    def test_missing_or_invalid_discovery_never_contacts_listener(self):
        with patch.object(bridge, 'request_health') as request:
            for value in ([], {}, {**self.context, 'pid': True},
                          {**self.context, 'port': 65536}, {**self.context, 'token': 'bad\r\nheader'}):
                self.file.write_text(json.dumps(value))
                self.assertEqual(self.run_check()['reason'], 'discovery_invalid')
            self.file.unlink()
            self.assertEqual(self.run_check()['reason'], 'discovery_missing')
            request.assert_not_called()

    def test_stale_pid_does_not_send_token(self):
        with patch.object(bridge, 'process_image', side_effect=bridge.ConnectionFailure('discovery_stale')), \
             patch.object(bridge, 'request_health') as request:
            report = self.run_check()
            self.assertEqual(report['reason'], 'discovery_stale')
            self.assertFalse(report['auto_retry'])
            request.assert_not_called()

    def test_unexpected_pid_is_rejected_before_using_token(self):
        with patch.object(bridge,'request_health') as request,patch.object(bridge,'process_image') as inspect:
            self.assertEqual(self.run_check(expected_pid=18)['reason'],'unexpected_host_pid')
            request.assert_not_called();inspect.assert_not_called()

    def test_wrong_image_or_port_owner_cannot_receive_token(self):
        with patch.object(bridge, 'process_image', return_value=Path('wrong.exe')), \
             patch.object(bridge, 'verify_host_image', side_effect=bridge.ConnectionFailure('host_identity_mismatch')), \
             patch.object(bridge, 'request_health') as request:
            self.assertEqual(self.run_check()['reason'], 'host_identity_mismatch')
            request.assert_not_called()
        with patch.object(bridge, 'process_image', return_value=Path('JianyingPro.exe')), \
             patch.object(bridge, 'verify_host_image'), \
             patch.object(bridge, 'listener_owned', return_value=False), \
             patch.object(bridge, 'request_health') as request:
            self.assertEqual(self.run_check()['reason'], 'listener_owner_mismatch_or_not_listening')
            request.assert_not_called()

    def test_healthy_is_not_entitlement_or_headless_launcher(self):
        with patch.object(bridge, 'process_image', return_value=Path('JianyingPro.exe')), \
             patch.object(bridge, 'verify_host_image'), \
             patch.object(bridge, 'listener_owned', return_value=True), \
             patch.object(bridge, 'request_health') as request:
            report = self.run_check()
            request.assert_called_once_with(self.context['port'], self.context['token'])
            self.assertEqual(report['status'], 'healthy')
            self.assertFalse(report['resource_authorization_verified'])
            self.assertFalse(report['headless_host_launcher_verified'])
            self.assertNotIn('PRIVATE', json.dumps(report))

    def test_health_failure_does_not_disclose_exception_or_auto_retry(self):
        with patch.object(bridge, 'process_image', return_value=Path('JianyingPro.exe')), \
             patch.object(bridge, 'verify_host_image'), \
             patch.object(bridge, 'listener_owned', return_value=True), \
             patch.object(bridge, 'request_health', side_effect=OSError(self.context['token'])) as request:
            report = self.run_check()
            self.assertEqual(report['status'], 'blocked')
            self.assertFalse(report['auto_retry'])
            self.assertNotIn(self.context['token'], json.dumps(report))
            request.assert_called_once()

    def test_discovery_change_prevents_stale_credential_use(self):
        with patch.object(bridge, 'process_image', return_value=Path('JianyingPro.exe')), \
             patch.object(bridge, 'verify_host_image'), \
             patch.object(bridge, 'listener_owned', return_value=True), \
             patch.object(bridge, 'read_discovery', side_effect=[self.context, {**self.context, 'pid': 18}]), \
             patch.object(bridge, 'request_health') as request:
            self.assertEqual(self.run_check()['reason'], 'discovery_changed')
            request.assert_not_called()

    def test_tcp_owner_table_byte_order_and_truncation(self):
        address, = struct.unpack('<I', socket.inet_aton('127.0.0.1'))
        raw = struct.pack('<7I', 1, 2, address, socket.htons(23456), 0, 0, 17)
        self.assertEqual(bridge.decode_tcp_table(raw), [
            {'state': 2, 'ip': '127.0.0.1', 'port': 23456, 'pid': 17}])
        for malformed in (b'', raw[:-1], struct.pack('<I', 0xffffffff)):
            with self.assertRaises(bridge.ConnectionFailure):
                bridge.decode_tcp_table(malformed)

    def test_wrong_hash_fails_even_with_correct_path(self):
        path = self.root / 'JianyingPro.exe'
        path.write_bytes(b'unreviewed-binary-not-executed')
        with self.assertRaisesRegex(bridge.ConnectionFailure, 'host_identity_mismatch'):
            bridge.verify_host_image(path, self.root)
