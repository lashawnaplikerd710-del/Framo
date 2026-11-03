"""TaskService's official-resource stage; legacy generated resources stay disabled."""
from __future__ import annotations

import json
from pathlib import Path
import uuid

from .local_executor import ToolFailed, ToolCancelled
from .native_packaging import native_modules, read_native, PackagingFailure, resource_identity
from .native_resource_planner import (collect_candidates, plan_native, validate_selection,
                                      missing_required, ResourcesPending)
from .resource_policy import ResourcePolicyBlocked
from .task_store import atomic_json, digest


def require_profile(service, job_id):
    profile = service.config.get('native_resources', {}) if service is not None else {}
    if profile.get('mode') != 'official_cli_v1' or profile.get('allow_isolated_host') is not True:
        raise ResourcePolicyBlocked('native_task_profile_not_configured')
    root = service.store.job_dir(job_id)/'n001'
    if not str(root).isascii() or len(str(root)) > 105:
        raise PackagingFailure('native_job_path_too_long: use --state-dir D:/JianyingAgentWork/tasks')


def catalog_checkpoint(service, job_id):
    rows = [a for a in service.store.list_artifacts(job_id) if a['kind'] == 'official_catalog']
    return rows[-1] if rows else None


def preflight(service, job_id, task, *, refresh_pending=False):
    require_profile(service, job_id)
    folder = service.store.job_dir(job_id)
    use = task.get('resource_use','commercial' if task['workflow'].startswith('product') else 'local_review')
    if use not in ('commercial','local_review'): raise PackagingFailure('invalid_task_resource_use')
    purpose = 'commercial' if use == 'commercial' else 'local_test'
    checkpoint = catalog_checkpoint(service, job_id)
    previous = json.loads(Path(checkpoint['path']).read_text(encoding='utf-8-sig')) if checkpoint else None
    if previous and previous.get('purpose') != purpose:
        raise PackagingFailure('catalog_resource_use_changed')
    missing = missing_required(previous, task) if previous else []
    if previous and not (refresh_pending and missing):
        result = previous
    else:
        # Catalog revisions are immutable artifacts. A retry searches only the
        # missing categories and carries verified candidates/history forward.
        revision = 'resources_' + uuid.uuid4().hex[:12] if previous else 'revision_001'
        target = folder/'resource-catalogs'/revision if previous else folder
        result = service._json_stage(job_id, 'official_catalog', target/'official-catalog.json',
        lambda: {'schema_version':'1.0', **collect_candidates(service.config['install_dir'],
            target/'catalog', allow_isolated_host=True, user_data=service.config.get('user_data_dir'),
            purpose=purpose, previous=previous,
            retry_kinds=[r['kind'] for r in missing] if previous else None,
            cancelled=lambda: service.get_job(job_id)['cancel_requested'])}, revision=revision, verified=True)
    if result.get('purpose') != purpose: raise PackagingFailure('catalog_resource_use_changed')
    result = {**result, 'pending_required_resources': missing_required(result, task)}
    return result


def failed_downloads(work, plan):
    """Only an actual failed download before any committed edit permits replanning."""
    path=work/'packaging-report.json'
    if not path.is_file():return []
    report=json.loads(path.read_text(encoding='utf-8'))
    if (report.get('error_type')!='ResourceFailure' or report.get('committed_elements')!=0 or
            report.get('reason')!='native_resource_command_failed:-100202'):
        return []
    selected={resource_identity(r) for r in plan['elements'] if resource_identity(r)}
    rejected=[]
    for receipt in (work/'resources').glob('*/provenance.json'):
        proof=json.loads(receipt.read_text(encoding='utf-8'))
        identity=(proof.get('resource_type'),proof.get('resource_id'))
        if (identity in selected and proof.get('status')=='failed' and
                proof.get('reason')=='native_resource_command_failed:-100202'):
            rejected.append({'kind':identity[0],'resource_id':identity[1],
                'reason':'official_download_failed','receipt':str(receipt),'sha256':digest(receipt)})
    return rejected


