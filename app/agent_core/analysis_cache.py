"""Content-addressed, local analysis cache for immutable media evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .task_store import atomic_json


class AnalysisCache:
    def __init__(self, root):
        self.root = Path(root).resolve() / 'cache' / 'analysis'

    def key(self, *, source_sha256, language, engine, model_identity=''):
        raw = json.dumps({'source_sha256': source_sha256, 'language': language,
                          'engine': engine, 'model_identity': model_identity},
                         sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(raw).hexdigest()

    def read(self, kind, key):
        path = self.root / kind / (key + '.json')
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) and value.get('cache_key') == key else None

    def write(self, kind, key, value):
        path = self.root / kind / (key + '.json')
        if path.exists():
            return path
        atomic_json(path, {**value, 'cache_key': key, 'cache_kind': kind})
        return path
