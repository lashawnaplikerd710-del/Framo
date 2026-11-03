"""Durable request identity and hash-checked responses; never stores credentials/prompts."""
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from .provider import ProviderError
from .task_store import atomic_json, digest, stamp


def _transient_transport_error(error):
    if error.category in ('timeout', 'connection', 'response_incomplete', 'dns'):
        return True
    # Retry unexpected TLS closures, never certificate/hostname or protocol
    # validation failures. POST recovery still queries its durable ID first.
    return (error.category == 'tls' and
            error.details.get('exception_type') in ('SSLEOFError', 'SSLZeroReturnError'))


def _terminal_text_recoverable(client, body, status, record):
    """One audited regeneration of a terminal lost text result, never tool IO."""
    if (getattr(client, 'terminal_text_regeneration', False) is not True
            or status.get('state') != 'UNCERTAIN'
            or status.get('error_code') not in ('worker_ended_without_receipt',
                'gateway_stream_interrupted', 'upstream_connect_or_response_failed')
            or record.get('terminal_regenerations')):
        return False
    try:
        payload = json.loads(body)
    except (ValueError, TypeError, UnicodeError):
        return False
    return (isinstance(payload, dict) and isinstance(payload.get('messages'), list)
            and not any(payload.get(k) for k in ('tools', 'functions', 'function_call', 'tool_choice'))
            and payload.get('modalities', ['text']) == ['text'])


