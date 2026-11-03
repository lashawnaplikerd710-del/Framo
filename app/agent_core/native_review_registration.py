"""Self-contained, evidence-checked native draft registration for local review.

This publishes only to the local Jianying draft box, never a social platform.
It does not turn pending creative/audio review into a finished MP4 delivery.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
from contextlib import contextmanager

from .internal_fonts import FONTS, resolve_font
from .native_packaging import validate_completed, verify_files, native_modules, read_native, write_native, process_lock
from .task_store import atomic_json, digest
from .file_io import io_path, logical_path


class RegistrationFailure(ValueError):
    pass


@contextmanager
def host_exclusion():
    """Share the isolated host's process-wide Win32 mutex; never create a window."""
    import ctypes as c
    from ctypes import wintypes as w
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[c.c_void_p,w.BOOL,w.LPCWSTR];kernel.CreateMutexW.restype=w.HANDLE
    kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];kernel.WaitForSingleObject.restype=w.DWORD
    kernel.ReleaseMutex.argtypes=[w.HANDLE];kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.CreateMutexW(None,False,'Local\\JianyingAgentIsolatedHost')
    if not handle:raise RegistrationFailure('registration_host_lock_unavailable')
    acquired=False
    try:
        acquired=kernel.WaitForSingleObject(handle,0) in (0,0x80)
        if not acquired:raise RegistrationFailure('managed_host_or_registration_active')
        yield
    finally:
        if acquired:kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def rewrite_paths(value, mappings):
    if isinstance(value,dict):return {k:rewrite_paths(v,mappings) for k,v in value.items()}
    if isinstance(value,list):return [rewrite_paths(v,mappings) for v in value]
    if isinstance(value,str):
        path=value.replace('\\','/')
        for old,new in sorted(mappings,key=lambda pair:len(pair[0]),reverse=True):
            old=old.replace('\\','/').rstrip('/')
            if path.casefold()==old.casefold():return new
            if path.casefold().startswith(old.casefold()+'/'):return new+path[len(old):]
        # Native rich text stores font paths inside a JSON string.
        if value.startswith(('{','[')):
            try: nested=json.loads(value)
            except ValueError:pass
            else:
                if isinstance(nested,(dict,list)):
                    changed=rewrite_paths(nested,mappings)
                    if changed!=nested:return json.dumps(changed,ensure_ascii=False)
    return value


def absolute_paths(value):
    result=[]
    if isinstance(value,dict):
        for v in value.values():result.extend(absolute_paths(v))
    elif isinstance(value,list):
        for v in value:result.extend(absolute_paths(v))
    elif isinstance(value,str):
        if re.match(r'^[A-Za-z]:[/\\]',value) or value.startswith(('\\\\','//')):result.append(value)
        elif value.startswith(('{','[')):
            try:result.extend(absolute_paths(json.loads(value)))
            except ValueError:pass
    return result


def copy_checked(source,dest):
    source=io_path(source);dest=io_path(dest)
    paths=[source] if source.is_file() else [source,*source.rglob('*')]
    for path in paths:
        if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
            raise RegistrationFailure('linked_resource_copy_rejected')
    if source.is_file():
        before=digest(source);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,dest)
        if before!=digest(source) or before!=digest(dest):raise RegistrationFailure('copied_dependency_changed')
    else:
        dest.mkdir(parents=True,exist_ok=False)
        for path in paths[1:]:
            if path.is_dir():(dest/path.relative_to(source)).mkdir(parents=True,exist_ok=True)
            elif path.is_file():copy_checked(path,dest/path.relative_to(source))


def checkpoint_source_mappings(snapshot,packaging,source_targets):
    mappings=[]
    for material in snapshot.get('materials',{}).get('videos',[]):
        for key in ('path','media_path'):
            raw=material.get(key)
            if not raw:continue
            path=Path(raw).resolve(strict=True)
            if path.is_relative_to(packaging.resolve()) and path.is_file():
                sha=digest(path)
                if sha not in source_targets:
                    raise RegistrationFailure('checkpoint_source_hash_unknown')
                mappings.append((raw,source_targets[sha]))
    return mappings


