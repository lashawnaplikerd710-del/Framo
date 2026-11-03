"""Gateway grants are scoped to an exact origin; provider keys are never loaded."""
from contextlib import contextmanager
import hashlib
import os
import re
from urllib.parse import urlsplit

ENV_NAME = 'JIANYING_GATEWAY_TOKEN'
TARGET_PREFIX = 'JianyingAutoEditAgent/Gateway/'


def validate_profile(config):
    url = urlsplit(config.get('base_url', ''))
    if (config.get('transport') != 'gateway' or config.get('api_key_env') != ENV_NAME or
            url.scheme != 'https' or not url.hostname or url.port not in (None, 443) or
            url.username is not None or url.password is not None or url.query or url.fragment or
            url.path != '/v1' or any(c.isspace() for c in config.get('base_url', '')) or
            any(k in config for k in ('api_key', 'key', 'token'))):
        raise ValueError('Gateway requires an exact HTTPS /v1 endpoint and a separate gateway token')
    if url.hostname.lower() in ('api.deepseek.com', 'dashscope.aliyuncs.com') or url.hostname.endswith('.maas.aliyuncs.com'):
        raise ValueError('Gateway grants cannot be sent to model-provider endpoints')
    return 'https://' + url.hostname.lower()


def target(config):
    return TARGET_PREFIX + hashlib.sha256(validate_profile(config).encode()).hexdigest()


def validate_token(value):
    if not isinstance(value, str) or not re.fullmatch(r'jyg_[A-Za-z0-9_-]{43}', value):
        raise ValueError('Invalid workbench access token; do not enter a model API key')
    return value


def save_token(config, value):
    from .credential_store import save_key
    save_key(validate_token(value), target=target(config))


@contextmanager
def credential_environment(config):
    from .credential_store import load_key
    validate_profile(config)
    previous = os.environ.get(ENV_NAME)
    value = previous or load_key(target=target(config))
    if value:
        os.environ[ENV_NAME] = validate_token(value)
    try:
        yield 'gateway_credential' if value else 'missing'
    finally:
        if previous is None:
            os.environ.pop(ENV_NAME, None)
        else:
            os.environ[ENV_NAME] = previous


def apply_gateway(config, base_url):
    """Return a separate profile. Do not modify the owner's direct profile."""
    import copy
    result = copy.deepcopy(config)
    for key in ('provider', 'vision_provider', 'complex_vision_provider'):
        if key in result:
            result[key].update(transport='gateway', base_url=base_url.rstrip('/'), api_key_env=ENV_NAME)
            validate_profile(result[key])
    return result