class ModelRecovery:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def log(self, diagnostic):
        with (self.root/'transport.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'timestamp': stamp(), **diagnostic}, ensure_ascii=False)+'\n')

    def execute(self, client, body, key):
        deadline = time.monotonic()+client.total_timeout
        if not getattr(client, '_recovery_protocol_verified', False):
            for attempt in range(3):
                try:
                    capabilities = json.loads(client._exchange(
                        None, key, str(uuid.uuid4()), path='/status', deadline=deadline))
                    break
                except ProviderError as exc:
                    remaining = deadline-time.monotonic()
                    if not _transient_transport_error(exc) or attempt == 2 or remaining <= 0:
                        raise
                    self.log({'phase': 'recovery_status_retry', 'attempt': attempt+1,
                              'category': exc.category, 'model': client.model})
                    time.sleep(min(1 << attempt, remaining))
            client._recovery_supported = capabilities.get('recovery_protocol') == 1
            client._recovery_protocol_verified = True
        supported = getattr(client, '_recovery_supported', True)
        binding = hashlib.sha256(client.endpoint.encode()+b'\0'+hashlib.sha256(key.encode()).digest()+b'\0'+body).hexdigest()
        path = self.root/(binding+'.json')
        record = json.loads(path.read_text('utf-8')) if path.exists() else {
            'schema_version': '1.0', 'binding': binding, 'request_id': str(uuid.uuid4()),
            'state': 'NEW', 'created_at': stamp(), 'created_epoch': time.time(),
            'recovery_protocol': 1 if supported else 0, 'attempts': []}
        if record.get('binding') != binding:
            raise ProviderError('Model request checkpoint binding changed.', category='integrity')
        result_path = self.root/(binding+'.response')
        if record['state'] == 'COMPLETED':
            if not result_path.is_file() or digest(result_path) != record.get('response_sha256'):
                raise ProviderError('Model response checkpoint content changed.', category='integrity')
            self.log({'phase': 'local_response_reused', 'request_id': record['request_id'], 'model': client.model})
            return result_path.read_bytes()
        if record['state'] == 'REJECTED':
            record.update(state='NEW', request_id=str(uuid.uuid4()), created_epoch=time.time())
            atomic_json(path, record)
        if time.time()-record.get('created_epoch', 0) >= 86400:
            raise ProviderError('Uncertain request exceeded gateway retention; automatic regeneration blocked.', category='outcome_uncertain')
        if record['state'] != 'NEW' and (not supported or record.get('recovery_protocol') != 1):
            raise ProviderError('Legacy gateway cannot query this uncertain request; checkpoint retained, no resubmission.',
                                category='outcome_uncertain', details={'request_id':record['request_id']})
        submissions = 0
        budget=getattr(client,'submission_budget',3)
        max_submissions=min(3,budget) if type(budget) is int else 3
        # A resumed uncertain request must be queried before any resubmission.
        query_first = record['state'] != 'NEW'
        status_grace_used = False
        while True:
            if time.monotonic() >= deadline:
                # The gateway marks a lost worker terminal after 210 seconds;
                # the normal client window is 200. Query through that small
                # gap once instead of asking the user to click Continue.
                if (query_first and supported and not status_grace_used
                        and getattr(client, 'terminal_text_regeneration', False) is True):
                    status_grace_used = True
                    deadline = time.monotonic()+30
                    self.log({'phase':'terminal_status_grace', 'request_id':record['request_id'],
                              'model':client.model, 'seconds':30})
                else:
                    break
            if query_first:
                try:
                    raw = client._exchange(None, key, record['request_id'],
                        path='/requests/'+record['request_id'], deadline=deadline)
                    status = json.loads(raw)
                    state = status.get('state')
                    if state == 'COMPLETED':
                        raw = client._exchange(None, key, record['request_id'],
                            path='/requests/'+record['request_id']+'/result', deadline=deadline)
                        return self._save(path, result_path, record, raw)
                    if state == 'RUNNING':
                        time.sleep(min(2, max(0, deadline-time.monotonic()))); continue
                    if state == 'FAILED' and status.get('status_code') in (400,401,402,403,422,429,503):
                        record.update(state='REJECTED', last_error={'status_code':status['status_code']})
                        atomic_json(path, record)
                        raise ProviderError('Gateway recorded a definite upstream rejection; resolve it before retry.',
                                            status_code=status['status_code'],category='upstream_rejected')
                    if _terminal_text_recoverable(client, body, status, record):
                        record['terminal_regenerations'] = [{'request_id':record['request_id'],
                            'gateway_state':state, 'error_code':status['error_code'],
                            'at':stamp(), 'policy':'terminal_text_once_v1'}]
                        self.log({'phase':'terminal_text_regeneration', 'request_id':record['request_id'],
                                  'model':client.model, 'gateway_error':status['error_code']})
                        record.update(state='NEW', request_id=str(uuid.uuid4()),
                                      created_epoch=time.time(), updated_at=stamp())
                        atomic_json(path, record)
                        # Only the confirmed terminal text loss gets one fresh
                        # deadline. Running/unknown requests never extend it.
                        deadline = time.monotonic()+client.total_timeout
                        query_first = False
                        continue
                    # Unknown/expired/failed requests cannot be blindly regenerated.
                    raise ProviderError('Gateway request outcome is uncertain; checkpoint retained for recovery.',
                                        category='outcome_uncertain', details={'request_id': record['request_id'], 'gateway_state': state})
                except ProviderError as exc:
                    if exc.status_code == 404 and exc.details.get('code') == 'request_not_found':
                        pass  # Same ID is safe only with the v1 recovery protocol.
                    elif _transient_transport_error(exc):
                        time.sleep(min(2, max(0, deadline-time.monotonic()))); continue
                    else:
                        raise
            if submissions >= max_submissions:
                break
            submissions += 1
            record.update(state='SUBMITTED', updated_at=stamp())
            record['attempts'].append({'submitted_at': stamp(), 'request_id': record['request_id']})
            atomic_json(path, record)  # Persist BEFORE network IO.
            try:
                raw = client._exchange(body, key, record['request_id'], recoverable=bool(supported), deadline=deadline)
                return self._save(path, result_path, record, raw)
            except ProviderError as exc:
                record.update(state='REJECTED' if exc.status_code in (400,401,402,403,422,429,503) else 'UNCERTAIN',
                              last_error={'category': exc.category, 'status_code': exc.status_code})
                atomic_json(path, record)
                if exc.status_code in (429, 503):
                    # A definite rejection may use a fresh ID; preserve the old receipt.
                    if submissions >= max_submissions:
                        exc.retryable = False
                        raise
                    time.sleep(min(2**submissions, max(0, deadline-time.monotonic())))
                    record.update(state='NEW', request_id=str(uuid.uuid4()))
                    atomic_json(path, record); query_first = False
                elif _transient_transport_error(exc) or exc.status_code in (409,502,504):
                    if not supported:
                        self.log({'phase':'recovery_unavailable_on_legacy_gateway','request_id':record['request_id'],'model':client.model})
                        raise
                    query_first = True
                else:
                    raise
        raise ProviderError('Model request recovery deadline reached; use the saved request ID to resume.',
                            category='outcome_uncertain', details={'request_id': record['request_id']})

    def _save(self, path, result_path, record, raw):
        temporary = result_path.with_suffix('.tmp')
        temporary.write_bytes(raw); temporary.replace(result_path)
        record.update(state='COMPLETED', response_sha256=digest(result_path), completed_at=stamp())
        atomic_json(path, record)
        return raw


def bind_recovery(provider, root):
    """Only real clients participate; custom/offline providers remain compatible."""
    clients = getattr(provider, '_clients', {'deepseek': getattr(provider, 'client', None)})
    for name, client in clients.items():
        from .provider import ProviderClient
        if isinstance(client, ProviderClient) and client.gateway:
            client.recovery = ModelRecovery(Path(root)/name)
            if name == 'deepseek':
                client.stream = True
                client.terminal_text_regeneration = True
    return provider


def model_preflight(provider, cache_root):
    """Five-minute, hash-bound real planning probe, before expensive native analysis."""
    from .provider import ProviderClient
    from .deepseek import ModelOutputError
    client = getattr(provider, 'client', None)
    if not isinstance(client, ProviderClient):
        return {'status': 'not_applicable'}
    cache_root = Path(cache_root); cache_root.mkdir(parents=True, exist_ok=True)
    import os
    credential = client._key_value or os.environ.get(client.key_env, '')
    identity = hashlib.sha256(json.dumps(provider.config, sort_keys=True).encode()+hashlib.sha256(credential.encode()).digest()).hexdigest()
    cache = cache_root/(identity+'.json')
    if cache.is_file():
        saved = json.loads(cache.read_text('utf-8'))
        if saved.get('identity') == identity and 0 <= time.time()-saved.get('checked_epoch', 0) < 300:
            return {**saved, 'cached': True}
    # A successful probe expires after five minutes. An interrupted probe is
    # different: preserve its request ID across resumes so the gateway can
    # retrieve a completed response instead of receiving a second paid request.
    marker = cache_root/(identity+'.probe.json')
    pending = json.loads(marker.read_text('utf-8')) if marker.is_file() else None
    reuse = (isinstance(pending, dict) and pending.get('identity') == identity
             and pending.get('state') in ('RUNNING', 'WAITING')
             and 0 <= time.time()-pending.get('created_epoch', 0) < 86400
             and isinstance(pending.get('directory'), str)
             and re.fullmatch(r'probe-[a-f0-9]{32}', pending['directory']))
    if not reuse:
        pending = {'identity': identity, 'directory': 'probe-'+uuid.uuid4().hex,
                   'created_epoch': time.time(), 'state': 'RUNNING'}
        atomic_json(marker, pending)
    old = client.recovery; old_limit=client.max_tokens
    client.recovery = ModelRecovery(cache_root/pending['directory']); client.max_tokens=256
    try:
        probe_messages = [{'role': 'user', 'content': '只返回JSON对象：{"ok":true}'}]
        probe_error = None
        for attempt in range(2):
            try:
                response = provider._call(probe_messages, json_object=True)
                if json.loads(response['message']['content']) == {'ok': True}:
                    probe_error = None
                    break
                probe_error = 'Planning model health probe returned a different JSON object.'
            # Length is a definite completed response, so one distinct, stricter
            # probe is safe. Content filtering must still stop immediately.
            except ModelOutputError as exc:
                if getattr(exc,'finish_reason',None)!='length':raise
                probe_error = 'Planning model health probe response exceeded its length limit.'
            except (TypeError, ValueError):
                probe_error = 'Planning model health probe returned malformed JSON.'
            if attempt == 0:
                probe_messages = [{'role': 'user', 'content':
                    '上次健康检查响应不是可解析JSON。请严格只返回一个对象：{"ok":true}，不要代码围栏或其他文字。'}]
        if probe_error:
            raise ProviderError(probe_error, category='preflight')
    except ProviderError as exc:
        pending.update(state='WAITING' if _transient_transport_error(exc) or exc.category == 'outcome_uncertain'
                       else 'REJECTED', last_category=exc.category, updated_at=stamp())
        atomic_json(marker, pending)
        raise
    finally:
        client.recovery = old
        client.max_tokens = old_limit
    result = {'schema_version': '1.0', 'identity': identity, 'checked_epoch': time.time(),
              'checked_at': stamp(), 'status': 'passed', 'cached': False,
              'gateway_recovery_available':getattr(client,'_recovery_supported',False)}
    atomic_json(cache, result)
    pending.update(state='COMPLETED', completed_at=stamp())
    atomic_json(marker, pending)
    return result
