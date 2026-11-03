import copy
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import json

from agent_core.gateway_credentials import apply_gateway, target, validate_profile, validate_token
from agent_core.model_router import create_provider
from agent_core.runtime import load_config
from agent_core.provider import ProviderClient
from agent_core.credential_store import credential_environment

ROOT = Path(__file__).resolve().parents[1]
TOKEN = 'jyg_' + 'a' * 43


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.base = json.loads((ROOT/'config.native.json').read_text(encoding='utf-8-sig'))
        self.config = apply_gateway(self.base, 'https://gateway.example.com/v1')

    def test_distinct_scoped_credentials_never_load_provider_keys(self):
        cfg = self.config['provider']
        with patch.dict(os.environ, {}, clear=True), patch('agent_core.credential_store.load_key', return_value=TOKEN) as read:
            with credential_environment(cfg):
                self.assertEqual(os.environ['JIANYING_GATEWAY_TOKEN'], TOKEN)
                self.assertNotIn('DEEPSEEK_API_KEY', os.environ)
                self.assertNotIn('DASHSCOPE_API_KEY', os.environ)
            self.assertNotIn('JIANYING_GATEWAY_TOKEN', os.environ)
            read.assert_called_once_with(target=target(cfg))

    def test_qwen_and_language_gateway_routing_ignore_direct_env_overrides(self):
        # Exercise both supported profiles explicitly. The shipped default
        # intentionally has no Qwen configuration or client.
        vision = {'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
                  'model': 'qwen3.8-omni-flash', 'api_key_env': 'DASHSCOPE_API_KEY',
                  'capabilities': {'vision': True, 'tool_calls': False}}
        with patch.dict(os.environ, {'DEEPSEEK_BASE_URL': 'https://wrong.example', 'DEEPSEEK_MODEL': 'wrong'}):
            for base in (self.base, {**self.base, 'vision_provider': vision}):
                provider = create_provider(apply_gateway(base, 'https://gateway.example.com/v1'))
                clients = getattr(provider, '_clients', {'deepseek': provider.client})
                for client in clients.values():
                    self.assertEqual(client.endpoint, 'https://gateway.example.com/v1/chat/completions')
                    self.assertEqual(client.key_env, 'JIANYING_GATEWAY_TOKEN')

    def test_shipped_default_does_not_reenable_qwen(self):
        self.assertNotIn('vision_provider', self.config)
        self.assertNotIn('complex_vision_provider', self.config)
        self.assertFalse(hasattr(create_provider(self.config), '_clients'))

    def test_no_plain_model_key_accepted_as_gateway_grant(self):
        for value in ('sk-secretsecretsecret', '', TOKEN + '\n'):
            with self.assertRaises(ValueError): validate_token(value)

    def test_gateway_origin_scope_and_endpoint_validation(self):
        cfg = self.config['provider']
        self.assertNotEqual(target(cfg), target({**cfg, 'base_url': 'https://other.example/v1'}))
        for url in ('http://example.com/v1', 'https://example.com/v1?key=x', 'https://user@example.com/v1',
                    'https://api.deepseek.com/v1', 'https://example.com/other'):
            with self.assertRaises(ValueError): validate_profile({**cfg, 'base_url': url})
        with self.assertRaises(ValueError): validate_profile({**cfg, 'api_key_env': 'DEEPSEEK_API_KEY'})

    def test_gateway_and_direct_profiles_cannot_be_mixed(self):
        self.config['vision_provider'] = {
            'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1',
            'model': 'qwen3.8-omni-flash', 'api_key_env': 'DASHSCOPE_API_KEY',
            'capabilities': {'vision': True}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.json'; path.write_text(json.dumps(self.config))
            with self.assertRaises(ValueError): load_config(path)

    def test_direct_config_remains_unchanged(self):
        self.assertEqual(self.base['provider']['api_key_env'], 'DEEPSEEK_API_KEY')
        self.assertNotIn('transport', self.base['provider'])

    def test_transport_sends_gateway_token_and_request_id(self):
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, _): return b'{"choices":[{"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}'
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.return_value = Response()
            client = ProviderClient(self.config['provider'], key_value=TOKEN)
            client.complete([{'role': 'user', 'content': 'test'}])
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header('Authorization'), 'Bearer ' + TOKEN)
            self.assertEqual(len(request.get_header('X-request-id')), 36)


if __name__ == '__main__': unittest.main()
