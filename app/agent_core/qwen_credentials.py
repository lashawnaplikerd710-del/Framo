"""Application-owned Beijing Qwen credential. Secrets never enter argv or logs."""
from contextlib import contextmanager
import argparse
import json
import os
import re
import sys
from urllib.parse import urlsplit

from .credential_store import (QWEN_TARGET, CredentialError, load_key, save_key,
                               delete_key, validate_key)

ENV_NAME = 'DASHSCOPE_API_KEY'


def eligible_provider(config):
    try:
        url = urlsplit(config.get('base_url', ''))
        host = url.hostname or ''
        return (config.get('api_key_env') == ENV_NAME and url.scheme == 'https'
                and (host == 'dashscope.aliyuncs.com' or re.fullmatch(
                    r'[a-z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com', host))
                and url.port in (None, 443) and not url.username and not url.password
                and not url.query and not url.fragment
                and url.path.rstrip('/') == '/compatible-mode/v1') is True
    except (ValueError, TypeError):
        return False


@contextmanager
def credential_environment(config):
    if config.get('transport') == 'gateway':
        from .gateway_credentials import credential_environment as gateway_environment
        with gateway_environment(config) as origin:
            yield origin
        return
    if not eligible_provider(config):
        raise CredentialError('Qwen saved credential requires the official Beijing endpoint.')
    previous = os.environ.get(ENV_NAME)
    loaded = False
    try:
        if previous and previous.strip():
            yield 'environment'
            return
        value = load_key(target=QWEN_TARGET)
        if value:
            os.environ[ENV_NAME] = value
            loaded = True
        yield 'windows_credential_manager' if loaded else 'missing'
    finally:
        if loaded:
            if previous is None:
                os.environ.pop(ENV_NAME, None)
            else:
                os.environ[ENV_NAME] = previous


def save_from_pipe(stream):
    value = stream.read(1025).rstrip('\r\n')
    try:
        save_key(validate_key(value), target=QWEN_TARGET)
    finally:
        value = None
    return {'saved': True, 'provider': 'qwen', 'region': 'cn-beijing',
            'store': 'Windows Credential Manager'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('save-stdin', 'status', 'delete'))
    args = parser.parse_args()
    try:
        if args.action == 'save-stdin':
            if sys.stdin.isatty():
                raise CredentialError('Use Save-QwenKey-Window.ps1 to enter the key locally.')
            result = save_from_pipe(sys.stdin)
        elif args.action == 'status':
            result = {'saved': load_key(target=QWEN_TARGET) is not None,
                      'target': QWEN_TARGET, 'region': 'cn-beijing'}
        else:
            result = {'removed': delete_key(target=QWEN_TARGET)}
        print(json.dumps(result))
        return 0
    except CredentialError as exc:
        print(json.dumps({'status': 'error', 'message': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
