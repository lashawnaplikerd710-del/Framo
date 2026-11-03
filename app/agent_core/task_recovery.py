"""Visible diagnostics and immutable, unapproved user script corrections."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import re
import uuid

from . import ad_narration, market_rules, narration_timing
from .local_executor import draft_lock
from .task_store import atomic_json

STOPPED = {'WAITING_FOR_INPUT', 'BLOCKED_RESOURCES', 'FAILED', 'CANCELLED'}
PLAN_KINDS = {'ad_plan', 'ad_script_pending', 'narration_measurement', 'ad_auto_pending',
              'ad_auto_proposal', 'ad_auto_script', 'user_script_repair'}
GENERATED_SCRIPT_KINDS = PLAN_KINDS - {'ad_plan'}


def compatible_task(spec, plan, *, checkpoint_kind=None):
    """Recover an evidenced legacy custom narration, never infer new speech.

    Older prompt-first jobs persisted the pre-resolution text-only defaults,
    while their hash-checked narration revisions contained generated speech.
    A direct request and such a revision are both required to migrate them.
    Artifact and runtime checks remain the responsibility of the caller.
    """
    if ad_narration.enabled(spec):
        return spec
    from . import caption_policy
    if (spec.get('render_backend') != 'direct_ffmpeg' or spec.get('workflow') != 'custom'
            or spec.get('editing_rules_version') != 'prompt_first_v1'
            or not caption_policy.highlights(spec) or checkpoint_kind not in GENERATED_SCRIPT_KINDS
            or not isinstance(plan, dict) or not isinstance(plan.get('clips'), list)
            or not plan['clips'] or not all(isinstance(clip, dict) for clip in plan['clips'])):
        return None
    instruction = spec.get('instruction', '')
    if not isinstance(instruction, str):
        return None
    if re.search(r'(?:不要|不用|无需|不加|不需要|禁止|不生成|不重新).{0,8}(?:口播|旁白|配音)'
                 r'|保留原(?:声|口播|配音)', instruction):
        return None
    requested = re.search(r'(?:生成|重新|新写|新增|添加|创作|制作|合成|更换).{0,12}(?:口播|旁白|配音)'
                          r'|(?:全新|新的|新)(?:中文|普通话|日语|英语)?(?:口播|旁白|配音)', instruction)
    texts = [plan.get('narration_script'), plan.get('voiceover'), plan.get('narrator_script'),
             *(clip.get('voiceover') for clip in plan['clips'] if isinstance(clip, dict))]
    if not requested or not any(isinstance(text, str) and text.strip() for text in texts):
        return None
    options = dict(spec.get('resolved_editing_options') or {})
    options['narration'] = 'new'
    return ad_narration.configure({**spec, 'narration_mode':'new_native', 'separate_narration':True,
                                   'resolved_editing_options':options})


def latest_plan(service, job_id, *, safe=False):
    if safe:
        artifacts, invalid = service.store.list_artifacts_safe(job_id)
    else:
        artifacts, invalid = service.store.list_artifacts(job_id), []
    latest = None
    for record in artifacts:
        if record['kind'] not in PLAN_KINDS:
            continue
        try:
            data = json.loads(Path(record['path']).read_text(encoding='utf-8'))
            if not isinstance(data, dict):
                raise ValueError('检查点格式无效')
        except (OSError, ValueError) as exc:
            if not safe:
                raise
            invalid.append({'kind':record['kind'], 'error':str(exc)})
            continue
        plan = data if record['kind'] == 'ad_plan' else data.get('plan')
        if isinstance(plan, dict) and isinstance(plan.get('clips'), list):
            latest = {'artifact': record, 'data': data, 'plan': plan}
    return latest, invalid


def binding_change(service, spec):
    old = spec.get('runtime_binding')
    if not old:
        return None, []
    new = service._runtime_binding()
    changed = {key for key in set(old)|set(new) if old.get(key) != new.get(key)}
    # A deliberate script correction may be recompiled/reviewed with current
    # core code; changed models, tools, libraries and profiles need a new job.
    if changed - {'core_sha256'}:
        raise ValueError('此任务的模型、工具或素材库版本已变化，请点击「按原要求新建」；旧记录会保留。')
    files = sorted(name for name in set(old.get('core_sha256', {}))|set(new.get('core_sha256', {}))
                   if old.get('core_sha256', {}).get(name) != new.get('core_sha256', {}).get(name))
    return new if changed else None, files


def wrong_headings(task, plan):
    if not market_rules.active(task):
        return []
    return [index for index, clip in enumerate(plan['clips'], 1)
            if clip.get('heading') and (not isinstance(clip['heading'], str)
                                       or not market_rules.validate_language(task, clip['heading']))]


def repair_guide(data, review=None):
    """Actionable presentation of actual checks; never invent replacement facts."""
    review = review if isinstance(review, dict) else {}
    reason = data.get('reason', '')
    wrong = [r['clip_index'] for r in data.get('headings', []) if r.get('problem')]
    if not data.get('editable'):
        title, focus = '当前不能直接修改文案', 'details'
        steps = [data.get('advice', ''), '可点击「带入原素材重新创建」，原要求和素材会自动填好。']
    elif wrong:
        title, focus = '第'+'、'.join(map(str, wrong))+'条字幕需要修改语言', 'captions'
        steps = ['选择标记的字幕，改成目标语言；也可以点击「自动翻译问题字幕」。',
                 '修改完成后，点击「保存并重新生成」。']
    elif not data.get('narration_script', '').strip():
        title, focus = '缺少完整口播文案', 'narration'
        steps = ['在下方输入完整口播，或者点击「AI 帮我改写」生成建议。',
                 '只写有画面依据的内容，保存后软件会重新审核、配音并生成成片。']
    elif any(word in reason for word in ('精简', '过长', '超出', '偏长')):
        title, focus = '口播需要精简或修改', 'narration'
        steps = ['删除重复内容和没有画面依据的说法，保留完整句子。',
                 '不会改可以点击「AI 帮我改写」，建议会填入下方；确认后保存并重新生成。']
    else:
        title, focus = '文案有几处需要调整', 'narration'
        steps = ['按下方具体意见修改；不要增加画面中看不到的材质、尺寸或功效。',
                 '不会改可以点击「AI 帮我改写」，再保存并重新生成。']
    problems = []
    for issue in review.get('issues', []):
        if not isinstance(issue, str):
            continue
        finding = next((f for f in review.get('findings', []) if isinstance(f, dict)
                        and f.get('problem') == issue), {})
        quote = finding.get('quote')
        problems.append({'problem':issue, 'quote':quote if isinstance(quote, str) else ''})
    return {'title':title, 'focus':focus, 'steps':steps, 'problems':problems}


def diagnose(service, job_id):
    job = service.get_job(job_id)
    spec = job['task_spec']
    checkpoint, invalid = latest_plan(service, job_id, safe=True)
    plan = checkpoint['plan'] if checkpoint else {}
    compatible = compatible_task(spec, plan, checkpoint_kind=checkpoint['artifact']['kind'] if checkpoint else None)
    editing_spec = compatible or spec
    plan = ad_narration.normalize_plan(editing_spec, plan)
    data = checkpoint['data'] if checkpoint else {}
    issues = (data.get('review') or {}).get('issues', [])
    reason = '\n'.join(dict.fromkeys(str(item) for item in [job.get('error'), data.get('reason'), *issues] if item))
    version_error = None
    try:
        _, core_changes = binding_change(service, spec)
    except (OSError, ValueError, KeyError) as exc:
        version_error, core_changes = str(exc), []
    editable = bool(checkpoint and not invalid and not version_error and job['state'] in STOPPED
                    and compatible)
    wrong = wrong_headings(editing_spec, plan) if checkpoint else []
    if version_error:
        advice = version_error
    elif invalid:
        advice = '部分检查点未通过文件校验，无法直接修改；请按原要求新建任务。'
    elif not editable:
        if job['state'] not in STOPPED:
            advice = '任务正在执行或已完成，当前只读；请关闭窗口并刷新任务状态。'
        elif not checkpoint:
            advice = '尚未保存可修订的旁白检查点。可复制排查信息，或按原要求新建任务。'
        else:
            advice = '此任务没有可安全修订的整段新旁白策略，当前只读；请按原要求新建任务。'
    elif wrong:
        advice = '标记的字幕未使用目标语言。可点击「修复字幕语言」生成建议，或直接修改，再保存并继续。'
    elif any(word in reason for word in ('精简', '过长', '超出', '偏长')):
        advice = '旁白未满足长度或审核要求。请根据下方具体意见精简完整旁白，保留有画面依据的表达，再保存并继续。'
    elif '资源' in reason or job['state'] == 'BLOCKED_RESOURCES':
        advice = '请先按错误补齐音色、音乐或工具资源；文案修订不能解决缺失资源。可复制排查信息。'
    elif editable:
        advice = '请按具体审核意见修改字幕或旁白。保存后仍会进行内容审核、配音时长检查和成片检查。'
    else:
        advice = '可复制排查信息定位问题，或按原要求新建任务。新建入口会带入原素材和要求，不会立即开始剪辑。'
    events = service.read_events(job_id)
    retries = sum(e.get('stage') == 'RECOVERY' for e in events)
    measurement = data.get('measurement') or {}
    result = {'job_id':job_id, 'state':job['state'], 'reason':reason or '暂无更详细的错误记录',
            'advice':advice, 'editable':editable, 'core_changes':core_changes,
            'legacy_narration_recovered':bool(compatible and compatible != spec),
            'checkpoint_id':checkpoint['artifact']['artifact_id'] if checkpoint else None,
            'locale':editing_spec.get('narration_locale') or editing_spec.get('target_locale') or '自动',
            'headings':[{'clip_index':i, 'heading':str(c.get('heading') or ''), 'problem':i in wrong}
                        for i,c in enumerate(plan.get('clips', []), 1)],
            'narration_script':str(plan.get('narration_script') or ''),
            'target_units':data.get('target_units'), 'speech_units':narration_timing.units(editing_spec, plan.get('narration_script', '')),
            'measurement':measurement, 'resume_count':retries,
            'video_status':'成品尚未生成；当前可检查的是已保存的字幕、旁白和错误记录。',
            'source_task':{'workflow':spec.get('workflow'), 'target_country':spec.get('target_country'),
                           'instruction':spec.get('instruction', ''), 'subtitle_font_id':spec.get('subtitle_font_id')},
            'assets':[{'asset_id':a['asset_id'], 'name':Path(a['path']).name,
                       'duration_us':a.get('video_duration_us'), 'width':a.get('width'), 'height':a.get('height')}
                      for a in (service.store.get_asset(aid) for aid in spec.get('asset_ids', []))]}
    result['guide'] = repair_guide(result, data.get('review'))
    result['speech_unit_label'] = '词' if editing_spec.get('narration_locale') == 'en-US' else '字'
    from .commerce import heading_limits
    for row in result['headings']:
        row['min_length'], row['max_length'] = heading_limits(editing_spec, row['heading'])
    return result


def checked_checkpoint(service, job_id, checkpoint_id):
    job = service.get_job(job_id)
    if job['state'] not in STOPPED:
        raise ValueError('任务正在执行或已完成，请刷新状态后再处理')
    current, _ = latest_plan(service, job_id)
    if not current or current['artifact']['artifact_id'] != checkpoint_id:
        raise ValueError('检查点已更新，请关闭弹窗后重新打开，避免覆盖新的修订')
    spec = compatible_task(job['task_spec'], current['plan'], checkpoint_kind=current['artifact']['kind'])
    if not spec:
        raise ValueError('此任务没有可直接修订的整段旁白，请按原要求新建')
    binding_change(service, job['task_spec'])
    return {**job, 'task_spec':spec, 'recovery_spec_changed':spec != job['task_spec']}, current


def save_correction(service, job_id, checkpoint_id, headings, narration):
    from .commerce import validate_ad_script
    with draft_lock(service.store.job_dir(job_id)/'.job-run.lock'):
        job, checkpoint = checked_checkpoint(service, job_id, checkpoint_id)
        spec = job['task_spec']
        plan = copy.deepcopy(ad_narration.normalize_plan(spec, checkpoint['plan']))
        if (not isinstance(headings, list) or len(headings) != len(plan['clips'])
                or not isinstance(narration, str) or not narration.strip()):
            raise ValueError('请提供完整的字幕列表和完整旁白')
        seen = set()
        for row in headings:
            index = row.get('clip_index') if isinstance(row, dict) else None
            text = row.get('heading') if isinstance(row, dict) else None
            if (type(index) is not int or not 1 <= index <= len(plan['clips']) or index in seen
                    or not isinstance(text, str) or len(text) > 1000):
                raise ValueError('字幕编号或文字无效，请刷新弹窗')
            seen.add(index)
            plan['clips'][index-1]['heading'] = text.strip()
        plan['narration_script'] = narration.strip()
        wrong = wrong_headings(spec, plan)
        if wrong:
            raise ValueError('第 '+ '、'.join(map(str, wrong))+' 处字幕仍未使用目标语言，请修改后保存')
        validate_ad_script(spec, plan)
        # These checks happen before any saved revision or runtime update.
        for asset_id in spec['asset_ids']:
            service._check_asset(service.store.get_asset(asset_id))
        new_binding, core_changes = binding_change(service, spec)
        if plan == checkpoint['plan'] and not job['recovery_spec_changed']:
            if checkpoint['artifact']['kind'] == 'user_script_repair' and not new_binding:
                return {'saved':True, 'checkpoint_id':checkpoint_id}
            raise ValueError('文案尚未修改；请先修订问题字幕或旁白')
        path = service.store.job_dir(job_id)/'user-script-repairs'/(uuid.uuid4().hex+'.json')
        atomic_json(path, {'schema_version':'1.0', 'plan':plan, 'approved':False, 'user_edited':True,
                          'parent_checkpoint_id':checkpoint_id, 'changed_core_files':core_changes,
                          'reason':'用户已修订，等待完整内容审核', 'quality_downgraded':False})
        artifact = service.store.put_artifact(job_id, 'user_script_repair', path.stem, path)
        if new_binding or job['recovery_spec_changed']:
            spec = {**spec, **({'runtime_binding':new_binding} if new_binding else {})}
            atomic_json(service.store.job_dir(job_id)/'task.json', spec)
            with service.store._connect() as db:
                db.execute('UPDATE jobs SET spec_json=? WHERE job_id=?', (json.dumps(spec, ensure_ascii=False), job_id))
        service.store.event(job_id, 'RECOVERY', 'progress',
                            {'message':'用户已修订字幕与旁白，镜头和旧检查点保留；尚未通过验收',
                             'artifact_id':artifact['artifact_id'], 'changed_core_files':core_changes})
        return {'saved':True, 'checkpoint_id':artifact['artifact_id']}


def suggest_language_fix(service, job_id, checkpoint_id):
    """Only translate the rejected headings. Suggestions require explicit save."""
    with draft_lock(service.store.job_dir(job_id)/'.job-run.lock'):
        job, checkpoint = checked_checkpoint(service, job_id, checkpoint_id)
        task, plan = job['task_spec'], checkpoint['plan']
        indices = wrong_headings(task, plan)
        if not indices:
            raise ValueError('当前没有字幕语言错误，请根据审核意见修改文字')
        payload = {'locale':task['narration_locale'], 'headings':[
            {'clip_index':i, 'heading':plan['clips'][i-1]['heading']} for i in indices]}
        prompt = ('只把headings中每一条文字翻译为locale指定语言，保留原意，不增加卖点、事实或承诺。'
                  '不要输出其它镜头，不修改旁白。返回JSON {"caption_fixes":[{"clip_index":1,"heading":"目标语言短文案"}]}。')
        provider = service.provider_factory()
        response = provider._json(prompt, payload)
        fixes = response.get('caption_fixes') if isinstance(response, dict) else None
        if not isinstance(fixes, list) or len(fixes) != len(indices):
            raise ValueError('语言修复建议格式无效，原文案未改动；可以手动修订或重新尝试')
        seen = set()
        for fix in fixes:
            index = fix.get('clip_index') if isinstance(fix, dict) else None
            text = fix.get('heading') if isinstance(fix, dict) else None
            if (type(index) is not int or index not in indices or index in seen or not isinstance(text, str)
                    or not text.strip() or len(text) > 1000 or not market_rules.validate_language(task, text)):
                raise ValueError('修复建议仍未满足目标语言，原文案未改动；可手动修订')
            seen.add(index)
        path = service.store.job_dir(job_id)/'user-script-repairs'/('suggestion-'+uuid.uuid4().hex+'.json')
        atomic_json(path, {'parent_checkpoint_id':checkpoint_id, 'response':response, 'approved':False})
        service.store.put_artifact(job_id, 'script_language_suggestion', path.stem, path)
        return {'caption_fixes':fixes}


def suggest_script_rewrite(service, job_id, checkpoint_id, draft=None):
    """Generate an editable proposal; never mark it approved or queue a render."""
    from .commerce import compile_ad, rewrite_direct_voiceover, NarrationPlanIssue
    from .gateway_credentials import credential_environment
    from .model_recovery import bind_recovery
    from contextlib import nullcontext
    with draft_lock(service.store.job_dir(job_id)/'.job-run.lock'):
        job, checkpoint = checked_checkpoint(service, job_id, checkpoint_id)
        task = dict(job['task_spec'])
        # This is an explicit user-requested suggestion, not another automatic
        # repair loop. Do not clear or overwrite the original automatic ledger.
        task.pop('_script_repair_ledger', None)
        if narration_timing.script_locked(task):
            raise ValueError('原剪辑要求指定口播原样保留，不能自动改写。请返回新建页调整要求。')
        assets = [service.store.get_asset(aid) for aid in task['asset_ids']]
        for asset in assets:
            service._check_asset(asset)
        folder = service.store.job_dir(job_id)/'ai'/uuid.uuid4().hex[:12]
        provider = bind_recovery(service.provider_factory(), folder/'m')
        config = service.config['provider']
        with credential_environment(config) if config.get('transport') == 'gateway' else nullcontext():
            sources = service._prepare_direct_ad_sources(job_id, task, assets,
                service.executor_factory(job_id), provider)
            plan = ad_narration.normalize_plan(task, copy.deepcopy(checkpoint['plan']))
            try:
                timeline, _, _ = compile_ad(task, assets, sources, plan)
            except NarrationPlanIssue as error:
                if not error.timeline:
                    raise
                timeline = error.timeline
            if draft is not None:
                rows = draft.get('headings') if isinstance(draft, dict) else None
                text = draft.get('narration_script') if isinstance(draft, dict) else None
                if (not isinstance(rows, list) or len(rows) != len(plan['clips'])
                        or not isinstance(text, str) or len(text) > 20000):
                    raise ValueError('当前编辑内容无效，请重新打开修复窗口')
                seen = set()
                for row in rows:
                    index = row.get('clip_index') if isinstance(row, dict) else None
                    heading = row.get('heading') if isinstance(row, dict) else None
                    if (type(index) is not int or not 1 <= index <= len(plan['clips']) or index in seen
                            or not isinstance(heading, str) or len(heading) > 1000):
                        raise ValueError('当前字幕编号或内容无效，请重新打开修复窗口')
                    plan['clips'][index-1]['heading'] = heading
                    seen.add(index)
                plan['narration_script'] = text
            data = checkpoint['data']
            issue = json.dumps({'issues':(data.get('review') or {}).get('issues', []),
                'reason':job.get('error'), 'instruction':'修复屏幕文案和整段旁白，保留镜头。'}, ensure_ascii=False)
            task = {**task, **({'_narration_repair_max_units':data['target_units']}
                              if type(data.get('target_units')) is int and data['target_units'] > 0 else {})}
            candidate, report = rewrite_direct_voiceover(provider, task, sources, plan, timeline, issue)
            # A malformed or overlong suggestion must not be presented as ready.
            new_timeline, _, _ = compile_ad(task, assets, sources, candidate)
        path = folder/'suggestion.json'
        atomic_json(path, {'plan':candidate, 'repair':report, 'approved':False,
            'parent_checkpoint_id':checkpoint_id, 'usage':provider.usage})
        service.store.put_artifact(job_id, 'script_rewrite_suggestion', folder.name, path)
        maximum, _ = narration_timing.budget(task, new_timeline, 1, planning=True)
        if type(report.get('max_speech_units')) is int and report['max_speech_units'] > 0:
            maximum = min(maximum, report['max_speech_units'])
        return {'headings':[{'clip_index':i, 'heading':c.get('heading', '')}
                            for i,c in enumerate(candidate['clips'], 1)],
                'narration_script':candidate['narration_script'], 'target_units':maximum,
                'approved':False}
