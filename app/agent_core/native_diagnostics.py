"""Bounded official JSB bootstrap under an explicitly owned isolated host.

This diagnoses transport, login context and server startup separately. No model
job, resource purchase, UI action, draft mutation or authorization override.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import quote

from . import native_connection as bridge
from .task_store import atomic_json

BUN_SHA256 = 'a9769e3a0e68cc9f65c5374172060ba9766df330a5a2db0ac2480a036f67716b'


class DiagnosticFailure(ValueError):
    """Fixed, non-secret reasons only."""


def request_json(port, path, *, token, header, method='GET', body=None, timeout=3, max_bytes=bridge.MAX_BYTES, extra_headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        headers = {header: token}
        if extra_headers:
            headers.update(extra_headers)
        if body is not None:
            headers['content-type'] = 'application/json'
        connection.request(method, path, body=None if body is None else json.dumps(body), headers=headers)
        response = connection.getresponse()
        raw = response.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise DiagnosticFailure('response_too_large')
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise DiagnosticFailure('response_invalid') from None
        if not isinstance(payload, dict):
            raise DiagnosticFailure('response_invalid')
        return response.status, payload
    finally:
        connection.close()


def summarize_context(payload):
    """Allowlist only. Never return identifiers, cookies or credential bodies."""
    auth = payload.get('authContext')
    context = payload.get('hostContext')
    if not isinstance(auth, dict) or not isinstance(context, dict):
        raise DiagnosticFailure('official_context_invalid')
    principal, context = auth.get('principal'), context.get('context')
    if not isinstance(principal, dict) or not isinstance(context, dict):
        raise DiagnosticFailure('official_context_invalid')
    status = principal.get('status')
    if status not in ('anonymous', 'authenticated'):
        status = 'unknown'
    return {'principal_status': status, 'login_required': status == 'anonymous',
            'credential_present': isinstance(auth.get('credential'), dict),
            'native_host': context.get('nativeHost') is True,
            'signing_fields_present': {key: bool(context.get(key)) for key in ('tdid', 'pf', 'appvr')}}


class OwnedBridge:
    def __init__(self, host):
        self.host = host
        self.path = Path(os.environ['TEMP']) / bridge.DISCOVERY_NAME
        self.context = bridge.read_discovery(self.path)
        expected = host.report['healthy_host_pid']
        if self.context['pid'] != expected:
            raise DiagnosticFailure('native_host_changed')
        if bridge.check(host.install, expected_pid=expected)['status'] != 'healthy':
            raise DiagnosticFailure('native_host_unhealthy')

    def verify(self):
        self.host.ensure_running()
        with self.host.lock:
            owned = self.host.job.members()
        if self.context['pid'] not in owned or bridge.read_discovery(self.path) != self.context:
            raise DiagnosticFailure('native_host_changed')
        if not bridge.listener_owned(self.context['pid'], self.context['port']):
            raise DiagnosticFailure('native_listener_changed')

    def request(self, path, method='GET', body=None, extra_headers=None):
        self.verify()
        result = request_json(self.context['port'], path, token=self.context['token'],
                              header='x-infinite-canvas-native-token', method=method, body=body,
                              extra_headers=extra_headers)
        self.verify()
        return result


def check_server(host, data):
    port, token = data.get('port'), data.get('localServerToken')
    if (type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str)
            or not 1 <= len(token) <= 4096 or any(ord(ch) < 33 or ord(ch) > 126 for ch in token)):
        raise DiagnosticFailure('native_server_context_invalid')
    host.ensure_running()
    with host.lock:
        owned = host.job.members()
    expected = (host.install / 'bun.exe').resolve(strict=True)
    if hashlib.sha256(expected.read_bytes()).hexdigest() != BUN_SHA256:
        raise DiagnosticFailure('native_server_binary_changed')
    owners = []
    for pid in owned:
        try:
            if bridge.process_image(pid).resolve(strict=True) == expected and bridge.listener_owned(pid, port):
                owners.append(pid)
        except bridge.ConnectionFailure:
            continue
    if len(owners) != 1:
        raise DiagnosticFailure('native_server_listener_not_owned')
    status, payload = request_json(port, '/.agent-host/health', token=token, header='x-custom-agent-token')
    host.ensure_running()
    with host.lock:
        still_owned = owners[0] in host.job.members()
    if not still_owned or not bridge.listener_owned(owners[0], port):
        raise DiagnosticFailure('native_server_changed')
    if status != 200 or payload.get('ok') is not True or payload.get('app') != 'product-agent-host':
        raise DiagnosticFailure('native_server_health_rejected')
    return {'status': 'healthy', 'owner_pid': owners[0], 'resource_authorization_verified': False}


def diagnose(host, *, attempts=6, interval=2):
    """Requires the caller's managed host. Always delete the temporary JSB session."""
    if type(attempts) is not int or not 1 <= attempts <= 12 or not 0 <= interval <= 5:
        raise ValueError('invalid_diagnostic_budget')
    report = {'schema': 'jianying-native-diagnostics-v1', 'status': 'blocked',
              'gui_operated': False, 'credentials_disclosed': False, 'model_job_submitted': False,
              'resource_authorization_verified': False, 'native_export_verified': False,
              'server_observations': [], 'automatic_host_restart': False}
    client = None
    session_path = None
    try:
        client = OwnedBridge(host)
        status, payload = client.request('/custom-agent/context')
        if status != 200:
            raise DiagnosticFailure('official_context_rejected')
        report['official_context'] = summarize_context(payload)
        del payload
        status, payload = client.request('/infinite-canvas/session', 'POST', {})
        session = payload.get('session_id')
        if status != 200 or not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session):
            raise DiagnosticFailure('native_session_not_created')
        session_path = '/infinite-canvas/session/' + quote(session, safe='')
        report['session_created'] = True
        for index in range(attempts):
            status, payload = client.request(session_path + '/jsb', 'POST',
                                              {'method': 'getLocalAgentServerContext', 'params': {}})
            data = payload.get('data')
            if status != 200 or payload.get('code') != 1 or not isinstance(data, dict):
                raise DiagnosticFailure('native_bootstrap_rejected')
            if type(data.get('running')) is not bool or type(data.get('starting')) is not bool:
                raise DiagnosticFailure('native_bootstrap_response_invalid')
            observation = {'running': data['running'], 'starting': data['starting']}
            report['server_observations'].append(observation)
            if data['running'] and not data['starting']:
                report['native_server'] = check_server(host, data)
                break
            if not data['starting']:
                break
            if index < attempts - 1:
                time.sleep(interval)
        if 'native_server' not in report:
            report['native_server'] = {'status': 'blocked', 'reason': 'native_server_startup_not_ready'}
        blockers = []
        if report['official_context']['login_required']:
            blockers.append('official_login_required')
        if report['native_server']['status'] != 'healthy':
            blockers.append('native_server_startup_not_ready')
        report['blockers'] = blockers
        report['status'] = 'blocked' if blockers else 'transport_ready_features_unverified'
    except (DiagnosticFailure, bridge.ConnectionFailure) as exc:
        report['reason'] = str(exc)
    except Exception:
        # Native bodies and exception messages can contain credentials.
        report['reason'] = 'native_diagnostic_failed'
    finally:
        if client is not None and session_path:
            try:
                status, _ = client.request(session_path, 'DELETE')
                report['session_deleted'] = status == 200
            except Exception:
                report['session_deleted'] = False
            if not report['session_deleted']:
                report.update(status='blocked', reason='native_session_cleanup_failed')
        atomic_json(host.output / 'native-diagnostics.json', report)
    return report
