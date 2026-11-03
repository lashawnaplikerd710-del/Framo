"""Bounded, non-retrying OpenAI-compatible transport. No credential files or logging."""
import ipaddress
import http.client
import json
import math
import os
import re
import uuid
import urllib.error
import urllib.parse
import urllib.request
import socket
import ssl
import time

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 40 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
NAME = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
ENV_NAME = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


class ProviderError(RuntimeError):
    """Safe, deliberately limited error text suitable for a task log."""
    def __init__(self, message, *, status_code=None, category='provider', retryable=False,
                 details=None):
        super().__init__(message)
        self.status_code = status_code
        self.category = category
        self.retryable = retryable
        self.details = details or {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _object_json(text):
    def reject_constant(_):
        raise ValueError('Non-finite JSON number')
    return json.loads(text, parse_constant=reject_constant)


def _redact_error_text(value, secret, limit):
    """Only bounded provider error fields may leave the transport, never raw bodies."""
    for token in sorted({secret, secret.strip()}, key=len, reverse=True):
        if token:
            value = value.replace(token, '[REDACTED]')
    value = re.sub(r'(?i)\bsk-[a-z0-9_.-]+', '[REDACTED]', value)
    value = re.sub(r'(?i)\bBearer\s+[^\s,;"\']+', 'Bearer [REDACTED]', value)
    value = re.sub(r'''(?ix)\b(?:api[_-]?key|authorization|access[_-]?token)\b
        ["']?\s*[:=]\s*(?:"[^"]*"|'[^']*'|[^\s,;]+)''',
        '[credential field redacted]', value)
    value = re.sub(r'(?i)data:[^\s"\']+', '[media data redacted]', value)
    return re.sub(r'[\x00-\x1f\x7f]', ' ', value)[:limit]


def _http_error_details(error, secret):
    try:
        raw = error.read(MAX_ERROR_BYTES + 1)
    except (OSError, ValueError, http.client.HTTPException):
        return {'body_format': 'unreadable'}
    finally:
        error.close()
    if len(raw) > MAX_ERROR_BYTES:
        return {'body_format': 'oversized'}
    if not raw:
        return {'body_format': 'empty'}
    try:
        parsed = _object_json(raw.decode('utf-8'))
    except (ValueError, UnicodeError):
        return {'body_format': 'non_json'}
    details = {'body_format': 'json'}
    error_fields = parsed.get('error', parsed) if isinstance(parsed, dict) else None
    if isinstance(error_fields, dict):
        for field in ('message', 'type', 'param', 'code'):
            value = error_fields.get(field)
            if isinstance(value, str):
                details[field] = _redact_error_text(value, secret, 512 if field == 'message' else 120)
    return details


def _endpoint(value):
    if not isinstance(value, str) or not value.strip():
        raise ProviderError('Set a nonempty provider base_url.')
    try:
        url = urllib.parse.urlsplit(value)
        hostname = url.hostname
        _ = url.port
        local = hostname == 'localhost'
        if hostname and not local:
            try:
                local = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                pass
        if (not hostname or url.username is not None or url.password is not None
                or url.query or url.fragment or any(c.isspace() for c in value)
                or url.scheme not in ('https', 'http') or (url.scheme == 'http' and not local)):
            raise ValueError('Unsafe endpoint')
    except (ValueError, TypeError):
        raise ProviderError('Use an HTTPS base_url without credentials, query or fragment; HTTP is allowed only for explicit localhost/loopback.') from None
    return value.rstrip('/') + '/chat/completions'


class ProviderClient:
    def __init__(self, config, key_value=None):
        if not isinstance(config, dict):
            raise ProviderError('Provider configuration must be an object.')
        self.endpoint = _endpoint(config.get('base_url'))
        self.gateway = config.get('transport') == 'gateway'
        if self.gateway:
            from .gateway_credentials import validate_profile
            validate_profile(config)
        self.model = config.get('model')
        self.key_env = config.get('api_key_env')
        if key_value is not None and (not isinstance(key_value, str) or '\r' in key_value or '\n' in key_value):
            raise ProviderError('API key must be a single-line string.')
        self._key_value = key_value
        if not isinstance(self.model, str) or not self.model.strip():
            raise ProviderError('Set the exact provider model ID; no fallback model is selected.')
        if not isinstance(self.key_env, str) or not ENV_NAME.fullmatch(self.key_env):
            raise ProviderError('Set api_key_env to a valid environment variable name.')
        self.timeout = config.get('timeout_seconds', 60)
        self.max_tokens = config.get('max_output_tokens', 4096)
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= 600):
            raise ProviderError('timeout_seconds must be greater than 0 and at most 600.')
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ProviderError('max_output_tokens must be a positive integer.')
        self.capabilities = config.get('capabilities', {})
        if not isinstance(self.capabilities, dict):
            raise ProviderError('Provider capabilities must be an object.')
        self.thinking = config.get('thinking')
        self.reasoning_effort = config.get('reasoning_effort')
        self.stream = config.get('stream', False)
        self.connect_timeout = config.get('connect_timeout_seconds', min(20, self.timeout))
        self.read_timeout = config.get('read_timeout_seconds', self.timeout)
        self.total_timeout = config.get('total_timeout_seconds', min(600, max(200, self.timeout)))
        for value in (self.connect_timeout, self.read_timeout, self.total_timeout):
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 600:
                raise ProviderError('Invalid connection/read/total timeout.')
        self.recovery = None
        self.diagnostics = []
        self.modalities = config.get('modalities')
        if type(self.stream) is not bool:
            raise ProviderError('stream must be boolean.')
        if self.modalities is not None and self.modalities != ['text']:
            raise ProviderError('Only text output modalities are supported.')
        if self.thinking is not None and self.thinking not in ('enabled', 'disabled'):
            raise ProviderError('thinking must be enabled or disabled.')
        efforts = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max') if self.model in ('qwen3.8-omni-flash', 'qwen3.8-max', 'qwen3.8-max-0902') else ('low', 'high', 'max')
        if self.reasoning_effort is not None and self.reasoning_effort not in efforts:
            raise ProviderError('Unsupported reasoning_effort for the selected model.')
        if self.thinking == 'disabled' and self.reasoning_effort is not None:
            raise ProviderError('reasoning_effort requires enabled thinking.')

    def complete(self, messages, tools=None, response_format=None):
        self.submission_count = 0
        if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) for m in messages):
            raise ProviderError('messages must be a nonempty list of objects.')
        if tools is not None and not isinstance(tools, list):
            raise ProviderError('tools must be a list.')
        if response_format is not None and response_format != {'type': 'json_object'}:
            raise ProviderError('Only json_object response format is supported.')
        if tools and self.capabilities.get('tool_calls') is not True:
            raise ProviderError('Tool calling is not enabled for this configured model.')
        if tools and self.stream:
            raise ProviderError('Streaming tool calls are not supported by this adapter.')
        has_images = any(isinstance(m.get('content'), list) and any(
            isinstance(part, dict) and part.get('type') in ('image_url', 'input_image', 'video', 'video_url', 'input_video')
            for part in m['content']) for m in messages)
        if has_images and self.capabilities.get('vision') is not True:
            raise ProviderError('Vision input is not enabled for this configured model.')
        key = self._key_value if self._key_value is not None else os.environ.get(self.key_env, '')
        if self.gateway and key:
            from .gateway_credentials import validate_token
            validate_token(key)
        if not key.strip():
            raise ProviderError('Missing API key environment variable: ' + self.key_env,
                                category='missing_key')
        if '\r' in key or '\n' in key:
            raise ProviderError('API key environment value contains invalid characters.')
        payload = {'model': self.model, 'messages': messages, 'stream': self.stream,
                   'max_tokens': self.max_tokens}
        if self.stream:
            payload['stream_options'] = {'include_usage': True}
        if self.modalities is not None:
            payload['modalities'] = self.modalities
        if self.thinking is not None:
            payload['thinking'] = {'type': self.thinking}
        if self.reasoning_effort is not None:
            payload['reasoning_effort'] = self.reasoning_effort
        if tools:
            payload['tools'] = tools
        if response_format is not None:
            payload['response_format'] = response_format
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, UnicodeError):
            raise ProviderError('Request is not valid JSON data.') from None
        if len(body) > MAX_REQUEST_BYTES:
            raise ProviderError('Request exceeds the configured body limit; reduce the image batch.')
        if self.recovery and self.gateway:
            raw = self.recovery.execute(self, body, key)
        else:
            raw = self._exchange(body, key, str(uuid.uuid4()))
        try:
            if self.stream:
                return self._parse_stream(raw.decode('utf-8'))
            response = _object_json(raw.decode('utf-8'))
            return self._parse(response)
        except ProviderError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, UnicodeError):
            raise ProviderError('Provider returned an invalid chat-completion response.') from None

    def _exchange(self, body, key, request_id, *, path=None, recoverable=False, deadline=None):
        if body is not None:
            self.submission_count = getattr(self, 'submission_count', 0)+1
        started = time.monotonic()
        deadline = min(deadline or started + self.total_timeout, started + self.total_timeout)
        diagnostic = {'request_id': request_id, 'model': self.model,
                      'phase': 'connect_or_response_headers', 'request_bytes': len(body or b''),
                      'response_bytes': 0, 'http_status': None, 'headers_ms': None,
                      'first_byte_ms': None, 'elapsed_ms': 0}
        headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json',
                   'Accept': 'text/event-stream' if self.stream else 'application/json'}
        if self.gateway:
            headers['X-Request-Id'] = request_id
            headers['User-Agent'] = 'Jianying-Agent-Workbench/0.1'
        if recoverable:
            headers['X-Recovery-Protocol'] = '1'
        endpoint = self.endpoint if path is None else self.endpoint.rsplit('/chat/completions', 1)[0] + path
        request = urllib.request.Request(endpoint, data=body, method='POST' if body is not None else 'GET', headers=headers)
        try:
            client = self
            class TimedHTTP(http.client.HTTPConnection):
                def connect(connection):
                    diagnostic['phase'] = 'connect'
                    super().connect()
                    diagnostic['phase'] = 'response_headers'
                    connection.sock.settimeout(min(client.read_timeout, max(.01, deadline-time.monotonic())))
            class TimedHTTPS(http.client.HTTPSConnection):
                def connect(connection):
                    diagnostic['phase'] = 'connect'
                    super().connect()
                    diagnostic['phase'] = 'response_headers'
                    connection.sock.settimeout(min(client.read_timeout, max(.01, deadline-time.monotonic())))
            class HTTPHandler(urllib.request.HTTPHandler):
                def http_open(handler, request):
                    return handler.do_open(TimedHTTP, request)
            class HTTPSHandler(urllib.request.HTTPSHandler):
                def https_open(handler, request):
                    return handler.do_open(TimedHTTPS, request, context=handler._context)
            opener = urllib.request.build_opener(NoRedirect(), HTTPHandler(), HTTPSHandler())
            with opener.open(request, timeout=min(self.connect_timeout, max(.01, deadline-started))) as response:
                diagnostic.update(http_status=response.status, headers_ms=round((time.monotonic()-started)*1000), phase='read_response')
                if response.status != 200:
                    raise ProviderError('Provider HTTP status ' + str(int(response.status)) + '; request was not retried.')
                if isinstance(response, http.client.HTTPResponse):
                    chunks = []
                    while True:
                        remaining = deadline-time.monotonic()
                        if remaining <= 0:
                            diagnostic['phase'] = 'total_deadline'
                            raise TimeoutError()
                        try:
                            response.fp.raw._sock.settimeout(min(self.read_timeout, remaining))
                        except AttributeError:
                            pass
                        chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1-diagnostic['response_bytes']))
                        if not chunk:
                            break
                        if diagnostic['first_byte_ms'] is None:
                            diagnostic['first_byte_ms'] = round((time.monotonic()-started)*1000)
                        chunks.append(chunk); diagnostic['response_bytes'] += len(chunk)
                        if diagnostic['response_bytes'] > MAX_RESPONSE_BYTES:
                            raise ProviderError('Provider response exceeded the allowed size.')
                    raw = b''.join(chunks)
                    if response.length not in (None, 0):
                        raise http.client.IncompleteRead(b'')
                else:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    diagnostic['response_bytes'] = len(raw)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ProviderError('Provider response exceeded the allowed size.')
                if self.stream and (body is not None or (path or '').endswith('/result')):
                    self._parse_stream(raw.decode('utf-8'))
                diagnostic['phase'] = 'complete'
                return raw
        except urllib.error.HTTPError as exc:
            diagnostic.update(http_status=exc.code, exception_type='HTTPError', phase='http_error')
            category = ('invalid_request' if exc.code in (400, 422) else
                        'authentication' if exc.code == 401 else
                        'balance' if exc.code == 402 else
                        'rate_limit' if exc.code == 429 else
                        'overloaded' if exc.code == 503 else 'http_error')
            details = _http_error_details(exc, key)
            message = 'Provider HTTP status ' + str(exc.code)
            if details.get('message'):
                message += ': ' + details['message']
            else:
                message += '; no readable JSON error detail was returned.'
            raise ProviderError(message,
                                status_code=exc.code, category=category,
                                retryable=exc.code in (429, 503), details=details) from None
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            category = ('dns' if isinstance(reason, socket.gaierror) else
                        'tls' if isinstance(reason, ssl.SSLError) else
                        'timeout' if isinstance(reason, TimeoutError) else
                        'response_incomplete' if isinstance(reason, http.client.IncompleteRead) else 'connection')
            diagnostic['exception_type'] = type(reason).__name__
            diagnostic['category'] = category
            raise ProviderError(f'Provider {category} during {diagnostic["phase"]}; outcome may be uncertain. Request was not retried.',
                                category=category, details=diagnostic) from None
        finally:
            diagnostic['elapsed_ms'] = round((time.monotonic()-started)*1000)
            self.diagnostics.append(diagnostic)
            if self.recovery:
                self.recovery.log(diagnostic)

    def _parse_stream(self, text):
        """Assemble bounded SSE text; reject interrupted or tool-bearing streams."""
        pieces, usage, finish, done, request_id, model = [], {}, None, False, None, self.model
        for event in text.replace('\r\n', '\n').split('\n\n'):
            data = '\n'.join(line[5:].lstrip(' ') for line in event.splitlines() if line.startswith('data:'))
            if not data:
                continue
            if data.strip() == '[DONE]':
                done = True
                continue
            if done:
                raise ProviderError('Provider sent data after stream completion.')
            chunk = _object_json(data)
            if not isinstance(chunk, dict) or chunk.get('error'):
                raise ProviderError('Provider stream reported an error; outcome may be uncertain.')
            request_id = chunk.get('id') or request_id
            model = chunk.get('model') or model
            if isinstance(chunk.get('usage'), dict):
                usage = chunk['usage']
            choices = chunk.get('choices', [])
            if not isinstance(choices, list) or len(choices) > 1:
                raise ProviderError('Provider stream must have one choice.')
            if choices:
                choice = choices[0]
                if not isinstance(choice, dict) or choice.get('index', 0) != 0:
                    raise ProviderError('Provider stream returned an invalid choice.')
                delta = choice.get('delta') or {}
                if not isinstance(delta, dict) or delta.get('tool_calls') or delta.get('audio'):
                    raise ProviderError('Only streaming text output is supported.')
                content = delta.get('content')
                if content is not None:
                    if not isinstance(content, str):
                        raise ProviderError('Provider stream content must be text.')
                    pieces.append(content)
                finish = choice.get('finish_reason') or finish
        if not done or finish is None:
            raise ProviderError('Provider stream was interrupted; outcome may be uncertain. Request was not retried.', category='response_incomplete')
        return self._parse({'id': request_id, 'model': model, 'usage': usage,
                           'choices': [{'message': {'role': 'assistant', 'content': ''.join(pieces)},
                                        'finish_reason': finish}]})

    def _parse(self, response):
        if not isinstance(response, dict) or not isinstance(response.get('choices'), list) or len(response['choices']) != 1:
            raise ProviderError('Provider must return exactly one completion choice.')
        choice = response['choices'][0]
        message = choice.get('message') if isinstance(choice, dict) else None
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            raise ProviderError('Provider response is missing an assistant message.')
        content = message.get('content')
        if content is not None and not isinstance(content, str):
            raise ProviderError('Provider assistant content must be text or null.')
        calls = message.get('tool_calls')
        if calls is None:
            calls = []
        if not isinstance(calls, list):
            raise ProviderError('Provider tool_calls must be a list.')
        ids = set()
        for call in calls:
            if (not isinstance(call, dict) or set(call) != {'id', 'type', 'function'}
                    or not isinstance(call['id'], str) or not call['id'].strip() or call['id'] in ids
                    or call['type'] != 'function' or not isinstance(call['function'], dict)
                    or set(call['function']) != {'name', 'arguments'}):
                raise ProviderError('Provider returned a malformed or duplicate tool call.')
            fn = call['function']
            if not isinstance(fn['name'], str) or not NAME.fullmatch(fn['name']) or not isinstance(fn['arguments'], str):
                raise ProviderError('Provider returned an invalid tool name or arguments.')
            try:
                arguments = _object_json(fn['arguments'])
            except (ValueError, TypeError):
                raise ProviderError('Provider tool arguments are invalid JSON.') from None
            if not isinstance(arguments, dict):
                raise ProviderError('Provider tool arguments must be a JSON object.')
            ids.add(call['id'])
        if calls and self.capabilities.get('tool_calls') is not True:
            raise ProviderError('Provider returned tool calls when tool calling is disabled.')
        if not calls and not (content or '').strip():
            raise ProviderError('Provider returned no usable content or tool call.')
        output = {'role': 'assistant', 'content': content}
        if calls:
            output['tool_calls'] = calls
        if isinstance(message.get('reasoning_content'), str):
            output['reasoning_content'] = message['reasoning_content']
        return {'message': output, 'usage': response.get('usage') or {},
                'finish_reason': choice.get('finish_reason'), 'request_id': response.get('id'),
                'model': response.get('model') or self.model}


Client = ProviderClient