def prepare_copy(install,packaging,output,*,allow_isolated_host=False,user_data=None):
    report=validate_completed(packaging)
    Service,check_references,_=native_modules()
    source=Path(report['draft']); draft=output/'portable';draft.mkdir()
    mappings=[]; provenance=[]; source_targets={}
    journal=read(packaging/'journal.json')
    for i,media in enumerate(journal['source_media']):
        path=Path(media['path']);target=draft/'media'/f'source-{i+1}{path.suffix.lower()}'
        if digest(path)!=media['sha256']:raise RegistrationFailure('source_media_changed')
        if media['sha256'] in source_targets:
            mappings.append((str(path),source_targets[media['sha256']]))
            continue
        copy_checked(path,target);mappings.append((str(path),target.as_posix()))
        source_targets[media['sha256']]=target.as_posix()
        provenance.append({'kind':'user_source','sha256':media['sha256'],'relative':target.relative_to(draft).as_posix()})
    for i,media in enumerate(report.get('narration_files',[])):
        path=Path(media['path']).resolve(strict=True)
        if not path.is_relative_to(packaging.resolve()) or digest(path)!=media['sha256']:
            raise RegistrationFailure('native_narration_dependency_changed')
        target=draft/'media'/f'narration-{i+1}{path.suffix.lower()}'
        copy_checked(path,target);mappings.append((str(path),target.as_posix()))
        provenance.append({'kind':'jianying_native_extracted_source_audio','sha256':media['sha256'],
            'relative':target.relative_to(draft).as_posix()})
    receipts=read(packaging/'resources-verified.json')
    if any(p.get('file_inventory')!='extended_windows_paths_v1' for p in receipts.values()):
        if not allow_isolated_host:raise RegistrationFailure('legacy_incomplete_inventory_needs_official_redownload')
        from .native_host import IsolatedNativeHost
        from .native_cli import NativeCLI
        from .native_resource_catalog import acquire
        with IsolatedNativeHost(install,output/'resource-host',allow_isolated_host=True,user_data=user_data,lifetime_timeout=600) as host:
            with NativeCLI(host) as client:
                refreshed={}
                for i,(identity,proof) in enumerate(receipts.items()):
                    target=output/'official'/str(i)
                    new=acquire(client,proof['resource_type'],proof['resource_id'],target)
                    refreshed[identity]={**new,'_directory':str(target),'original_path':proof['path']}
        receipts=refreshed
        atomic_json(output/'refreshed-resources.json',receipts)
    for i,(identity,proof) in enumerate(receipts.items()):
        if proof.get('file_inventory')!='extended_windows_paths_v1':raise RegistrationFailure('complete_resource_inventory_required')
        for item in proof['files']:
            if digest(Path(proof['_directory'])/item['path'])!=item['sha256']:
                raise RegistrationFailure('official_resource_payload_changed')
        if proof['entitlement'].get('nonmember_metadata_verified') is not True:
            raise RegistrationFailure('resource_free_receipt_missing')
        path=Path(proof['path'])
        # The resource ID/hash hierarchy is part of the native package layout.
        # Flattening it silently disables some effects even when bytes match.
        payload=Path(proof['_directory'])/'payload'
        target=draft/'resources'/identity/'payload'/path.relative_to(payload)
        copy_checked(path,target);mappings.append((proof.get('original_path',str(path)),target.as_posix()))
        provenance.append({'kind':'official_resource','identity':identity,'entitlement':proof['entitlement'],
                           'relative':target.relative_to(draft).as_posix()})
    with Service(install,output/'prepare-service') as svc:
        snapshot=read_native(svc,source)
        # CLI checkpoint copies can relocate original videos below packaging.
        # Resolve those aliases by verified bytes, never by filename alone.
        mappings.extend(checkpoint_source_mappings(snapshot,packaging,source_targets))
        paths=absolute_paths(snapshot)
        for font_id in FONTS:
            font=resolve_font(install,font_id)
            if any(p.replace('\\','/').casefold()==font['path'].casefold() for p in paths):
                target=draft/'fonts'/(font_id+'.ttf');copy_checked(Path(font['path']),target)
                mappings.append((font['path'],target.as_posix()))
                atomic_json(draft/'fonts'/(font_id+'-license.json'),font)
        snapshot=rewrite_paths(snapshot,mappings)
        snapshot=rewrite_paths(snapshot,[(source.as_posix(),draft.as_posix())])
        for raw in absolute_paths(snapshot):
            p=Path(raw).resolve(strict=True)
            if not p.is_relative_to(draft):raise RegistrationFailure('unverified_external_draft_dependency')
        check_references(snapshot);write_native(svc,draft/'draft_content.json',snapshot)
    atomic_json(draft/'internal-resource-provenance.json',{'schema':'jianying-local-review-copy-v1',
        'source_packaging_report_sha256':digest(packaging/'packaging-report.json'),
        'items':provenance,'publishing_scope_verified':False,'delivery_accepted':False})
    validate_completed(packaging)
    return draft,report


def file_manifest(dest):
    physical=io_path(dest)
    return [{'path':p.relative_to(physical).as_posix(),'sha256':digest(p)} for p in sorted(physical.rglob('*')) if p.is_file()]


