"""Serial external-model/tool loop. No Codex service dependency or generic shell."""
import hashlib
import json
import os
from pathlib import Path
import time

from .image_payload import data_url, fit_png_bytes

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = {'max_model_calls': 12, 'max_tool_calls': 24, 'token_stop_threshold': 80000,
            'max_images_per_call': 20, 'max_image_bytes': 4194304,
            'max_tool_result_chars': 60000, 'max_identical_failures': 2}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path = Path(path)
    temp = path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temp.replace(path)


def load_config(path):
    path = Path(path).resolve(strict=True)
    data = read(path)
    if data.get('schema') != 'jianying-external-agent-v1':
        raise ValueError('Expected jianying-external-agent-v1 configuration')
    # ``install_dir`` is retained only as an optional legacy compatibility
    # value.  The direct FFmpeg backend must be loadable on a machine without
    # Jianying installed.
    for key in ('provider', 'toolchain_root'):
        if key not in data:
            raise ValueError('Missing configuration field: ' + key)
    if any(k in data['provider'] for k in ('api_key', 'key', 'token')):
        raise ValueError('Use api_key_env, not a secret value in the configuration')
    data['limits'] = {**DEFAULTS, **data.get('limits', {})}
    for name, value in data['limits'].items():
        if name not in DEFAULTS or type(value) is not int or value <= 0:
            raise ValueError('Invalid runtime limit: ' + name)
    provider = data['provider']
    profiles = [data[k] for k in ('provider', 'vision_provider', 'complex_vision_provider') if data.get(k)]
    if any(p.get('transport') == 'gateway' for p in profiles):
        from .gateway_credentials import validate_profile
        origins = {validate_profile(p) for p in profiles}
        if len(origins) != 1:
            raise ValueError('All gateway model profiles must use the same origin')
    for name in ('base_url', 'model', 'api_key_env'):
        if not isinstance(provider.get(name), str) or not provider[name].strip():
            raise ValueError('Missing provider field: ' + name)
    if not provider.get('capabilities', {}).get('tool_calls'):
        raise ValueError('The selected provider profile must support tool calls')
    from .provider import ProviderClient
    ProviderClient(provider)  # Static endpoint/config validation; no key read or network.
    if 'visual_review_provider' in data:
        raise ValueError('Move Max to complex_vision_provider; output review now uses vision_provider (Omni)')
    for name in ('vision_provider', 'complex_vision_provider'):
        if name in data:
            from .model_router import validate_vision_config
            validate_vision_config(data[name])
    if data.get('complex_vision_provider') and not data.get('vision_provider'):
        raise ValueError('complex_vision_provider requires vision_provider')
    if data.get('vision_provider'):
        from .model_router import create_provider
        create_provider(data)  # Validate semantic role assignment and routing, no network/key read.
    toolchain = Path(data['toolchain_root'])
    if not toolchain.is_absolute():
        data['toolchain_root'] = str((path.parent / toolchain).resolve(strict=True))
    if data.get('asr_model_dir'):
        model_dir = Path(data['asr_model_dir'])
        data['asr_model_dir'] = str((path.parent / model_dir).resolve()) if not model_dir.is_absolute() else str(model_dir)
    # Portable bundles may keep their standalone renderer/resources beside the
    # config.  Resolve these paths once so worker processes do not depend on
    # the caller's current working directory.
    for key in ('direct_ffmpeg_dir', 'direct_resources_root', 'direct_output_dir'):
        value = data.get(key)
        if value:
            candidate = Path(value)
            if not candidate.is_absolute():
                data[key] = str((path.parent / candidate).resolve())
    return data


