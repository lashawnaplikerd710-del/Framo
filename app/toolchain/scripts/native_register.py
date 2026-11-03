"""Copy a native draft into a new folder and append its homepage registration."""
import argparse
import copy
import json
import os
from pathlib import Path
import re
import shutil
import sys
import time
import uuid
from contextlib import contextmanager

_runtime=Path(__file__).resolve().parents[2]
if str(_runtime) not in sys.path:sys.path.insert(0,str(_runtime))
from agent_core.file_io import io_path

from native_service import Service, NativeServiceError, validate_player_state
from native_draft_codec import editor_pids, file_digest, validate_json
from native_build import sidecar_metadata


def new_json(path, value):
    with io_path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)


def rebase(value, old, new, old_id, new_id):
    if isinstance(value, dict):
        return {key: rebase(item, old, new, old_id, new_id) for key, item in value.items()}
    if isinstance(value, list):
        return [rebase(item, old, new, old_id, new_id) for item in value]
    if isinstance(value, str):
        if value == old_id:
            return new_id
        normalized = value.replace('\\', '/')
        prefix = old.as_posix().rstrip('/')
        if normalized.casefold().startswith(prefix.casefold() + '/'):
            return str(new / normalized[len(prefix) + 1:])
        if normalized.casefold() == prefix.casefold():
            return str(new)
        if value.startswith(('{', '[')):
            try:
                nested = json.loads(value)
            except ValueError:
                pass
            else:
                if isinstance(nested, (dict, list)):
                    changed = rebase(nested, old, new, old_id, new_id)
                    if changed != nested:
                        return json.dumps(changed, ensure_ascii=False)
    return value


def root_entry(meta, media_size):
    entry = {key: meta[key] for key in ('draft_cover', 'draft_fold_path', 'draft_id',
        'draft_json_file', 'draft_name', 'draft_new_version', 'draft_root_path',
        'tm_draft_create', 'tm_draft_modified', 'tm_draft_removed', 'tm_duration')}
    entry.update({
        'cloud_draft_cover': False, 'cloud_draft_sync': False,
        'draft_cloud_last_action_download': False, 'draft_cloud_purchase_info': '',
        'draft_cloud_template_id': '', 'draft_cloud_tutorial_info': '',
        'draft_cloud_videocut_purchase_info': '', 'draft_is_ai_shorts': False,
        'draft_is_cloud_temp_draft': False, 'draft_is_infinite_canvas_draft': False,
        'draft_is_invisible': False, 'draft_is_pippit_draft': False,
        'draft_is_web_article_video': False, 'draft_type': '',
        'draft_web_article_video_enter_from': '', 'streaming_edit_draft_ready': True,
        'draft_timeline_materials_size': media_size, 'tm_draft_cloud_completed': '',
        'tm_draft_cloud_entry_id': -1, 'tm_draft_cloud_modified': 0,
        'tm_draft_cloud_parent_entry_id': -1, 'tm_draft_cloud_space_id': -1,
        'tm_draft_cloud_user_id': -1})
    return entry


@contextmanager
def index_lock(index):
    """The OS releases this lock even if a registration process is killed."""
    import msvcrt
    index.parent.mkdir(parents=True, exist_ok=True)
    lock = index.with_name(index.name + '.codex-registration.lock')
    with lock.open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError('Another homepage registration is active') from None
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def append_index(index, entry, report_dir, require_closed=True):
    with index_lock(index):
        return _append_index_locked(index, entry, report_dir, require_closed)