def verify_dependency_copy(source,dest):
    source=io_path(source);dest=io_path(dest)
    checked=0
    for group in ('media','resources','fonts','textReading'):
        for path in (source/group).rglob('*'):
            if path.is_file():
                target=dest/path.relative_to(source)
                if not target.is_file() or digest(path)!=digest(target):
                    raise RegistrationFailure('registered_resource_payload_changed')
                checked+=1
    if checked==0:raise RegistrationFailure('registered_dependencies_empty')
    return checked


def verify_manifest(dest,files):
    dest=io_path(dest).resolve(strict=True)
    for item in files:
        path=(dest/item['path']).resolve(strict=True)
        if not path.is_relative_to(dest) or digest(path)!=item['sha256']:
            raise RegistrationFailure('registered_draft_or_dependency_changed')


def validate_registered(output):
    output=Path(output);report=read(output/'registration-report.json')
    if report.get('status')!='native_draft_registered':raise RegistrationFailure('registration_not_complete')
    dest=Path(report['destination']).resolve(strict=True)
    verify_manifest(dest,report['destination_files'])
    rows=read(report['registration']['index']).get('all_draft_store',[])
    matches=[r for r in rows if r.get('draft_id')==report['registration']['new_entry_id']]
    if len(matches)!=1 or Path(matches[0]['draft_fold_path']).resolve()!=dest:
        raise RegistrationFailure('registered_homepage_entry_missing_or_changed')
    return report


def commit_review_intent(output, journal):
    """Recover both sides of the atomic index write without a second entry."""
    intent_path=output/'registration-intent.json'
    if digest(intent_path)!=journal.get('intent_sha256'):raise RegistrationFailure('registration_intent_changed')
    intent=read(intent_path);dest=Path(intent['destination']).resolve(strict=True)
    verify_manifest(dest,intent['destination_files'])
    preview=Path(intent['preview_report'])
    if digest(preview)!=intent['preview_sha256']:raise RegistrationFailure('registration_preview_changed')
    for frame in read(preview)['frames']:
        if digest(Path(frame['path']))!=frame['sha256']:raise RegistrationFailure('registration_pixels_changed')
    native_modules()
    from native_register import append_index
    index=Path(intent['index']);entry=intent['entry'];current=read(index) if index.exists() else {'all_draft_store':[]}
    rows=current['all_draft_store']
    by_id=[r for r in rows if r.get('draft_id')==entry['draft_id']]
    by_path=[r for r in rows if Path(r.get('draft_fold_path','')).resolve()==dest]
    if by_id or by_path:
        if by_id!=[entry] or by_path!=[entry]:raise RegistrationFailure('registration_entry_conflict')
        if not all(r in rows for r in intent['index_before'].get('all_draft_store',[])):
            raise RegistrationFailure('old_homepage_entries_changed')
        registration={'index':str(index),'new_entry_id':entry['draft_id'],'index_readback_equal':True,
            'existing_entries_preserved':True,'old_entry_count':len(intent['index_before'].get('all_draft_store',[])),
            'new_entry_count':len(rows),'recovered_existing_entry':True}
    else:
        registration=append_index(index,entry,output)
    result={'schema':'jianying-native-local-review-registration-v1','status':'native_draft_registered',
        'destination':str(dest),'registration':registration,'destination_files':intent['destination_files'],
        'pre_registration_validation':intent['validation'],'self_contained_dependencies':True,
        'source_packaging_report':intent['source_packaging_report'],'local_review_only':True,
        'publishing_scope_verified':False,'delivery_accepted':False,'continuous_av_review_passed':False,
        'final_export_created':False,'gui_opened':False,'recovered_from_commit_intent':True}
    atomic_json(output/'registration-report.json',result)
    return validate_registered(output)


def quarantine_unregistered(output,draft_root,user_data,name):
    """A failed candidate must not be rediscovered by the next official host."""
    failure=output/'native-register/registration-failure.json'
    if not failure.is_file():return None
    info=read(failure)
    if info.get('destination_created') is not True:return None
    source=(draft_root/name).resolve()
    target=(output/'quarantined-draft').resolve()
    if (not source.is_dir() or source.parent!=draft_root or Path(info['destination']).resolve()!=source or
            target.exists() or not target.is_relative_to(output) or source.drive!=target.drive):
        return {'status':'manual_evidence_review_required'}
    from .native_host import editor_pids
    if editor_pids():return {'status':'editor_running_preserved_candidate'}
    index=user_data/'Projects/com.lveditor.draft/root_meta_info.json'
    rows=read(index).get('all_draft_store',[]) if index.exists() else []
    if any(r.get('draft_id')==info.get('draft_id') or Path(r.get('draft_fold_path','')).resolve()==source for r in rows):
        return {'status':'indexed_candidate_preserved_for_commit_recovery'}
    source.rename(target)  # Same-volume atomic move of this invocation's new directory.
    return {'status':'unregistered_candidate_quarantined','source':str(source),'destination':str(target),'files_deleted':False}