def doctor(config):
    from .provider import ProviderClient
    from .resource_policy import capability_report
    ProviderClient(config['provider'])
    scripts = Path(config['toolchain_root'])/'scripts'
    required = ('autocut_task.py', 'native_timeline.py', 'native_preview.py', 'draft_quality.py', 'quality_review.py')
    missing = [p for p in required if not (scripts/p).is_file()]
    return {'runtime': 'standalone-python', 'model': config['provider']['model'],
            'model_transport': config['provider'].get('transport', 'direct'),
            'visual_model': (config.get('vision_provider') or {}).get('model'),
            'visual_review_model': (config.get('vision_provider') or {}).get('model'),
            'complex_visual_model': (config.get('complex_vision_provider') or {}).get('model'),
            'visual_routing': config.get('visual_routing'),
            'qwen_key_present': (bool(os.environ.get(config['vision_provider']['api_key_env']))
                                 if config.get('vision_provider') else None),
            'endpoint': config['provider']['base_url'],
            'key_environment_variable': config['provider']['api_key_env'],
            'key_present': bool(os.environ.get(config['provider']['api_key_env'])),
            'toolchain_missing_files': missing,
            'jianying_install_exists': bool(config.get('install_dir')) and
                                      Path(config['install_dir']).is_dir(),
            'network_requested': False, 'model_access_verified': False,
            'codex_session_required': False,
            'output_resource_policy': capability_report()}


def image_messages(results, run_dir, limits, vision):
    content = []
    omitted = []
    count = 0
    for name, result in results:
        if name not in ('sample_frames', 'preview_draft') or result.get('ok') is not True:
            continue
        for frame in result.get('frames', []):
            label = {'tool': name, 'source_id': result.get('source_id'), 'built_id': result.get('built_id'),
                     'requested_us': frame.get('requested_us'), 'native_pts_us': frame.get('native_pts_us'),
                     'sha256': frame.get('sha256')}
            if not vision or count >= limits['max_images_per_call']:
                omitted.append(label); continue
            path = Path(frame['path']).resolve(strict=True)
            if not path.is_relative_to(run_dir.resolve()) or path.suffix.lower() != '.png':
                raise ValueError('Only run-owned native PNG frames can be sent to the model')
            if path.stat().st_size > limits['max_image_bytes']:
                omitted.append(label); continue
            raw = path.read_bytes()
            if not raw.startswith(b'\x89PNG\r\n\x1a\n') or hashlib.sha256(raw).hexdigest() != frame['sha256']:
                raise ValueError('Native frame integrity check failed')
            payload = fit_png_bytes(raw)
            if payload != raw:
                label.update({'payload_sha256': hashlib.sha256(payload).hexdigest(),
                              'payload_bytes': len(payload), 'payload_compressed': True})
            content.extend([{'type': 'text', 'text': 'ACTUAL NATIVE FRAME '+json.dumps(label, ensure_ascii=False)},
                            {'type': 'image_url', 'image_url': {'url': data_url(payload)}}])
            count += 1
    if omitted:
        content.append({'type': 'text', 'text': 'These frames were NOT sent or seen. Request smaller batches; do not claim to have viewed them: '+json.dumps(omitted, ensure_ascii=False)})
    return [{'role': 'user', 'content': content}] if content else []


def limit_history_images(messages, maximum):
    """Bound the entire next API request, not only its most recent image batch."""
    positions=[]
    for mi,message in enumerate(messages):
        if isinstance(message.get('content'),list):
            for pi,part in enumerate(message['content']):
                if isinstance(part,dict) and part.get('type')=='image_url':
                    positions.append((mi,pi))
    for mi,pi in positions[:max(0,len(positions)-maximum)]:
        messages[mi]['content'][pi]={'type':'text','text':
            'Earlier image omitted from this API request to respect the image budget. Refer to earlier recorded findings or request it again; this text is not fresh visual evidence.'}


