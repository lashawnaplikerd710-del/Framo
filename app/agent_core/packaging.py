"""Shared, model-planned editable layers. Models select catalog IDs, never code/paths."""
from __future__ import annotations

import math
from pathlib import Path

from .task_store import atomic_json
from .timeline_compiler import PlanIssue

POLICY = 'editable_layers_v1'
CATEGORIES = ('narration', 'captions', 'headings', 'music', 'stickers', 'sfx', 'transitions')


def validate_packaging(plan, timeline):
    if not isinstance(plan, dict) or set(plan) != {'schema_version', 'decisions', 'music', 'stickers', 'sfx', 'transitions'}:
        raise PlanIssue('Packaging requires exactly schema_version, decisions, music, stickers, sfx, transitions')
    if plan['schema_version'] != '1.0' or not isinstance(plan['decisions'],dict) or set(plan['decisions']) != set(CATEGORIES):
        raise PlanIssue('Every packaging category needs an explicit decision')
    for category, row in plan['decisions'].items():
        if (not isinstance(row, dict) or set(row) != {'status', 'reason'} or
                row['status'] not in ('applied', 'not_needed', 'blocked') or
                not isinstance(row['reason'], str) or not row['reason'].strip()):
            raise PlanIssue('Invalid packaging decision: '+category)
        if row['status'] == 'blocked':
            raise PlanIssue('Required packaging is blocked: '+category+': '+row['reason'])
    expected_narration = 'applied' if timeline.get('has_original_audio', True) else 'not_needed'
    if plan['decisions']['narration']['status'] != expected_narration:
        raise PlanIssue('Narration decision must match actual source audio presence; separate existing sound, never invent a silent voice track')
    for category, cues in [('headings',[c for c in timeline['captions'] if 'ad_style' in c]),
                           ('captions',[c for c in timeline['captions'] if 'ad_style' not in c])]:
        if bool(cues) != (plan['decisions'][category]['status']=='applied'):
            raise PlanIssue('Existing editable text and decision disagree: '+category)
    music = plan['music']
    if music is not None:
        if (not isinstance(music, dict) or set(music) != {'theme', 'volume'} or
                music['theme'] not in ('light', 'gentle') or not bounded(music['volume'], .05, .3)):
            raise PlanIssue('Music must use light/gentle original catalog and volume .05.. .3')
    for category in ('music', 'stickers', 'sfx', 'transitions'):
        if bool(plan[category]) != (plan['decisions'][category]['status'] == 'applied'):
            raise PlanIssue('Decision and actual items disagree: '+category)
    count = len(timeline['source_map'])
    for category in ('stickers', 'sfx', 'transitions'):
        rows = plan[category]
        if not isinstance(rows, list) or len(rows) > min(12, count*2):
            raise PlanIssue('Too many packaging events: '+category)
        used = set()
        for row in rows:
            fields = ({'clip_index', 'icon', 'anchor', 'offset_ms', 'duration_ms', 'reason'} if category == 'stickers' else
                      {'clip_index', 'kind', 'offset_ms', 'volume', 'reason'} if category == 'sfx' else
                      {'before_clip', 'duration_frames', 'reason'})
            if not isinstance(row, dict) or set(row) != fields or not isinstance(row['reason'], str) or not row['reason'].strip():
                raise PlanIssue('Invalid event fields: '+category)
            key = 'before_clip' if category == 'transitions' else 'clip_index'
            i = row[key]
            if type(i) is not int or not (2 if category == 'transitions' else 1) <= i <= count or i in used:
                raise PlanIssue('Use a distinct existing clip index for '+category)
            used.add(i)
            clip = timeline['source_map'][i-1]
            length = clip['timeline_end_us']-clip['timeline_start_us']
            if category == 'transitions':
                if type(row['duration_frames']) is not int or not 3 <= row['duration_frames'] <= 9:
                    raise PlanIssue('Transition duration is 3..9 project frames')
                length_us = row['duration_frames']*1_000_000//timeline['native_plan']['canvas']['fps']
                before = timeline['source_map'][i-2]
                if min(length, before['timeline_end_us']-before['timeline_start_us']) < length_us*2:
                    raise PlanIssue('Transition is too long for its neighboring clips')
            else:
                if type(row['offset_ms']) is not int or not 0 <= row['offset_ms']*1000 < length:
                    raise PlanIssue('Event offset is outside its clip')
                if category == 'stickers':
                    if (row['icon'] not in ('sparkle', 'check', 'arrow') or
                            row['anchor'] not in ('upper_left', 'upper_right', 'lower_left', 'lower_right') or
                            type(row['duration_ms']) is not int or not 600 <= row['duration_ms'] <= 3000 or
                            (row['offset_ms']+row['duration_ms'])*1000 > length):
                        raise PlanIssue('Invalid sticker resource/placement/duration')
                elif row['kind'] not in ('chime', 'tick', 'whoosh') or not bounded(row['volume'], .08, .5):
                    raise PlanIssue('Invalid SFX resource/volume')
    return plan


