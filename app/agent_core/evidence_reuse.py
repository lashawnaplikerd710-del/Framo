"""Rerun after a runtime upgrade, preserving the old task and verified source evidence."""
import copy
import json
from pathlib import Path
import shutil
from .task_store import atomic_json, digest


def rerun_with_evidence(service, old_id):
    old=service.get_job(old_id)
    if old['state'] not in ('FAILED','CANCELLED','READY_FOR_REVIEW'):
        raise ValueError('Stop the old job before rerunning under an updated runtime')
    spec=copy.deepcopy(old['task_spec'])
    if spec['workflow'] not in ('product_ad','product_montage','crossborder_montage'):
        raise ValueError('Source evidence reuse currently supports multi-source workflows')
    prior=spec.pop('runtime_binding');current=service._runtime_binding()
    for key in ('model','install_dir','asr_model_sha256'):
        if prior.get(key)!=current.get(key): raise ValueError('Evidence configuration changed: '+key)
    for name in ('local_asr.py','autocut_task.py','native_preview.py'):
        if prior['tool_sha256'].get(name)!=current['tool_sha256'].get(name):
            raise ValueError('Evidence tool changed: '+name)
    if prior['core_sha256']['deepseek.py']!=current['core_sha256']['deepseek.py']:
        raise ValueError('Analysis provider changed; start a fresh task')
    old_dir=service.store.job_dir(old_id).resolve()
    artifacts=[a for a in service.list_artifacts(old_id) if a['kind'].startswith(
        ('transcript_asset_','frame_evidence_asset_','analysis_asset_'))]
    # Check pixels and source identity before copying anything.
    for a in artifacts:
        data=json.loads(Path(a['path']).read_text(encoding='utf-8'))
        if a['kind'].startswith('frame_evidence_'):
            for f in data['frames']:
                p=Path(f['path']).resolve(strict=True)
                if not p.is_relative_to(old_dir/'sources') or digest(p)!=f['sha256']:
                    raise ValueError('Native frame evidence changed')
        if a['kind'].startswith('transcript_'):
            asset=service._check_asset(service.store.get_asset(data['asset_id']))
            if data.get('source_sha256')!=asset['sha256']:raise ValueError('Source transcript identity changed')
    new_id=service.create_job(spec);new_dir=service.store.job_dir(new_id).resolve()
    def rebase(value):
        if isinstance(value,dict):return {k:rebase(v) for k,v in value.items()}
        if isinstance(value,list):return [rebase(v) for v in value]
        if isinstance(value,str):return value.replace(str(old_dir),str(new_dir)).replace(old_dir.as_posix(),new_dir.as_posix())
        return value
    files=[]
    for p in (old_dir/'sources').rglob('*'):
        if p.is_symlink() or not p.resolve().is_relative_to(old_dir/'sources'):
            raise ValueError('Source evidence contains an external link')
        if not p.is_file():continue
        target=new_dir/p.relative_to(old_dir);target.parent.mkdir(parents=True,exist_ok=True)
        before=digest(p)
        if p.suffix.lower()=='.json':
            try:value=rebase(json.loads(p.read_text(encoding='utf-8-sig')))
            except (ValueError,UnicodeError):shutil.copyfile(p,target)
            else:
                # This is a new unpublished task: exclusive create avoids MAX_PATH
                # overflow from adding a UUID suffix to deeply nested native evidence.
                with target.open('x',encoding='utf-8') as stream:
                    json.dump(value,stream,ensure_ascii=False,indent=2,allow_nan=False)
        else:shutil.copyfile(p,target)
        if digest(p)!=before:raise ValueError('Source evidence changed during copy')
        files.append({'path':str(p.relative_to(old_dir)),'old_sha256':before,'new_sha256':digest(target)})
    for a in artifacts:
        service.store.put_artifact(new_id,a['kind'],a['revision_id'],
            new_dir/Path(a['path']).relative_to(old_dir),verified=a['verified'])
    # The official resource catalog is immutable, verified evidence too.  A
    # runtime retry should reuse it instead of launching another host scan;
    # the catalog is still checked through TaskStore's path and SHA-256 guard.
    catalog_artifact = next((a for a in service.list_artifacts(old_id)
                             if a['kind'] == 'official_catalog' and a['verified']), None)
    if catalog_artifact:
        catalog_target = new_dir / 'official-catalog.json'
        before = digest(Path(catalog_artifact['path']))
        shutil.copyfile(catalog_artifact['path'], catalog_target)
        if digest(catalog_target) != before:
            raise ValueError('Official catalog changed during evidence copy')
        service.store.put_artifact(new_id, 'official_catalog', catalog_artifact['revision_id'],
                                   catalog_target, verified=True)
    # Reuse a plan that already passed compile_ad in the source task.  This is
    # an editorial checkpoint, not a bypass: the new run still recompiles it
    # against the copied source evidence and runs every native/readback check.
    prior_plan = next((a for a in service.list_artifacts(old_id)
                       if a['kind'] == 'ad_plan' and a['revision_id'] == 'revision_001'), None)
    if prior_plan:
        plan_target = new_dir / 'revisions' / '001' / 'ad-plan.json'
        plan_target.parent.mkdir(parents=True, exist_ok=True)
        before = digest(Path(prior_plan['path']))
        shutil.copyfile(prior_plan['path'], plan_target)
        if digest(plan_target) != before:
            raise ValueError('Editorial plan changed during evidence copy')
        service.store.put_artifact(new_id, 'ad_plan', prior_plan['revision_id'],
                                   plan_target, verified=prior_plan['verified'])
    atomic_json(new_dir/'evidence-reuse.json',{'schema_version':'1.0','source_job':old_id,
        'new_job':new_id,'old_runtime':prior,'new_runtime':current,'files':files,
        'edit_decisions_reused':False,'old_task_modified':False})
    service.store.event(new_id,'CREATED','progress',{'message':'复用指纹验证的素材观察，使用更新后的执行程序重新规划和生成新草稿'})
    return new_id
