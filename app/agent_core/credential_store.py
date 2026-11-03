"""One application-owned Windows Generic Credential; never enumerate the vault."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
import sys
from urllib.parse import urlsplit

TARGET = 'JianyingAutoEditAgent/DeepSeek/api.deepseek.com'
QWEN_TARGET = 'JianyingAutoEditAgent/Qwen/cn-beijing/dashscope.aliyuncs.com'
ENV_NAME = 'DEEPSEEK_API_KEY'
GENERIC = 1
LOCAL_MACHINE = 2  # Same Windows user on this computer, across logon sessions.
NOT_FOUND = 1168


class CredentialError(RuntimeError):
    """Messages contain error numbers only, never secret values."""


class CREDENTIAL(ctypes.Structure):
    _fields_ = [('Flags', wintypes.DWORD), ('Type', wintypes.DWORD),
        ('TargetName', wintypes.LPWSTR), ('Comment', wintypes.LPWSTR),
        ('LastWritten', wintypes.FILETIME), ('CredentialBlobSize', wintypes.DWORD),
        ('CredentialBlob', ctypes.POINTER(ctypes.c_ubyte)), ('Persist', wintypes.DWORD),
        ('AttributeCount', wintypes.DWORD), ('Attributes', ctypes.c_void_p),
        ('TargetAlias', wintypes.LPWSTR), ('UserName', wintypes.LPWSTR)]


def _api():
    if sys.platform != 'win32':
        raise CredentialError('Saved credentials require Windows Credential Manager.')
    api = ctypes.WinDLL('Advapi32.dll', use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                            ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    api.CredFree.restype = None
    return api


def _target(target):
    import re
    if re.fullmatch(r'JianyingAutoEditAgent/Gateway/[a-f0-9]{64}', target):
        return target
    if not any(target == owned or target.startswith(owned + '/test/')
               for owned in (TARGET, QWEN_TARGET)):
        raise CredentialError('Only this Agent credential may be accessed.')
    return target


def validate_key(value):
    if (not isinstance(value, str) or not 16 <= len(value) <= 512 or
            not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        raise CredentialError('Paste the complete API key without spaces or control characters.')
    return value


def load_key(*, target=TARGET):
    api = _api()
    pointer = ctypes.POINTER(CREDENTIAL)()
    if not api.CredReadW(_target(target), GENERIC, 0, ctypes.byref(pointer)):
        code = ctypes.get_last_error()
        if code == NOT_FOUND:
            return None
        raise CredentialError(f'Windows credential read failed (code {code}).')
    try:
        cred = pointer.contents
        if cred.CredentialBlobSize > 2560 or not cred.CredentialBlob:
            raise CredentialError('Saved Agent credential is invalid; replace it with save.')
        try:
            value = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode('utf-8')
            return validate_key(value)
        except (UnicodeError, CredentialError):
            raise CredentialError('Saved Agent credential is invalid; replace it with save.') from None
    finally:
        cred = pointer.contents
        if cred.CredentialBlob and cred.CredentialBlobSize <= 2560:
            ctypes.memset(cred.CredentialBlob, 0, cred.CredentialBlobSize)
        api.CredFree(pointer)


def save_key(value, *, target=TARGET):
    value = validate_key(value)
    raw = value.encode('utf-8')
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    cred = CREDENTIAL()
    cred.Type, cred.TargetName = GENERIC, _target(target)
    vendor = 'Gateway' if '/Gateway/' in target else 'Qwen' if target.startswith(QWEN_TARGET) else 'DeepSeek'
    cred.Comment = vendor + ' API key for the standalone Jianying editing Agent'
    cred.UserName = vendor + ' API'
    cred.Persist = LOCAL_MACHINE
    cred.CredentialBlobSize, cred.CredentialBlob = len(raw), buffer
    try:
        if not _api().CredWriteW(ctypes.byref(cred), 0):
            raise CredentialError(f'Windows credential save failed (code {ctypes.get_last_error()}).')
    finally:
        ctypes.memset(buffer, 0, len(raw))
    if load_key(target=target) != value:
        raise CredentialError('Saved credential could not be verified.')


def delete_key(*, target=TARGET):
    if not _api().CredDeleteW(_target(target), GENERIC, 0):
        code = ctypes.get_last_error()
        if code == NOT_FOUND:
            return False
        raise CredentialError(f'Windows credential removal failed (code {code}).')
    return True


def eligible_provider(config):
    """A saved DeepSeek key is never automatically applied to custom endpoints."""
    if config.get('api_key_env') != ENV_NAME:
        return False
    try:
        url = urlsplit(os.environ.get('DEEPSEEK_BASE_URL') or config.get('base_url', ''))
        return (url.scheme == 'https' and url.hostname == 'api.deepseek.com' and
                url.port in (None, 443) and not url.username and not url.password and
                not url.query and not url.fragment and url.path.rstrip('/') in ('', '/v1'))
    except ValueError:
        return False


@contextmanager
def credential_environment(config, *, interactive=False):
    """Memory-only runtime env; optional hidden prompt and OS-managed persistence.

    No plaintext is returned on stdout or passed on a command line. Native child
    tools continue to have API-key environment variables removed by LocalExecutor.
    """
    if config.get('transport') == 'gateway':
        from .gateway_credentials import credential_environment as gateway_environment
        with gateway_environment(config) as origin:
            yield origin
        return
    previous = os.environ.get(config.get('api_key_env', ENV_NAME))
    loaded = False
    try:
        if previous and previous.strip():
            yield 'environment'
            return
        if not eligible_provider(config):
            if interactive:
                raise CredentialError('Saved DeepSeek credentials only support the official HTTPS endpoint.')
            yield 'unavailable'
            return
        value = load_key()
        origin = 'windows_credential_manager'
        if value is None and interactive:
            value = prompt_and_save()
        if value:
            os.environ[ENV_NAME] = value
            loaded = True
        yield origin if loaded else 'missing'
    finally:
        if loaded:
            if previous is None:
                os.environ.pop(ENV_NAME, None)
            else:
                os.environ[ENV_NAME] = previous


def prompt_and_save():
    import getpass
    if not sys.stdin.isatty():
        raise CredentialError('Open a local PowerShell window to enter the key at a hidden prompt.')
    print('Enter your FULL DeepSeek API key once. RIGHT-CLICK to paste; input is hidden.', flush=True)
    print('It will be saved in Windows Credential Manager for this Windows account.', flush=True)
    for attempt in range(3):
        try:
            value = validate_key(getpass.getpass('DeepSeek API Key: ').strip())
        except CredentialError:
            print('Incomplete input. Copy the full key and paste again.', flush=True)
            continue
        save_key(value)
        print('SAVED: future Agent launches will reuse the local Windows credential.', flush=True)
        return value
    raise CredentialError('No complete key entered. Nothing was saved.')
