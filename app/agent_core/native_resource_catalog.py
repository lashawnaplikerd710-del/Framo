"""Official query -> explicit nonmember check -> native download -> file hashes.

Resource metadata does not certify every publishing territory/use, nor prove
that an effect actually renders. Those remain separate acceptance stages.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .internal_resources import RESOURCE_TYPES
from .task_store import atomic_json
from .file_io import io_path, logical_path


class ResourceFailure(ValueError):
    pass


def entitlement(resource_type, row):
    if resource_type == 'music':
        free = row.get('paid_mode') == 'free' and row.get('subscribe_package') in ('', None)
        field, commercial = 'is_commerce', row.get('is_commerce')
    else:
        free = row.get('is_vip') is False and row.get('paid_type') in (None, 0, 'free')
        field, commercial = 'is_business', row.get('is_business')
    if row.get('is_vip') is True or row.get('paid_mode') in ('subscribe', 'paid') or row.get('subscribe_package') not in (None, ''):
        free = False
    return {'nonmember_metadata_verified': free,
            'commercial_field': field, 'commercial_field_value': commercial if type(commercial) is bool else None,
            'publishing_scope_verified': False}


def envelope(client, arguments, timeout=30):
    result = client.run(arguments, timeout=timeout)
    try:
        data = json.loads(result.stdout.decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        raise ResourceFailure('native_resource_response_invalid') from None
    if not isinstance(data, dict) or result.returncode != 0 or data.get('err_code') != 0:
        code=data.get('err_code') if isinstance(data,dict) else None
        suffix=':'+str(code) if type(code) is int else ''
        raise ResourceFailure('native_resource_command_failed'+suffix)
    if not isinstance(data.get('result'), dict):
        raise ResourceFailure('native_resource_result_invalid')
    return data['result']


def acquire(client, resource_type, resource_id, output):
    if resource_type not in RESOURCE_TYPES or not isinstance(resource_id, str) or not re.fullmatch(r'[0-9]{1,32}', resource_id):
        raise ValueError('invalid_native_resource_identity')
    output = Path(output).resolve()
    if output.exists() or not str(output).isascii() or len(str(output)) > 160 or output.is_relative_to(client.host.install):
        raise ValueError('resource_acquisition_needs_new_short_ascii_directory_outside_install')
    result = envelope(client, ['material','query','--resource-id',resource_id,'--type',resource_type])
    items = result.get('items')
    matches = [row for row in items if isinstance(row, dict) and str(row.get('resource_id')) == resource_id] if isinstance(items, list) else []
    if len(matches) != 1:
        raise ResourceFailure('authoritative_resource_identity_missing_or_ambiguous')
    row = matches[0]
    rights = entitlement(resource_type, row)
    if not rights['nonmember_metadata_verified']:
        raise ResourceFailure('resource_requires_membership_or_free_status_is_unknown')
    if resource_type == 'music':
        platform = 'music'
    elif row.get('source') == 1:
        platform = 'vimo'
    else:
        raise ResourceFailure('resource_platform_not_yet_validated')
    output.mkdir(parents=True, exist_ok=False)
    fields = ('resource_id','title','is_vip','paid_type','paid_mode','subscribe_package',
              'is_business','is_commerce','source','effect_type','md5','duration')
    report = {'schema':'jianying-official-resource-v1','resource_type':resource_type,'resource_id':resource_id,
              'status':'query_verified','entitlement':rights,'source_metadata':{k:row[k] for k in fields if k in row},
              'download_transport':'official_app_owned_lyra','native_application_verified':False,
              'delivery_accepted':False,'credentials_disclosed':False}
    atomic_json(output/'provenance.json',report)
    payload = output/'payload'
    # This build needs existing parent/lock directories for reliable local IO.
    (payload/resource_id/'.locks').mkdir(parents=True)
    try:
        downloaded = envelope(client, ['material','download','--type',resource_type,'--resource-id',resource_id,
            '--resource-platform',platform,'--resource-dir',payload.as_posix()], timeout=60)
        if str(downloaded.get('resource_id')) != resource_id or not isinstance(downloaded.get('path'), str):
            raise ResourceFailure('download_identity_mismatch')
        path = Path(downloaded['path']).resolve(strict=True)
        if not path.is_relative_to(payload) or path.is_symlink():
            raise ResourceFailure('download_outside_resource_directory')
        physical=io_path(path)
        files = [physical] if physical.is_file() else sorted(physical.rglob('*'))
        records = []
        total = 0
        for item in files:
            logical=logical_path(item)
            if item.is_symlink() or not logical_path(item.resolve()).is_relative_to(payload):
                raise ResourceFailure('download_contains_external_link')
            if not item.is_file():
                continue
            total += item.stat().st_size
            if total > 512*1024*1024 or len(records) >= 10000:
                raise ResourceFailure('resource_package_exceeds_validation_budget')
            with item.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            records.append({'path':logical.relative_to(output).as_posix(),'bytes':item.stat().st_size,'sha256':digest})
        if not records or total <= 0:
            raise ResourceFailure('download_payload_empty')
        report.update(status='downloaded_nonmember_resource_needs_application_review',path=str(path),
                      files=records,total_bytes=total,file_integrity_recorded=True,
                      file_inventory='extended_windows_paths_v1')
    except ResourceFailure as exc:
        report.update(status='failed',reason=str(exc))
        raise
    except Exception:
        report.update(status='failed',reason='native_resource_acquisition_failed')
        raise ResourceFailure('native_resource_acquisition_failed') from None
    finally:
        atomic_json(output/'provenance.json',report)
    return report
