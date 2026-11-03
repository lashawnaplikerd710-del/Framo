import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_core.deepseek import DeepSeekProvider
from agent_core.diagnostics import check_model
from agent_core.provider import ProviderError


class ModelDiagnosticsTests(unittest.TestCase):
    def test_image_only_report_leaves_unrequested_checks_unverified(self):
        class Client:
            def complete(self, messages, **kwargs):
                return {'message': {'content': '{"schema_version":"1.0","dominant_color":"lime green"}'},
                        'finish_reason': 'stop', 'usage': {'total_tokens': 9}}

        config = {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
                  'api_key_env': 'DEEPSEEK_API_KEY'}
        stages = []
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            result = check_model(config, Path(folder) / 'report.json', only_stage='image',
                                 progress=stages.append,
                                 provider_factory=lambda cfg: DeepSeekProvider(cfg, client=Client()))
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(stages, ['image'])
        self.assertIsNone(result['text_verified'])
        self.assertIsNone(result['json_verified'])
        self.assertTrue(result['image_verified'])
        self.assertEqual(result['usage']['model_calls'], 1)

    def test_failed_json_probe_preserves_completed_stage_and_safe_report(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, **kwargs):
                self.calls += 1
                if self.calls == 2:
                    raise ProviderError('Provider HTTP status 400: parameter rejected',
                        status_code=400, category='invalid_request',
                        details={'param': 'response_format', 'body_format': 'json'})
                return {'message': {'content': '连接成功'}, 'finish_reason': 'stop',
                        'usage': {'prompt_tokens': 5, 'completion_tokens': 3,
                                  'total_tokens': 8}}

        config = {'base_url': 'https://api.deepseek.com', 'model': 'deepseek-flash',
                  'api_key_env': 'DEEPSEEK_API_KEY'}
        client = Client()
        provider = DeepSeekProvider(config, client=client)
        stages = []
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,
                {'DEEPSEEK_API_KEY': 'sk-private-test-secret'}):
            path = Path(folder) / 'diagnostics.json'
            result = check_model(config, path, progress=stages.append,
                                 provider_factory=lambda _: provider)
            saved = path.read_text(encoding='utf-8')
            self.assertNotIn('sk-private-test-secret', saved)
            self.assertEqual(json.loads(saved), result)
        self.assertEqual(stages, ['text', 'json'])
        self.assertEqual(result['failed_stage'], 'json')
        self.assertTrue(result['text_verified'])
        self.assertFalse(result['json_verified'])
        self.assertFalse(result['image_verified'])
        self.assertEqual(result['http_status'], 400)
        self.assertEqual(result['usage']['total_tokens'], 8)
        self.assertEqual(client.calls, 2)


if __name__ == '__main__':
    unittest.main()
