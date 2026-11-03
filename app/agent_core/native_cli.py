"""Execute the installed CLI through the real app-owned official runner.

The local service credential comes from its native JSB response, remains in
memory, and is sent only to the owned loopback listener. This is not a token
generator, credential importer, account login or alternative renderer.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time
import uuid
from urllib.parse import quote

from . import native_connection as bridge
from .native_diagnostics import DiagnosticFailure, OwnedBridge, check_server, request_json, summarize_context
from .isolated_desktop import child_environment
from .internal_resources import LYRA_SHA256


def command_line(arguments):
    if (not isinstance(arguments, (list, tuple)) or not arguments or len(arguments) > 100 or
            (arguments[0] not in ('material', 'draft', 'auth', '--help', '--version') and
             not (arguments[0] == 'media' and arguments[-1] == '--help'))):
        raise ValueError('unsupported_native_cli_domain')
    for value in arguments:
        if not isinstance(value, str) or len(value) > 16000 or any(ord(c) < 32 for c in value) or '\\' in value:
            raise ValueError('native_cli_arguments_require_printable_text_and_forward_slash_paths')
    if arguments[0] == 'auth':
        if len(arguments) != 4 or arguments[1] != 'login' or arguments[2] != '--credential-json':
            raise ValueError('only_official_lyra_auth_login_is_supported')
        try:
            value = json.loads(arguments[3])
        except (ValueError, TypeError):
            raise ValueError('lyra_auth_credential_json_invalid') from None
        if not isinstance(value, dict) or not isinstance(value.get('cookie'), str) or not value['cookie']:
            raise ValueError('lyra_auth_credential_json_invalid')
    return 'lyra-cli ' + ' '.join(shlex.quote(value) for value in arguments)


class NativeCLI:
    def __init__(self, host):
        self.host = host
        self.bridge = None
        self.session = None
        self.port = self.token = self.pid = None
        self.closed = False

    def __enter__(self):
        try:
            self.bridge = OwnedBridge(self.host)
            status, payload = self.bridge.request('/custom-agent/context')
            context = summarize_context(payload) if status == 200 else {}
            if context.get('principal_status') != 'authenticated':
                raise DiagnosticFailure('official_login_required')
            status, payload = self.bridge.request('/infinite-canvas/session', 'POST', {})
            self.session_payload = payload
            value = payload.get('session_id')
            if status != 200 or not isinstance(value, str) or not value or len(value) > 128:
                raise DiagnosticFailure('native_session_not_created')
            self.session = '/infinite-canvas/session/' + quote(value, safe='')
            for attempt in range(10):
                status, payload = self.bridge.request(self.session+'/jsb', 'POST',
                    {'method':'getLocalAgentServerContext', 'params':{}})
                data = payload.get('data')
                if status != 200 or payload.get('code') != 1 or not isinstance(data, dict):
                    raise DiagnosticFailure('native_bootstrap_rejected')
                if data.get('running') is True and data.get('starting') is False:
                    self.pid = check_server(self.host, data)['owner_pid']
                    self.port, self.token = data['port'], data['localServerToken']
                    break
                if attempt < 9:
                    time.sleep(1)
            else:
                raise DiagnosticFailure('native_server_startup_not_ready')
            status, payload = self.request('/api/custom-agent/diagnostics/host-environment', timeout=20)
            commands = payload.get('cliCommands', [])
            actual = next((v.get('executablePath') for v in commands if isinstance(v, dict) and v.get('id') == 'lyra-cli'), None)
            expected = (self.host.install / 'lyra-cli.exe').resolve(strict=True)
            if (status != 200 or not isinstance(actual, str) or Path(actual).resolve(strict=True) != expected or
                    hashlib.sha256(expected.read_bytes()).hexdigest() != LYRA_SHA256):
                raise DiagnosticFailure('official_cli_resolution_unverified')
            return self
        except BaseException:
            self.close()
            raise

    def verify(self):
        if self.closed or self.pid is None:
            raise DiagnosticFailure('native_cli_not_open')
        self.bridge.verify()
        with self.host.lock:
            owned = self.pid in self.host.job.members()
        if not owned or not bridge.listener_owned(self.pid, self.port):
            raise DiagnosticFailure('native_server_changed')

    def request(self, path, *, method=None, body=None, timeout=5, extra_headers=None):
        self.verify()
        result = request_json(self.port, path, token=self.token, header='x-custom-agent-token',
            method=method or ('POST' if body is not None else 'GET'), body=body, timeout=timeout, max_bytes=1024*1024,
            extra_headers=extra_headers)
        self.verify()
        return result

    def run(self, arguments, *, timeout=20):
        if type(timeout) not in (int, float) or not 1 <= timeout <= 120:
            raise ValueError('invalid_native_command_timeout')
        command = command_line(arguments)
        status, payload = self.request('/api/custom-agent/diagnostics/execute', timeout=timeout+5,
            body={'commandLine':command, 'cwd':self.host.install.as_posix(),
                  'sandboxed':False, 'timeoutMs':int(timeout*1000)})
        if status != 200:
            raise DiagnosticFailure('official_runner_rejected')
        if payload.get('timedOut') is True:
            raise subprocess.TimeoutExpired('official_native_cli', timeout)
        code = payload.get('exitCode')
        if type(code) is not int or not isinstance(payload.get('stdout'), str):
            raise DiagnosticFailure('official_runner_response_invalid')
        return subprocess.CompletedProcess(['lyra-cli', *arguments], code,
            stdout=payload['stdout'].encode('utf-8'), stderr=b'')

    def sync_lyra_auth(self):
        """Sync the already authenticated Jianying host context into Lyra.

        The credential is read from the app-owned context and passed only in
        memory to the official CLI login command. It is never written to a
        report, checkpoint, environment variable, or command log.
        """
        resource_id = 'tts-auth-' + uuid.uuid4().hex
        status, payload = self.request('/api/agent-runtime/v1/instances:resolve', timeout=20,
            body={'pluginId':'custom-agent','resource':{'id':resource_id,'type':'canvas'},
                  'retention':{'mode':'keep_alive','idleTtlMs':300000}})
        instance_id = (payload.get('instance') or {}).get('instanceId') if isinstance(payload,dict) else None
        if status not in (200,201) or not isinstance(instance_id,str) or not instance_id:
            raise DiagnosticFailure('native_runtime_instance_not_created')
        base='/api/agent-runtime/v1/instances/'+quote(instance_id,safe='')
        status, payload = self.request(base+'/leases', timeout=20,
            body={'ownerId':'jianying-agent-tts','ttlMs':30000})
        lease_id=(payload.get('lease') or {}).get('leaseId') if isinstance(payload,dict) else None
        if status not in (200,201) or not isinstance(lease_id,str) or not lease_id:
            raise DiagnosticFailure('native_runtime_lease_not_created')
        try:
            status, payload = self.request(base+'/sessions?includeArchived=false&leaseId='+quote(lease_id,safe=''), timeout=20)
            rows=payload.get('items',[]) if isinstance(payload,dict) else []
            session_id=rows[0].get('sessionId') if rows else None
            if not session_id:
                status, payload = self.request(base+'/sessions', timeout=20,
                    body={'title':'TTS修复','restoreSessionId':'default'})
                session_id=payload.get('sessionId') if isinstance(payload,dict) else None
            if not isinstance(session_id,str) or not session_id:
                raise DiagnosticFailure('native_runtime_session_not_created')
            status, _ = self.request(base+'/leases/'+quote(lease_id,safe='')+'/selected-session', method='PUT',
                body={'sessionId':session_id}, timeout=20)
            if status != 200:
                raise DiagnosticFailure('native_runtime_session_not_selected')
            status, payload = self.request('/api/custom-agent/infra/auth/start', timeout=90,
                body={'providerId':'provider.local.lyra','authRef':'auth.lyra',
                      'authRequestId':'tts-auth-'+uuid.uuid4().hex},
                extra_headers={'x-agent-runtime-instance-id':instance_id,
                               'x-agent-runtime-session-id':session_id})
            if status != 200 or not isinstance(payload,dict) or payload.get('status') != 'authorized':
                raise DiagnosticFailure('native_lyra_auth_sync_failed')
            return {'status':'authorized','runtime_auth_service':'app_owned'}
        finally:
            try:
                self.request(base+'/leases/'+quote(lease_id,safe=''), method='DELETE', timeout=20)
            except Exception:
                pass

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.token = None
        if self.bridge and self.session:
            path, self.session = self.session, None
            status, _ = self.bridge.request(path, 'DELETE')
            if status != 200:
                raise DiagnosticFailure('native_session_cleanup_failed')

    def __exit__(self, kind, value, traceback):
        self.close()