def _append_index_locked(index, entry, report_dir, require_closed=True):
    """Append one new identity, retaining every existing entry and unknown field."""
    if require_closed and editor_pids():
        raise RuntimeError('Save and exit Jianying before updating the homepage index')
    original = index.read_bytes() if index.exists() else None
    before = validate_json(original) if original is not None else {'all_draft_store': []}
    rows = before.get('all_draft_store')
    if not isinstance(rows, list):
        raise ValueError('Homepage index has an unexpected format')
    for row in rows:
        if isinstance(row, dict) and (row.get('draft_id') == entry['draft_id'] or
            str(row.get('draft_fold_path', '')).replace('\\', '/').casefold() ==
                entry['draft_fold_path'].replace('\\', '/').casefold()):
            raise ValueError('Draft identity or destination already registered')
    after = copy.deepcopy(before)
    after['all_draft_store'].insert(0, entry)
    assert after['all_draft_store'][1:] == before['all_draft_store']
    index.parent.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex
    temp = index.with_name(index.name + '.codex-' + nonce + '.tmp')
    backup = index.with_name(index.name + '.codex-backup-' + nonce) if original is not None else None
    # Process and byte checks also guard against the desktop editor or an
    # unrelated writer changing the index during preparation.
    try:
        if (index.read_bytes() if index.exists() else None) != original:
            raise RuntimeError('Homepage index changed; registration aborted')
        if require_closed and editor_pids():
            raise RuntimeError('Jianying restarted; registration aborted')
        if backup is not None:
            with backup.open('xb') as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        new_json(temp, after)
        if (index.read_bytes() if index.exists() else None) != original:
            raise RuntimeError('Homepage index changed before publication')
        if require_closed and editor_pids():
            raise RuntimeError('Jianying restarted before publication')
        os.replace(temp, index)
        persisted = validate_json(index.read_bytes())
        if persisted != after:
            raise RuntimeError('Homepage index readback differs; inspect the saved backup')
        return {'index': str(index), 'backup': str(backup) if backup else None,
            'new_entry_id': entry['draft_id'], 'old_entry_count': len(rows),
            'new_entry_count': len(rows) + 1, 'existing_entries_preserved': True,
            'index_readback_equal': True, 'gui_homepage_visibility': 'not_checked'}
    finally:
        # Only exact temporary files created by this invocation are removed.
        if temp.exists():
            temp.unlink()


