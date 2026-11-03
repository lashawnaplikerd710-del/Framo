"""Bounded duration repair: the model chooses among measured visual schedules."""
from itertools import combinations, islice


def feasible_schedules(task, sources):
    target = task.get('target', {}).get('duration_ms')
    if not target or any(s.get('speech_mode') != 'visual' for s in sources):
        return []
    tolerance = max(0, task['target']['tolerance_ms'] - 100)
    pool = []
    for source in sources:
        for unit in source['speech_units']:
            duration = unit['end_us'] - unit['start_us']
            frames = [f['requested_us'] for f in source['frames']
                      if unit['start_us'] <= f['requested_us'] < unit['end_us']]
            if 1_000_000 <= duration <= 10_000_000 and frames:
                pool.append({'asset_id':source['asset_id'], 'first_unit':unit['unit_id'],
                    'last_unit':unit['unit_id'], 'start_us':unit['start_us'], 'end_us':unit['end_us'],
                    'duration_ms':duration/1000, 'frame_times_us':frames})
    # Repair is optional and bounded. The ordinary planner retains the full
    # source pool; do not fabricate a schedule for an unsupported search size.
    if not 1 <= len(pool) <= 20:
        return []
    matches=[]
    for count in range(1, min(10,len(pool))+1):
        for indices in islice(combinations(range(len(pool)),count),100_000):
            chosen=[pool[i] for i in indices]
            total=sum(s['duration_ms'] for s in chosen)
            if abs(total-target)>tolerance:
                continue
            if not task.get('allow_single_source') and task.get('workflow') != 'custom' and len({s['asset_id'] for s in chosen})<2:
                continue
            matches.append((abs(total-target),indices,total))
    if not matches:
        return []
    matches.sort(key=lambda item:(item[0],len(item[1]),item[1]))
    best=matches[0][0]
    matches=[m for m in matches if m[0]<=best+100]
    # Keep choices across the whole source, rather than only its first frames.
    chosen_matches=[matches[round(i*(len(matches)-1)/min(11,len(matches)-1))]
                    for i in range(min(12,len(matches)))] if len(matches)>1 else matches
    schedules=[];seen=set()
    for _,indices,total in chosen_matches:
        raw=[dict(pool[i]) for i in indices]
        merged=[]
        for row in raw:
            if (merged and merged[-1]['asset_id']==row['asset_id'] and merged[-1]['end_us']==row['start_us']
                    and row['end_us']-merged[-1]['start_us']<=10_000_000):
                prev=merged[-1]
                prev.update(last_unit=row['last_unit'],end_us=row['end_us'],
                            duration_ms=(row['end_us']-prev['start_us'])/1000,
                            frame_times_us=prev['frame_times_us']+row['frame_times_us'])
            else:
                merged.append(dict(row))
        # Visual-only shots are atomic after the dense-evidence change. The
        # merged representation is still useful for speech anchored footage,
        # but returning it here would make the duration repair ask the model
        # to combine adjacent visual windows that the compiler must reject.
        visual_only_sources = all(source.get('speech_mode') == 'visual' for source in sources)
        from .ad_narration import enabled as whole_ad
        slot_variants = (raw,) if visual_only_sources and not whole_ad(task) else (raw, merged)
        for slots in slot_variants:
            key=tuple((s['asset_id'],s['first_unit'],s['last_unit']) for s in slots)
            if key in seen:continue
            seen.add(key)
            schedules.append({'schedule_id':f'schedule_{len(schedules)+1:02d}',
                              'measured_total_ms':round(total,3),'slots':slots})
    return schedules


def plan_duration_bound_ad(provider, task, sources, previous):
    from .commerce import planning_evidence
    from .market_rules import active, prompt, speech_budget
    from .timeline_compiler import PlanIssue
    schedules=feasible_schedules(task,sources)
    if not schedules:
        return None
    for schedule in schedules:
        for slot in schedule['slots']:
            seconds=slot['duration_ms']/1000
            budget,unit=speech_budget(task,seconds,planning=True) if active(task) else (max(1,int(seconds*3.4)),'characters')
            slot.update(max_speech_units=budget,speech_unit=unit)
    frames=[f for s in sources for f in s['frames'][::4]][:8]
    result=provider._json(
        '上次计划选段时长计算错误。现在只从已核算的schedules中选择一个最适合真实内容的完整镜头组合。'
        '根据sources.observations的真实画面记录和时间选择，有内容递进，避免重复。'
        '不要求用完整原片，每个组合都满足本任务时长。不要新增、删除、合并或调换组合里的镜头。'
        '只填写每个固定镜头的文案和选择原因，每句必须对应该镜头时间内可见的商品和动作。'
        '可见叶片不能证明新鲜/刚采摘，可见包装不能证明配送承诺；禁止编造甜度、产地、价格、功效。'
        'heading是简短标题，voiceover是自然的完整口播，每句不超过该slot的max_speech_units。'
        '无原声无字幕不是缺陷，直接依据画面写新文案。'+prompt(task)+
        '返回JSON {"schema_version":"1.0","schedule_id":"实际组合ID","title":"简短主题",'
        '"story":"根据画面选择的理由","clips":[{"clip_index":1,"role":"hook|proof|use|closing",'
        '"reason":"内容依据","heading":"短标题","voiceover":"目标语言的新口播",'
        '"palette":"cream|mint|pink","placement":"upper|lower"}]}。',
        {'task':task,'sources':planning_evidence(sources,task=task),'schedules':schedules,
         'previous_error':previous.get('validation_error')},images=frames,require_images=True)
    schedule=next((s for s in schedules if s['schedule_id']==result.get('schedule_id')),None)
    clips=result.get('clips')
    if (schedule is None or not isinstance(clips,list) or len(clips)!=len(schedule['slots'])
            or any(not isinstance(c,dict) or type(c.get('clip_index')) is not int or c['clip_index']!=i
                   for i,c in enumerate(clips,1))):
        raise PlanIssue('时长修订必须选择有效组合，并逐条填写所有镜头文案')
    return {**result,'clips':[{**clip,**{k:slot[k] for k in ('asset_id','first_unit','last_unit','frame_times_us')}}
                             for clip,slot in zip(clips,schedule['slots'])],
            'duration_repair':{'method':'model_selected_measured_schedule','schedule':schedule}}