def bounded(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def plan_packaging(provider, task, timeline, evidence, *, previous=None):
    from .resource_policy import require_internal_resources
    require_internal_resources('legacy_external_packaging_plan')
    role = ('你是剪辑声音与视觉包装师。必须为每类包装自主作决定，用户无需逐项点名。'
            '所有新增内容都必须是剪映可编辑独立层，转场是上层视频透明度关键帧叠化，非在线模板。'
            '商品带货默认应有轻音乐、少量卖点贴图、节点音效、适量叠化，不能只拼接视频。'
            '口播访谈可克制使用，依据内容决定，不机械堆满。现有原声会逐镜头分离音轨且视频静音，'
            '保留同期声；素材中的已混合音乐不能单独分离，新增音乐务必低音量，不能声称听过。'
            '只用提供的原创素材目录：light/gentle=原创伴奏，sparkle/check/arrow=本地透明贴图，'
            'has_original_audio=false时narration应为not_needed，说明素材没有音频；不要虚构原声轨。'
            'chime/tick/whoosh=原创音效。未提供TTS工具，使用已有口播，不虚构新配音。'
            '已有文字层已按时间线写入；烧录字幕只能保留，不能当作新增可编辑字幕。'
            '不要更改现有口播字幕或标题，避免贴图遮挡它们与主体。贴图位于左右边缘，轻量短时出现。'
            '每类均有applied或not_needed及具体理由；需要但做不到的填blocked，不允许谎报。'
            '已有文字存在才可将headings或captions填applied。reason为中文。'
            '仅返回JSON：{"schema_version":"1.0","decisions":{'
            '"narration":{"status":"applied","reason":"分离已有原声"},'
            '"captions":{"status":"not_needed","reason":"保留烧录字幕"},'
            '"headings":{"status":"applied","reason":"已有卖点标题"},'
            '"music":{"status":"applied","reason":"情绪节奏"},'
            '"stickers":{"status":"applied","reason":"强调卖点"},'
            '"sfx":{"status":"applied","reason":"提示节点"},'
            '"transitions":{"status":"applied","reason":"段落过渡"}},'
            '"music":{"theme":"light","volume":0.15},'
            '"stickers":[{"clip_index":1,"icon":"sparkle","anchor":"upper_left",'
            '"offset_ms":300,"duration_ms":1000,"reason":"外观亮点"}],'
            '"sfx":[{"clip_index":1,"kind":"chime","offset_ms":300,"volume":0.22,"reason":"开场"}],'
            '"transitions":[{"before_clip":2,"duration_frames":6,"reason":"信息段落变化"}]}。'
            '不需要的music=null，其余为空数组。clip_index从1开始，事件不能超出镜头长度。')
    error = None
    for attempt in range(3):
        proposal = provider._json(role, {'task': task, 'clips': [dict(clip_index=i+1, **m)
            for i, m in enumerate(timeline['source_map'])], 'existing_text': timeline['captions'],
            'has_original_audio':timeline.get('has_original_audio',True),
            'evidence': evidence, 'previous': previous, 'validation_error': error})
        try:
            return validate_packaging(proposal, timeline)
        except PlanIssue as exc:
            error = str(exc)
            if attempt == 2:
                raise


def package_draft(service, job_id, task, provider, adapter, draft, out, timeline, evidence, *, previous=None):
    from .resource_policy import require_internal_resources
    require_internal_resources('package_draft')
    selected_ids={m.get('asset_id') for m in timeline['source_map'] if m.get('asset_id')} or set(task['asset_ids'])
    timeline={**timeline,'has_original_audio':any(service.store.get_asset(a).get('has_audio') for a in selected_ids)}
    rev = timeline.get('revision_id', 'revision_001')
    plan = service._json_stage(job_id, 'packaging_plan', out/'packaging-plan.json',
        lambda: plan_packaging(provider, task, timeline, evidence, previous=previous), revision=rev)
    validate_packaging(plan, timeline)
    request = out/'packaging-request.json'
    if not request.exists():
        atomic_json(request, {'schema': 'jianying-editable-packaging-v1', 'plan': plan,
                              'timeline': timeline})
    result = adapter.package(draft, request, out/'layers')
    service.store.put_artifact(job_id, 'packaging_report', rev, out/'layers/packaging-report.json', verified=True)
    content = out/'layers/draft/draft_content.json'
    service.store.put_artifact(job_id, 'packaged_draft', rev, content, verified=True)
    return content.parent, result


def preview_layers(adapter, draft, output, positions):
    frames=[]
    for start in range(0,len(positions),20):
        result=adapter.preview(draft, Path(output)/f'batch-{start//20+1:02d}', positions[start:start+20])
        frames.extend(result['frames'])
    return {'frames':frames}
