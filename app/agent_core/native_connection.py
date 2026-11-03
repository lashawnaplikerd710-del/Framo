"""Connect to the installed official bridge without launching an editor window.

The discovery token is an official local IPC credential. It is used only after
the PID, pinned executable and listening-port owner match. It never enters a
report. Health is not resource authorization and cannot unlock resource policy.
"""
from __future__ import annotations

import ctypes
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import struct

HOST_SHA256 = 'f77353701efd7bc3e010b054e61a48ab78cc2f4413407109989fe8447cf622cc'
DISCOVERY_NAME = 'infinite-canvas-native-bridge.json'
HEALTH_PATH = '/.infinite-canvas-native-bridge/health'
MAX_BYTES = 65536


class ConnectionFailure(ValueError):
    """Fixed public reason only; never include request headers or server bodies."""


def read_discovery(path):
    path = Path(path)
    if not path.is_file():
        raise ConnectionFailure('discovery_missing')
    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
        raise ConnectionFailure('discovery_invalid')
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, UnicodeError):
        raise ConnectionFailure('discovery_invalid') from None
    if not isinstance(data, dict):
        raise ConnectionFailure('discovery_invalid')
    pid, port, token = (data.get(k) for k in ('pid', 'port', 'token'))
    if (type(pid) is not int or not 0 < pid <= 0xffffffff or
            type(port) is not int or not 1 <= port <= 65535 or
            not isinstance(token, str) or not 1 <= len(token) <= 4096 or
            any(ord(c) < 33 or ord(c) > 126 for c in token)):
        raise ConnectionFailure('discovery_invalid')
    return {'pid': pid, 'port': port, 'token': token}


def process_image(pid):
    if os.name != 'nt':
        raise ConnectionFailure('windows_required')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                               ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        raise ConnectionFailure('discovery_stale' if error == 87 else 'process_unavailable')
    try:
        code = ctypes.c_uint32()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            raise ConnectionFailure('process_unavailable')
        if code.value != 259:
            raise ConnectionFailure('discovery_stale')
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_uint32(len(buf))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            raise ConnectionFailure('process_unavailable')
        return Path(buf.value)
    finally:
        kernel.CloseHandle(handle)


def decode_tcp_table(raw):
    """Windows MIB_TCPTABLE_OWNER_PID: count followed by six DWORDs per row."""
    if len(raw) < 4:
        raise ConnectionFailure('listener_inspection_failed')
    count, = struct.unpack_from('<I', raw)
    if count > 100000 or 4 + count * 24 > len(raw):
        raise ConnectionFailure('listener_inspection_failed')
    rows = []
    for offset in range(4, 4 + count * 24, 24):
        state, addr, port, _, _, pid = struct.unpack_from('<6I', raw, offset)
        rows.append({'state': state, 'ip': socket.inet_ntoa(struct.pack('<I', addr)),
                     'port': socket.ntohs(port & 0xffff), 'pid': pid})
    return rows


def listener_owned(pid, port):
    if os.name != 'nt':
        raise ConnectionFailure('windows_required')
    library = ctypes.WinDLL('iphlpapi', use_last_error=True)
    get_table = library.GetExtendedTcpTable
    get_table.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
                         ctypes.c_int, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    get_table.restype = ctypes.c_uint32
    size = ctypes.c_uint32()
    # AF_INET, TCP_TABLE_OWNER_PID_ALL. A bounded retry handles table growth.
    code = get_table(None, ctypes.byref(size), False, 2, 5, 0)
    for _ in range(3):
        if code not in (0, 122) or not 4 <= size.value <= 4 * 1024 * 1024:
            raise ConnectionFailure('listener_inspection_failed')
        buf = ctypes.create_string_buffer(size.value)
        code = get_table(buf, ctypes.byref(size), False, 2, 5, 0)
        if code == 0:
            rows = decode_tcp_table(buf.raw[:size.value])
            return any(row['state'] == 2 and row['ip'] in ('127.0.0.1', '0.0.0.0') and
                       row['port'] == port and row['pid'] == pid for row in rows)
    raise ConnectionFailure('listener_inspection_failed')


def verify_host_image(path, install):
    expected = (Path(install) / 'JianyingPro.exe').resolve(strict=True)
    actual = Path(path).resolve(strict=True)
    if actual != expected or hashlib.sha256(actual.read_bytes()).hexdigest() != HOST_SHA256:
        raise ConnectionFailure('host_identity_mismatch')


def request_health(port, token):
    # http.client uses no proxy and follows no redirect. The destination is fixed.
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    try:
        connection.request('GET', HEALTH_PATH, headers={'x-infinite-canvas-native-token': token})
        response = connection.getresponse()
        if response.status != 200:
            raise ConnectionFailure('health_rejected')
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ConnectionFailure('health_invalid')
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise ConnectionFailure('health_invalid') from None
        if not isinstance(payload, dict) or payload.get('ok') is not True:
            raise ConnectionFailure('health_invalid')
    finally:
        connection.close()


def check(install, *, discovery_path=None, health=True, expected_pid=None):
    """Read-only bounded connection check. Never start/kill/reconfigure a host."""
    report = {'schema_version': '1.0', 'provider': 'installed_jianying_native_bridge',
              'build': '11.5.0.14471', 'status': 'blocked', 'health_requested': False,
              'host_identity_verified': False, 'listener_owner_verified': False,
              'resource_authorization_verified': False, 'gui_started': False,
              'headless_host_launcher_verified': False, 'credentials_disclosed': False,
              'auto_retry': False}
    path = Path(discovery_path) if discovery_path is not None else Path(
        os.environ.get('TEMP', '')) / DISCOVERY_NAME
    try:
        context = read_discovery(path)
        report['pid'] = context['pid']
        if expected_pid is not None and context['pid'] != expected_pid:
            raise ConnectionFailure('unexpected_host_pid')
        image = process_image(context['pid'])
        verify_host_image(image, install)
        report['host_identity_verified'] = True
        if not listener_owned(context['pid'], context['port']):
            raise ConnectionFailure('listener_owner_mismatch_or_not_listening')
        report['listener_owner_verified'] = True
        if not health:
            return {**report, 'status': 'identity_verified', 'reason': 'health_not_requested'}
        # Re-read before sending a token, then verify state again after the call.
        if read_discovery(path) != context:
            raise ConnectionFailure('discovery_changed')
        report['health_requested'] = True
        request_health(context['port'], context['token'])
        if read_discovery(path) != context or process_image(context['pid']) != image:
            raise ConnectionFailure('connection_changed')
        if not listener_owned(context['pid'], context['port']):
            raise ConnectionFailure('connection_changed')
        return {**report, 'status': 'healthy', 'reason': 'resource_authorization_not_yet_verified'}
    except ConnectionFailure as exc:
        report['reason'] = str(exc)
    except (OSError, ValueError, http.client.HTTPException):
        # Exception messages can contain tokens or server content; never report them.
        report['reason'] = 'connection_check_failed'
    report['recovery'] = 'Recheck after an official host connection changes; this check never launches or restarts a host.'
    return report
