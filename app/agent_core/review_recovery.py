"""Explicit recovery adapter for malformed product-ad model reviews.

Uses TaskService's provider factory; retains the saved runtime binding, native
drafts and model decisions. Model proposals are validated, never relabelled as
acceptance by local code. No API key is written to recovery evidence.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
import uuid

from .deepseek import DeepSeekProvider, ModelOutputError
from .provider import ProviderError
from .task_store import atomic_json, digest

FLAGGED_SOURCE_CLAIMS = re.compile('气管|颈椎|肠胃|狼吞虎咽|压迫|耐摔|不贵|十来块|减缓进食')


def excluded_units(sources):
    return {(source['asset_id'], unit['unit_id']) for source in sources
            for unit in source['speech_units'] if FLAGGED_SOURCE_CLAIMS.search(unit['text'])}


def validate_selected_units(plan, sources, blocked):
    by_id = {source['asset_id']: source for source in sources}
    for clip in plan.get('clips', []):
        source = by_id.get(clip.get('asset_id'))
        if source is None:
            raise ModelOutputError('Unknown repair source')
        ids = [unit['unit_id'] for unit in source['speech_units']]
        if clip.get('first_unit') not in ids or clip.get('last_unit') not in ids:
            raise ModelOutputError('Unknown repair speech unit')
        first, last = ids.index(clip['first_unit']), ids.index(clip['last_unit'])
        if first > last or any((source['asset_id'], unit) in blocked for unit in ids[first:last+1]):
            raise ModelOutputError('Selected interval crosses an excluded source claim; choose another complete range')
        if source.get('observations'):
            known = {t for observation in source['observations'] for t in observation['frame_times_us']}
            start = source['speech_units'][first]['start_us']
            end = source['speech_units'][last]['end_us']
            nearby = sorted(t for t in known if start-2_000_000 <= t <= end+2_000_000)
            cited = clip.get('frame_times_us', [])
            if (not isinstance(cited, list) or not cited or any(type(t) is not int or t not in known for t in cited)
                    or not any(t in nearby for t in cited)):
                raise ModelOutputError(f"{source['asset_id']} {clip['first_unit']}..{clip['last_unit']}: cite actual nearby frame_times_us from {nearby}")


def validate_review(value, count):
    if (not isinstance(value, dict) or value.get('schema_version') != '1.0'
            or value.get('decision') not in ('accept', 'revise', 'uncertain')
            or not isinstance(value.get('issues'), list)
            or not isinstance(value.get('limits'), str)):
        raise ModelOutputError('Review must contain schema_version, decision, issues and limits')
    for issue in value['issues']:
        if (not isinstance(issue, dict) or type(issue.get('clip_index')) is not int
                or not 1 <= issue['clip_index'] <= count
                or any(not isinstance(issue.get(k), str) or not issue[k].strip()
                       for k in ('problem', 'suggested_fix'))):
            raise ModelOutputError(f'Each issue needs clip_index 1..{count}, problem and suggested_fix')
    if value['decision'] == 'accept' and value['issues']:
        raise ModelOutputError('accept requires an empty issues list')
    if value['decision'] == 'revise' and not value['issues']:
        raise ModelOutputError('revise requires at least one specific issue')
    return value


class ReviewRecoveryProvider(DeepSeekProvider):
    def __init__(self, config, *, evidence_dir, **kwargs):
        super().__init__(config, **kwargs)
        self.evidence_dir = Path(evidence_dir)
        self.review_calls = 0

    def _json(self, role, payload, *, images=(), time_domain='source'):
        if not (isinstance(payload, dict) and 'selected_transcript' in payload
                and 'plan' in payload and 'source_map' in payload):
            if isinstance(payload, dict) and 'sources' in payload and payload.get('previous'):
                role += ('\n修订时不要选取含气管、颈椎呵护或改掉狼吞虎咽等未证实效果的语音单位。'
                         '只选择结构、放粮放水、拆洗、外观等事实完整表达。'
                         'ASR错字不等于真实音频错误，不要为了错字猜测或生成源素材没有的台词。')
                blocked = excluded_units(payload['sources'])
                request = {**payload, 'excluded_unit_ids': sorted(blocked), 'sources': [
                    {**source, 'speech_units': [unit for unit in source['speech_units']
                        if (source['asset_id'], unit['unit_id']) not in blocked]}
                    for source in payload['sources']]}
                for source in request['sources']:
                    times = {t for observation in source['observations'] for t in observation['frame_times_us']}
                    source['speech_units'] = [{**unit, 'nearby_frame_times_us': sorted(t for t in times
                        if unit['start_us']-2_000_000 <= t <= unit['end_us']+2_000_000)}
                        for unit in source['speech_units']]
                role += '被排除的语音单位不允许选用，也不允许首尾区间跨过被排除单位。'
                for attempt in range(3):
                    proposal = super()._json(role, request, images=images, time_domain=time_domain)
                    try:
                        validate_selected_units(proposal, payload['sources'], blocked)
                        return proposal
                    except ModelOutputError as exc:
                        atomic_json(self.evidence_dir / f'excluded-claim-plan-{uuid.uuid4().hex}.json',
                                    {'proposal': proposal, 'validation_error': str(exc)})
                        if attempt == 2:
                            raise
                        request['response_validation_error'] = str(exc)
                raise AssertionError('Unreachable bounded plan retry')
            return super()._json(role, payload, images=images, time_domain=time_domain)
        clips = payload['plan']['clips']
        speech = {row['segment_id']: row['text'] for row in payload['selected_transcript']}
        numbered = []
        for index, (clip, mapping) in enumerate(zip(clips, payload['source_map']), 1):
            numbered.append({'clip_index': index, 'heading': clip['heading'],
                             **mapping, 'asr_text': speech[mapping['source_segment_id']]})
        if len(numbered) != len(clips):
            raise ModelOutputError('Review mapping does not cover every clip')
        self.review_calls += 1
        error = None
        # A small independent bound also covers empty responses whose transport
        # cannot report token usage. It does not permit unlimited model retries.
        for attempt in range(1, 4):
            request = {**payload, 'review_clips': numbered,
                       'response_validation_error': error}
            review_role = role + (
                '\n复核只使用review_clips里明确给出的clip_index，编号从1开始，不得返回0。'
                '每条问题必须对应该编号的真实标题、台词和时间；不要根据数组下标猜编号。'
                'ASR错字不等于原声音频有错，未写入画面的ASR文本也不是实际字幕。'
                '没有连续动作或听感证据时放在limits，不把静帧未显示某个动作判为动作不存在。'
                '不要为了避免重复而增加没有证据的产品功效或台词。'
                '如果上次格式错误，请重新输出完整有效JSON，不改变验收标准。')
            proposal = None
            try:
                proposal = super()._json(review_role, request, images=images,
                                         time_domain=time_domain)
                validate_review(proposal, len(clips))
            except (ModelOutputError, ProviderError) as exc:
                if isinstance(exc, ProviderError) and str(exc) != 'Provider returned no usable content or tool call.':
                    raise
                error = str(exc)
                atomic_json(self.evidence_dir / f'review-{self.review_calls:02d}-attempt-{attempt:02d}.json',
                            {'valid': False, 'validation_error': error, 'proposal': proposal})
                if attempt == 3:
                    raise
            else:
                atomic_json(self.evidence_dir / f'review-{self.review_calls:02d}-attempt-{attempt:02d}.json',
                            {'valid': True, 'proposal': proposal})
                return proposal


def main():
    from .runtime import load_config
    from .task_service import TaskService
    from .credential_store import credential_environment
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(root / 'config.example.json'))
    parser.add_argument('--state-dir', default=str(root / 'data'))
    parser.add_argument('--job', required=True)
    parser.add_argument('--extra-revision', action='store_true',
                        help='Allow one recorded additional revision after the default two rounds')
    args = parser.parse_args()
    from .resource_policy import require_internal_resources
    require_internal_resources('historical_review_recovery')
    config = load_config(args.config)
    if config.get('vision_provider'):
        raise ValueError('Historical single-model recovery cannot run a multimodel task; use the normal TaskService resume flow')
    service = TaskService(config, args.state_dir)
    job = service.get_job(args.job)
    if job['task_spec']['workflow'] != 'product_ad':
        raise ValueError('Recovery requires a failed product-ad job')
    allowed_errors = ('provider: Provider returned no usable content or tool call.',
                      'Review issue lacks an actual clip and actionable evidence',
                      'Malformed product-ad review')
    if args.extra_revision:
        if job['state'] not in ('READY_FOR_REVIEW', 'FAILED') or job['task_spec']['max_revision_rounds'] != 2:
            raise ValueError('One additional revision requires a completed default two-round run')
        delivery = json.loads((service.store.job_dir(args.job) / 'delivery.json').read_text(encoding='utf-8'))
        if delivery['revision_count'] not in (3, 4) or delivery['model_review_decision'] != 'revise':
            raise ValueError('No unresolved default-limit revision is available')
    elif job['state'] != 'FAILED' or job['error'] not in allowed_errors:
        raise ValueError('This failure is not a supported review recovery case')
    output = service.store.job_dir(args.job) / 'review-recovery' / uuid.uuid4().hex
    output.mkdir(parents=True, exist_ok=False)
    atomic_json(output / 'adapter.json', {'schema_version': '1.0',
        'adapter_sha256': digest(Path(__file__)), 'model': config['provider']['model'],
        'thinking': config['provider'].get('thinking'), 'max_review_attempts': 3,
        'purpose': 'Explicitly number review clips and request schema-correct model answers',
        'automatic_acceptance': False, 'original_error': job['error'],
        'additional_revision_authorized_by_operator': args.extra_revision})
    limits = config.get('limits', {})
    service.provider_factory = lambda: ReviewRecoveryProvider(config['provider'],
        evidence_dir=output, max_calls=limits.get('max_model_calls', 12),
        token_threshold=limits.get('token_stop_threshold', 80000))
    service.store.event(args.job, 'CHECKING', 'progress',
        {'message': '使用明确片段编号的模型复核恢复适配器；保留原草稿和绑定，不替模型做验收决定',
         'adapter_evidence': str(output / 'adapter.json')})
    with credential_environment(config['provider']):
        if args.extra_revision:
            from .commerce import run_ad
            from .local_executor import draft_lock
            if job['task_spec']['runtime_binding'] != service._runtime_binding():
                raise ValueError('Runtime changed; cannot reuse prior native stages')
            assets = [service._check_asset(service.store.get_asset(aid))
                      for aid in job['task_spec']['asset_ids']]
            # Keep the original TaskSpec intact. This explicit operation has its
            # own recorded allowance; a normal resume does not gain extra rounds.
            task = {**job['task_spec'], 'max_revision_rounds': delivery['revision_count']}
            with draft_lock(service.store.job_dir(args.job) / '.job-run.lock'):
                try:
                    result = run_ad(service, args.job, task, assets,
                                    service.executor_factory(args.job), service.provider_factory)
                except (ValueError, ProviderError, ModelOutputError) as exc:
                    service.store.set_state(args.job, 'FAILED', error=str(exc),
                                            event_type='error', message='Additional revision failed validation')
                    result = service.get_job(args.job)
        else:
            result = service.resume_job(args.job)
    summary = {'job_id': args.job, 'state': result['state'], 'error': result['error']}
    atomic_json(output / 'result.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result['state'] == 'READY_FOR_REVIEW' else 2


if __name__ == '__main__':
    raise SystemExit(main())
