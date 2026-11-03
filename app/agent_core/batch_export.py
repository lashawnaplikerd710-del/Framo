"""Independent-output preparation with honest native-export reporting."""
from __future__ import annotations

import json
from pathlib import Path

from .master_draft import extract_output, write_snapshot_draft, MasterDraftError
from .native_export import export as native_export
from .task_store import atomic_json, digest


def prepare_independent_outputs(master_snapshot, destination, *, template=None):
    """Split a master snapshot into zero-based temporary drafts.

    This is the reliable fallback when the installed native CLI has no range
    export command. It never labels a temporary draft as an MP4 delivery.
    """
    destination = Path(destination).resolve()
    if destination.exists():
        existing = destination / 'independent-export-report.json'
        if existing.is_file():
            try:
                return json.loads(existing.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                pass
        raise FileExistsError('independent output directory already exists without a valid report')
    destination.mkdir(parents=True)
    rows = []
    for item in master_snapshot.get('batch_outputs', []):
        output_id = item['output_id']
        snapshot = extract_output(master_snapshot, output_id)
        folder = destination / output_id
        write_snapshot_draft(snapshot, folder, template=template)
        rows.append({'output_id': output_id, 'draft_path': str(folder),
                     'duration_us': snapshot['duration'],
                     'status': 'temporary_draft_ready_for_export',
                     'mp4_path': None, 'native_export_verified': False})
    report = {'schema_version': '1.0', 'status': 'temporary_drafts_ready',
              'native_range_export_supported': False, 'outputs': rows,
              'source_snapshot_sha256': digest(Path(destination.parent) / 'master-snapshot.json')
              if (destination.parent / 'master-snapshot.json').is_file() else None}
    atomic_json(destination / 'independent-export-report.json', report)
    return report


def export_independent_outputs(master_snapshot, destination, *, install=None,
                               template=None, runner=None):
    """Prepare and, only when explicitly supported, export each output.

    ``native_export`` performs the installed-CLI capability check for every
    child. If it returns EXPORT_COMMAND_UNAVAILABLE, all child reports remain
    honest and the temporary drafts are retained for manual/native export.
    """
    report = prepare_independent_outputs(master_snapshot, destination, template=template)
    if not install:
        report['status'] = 'temporary_drafts_ready_for_export'
        return report
    for row in report['outputs']:
        if row.get('native_export_verified'):
            continue
        output_dir = Path(row['draft_path']).parent / (row['output_id'] + '-mp4')
        if output_dir.exists():
            # Native export outputs are immutable evidence. A retry gets its
            # own directory instead of overwriting a failed or partial run.
            attempt = 2
            while (Path(row['draft_path']).parent /
                   f"{row['output_id']}-mp4-attempt-{attempt}").exists():
                attempt += 1
            output_dir = Path(row['draft_path']).parent / f"{row['output_id']}-mp4-attempt-{attempt}"
        result = native_export(install, row['draft_path'], output_dir, runner=runner)
        row['export_report'] = result
        row['mp4_path'] = str(output_dir / 'native-output.mp4') if result.get('output_exists') else None
        row['native_export_verified'] = bool(result.get('native_export_verified'))
        row['status'] = 'exported_needs_quality_review' if row['native_export_verified'] else result.get('status', 'export_failed')
        if result.get('status') == 'EXPORT_COMMAND_UNAVAILABLE':
            report['native_range_export_supported'] = False
            # Do not waste attempts on the remaining children when the same
            # installed contract is unavailable; their drafts remain usable.
            for pending in report['outputs']:
                if 'export_report' not in pending:
                    pending['status'] = 'temporary_draft_ready_for_export'
            break
    if all(row.get('native_export_verified') for row in report['outputs']):
        report['status'] = 'all_outputs_exported_needs_quality_review'
    elif any(row.get('native_export_verified') for row in report['outputs']):
        report['status'] = 'partial_export'
    else:
        report['status'] = 'temporary_drafts_ready_for_export'
    atomic_json(Path(destination) / 'independent-export-report.json', report)
    return report