def earlier_failed_downloads(service, job_id, revision):
    """Carry verified unavailability into later revisions of this same job."""
    folder=service.store.job_dir(job_id).resolve()
    current=int(revision[9:]); excluded={}
    artifacts=sorted(service.store.list_artifacts(job_id),key=lambda a:(a['revision_id'],a['kind']))
    for artifact in artifacts:
        prior=artifact['revision_id']
        if (not artifact['kind'].startswith('native_resource_recovery') or
                not prior.startswith('revision_') or not prior[9:].isdigit() or
                int(prior[9:])>=current):continue
        report=Path(artifact['path']).resolve()
        if not report.is_relative_to(folder) or digest(report)!=artifact['sha256']:
            raise PackagingFailure('recovery_evidence_changed')
        for row in json.loads(report.read_text(encoding='utf-8'))['excluded']:
            receipt=Path(row['receipt']).resolve()
            if (row.get('reason')!='official_download_failed' or not receipt.is_relative_to(folder) or
                    digest(receipt)!=row['sha256']):raise PackagingFailure('recovery_evidence_changed')
            excluded.setdefault((row['kind'],row['resource_id']),row)
    return list(excluded.values())


def package_draft(service, job_id, task, provider, adapter, draft, out, timeline, evidence, *, previous=None):
    require_profile(service, job_id)
    catalog = preflight(service, job_id, task)
    pending_required = list(catalog.get('pending_required_resources', []))
    folder = service.store.job_dir(job_id); out = Path(out); draft = Path(draft)
    revision = timeline.get('revision_id','revision_001')
    if not revision.startswith('revision_') or not revision[9:].isdigit():
        raise PackagingFailure('invalid_native_revision')
    Service, _, _ = native_modules()
    with Service(service.config['install_dir'],out/('packaging-read-'+uuid.uuid4().hex[:8])) as svc:
        snapshot = read_native(svc,draft)
    positions = sorted({s['target_timerange'].get('start',0)+s['target_timerange']['duration']//2
        for t in snapshot['tracks'] if t['type']=='video' for s in t['segments']})
    if len(positions)>8: positions=[positions[i*(len(positions)-1)//7] for i in range(8)]
    preview = adapter.preview(draft,out/'packaging-input-preview',positions)
    service.store.put_artifact(job_id,'packaging_input_preview',revision,
        out/'packaging-input-preview/preview-report.json',verified=True)
    source_sha = digest(draft/'draft_content.json')
    checkpoint = catalog_checkpoint(service, job_id)
    binding = {'source_sha256':source_sha,'catalog_sha256':checkpoint['sha256'],
               'frames':[{k:r[k] for k in ('requested_us','sha256')} for r in preview['frames']]}
    excluded=earlier_failed_downloads(service,job_id,revision); feedback=previous
    if excluded:
        feedback={'previous_editorial_review':previous,'unavailable_resources':[
            {k:r[k] for k in ('kind','resource_id','reason')} for r in excluded],
            'instruction':'本任务较早版本已证实这些官方下载不可用；从其余候选选择，不要重复请求。'}
    for attempt in range(1,4):
        if service.get_job(job_id)['cancel_requested']:raise ToolCancelled('Native packaging cancelled')
        suffix='' if attempt==1 else f'_{attempt:02d}'
        file_suffix=suffix.replace('_','-')
        active_catalog={**catalog,'candidates':[c for c in catalog['candidates']
            if (c['kind'],c['resource_id']) not in {(r['kind'],r['resource_id']) for r in excluded}]}
        current_binding={**binding,**({'excluded_ids':[[r['kind'],r['resource_id']] for r in excluded]} if excluded else {})}
        service.store.set_state(job_id,'PLANNING',message='DeepSeek 根据剪映实际画面选择已核实的内部资源')
        proposal = service._json_stage(job_id,'native_packaging_plan'+suffix,out/f'native-packaging-plan{file_suffix}.json',
            lambda: {'schema_version':'1.0','binding':current_binding,**plan_native(provider,task,active_catalog,snapshot,
                service.config['install_dir'],evidence,preview['frames'],previous=feedback,
                attempt_dir=out/('packaging-proposals'+file_suffix))},revision=revision)
        if proposal['binding'] != current_binding: raise PackagingFailure('cached_native_plan_evidence_changed')
        selected = {'plan':proposal['plan'],'decisions':proposal['decisions']}
        validate_selection(selected,active_catalog,snapshot,service.config['install_dir'],
            required=task.get('required_native_categories',()),require_online_font=task.get('require_online_font',False),
            minimum_counts=task.get('minimum_native_counts'),minimum_online_fonts=task.get('minimum_online_fonts',0))
        pending = {r['kind']: dict(r) for r in pending_required}
        for kind, decision in selected['decisions'].items():
            if decision.get('status') == 'blocked' and kind in pending:
                pending[kind]['decision_reason'] = decision['reason']
        request = out/f'native-packaging-request{file_suffix}.json'
        if request.is_file():
            if json.loads(request.read_text(encoding='utf-8')) != proposal['plan']:
                raise PackagingFailure('cached_native_request_changed')
        else: atomic_json(request,proposal['plan'])
        service.store.put_artifact(job_id,'native_packaging_request'+suffix,revision,request,verified=True)
        work = folder/('n'+revision[9:]+suffix)
        # Persist evidence of a rejected resource. Later resumes skip this known
        # failed choice, instead of charging again or creating another host for it.
        recovery=service.store.artifact(job_id,'native_resource_recovery'+suffix,revision)
        if recovery:
            failed=json.loads(Path(recovery['path']).read_text(encoding='utf-8'))['excluded']
            for row in failed:
                if digest(Path(row['receipt']))!=row['sha256']:raise PackagingFailure('recovery_evidence_changed')
        else:
            failed=failed_downloads(work,proposal['plan'])
            if not failed:
                service.store.set_state(job_id,'BUILDING_DRAFT',message='下载官方免费元素，逐层写入并核对可编辑原生对象')
                try:
                    native = adapter.package_internal(draft,request,work,allow_isolated_host=True)
                    break
                except ToolFailed:
                    failed=failed_downloads(work,proposal['plan'])
                    if not failed:raise
            atomic_json(out/f'native-resource-recovery{file_suffix}.json',{'schema_version':'1.0','excluded':failed})
            service.store.put_artifact(job_id,'native_resource_recovery'+suffix,revision,
                out/f'native-resource-recovery{file_suffix}.json',verified=True)
        if attempt==3:
            raise ResourcesPending([dict(r, required=True) for r in failed],
                                   reason='native_resource_replanning_budget_exhausted')
        excluded.extend(failed)
        feedback={'previous_editorial_review':previous,'unavailable_resources':[
            {k:r[k] for k in ('kind','resource_id','reason')} for r in excluded],
            'instruction':'实际官方下载失败；从其余已核实候选重新选择，保留原剪辑目标。'}
        service.store.event(job_id,'PLANNING','progress',{'message':'官方下载失败，保留证据并让 DeepSeek 选择其他免费候选',
            'attempt':attempt,'excluded_resources':feedback['unavailable_resources']})
    final = Path(native['draft'])
    final_snapshot = json.loads((final.parent/'snapshot.json').read_text(encoding='utf-8'))
    records = json.loads((work/'journal.json').read_text(encoding='utf-8'))['steps']
    segments = {s['id']:s for t in final_snapshot['tracks'] for s in t['segments']}
    roles = {'music':'music','sound':'sfx','sticker':'stickers','transition':'transitions',
             'effect':'effects','text':'headings','filter':'filters'}
    items = [{'role':roles[row['kind']],'kind':row['kind'],'segment_id':record['segment_id'],
              'expected':segments[record['segment_id']], 'request':row}
             for row,record in zip(native.get('applied_elements',proposal['plan']['elements']),records)]
    report = {'schema_version':'1.0','schema':'jianying-native-task-packaging-v1',**native,
              'editable_layers_verified':True,'original_tracks':snapshot['tracks'],
              'items':items,'tracks':[{'id':t['id'],'type':t['type'],'segment_count':len(t['segments'])}
                                    for t in final_snapshot['tracks']],
              'decisions':proposal['decisions'],'source_audio_mode':native.get('source_audio_mode','preserved_embedded'),
              'native_report':str(work/'packaging-report.json'), 'resource_replanning_count':attempt-1,
              'unavailable_resources':[{k:r[k] for k in ('kind','resource_id','reason')} for r in excluded],
              'pending_resources':list(pending.values()),
              'quality_downgraded':False,
              'delivery_eligible':not bool(pending)}
    layers=out/'layers';layers.mkdir(exist_ok=True,parents=True)
    atomic_json(layers/'draft-snapshot.json',final_snapshot)
    atomic_json(layers/'packaging-report.json',report)
    service.store.put_artifact(job_id,'packaging_snapshot',revision,layers/'draft-snapshot.json',verified=True)
    service.store.put_artifact(job_id,'packaging_report',revision,layers/'packaging-report.json',verified=True)
    service.store.put_artifact(job_id,'packaged_draft',revision,final/'draft_content.json',verified=True)
    return final,report