def run(config, sources, constraints, output, *, client=None, hub_class=None, stop_event=None):
    from .resource_policy import require_internal_resources
    require_internal_resources('legacy_tool_loop')
    if client is None:
        from .provider import ProviderClient
        client = ProviderClient(config['provider'])
    if hub_class is None:
        from .tools import ToolHub
        hub_class = ToolHub
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('Use a new independent run directory')
    selected = [Path(p).resolve(strict=True) for p in sources]
    if not selected:
        raise ValueError('At least one material source is required')
    for source in selected:
        if source.is_dir() and output.is_relative_to(source):
            raise ValueError('Keep output outside the input material directory')
    output.mkdir(parents=True)
    lock = output/'.runtime.lock'
    lock.write_text(str(os.getpid()), encoding='utf-8')
    state = {'schema': 'jianying-external-run-v1', 'runtime': 'standalone-python',
             'provider_model': config['provider']['model'], 'started_at': time.time(),
             'model_calls': 0, 'tool_calls': 0, 'reported_tokens': 0,
             'status': 'running', 'quality_status': 'not_certified', 'events': []}
    save(output/'runtime-state.json', state)
    try:
        hub = hub_class(Path(config['toolchain_root']), Path(config['install_dir']), output, selected, constraints)
        prompt = (ROOT/'agent-prompt.md').read_text(encoding='utf-8')
        prompt += '\nActual external model identity: '+config['provider']['model']
        messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content':
          '请自主处理本次提供的素材。素材仅由 inventory 工具给出。约束：'+constraints}]
        failures = {}
        limits = config['limits']
        while state['model_calls'] < limits['max_model_calls']:
            if stop_event is not None and stop_event.is_set():
                state['status'] = 'stopped_by_user'; break
            if state['reported_tokens'] >= limits['token_stop_threshold']:
                state['status'] = 'token_threshold_reached'; break
            state['model_calls'] += 1
            limit_history_images(messages, limits['max_images_per_call'])
            response = client.complete(messages, hub.definitions())
            if stop_event is not None and stop_event.is_set():
                state['status'] = 'stopped_by_user'; break
            usage = response.get('usage') or {}
            tokens = usage.get('total_tokens', 0)
            if type(tokens) is not int or tokens < 0:
                raise ValueError('Provider returned invalid token usage')
            state['reported_tokens'] += tokens
            state['events'].append({'type': 'model_response', 'request_id': response.get('request_id'),
              'model': response.get('model'), 'finish_reason': response.get('finish_reason'), 'usage': usage})
            message = response['message']
            if response.get('finish_reason') in ('length', 'content_filter'):
                state['status'] = 'model_response_incomplete'; break
            calls = message.get('tool_calls') or []
            messages.append(message)
            if not calls:
                state['status'] = 'model_finished_with_limits'
                state['model_summary'] = message.get('content', '')
                break
            if state['tool_calls']+len(calls) > limits['max_tool_calls']:
                state['status'] = 'tool_limit_reached'; break
            results = []
            for call in calls:
                if stop_event is not None and stop_event.is_set():
                    state['status'] = 'stopped_by_user'; break
                fn = call['function'];name = fn['name'];args = json.loads(fn['arguments'])
                signature = name+json.dumps(args, sort_keys=True, ensure_ascii=False)
                if failures.get(signature, 0) >= limits['max_identical_failures']:
                    state['status'] = 'repeated_tool_failure'; break
                state['tool_calls'] += 1
                result = hub.execute(name, args)
                failures[signature] = failures.get(signature, 0)+1 if result.get('ok') is not True else 0
                artifact = output/f'tool-{state["tool_calls"]:04d}.json'
                save(artifact, {'tool': name, 'arguments': args, 'result': result})
                state['events'].append({'type': 'tool_result', 'name': name, 'ok': result.get('ok'), 'artifact': str(artifact)})
                encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
                if len(encoded) > limits['max_tool_result_chars']:
                    encoded = json.dumps({'ok': False, 'error': 'Tool response exceeds current context budget; request smaller scope. Full result persisted locally.',
                       'result_chars': len(encoded), 'artifact_name': artifact.name}, ensure_ascii=False)
                messages.append({'role': 'tool', 'tool_call_id': call['id'], 'content': encoded})
                results.append((name, result))
                save(output/'runtime-state.json', state)
            if state['status'] != 'running':
                break
            messages.extend(image_messages(results, output, limits, bool(config['provider'].get('capabilities', {}).get('vision'))))
        else:
            state['status'] = 'model_call_limit_reached'
    except Exception as exc:
        # Native execution details live in tool reports. Transport errors are
        # controlled by provider.py; do not dump request messages or headers.
        state['status'] = 'failed'
        state['error_type'] = type(exc).__name__
        if isinstance(exc, (ValueError, FileExistsError)):
            state['error'] = str(exc)
    finally:
        state['finished_at'] = time.time()
        state['verified_delivery'] = False
        state['limitations'] = ['Initial external-model adapter: final model text is not a quality acceptance certificate.',
                               'No native MP4 export, automatic full listening, homepage registration or arbitrary shell tool.',
                               'Token threshold is based on reported usage; it is not an exact monetary spending cap.',
                               'Run artifacts persist for diagnosis; interrupted execution is not automatically resumed.']
        save(output/'runtime-state.json', state)
        lock.unlink(missing_ok=True)
    return state
