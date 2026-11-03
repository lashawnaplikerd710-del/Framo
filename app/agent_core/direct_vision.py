"""Timestamped visual evidence for direct editing, before script generation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .analysis_cache import AnalysisCache
from .task_store import digest
from .deepseek import ModelOutputError, VisualEvidenceError
from .provider import ProviderError


class VisualEvidencePending(ValueError):
    pass


def cached_analyze_frames(root, provider, asset, frames):
    """Share only hash-bound observations, never editorial decisions or prompts."""
    for frame in frames:
        if digest(Path(frame['path'])) != frame['sha256']:
            raise ValueError('Frame evidence changed before visual cache lookup')
    binding = {'asset_id':asset['asset_id'], 'source_sha256':asset['sha256'],
        'frames':[{'requested_us':f['requested_us'], 'sha256':f['sha256']} for f in frames],
        'provider':{k:provider.config.get(k) for k in
                    ('model','base_url','transport','thinking','reasoning_effort')}}
    encoded = json.dumps(binding, ensure_ascii=False, sort_keys=True)
    cache = AnalysisCache(root)
    key = cache.key(source_sha256=asset['sha256'], language='zh',
                    engine='direct_visual_stills_v1', model_identity=encoded)
    saved = cache.read('visual', key)
    if saved is not None:
        report = saved.get('report')
        actual = hashlib.sha256(json.dumps(report, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if (saved.get('binding') != binding or saved.get('report_sha256') != actual
                or not isinstance(report, dict) or report.get('image_input_accepted') is not True):
            raise ValueError('Shared visual evidence cache content changed')
        return report
    # An unreadable existing record must not silently become fresh evidence.
    if (cache.root / 'visual' / (key+'.json')).exists():
        raise ValueError('Shared visual evidence cache is unreadable')
    report = analyze_frames(provider, asset['asset_id'], frames)
    cache.write('visual', key, {'binding':binding, 'report':report,
        'report_sha256':hashlib.sha256(json.dumps(report, ensure_ascii=False, sort_keys=True).encode()).hexdigest()})
    return report


def analyze_frames(provider, asset_id, frames):
    try:
        return _analyze_frames_once(provider, asset_id, frames)
    except VisualEvidencePending as error:
        if str(error) not in ('画面识别结果缺少实际图片回执或完整帧记录', '画面识别结果格式或时间戳不匹配，未进入文案生成'):
            raise
        # Retry real pixels in smaller batches, never invent missing observations.
        reports = [_analyze_frames_once(provider, asset_id, [frame], correction=True) for frame in frames]
        return {**reports[0], 'frames':[row for report in reports for row in report['frames']]}


def _analyze_frames_once(provider, asset_id, frames, *, correction=False):
    """Observe small batches. Source identity/time/hash come from local extraction."""
    labels = [{'requested_us': f['requested_us']} for f in frames]
    try:
        result = provider._json(
            '逐张观察提供的真实视频静帧，用中文记录可见主体、外观和正在展示的动作。'
            '只描述图片中有证据的内容；不要依据文件名猜商品，不根据用户要求虚构卖点。'
            '无音频、无字幕是正常素材；不要因此拒绝观察。静帧不能证明持续动作、材质成分、价格、功效或耐用性。'
            '无法辨认时subject为空字符串、confidence为low，写明uncertainty；不能把猜测当事实。'
            '若能可靠定位主体，可额外返回subject_bbox，使用0到1之间的归一化x、y、w、h；无法定位时省略，不能猜框。'
            '按输入顺序返回所有帧，不合并、不新增时间戳。返回JSON '
            '{"schema_version":"1.0","frames":[{"requested_us":0,"subject":"具体品类或主体",'
            '"description":"本帧真实画面","visible_features":["可见结构"],'
            '"action":"本帧可观察状态或动作","visible_text":["可辨认文字"],'
            '"confidence":"high|medium|low","uncertainty":"核验边界",'
            '"subject_bbox":{"x":0.1,"y":0.1,"w":0.5,"h":0.7}}]}。',
            {'asset_id': asset_id, 'frames': labels, **({'validation_feedback':'上轮帧记录缺失或格式错误；本轮仅一张真实图片，必须按给定时间戳返回完整一条记录'} if correction else {})}, images=frames, require_images=True)
    except (ProviderError, ModelOutputError) as exc:
        # A quota/auth rejection says nothing about the readability of footage.
        # Keep the transport category so the service can show its real remedy.
        if isinstance(exc, ProviderError) and exc.category in (
                'balance', 'authentication', 'missing_key', 'rate_limit', 'overloaded'):
            raise
        raise VisualEvidencePending('画面识别未完成：'+str(exc)) from exc
    rows = result.get('frames')
    if (result.get('schema_version') != '1.0'
            or result.get('_input_evidence', {}).get('images') != 'submitted'
            or not isinstance(rows, list) or len(rows) != len(frames)):
        raise VisualEvidencePending('画面识别结果缺少实际图片回执或完整帧记录')
    verified = []
    for row, frame in zip(rows, frames):
        if (not isinstance(row, dict) or type(row.get('requested_us')) is not int
                or row['requested_us'] != frame['requested_us']
                or row.get('confidence') not in ('high', 'medium', 'low')
                or any(not isinstance(row.get(k), str) for k in ('subject', 'description', 'action', 'uncertainty'))
                or not row['description'].strip()
                or any(not isinstance(row.get(k), list) or len(row[k]) > 30
                       or any(not isinstance(v, str) for v in row[k])
                       for k in ('visible_features', 'visible_text'))):
            raise VisualEvidencePending('画面识别结果格式或时间戳不匹配，未进入文案生成')
        verified_row = {**{k: row[k] for k in ('subject','description','action','visible_features',
                            'visible_text','confidence','uncertainty')},
                         'asset_id':asset_id, 'requested_us':frame['requested_us'],
                         'frame_sha256':frame['sha256']}
        # Keep only a bounded, normalized box.  The value is optional and is
        # used by the OpenClipper-inspired layout supplement; missing boxes
        # remain an explicit center-crop fallback.
        box = row.get('subject_bbox')
        if isinstance(box, dict):
            try:
                values = [float(box[key]) for key in ('x', 'y', 'w', 'h')]
            except (KeyError, TypeError, ValueError):
                values = []
            if (len(values) == 4 and all(0 <= value <= 1 for value in values)
                    and values[2] > 0 and values[3] > 0
                    and values[0] + values[2] <= 1 and values[1] + values[3] <= 1):
                verified_row['subject_bbox'] = {
                    key: round(value, 6) for key, value in zip(('x', 'y', 'w', 'h'), values)}
        verified.append(verified_row)
    return {'schema_version':'1.0', 'asset_id':asset_id, 'frames':verified,
            'model':provider.config['model'], 'image_input_accepted':True,
            'evidence_type':'sampled_stills', 'continuous_video_reviewed':False}


def selected_observations(source, start_us, end_us):
    return [row for report in source.get('observations', []) for row in report.get('frames', [])
            if start_us <= row.get('requested_us', -1) < end_us]
