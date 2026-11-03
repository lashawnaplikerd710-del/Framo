"""No network calls: all HTTP interactions use an in-process mocked opener."""
import copy
import io
import http.client
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_core.provider import Client, NoRedirect, ProviderError, MAX_RESPONSE_BYTES, MAX_REQUEST_BYTES

CONFIG = {'base_url': 'https://example.test/compatible-mode/v1', 'model': 'exact-user-model',
          'api_key_env': 'TEST_PROVIDER_KEY', 'timeout_seconds': 12, 'max_output_tokens': 256,
          'capabilities': {'vision': True, 'tool_calls': True}}
MESSAGES = [{'role': 'user', 'content': 'Analyze the supplied evidence.'}]


def completion(content='Ready', calls=None):
    message = {'role': 'assistant', 'content': content}
    if calls is not None:
        message['tool_calls'] = calls
    return {'id': 'request-1', 'model': 'exact-user-model', 'choices': [
        {'message': message, 'finish_reason': 'tool_calls' if calls else 'stop'}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}}


def call(name='sample_frames', arguments='{"source_id":"s001"}', cid='call-1'):
    return {'id': cid, 'type': 'function', 'function': {'name': name, 'arguments': arguments}}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'TEST_PROVIDER_KEY': 'SECRET-test-key'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.build = patch('agent_core.provider.urllib.request.build_opener').start()
        self.addCleanup(patch.stopall)
        self.response = Mock(status=200)
        self.response.__enter__ = Mock(return_value=self.response)
        self.response.__exit__ = Mock(return_value=False)
        self.build.return_value.open.return_value = self.response

    def run_response(self, value=None, config=None, messages=None, tools=None, raw=None):
        self.response.read.return_value = raw if raw is not None else json.dumps(value if value is not None else completion()).encode()
        return Client(config or CONFIG).complete(messages or MESSAGES, tools)

    def test_exact_model_endpoint_and_request(self):
        result = self.run_response()
        request = self.build.return_value.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, CONFIG['base_url'] + '/chat/completions')
        self.assertEqual(payload['model'], 'exact-user-model')
        self.assertFalse(payload['stream']); self.assertEqual(payload['max_tokens'], 256)
        self.assertEqual(self.build.return_value.open.call_args.kwargs['timeout'], 12)
        self.assertEqual(result['request_id'], 'request-1')
        self.assertEqual(result['usage']['total_tokens'], 12)
        self.response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)
        self.assertIsInstance(self.build.call_args.args[0], NoRedirect)

    def test_deepseek_thinking_and_tool_context(self):
        config = {**CONFIG, 'base_url': 'https://api.deepseek.com',
                  'model': 'deepseek-flash', 'thinking': 'enabled',
                  'reasoning_effort': 'low'}
        value = completion(None, [call()])
        value['choices'][0]['message']['reasoning_content'] = 'internal continuation'
        self.response.read.return_value = json.dumps(value).encode()
        first = Client(config).complete(MESSAGES, [{'type': 'function'}])
        self.assertEqual(first['message']['reasoning_content'], 'internal continuation')
        self.assertEqual(first['message']['tool_calls'][0]['function']['name'], 'sample_frames')
        self.response.read.return_value = json.dumps(completion()).encode()
        Client(config).complete(MESSAGES + [first['message'],
                                {'role': 'tool', 'tool_call_id': 'call-1', 'content': '{"ok":true}'}],
                                [{'type': 'function'}])
        request = self.build.return_value.open.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, 'https://api.deepseek.com/chat/completions')
        self.assertEqual(payload['thinking'], {'type': 'enabled'})
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertEqual(payload['messages'][1]['reasoning_content'], 'internal continuation')

    def test_structured_json_response_format_is_sent_and_bounded(self):
        self.response.read.return_value = json.dumps(completion('{"answer":7}')).encode()
        Client(CONFIG).complete(MESSAGES, response_format={'type':'json_object'})
        request = self.build.return_value.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)['response_format'], {'type':'json_object'})
        with self.assertRaises(ProviderError):
            Client(CONFIG).complete(MESSAGES, response_format={'type':'json_schema'})

    def test_thinking_option_validation(self):
        for options in ({'thinking': 'sometimes'}, {'reasoning_effort': 'ultra'},
                        {'thinking': 'disabled', 'reasoning_effort': 'high'}):
            with self.subTest(options=options), self.assertRaises(ProviderError):
                Client({**CONFIG, **options})

    def test_tool_call_and_reasoning_are_preserved(self):
        value = completion(None, [call()]); value['choices'][0]['message']['reasoning_content'] = 'provider continuation'
        result = self.run_response(value, tools=[{'type': 'function', 'function': {'name': 'sample_frames'}}])
        self.assertEqual(result['message']['tool_calls'][0]['function']['arguments'], '{"source_id":"s001"}')
        self.assertEqual(result['message']['reasoning_content'], 'provider continuation')

    def test_no_redirect_followup(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.test'))
        self.build.return_value.open.side_effect = urllib.error.HTTPError('https://x', 302, 'SECRET-test-key', {}, io.BytesIO(b'SECRET-test-key'))
        with self.assertRaisesRegex(ProviderError, 'HTTP status 302') as ctx:
            Client(CONFIG).complete(MESSAGES)
        self.assertNotIn('SECRET', str(ctx.exception)); self.build.return_value.open.assert_called_once()

    def test_http_errors_are_sanitized_and_not_retried(self):
        for code in (400, 401, 403, 429, 500):
            with self.subTest(code=code):
                self.build.return_value.open.reset_mock()
                self.build.return_value.open.side_effect = urllib.error.HTTPError('https://x', code, 'SECRET-test-key', {}, io.BytesIO(b'SECRET-test-key'))
                with self.assertRaises(ProviderError) as ctx: Client(CONFIG).complete(MESSAGES)
                self.assertNotIn('SECRET', str(ctx.exception)); self.build.return_value.open.assert_called_once()

    def test_json_error_explains_bad_parameter_without_echoing_credentials(self):
        body = {'error': {'message': 'Unsupported response_format; SECRET-test-key; '
                          'Bearer alternate-secret; api_key="another-secret"; sk-othersecret',
                          'type': 'invalid_request_error', 'param': 'response_format'},
                'request': {'Authorization': '[REDACTED_CREDENTIAL]', 'messages': MESSAGES}}
        self.build.return_value.open.side_effect = urllib.error.HTTPError(
            'https://example.test', 400, 'Bad Request', {},
            io.BytesIO(json.dumps(body).encode()))
        with self.assertRaises(ProviderError) as caught:
            Client(CONFIG).complete(MESSAGES)
        error = caught.exception
        self.assertEqual(error.category, 'invalid_request')
        self.assertEqual(error.details['param'], 'response_format')
        self.assertIn('Unsupported response_format', str(error))
        public = str(error) + json.dumps(error.details)
        for secret in ('SECRET-test-key', 'alternate-secret', 'another-secret', 'sk-othersecret'):
            self.assertNotIn(secret, public)
        self.assertNotIn('request', error.details)

    def test_html_http_error_is_classified_without_dumping_response(self):
        self.build.return_value.open.side_effect = urllib.error.HTTPError(
            'https://example.test', 400, 'Bad Request', {},
            io.BytesIO(b'<html>proxy failure SECRET-test-key</html>'))
        with self.assertRaises(ProviderError) as caught:
            Client(CONFIG).complete(MESSAGES)
        self.assertEqual(caught.exception.details, {'body_format': 'non_json'})
        self.assertNotIn('SECRET', str(caught.exception))

    def test_timeout_is_sanitized_and_not_retried(self):
        self.build.return_value.open.side_effect = TimeoutError('SECRET-test-key')
        with self.assertRaisesRegex(ProviderError, 'outcome may be uncertain') as ctx:
            Client(CONFIG).complete(MESSAGES)
        self.assertNotIn('SECRET', str(ctx.exception)); self.build.return_value.open.assert_called_once()

    def test_missing_key_never_opens_connection(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ProviderError, 'TEST_PROVIDER_KEY'): Client(CONFIG).complete(MESSAGES)
        self.build.assert_not_called()

    def test_endpoint_policy(self):
        for url in ('http://example.test/v1', 'https://user:pass@example.test/v1', 'https://x/v1?secret=yes', 'https://x/#f', 'file:///tmp/x'):
            with self.subTest(url=url), self.assertRaises(ProviderError): Client({**CONFIG, 'base_url': url})
        for url in ('http://localhost:123/v1', 'http://127.0.0.1:123/v1', 'http://[::1]:123/v1'):
            self.assertEqual(Client({**CONFIG, 'base_url': url}).endpoint, url + '/chat/completions')

    def test_required_config_and_bounds(self):
        for key, value in (('model', ''), ('api_key_env', 'bad\nname'), ('timeout_seconds', 0), ('timeout_seconds', float('nan')), ('timeout_seconds', 601), ('max_output_tokens', True)):
            with self.subTest(key=key, value=value), self.assertRaises(ProviderError): Client({**CONFIG, key: value})

    def test_response_bound_and_malformed_json(self):
        for raw in (b'x' * (MAX_RESPONSE_BYTES + 1), b'not JSON SECRET-test-key', b'\xff', b'null'):
            with self.subTest(size=len(raw)), self.assertRaises(ProviderError) as ctx: self.run_response(raw=raw)
            self.assertNotIn('SECRET', str(ctx.exception))

    def test_empty_and_bad_message(self):
        for value in (completion(' '), completion(None), {'choices': []}, {'choices': [{'message': {}}]}, completion(['text'])):
            with self.subTest(value=value), self.assertRaises(ProviderError): self.run_response(value)

    def test_tool_call_validation(self):
        cases = [{}, '', [call(arguments='[1]')], [call(arguments='{"a":NaN}')], [call(arguments='oops')],
                 [call(name='bad name')], [call(), call()], [{'id':'x','type':'other','function':{}}],
                 [dict(call(), extra='unexpected')]]
        for calls in cases:
            with self.subTest(calls=calls), self.assertRaises(ProviderError): self.run_response(completion(None,calls))

    def test_incomplete_response_is_sanitized(self):
        self.response.read.side_effect = http.client.IncompleteRead(b'SECRET-test-key')
        with self.assertRaisesRegex(ProviderError, 'outcome may be uncertain') as ctx:
            Client(CONFIG).complete(MESSAGES)
        self.assertNotIn('SECRET', str(ctx.exception))

    def test_disabled_capabilities(self):
        disabled={**CONFIG,'capabilities':{'vision':False,'tool_calls':False}}
        for messages, tools in ((MESSAGES,[{'type':'function'}]),
            ([{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,AAAA'}}]}],None)):
            with self.assertRaises(ProviderError): self.run_response(config=disabled,messages=messages,tools=tools)
        with self.assertRaises(ProviderError): self.run_response(completion(None,[call()]),config=disabled)

    def test_invalid_request_json(self):
        with self.assertRaises(ProviderError): Client(CONFIG).complete([{'role':'user','content':object()}])
        self.build.assert_not_called()

    def test_request_body_bound_before_network(self):
        with self.assertRaisesRegex(ProviderError, 'body limit'):
            Client(CONFIG).complete([{'role': 'user', 'content': 'x' * MAX_REQUEST_BYTES}])
        self.build.assert_not_called()


if __name__ == '__main__':
    unittest.main()
