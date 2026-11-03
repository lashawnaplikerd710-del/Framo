"""Process-local task API. CLI and future UIs call this service, never edit drafts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone

from .deepseek import DeepSeekProvider, ModelOutputError, VisualEvidenceError
from .direct_vision import VisualEvidencePending, cached_analyze_frames
from .framo_supplements import annotate_candidates, apply_supplements
from .model_router import create_provider
from .provider import ProviderError
from .local_executor import JianyingAdapter, LocalExecutor, MediaInspector, ToolCancelled, draft_lock
from .task_store import TaskStore, atomic_json, digest
from .batch_model import (DEFAULT_GAP_US, MAX_BATCH_OUTPUTS, batch_counts, batch_state,
                          estimate_timing, validate_batch_spec, workload_features)
from .analysis_cache import AnalysisCache
from .commerce import ScriptRevisionPending
from .formal_ad_policy import (FORMAL_AD_MINIMUM_NATIVE_COUNTS,
                               FORMAL_AD_REQUIRED_NATIVE_CATEGORIES, policy_for)
from .timeline_compiler import PlanIssue, candidates_from_cues, compile_timeline, validate_edit_plan
from .resource_policy import POLICY as RESOURCE_POLICY, bind_policy, require_internal_resources, ResourcePolicyBlocked


DEFAULT_INSTRUCTION = '保留有价值的完整表达，删去重复与无意义停顿，生成可编辑草稿'
from .video_limits import MAX_ASSETS, MAX_SOURCE_US, MAX_OUTPUT_MS, duration_from_instruction
MAX_MONTAGE_ASSETS = MAX_ASSETS

# Cross-border copy is generated in the language normally used in the target
# market.  The model may refine the locale for countries not in this small
# routing table, while native font validation still remains the final guard.
COUNTRY_LOCALES = {
    '中国': 'zh-CN', 'china': 'zh-CN',
    '日本': 'ja-JP', 'japan': 'ja-JP',
    '韩国': 'ko-KR', 'south korea': 'ko-KR', 'korea': 'ko-KR',
    '美国': 'en-US', 'usa': 'en-US', 'us': 'en-US', 'united states': 'en-US',
    '英国': 'en-GB', 'uk': 'en-GB', 'united kingdom': 'en-GB',
    '加拿大': 'en-CA', 'canada': 'en-CA',
    '澳大利亚': 'en-AU', 'australia': 'en-AU',
    '德国': 'de-DE', 'germany': 'de-DE',
    '法国': 'fr-FR', 'france': 'fr-FR',
    '西班牙': 'es-ES', 'spain': 'es-ES',
    '意大利': 'it-IT', 'italy': 'it-IT',
    '葡萄牙': 'pt-PT', 'portugal': 'pt-PT',
    '巴西': 'pt-BR', 'brazil': 'pt-BR',
    '墨西哥': 'es-MX', 'mexico': 'es-MX',
    '俄罗斯': 'ru-RU', 'russia': 'ru-RU',
}


def target_locale(country):
    """Return a stable locale hint without rejecting less common countries."""
    value = str(country or '').strip()
    return COUNTRY_LOCALES.get(value.casefold(), COUNTRY_LOCALES.get(value, 'auto'))


def normalize_target_country(country):
    value = str(country or '').strip()
    if not 1 <= len(value) <= 80 or any(ord(ch) < 32 for ch in value):
        raise ValueError('Cross-border montage requires a target country up to 80 characters')
    return value


def task_spec(instruction, asset_ids, workflow='talking_head', *, target_country=None,
              render_backend='native_jianying'):
    from .video_limits import configure as configure_video
    original_requested = workflow == 'long_video_no_voice'
    long_requested = workflow in ('long_video', 'long_video_no_voice')
    if long_requested:
        if render_backend != 'direct_ffmpeg':
            raise ValueError('长视频请使用 MP4 直接导出')
        workflow = 'custom'
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError('A natural-language instruction is required')
    if workflow not in ('talking_head', 'interview', 'product_montage', 'product_ad', 'custom', 'crossborder_montage'):
        raise ValueError('Unsupported workflow')
    montage = workflow in ('product_montage', 'product_ad', 'custom', 'crossborder_montage')
    minimum_assets = 1 if workflow == 'custom' or (workflow == 'crossborder_montage' and render_backend == 'direct_ffmpeg') else 2
    if montage and (not isinstance(asset_ids, list) or not minimum_assets <= len(asset_ids) <= MAX_MONTAGE_ASSETS or len(set(asset_ids)) != len(asset_ids)):
        raise ValueError(f'{workflow} requires {minimum_assets}..{MAX_MONTAGE_ASSETS} distinct registered video assets')
    if not montage and (not isinstance(asset_ids, list) or len(asset_ids) != 1):
        raise ValueError('The first workflow requires exactly one registered video asset')
    country = normalize_target_country(target_country) if workflow == 'crossborder_montage' else None
    target_ms = duration_from_instruction(instruction)
    vertical = any(word in instruction for word in ('竖屏', '9:16'))
    horizontal = any(word in instruction for word in ('横屏', '16:9'))
    if vertical and horizontal:
        raise ValueError('Conflicting horizontal and vertical aspect requirements')
    aspect = '9:16' if vertical else (
        '16:9' if any(word in instruction for word in ('横屏', '16:9')) else 'source')
    formal_ad = workflow in ('product_ad', 'custom')
    if formal_ad:
        # Formal ads always contain a new editable selling-copy layer. Source
        # burned-in text remains evidence only and is never the replacement.
        captions = True
    elif workflow == 'crossborder_montage':
        # Cross-border ad mode always adds localized selling copy as an
        # editable subtitle layer; the no-voice rule applies only to audio.
        captions = True
    elif montage or '不要字幕' in instruction or '无字幕' in instruction:
        captions = False
    else:
        captions = True
    if render_backend not in ('direct_ffmpeg', 'native_jianying'):
        raise ValueError('Unsupported render backend')
    # Direct rendering is now the default delivery path.  The legacy native
    # draft path remains available only when explicitly requested by an
    # integration that still needs an editable Jianying project.
    deliverable = 'final_export' if render_backend == 'direct_ffmpeg' else (
        'final_export' if any(word in instruction for word in ('成片', '导出MP4', '导出 mp4')) else 'draft')
    # Only explicitly quoted speech becomes a hard local constraint. General
    # requests such as "删除重复" remain editorial goals, not fake transcript text.
    must_keep = re.findall(r'(?:必须保留|务必保留|保留原话)\s*[“"「]([^”"」]+)[”"」]', instruction)
    must_remove = re.findall(r'(?:必须删除|务必删除|删掉原话|删除原话)\s*[“"「]([^”"」]+)[”"」]', instruction)
    if set(must_keep) & set(must_remove):
        raise ValueError('The same quoted speech cannot be both kept and removed')
    allow_reorder = not any(word in instruction for word in
                            ('按原顺序', '不要重排', '不许重排', '保持原顺序'))
    preserve_audio_request = any(word in instruction for word in
                                 ('保留原声', '保留原口播', '保留素材声音', '保留素材原声'))
    # These workflows are source-audio edits by contract.  A user should not
    # have to repeat the category's promise in the free-form instruction.  Ad
    # workflows remain muted unless product_ad receives the existing explicit
    # preserve request; cross-border ads are always text-only and therefore
    # keep the source audio muted.
    if workflow == 'crossborder_montage':
        preserve_source_audio = False
    elif workflow == 'product_ad':
        preserve_source_audio = preserve_audio_request
    else:
        preserve_source_audio = preserve_audio_request or workflow in (
            'talking_head', 'interview', 'product_montage')
    policy = policy_for(workflow)
    spec = {'schema_version': '1.0', 'instruction': instruction.strip(), 'asset_ids': asset_ids,
            'workflow': workflow, **({'creation_mode':'long_video_v1'} if long_requested else {}),
            **({'original_montage_policy':'semantic_original_montage_v1', 'opening_policy':'source_prefix_40s_v1'} if original_requested else {}), 'target': {'duration_ms': target_ms, 'tolerance_ms': 3000,
                                             'aspect_ratio': aspect, 'fps': 30},
            'constraints': {'preserve_meaning': True, 'must_keep': must_keep,
                            'must_remove': must_remove,
                            'allow_reorder': allow_reorder, 'subtitles': captions},
            'deliverable': deliverable, 'render_backend': render_backend,
            'delivery_mode': 'mp4' if render_backend == 'direct_ffmpeg' else 'draft',
            'resource_source_policy': 'online_or_builtin',
            'max_revision_rounds': 2,
            'packaging_policy': 'editable_layers_v1',
            'resource_policy': RESOURCE_POLICY,
            'resource_use': 'commercial' if montage else 'local_review',
            'transcription_language': ('ja' if '日语' in instruction else 'auto') if montage else 'zh',
            'subtitle_policy': ('editable_selling_copy_plus_original_burned_text'
                                if formal_ad else
                                'editable_localized_selling_copy_plus_original_burned_text'
                                if workflow == 'crossborder_montage' else
                                'preserve_source' if montage else 'editable_if_absent'),
            'formal_ad_policy': policy,
            'allow_single_source': workflow == 'custom',
            'mute_source_audio': not preserve_source_audio,
            'required_native_categories': list(FORMAL_AD_REQUIRED_NATIVE_CATEGORIES)
                if workflow == 'product_ad' else list(policy.get('required_native_categories', [])),
            'minimum_native_counts': dict(FORMAL_AD_MINIMUM_NATIVE_COUNTS)
                if workflow == 'product_ad' else dict(policy.get('minimum_native_counts', {})),
            'require_online_font': bool(formal_ad),
            'minimum_online_fonts': 1 if formal_ad else 0,
            'narration_mode': 'new_native' if formal_ad else 'preserve_source',
            'direct_media': {},
            'require_direct_audio_resources': False,
            **({'target_country': country, 'target_locale': target_locale(country),
                'copy_only': True, 'narration_mode': 'text_only'} if country else {}),
            'defaults_applied': ['tolerance_ms=3000', 'fps=30', 'preserve_meaning=true',
                                 *(['allow_reorder=true'] if allow_reorder else []),
                                 'source_audio_muted=true' if not preserve_source_audio else
                                 'source_audio_muted=false; explicit preserve request',
                                 *(['subtitles=true'] if captions else []),
                                 *(['subtitle_policy=editable_selling_copy_plus_original_burned_text; new headings required']
                                   if formal_ad else
                                   ['subtitle_policy=editable_localized_selling_copy_plus_original_burned_text; no narration']
                                   if workflow == 'crossborder_montage' else
                                   ['subtitle_policy=preserve_source; embedded audio retained'] if montage else [])]}
    if original_requested:
        from .editing_rules import configure_rules
        spec = configure_rules(spec)
    return spec


class TaskService:
    def __init__(self, config, root, *, inspector=None, provider_factory=None,
                 executor_factory=None):
        self.config = config
        self.store = TaskStore(root)
        self.inspector = inspector or MediaInspector()
        limits = config.get('limits', {})
        self.provider_factory = provider_factory or (lambda: create_provider(config))
        if executor_factory is None:
            def _default_executor(job_id):
                # Direct FFmpeg jobs still reuse the local ASR/frame adapters,
                # but they do not need a Jianying installation.  Give the
                # adapter a valid bundle root so it can operate without ever
                # entering the native host path.
                install = config.get('install_dir')
                if not install:
                    install = str(Path(__file__).resolve().parents[1])
                return LocalExecutor(
                    Path(__file__).resolve().parents[1] / 'python/python.exe',
                    Path(config['toolchain_root']) / 'scripts', install, self.store, job_id)
            self.executor_factory = _default_executor
        else:
            self.executor_factory = executor_factory
        self.model_dir = config.get('asr_model_dir')

    def register_asset(self, input):
        record = self.inspector.inspect(input)
        if record.get('video_duration_us', record.get('duration_us', 0)) > MAX_SOURCE_US:
            raise ValueError('单条素材最长支持 7 分钟，请分段后再添加')
        if str(record.get('frame_rate_mode','')).upper()=='VFR':
            from .media_normalize import normalize
            record=normalize(record,self.inspector,self.store.root/'cache/normalized-media',
                Path(self.config.get('direct_ffmpeg_dir') or self.config['toolchain_root'])/'ffmpeg.exe')
        return self.store.put_asset(record)

    def create_job(self, spec):
        if not isinstance(spec, dict) or spec.get('schema_version') != '1.0':
            raise ValueError('TaskSpec schema_version must be 1.0')
        if not isinstance(spec.get('instruction'), str) or not 1 <= len(spec['instruction'].strip()) <= 10_000:
            raise ValueError('TaskSpec needs a nonempty instruction up to 10000 characters')
        from .font_library import validate_subtitle_font_id
        validate_subtitle_font_id(spec.get('subtitle_font_id'))
        workflow = spec.get('workflow')
        from .market_rules import active as market_rules_active
        market_ad = market_rules_active(spec)
        montage = workflow in ('product_montage', 'product_ad', 'custom', 'crossborder_montage')
        if montage and (not isinstance(spec.get('asset_ids'), list) or
                not (1 if spec.get('workflow') == 'custom' or market_ad else 2) <= len(spec['asset_ids']) <= MAX_MONTAGE_ASSETS or len(set(spec['asset_ids'])) != len(spec['asset_ids'])):
            minimum_assets = 1 if spec.get('workflow') == 'custom' or market_ad else 2
            raise ValueError(f'{spec.get("workflow")} requires {minimum_assets}..{MAX_MONTAGE_ASSETS} distinct assets')
        if not montage and (not isinstance(spec.get('asset_ids'), list) or len(spec['asset_ids']) != 1):
            raise ValueError('First workflow requires exactly one asset_id')
        for asset_id in spec['asset_ids']:
            self._check_asset(self.store.get_asset(asset_id))
        if workflow not in ('talking_head', 'interview', 'product_montage', 'product_ad', 'custom', 'crossborder_montage'):
            raise ValueError('Unsupported workflow')
        if workflow == 'crossborder_montage':
            country = normalize_target_country(spec.get('target_country'))
            if spec.get('target_locale') != target_locale(country):
                raise ValueError('Cross-border target locale does not match target country')
            if spec.get('copy_only') is not True and not market_ad:
                raise ValueError('Cross-border montage is text-only and does not support narration')
        render_backend = spec.get('render_backend', 'native_jianying')
        if render_backend not in ('direct_ffmpeg', 'native_jianying'):
            raise ValueError('Unsupported render backend')
        if render_backend == 'direct_ffmpeg' and spec.get('delivery_mode') != 'mp4':
            raise ValueError('Direct rendering requires delivery_mode=mp4')
        if render_backend == 'native_jianying' and spec.get('deliverable') != 'draft':
            raise ValueError('Native final MP4 export is not verified; request a draft')
        target, constraints = spec.get('target'), spec.get('constraints')
        if not isinstance(target, dict) or not isinstance(constraints, dict):
            raise ValueError('TaskSpec requires target and constraints')
        if (target.get('aspect_ratio') not in ('9:16', '16:9', 'source') or
                type(target.get('fps')) is not int or not 1 <= target['fps'] <= 120 or
                type(target.get('tolerance_ms')) is not int or target['tolerance_ms'] < 0 or
                (target.get('duration_ms') is not None and
                 (type(target['duration_ms']) is not int or not 1_000 <= target['duration_ms'] <= MAX_OUTPUT_MS))):
            raise ValueError('TaskSpec target is invalid')
        if any(type(constraints.get(key)) is not bool for key in
               ('preserve_meaning', 'allow_reorder', 'subtitles')):
            raise ValueError('TaskSpec boolean constraints are invalid')
        if workflow == 'product_montage' and (
                spec.get('subtitle_policy') != 'preserve_source' or constraints['subtitles']):
            raise ValueError('Product montage currently preserves source text; new subtitle layers are unsupported')
        prompt_rules = spec.get('editing_rules_version') == 'prompt_first_v1' and render_backend == 'direct_ffmpeg'
        if workflow == 'crossborder_montage' and not prompt_rules and (
                spec.get('subtitle_policy') != 'editable_localized_selling_copy_plus_original_burned_text'
                or not constraints['subtitles']):
            raise ValueError('Cross-border ad workflows require an editable localized selling-copy layer')
        if workflow in ('product_ad', 'custom') and not prompt_rules and (spec.get('subtitle_policy') !=
                'editable_selling_copy_plus_original_burned_text' or not constraints['subtitles']):
            raise ValueError('Formal ad workflows require a new editable selling-copy layer')
        if spec.get('transcription_language', 'zh') not in ('auto', 'zh', 'ja', 'en', 'ko', 'yue'):
            raise ValueError('Invalid transcription language')
        if any(not isinstance(constraints.get(key), list) or
               any(not isinstance(item, str) for item in constraints[key])
               for key in ('must_keep', 'must_remove')):
            raise ValueError('TaskSpec keep/remove constraints must be string lists')
        rounds = spec.get('max_revision_rounds')
        if type(rounds) is not int or not 0 <= rounds <= 3:
            raise ValueError('max_revision_rounds must be an integer in 0..3')
        spec = bind_policy(spec)
        if spec.get('resource_use', 'commercial' if montage else 'local_review') not in ('commercial','local_review'):
            raise ValueError('resource_use must be commercial or local_review')
        required=spec.get('required_native_categories',[])
        from .native_packaging import KINDS
        if not isinstance(required,list) or any(k not in KINDS for k in required) or len(set(required))!=len(required):
            raise ValueError('Invalid required native categories')
        if type(spec.get('require_online_font',False)) is not bool:
            raise ValueError('require_online_font must be boolean')
        counts=spec.get('minimum_native_counts',{})
        if (not isinstance(counts,dict) or any(k not in KINDS or type(v) is not int or not 0<=v<=32
                for k,v in counts.items()) or sum(counts.values())>32):
            raise ValueError('Invalid minimum native element counts')
        fonts=spec.get('minimum_online_fonts',0)
        if type(fonts) is not int or not 0<=fonts<=8:raise ValueError('Invalid minimum online fonts')
        if type(spec.get('separate_narration',False)) is not bool:
            raise ValueError('separate_narration must be boolean')
        if not isinstance(spec.get('direct_media', {}), dict):
            raise ValueError('direct_media must be an object')
        if type(spec.get('require_direct_audio_resources', False)) is not bool:
            raise ValueError('require_direct_audio_resources must be boolean')
        mode=spec.get('narration_mode')
        if mode is None:
            mode=('new_native' if any(s in spec['instruction'] for s in ('全新口播','全新配音','重新配音')) else
                  'preserve_source' if any(s in spec['instruction'] for s in ('保留原口播','保留原声')) else
                  self.config.get('narration',{}).get('product_ad_default','preserve_source')
                  if spec['workflow']=='product_ad' else
                  'new_native' if spec['workflow']=='custom' else 'preserve_source')
        if workflow == 'product_ad':
            # Source speech is reference evidence only; formal ads always get
            # a newly authored native narration track.
            mode = 'new_native'
        if mode not in ('new_native','preserve_source','text_only'):raise ValueError('Invalid narration mode')
        if mode == 'text_only' and workflow != 'crossborder_montage' and not prompt_rules:
            raise ValueError('Text-only narration mode requires crossborder_montage workflow')
        if workflow == 'crossborder_montage' and mode != 'text_only' and not prompt_rules:
            raise ValueError('Cross-border montage is text-only and does not support narration')
        if mode=='new_native' and spec['workflow'] not in ('product_ad', 'custom') and not market_ad:
            raise ValueError('New native narration currently requires product_ad or custom workflow')
        spec={**spec,'narration_mode':mode}
        if mode=='new_native':spec['separate_narration']=True
        if workflow == 'crossborder_montage' and not prompt_rules:
            spec['narration_mode'] = 'text_only'
            spec['separate_narration'] = False
        minimum=target.get('minimum_duration_ms',0)
        if type(minimum) is not int or not 0 <= minimum <= MAX_OUTPUT_MS:
            raise ValueError('Invalid minimum duration')
        from .caption_policy import configure as configure_captions
        spec = configure_captions(spec)
        from .ad_narration import configure as configure_ad_audio
        spec = configure_ad_audio(spec)
        spec['runtime_binding'] = self._runtime_binding()
        return self.store.put_job(spec)

    def create_batch(self, specs, *, gap_us=DEFAULT_GAP_US, quality='standard'):
        """Create up to fifteen ordinary child jobs under one durable batch.

        Child creation reuses ``create_job`` and therefore keeps the existing
        task validation, runtime binding and checkpoint contracts intact.
        """
        if not isinstance(specs, list) or not 1 <= len(specs) <= MAX_BATCH_OUTPUTS:
            raise ValueError(f'batch supports 1..{MAX_BATCH_OUTPUTS} outputs')
        rows = []
        for index, spec in enumerate(specs, 1):
            if not isinstance(spec, dict):
                raise ValueError(f'batch output {index} must be an object')
            rows.append({'output_id': spec.get('output_id', f'output_{index:02d}'),
                         'instruction': spec.get('instruction', ''),
                         'asset_ids': spec.get('asset_ids', []),
                         'workflow': spec.get('workflow', 'product_ad'),
                         'render_backend': spec.get('render_backend',
                                                     self.config.get('render_backend_default',
                                                                     'native_jianying')),
                         **({'target_country': spec['target_country']} if spec.get('target_country') else {})})
        normalized = validate_batch_spec({'outputs': rows, 'gap_us': gap_us,
                                          'quality': quality})
        batch_id = self.store.put_batch(normalized)
        children = []
        try:
            for index, row in enumerate(normalized['outputs'], 1):
                source = specs[index - 1]
                spec = (dict(source) if source.get('schema_version') == '1.0' and
                        isinstance(source.get('target'), dict) and isinstance(source.get('constraints'), dict)
                        else task_spec(row['instruction'], row['asset_ids'], row['workflow'],
                                       target_country=row.get('target_country'),
                                       render_backend=row.get('render_backend', 'native_jianying')))
                # The batch link is metadata only; all existing job checks and
                # the stage-one registration flow remain unchanged.
                spec = {**spec, 'batch_id': batch_id,
                        'batch_output_id': row['output_id'],
                        'batch_output_index': index,
                        'batch_gap_us': normalized['gap_us'],
                        'batch_quality': normalized['quality'],
                        'timing_plan': estimate_timing(workload_features(
                            duration_us=((spec.get('target') or {}).get('duration_ms') or 0) * 1000,
                            source_count=len(spec.get('asset_ids', [])), output_count=len(normalized['outputs']))) }
                job_id = self.create_job(spec)
                self.store.add_batch_child(batch_id, row['output_id'], job_id, index)
                children.append({'output_id': row['output_id'], 'job_id': job_id,
                                 'state': 'CREATED', 'ordinal': index})
        except Exception as exc:
            self.store.set_batch_state(batch_id, 'FAILED', error=str(exc), children=children)
            raise
        manifest = self.store.batch_dir(batch_id) / 'children.json'
        atomic_json(manifest, {'schema_version': '1.0', 'batch_id': batch_id,
                               'children': children, 'gap_us': normalized['gap_us']})
        self.store.set_batch_state(batch_id, 'CREATED', children=children)
        return {'batch_id': batch_id, 'children': children,
                'output_count': len(children), 'gap_us': normalized['gap_us']}

    def batch_status(self, batch_id):
        batch = self.store.get_batch(batch_id)
        children = []
        for row in batch['children']:
            child = self.store.get_job(row['job_id'])
            children.append({'output_id': row['output_id'], 'job_id': row['job_id'],
                             'ordinal': row['ordinal'], 'state': child['state'],
                             'error': child['error'], 'updated_at': child['updated_at']})
        counts = batch_counts(child['state'] for child in children)
        state = batch_state(child['state'] for child in children)
        if state != batch['state']:
            self.store.set_batch_state(batch_id, state,
                                       children=children)
        report = {'schema_version': '1.0', 'batch_id': batch_id, 'state': state,
                'counts': counts, 'children': children,
                'gap_us': batch['batch_spec'].get('gap_us', DEFAULT_GAP_US),
                'quality': batch['batch_spec'].get('quality', 'standard'),
                'generated_at': datetime.now(timezone.utc).isoformat()}
        finalization_path = self.store.batch_dir(batch_id) / 'batch-finalization.json'
        if counts['success']:
            try:
                report['finalization'] = self.finalize_batch(batch_id)
            except Exception as exc:
                report['finalization'] = {'status': 'preparation_failed',
                                          'error': type(exc).__name__ + ': ' + str(exc)}
        elif finalization_path.is_file():
            try:
                report['finalization'] = json.loads(finalization_path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                report['finalization'] = {'status': 'report_unreadable'}
        atomic_json(self.store.batch_dir(batch_id) / 'batch-report.json', report)
        return {**report, 'report_path': str(self.store.batch_dir(batch_id) / 'batch-report.json')}

    def list_batches(self, limit=100):
        return [self.batch_status(row['batch_id']) for row in self.store.list_batches(limit)]

    def retry_batch_child(self, batch_id, output_id):
        batch = self.store.get_batch(batch_id)
        child = next((row for row in batch['children'] if row['output_id'] == output_id), None)
        if child is None:
            raise KeyError('Unknown batch output_id')
        job = self.store.get_job(child['job_id'])
        if job['state'] not in ('FAILED', 'WAITING_FOR_INPUT', 'BLOCKED_RESOURCES'):
            raise ValueError('Only failed or waiting batch outputs can be retried')
        self.store.clear_cancel(child['job_id'])
        self.store.set_state(child['job_id'], 'CREATED', error=None,
                             message='批次子任务已排队重试')
        return {'batch_id': batch_id, 'output_id': output_id,
                'job_id': child['job_id'], 'state': 'CREATED'}

    def _batch_snapshots(self, batch):
        """Read the currently usable child drafts and build a stable input key.

        Children finish independently.  The key makes finalization resumable:
        a later child must cause a new master draft, while an unchanged batch
        can safely reuse its existing reports and registration.
        """
        snapshots = []
        missing = []
        fingerprint_rows = []
        for row in batch['children']:
            job = self.store.get_job(row['job_id'])
            if job['state'] not in ('READY_FOR_REVIEW', 'COMPLETED'):
                missing.append({'output_id': row['output_id'], 'state': job['state']})
                fingerprint_rows.append([row['output_id'], job['state'], None])
                continue
            artifacts = self.list_artifacts(row['job_id'])
            delivery = next((item for item in reversed(artifacts) if item['kind'] == 'delivery'), None)
            if not delivery:
                missing.append({'output_id': row['output_id'], 'state': 'missing_delivery'})
                fingerprint_rows.append([row['output_id'], 'missing_delivery', None])
                continue
            data = json.loads(Path(delivery['path']).read_text(encoding='utf-8-sig'))
            draft = Path(data['draft_path'])
            content = draft / 'draft_content.json'
            if not content.is_file():
                missing.append({'output_id': row['output_id'], 'state': 'missing_draft'})
                fingerprint_rows.append([row['output_id'], 'missing_draft', None])
                continue
            snapshot = json.loads(content.read_text(encoding='utf-8-sig'))
            snapshot_sha = hashlib.sha256(json.dumps(
                snapshot, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
            snapshots.append((row['output_id'], snapshot, draft))
            fingerprint_rows.append([row['output_id'], job['state'], snapshot_sha])
        fingerprint = hashlib.sha256(json.dumps(
            fingerprint_rows, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        return snapshots, missing, fingerprint

    def finalize_batch(self, batch_id, *, install=None):
        """Compose successful child drafts and prepare independent exports.

        This method is idempotent for an unchanged child set. It never claims
        MP4 success when the installed native export contract is unavailable.
        """
        batch = self.store.get_batch(batch_id)
        # Direct-output batches already have one immutable MP4 per child; they
        # do not need (and must not create) a synthetic Jianying master draft.
        if batch['children'] and all(
                self.store.get_job(row['job_id'])['task_spec'].get('render_backend') == 'direct_ffmpeg'
                for row in batch['children']):
            outputs, missing, fingerprint_rows = [], [], []
            for row in batch['children']:
                job = self.store.get_job(row['job_id'])
                artifact = next((item for item in reversed(self.list_artifacts(row['job_id']))
                                 if item['kind'] == 'delivery'), None)
                if job['state'] not in ('COMPLETED', 'READY_FOR_REVIEW') or not artifact:
                    missing.append({'output_id': row['output_id'], 'state': job['state']})
                    fingerprint_rows.append([row['output_id'], job['state'], None])
                    continue
                delivery = json.loads(Path(artifact['path']).read_text(encoding='utf-8-sig'))
                output = Path(delivery.get('output_file', ''))
                if not output.is_file():
                    missing.append({'output_id': row['output_id'], 'state': 'missing_output'})
                    fingerprint_rows.append([row['output_id'], 'missing_output', None])
                    continue
                output_sha = digest(output)
                outputs.append({'output_id': row['output_id'], 'job_id': row['job_id'],
                                'status': 'COMPLETED', 'output_file': str(output),
                                'sha256': output_sha, 'duration_us': delivery.get('duration_us'),
                                'media': delivery.get('media')})
                fingerprint_rows.append([row['output_id'], job['state'], output_sha])
            fingerprint = hashlib.sha256(json.dumps(fingerprint_rows, ensure_ascii=False,
                                                    separators=(',', ':')).encode()).hexdigest()
            folder = self.store.batch_dir(batch_id)
            finalization_path = folder / 'batch-finalization.json'
            previous = None
            if finalization_path.is_file():
                try:
                    previous = json.loads(finalization_path.read_text(encoding='utf-8-sig'))
                except (OSError, ValueError):
                    previous = None
            if previous and previous.get('input_fingerprint') == fingerprint:
                return previous
            result = {'schema_version': '1.0', 'batch_id': batch_id,
                      'status': 'direct_outputs_ready' if not missing else 'direct_outputs_partial',
                      'render_backend': 'direct_ffmpeg', 'master_snapshot': None,
                      'master_draft': None, 'outputs': outputs, 'missing': missing,
                      'validation': {'status': 'clear' if not missing else 'waiting',
                                     'output_count': len(outputs), 'empty_gaps_verified': True},
                      'quality': {'status': 'clear' if not missing else 'waiting'},
                      'exports': {'status': 'already_rendered', 'outputs': outputs},
                      'input_fingerprint': fingerprint, 'registration': None}
            atomic_json(finalization_path, result)
            return result
        snapshots, missing, input_fingerprint = self._batch_snapshots(batch)
        if not snapshots:
            return {'status': 'waiting_children', 'missing': missing, 'batch_id': batch_id}
        folder = self.store.batch_dir(batch_id)
        previous = None
        finalization_path = folder / 'batch-finalization.json'
        if finalization_path.is_file():
            try:
                previous = json.loads(finalization_path.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                previous = None
        if previous and previous.get('input_fingerprint') == input_fingerprint:
            return previous
        from .master_draft import compose_master, write_snapshot_draft
        from .batch_quality import inspect_batch_snapshot
        master, validation = compose_master([(item[0], item[1]) for item in snapshots],
                                            gap_us=batch['batch_spec'].get('gap_us', DEFAULT_GAP_US),
                                            name=f"剪映 Agent 批量成品 {batch_id[-6:]}")
        quality = inspect_batch_snapshot(master, strict_files=True)
        registered_before = bool(previous and
                                 (previous.get('registration') or {}).get('homepage_index_registered'))
        suffix = '' if not registered_before else '-' + input_fingerprint[:8]
        master_path = folder / ('master-snapshot' + suffix + '.json')
        atomic_json(master_path, master)
        draft_dir = folder / ('master-draft' + suffix)
        if not draft_dir.exists():
            write_snapshot_draft(master, draft_dir, templates=[item[2] for item in snapshots])
        else:
            # Rebuild only the deterministic content, never touch a user
            # registered draft in the homepage.
            atomic_json(draft_dir / 'draft_content.json', master)
        child_state_rows = [{'output_id': row['output_id'],
                             'state': self.store.get_job(row['job_id'])['state']}
                            for row in batch['children']]
        self.store.set_batch_state(batch_id, batch_state(item['state'] for item in child_state_rows),
                                   children=child_state_rows)
        result = {'schema_version': '1.0', 'batch_id': batch_id,
                  'status': 'master_ready' if validation['status'] == 'clear' and quality['status'] == 'clear' else 'master_needs_review',
                  'master_snapshot': str(master_path), 'master_draft': str(draft_dir),
                  'validation': validation, 'quality': quality, 'missing': missing, 'exports': None}
        try:
            from .batch_export import prepare_independent_outputs
            export_dir = folder / ('independent-outputs' + suffix)
            if not export_dir.exists():
                master_for_export = json.loads((draft_dir / 'draft_content.json').read_text(encoding='utf-8-sig'))
                result['exports'] = prepare_independent_outputs(master_for_export, export_dir,
                                                                template=draft_dir)
            else:
                result['exports'] = json.loads((export_dir / 'independent-export-report.json').read_text(encoding='utf-8'))
        except Exception as exc:
            result['exports'] = {'status': 'preparation_failed', 'error': str(exc)}
        result['input_fingerprint'] = input_fingerprint
        # A changed child set gets a new master draft. Never carry the old
        # homepage registration over to that new fingerprint.
        result['registration'] = None
        atomic_json(finalization_path, result)
        return result

    def export_batch(self, batch_id):
        finalization = self.store.batch_dir(batch_id) / 'batch-finalization.json'
        if not finalization.is_file():
            prepared = self.finalize_batch(batch_id)
            if not finalization.is_file():
                return prepared
        finalization_data = json.loads(finalization.read_text(encoding='utf-8-sig'))
        if finalization_data.get('render_backend') == 'direct_ffmpeg':
            return finalization_data.get('exports') or {'status': 'already_rendered',
                                                        'outputs': finalization_data.get('outputs', [])}
        master_path = Path(finalization_data['master_snapshot'])
        master = json.loads(master_path.read_text(encoding='utf-8-sig'))
        from .batch_export import export_independent_outputs
        report = export_independent_outputs(master, self.store.batch_dir(batch_id) / 'independent-outputs-export',
                                            install=self.config.get('install_dir'))
        finalization_data['exports'] = report
        atomic_json(finalization, finalization_data)
        return report

    def publish_batch(self, batch_id):
        """Register the complete multi-output master draft in Jianying's homepage.

        This uses the existing native register adapter on a fresh master draft.
        It does not alter the single-job publication path or claim GUI
        visibility beyond the verified homepage-index readback.
        """
        batch = self.store.get_batch(batch_id)
        finalization = self.finalize_batch(batch_id)
        if finalization.get('render_backend') == 'direct_ffmpeg':
            return {'status': 'not_applicable', 'reason': '直接成片模式不创建或登记剪映草稿',
                    'batch_id': batch_id, 'outputs': finalization.get('outputs', [])}
        if finalization.get('status') not in ('master_ready', 'all_outputs_exported_needs_quality_review'):
            return {'status': 'waiting_for_batch', 'batch_id': batch_id,
                    'finalization': finalization}
        if finalization.get('missing'):
            return {'status': 'waiting_for_batch', 'batch_id': batch_id,
                    'missing': finalization['missing']}
        prior = finalization.get('registration')
        if isinstance(prior, dict) and prior.get('homepage_index_registered'):
            return prior
        root = Path(self.config.get('draft_root', ''))
        user_data = Path(self.config.get('user_data_dir') or
                         Path(os.environ.get('LOCALAPPDATA', '')) / 'JianyingPro/User Data')
        if not root.is_dir() or not user_data.is_dir():
            raise ValueError('Configure existing draft_root and user_data_dir before batch publication')
        from .native_host import editor_pids
        if editor_pids():
            raise RuntimeError('Save and exit Jianying before publishing a registered draft')
        children = batch['children']
        if not children:
            raise ValueError('Batch has no child outputs')
        # Registration may bypass the ordinary packaging gate only when every
        # child carries the same verified native-resource evidence used by the
        # existing single-job registration workflow.
        for child in children:
            job = self.store.get_job(child['job_id'])
            delivery_artifact = next((item for item in reversed(self.list_artifacts(child['job_id']))
                                      if item['kind'] == 'delivery'), None)
            if not delivery_artifact:
                raise ResourcePolicyBlocked('batch_publication_missing_child_delivery')
            delivery = json.loads(Path(delivery_artifact['path']).read_text(encoding='utf-8-sig'))
            if not delivery.get('editable_layers_verified'):
                raise ResourcePolicyBlocked('batch_publication_child_resources_unverified')
            revision = delivery.get('revision_id', delivery_artifact['revision_id'])
            packaging = self.store.artifact(child['job_id'], 'packaging_report', revision)
            if packaging is None or not packaging.get('verified'):
                raise ResourcePolicyBlocked('batch_publication_missing_child_packaging_evidence')
        first_job = children[0]['job_id']
        from .local_executor import JianyingAdapter
        executor = self.executor_factory(first_job)
        folder = self.store.batch_dir(batch_id)
        master_dir = Path(finalization['master_draft']).resolve(strict=True)
        fingerprint = finalization.get('input_fingerprint', batch_id[-8:])
        publication_root = folder / 'batch-publication'
        attempt = 1
        output = publication_root / fingerprint
        while output.exists():
            attempt += 1
            output = publication_root / f'{fingerprint}-attempt-{attempt}'
        name = 'Agent_批次_' + batch_id[-8:] + '_' + fingerprint[:6]
        if attempt > 1:
            name += f'_{attempt}'
        # LocalExecutor deliberately confines native inputs/outputs to one
        # child job.  Stage a self-contained copy under that child so the
        # existing native registration adapter can be reused without relaxing
        # its output-boundary checks.
        staging = self.store.job_dir(first_job) / 'batch-master-staging' / fingerprint
        if staging.exists():
            raise ValueError('Batch registration staging already exists; inspect the prior attempt')
        from .master_draft import write_snapshot_draft
        master_snapshot = json.loads((master_dir / 'draft_content.json').read_text(encoding='utf-8-sig'))
        write_snapshot_draft(master_snapshot, staging, template=master_dir)
        report = JianyingAdapter(executor).publish(staging, root, user_data, name, output,
                                                  resource_verified=True)
        registration = report.get('registration') or {}
        if (report.get('status') != 'native_draft_registered' or
                not registration.get('index_readback_equal') or
                not registration.get('existing_entries_preserved')):
            raise ValueError('Batch homepage registration did not verify the index readback')
        summary = {'schema_version': '1.0', 'batch_id': batch_id,
                   'registered_draft_path': report['destination'],
                   'homepage_index_registered': True,
                   'homepage_index_readback_passed': True,
                   'draft_fingerprint': digest(master_dir / 'draft_content.json'),
                   'staged_draft_path': str(staging),
                   'registration_report': str(output / 'registration-report.json'),
                   'local_review_only': True, 'final_export_created': False,
                   'jianying_open_verified': False}
        atomic_json(folder / 'batch-registration-report.json', {**summary, 'native_report': report})
        finalization['registration'] = summary
        atomic_json(folder / 'batch-finalization.json', finalization)
        return summary

    def _runtime_binding(self):
        scripts = Path(self.config['toolchain_root']) / 'scripts'
        names = ('native_timeline.py', 'native_subtitles.py', 'native_preview.py',
                 'native_register.py', 'local_asr.py', 'autocut_task.py', 'draft_quality.py',
                 'native_packaging.py', 'packaging_assets.py', 'native_audio_scan.py')
        model = Path(self.model_dir) if self.model_dir else None
        model_files = ('model.int8.onnx', 'tokens.txt')
        return {'schema_version': '1.0', 'model': (os.environ.get('DEEPSEEK_MODEL') if self.config['provider'].get('transport') != 'gateway' else None) or
                self.config['provider']['model'], 'install_dir': str(Path(
                    self.config.get('install_dir') or Path(__file__).resolve().parents[1]).resolve()),
                'tool_sha256': {name: digest(scripts / name) for name in names},
                'core_sha256': {name: digest(Path(__file__).with_name(name)) for name in
                    ('task_service.py', 'timeline_compiler.py', 'local_executor.py', 'media_normalize.py', 'video_geometry.py',
                     'quality_controller.py', 'deepseek.py', 'provider.py', 'montage.py', 'commerce.py', 'packaging.py', 'resource_policy.py', 'internal_fonts.py',
                     'native_workflow.py', 'native_resource_planner.py', 'native_packaging.py', 'editing_rules.py', 'market_rules.py',
                     'native_resource_catalog.py', 'native_cli.py', 'native_host.py', 'native_review_registration.py','file_io.py',
                     'native_narration.py', 'model_router.py', 'model_recovery.py', 'qwen_credentials.py', 'gateway_credentials.py',
                     'batch_model.py', 'master_draft.py', 'batch_export.py', 'analysis_cache.py', 'batch_quality.py',
                     'direct_renderer.py', 'independent_export.py', 'visual_presets.py', 'compound_transitions.py', 'caption_motion.py', 'caption_layout.py', 'subtitle_styles.py', 'auto_decorations.py', 'font_library.py', 'direct_resources.py', 'narration_timing.py', 'caption_policy.py', 'local_voice.py', 'local_voice_worker.py', 'direct_vision.py', 'direct_duration.py', 'ad_narration.py', 'sound_design.py', 'task_recovery.py', 'video_limits.py', 'long_video.py', 'original_montage.py', 'original_montage_render.py')},
                'media_library_sha256': {kind: digest(path) for kind in ('font-library','sticker-library','sound-library')
                    if (path := Path(__file__).resolve().parents[1]/'desktop-assets/media-library'/kind/'manifest.json').is_file()},
                'packaging_asset_sha256': {p.name:digest(p) for p in sorted((scripts.parent/'assets/packaging').glob('*')) if p.is_file()},
                'asr_model_sha256': ({name: digest(model / name) for name in model_files}
                    if model and all((model / name).is_file() for name in model_files) else None),
                'native_resource_profile':self.config.get('native_resources'),
                'narration_profile':self.config.get('narration'),
                'model_routing_profile': {k: self.config.get(k) for k in
                    ('provider', 'vision_provider', 'complex_vision_provider', 'visual_routing', 'limits')},
                'native_packaging_wrapper_sha256':digest(scripts/'native_internal_packaging.py')
                    if (scripts/'native_internal_packaging.py').is_file() else None,
                'native_review_registration_wrapper_sha256':digest(scripts/'native_review_register.py')
                    if (scripts/'native_review_register.py').is_file() else None,
                'agent_contract_version': 'native-internal-resources-v5'}

    def get_job(self, job_id):
        return self.store.get_job(job_id)

    def cancel_job(self, job_id):
        return self.store.request_cancel(job_id)

    def resume_job(self, job_id):
        job = self.store.get_job(job_id)
        if job['state'] in ('COMPLETED', 'READY_FOR_REVIEW'):
            raise ValueError('Review-ready or completed jobs cannot be resumed')
        for asset_id in job['task_spec']['asset_ids']:
            self._check_asset(self.store.get_asset(asset_id))
        binding = job['task_spec'].get('runtime_binding')
        if binding is not None and binding != self._runtime_binding():
            raise ValueError('Model, installation or tool version changed; create a new task')
        self.store.clear_cancel(job_id)
        return self.start_job(job_id, resume=True)

    def upgrade_failed_job_runtime(self,job_id,*,io_only=False,narration_only=False):
        """Explicit code-repair migration; completed output is never relabelled.

        Asset/artifact hashes and saved native checkpoints are revalidated;
        resume must still pass the ordinary compiler, native reload and review.
        """
        with draft_lock(self.store.job_dir(job_id)/'.job-run.lock'):
            job=self.get_job(job_id)
            if job['state']!='FAILED':raise ValueError('Only failed jobs can migrate after a code repair')
            folder=self.store.job_dir(job_id)
            if (folder/'delivery.json').exists():raise ValueError('A delivered revision needs a new task')
            old=job['task_spec'].get('runtime_binding');new=self._runtime_binding()
            if not old:raise ValueError('Missing original runtime binding')
            changed={k for k in set(old)|set(new) if old.get(k)!=new.get(k)}
            if not changed <= {'core_sha256','tool_sha256'}:
                raise ValueError('Model, install, assets or runtime profile migration requires a new task')
            if io_only and narration_only:raise ValueError('Choose one repair scope')
            if io_only or narration_only:
                core_changes={name for name in set(old['core_sha256'])|set(new['core_sha256'])
                    if old['core_sha256'].get(name)!=new['core_sha256'].get(name)}
                allowed=({'native_narration.py','native_cli.py','native_diagnostics.py','task_service.py'} if narration_only
                    else {'file_io.py','native_review_registration.py','task_service.py'})
                if changed-{'core_sha256'} or not core_changes <= allowed:
                    raise ValueError('IO-only repair cannot preserve checkpoints for changed editing or model code')
                voice = folder/'voice-revision'
                unfinished_voice = ((voice/'tts-receipts.json').is_file() or
                                    any(voice.glob('speech-*.intent.json')))
                complete_voice = False
                report_path = voice/'narration-report.json'
                if report_path.exists():
                    try:
                        report_data=json.loads(report_path.read_text(encoding='utf-8'))
                        script_data=json.loads((voice/'new-script.json').read_text(encoding='utf-8'))
                        complete_voice=(report_data.get('voice_count') == len(script_data.get('sentences', [])))
                    except (OSError, ValueError, TypeError):
                        complete_voice=True
                if narration_only and (not unfinished_voice or
                        complete_voice):
                    raise ValueError('Narration repair requires unfinished synthesized voice checkpoints')
            for aid in job['task_spec']['asset_ids']:self._check_asset(self.store.get_asset(aid))
            for row in self.store.list_artifacts(job_id):
                if digest(Path(row['path']))!=row['sha256']:raise ValueError('Cached artifact changed before upgrade')
            for path in folder.glob('n*/journal.json'):
                journal=json.loads(path.read_text(encoding='utf-8'))
                for step in journal.get('steps',[]):
                    if digest(Path(step['draft'])/'draft_content.json')!=step['sha256']:
                        raise ValueError('Native checkpoint changed before upgrade')
            record=folder/'runtime-upgrades'/f'{len(list((folder/"runtime-upgrades").glob("*.json")))+1:03d}.json'
            atomic_json(record,{'old_binding':old,'new_binding':new,'cached_artifacts_hash_checked':True,
                'completed_quality_not_inferred':True,'io_only_preserved_checkpoints':io_only,
                'narration_finalization_repair':narration_only})
            if changed and not (io_only or narration_only):
                import shutil
                invalidated=[]
                derived={'packaging_snapshot','packaging_report','packaged_draft','subtitled_draft',
                    'readback_report','model_review','audio_signal_report','delivery'}
                for row in self.store.list_artifacts(job_id):
                    if row['kind'] not in derived:continue
                    path=Path(row['path'])
                    if path.suffix.lower()=='.json' and path.name!='draft_content.json' and row['kind']!='audio_signal_report':
                        if not path.is_relative_to(folder):raise ValueError('Derived report leaves job')
                        backup=record.with_suffix('')/path.relative_to(folder)
                        backup.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,backup)
                        path.unlink()  # Regenerable report; exact old bytes are retained above.
                    invalidated.append(row['artifact_id'])
                with self.store._connect() as db:
                    db.executemany('DELETE FROM artifacts WHERE artifact_id=?',[(a,) for a in invalidated])
                    db.execute("UPDATE operations SET status='failed' WHERE job_id=? AND stage='package_internal' AND status='completed'",(job_id,))
                atomic_json(record.with_suffix('')/'invalidated-reports.json',{'artifact_ids':invalidated,
                    'native_media_and_checkpoints_preserved':True})
            spec={**job['task_spec'],'runtime_binding':new}
            with self.store._connect() as db:
                db.execute('UPDATE jobs SET spec_json=? WHERE job_id=? AND state=?',
                    (json.dumps(spec,ensure_ascii=False),job_id,'FAILED'))
            self.store.event(job_id,'FAILED','progress',{'operation':'runtime_upgrade','message':'修复后升级运行时，保留已核验检查点并重新执行原生验收',
                'record':str(record),'sha256':digest(record)})
            return self.get_job(job_id)

    def list_artifacts(self, job_id):
        return self.store.list_artifacts(job_id)

    def list_artifacts_safe(self, job_id):
        return self.store.list_artifacts_safe(job_id)

    def publish_job(self, job_id):
        """Register a checked draft in the user's homepage without opening the GUI."""
        with draft_lock(self.store.job_dir(job_id) / '.job-run.lock'):
            job = self.get_job(job_id)
            if job['state'] != 'READY_FOR_REVIEW':
                raise ValueError('Only a review-ready draft can be published')
            from .native_workflow import require_profile
            require_profile(self,job_id)
            binding = job['task_spec'].get('runtime_binding')
            if binding is not None and binding != self._runtime_binding():
                raise ValueError('Runtime changed since draft creation; publish with matching tools')
            folder = self.store.job_dir(job_id)
            delivery_artifact = next((row for row in reversed(self.store.list_artifacts(job_id))
                                      if row['kind'] == 'delivery'), None)
            if delivery_artifact is None:
                raise ValueError('No verified delivery report is available')
            delivery = json.loads(Path(delivery_artifact['path']).read_text(encoding='utf-8-sig'))
            revision_id = delivery_artifact['revision_id']
            if not delivery.get('draft_readback_passed') or not delivery.get('jianying_native_preview_checked'):
                raise ValueError('Draft readback and native preview must pass before publication')
            draft = Path(delivery['draft_path']).resolve(strict=True)
            if not draft.is_relative_to(folder):
                raise ValueError('Draft path is outside the job workspace')
            content = draft / 'draft_content.json'
            if not content.is_file():
                raise ValueError('Native draft content is missing')
            expected_kind = ('packaged_draft' if delivery.get('editable_layers_verified') else
                             'subtitled_draft' if delivery['subtitle_mode'] == 'editable_native'
                             else 'native_draft')
            verified_draft = self.store.artifact(job_id, expected_kind, revision_id)
            if verified_draft is None or Path(verified_draft['path']) != content:
                raise ValueError('Delivery draft differs from checked draft artifact')
            packaged=self.store.artifact(job_id,'packaging_report',revision_id)
            if packaged is None:raise ResourcePolicyBlocked('local_review_missing_native_resource_evidence')
            packaged= json.loads(Path(packaged['path']).read_text(encoding='utf-8-sig'))
            if packaged.get('schema')!='jianying-native-task-packaging-v1':
                raise ResourcePolicyBlocked('local_review_legacy_resource_report_rejected')
            native_report=Path(packaged['native_report']).resolve(strict=True)
            if not native_report.is_relative_to(folder):raise ValueError('Native resource proof leaves job')
            from .native_packaging import validate_completed
            native=validate_completed(native_report.parent)
            voice_revision=delivery.get('new_narration_revision')
            if not native['source_preserved'] or (not voice_revision and Path(native['draft']).resolve()!=draft):
                raise ValueError('Native resource proof refers to another draft')
            if voice_revision:
                from .native_narration import validate_revision
                voice_path=Path(voice_revision).resolve(strict=True)
                if not voice_path.is_relative_to(folder):raise ValueError('Voice evidence leaves job')
                voice=validate_revision(voice_path)
                if Path(voice['draft']).resolve()!=draft:raise ValueError('Voice draft differs from delivery')
            root = Path(self.config.get('draft_root', ''))
            user_data = Path(self.config.get('user_data_dir') or
                             Path(os.environ.get('LOCALAPPDATA', '')) / 'JianyingPro/User Data')
            if not root.is_dir() or not user_data.is_dir():
                raise ValueError('Configure existing draft_root and user_data_dir before publication')
            output = folder / 'publication' / ('registered-' + revision_id)
            name = 'Agent_' + job_id
            if job['task_spec']['workflow'] in ('product_ad', 'custom', 'crossborder_montage'):
                title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', delivery['draft_name']).strip(' .')[:40]
                name = title + '_' + job_id[-6:]
            executor = self.executor_factory(job_id)
            self.store.event(job_id, 'PUBLISHING', 'progress',
                             {'message': '将已读回草稿登记到剪映首页索引'})
            if voice_revision:
                from .native_narration import register_revision
                report=register_revision(self.config,voice_path,name)
                output=voice_path/'publication'
            else:
                report = JianyingAdapter(executor).register_review(native_report.parent, root, user_data, name, output)
            registration = report.get('registration') or {}
            if (report.get('status') != 'native_draft_registered' or
                    not registration.get('index_readback_equal') or
                    not registration.get('existing_entries_preserved')):
                raise ValueError('Native registration did not verify the homepage index')
            self.store.put_artifact(job_id, 'registration_report', revision_id,
                                    output / 'registration-report.json', verified=True)
            summary = {'schema_version': '1.0', 'job_id': job_id,
                       'registered_draft_path': report['destination'],
                       'homepage_index_registered': True,
                       'homepage_index_readback_passed': True,
                       'local_review_only':True,'self_contained_dependencies':report['self_contained_dependencies'],
                       'final_export_created':False,'full_quality_accepted':False,
                       'jianying_open_verified': False}
            summary_path = folder / 'publication' / (revision_id + '-summary.json')
            atomic_json(summary_path, summary)
            self.store.put_artifact(job_id, 'publication_summary', revision_id,
                                    summary_path, verified=True)
            return summary

    def get_artifact(self, artifact_id):
        return self.store.get_artifact(artifact_id)

    def read_events(self, job_id, after_sequence=0):
        return self.store.read_events(job_id, after_sequence)

    def _check_asset(self, asset):
        if asset.get('video_duration_us', asset.get('duration_us', 0)) > MAX_SOURCE_US:
            raise ValueError('单条素材最长支持 7 分钟，请分段后再添加')
        path = Path(asset['path']).resolve(strict=True)
        if digest(path) != asset['sha256']:
            raise ValueError('Registered source changed; create a new asset and task')
        return asset

    def _json_stage(self, job_id, kind, path, producer, *, revision='revision_001', verified=False):
        existing = self.store.artifact(job_id, kind, revision)
        if existing:
            return json.loads(Path(existing['path']).read_text(encoding='utf-8-sig'))
        if not path.is_file():
            value = producer()
            atomic_json(path, value)
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if value.get('schema_version') != '1.0':
            raise ValueError(kind + ' has no valid schema_version')
        self.store.put_artifact(job_id, kind, revision, path, verified=verified)
        return value

    def _transcribe(self, job_id, asset, executor, *, multi=False, language='zh'):
        folder = self.store.job_dir(job_id)
        kind = 'transcript'
        if multi:
            folder = folder / 'sources' / asset['asset_id']
            kind += '_' + asset['asset_id']
        path = folder / 'transcript.json'
        if path.is_file():
            result=self._json_stage(job_id, kind, path, lambda: None)
            if result.get('asset_id')!=asset['asset_id'] or result.get('source_sha256')!=asset['sha256']:
                raise ValueError('Cached transcript belongs to a different source')
            return result
        cache = AnalysisCache(self.store.root)
        model_identity = str(self.model_dir or '')
        cache_key = cache.key(source_sha256=asset['sha256'], language=language,
                              engine='local_sensevoice', model_identity=model_identity)
        cached = cache.read('transcript', cache_key)
        if cached and cached.get('asset_id') == asset['asset_id'] and cached.get('source_sha256') == asset['sha256']:
            prior_raw=cached.get('raw_path')
            if not cached.get('timed_tokens') and prior_raw and Path(prior_raw).is_file():
                raw=json.loads(Path(prior_raw).read_text(encoding='utf-8-sig'))
                cached={**cached,'timed_tokens':{k:raw.get(k,[]) for k in ('tokens','timestamps')}}
            cached = {**cached, 'raw_path': None, 'cache_hit': True}
            return self._json_stage(job_id, kind, path, lambda: cached)
        if multi and not asset['has_audio']:
            result = {
                'schema_version': '1.0', 'asset_id': asset['asset_id'], 'engine': 'no_audio_stream',
                'source_sha256': asset['sha256'], 'cues': [], 'proofreading_required': False,
                'cache_hit': False}
            cache.write('transcript', cache_key, result)
            return self._json_stage(job_id, kind, path, lambda: result)
        if not self.model_dir or not Path(self.model_dir).is_dir():
            raise FileNotFoundError('Local ASR model directory is not configured or missing')
        if not asset['has_audio'] or asset['duration_us'] > MAX_SOURCE_US:
            raise ValueError('本地语音识别需要有声音的素材，单条最长 7 分钟')
        output = folder / 'asr'
        executor.run(kind, 'local_asr.py', ['--input', asset['path'],
            '--model-dir', self.model_dir, '--out', output, '--fps', 30,
            '--language', language, *(['--allow-empty'] if multi else [])],
            output_dir=output, report_file=output / 'asr-report.json', timeout_seconds=1800)
        cues = json.loads((output / 'captions.json').read_text(encoding='utf-8-sig'))
        result = {'schema_version': '1.0', 'asset_id': asset['asset_id'], 'engine': 'local_sensevoice',
                  'source_sha256': asset['sha256'], 'cues': cues, 'raw_path': str(output / 'raw-asr.json'),
                  'proofreading_required': True, 'word_alignment_verified': False}
        raw = json.loads((output / 'raw-asr.json').read_text(encoding='utf-8-sig'))
        result['timed_tokens'] = {k:raw.get(k,[]) for k in ('tokens','timestamps')}
        result['language'] = language
        result['cache_hit'] = False
        cache.write('transcript', cache_key, result)
        return self._json_stage(job_id, kind, path, lambda: result)

    def _sample_direct(self, job_id, asset, instruction, *, multi=False, count=8):
        """Extract immutable PNG evidence with the standalone FFmpeg toolchain.

        Direct jobs must never instantiate ``ToolHub`` because that adapter is
        intentionally tied to the legacy Jianying installation.  The source
        file is read only; each frame is written below the job checkpoint and
        hashed before it is offered to DeepSeek.
        """
        folder = self.store.job_dir(job_id)
        kind = 'frame_evidence'
        if multi:
            folder = folder / 'sources' / asset['asset_id']
            kind += '_' + asset['asset_id']
        path = folder / 'frame-evidence.json'

        def produce():
            from .independent_export import discover_tools
            from .montage import sample_positions
            tools = discover_tools(self.config.get('direct_ffmpeg_dir'))
            ffmpeg = tools.get('ffmpeg')
            if not ffmpeg:
                raise FileNotFoundError('未找到 FFmpeg，无法提取直接模式的画面证据')
            duration = int(asset['video_duration_us'])
            positions = (sample_positions(duration, count=count) if multi else
                         sorted({min(duration - 1, duration * index // 6)
                                 for index in range(6)}))
            frame_dir = folder / 'direct-frames'
            frame_dir.mkdir(parents=True, exist_ok=True)
            frames = []
            for index, position in enumerate(positions, 1):
                output = frame_dir / f'frame-{index:03d}.png'
                self._extract_direct_frame(ffmpeg, asset['path'], int(position), output)
                frames.append({'path': str(output), 'requested_us': int(position),
                               'native_pts_us': int(position), 'sha256': digest(output),
                               'width': 360, 'height': None, 'asset_id': asset['asset_id'],
                               'source_duration_us': duration})
            return {'schema_version': '1.0', 'asset_id': asset['asset_id'],
                    'sampling': (f'{count}_direct_ffmpeg_stills_including_tail' if multi else
                                 'six_direct_ffmpeg_stills_across_full_source'),
                    'frames': frames, 'continuous_video_reviewed': False,
                    'audio_reviewed': False, 'adapter': 'direct_ffmpeg'}

        result = self._json_stage(job_id, kind, path, produce)
        for frame in result['frames']:
            if digest(Path(frame['path'])) != frame['sha256']:
                raise ValueError('Direct source frame changed')
        return result

    @staticmethod
    def _extract_direct_frame(ffmpeg, source, position_us, output):
        """Extract one frame with bounded retry and a seek fallback.

        Some H.264 files intermittently return exit code 0 without producing a
        frame when fast seeking is used before ``-i``. Keep the evidence
        contract strict, but retry the same immutable source and then use an
        accurate input seek. A temporary output prevents a partial PNG from
        being mistaken for valid evidence after a failed invocation.
        """
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        seconds = f'{position_us / 1_000_000:.6f}'
        attempts = (
            ('fast_seek', ['-ss', seconds, '-i', str(source)]),
            ('accurate_seek', ['-i', str(source), '-ss', seconds]),
            ('fast_seek_retry', ['-ss', seconds, '-i', str(source)]),
        )
        failures = []
        for label, seek_args in attempts:
            # Keep the image suffix so FFmpeg selects the PNG muxer; the
            # temporary marker remains before the final extension.
            temporary = output.with_name(output.stem + '.' + uuid.uuid4().hex + '.tmp' + output.suffix)
            command = [ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
                       *seek_args, '-an', '-frames:v', '1', '-vf', 'scale=360:-2',
                       str(temporary)]
            try:
                result = subprocess.run(command, capture_output=True, text=True,
                                        encoding='utf-8', errors='replace', timeout=120,
                                        stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                detail = (result.stderr or '').strip()[-1000:]
                if result.returncode == 0 and temporary.is_file() and temporary.stat().st_size > 0:
                    temporary.replace(output)
                    return
                failures.append({'method': label, 'returncode': result.returncode,
                                 'detail': detail or 'FFmpeg 未生成画面证据'})
            except subprocess.TimeoutExpired:
                failures.append({'method': label, 'returncode': 'timeout',
                                 'detail': 'FFmpeg 抽帧超时'})
            finally:
                if temporary.exists():
                    temporary.unlink()
        detail = '; '.join(f"{row['method']}: {row['detail']}" for row in failures)
        raise RuntimeError(f'直接画面抽帧失败（{position_us}us），已尝试{len(attempts)}次：{detail}')

    def _sample(self, job_id, asset, instruction, *, multi=False, count=8, direct=False):
        folder = self.store.job_dir(job_id)
        kind = 'frame_evidence'
        if multi:
            folder = folder / 'sources' / asset['asset_id']
            kind += '_' + asset['asset_id']
        path = folder / 'frame-evidence.json'
        if direct:
            return self._sample_direct(job_id, asset, instruction, multi=multi, count=count)

        def produce():
            from .tools import ToolHub
            hub = ToolHub(self.config['toolchain_root'], self.config['install_dir'],
                          folder / 'source-analysis', [Path(asset['path'])], instruction)
            inventory = hub.execute('inventory', {})
            if not inventory.get('ok') or len(inventory['sources']) != 1:
                raise ValueError('Native source inventory failed: ' + str(inventory.get('error')))
            source_id = inventory['sources'][0]['id']
            duration = asset['video_duration_us']
            from .montage import sample_positions
            positions = (sample_positions(duration, count=count) if multi else
                         sorted({min(duration - 1, duration * index // 6) for index in range(6)}))
            result = hub.execute('sample_frames', {'source_id': source_id,
                                                   'times_us': positions, 'width': 360})
            if not result.get('ok'):
                raise ValueError('Native frame sampling failed: ' + str(result.get('error')))
            return {'schema_version': '1.0', 'asset_id': asset['asset_id'],
                    'sampling': f'{count}_native_stills_including_tail' if multi else 'six_native_stills_across_full_source',
                    'frames': [{**frame, 'asset_id': asset['asset_id'],
                                'source_duration_us': asset['video_duration_us']} for frame in result['frames']],
                    'continuous_video_reviewed': False, 'audio_reviewed': False}
        result = self._json_stage(job_id, kind, path, produce)
        for frame in result['frames']:
            if digest(Path(frame['path'])) != frame['sha256']:
                raise ValueError('Native source frame changed')
            frame['source_duration_us'] = asset['video_duration_us']
        return result

    def _plan(self, job_id, task, candidates, frames, provider):
        folder = self.store.job_dir(job_id)
        revision_dir = folder / 'revisions' / '001'; revision_dir.mkdir(parents=True, exist_ok=True)
        path = revision_dir / 'edit_plan.json'
        previous_issue = None
        for attempt in range(task['max_revision_rounds'] + 1):
            if path.is_file():
                plan = self._json_stage(job_id, 'edit_plan', path, lambda: None)
            else:
                try:
                    plan = provider.plan(task, candidates, frames, previous_issue=previous_issue)
                except ModelOutputError as exc:
                    previous_issue = str(exc)
                    self.store.event(job_id, 'PLANNING', 'progress',
                        {'message': '模型输出无法解析，有限重试', 'attempt': attempt + 1,
                         'issue': previous_issue})
                    if attempt >= task['max_revision_rounds']:
                        raise
                    continue
                plan['revision_id'] = 'revision_001'
                proposal_dir = revision_dir / 'proposals'
                atomic_json(proposal_dir / f'attempt-{attempt + 1:02d}.json', plan)
            try:
                selected = validate_edit_plan(plan, candidates, task['target'])
                constraints = task['constraints']
                selected_text = ''.join(row['text'] for row in selected)
                if any(phrase not in selected_text for phrase in constraints['must_keep']):
                    raise PlanIssue('Plan omits a must_keep phrase from transcript evidence')
                if any(phrase in selected_text for phrase in constraints['must_remove']):
                    raise PlanIssue('Plan includes a must_remove phrase from transcript evidence')
                if not constraints['allow_reorder']:
                    starts = [row['start_us'] for row in selected]
                    if starts != sorted(starts):
                        raise PlanIssue('Plan reorders clips despite allow_reorder=false')
                visual = plan.get('visual_assessment')
                if not isinstance(visual, dict) or visual.get('burned_captions') not in ('present', 'absent', 'uncertain'):
                    raise PlanIssue('EditPlan needs burned_captions=present|absent|uncertain visual assessment')
                sampled = {frame['requested_us'] for frame in frames}
                times = visual.get('evidence_frame_times_us')
                # Text-only fallback may plan from transcript evidence but is
                # not a visual observation. Never invent citations for it.
                unavailable = (plan.get('_input_evidence', {}).get('images') ==
                               'rejected_by_provider' and
                               visual['burned_captions'] == 'uncertain' and times == [])
                if (not isinstance(times, list) or
                        (not times and not unavailable and not sampled) or
                        any(type(t) is not int or t not in sampled for t in times)):
                    raise PlanIssue('Visual assessment must cite actual sampled source frame times')
                if not times and not unavailable:
                    ordered = sorted(sampled)
                    times = sorted({ordered[0], ordered[len(ordered)//2], ordered[-1]})
                    visual = {**visual, 'evidence_frame_times_us': times,
                              'evidence_repair': 'filled_from_actual_sampled_source_frames'}
                    plan['visual_assessment'] = visual
                    self.store.event(job_id, 'PLANNING', 'progress', {
                        'message': '模型遗漏视觉证据时间，已用本任务实际采样帧补齐',
                        'original_times': [], 'repaired_times': times,
                        'visual_review_verified': True})
                if unavailable:
                    self.store.event(job_id, 'PLANNING', 'progress', {
                        'message': '模型接口拒收图片：按转写继续，画面与烧录字幕待人工核验',
                        'visual_review_verified': False})
                if visual['burned_captions'] == 'absent' and (
                    len(set(times)) < min(3, len(sampled)) or
                    min(times) > min(sampled) or max(times) < max(sampled)):
                    raise PlanIssue('Declaring no burned captions requires first/middle/last sampled frame coverage')
                if not path.is_file():
                    atomic_json(path, plan)
                self.store.put_artifact(job_id, 'edit_plan', 'revision_001', path)
                return plan
            except PlanIssue as exc:
                previous_issue = str(exc)
                self.store.event(job_id, 'PLANNING', 'progress',
                    {'message': '模型计划未通过本地校验，要求修订', 'attempt': attempt + 1,
                     'issue': previous_issue})
                if path.is_file() or attempt >= task['max_revision_rounds']:
                    raise
        raise RuntimeError('Planner exhausted revision budget')

    def start_job(self, job_id, *, resume=False):
        # A killed CLI releases the OS lock; a second live worker cannot replay
        # model calls or native draft writes for the same job concurrently.
        with draft_lock(self.store.job_dir(job_id) / '.job-run.lock'):
            return self._run_job(job_id, resume=resume)

    def _package_native_revision(self, job_id, task, provider, adapter, draft, out, timeline,
                                 evidence, *, previous=None):
        """Shared task stage; caller holds the job execution lock."""
        from .native_workflow import package_draft
        return package_draft(self,job_id,task,provider,adapter,draft,out,timeline,evidence,previous=previous)

    def _prepare_montage(self, job_id, task, assets, executor):
        from .montage import montage_candidates
        candidates, frames, transcripts = [], [], {}
        for index, asset in enumerate(assets, 1):
            if self.get_job(job_id)['cancel_requested']:
                raise ToolCancelled('Montage analysis cancelled')
            self.store.event(job_id, 'ANALYZING', 'progress', {
                'message': '分析多素材的原语言声音和原生画面', 'completed': index - 1,
                'total': len(assets), 'asset_id': asset['asset_id']})
            transcript = self._transcribe(job_id, asset, executor, multi=True,
                                          language=task.get('transcription_language', 'auto'))
            transcripts[asset['asset_id']] = transcript
            asset_frames = self._sample(job_id, asset, task['instruction'], multi=True,
                                        direct=task.get('render_backend') == 'direct_ffmpeg')['frames']
            asset_candidates = montage_candidates(asset, transcript, task['target']['fps'])
            # VideoHighlighter-inspired scores are advisory metadata.  The
            # existing task rules and planner still decide what is selected.
            candidates.extend(annotate_candidates(asset_candidates, frames=asset_frames,
                                                  transcript=transcript))
            frames.extend(asset_frames)
        evidence = self._json_stage(job_id, 'evidence', self.store.job_dir(job_id) / 'evidence.json',
            lambda: {'schema_version': '1.0', 'segments': candidates, 'asset_ids': task['asset_ids']})
        return evidence['segments'], frames, transcripts

    def _prepare_direct_ad_sources(self, job_id, task, assets, executor, provider):
        """Prepare the evidence contract used by the model's ad planner.

        This is the native-independent counterpart of ``commerce.run_ad``:
        it performs local ASR/frame sampling and leaves all rendering to the
        direct backend.
        """
        from .commerce import editing_units
        sources = []
        for asset in assets:
            if self.get_job(job_id)['cancel_requested']:
                raise ToolCancelled('Direct ad analysis cancelled')
            transcript = self._transcribe(job_id, asset, executor, multi=True,
                                          language=task.get('transcription_language', 'auto'))
            # Dense stills are required for silent/visual-only footage. They
            # do not claim continuous video understanding, but they give each
            # short visual unit evidence on both sides of a possible cut.
            from .original_montage import active as original_montage
            sample_count = (min(108,max(8,(asset['video_duration_us']+3_999_999)//4_000_000))
                            if original_montage(task) else 8 if task.get('creation_mode') == 'long_video_v1' else 20)
            frames = self._sample(job_id, asset, task['instruction'], multi=True, count=sample_count,
                                  direct=True)['frames']
            units, speech_mode = editing_units(task, asset, transcript, frames)
            if not units:
                raise PlanIssue('素材太短，无法建立直接成片镜头窗口')
            observations = []
            observation_frames = ([frames[round(i*(len(frames)-1)/7)] for i in range(8)]
                                  if original_montage(task) and len(frames)>8 else frames)
            observation_batch = 8 if task.get('creation_mode') == 'long_video_v1' else 4
            for offset in range(0, len(observation_frames), observation_batch):
                if self.get_job(job_id)['cancel_requested']:
                    raise ToolCancelled('Direct visual analysis cancelled')
                batch = offset // observation_batch + 1
                aid = asset['asset_id']
                self.store.event(job_id, 'ANALYZING', 'progress', {
                    'message': f'DeepSeek 识别实际画面：素材 {aid}，第 {batch} 组',
                    'asset_id': aid, 'frame_count':len(observation_frames[offset:offset+observation_batch])})
                observations.append(self._json_stage(job_id, f'visual_{aid}_{batch:02d}',
                    self.store.job_dir(job_id) / 'sources' / aid / f'visual-observations-{batch:02d}.json',
                    lambda chunk=observation_frames[offset:offset+observation_batch]: cached_analyze_frames(
                        self.store.root, provider, asset, chunk), verified=True))
            if not any(row.get('subject', '').strip() and row.get('confidence') in ('high','medium')
                       for report in observations for row in report['frames']):
                raise VisualEvidencePending('已读取图片，但无法可靠辨认素材主体；请补充清晰素材或主体说明')
            units = annotate_candidates(units, observations=observations, frames=frames,
                                        transcript=transcript)
            sources.append({'asset_id': asset['asset_id'], 'speech_units': units,
                            'speech_mode': speech_mode, 'frames': frames,
                            'transcript': transcript, 'observations': observations,
                            'video_duration_us': asset.get('video_duration_us'),
                            'audio_duration_us': asset.get('audio_duration_us'),
                            'has_audio': bool(asset.get('has_audio'))})
        return sources

    def _plan_direct_ad(self, job_id, task, assets, sources, provider):
        task = {**task, '_script_repair_ledger':str(self.store.job_dir(job_id)/'auto-narration/repair-budget.json')}
        from .commerce import (compile_ad, plan_ad, plan_feedback, repair_direct_ad_duration,
                               NarrationPlanIssue, rewrite_direct_voiceover)
        from .ad_narration import repair_heading_language
        folder = self.store.job_dir(job_id)
        from .original_montage import active as original_montage
        if original_montage(task):
            task={**task,'_original_montage_seed':job_id}
        path = folder / 'revisions/001/ad-plan.json'
        checkpoint = self._narration_checkpoint(job_id)
        if checkpoint:
            # Delivery resumes the latest revision, including unresolved review
            # feedback. Never re-review the obsolete original and lose fixes.
            from .ad_narration import normalize_plan
            plan = normalize_plan(task, checkpoint['plan'])
            try:
                plan = repair_heading_language(provider, task, plan, NarrationPlanIssue)
                timeline, candidates, edit_plan = compile_ad(task, assets, sources, plan)
            except NarrationPlanIssue as exc:
                if not exc.timeline:
                    raise
                try:
                    plan, report = rewrite_direct_voiceover(provider, task, sources, plan,
                                                            exc.timeline, str(exc))
                    plan = repair_heading_language(provider, task, plan, NarrationPlanIssue)
                    timeline, candidates, edit_plan = compile_ad(task, assets, sources, plan)
                except NarrationPlanIssue:
                    alternative = self._factual_narration_repair(job_id, task, assets, sources,
                        plan, exc.timeline, provider, measurement=checkpoint.get('measurement'))
                    if not alternative:
                        raise
                    plan, timeline = alternative
                    plan = repair_heading_language(provider, task, plan, NarrationPlanIssue)
                    timeline, candidates, edit_plan = compile_ad(task, assets, sources, plan)
            if plan != checkpoint['plan']:
                path = folder/'auto-narration'/('normalized-'+uuid.uuid4().hex+'.json')
                atomic_json(path, {**checkpoint, 'plan':plan, 'approved':False,
                    'reason':'旧旁白已恢复为完整文案，等待重新审核和实测配音'})
                self.store.put_artifact(job_id, 'ad_auto_pending', path.stem, path)
                checkpoint = {**checkpoint, 'approved':False}
            if checkpoint.get('approved'):
                timeline['ad_content_review'] = checkpoint['review']
            return plan, timeline, candidates, edit_plan
        proposal_dir = folder / 'revisions/001/proposals' / uuid.uuid4().hex
        pending_plan = None
        pending_dir = folder / 'revisions/001/script-pending'
        if pending_dir.is_dir() and not path.is_file():
            saved = [a for a in self.store.list_artifacts(job_id) if a['kind'] == 'ad_script_pending']
            if saved:
                # Artifacts and evidence retain strict hashes; this is an
                # unapproved proposal, never an accepted plan or render input.
                pending_plan = json.loads(Path(saved[-1]['path']).read_text('utf-8'))['plan']
        previous = None
        for attempt in range(task['max_revision_rounds'] + 1):
            plan = None
            try:
                # Read through the artifact store so historical fingerprints
                # remain enforced. Invalid old checkpoints require a new job.
                if path.is_file():
                    plan = self._json_stage(job_id, 'ad_plan', path, lambda: None)
                elif pending_plan is not None:
                    plan, pending_plan = pending_plan, None
                else:
                    if (previous and 'outside target' in previous.get('validation_error', '')
                            and task.get('narration_mode') == 'new_native'
                            and task.get('workflow') != 'custom'):
                        from .direct_duration import plan_duration_bound_ad
                        plan = plan_duration_bound_ad(provider, task, sources, previous)
                    if plan is None:
                        plan = plan_ad(provider, task, sources, previous=previous)
                atomic_json(proposal_dir / f'attempt-{attempt+1:02d}.json', plan)
                # DeepSeek sometimes rounds an evidence timestamp to a point
                # between two sampled stills.  Keep the integrity contract
                # strict by snapping only to an actually extracted frame when
                # the rounded value is within half the local sampling gap.
                # Values farther away remain invalid and are rejected below.
                normalized = []
                by_id = {source['asset_id']: source for source in sources}
                for clip_index, clip in enumerate(plan.get('clips', []), 1):
                    source = by_id.get(clip.get('asset_id'))
                    known = sorted({frame['requested_us'] for frame in (source or {}).get('frames', [])
                                    if type(frame.get('requested_us')) is int})
                    if len(known) < 2 or not isinstance(clip.get('frame_times_us'), list):
                        continue
                    gaps = [right - left for left, right in zip(known, known[1:])]
                    tolerance = max(1_000, max(gaps) // 2)
                    repaired = []
                    changed = []
                    for value in clip['frame_times_us']:
                        if type(value) is not int:
                            repaired.append(value)
                            continue
                        nearest = min(known, key=lambda item: abs(item - value))
                        if nearest != value and abs(nearest - value) <= tolerance:
                            repaired.append(nearest)
                            changed.append({'requested_us': value, 'aligned_us': nearest})
                        else:
                            repaired.append(value)
                    if changed:
                        plan['clips'][clip_index - 1] = {**clip, 'frame_times_us': repaired}
                        normalized.append({'clip_index': clip_index, 'asset_id': clip.get('asset_id'),
                                            'tolerance_us': tolerance, 'changes': changed})
                if normalized:
                    report = folder / 'revisions/001' / f'frame-evidence-normalization-{attempt + 1:02d}.json'
                    atomic_json(report, {'schema_version': '1.0', 'source': 'model_plan',
                                         'strict_snap_only': True, 'clips': normalized})
                    self.store.event(job_id, 'PLANNING', 'progress',
                                     {'message': '将模型近似帧时间对齐到已提取证据帧',
                                      'clips': normalized, 'report_path': str(report)})
                for voice_attempt in range(3):
                    try:
                        from .ad_narration import normalize_plan
                        plan = normalize_plan(task, plan)
                        plan = repair_heading_language(provider, task, plan, NarrationPlanIssue)
                        timeline, candidates, edit_plan = compile_ad(task, assets, sources, plan)
                        break
                    except NarrationPlanIssue as voice_issue:
                        if path.is_file() or voice_attempt == 2 or not voice_issue.timeline:
                            raise
                        self.store.event(job_id, 'PLANNING', 'progress', {
                            'message':'保留镜头位置，按实际时长局部修订口播', 'issue':str(voice_issue)})
                        plan, voice_report = rewrite_direct_voiceover(
                            provider, task, sources, plan, voice_issue.timeline, voice_issue)
                        atomic_json(proposal_dir / f'voice-repair-{attempt+1:02d}-{voice_attempt+1:02d}.json',
                                    {'plan':plan, **voice_report})
                if task.get('narration_mode') == 'new_native':
                    from .commerce import review_direct_script
                    for content_attempt in range(3):
                        review = review_direct_script(provider, task, sources, plan, timeline)
                        atomic_json(proposal_dir / f'content-review-{attempt+1:02d}-{content_attempt+1:02d}.json', review)
                        if review['decision'] == 'uncertain' and not review['issues']:
                            raise VisualEvidencePending('广告内容证据待确认：'+review['limits'])
                        if review['decision'] == 'accept':
                            break
                        if path.is_file() or content_attempt == 2:
                            alternative = self._factual_narration_repair(
                                job_id, task, assets, sources, plan, timeline, provider)
                            if alternative:
                                plan, timeline = alternative
                                review = timeline['ad_content_review']
                                plan = repair_heading_language(provider, task, plan, NarrationPlanIssue)
                                timeline, candidates, edit_plan = compile_ad(task, assets, sources, plan)
                                timeline['ad_content_review'] = review
                                return plan, timeline, candidates, edit_plan
                        if path.is_file() or content_attempt == 2:
                            raise PlanIssue('广告内容审核未通过：'+json.dumps(review['issues'], ensure_ascii=False))
                        self.store.event(job_id, 'PLANNING', 'progress', {
                            'message':'按内容审核意见局部修订文案，保留已校验镜头', 'issues':review['issues']})
                        plan, voice_report = rewrite_direct_voiceover(
                            provider, task, sources, plan, timeline, json.dumps(review,ensure_ascii=False))
                        atomic_json(proposal_dir / f'content-repair-{attempt+1:02d}-{content_attempt+1:02d}.json',
                                    {'plan':plan, **voice_report})
                        for length_attempt in range(3):
                            try:
                                plan = repair_heading_language(provider, task, plan, NarrationPlanIssue)
                                timeline, candidates, edit_plan = compile_ad(task, assets, sources, plan)
                                break
                            except NarrationPlanIssue as voice_issue:
                                if length_attempt == 2 or not voice_issue.timeline:
                                    raise
                                self.store.event(job_id, 'PLANNING', 'progress', {
                                    'message':'内容修订后仅精简超长句，保留镜头和其他已通过文案', 'issue':str(voice_issue)})
                                plan, voice_report = rewrite_direct_voiceover(
                                    provider, task, sources, plan, voice_issue.timeline, voice_issue)
                                atomic_json(proposal_dir / f'content-length-repair-{attempt+1:02d}-{content_attempt+1:02d}-{length_attempt+1:02d}.json',
                                            {'plan':plan, **voice_report})
                    timeline['ad_content_review'] = review
                if not path.is_file():
                    atomic_json(path, plan)
                self.store.put_artifact(job_id, 'ad_plan', 'revision_001', path)
                return plan, timeline, candidates, edit_plan
            except NarrationPlanIssue as exc:
                if path.is_file() or not isinstance(plan, dict) or not exc.timeline:
                    raise
                alternative = self._factual_narration_repair(job_id, task, assets, sources,
                                                            plan, exc.timeline, provider)
                if alternative:
                    plan, timeline = alternative
                    plan = repair_heading_language(provider, task, plan, NarrationPlanIssue)
                    timeline, candidates, edit_plan = compile_ad(task, assets, sources, plan)
                    checkpoint = self._narration_checkpoint(job_id)
                    timeline['ad_content_review'] = checkpoint['review']
                    return plan, timeline, candidates, edit_plan
                checkpoint = pending_dir / (uuid.uuid4().hex + '.json')
                atomic_json(checkpoint, {'schema_version':'1.0', 'plan':plan,
                    'reason':str(exc), 'approved':False, 'quality_downgraded':False,
                    'resume_condition':'继续时仅修订未通过的口播，再做完整内容审核'})
                self.store.put_artifact(job_id, 'ad_script_pending', checkpoint.stem, checkpoint)
                raise ScriptRevisionPending('口播待修订，已保留镜头与文案检查点：'+str(exc)) from exc
            except VisualEvidenceError as exc:
                raise VisualEvidencePending(str(exc)) from exc
            except (PlanIssue, ModelOutputError) as exc:
                if path.is_file():
                    raise PlanIssue('已有计划未通过新版内容检查，历史检查点已保留，请创建新任务：'+str(exc)) from exc
                previous = (plan_feedback(task, sources, plan, exc) if isinstance(plan, dict)
                            else {'validation_error': str(exc), 'rejected_plan': getattr(exc,'rejected_original_plan',None)})
                atomic_json(proposal_dir / f'feedback-{attempt+1:02d}.json', previous)
                self.store.event(job_id, 'PLANNING', 'progress',
                                 {'message': '直接广告计划未通过本地校验，返回模型修订',
                                  'attempt': attempt + 1, 'issue': str(exc)})
                if attempt >= task['max_revision_rounds']:
                    # A model can describe a plausible duration in ``story``
                    # while its selected visual units sum to a different
                    # measured duration.  Keep all rejected plans and use a
                    # bounded, evidence-only repair before failing the job.
                    if isinstance(plan, dict) and task.get('narration_mode') != 'new_native':
                        repaired = repair_direct_ad_duration(task, sources, plan)
                        if repaired is not None:
                            repaired_plan, repair_report = repaired
                            repair_path = folder / 'revisions/001' / 'local-duration-repair.json'
                            atomic_json(repair_path, {
                                'schema_version': '1.0',
                                'source_attempt': attempt + 1,
                                'validation_error': str(exc),
                                **repair_report,
                            })
                            try:
                                repaired_timeline, repaired_candidates, repaired_edit_plan = compile_ad(
                                    task, assets, sources, repaired_plan)
                            except (PlanIssue, ModelOutputError):
                                # The helper is deliberately conservative. If
                                # final compilation still rejects it, preserve
                                # the original failure and evidence.
                                self.store.event(job_id, 'PLANNING', 'progress', {
                                    'message': '本地时长修复未通过最终编译，保留原失败证据',
                                    'repair_path': str(repair_path)})
                            else:
                                atomic_json(proposal_dir / 'local-duration-repaired-plan.json', repaired_plan)
                                atomic_json(path, repaired_plan)
                                self.store.put_artifact(job_id, 'ad_plan', 'revision_001', path)
                                self.store.event(job_id, 'PLANNING', 'progress', {
                                    'message': '使用已验证视觉单位完成本地时长收敛',
                                    'repair_path': str(repair_path),
                                    'measured_total_ms': repair_report['measured_total_ms'],
                                    'target_ms': repair_report['target_ms'],
                                    'clip_count': repair_report['repaired_clip_count']})
                                return repaired_plan, repaired_timeline, repaired_candidates, repaired_edit_plan
                    raise


    def _plan_montage(self, job_id, task, assets, candidates, frames, transcripts, provider):
        from .montage import compile_montage
        folder = self.store.job_dir(job_id)
        observations = []
        for asset in assets:
            aid = asset['asset_id']
            self.store.event(job_id, 'PLANNING', 'progress',
                             {'message': 'DeepSeek 逐条理解真实画面及原语言转写', 'asset_id': aid})
            observations.append(self._json_stage(job_id, 'analysis_' + aid,
                folder / 'sources' / aid / 'model-analysis.json',
                lambda a=asset: provider.analyze_montage_source(task, a,
                    transcripts[a['asset_id']], [f for f in frames if f['asset_id'] == a['asset_id']])))
        path = folder / 'revisions/001/edit_plan.json'
        previous = None
        for attempt in range(task['max_revision_rounds'] + 1):
            cached = path.is_file()
            try:
                plan = (self._json_stage(job_id, 'edit_plan', path, lambda: None) if cached else
                        provider.plan_montage(task, candidates, observations, previous_issue=previous))
                plan['revision_id'] = 'revision_001'
                assessments = [row['burned_captions'] for row in observations]
                plan['visual_assessment'] = {
                    'burned_captions': 'present' if 'present' in assessments else
                                      'absent' if all(x == 'absent' for x in assessments) else 'uncertain',
                    'source_assessments': observations}
                # Compile before committing the plan so invalid durations can
                # be returned to the planner instead of failing after checkpoint.
                compile_montage(task, assets, candidates, plan)
                if not cached:
                    atomic_json(path, plan)
                self.store.put_artifact(job_id, 'edit_plan', 'revision_001', path)
                return plan
            except (PlanIssue, ModelOutputError) as exc:
                previous = str(exc)
                self.store.event(job_id, 'PLANNING', 'progress',
                    {'message': '多素材计划未通过本地校验，返回模型修订', 'attempt': attempt + 1,
                     'issue': previous})
                if cached or attempt >= task['max_revision_rounds']:
                    raise

    def _narration_checkpoint(self, job_id):
        """Read only hash-verified, ordered revisions; rejected is not approved."""
        latest, measurement, reviews, pacing_retried = None, None, [], False
        for artifact in self.store.list_artifacts(job_id):
            kind = artifact['kind']
            if kind not in ('narration_measurement', 'ad_auto_script', 'ad_auto_proposal', 'ad_auto_pending', 'user_script_repair'):
                continue
            data = json.loads(Path(artifact['path']).read_text('utf-8'))
            pacing_retried = pacing_retried or data.get('measurement', {}).get('kind') == 'underfilled'
            if kind == 'narration_measurement':
                measurement = data
            if data.get('review'):
                reviews.append(data['review'])
            latest = dict(data)
            latest['approved'] = (kind == 'ad_auto_script'
                                  and (data.get('review') or {}).get('decision') == 'accept')
            if 'target_units' not in latest and measurement and data.get('measurement') == measurement.get('measurement'):
                latest['target_units'] = measurement.get('target_units')
        if latest:
            latest['review_history'] = reviews[-6:]
            latest['pacing_retried'] = pacing_retried
        return latest

    def _persist_editing_rules(self, job_id, task, resolved):
        """Persist the executed options, including the whole-output speech policy."""
        from .editing_rules import apply_rules
        from .ad_narration import configure
        task = configure(apply_rules(task, resolved))
        atomic_json(self.store.job_dir(job_id) / 'task.json', task)
        with self.store._connect() as db:
            db.execute('UPDATE jobs SET spec_json=? WHERE job_id=?',
                       (json.dumps(task, ensure_ascii=False), job_id))
        return task


    def _factual_narration_repair(self, job_id, task, assets, sources, plan, timeline,
                                  provider, *, measurement=None, goal=None):
        """Switch to simpler evidence-bound alternatives, never bypass review."""
        from . import ad_narration
        from . import narration_timing
        from .commerce import compile_ad, review_direct_script, NarrationPlanIssue
        if narration_timing.script_locked(task):
            return None
        repair_task = {**task, **({'_narration_repair_max_units':goal} if goal else {})}
        feedback = ''
        # One correction after independent rejection; never accept by exhaustion.
        for review_round in range(2):
            for candidate, report in ad_narration.factual_candidates(repair_task, sources, plan, timeline, provider=provider, feedback=feedback):
                if self.store.get_job(job_id)['cancel_requested']:
                    raise ToolCancelled('任务已取消')
                self.store.set_state(job_id, 'PLANNING', message='切换为简洁画面描述方案，重新审核并实测配音')
                try:
                    new_timeline, _, _ = compile_ad(task, assets, sources, candidate)
                except (PlanIssue, NarrationPlanIssue) as error:
                    feedback = str(error)
                    continue
                path = self.store.job_dir(job_id)/'auto-narration'/('alternative-'+uuid.uuid4().hex+'.json')
                saved = {'plan':candidate, 'repair':report, 'measurement':measurement or {},
                         'approved':False, 'reason':'替代方案等待独立内容审核'}
                atomic_json(path, saved)
                self.store.put_artifact(job_id, 'ad_auto_pending', path.stem, path)
                review = review_direct_script(provider, task, sources, candidate, new_timeline)
                accepted = review.get('decision') == 'accept'
                reviewed = path.with_name('reviewed-'+path.name)
                atomic_json(reviewed, {**saved, 'review':review, 'approved':accepted,
                                       'reason':'替代方案已通过内容审核' if accepted else '替代方案仍需调整'})
                self.store.put_artifact(job_id, 'ad_auto_script' if accepted else 'ad_auto_proposal',
                                        reviewed.stem, reviewed)
                feedback = json.dumps({'previous_candidate':candidate.get('narration_script'),
                    'previous_heading':candidate['clips'][0].get('heading'),
                    'review':review},ensure_ascii=False)
                if accepted:
                    new_timeline['ad_content_review'] = review
                    return candidate, new_timeline
        return None


    def _extend_narration_shots(self, job_id, task, assets, sources, plan, timeline,
                                provider, measurement):
        """Use additional observed source time when the user left length open."""
        import copy
        from .commerce import compile_ad, review_direct_script
        from .ad_narration import enabled
        if not enabled(task) or (task.get('target') or {}).get('duration_ms') is not None:
            return None
        needed = int(measurement.get('measured_us', 0)) + 500_000
        if needed <= timeline['duration_us'] or needed > MAX_OUTPUT_MS * 1000:
            return None
        candidate = copy.deepcopy(plan)
        by_id = {s['asset_id']:s for s in sources}
        for _ in range(40):
            if self.store.get_job(job_id)['cancel_requested']:
                raise ToolCancelled('任务已取消')
            changed = False
            for index, clip in enumerate(candidate['clips']):
                source = by_id[clip['asset_id']]
                units = source['speech_units']
                ids = [u['unit_id'] for u in units]
                left, right = ids.index(clip['first_unit']), ids.index(clip['last_unit'])
                for a, b in ((left, right+1), (left-1, right)):
                    if a < 0 or b >= len(units):
                        continue
                    start, end = units[a]['start_us'], units[b]['end_us']
                    if end-start > (60_000_000 if task.get('creation_mode') == 'long_video_v1' else 10_000_000):
                        continue
                    overlap = False
                    for other in candidate['clips']:
                        if other is clip or other['asset_id'] != clip['asset_id']:
                            continue
                        x, y = ids.index(other['first_unit']), ids.index(other['last_unit'])
                        if start < units[y]['end_us'] and end > units[x]['start_us']:
                            overlap = True
                    frames = [f['requested_us'] for f in source['frames']
                              if start <= f['requested_us'] < end]
                    if overlap or not frames:
                        continue
                    clip.update(first_unit=ids[a], last_unit=ids[b], frame_times_us=frames)
                    changed = True
                    break
                if changed:
                    break
            if not changed:
                return None
            try:
                expanded, _, _ = compile_ad(task, assets, sources, candidate)
            except PlanIssue:
                continue
            if expanded['duration_us'] < needed:
                continue
            self.store.set_state(job_id, 'PLANNING', message='在已识别素材内延长分镜，保留完整口播并重新审核')
            review = review_direct_script(provider, task, sources, candidate, expanded)
            accepted = review.get('decision') == 'accept'
            path = self.store.job_dir(job_id)/'auto-narration'/('extended-'+uuid.uuid4().hex+'.json')
            atomic_json(path, {'plan':candidate, 'review':review, 'approved':accepted,
                'measurement':measurement, 'repair':{'strategy':'extend_observed_shots',
                'original_duration_us':timeline['duration_us'], 'duration_us':expanded['duration_us'],
                'source_only':True, 'frame_freeze':False}})
            self.store.put_artifact(job_id, 'ad_auto_script' if accepted else 'ad_auto_proposal', path.stem, path)
            if accepted:
                expanded['ad_content_review'] = review
                return candidate, expanded
            return None


    def _deliver_direct_ad(self, job_id, task, assets, sources, timeline, plan, provider):
        task = {**task, '_script_repair_ledger':str(self.store.job_dir(job_id)/'auto-narration/repair-budget.json')}
        """Keep length repairs inside the running job, with immutable revisions."""
        from . import narration_timing
        from .commerce import compile_ad, rewrite_direct_voiceover, review_direct_script, NarrationPlanIssue
        folder = self.store.job_dir(job_id)
        checkpoint = self._narration_checkpoint(job_id)
        pending_repair = None
        if checkpoint:
            from .ad_narration import normalize_plan
            plan = normalize_plan(task, checkpoint['plan'])
            from .ad_narration import repair_heading_language
            plan = repair_heading_language(provider, task, plan, NarrationPlanIssue)
            timeline, _, _ = compile_ad(task, assets, sources, plan)
            task = {**task, '_narration_review_history': checkpoint['review_history'],
                    '_narration_pacing_retried': checkpoint['pacing_retried']}
            if checkpoint.get('approved'):
                timeline['ad_content_review'] = checkpoint['review']
                if checkpoint.get('measurement', {}).get('kind') == 'underfilled':
                    task['_narration_pacing_retried'] = True
            elif checkpoint.get('user_edited'):
                review = review_direct_script(provider, task, sources, plan, timeline)
                accepted = review.get('decision') == 'accept'
                path = folder/'user-script-repairs'/('review-'+uuid.uuid4().hex+'.json')
                atomic_json(path, {'plan':plan, 'review':review, 'approved':accepted, 'user_edited':True,
                                  'reason':'用户修订已通过内容审核' if accepted else '用户修订仍需调整'})
                self.store.put_artifact(job_id, 'ad_auto_script' if accepted else 'ad_auto_proposal', path.stem, path)
                if not accepted:
                    reasons = '；'.join(str(i) for i in review.get('issues', [])) or review.get('limits', '内容依据待确认')
                    alternative = self._factual_narration_repair(job_id, task, assets, sources,
                                                                plan, timeline, provider)
                    if not alternative:
                        raise ScriptRevisionPending('用户修订尚未通过内容审核：'+reasons)
                    plan, timeline = alternative
                    review = timeline['ad_content_review']
                timeline['ad_content_review'] = review
            else:
                pending_repair = checkpoint
                # A candidate's saved measurement normally belongs to the
                # paragraph before revision. Reuse it only when the original
                # measured artifact proves the spoken text and selected shot
                # intervals are unchanged. Legacy migration changes the TTS
                # structure, so it needs a fresh measurement as well.
                from .ad_narration import enabled as whole_ad
                measured = checkpoint.get('measurement')
                measurement_current = (isinstance(measured, dict)
                    and all(type(measured.get(key)) is int and measured[key] > 0
                            for key in ('available_us', 'measured_us'))
                    and (measured.get('kind') == 'underfilled'
                         and measured['measured_us'] < measured['available_us']
                         or measured.get('kind') in (None, 'overflow')
                         and measured['measured_us'] > measured['available_us']))
                if measurement_current:
                    original = None
                    for artifact in reversed(self.store.list_artifacts(job_id)):
                        if artifact['kind'] == 'narration_measurement':
                            original = json.loads(Path(artifact['path']).read_text('utf-8'))
                            break
                    measured_plan = (original or {}).get('plan') or {}
                    shot_keys = ('asset_id', 'first_unit', 'last_unit')
                    same_shots = ([{k:c.get(k) for k in shot_keys} for c in measured_plan.get('clips', [])]
                                  == [{k:c.get(k) for k in shot_keys} for c in plan.get('clips', [])])
                    if whole_ad(task):
                        same_speech = (isinstance(measured_plan.get('narration_script'), str)
                                       and measured_plan['narration_script'] == plan.get('narration_script'))
                    else:
                        same_speech = ([c.get('voiceover') for c in measured_plan.get('clips', [])]
                                       == [c.get('voiceover') for c in plan.get('clips', [])])
                    measurement_current = (bool(original) and original.get('measurement') == measured
                                           and same_shots and same_speech)
                if not measurement_current:
                    self.store.set_state(job_id, 'PLANNING',
                        message='恢复最新完整文案，重新审核并实测配音，不沿用旧文案时长')
                    review = review_direct_script(provider, task, sources, plan, timeline)
                    accepted = review.get('decision') == 'accept'
                    path = folder/'auto-narration'/('resume-review-'+uuid.uuid4().hex+'.json')
                    atomic_json(path, {'plan':plan, 'review':review, 'approved':accepted,
                        'measurement':{}, 'reason':'恢复文案已通过内容审核，等待实测配音' if accepted
                        else '恢复文案仍需调整，保留最新候选'})
                    self.store.put_artifact(job_id, 'ad_auto_script' if accepted else 'ad_auto_proposal', path.stem, path)
                    if not accepted:
                        history = [*task.get('_narration_review_history', []), review]
                        task = {**task, '_narration_review_history':history[-6:]}
                        alternative = self._factual_narration_repair(job_id, task, assets,
                                                                    sources, plan, timeline, provider)
                        if not alternative:
                            reasons = '；'.join(str(i) for i in review.get('issues', [])) or review.get('limits', '内容依据待确认')
                            raise ScriptRevisionPending('恢复文案与替代方案尚未通过内容审核：'+reasons)
                        plan, timeline = alternative
                    else:
                        timeline['ad_content_review'] = review
                    pending_repair = None
        for attempt in range(6):
            if self.store.get_job(job_id)['cancel_requested']:
                raise ToolCancelled('任务已取消')
            resumed_repair, pending_repair = pending_repair, None
            try:
                if resumed_repair:
                    # This is a real, hash-verified measurement of unchanged
                    # speech. Changed/unmeasured candidates were reviewed above
                    # and must reach actual synthesis before any length repair.
                    raise narration_timing.NarrationDurationIssue(resumed_repair['measurement'])
                return self._run_direct_delivery(job_id, task, assets, timeline, plan, provider)
            except narration_timing.NarrationDurationIssue as exc:
                if narration_timing.script_locked(task):
                    raise ScriptRevisionPending('指定口播要求原样保留，但实际配音超出固定成品时长；请允许精简文案或调整成品时长') from exc
                measured = exc.measurement
                underfilled = measured.get('kind') == 'underfilled'
                action = '扩写' if underfilled else '精简'
                if attempt == 5:
                    raise ScriptRevisionPending(f'自动{action}与实测配音仍未匹配；已保留全部修订和配音，需检查指定文案或音色服务') from exc
                if attempt >= 2 and not underfilled:
                    extended = self._extend_narration_shots(job_id, task, assets, sources,
                                                          plan, timeline, provider, measured)
                    if extended:
                        plan, timeline = extended
                        continue
                from .ad_narration import enabled as whole_ad
                old_count = (narration_timing.units(task, plan['narration_script']) if whole_ad(task)
                             else sum(narration_timing.units(task, c['voiceover']) for c in plan['clips']))
                goal = max(1, int(old_count * measured['available_us']/max(1,measured['measured_us']) * .82))
                if resumed_repair and resumed_repair.get('target_units'):
                    goal = resumed_repair['target_units']
                if underfilled:
                    hard, _ = narration_timing.budget(task, timeline, 1)
                    goal = min(hard, goal)
                    task = {**task, '_narration_pacing_retried':True}
                self.store.set_state(job_id, 'PLANNING', message='旁白过早结束，自动展开叙述并重新配音' if underfilled
                    else '实际配音偏长，自动精简文案并重新配音，无需点击继续')
                pending = folder/'auto-narration'/('measurement-'+uuid.uuid4().hex+'.json')
                atomic_json(pending, {'measurement': measured, 'target_units': goal, 'plan': plan, 'approved': False,
                                     'review': (resumed_repair or {}).get('review')})
                self.store.put_artifact(job_id, 'narration_measurement', pending.stem, pending)
                repair_task = {**task, '_narration_repair_max_units': goal}
                if underfilled:
                    repair_task['_narration_repair_min_units'] = min(goal, max(old_count+1, int(goal*.8)))
                # Content feedback remains bounded and never changes the frozen
                # visual selections. A shorter script still needs real review.
                history = list(task.get('_narration_review_history', []))
                issue = (json.dumps(resumed_repair['review'], ensure_ascii=False)
                         if resumed_repair and resumed_repair.get('review') else str(exc))
                content_issue = issue
                for review_attempt in range(3):
                    repair_task['_narration_review_history'] = history[-6:]
                    repair_task['_narration_repair_strategy'] = ('repair', 'facts_only', 'minimal')[review_attempt]
                    try:
                        revised, report = rewrite_direct_voiceover(provider, repair_task, sources, plan, timeline, issue)
                    except NarrationPlanIssue as repair_error:
                        pending = folder/'auto-narration'/('pending-'+uuid.uuid4().hex+'.json')
                        atomic_json(pending, {'measurement':measured, 'target_units':goal, 'plan':plan,
                            'review':history[-1] if history else None, 'approved':False, 'reason':str(repair_error)})
                        self.store.put_artifact(job_id, 'ad_auto_pending', pending.stem, pending)
                        if review_attempt == 2:
                            alternative = self._factual_narration_repair(job_id, repair_task, assets,
                                sources, plan, timeline, provider, measurement=measured, goal=goal)
                            if alternative:
                                plan, timeline = alternative
                                break
                            raise ScriptRevisionPending(f'自动{action}及替代方案仍未得到合格完整文案；已保存最新修订。具体原因：{repair_error}') from repair_error
                        # Keep content issues active when a length retry occurs.
                        issue = content_issue + '\n本次格式/长度问题：' + str(repair_error)
                        continue
                    new_timeline, _, _ = compile_ad(task, assets, sources, revised)
                    # Preserve the candidate before a network-dependent review.
                    # A timeout must not discard already applied caption fixes.
                    candidate_path = folder/'auto-narration'/('pending-'+uuid.uuid4().hex+'.json')
                    atomic_json(candidate_path, {'plan':revised, 'measurement':measured,
                        'target_units':goal, 'review':history[-1] if history else None,
                        'approved':False, 'reason':'最新文案等待独立内容审核'})
                    self.store.put_artifact(job_id, 'ad_auto_pending', candidate_path.stem, candidate_path)
                    review = review_direct_script(provider, {**task, '_narration_review_history':history[-6:]}, sources, revised, new_timeline)
                    proposal = folder/'auto-narration'/('script-'+uuid.uuid4().hex+'.json')
                    atomic_json(proposal, {'plan': revised, 'review': review, 'repair': report,
                                          'measurement': measured, 'target_units':goal,
                                          'approved': review.get('decision') == 'accept'})
                    self.store.put_artifact(job_id, 'ad_auto_script' if review.get('decision') == 'accept' else 'ad_auto_proposal', proposal.stem, proposal)
                    # Accumulate caption fixes even before acceptance; the next
                    # reviewer still checks the entire candidate independently.
                    plan, timeline = revised, new_timeline
                    history.append(review)
                    task = {**task, '_narration_review_history':history[-6:]}
                    if review.get('decision') == 'accept':
                        timeline['ad_content_review'] = review
                        break
                    issue = json.dumps(review, ensure_ascii=False)
                    content_issue = issue
                else:
                    reasons = '；'.join(review.get('issues', [])) or review.get('limits', '画面依据待确认')
                    alternative = self._factual_narration_repair(job_id, repair_task, assets,
                        sources, plan, timeline, provider, measurement=measured, goal=goal)
                    if alternative:
                        plan, timeline = alternative
                        continue
                    raise ScriptRevisionPending(f'自动{action}文案及替代方案尚未通过内容审核；已保存最新修订。具体原因：{reasons}')


    def _run_direct_delivery(self, job_id, task, assets, timeline, plan, provider):
        """Render a planned timeline directly to MP4, without Jianying.

        The renderer consumes only the immutable timeline/source evidence.  It
        writes job-local checkpoints plus the user-selected output and never
        enters the native host or draft registration path.
        """
        from .direct_renderer import render_snapshot, snapshot_from_timeline, write_snapshot
        from .direct_resources import build_direct_media_plan
        from .independent_export import validate_output_directory

        folder = self.store.job_dir(job_id)
        # A resumed job must never rewrite revision_001.  Keep all prior
        # reports/checkpoints readable and make the newest revision the one
        # surfaced by list_artifacts()/the workbench.
        revisions_root = folder / 'revisions'
        revisions_root.mkdir(parents=True, exist_ok=True)
        numbers = []
        for item in revisions_root.iterdir():
            if not item.is_dir():
                continue
            try:
                # Existing jobs use ``001`` folders while newer checkpoints
                # may use ``revision_001``.  Treat both as the same sequence.
                number = int(item.name.split('_', 1)[1]) if item.name.startswith('revision_') else int(item.name)
                numbers.append(number)
            except (ValueError, IndexError):
                pass
        next_number = max(numbers) + 1 if numbers else 1
        revision_id = f'revision_{next_number:03d}'
        revision_dir = revisions_root / f'{next_number:03d}'
        revision_dir.mkdir(parents=True, exist_ok=False)
        if task.get('narration_mode') == 'new_native':
            from .direct_resources import _cue_text
            _cue_text(plan or {}, timeline, required=True)
            content_review = timeline.get('ad_content_review') or {}
            if content_review.get('decision') != 'accept':
                raise PlanIssue('正式广告尚未通过独立文案内容审核，保留任务等待重规划')
            atomic_json(revision_dir/'ad-content-review.json', content_review)
            self.store.put_artifact(job_id, 'ad_content_review', revision_id,
                                    revision_dir/'ad-content-review.json', verified=True)
        self.store.set_state(job_id, 'RENDERING', message='使用 FFmpeg 独立渲染成片（不启动剪映）')
        media_plan = build_direct_media_plan(task, timeline, plan or {}, folder, self.config,
                                             report_dir=revision_dir)
        media_report_path = Path(media_plan['report_path'])
        self.store.put_artifact(job_id, 'direct_resource_plan', revision_id,
                                media_report_path, verified=True)
        if media_plan.get('report', {}).get('narration_overflow'):
            from .narration_timing import NarrationDurationIssue
            raise NarrationDurationIssue(media_plan['report']['narration_overflow'])
        from .ad_narration import pacing_issue
        pacing = pacing_issue(task, media_plan.get('narration_cues'), timeline['duration_us'])
        if pacing and not task.get('_narration_pacing_retried'):
            from .narration_timing import NarrationDurationIssue
            raise NarrationDurationIssue(pacing)
        if media_plan.get('pending') and (task.get('require_direct_audio_resources') or task.get('formal_ad_policy')):
            from .native_resource_planner import ResourcesPending
            raise ResourcesPending([
                {'kind': item.get('kind', 'audio'), 'reason': item.get('reason', 'resource_unavailable')}
                for item in media_plan['pending']], reason='direct_audio_resources_pending')
        snapshot = snapshot_from_timeline(task, assets, timeline, media_plan=media_plan, work_dir=folder)
        from .caption_policy import acceptance
        acceptance_report = acceptance(task, snapshot, media_plan)
        acceptance_path = revision_dir / 'creative-acceptance.json'
        atomic_json(acceptance_path, acceptance_report)
        self.store.put_artifact(job_id, 'creative_acceptance', revision_id, acceptance_path, verified=True)
        if acceptance_report['status'] != 'passed':
            raise PlanIssue('成品执行检查发现用户要求未落实：' + ', '.join(c['name'] for c in acceptance_report['checks'] if not c['passed']))
        snapshot_path = revision_dir / 'direct-snapshot.json'
        write_snapshot(snapshot_path, snapshot)
        self.store.put_artifact(job_id, 'direct_snapshot', revision_id, snapshot_path, verified=True)
        source_paths = [asset['path'] for asset in assets]
        output_root = task.get('output_dir') or self.config.get('direct_output_dir')
        if not output_root:
            output_root = self.store.root / 'Exports' / 'Direct' / job_id
        output_dir = validate_output_directory(output_root, source_paths=source_paths,
                                               draft_paths=[self.config.get('draft_root')])
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / (str(task.get('output_id') or job_id) + '.mp4')
        # A retry never overwrites an existing verified delivery.
        if output_path.exists():
            index = 2
            while (output_dir / f'{output_path.stem}-{index}{output_path.suffix}').exists():
                index += 1
            output_path = output_dir / f'{output_path.stem}-{index}{output_path.suffix}'
        report_path = revision_dir / 'direct-render-report.json'
        font_path = None
        if timeline.get('captions'):
            try:
                from .internal_fonts import resolve_font
                font_path = resolve_font(self.config.get('install_dir'), 'source_han_cn')['path']
            except Exception as exc:
                # The friend bundle has a licensed rounded font and direct
                # mode must remain usable when Jianying is not installed.
                bundled = Path(__file__).resolve().parents[1] / 'desktop-assets' / 'fonts' / 'LXGW975YuanSC-400W.ttf'
                if bundled.is_file():
                    font_path = str(bundled)
                else:
                    raise ValueError('直接渲染字幕需要已校验的内置字体：' + str(exc))
        def render_progress(value):
            duration=value.get('duration_s') or 0
            seconds=value.get('processed_s') or 0
            message=value.get('phase','视频渲染')
            if duration and value.get('percent') is not None:
                message+=f'：已处理 {seconds:.1f}/{duration:.1f} 秒（{value["percent"]:.0f}%）'
            speed=value.get('speed')
            if speed and speed!='N/A':message+='，速度 '+speed
            self.store.event(job_id,'RENDERING','progress',{'message':message,'render_progress':value})
        try:
            result = render_snapshot(snapshot, output_path,
                                    install_dir=self.config.get('direct_ffmpeg_dir'),
                                    work_dir=folder / 'direct-render-work',
                                    cancelled=lambda: self.store.get_job(job_id)['cancel_requested'],
                                    font_path=font_path,progress_callback=render_progress)
        except Exception as exc:
            failure = {'schema_version': '1.0', 'job_id': job_id,
                       'backend': 'direct_ffmpeg', 'status': 'FAILED',
                       'output_dir': str(output_dir), 'output_file': str(output_path),
                       'error': f'{type(exc).__name__}: {exc}',
                       'timeline_path': str(folder / 'revisions/001/timeline.json'),
                       'revision_id': revision_id,
                       'quality_downgraded': False}
            atomic_json(report_path, failure)
            self.store.put_artifact(job_id, 'direct_render_report', revision_id, report_path, verified=True)
            raise
        result.update({'schema_version': '1.0', 'job_id': job_id,
                       'output_dir': str(output_dir), 'timeline_path': str(folder / 'revisions/001/timeline.json'),
                       'snapshot_path': str(snapshot_path), 'quality_downgraded': False,
                       'model_planning_used': bool(provider),
                       'narration_mode': task.get('narration_mode'),
                       'resource_source_policy': task.get('resource_source_policy', 'online_or_builtin'),
                       'direct_resource_report': media_plan.get('report_path'),
                       'direct_resources': media_plan.get('resources', []),
                       'direct_resource_pending': media_plan.get('pending', []),
                       'source_audio_removed': bool(snapshot.get('source_audio_muted')),
                       'new_narration_generated': bool(((media_plan.get('report') or {}).get('tts') or {}).get('status') == 'COMPLETED'),
                       'ad_content_review': timeline.get('ad_content_review'),
                       'creative_acceptance': acceptance_report,
                       'continuous_video_reviewed': False, 'final_audio_listened': False,
                       'limitations': [
                           '剪映专属效果不参与独立渲染；不支持的效果会明确失败',
                           '直接资源只使用已缓存并记录指纹的本地/内置文件；缺失资源会记录为待补，不会静默伪造',
                           *(['模型接口拒收图片，画面与烧录字幕待人工核验']
                             if (plan or {}).get('_input_evidence', {}).get('images') == 'rejected_by_provider' else []),
                       ]})
        atomic_json(report_path, result)
        self.store.put_artifact(job_id, 'direct_render_report', revision_id, report_path, verified=True)
        delivery = {'schema_version': '1.0', 'job_id': job_id,
                    'revision_id': revision_id, 'render_backend': 'direct_ffmpeg',
                    'delivery_mode': 'mp4', 'final_export_created': True,
                    'final_export_checked': bool(result.get('playback_ok')),
                    'output_file': str(output_path), 'output_dir': str(output_dir),
                    'duration_us': snapshot['duration'], 'media': result.get('media'),
                    'quality_downgraded': False, 'draft_created': False,
                    'draft_path': None, 'model_planning_used': bool(provider),
                    'direct_resource_report': media_plan.get('report_path'),
                    'direct_resources': media_plan.get('resources', []),
                    'direct_resource_pending': media_plan.get('pending', []),
                    'source_audio_removed': bool(snapshot.get('source_audio_muted')),
                    'new_narration_generated': bool(((media_plan.get('report') or {}).get('tts') or {}).get('status') == 'COMPLETED'),
                    'ad_content_review': timeline.get('ad_content_review'),
                    'creative_acceptance': acceptance_report,
                    'continuous_video_reviewed': False, 'final_audio_listened': False,
                    'limitations': result['limitations']}
        delivery_path = revision_dir / 'delivery.json'
        atomic_json(delivery_path, delivery)
        self.store.put_artifact(job_id, 'delivery', revision_id, delivery_path, verified=True)
        self.store.set_state(job_id, 'COMPLETED', event_type='task_finished',
                             message='直接 MP4 成片已生成并通过时长、画幅和完整解码检查')
        return self.get_job(job_id)

    def _apply_supplemental_stage(self, job_id, task, assets, timeline, *, candidates=None,
                                  frames=None, sources=None):
        """Checkpoint evidence scores and optional subject layout tracks.

        The supplement is advisory for native Jianying drafts and is consumed
        by the direct FFmpeg renderer when verified subject boxes exist.
        """
        folder = self.store.job_dir(job_id)
        path = folder / 'revisions/001/supplemental-analysis.json'
        observation_map = {}
        for source in sources or []:
            if isinstance(source, dict) and source.get('asset_id'):
                observation_map[source['asset_id']] = source.get('observations') or []
        # A resumed direct job may not have the in-memory sources list. Recover
        # only immutable visual reports already stored below this job.
        for asset in assets:
            aid = asset.get('asset_id')
            if aid in observation_map:
                continue
            reports = []
            source_dir = folder / 'sources' / str(aid)
            for report_path in sorted(source_dir.glob('visual-observations-*.json')):
                try:
                    reports.append(json.loads(report_path.read_text(encoding='utf-8-sig')))
                except (OSError, json.JSONDecodeError):
                    continue
            if reports:
                observation_map[aid] = reports
        if path.is_file():
            report = self._json_stage(job_id, 'supplemental_analysis', path, lambda: None,
                                      verified=True)
            merged = dict(timeline)
            merged['supplement_version'] = report.get('supplement_version')
            merged['layout_tracks'] = report.get('layout_tracks') or []
            merged['supplemental_analysis'] = {
                'version': report.get('supplement_version'),
                'artifact': 'supplemental-analysis.json'}
            return merged
        merged, report = apply_supplements(
            task, timeline, assets, observations_by_asset=observation_map,
            candidates=candidates, frames=frames)
        report['timeline_duration_us'] = timeline.get('duration_us')
        atomic_json(path, report)
        self.store.put_artifact(job_id, 'supplemental_analysis', 'revision_001', path,
                                verified=True)
        self.store.event(job_id, 'PLANNING', 'progress', {
            'message': '已生成补充证据、候选评分与主体布局轨迹',
            'supplement_version': report.get('supplement_version'),
            'evidence_count': len(report.get('evidence') or []),
            'layout_track_count': len(report.get('layout_tracks') or []),
        })
        return merged

    def _run_job(self, job_id, *, resume=False):
        from .native_resource_planner import ResourcesPending
        job = self.store.get_job(job_id)
        if job['state'] != 'CREATED' and not resume:
            raise ValueError('Job is not newly created; use resume_job')
        task = job['task_spec']; folder = self.store.job_dir(job_id)
        assets = [self._check_asset(self.store.get_asset(aid)) for aid in task['asset_ids']]
        asset = assets[0]
        direct = task.get('render_backend', 'native_jianying') == 'direct_ffmpeg'
        montage = task['workflow'] in ('product_montage', 'product_ad', 'custom', 'crossborder_montage')
        provider = None
        sources = []
        try:
            if self.store.get_job(job_id)['cancel_requested']:
                raise ToolCancelled('Job cancellation requested')
            self.store.set_state(job_id, 'VALIDATING', message='检查素材指纹与执行环境')
            if any(item['frame_rate_mode'] == 'VFR' for item in assets):
                from .media_normalize import normalize
                self.store.event(job_id,'VALIDATING','progress',{'message':'识别到可变帧率，自动转换固定30帧工作副本；保留原素材'})
                assets=[{**normalize(item,self.inspector,self.store.root/'cache/normalized-media',
                    Path(self.config.get('direct_ffmpeg_dir') or self.config['toolchain_root'])/'ffmpeg.exe'),
                    'asset_id':item['asset_id']} if item['frame_rate_mode']=='VFR' else item for item in assets]
                asset=assets[0]
            bind_policy(task)
            if not direct:
                from .native_workflow import require_profile
                require_profile(self, job_id)  # Legacy native draft capability check.
            from .model_recovery import bind_recovery, model_preflight
            provider = bind_recovery(self.provider_factory(), folder/'model-requests')
            self.store.event(job_id, 'VALIDATING', 'progress',
                             {'message': '先检查模型连接，再分析素材画面' if direct else '先检查规划模型实际生成接口，再读取剪映资源'})
            model_check = model_preflight(provider, self.store.root/'model-health')
            self.store.event(job_id, 'VALIDATING', 'progress',
                             {'message': '规划模型预检完成', 'model_health': model_check})
            if direct and task.get('editing_rules_version') == 'prompt_first_v1':
                from .editing_rules import resolve_rules
                resolved = self._json_stage(job_id, 'editing_requirements', folder / 'editing-requirements.json',
                                           lambda: resolve_rules(provider, task))
                task = self._persist_editing_rules(job_id, task, resolved)
                self.store.event(job_id, 'VALIDATING', 'progress', {
                    'message':'已按用户指令确认剪辑规则，未指定项使用类别默认值',
                    'options':task['resolved_editing_options']})
            if not direct:
                from .native_workflow import preflight
                preflight(self, job_id, task, refresh_pending=True)
            executor = self.executor_factory(job_id)
            if task['workflow'] in ('product_ad', 'custom', 'crossborder_montage') and not direct:
                from .commerce import run_ad
                prepared_provider = provider
                provider = None  # Commerce owns this provider's usage receipt.
                return run_ad(self, job_id, task, assets, executor, lambda: prepared_provider)
            self.store.set_state(job_id, 'ANALYZING', message='提取素材画面与可用转写' if direct else '生成本地带时间字幕与原生画面证据')
            direct_ad = direct and task['workflow'] in ('product_ad', 'crossborder_montage', 'custom')
            if direct_ad:
                sources = self._prepare_direct_ad_sources(job_id, task, assets, executor, provider)
                candidates = [unit for source in sources for unit in source['speech_units']]
                frames = [frame for source in sources for frame in source['frames']]
                transcripts = {source['asset_id']: source['transcript'] for source in sources}
            elif montage:
                candidates, frames, transcripts = self._prepare_montage(job_id, task, assets, executor)
            else:
                transcript = self._transcribe(job_id, asset, executor)
                candidates = self._json_stage(job_id, 'evidence', folder / 'evidence.json',
                    lambda: {'schema_version': '1.0', 'asset_id': asset['asset_id'],
                             'segments': candidates_from_cues(asset['asset_id'], transcript['cues'],
                                                              asset['video_duration_us'])})['segments']
                frames = self._sample(job_id, asset, task['instruction'], direct=direct)['frames']
                candidates = annotate_candidates(candidates, frames=frames, transcript=transcript)
            self.store.set_state(job_id, 'PLANNING', message='DeepSeek 根据转写与实际静帧选择片段')
            if direct_ad:
                understanding = {'schema_version': '1.0', 'mode': 'direct_ad_planner'}
                plan, direct_timeline, ad_candidates, edit_plan = self._plan_direct_ad(
                    job_id, task, assets, sources, provider)
            else:
                understanding = self._json_stage(job_id, 'requirement_analysis',
                    folder / 'requirement-analysis.json', lambda: provider.understand(task, assets if montage else asset))
            if direct_ad:
                timeline = self._json_stage(job_id, 'timeline', folder / 'revisions/001/timeline.json',
                    lambda: direct_timeline, verified=True)
            elif montage:
                plan = self._plan_montage(job_id, {**task, 'editorial_analysis': understanding},
                                         assets, candidates, frames, transcripts, provider)
            else:
                plan = self._plan(job_id, {**task, 'editorial_analysis': understanding},
                                  candidates, frames, provider)
            self.store.set_state(job_id, 'COMPILING', message=('确定性编译源区间、字幕与直接渲染计划'
                                                               if direct else '确定性编译源区间、字幕与剪映原生计划'))
            if not direct_ad:
                from .montage import compile_montage
                timeline = self._json_stage(job_id, 'timeline', folder / 'revisions/001/timeline.json',
                    lambda: compile_montage(task, assets, candidates, plan) if montage else
                            compile_timeline(task, asset, transcript, candidates, plan), verified=True)
            timeline = self._apply_supplemental_stage(
                job_id, task, assets, timeline,
                candidates=(ad_candidates if direct_ad else candidates),
                frames=frames, sources=sources)
            if direct:
                if direct_ad and task.get('narration_mode') == 'new_native':
                    return self._deliver_direct_ad(job_id, task, assets, sources, timeline, plan, provider)
                return self._run_direct_delivery(job_id, task, assets, timeline, plan, provider)
            native_plan = folder / 'revisions/001/native-plan.json'
            if not native_plan.is_file():
                atomic_json(native_plan, timeline['native_plan'])
            self.store.put_artifact(job_id, 'native_plan', 'revision_001', native_plan, verified=True)
            self.store.set_state(job_id, 'BUILDING_DRAFT', message='剪映原生服务创建独立新草稿')
            adapter = JianyingAdapter(executor)
            build_dir = folder / 'revisions/001/native-build'
            build = adapter.build(native_plan, build_dir)
            draft_dir = build_dir / 'draft'
            self.store.put_artifact(job_id, 'native_draft', 'revision_001',
                                    draft_dir / 'draft_content.json', verified=True)
            caption_assessment = plan['visual_assessment']['burned_captions']
            add_editable_subtitles = task['constraints']['subtitles'] and caption_assessment == 'absent'
            if add_editable_subtitles:
                cues_file = folder / 'revisions/001/output-captions.json'
                if not cues_file.is_file():
                    atomic_json(cues_file, timeline['captions'])
                self.store.put_artifact(job_id, 'output_captions', 'revision_001', cues_file,
                                        verified=True)
                subtitles_dir = folder / 'revisions/001/subtitles'
                adapter.add_subtitles(draft_dir, cues_file, subtitles_dir)
                draft_dir = subtitles_dir / 'draft'
                self.store.put_artifact(job_id, 'subtitled_draft', 'revision_001',
                                        draft_dir / 'draft_content.json', verified=True)
            packaging = None
            if task.get('packaging_policy') == 'editable_layers_v1':
                draft_dir, packaging = self._package_native_revision(job_id, task, provider, adapter,
                    draft_dir, folder/'revisions/001',
                    {**timeline, 'captions':timeline['captions'] if add_editable_subtitles else []},
                    {'visual_assessment':plan['visual_assessment'], 'selected_plan':plan})
            self.store.set_state(job_id, 'CHECKING', message='读取原生草稿并检查时间线与字幕结构')
            snapshot_path = (subtitles_dir if add_editable_subtitles else build_dir) / 'draft-snapshot.json'
            if packaging: snapshot_path = folder/'revisions/001/layers/draft-snapshot.json'
            sys.path.insert(0, str(Path(self.config['toolchain_root']) / 'scripts'))
            try:
                from draft_quality import audit
                snapshot = json.loads(snapshot_path.read_text(encoding='utf-8-sig'))
                audit_result = audit(snapshot, base_dir=snapshot_path.parent,
                    primary_track=next(t['id'] for t in packaging['original_tracks'] if t['type']=='video') if packaging else None)
            finally:
                sys.path.pop(0)
            from .quality_controller import check_native_draft
            readback = check_native_draft(snapshot, timeline, audit_result,
                                          editable_captions=add_editable_subtitles,
                                          source_sha256=None if montage else asset['sha256'],
                                          source_has_audio=False if montage else asset['has_audio'],
                                          source_manifest={a['asset_id']: a for a in assets} if montage else None,
                                          packaging_report=packaging)
            structural_clear = not readback['issues']
            duration_match = readback['duration_match']
            video_mapping_match = readback['video_mapping_match']
            caption_match = readback['caption_mapping_match']
            self.store.set_state(job_id, 'PREVIEWING', message='捕获剪映实际渲染的首中尾画面')
            duration = snapshot['duration']
            positions = sorted({min(duration - 1, x) for x in (0, duration // 2, duration - 1)})
            if montage:
                positions = sorted(set(positions) | {
                    (row['timeline_start_us'] + row['timeline_end_us']) // 2 for row in timeline['source_map']})
                # preview_layers batches positions in groups of 20; keep all
                # source-map checkpoints so a larger montage remains reviewable.
            if packaging:
                positions = sorted(set(positions) | {r['expected']['target_timerange']['start']+
                    r['expected']['target_timerange']['duration']//2 for r in packaging['items']
                    if r['role'] in ('stickers','transitions')})
            from .packaging import preview_layers
            preview = preview_layers(adapter, draft_dir, folder / 'previews/native-001', positions)
            for frame in preview['frames']:
                if digest(Path(frame['path'])) != frame['sha256']:
                    raise ValueError('Native preview frame hash mismatch')
            if hasattr(provider, 'review'):
                self.store.set_state(job_id, 'CHECKING',
                                     message='模型复核选段台词与剪映实际静帧')
                model_review = self._json_stage(job_id, 'model_review',
                    folder / 'revisions/001/model-review.json',
                    lambda: provider.review(task, candidates, plan, timeline,
                        ([preview['frames'][i * (len(preview['frames']) - 1) // 7] for i in range(8)]
                         if len(preview['frames']) > 8 else preview['frames'])))
            else:
                model_review = {'schema_version': '1.0', 'decision': 'uncertain',
                    'issues': [], 'uncertainty': 'This test provider has no semantic reviewer',
                    'review_scope': 'not_run_scripted_provider', 'audio_listened': False,
                    'continuous_video_reviewed': False}
            if packaging:
                signal = adapter.audio_scan(draft_dir,folder/'revisions/001/audio-signal')
                self.store.put_artifact(job_id,'audio_signal_report','revision_001',
                    folder/'revisions/001/audio-signal/audio-scan-report.json',verified=True)
            semantic_issues = [{'code': 'model_semantic_issue', 'severity': 'review',
                'source_segment_id': item['source_segment_id'],
                'output_time_us': item['output_time_us'], 'problem': item['problem'],
                'evidence': item['evidence'], 'suggested_fix': item['suggested_fix']}
                for item in model_review['issues']]
            quality = {'schema_version': '1.0', 'revision_id': 'revision_001',
                       'structural_clear': structural_clear, 'duration_match': duration_match,
                       'video_mapping_match': video_mapping_match,
                       'caption_mapping_match': caption_match,
                       'embedded_audio_metadata_checked': readback['embedded_audio_metadata_checked'],
                       'native_preview_frame_count': len(preview['frames']),
                       'burned_caption_assessment': caption_assessment,
                       'editable_subtitles_added': add_editable_subtitles,
                       'issues': readback['issues'] + semantic_issues,
                       'model_review': model_review,
                       'automatic_post_build_revision_applied': False,
                       'continuous_video_reviewed': False,
                       'final_audio_listened': False, 'lip_sync_verified': False,
                       'narrative_verified': False, 'jianying_open_verified': False}
            self._json_stage(job_id, 'quality_report', folder / 'revisions/001/quality_report.json',
                             lambda: quality)
            delivery = {'schema_version': '1.0', 'job_id': job_id, 'revision_id': 'revision_001',
                        'editable_layers_verified': bool(packaging and packaging['editable_layers_verified']),
                        'packaging_policy':task.get('packaging_policy'),
                        'packaging_decisions':packaging['decisions'] if packaging else None,
                        'tracks':packaging['tracks'] if packaging else None,
                        'draft_path': str(draft_dir), 'draft_created': True,
                        'draft_readback_passed': structural_clear and duration_match and
                                                 video_mapping_match and caption_match,
                        'jianying_native_preview_checked': len(preview['frames']) == len(positions),
                        'subtitle_mode': ('editable_native' if add_editable_subtitles else
                                          'source_burned_retained' if caption_assessment == 'present' else
                                          'not_added_visual_uncertain' if task['constraints']['subtitles'] else 'not_requested'),
                        'jianying_open_verified': False, 'final_export_created': False,
                        'final_export_checked': False, 'semantic_review_pending': True,
                        'model_semantic_review_completed': model_review['decision'] != 'uncertain',
                        'model_revision_suggested_unapplied': model_review['decision'] == 'revise',
                        'transcript_proofreading_pending': True,
                        'limitations': quality['issues'] + ['Continuous viewing/listening and desktop open are pending'] +
                           (['Editable subtitle request remains unmet because original burned text was present or uncertain']
                            if task['constraints']['subtitles'] and not add_editable_subtitles else [])}
            self._json_stage(job_id, 'delivery', folder / 'delivery.json', lambda: delivery)
            if not delivery['draft_readback_passed']:
                self.store.set_state(job_id, 'FAILED', event_type='error',
                    message='原生草稿结构、逐镜头时间映射或字幕内容未通过读回检查')
            else:
                self.store.set_state(job_id, 'READY_FOR_REVIEW', event_type='task_finished',
                    message='可编辑草稿已创建并通过结构/抽帧检查，连续视听与桌面打开待验收')
        except ScriptRevisionPending as exc:
            self.store.set_state(job_id, 'WAITING_FOR_INPUT', error=str(exc), event_type='waiting_input',
                message='口播有限修订后仍未通过；保留镜头和文案，继续时从脚本检查点恢复')
        except VisualEvidencePending as exc:
            report_path = folder / 'visual-pending' / (uuid.uuid4().hex + '.json')
            atomic_json(report_path, {'schema_version':'1.0', 'status':'WAITING_FOR_INPUT',
                'reason':str(exc), 'quality_downgraded':False, 'automatic_retry':False,
                'resume_condition':'恢复图片服务或补充清晰素材后继续；已成功的画面分析检查点将复用'})
            self.store.put_artifact(job_id, 'visual_pending', report_path.stem, report_path, verified=True)
            self.store.set_state(job_id, 'WAITING_FOR_INPUT', error=str(exc), event_type='waiting_input',
                message='待补画面依据；保留任务和已完成检查点，不再以纯文字盲目重试')
        except ResourcesPending as exc:
            # A missing resource is a resumable checkpoint, not a discarded
            # edit. Never rewrite prior reports or declare a degraded delivery.
            pending_id = 'pending_' + uuid.uuid4().hex[:12]
            report_path = folder / 'resource-pending' / (pending_id + '.json')
            artifacts = self.store.list_artifacts(job_id)
            report = {'schema_version':'1.0', 'status':'BLOCKED_RESOURCES',
                'reason':exc.reason, 'pending_resources':exc.pending,
                'quality_downgraded':False, 'automatic_retry':False,
                'resume_condition':'补充可用的官方资源或恢复资源服务后，使用继续/重试从检查点恢复',
                'preserved_artifacts':[{k:a[k] for k in ('kind','revision_id','path','sha256')}
                                       for a in artifacts]}
            atomic_json(report_path, report)
            self.store.put_artifact(job_id, 'resource_pending', pending_id, report_path, verified=True)
            names = {'music':'音乐','sound':'音效','sticker':'贴纸','effect':'特效',
                     'transition':'转场','filter':'滤镜','font':'字体','text':'文案'}
            kinds = '、'.join(names.get(k,k) for k in sorted({r['kind'] for r in exc.pending}))
            self.store.set_state(job_id, 'BLOCKED_RESOURCES', error=str(exc), event_type='waiting_input',
                message='待补资源（'+kinds+'）；已保留已完成内容和检查点，补齐后可继续，质量要求保持不变')
        except ResourcePolicyBlocked as exc:
            from .native_connection import check as check_native_connection
            report_path = folder / 'resource-preflight.json'
            report = dict(exc.report)
            report['native_connection'] = check_native_connection(self.config.get('install_dir', ''))
            report['automatic_retry'] = False
            report['resume_condition'] = 'Verified official host and resource adapter capability changed'
            atomic_json(report_path, report)
            self.store.put_artifact(job_id, 'resource_preflight', 'policy_v1', report_path, verified=True)
            self.store.set_state(job_id, 'BLOCKED_RESOURCES', error=str(exc), event_type='error',
                                 message='当前任务未配置已验证的内部资源执行路径；保留原有素材和检查点')
        except ToolCancelled as exc:
            self.store.set_state(job_id, 'CANCELLED', event_type='task_finished', message=str(exc))
        except ProviderError as exc:
            self.store.event(job_id, 'MODEL_TRANSPORT', 'error',
                {'category': exc.category, 'status_code': exc.status_code,
                 'details': exc.details, 'message': str(exc)})
            # Gateway saturation and upstream overload are recoverable external
            # conditions. Keep the child queued instead of turning normal
            # capacity waits into a terminal failure; the bounded transport
            # still records the exact provider category and never retries a
            # request whose outcome is uncertain.
            waiting_categories = {'missing_key', 'authentication', 'balance',
                                 'rate_limit', 'overloaded'}
            from .model_recovery import _transient_transport_error
            network_wait = _transient_transport_error(exc) or exc.category == 'outcome_uncertain'
            waiting = exc.category in waiting_categories or network_wait
            if waiting:
                message = ('模型连接暂不可用或请求结果待确认；已保留检查点，恢复网络后点击“从检查点继续”，先查询原请求结果，不盲目重复提交'
                           if network_wait else
                           '网关当前授权的每日额度已用尽；保留检查点，额度恢复后可继续'
                           if exc.details.get('code') == 'daily_allowance_exhausted' else
                           'DeepSeek 网关忙碌或上游过载，任务保留在等待队列；健康检查恢复后可重试'
                           if exc.category in ('rate_limit', 'overloaded') else
                           'DeepSeek 授权/余额需要处理')
            else:
                message = 'DeepSeek 调用失败；检查操作记录'
            self.store.set_state(job_id, 'WAITING_FOR_INPUT' if waiting else 'FAILED',
                error=(message+'（daily_allowance_exhausted）' if exc.details.get('code') == 'daily_allowance_exhausted'
                       else f'{exc.category}: {exc}'), event_type='waiting_input' if waiting else 'error',
                message=message)
        except Exception as exc:
            self.store.set_state(job_id, 'FAILED', error=str(exc), event_type='error',
                                 message='任务在当前阶段失败；已保留检查点和操作记录')
        finally:
            if provider is not None:
                sequence = len(list(folder.glob('usage-*.json'))) + 1
                usage_path = folder / f'usage-{sequence:03d}.json'
                atomic_json(usage_path, {'schema_version': '1.0', **provider.usage,
                    'model': provider.config['model'], 'currency_cost': None})
                self.store.put_artifact(job_id, 'usage', f'usage_{sequence:03d}', usage_path)
        return self.get_job(job_id)