def register(install,packaging,draft_root,user_data,name,output,*,allow_isolated_host=False):
    install=Path(install).resolve(strict=True);packaging=Path(packaging).resolve(strict=True)
    output=Path(output).resolve();draft_root=Path(draft_root).resolve(strict=True)
    user_data=Path(user_data).resolve(strict=True)
    if (output.is_relative_to(packaging) or packaging.is_relative_to(output) or output.is_relative_to(install)
            or output.drive.casefold()!=draft_root.drive.casefold()):
        raise RegistrationFailure('registration_output_must_be_separate')
    binding=hashlib.sha256(json.dumps([str(install),str(packaging),digest(packaging/'packaging-report.json'),
        str(draft_root),str(user_data),name],ensure_ascii=False).encode()).hexdigest()
    if output.exists() and not (output/'registration-journal.json').is_file():
        raise RegistrationFailure('registration_output_already_exists_without_journal')
    output.mkdir(parents=True,exist_ok=True)
    with process_lock(output/'registration.lock'),host_exclusion():
        journal_path=output/'registration-journal.json'
        if journal_path.exists():
            journal=read(journal_path)
            if journal['binding']!=binding:raise RegistrationFailure('registration_inputs_changed')
            if (output/'registration-report.json').is_file():return validate_registered(output)
            if journal.get('state')=='ready_to_index':return commit_review_intent(output,journal)
            raise RegistrationFailure('interrupted_registration_needs_evidence_recovery')
        atomic_json(journal_path,{'binding':binding,'state':'preparing'})
        try:
            draft,original=prepare_copy(install,packaging,output,allow_isolated_host=allow_isolated_host,user_data=user_data)
            Service,_,_=native_modules()
            from native_register import publish
            from native_preview import preview_batches
            # Verify relocation before the homepage is changed. The stored
            # original frames were already checked by validate_completed().
            selected=original['frames']
            positions=[f['requested_us'] for f in selected]
            width=selected[0]['width']
            def before_register(dest):
                dependency_count=verify_dependency_copy(draft,dest)
                with Service(install,output/'check-destination-service') as svc:
                    snapshot=read_native(svc,dest)
                    meta=json.loads(svc.request('draft','readFile',{'path':str(dest/'draft_meta_info.json'),
                        'rootPath':str(dest)})['result']['content'])
                for raw in absolute_paths(snapshot):
                    if not Path(raw).resolve(strict=True).is_relative_to(dest):
                        raise RegistrationFailure('registered_dependency_outside_project')
                rendered=preview_batches(install,dest,output/'registered-preview',positions,width)['frames']
                if len(rendered)!=len(selected) or any(a['sha256']!=b['sha256'] for a,b in zip(rendered,selected)):
                    raise RegistrationFailure('registered_pixels_differ_before_index_update')
                validation={'exact_native_frame_comparison':True,'frame_count':len(rendered),
                        'byte_identical_dependency_files':dependency_count,
                        'continuous_av_review_passed':False,'frames':rendered}
                from native_register import root_entry
                index=user_data/'Projects/com.lveditor.draft/root_meta_info.json'
                intent={'destination':str(dest),'entry':root_entry(meta,sum(p.stat().st_size for p in (dest/'media').rglob('*') if p.is_file())),
                    'index':str(index),'index_before':read(index) if index.exists() else {'all_draft_store':[]},
                    'destination_files':file_manifest(dest),'validation':validation,
                    'source_packaging_report':str(packaging/'packaging-report.json'),
                    'preview_report':str(output/'registered-preview/preview-report.json'),
                    'preview_sha256':digest(output/'registered-preview/preview-report.json')}
                atomic_json(output/'registration-intent.json',intent)
                atomic_json(journal_path,{'binding':binding,'state':'ready_to_index',
                    'intent_sha256':digest(output/'registration-intent.json')})
                return validation
            result=publish(install,draft,draft_root,user_data,name,output/'native-register',before_register=before_register)
            dest=Path(result['destination'])
            result.update(schema='jianying-native-local-review-registration-v1',
                destination_files=file_manifest(dest),
                self_contained_dependencies=True,source_packaging_report=str(packaging/'packaging-report.json'),
                local_review_only=True,publishing_scope_verified=False,delivery_accepted=False,
                continuous_av_review_passed=False,final_export_created=False)
            atomic_json(output/'registration-report.json',result)
            return validate_registered(output)
        except Exception as exc:
            quarantined=quarantine_unregistered(output,draft_root,user_data,name)
            atomic_json(output/'failure.json',{'status':'failed','error_type':type(exc).__name__,
                'reason':str(exc) if isinstance(exc,RegistrationFailure) else 'native_registration_failed',
                'candidate_cleanup':quarantined,
                'delivery_accepted':False})
            raise
