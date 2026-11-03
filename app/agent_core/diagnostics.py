"""Persisted model capability diagnostics containing no key or request payload."""
from __future__ import annotations

import os
from pathlib import Path

from .deepseek import DeepSeekProvider, ModelOutputError
from .provider import ProviderError
from .task_store import atomic_json, stamp


def check_model(config, report_path, *, progress=None, only_stage='all', provider_factory=DeepSeekProvider):
    provider = provider_factory(config)
    secret = os.environ.get(config['api_key_env'], '')
    credential_check = {
        'present': bool(secret.strip()),
        'looks_incomplete': bool(secret.strip()) and len(secret.strip()) < 16,
        'contains_whitespace': any(char.isspace() for char in secret),
    }
    del secret
    report = {'schema_version': '1.0', 'timestamp': stamp(),
              'model': provider.config['model'], 'credential_check': credential_check,
              'text_verified': False, 'json_verified': False, 'image_verified': False}
    try:
        report.update(provider.check(progress=progress, only_stage=only_stage))
        report['status'] = 'passed'
    except (ProviderError, ModelOutputError) as exc:
        report.update(getattr(provider, 'check_state', {}))
        report.update(status='error', error_type=type(exc).__name__, message=str(exc),
                      failed_stage=report.get('stage', 'initializing'))
        if isinstance(exc, ProviderError):
            report.update(http_status=exc.status_code, category=exc.category,
                          provider_error=exc.details)
    report['usage'] = dict(provider.usage)
    report_path = Path(report_path).resolve()
    report['report_path'] = str(report_path)
    atomic_json(report_path, report)
    return report
