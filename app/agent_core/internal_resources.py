"""Read-only official resource discovery, with precise host/auth failure reporting.

Search success alone never authorizes download/use. No credentials are created,
imported from another process, or written to reports; no GUI launcher is used.
"""
from __future__ import annotations
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess

LYRA_SHA256 = '8db519e796a17d074fbe071adb5635b8a069c9c9629bbefb056a3ba6eb24d87f'
RESOURCE_TYPES = ('music', 'sound', 'sticker', 'effect', 'filter', 'transition', 'text_template', 'font')


def bridge_status():
    """Inspect the documented discovery file, never expose or use its token."""
    path = Path(os.environ.get('TEMP', '')) / 'infinite-canvas-native-bridge.json'
    result = {'discovery_exists': path.is_file(), 'gui_started': False,
              'credentials_printed': False, 'health_requested': False}
    if not path.is_file():
        return {**result, 'status': 'missing'}
    try:
        if path.stat().st_size > 65536:
            raise ValueError('oversized discovery')
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        pid = data.get('pid')
        if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
            raise ValueError('invalid discovery PID')
        result.update(pid=pid, token_present=bool(data.get('token')))
        if os.name != 'nt':
            return {**result, 'status': 'windows_required'}
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                     ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            error = ctypes.get_last_error()
            return {**result, 'status': 'stale' if error == 87 else 'process_unavailable',
                    'win32_error': error}
        try:
            buf = ctypes.create_unicode_buffer(32768); length = ctypes.c_ulong(len(buf))
            if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(length)):
                return {**result, 'status': 'process_unavailable'}
            return {**result, 'status': 'process_exists_not_health_verified', 'process_image': buf.value}
        finally:
            kernel.CloseHandle(handle)
    except (OSError, ValueError, TypeError):
        return {**result, 'status': 'invalid_discovery'}


def search(install, resource_type, prompt, *, limit=5, runner=None):
    if resource_type not in RESOURCE_TYPES:
        raise ValueError('Unsupported internal resource category')
    if (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 200 or
            any(ord(c) < 32 for c in prompt) or type(limit) is not int or not 1 <= limit <= 30):
        raise ValueError('Search requires 1..200 printable characters and limit 1..30')
    root = Path(install).resolve(strict=True)
    binary = root / 'lyra-cli.exe'
    if binary.resolve(strict=True) != binary or hashlib.sha256(binary.read_bytes()).hexdigest() != LYRA_SHA256:
        raise ValueError('Untested Lyra CLI; revalidate the installation before use')
    args = [str(binary), 'material', 'search', '--type', resource_type, '--prompt', prompt,
            '--limit', str(limit), '--filter-commercial']
    # Inherit only the normal host environment. Never invent LYRA auth settings;
    # remove unrelated model credentials before starting a resource subprocess.
    system_keys = {'SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','PATHEXT','LOCALAPPDATA',
                   'APPDATA','USERPROFILE','HOME','TEMP','TMP','PATH','PROGRAMDATA',
                   'PROCESSOR_ARCHITECTURE','NUMBER_OF_PROCESSORS'}
    env = {k:v for k,v in os.environ.items() if k.upper() in system_keys or
           k.startswith(('LYRA_CLI_', 'CUSTOM_AGENT_HOST_'))}
    env['PATH'] = str(root) + os.pathsep + env.get('PATH', '')
    report = {'schema_version':'1.0', 'provider':'installed_jianying_lyra',
              'operation':'material search', 'resource_type':resource_type,
              'binary_sha256':LYRA_SHA256, 'gui_started':False,
              'authorization_fabricated':False, 'download_performed':False,
              'native_application_verified':False, 'usable_resources':[],
              'bridge':bridge_status()}
    try:
        if runner is not None:
            process = runner(args[1:], timeout=20)
            report['execution_transport'] = 'official_app_owned_runner'
        else:
            process = subprocess.run(args, cwd=root, env=env, capture_output=True, timeout=20,
                                     creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            report['execution_transport'] = 'standalone_cli'
    except subprocess.TimeoutExpired:
        return {**report, 'status':'timeout', 'reason':'official_resource_search_timed_out'}
    report['exit_code'] = process.returncode
    try:
        envelope = json.loads(process.stdout.decode('utf-8-sig'))
        if not isinstance(envelope,dict): raise ValueError('not an envelope')
    except (ValueError, UnicodeError):
        return {**report, 'status':'invalid_response', 'reason':'official_cli_did_not_return_json'}
    code = envelope.get('err_code')
    report['official_error_code'] = code if isinstance(code,(int,str)) else None
    if code == -100015:
        return {**report, 'status':'HOST_AUTH_REQUIRED', 'reason':'unauthorized host process',
                'required_connection':'officially authorized Jianying host; standalone native editor service is insufficient'}
    if process.returncode != 0 or code != 0:
        return {**report,'status':'search_failed','reason':'official_cli_rejected_resource_search'}
    result = envelope.get('result')
    items = result.get('items') if isinstance(result,dict) else None
    if not isinstance(items,list):
        return {**report,'status':'invalid_response','reason':'result.items missing'}
    fields = ('resource_id','title','type','is_vip','paid_type','is_commerce','commercial_scope',
              'paid_mode','subscribe_package','is_business','source','effect_type','md5','duration')
    report['candidates'] = [{k:row[k] for k in fields if k in row and
                             isinstance(row[k],(str,int,float,bool,type(None)))}
                            for row in items[:limit] if isinstance(row,dict)]
    return {**report, 'status':'metadata_received_unverified',
            'reason':'Full authoritative entitlement and native application still need validation'}
