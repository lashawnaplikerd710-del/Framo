"""Project-wide output resource boundary; no prompt/config can enable substitutes.

Official authenticated query/download adapters and five native resource samples
are connected. TaskService has an explicit official CLI profile and model
selection stage. Publishing-scope checks and final delivery acceptance remain
incomplete. The old external packager must stay disabled.
"""
from __future__ import annotations

from datetime import datetime, timezone
from contextlib import closing
import os
from pathlib import Path
import sqlite3

POLICY = 'jianying_internal_only_v1'
CATEGORIES = ('music', 'sfx', 'stickers', 'transitions', 'effects', 'fonts', 'tts')


class ResourcePolicyBlocked(ValueError):
    def __init__(self, operation):
        self.report = capability_report()
        self.report['operation'] = operation
        super().__init__('BLOCKED_RESOURCES: 仅允许用户素材及剪映官方免费元素；'
                         '官方查询、非会员下载及音乐/音效/贴纸/转场/特效样本已接通。'
                         '当前入口或配置未通过原生资源交付条件；新原生配置单独核验，旧外部路径保持停用。')


def capability_report():
    return {
        'schema_version': '1.0', 'policy': POLICY, 'status': 'BLOCKED_RESOURCES',
        'official_catalog_adapter_ready': True,
        'native_packaging_executor_ready': True,
        'native_packaging_plan_source': 'DeepSeek selection from exact-query verified official candidates, or explicit typed plan',
        'authenticated_host_required': True,
        'runtime_connection_checked': False,
        'taskservice_native_packaging_integrated': True,
        'status_scope': 'default/legacy entry points remain blocked; official_cli_v1 profile has runtime preflight',
        'native_resource_download_replanning_ready': True,
        'full_delivery_accepted': False,
        'last_native_sample_validation': '2026-09-28; installed build 11.5.0.14471',
        'external_fallback_allowed': False, 'member_resources_allowed': False,
        'cache_path_is_provenance': False,
        'categories': {name: ({'status': 'partially_connected',
                              'verified_builtin_ids': ['source_han_cn', 'noto_sans'],
                              'scope': 'pinned bundled files; native text/save/reload/pixels tested',
                              'online_font_catalog_connected': False} if name == 'fonts' else
                             {'status': 'sample_connected',
                              'scope': 'official query/download and native editable sample; not all resources',
                              'publishing_scope_verified': False,
                              'automatic_task_packaging_ready': False} if name != 'tts' else
                             {'status': 'sample_connected', 'resource_id': '7023300468310807070',
                              'title': '亲切女声', 'scope': 'official real TTS with native text_to_audio readback',
                              'automatic_product_ad_narration_profile': 'new_native',
                              'runtime_free_status_recheck_required': True})
                       for name in CATEGORIES},
        'source_media_and_source_audio': 'permitted; supplied media only',
        'copywriting': 'model-authored text permitted; rendering/font must satisfy this policy',
        'native_primitives': 'cut/order/crop/gain/fades permitted; not evidence of catalog access',
        'required_evidence': ['official resource identity and origin', 'explicit free/nonmember status',
                              'applicable usage rights', 'local content identity',
                              'native resource object application and saved-draft readback'],
    }


def bind_policy(task):
    """Missing policy in historical specs inherits the stricter current policy."""
    if task.get('resource_policy', POLICY) != POLICY:
        raise ValueError('Only resource_policy=' + POLICY + ' is supported')
    return {**task, 'resource_policy': POLICY}


def require_internal_resources(operation):
    # Intentional capability stop, not a user-toggleable permission gate.
    # Replace only with a tested official catalog/application adapter.
    raise ResourcePolicyBlocked(operation)


def inspect_local_catalog(user_data=None):
    """Count only resource tables. Never read account/auth blobs or infer licenses."""
    root = Path(user_data) if user_data else Path(os.environ.get('LOCALAPPDATA', '')) / 'JianyingPro/User Data'
    cache = root / 'Cache/ressdk_db'
    result = capability_report()
    result.update({'checked_at': datetime.now(timezone.utc).isoformat(),
                   'network_requested': False, 'gui_started': False,
                   'catalog_databases': [], 'evidence_scope': 'read-only local catalog row counts; not a live host API test'})
    try:
        paths = sorted(cache.rglob('rp.db')) if cache.is_dir() else []
        for path in paths:
            row = {'database': str(path), 'tables': {}}
            try:
                with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                    db.execute('PRAGMA query_only=ON')
                    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    for name in ('effect', 'loki_effect', 'music'):
                        if name in tables:
                            row['tables'][name] = db.execute('SELECT count(*) FROM ' + name).fetchone()[0]
            except (sqlite3.Error, OSError) as exc:
                row['inspection_error'] = type(exc).__name__
            result['catalog_databases'].append(row)
    except OSError as exc:
        result['inspection_error'] = type(exc).__name__
    return result