def publish(install, source, draft_root, user_data, name, output, *, sandbox=False, before_register=None):
    source = Path(source).resolve(strict=True)
    draft_root = Path(draft_root).resolve(strict=True)
    user_data = Path(user_data).resolve(strict=True)
    output = Path(output).resolve()
    install = Path(install).resolve(strict=True)
    if not source.is_dir() or not draft_root.is_dir() or not user_data.is_dir():
        raise ValueError('Source draft, draft root and User Data must be existing directories')
    if (not name or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith((' ', '.'))
            or name in ('.', '..') or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I)):
        raise ValueError('Choose a valid, new Windows folder name')
    dest = draft_root / name
    if dest.exists() or output.exists() or dest.is_symlink() or output.is_symlink():
        raise FileExistsError('Draft destination and report directory must both be new')
    if (dest.is_relative_to(source) or source.is_relative_to(dest) or output.is_relative_to(source)
            or source.is_relative_to(output) or output.is_relative_to(dest) or dest.is_relative_to(output)
            or dest.is_relative_to(install) or output.is_relative_to(install)):
        raise ValueError('Source, destination and output must be separate, outside the app installation')
    index_requested = user_data / 'Projects/com.lveditor.draft/root_meta_info.json'
    index = index_requested.resolve()
    if not index.is_relative_to(user_data) or any(index.is_relative_to(p) for p in (source, dest, output, install)):
        raise ValueError('Homepage index must stay inside the selected User Data and outside source/destination/output/install')
    for parent in (index_requested, *index_requested.parents):
        if parent == user_data.parent:
            break
        if parent.is_symlink() or (hasattr(parent, 'is_junction') and parent.is_junction()):
            raise ValueError('Linked homepage index paths are not supported')
    if not sandbox and editor_pids():
        raise RuntimeError('Save and exit Jianying before publishing a registered draft')
    source_io=io_path(source)
    for item in source_io.rglob('*'):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise ValueError('Linked files/directories are not supported in a draft copy')
    source_file = source / 'draft_content.json'
    source_hashes = {str(p.relative_to(source_io)): file_digest(p) for p in source_io.rglob('*') if p.is_file()}
    output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'preparing', 'source': str(source), 'destination': str(dest),
              'gui_opened': False, 'sandbox': sandbox}
    try:
        with Service(install, output / 'service') as service:
            original = service.request('draft', 'readFile', {'path': str(source_file), 'rootPath': str(source)})['result']
            snapshot = validate_json(original['content'].encode('utf-8'))
            if not isinstance(snapshot.get('tracks'), list) or not isinstance(snapshot.get('materials'), dict) or not snapshot.get('id'):
                raise ValueError('Source is not a supported native timeline')
            old_id, new_id = snapshot['id'], str(uuid.uuid4()).upper()
            snapshot = rebase(snapshot, source, dest, old_id, new_id)
            snapshot['name'] = name
            skip = {'draft_content.json', 'draft_content.json.bak', 'draft_meta_info.json',
                    'draft_meta_info.json.bak', 'template-2.tmp'}
            dest.mkdir(exist_ok=False)
            report.update(destination_created=True,draft_id=new_id)
            for file in source_io.rglob('*'):
                relative = file.relative_to(source_io)
                if len(relative.parts) == 1 and file.name in skip:
                    continue
                target = io_path(dest / relative)
                if file.is_dir():
                    target.mkdir(exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if file.suffix == '.json':
                        try:
                            value = validate_json(file.read_bytes())
                        except (ValueError, UnicodeError):
                            shutil.copyfile(file, target)
                        else:
                            changed = rebase(value, source, dest, old_id, new_id)
                            if changed == value:
                                # Official package JSON is opaque content when
                                # it has no draft references; retain its bytes.
                                shutil.copyfile(file, target)
                            else:
                                new_json(target, changed)
                    else:
                        shutil.copyfile(file, target)
                    if file.suffix != '.json' and file_digest(file) != file_digest(target):
                        raise RuntimeError('Copied media or auxiliary file hash differs')
            now = time.time_ns() // 1000
            meta = sidecar_metadata(snapshot, dest, draft_root, now)
            for filename, value in [('draft_content.json', snapshot), ('draft_meta_info.json', meta)]:
                path = dest / filename
                service.request('draft', 'writeFile', {'path': str(path), 'rootPath': str(draft_root), 'content': json.dumps(value, ensure_ascii=False)})
                decoded = service.request('draft', 'readFile', {'path': str(path), 'rootPath': str(draft_root)})['result']
                if json.loads(decoded['content']) != value:
                    raise NativeServiceError('Published draft native roundtrip failed')
                shutil.copyfile(path, dest / (filename + '.bak'))
            shutil.copyfile(dest / 'draft_content.json', dest / 'template-2.tmp')
            session = service.request('player', 'create', {'kind': 'draft', 'serviceGeneration': 1})['result']['sessionId']
            try:
                service.request('player', 'load', {'draftPath': str(dest / 'draft_content.json'), 'workspacePath': str(draft_root)}, session)
                state = service.request('player', 'getState', {}, session)['result']
                canvas = snapshot.get('canvas_config', {})
                report['native_preview_dimensions'] = validate_player_state(state, snapshot.get('duration'), canvas)
                report['native_reload_state'] = state.get('status')
            finally:
                service.request('player', 'destroy', {}, session)
        report['service'] = service.report
        if any(file_digest(source_io / relative) != digest for relative, digest in source_hashes.items()):
            raise RuntimeError('Source draft changed; homepage index was not updated')
        if before_register is not None:
            report['pre_registration_validation'] = before_register(dest)
        entry = root_entry(meta, sum(p.stat().st_size for p in (dest / 'media').rglob('*') if p.is_file()) if (dest / 'media').is_dir() else 0)
        report['registration'] = append_index(index, entry, output, require_closed=not sandbox)
        report.update(status='native_draft_registered', source_files_preserved=True,
                      native_roundtrip_verified=True, gui_homepage_visibility='not_checked')
        new_json(output / 'registration-report.json', report)
        return report
    except Exception as exc:
        report.update(status='failed', error=str(exc), index_may_be_unregistered=True)
        new_json(output / 'registration-failure.json', report)
        raise


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', required=True)
    parser.add_argument('--draft', required=True)
    parser.add_argument('--draft-root', required=True)
    parser.add_argument('--user-data', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        result = publish(args.install_dir, args.draft, args.draft_root, args.user_data, args.name, args.out)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'error', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
