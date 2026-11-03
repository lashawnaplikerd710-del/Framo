"""Owner-only provisioning: Windows credentials -> Wrangler stdin -> Worker Secrets.

Never put model secrets in argv, a generated file, stdout or a friend bundle.
The local grants registry contains hashes, not usable access tokens.
"""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

HERE = Path(__file__).resolve().parents[1]
RUNTIME = HERE.parent / 'agent-runtime'
sys.path.insert(0, str(RUNTIME))
from agent_core.credential_store import load_key, QWEN_TARGET
from agent_core.gateway_credentials import apply_gateway, save_token, target, validate_token
from agent_core.task_store import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--node', default='node')
    args = parser.parse_args()
    url = args.url.rstrip('/')
    if not url.endswith('/v1'): url += '/v1'
    config = apply_gateway(json.loads((RUNTIME/'config.native.json').read_text(encoding='utf-8-sig')), url)
    token = load_key(target=target(config['provider'])) or ('jyg_' + secrets.token_urlsafe(32))
    validate_token(token)
    registry = HERE/'private/grants.json'
    clients = json.loads(registry.read_text(encoding='utf-8')) if registry.exists() else []
    if any(c['id'] != 'owner-test' for c in clients):
        raise ValueError('This bootstrap tool will not replace an existing friend registry')
    clients = [{'id': 'owner-test', 'token_hash': hashlib.sha256(token.encode()).hexdigest(),
                'enabled': True, 'expires_at': (datetime.now(timezone.utc)+timedelta(days=30)).isoformat(),
                'daily_units': 200, 'max_concurrent': 1,
                'models': ['deepseek-flash','qwen3.8-omni-flash','qwen3.8-max']}]
    deepseek, qwen = load_key(), load_key(target=QWEN_TARGET)
    if not deepseek or not qwen: raise ValueError('Both owner model credentials must already be saved')
    payload = json.dumps({'DEEPSEEK_API_KEY': deepseek, 'DASHSCOPE_API_KEY': qwen,
                          'AUTH_CLIENTS': json.dumps(clients, separators=(',', ':'))})
    environment = {k:v for k,v in os.environ.items() if not k.endswith(('_API_KEY','_TOKEN'))}
    environment['WRANGLER_SEND_METRICS'] = 'false'
    proc = subprocess.run([args.node, str(HERE/'node_modules/wrangler/bin/wrangler.js'), 'secret', 'bulk'],
        cwd=HERE, input=payload, text=True, encoding='utf-8', capture_output=True, timeout=180,
        env=environment, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
    # Only redact for a safe diagnostic; successful commands print our own receipt.
    if proc.returncode:
        message = proc.stdout + '\n' + proc.stderr
        for value in (deepseek, qwen, token, payload): message = message.replace(value, '[REDACTED]')
        print(json.dumps({'status':'provision_failed','exit_code':proc.returncode,'diagnostic':message[-2200:]}, ensure_ascii=False))
        return 2
    save_token(config['provider'], token)
    atomic_json(registry, clients)
    config_path = RUNTIME/'config.gateway.local.json'
    atomic_json(config_path, config)
    receipt = {'schema':'jianying-gateway-provision-v1','status':'secrets_uploaded',
               'endpoint':url,'config':str(config_path),'model_keys_saved_in_client_config':False,
               'gateway_grant_store':'windows_credential_manager','client_id':'owner-test',
               'model_connectivity_verified':False}
    atomic_json(RUNTIME/'data/gateway-onboarding/provision.json', receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status':'provision_failed','error_type':type(exc).__name__})); raise SystemExit(2)
