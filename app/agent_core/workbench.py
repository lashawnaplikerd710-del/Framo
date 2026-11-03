"""Loopback-only workbench. Editing uses the existing TaskService and worker."""
from __future__ import annotations

import argparse
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser

from .gateway_credentials import apply_gateway, save_token, target, validate_profile, validate_token
from .credential_store import load_key
from .local_executor import draft_lock
from .native_host import editor_pids
from .provider import NoRedirect, _redact_error_text
from .runtime import load_config
from .task_service import MAX_MONTAGE_ASSETS, TaskService, task_spec
from .batch_model import DEFAULT_GAP_US, MAX_BATCH_OUTPUTS, ResourceSlots, estimate_timing, workload_features
from .formal_ad_policy import (FORMAL_AD_MINIMUM_NATIVE_COUNTS,
                               FORMAL_AD_REQUIRED_NATIVE_CATEGORIES)
from .independent_export import IndependentExport, load_report, validate_output_directory
from .task_store import atomic_json, stamp

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT/'workbench'
TESTED_VERSION = '11.5.0.14471'
MAX_CONCURRENT_JOBS = 1
SETTING_KEYS = {'gateway_url','install_dir','draft_root','user_data_dir'}


def detect_install():
    roots = [Path(os.environ.get('LOCALAPPDATA',''))/'JianyingPro/Apps',
             Path(os.environ.get('LOCALAPPDATA',''))/'JianyingPro',
             Path(os.environ.get('ProgramFiles','C:/Program Files'))/'JianyingPro', Path('D:/JianyingPro')]
    for root in roots:
        candidate = root/TESTED_VERSION
        if (candidate/'JianyingPro.exe').is_file(): return str(candidate)
    return ''


def default_settings():
    gateway = ''
    shipped = ROOT/'gateway-endpoint.json'
    owner = ROOT/'config.gateway.local.json'
    if shipped.exists(): gateway = json.loads(shipped.read_text(encoding='utf-8'))['base_url']
    elif owner.exists(): gateway = json.loads(owner.read_text(encoding='utf-8'))['provider']['base_url']
    from .installed_runtime import is_installed
    if is_installed(ROOT):
        return {'gateway_url': gateway, 'install_dir': '', 'draft_root': '', 'user_data_dir': ''}
    user_data = Path(os.environ.get('LOCALAPPDATA',''))/'JianyingPro/User Data'
    draft_root = user_data/'Projects/com.lveditor.draft'
    # Read only the user's own configured draft location, never a packaged owner path.
    index = draft_root/'root_meta_info.json'
    if index.exists():
        try:
            records = json.loads(index.read_text(encoding='utf-8-sig')).get('all_draft_store', [])
            paths = [Path(r.get('draft_fold_path','')).parent for r in records if r.get('draft_fold_path')]
            if paths and paths[0].is_dir(): draft_root = paths[0]
        except (ValueError,OSError): pass
    return {'gateway_url':gateway,'install_dir':detect_install(),'draft_root':str(draft_root),'user_data_dir':str(user_data)}


class Workbench:
    def __init__(self, state_dir):
        self.root = Path(state_dir).resolve()
        from .installed_runtime import is_installed
        if not is_installed(ROOT) and (not str(self.root).isascii() or len(str(self.root)) > 53):
            raise ValueError('工作目录请使用不超过 53 字符的英文短路径，例如 C:\\JianyingAgentWork\\Workbench')
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.root/'settings.json'
        self.settings = json.loads(self.settings_path.read_text(encoding='utf-8')) if self.settings_path.exists() else default_settings()
        self.config_path = self.root/'runtime-config.json'
        self.stop = threading.Event(); self.mutex = threading.RLock()
        self.busy_action = None
        self.registration_thread = None
        self.resource_slots = ResourceSlots({'model': 1, 'analysis': 1})
        self.worker_leases = {}
        self.export_threads = {}
        self.export_cancel_events = {}
        self.export_index_path = self.root/'independent-export-index.json'
        self.service = None
        self._configure()

    def _configure(self):
        # Direct FFmpeg mode only needs the model gateway.  A Jianying path is
        # optional and is copied through solely for explicitly legacy jobs.
        if not self.settings['gateway_url']: return
        template = json.loads((ROOT/'config.native.json').read_text(encoding='utf-8-sig'))
        config = apply_gateway(template, self.settings['gateway_url'])
        # Full native packaging, source vision, new narration and bounded repair
        # share this run budget. The smaller CLI preset stopped a real two-source
        # job before its third resource selection; the gateway still enforces
        # each user's independent daily ceiling.
        config['limits'] = {**config.get('limits', {}),
                            'max_model_calls':48, 'token_stop_threshold':640000}
        for key in ('install_dir','draft_root','user_data_dir'):
            config[key] = self.settings.get(key, '')
        config['toolchain_root'] = str(ROOT/'toolchain')
        for key in ('direct_ffmpeg_dir', 'direct_resources_root'):
            value = config.get(key)
            if value and not Path(value).is_absolute():
                config[key] = str((ROOT / value).resolve())
        model = ROOT/'models/sensevoice'
        if not model.exists(): model = (ROOT/template['asr_model_dir']).resolve()
        config['asr_model_dir'] = str(model)
        voice = (config.get('direct_media') or {}).get('local_voice_library')
        if voice and not Path(voice).is_absolute():
            config['direct_media']['local_voice_library'] = str((ROOT / voice).resolve())
        from .installed_runtime import is_installed
        if is_installed(ROOT) and not config.get('direct_output_dir'):
            config['direct_output_dir'] = str(self.root / 'Exports' / 'Direct')
        atomic_json(self.config_path, config)
        self.service = TaskService(load_config(self.config_path), self.root/'tasks')

    def jobs(self, *, all_jobs=False):
        if not self.service: return []
        if all_jobs:
            return self._queue_jobs()
        with self.service.store._connect() as db:
            query = 'SELECT job_id FROM jobs ORDER BY created_at DESC'
            ids = [r[0] for r in db.execute(query if all_jobs else query + ' LIMIT 100')]
        # One stale or damaged artifact must not abort the scheduler poll for
        # every other child. Preserve the job row and surface the isolated
        # inspection error so it remains retryable and auditable.
        result = []
        for job_id in ids:
            try:
                result.append(self.job(job_id))
            except Exception as exc:
                try:
                    raw = self.service.get_job(job_id)
                    result.append({
                        'job_id': job_id, 'state': raw['state'],
                        'instruction': raw['task_spec'].get('instruction', ''),
                        'workflow': raw['task_spec'].get('workflow'),
                        'render_backend': raw['task_spec'].get('render_backend', 'direct_ffmpeg'),
                        'created_at': raw['created_at'],
                        'error': self.safe_error(exc),
                        'worker_running': self.live_worker(job_id),
                        'events': [], 'delivery': None, 'publication': None,
                        'review': None, 'direct_render_report': None, 'registration': None,
                        'cancel_requested': raw['cancel_requested'],
                        'batch': self.service.store.batch_for_job(job_id),
                        'inspection_error': True,
                        'timing': estimate_timing(workload_features(output_count=1)),
                    })
                except Exception:
                    # A deleted/corrupt DB row should not kill the poll loop.
                    continue
        return result

    def _queue_jobs(self):
        """Scheduling needs queue rows, not a full audit of historical files.

        The worker still verifies inputs and checkpoints before execution.
        Native publication retains its existing verified-artifact path.
        """
        with self.service.store._connect() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY created_at DESC').fetchall()
            links = {r['job_id']:dict(r) for r in db.execute('SELECT * FROM batch_children')}
        jobs = []
        for row in rows:
            job = dict(row)
            job['batch'] = links.get(job['job_id'])
            job['worker_running'] = self.live_worker(job['job_id'])
            job['cancel_requested'] = bool(job['cancel_requested'])
            job['publication'] = None
            if job['state'] == 'READY_FOR_REVIEW':
                artifacts, _ = self.service.store.list_artifacts_safe(job['job_id'])
                for artifact in artifacts:
                    if artifact['kind'] == 'publication_summary':
                        job['publication'] = json.loads(Path(artifact['path']).read_text('utf-8'))
            jobs.append(job)
        return jobs

    def batches(self):
        if not self.service:
            return []
        try:
            # Status polling must not finalize batches or hash every MP4.
            from .batch_model import batch_counts, batch_state
            with self.service.store._connect() as db:
                states = {r['job_id']:dict(r) for r in db.execute(
                    'SELECT job_id,state,error,updated_at FROM jobs')}
            result = []
            for batch in self.service.store.list_batches():
                children = [{**c, **states[c['job_id']]} for c in batch['children']]
                folder = self.service.store.batch_dir(batch['batch_id'])
                report = {**batch, 'children':children,
                    'state':batch_state(c['state'] for c in children),
                    'counts':batch_counts(c['state'] for c in children),
                    'gap_us':batch['batch_spec'].get('gap_us', DEFAULT_GAP_US),
                    'report_path':str(folder/'batch-report.json')}
                final = folder/'batch-finalization.json'
                if final.is_file():
                    try:
                        report['finalization'] = json.loads(final.read_text('utf-8-sig'))
                    except (OSError, ValueError):
                        report['finalization'] = {'status':'report_unreadable'}
                result.append(report)
            return result
        except Exception as exc:
            # A corrupt child must not hide ordinary jobs or stop the scheduler.
            return [{'batch_id': 'unknown', 'state': 'ERROR',
                     'error': self.safe_error(exc), 'counts': {}}]

    def live_worker(self, job_id):
        receipt = self.service.store.job_dir(job_id)/'background-latest.json'
        if not receipt.exists(): return False
        try:
            import psutil
            data = json.loads(receipt.read_text(encoding='utf-8'))
            proc = psutil.Process(data['pid']); cmd = proc.cmdline()
            return proc.is_running() and 'agent_core.background' in cmd and job_id in cmd and data['worker_id'] in cmd
        except (OSError,ValueError,KeyError,psutil.Error): return False

    def active(self):
        return self.busy_action is not None or any(j['worker_running'] for j in self.jobs())

    def active_count(self):
        return sum(1 for job in self.jobs() if job['worker_running'])

    def job(self, job_id):
        job = self.service.get_job(job_id)
        artifacts, invalid_artifacts = self.service.list_artifacts_safe(job_id)
        delivery = publication = review = direct_report = None
        for artifact in artifacts:
            if artifact['kind'] in ('delivery','publication_summary','model_review','direct_render_report'):
                try:
                    data = json.loads(Path(artifact['path']).read_text(encoding='utf-8'))
                    if artifact['kind'] == 'delivery': delivery = data
                    elif artifact['kind'] == 'publication_summary': publication = data
                    elif artifact['kind'] == 'model_review': review = data
                    else: direct_report = data
                except (OSError,ValueError): pass
        target_duration_ms = ((job['task_spec'].get('target') or {}).get('duration_ms') or 0)
        features = workload_features(
            duration_us=target_duration_ms * 1000,
            source_count=len(job['task_spec'].get('asset_ids', [])), output_count=1)
        return {'job_id':job_id,'state':job['state'],'instruction':job['task_spec']['instruction'],
                'workflow':job['task_spec']['workflow'],'render_backend':job['task_spec'].get('render_backend', 'direct_ffmpeg'),'created_at':job['created_at'],
                'error':self.safe_error(job['error']) if job['error'] else None,'worker_running':self.live_worker(job_id),
                'events':self.service.read_events(job_id)[-40:],
                'native_progress':self.native_progress(job_id) if job['state']=='BUILDING_DRAFT' else None,
                'delivery':delivery,'publication':publication,'review':review,
                'direct_render_report':direct_report,
                'registration':self.registration_status(job_id) if job['state']=='READY_FOR_REVIEW' and not publication else None,
                'cancel_requested':job['cancel_requested'],
                'integrity_warnings': invalid_artifacts,
                'batch':self.service.store.batch_for_job(job_id),
                'timing':estimate_timing(features)}

    def registration_status(self, job_id):
        path = self.service.store.job_dir(job_id)/'auto-registration.json'
        if path.exists():
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(value, dict) or not isinstance(value.get('status'), str):
                    raise ValueError('Invalid registration checkpoint')
                return value
            except (OSError, ValueError):
                return {'status':'failed','message':'登记检查点无法读取，请检查后重试登记。'}
        return {'status':'queued','message':'草稿自动检查已通过，等待自动登记到剪映首页。'}

    def _registration_status(self, job_id, status, message):
        prior = self.registration_status(job_id)
        if prior.get('status') == status and prior.get('message') == message:
            return
        atomic_json(self.service.store.job_dir(job_id)/'auto-registration.json',
                    {'status':status,'message':message,'updated_at':stamp()})
        self.service.store.event(job_id, 'PUBLISHING', 'error' if status=='failed' else 'progress',
                                 {'message':message,'registration_status':status})

    def _start_registration(self, job_id):
        """Caller holds the scheduler mutex; manual and automatic requests share one lane."""
        self._registration_status(job_id, 'registering', '草稿自动检查已通过，正在登记到剪映首页。')
        self.busy_action = '正在登记草稿'
        def publish():
            try:
                # Keep all existing native readback, resource and fingerprint checks.
                self.service.publish_job(job_id)
                self._registration_status(job_id, 'registered', '已登记到剪映首页的“本地草稿”，可打开检查音画。')
            except Exception as exc:
                # Only pre-mutation contention is retried automatically. A real
                # registration failure retains the checked draft for inspection.
                if str(exc) == 'Save and exit Jianying before publishing a registered draft':
                    self._registration_status(job_id, 'waiting_editor',
                        '草稿自动检查已通过，等待退出剪映后自动登记；请保持工作台运行。')
                elif str(exc) == 'managed_host_or_registration_active':
                    self._registration_status(job_id, 'waiting_workers', '等待其他原生操作结束后自动登记。')
                else:
                    self._registration_status(job_id, 'failed',
                        '草稿已保留，登记未完成：'+self.safe_error(exc)+'；处理后可点击“重试登记”。')
            finally:
                with self.mutex:
                    self.busy_action = None
        self.registration_thread = threading.Thread(target=publish, daemon=True)
        try:
            self.registration_thread.start()
        except Exception:
            self.busy_action = None
            raise

    def native_progress(self, job_id):
        # Long native operations write checkpoints between elements. Show those
        # checkpoints without changing or interrupting the editing worker.
        try:
            journals=list(self.service.store.job_dir(job_id).glob('n*/journal.json'))
            if not journals:return None
            latest=max(journals,key=lambda p:p.stat().st_mtime_ns)
            journal=json.loads(latest.read_text(encoding='utf-8'))
            plan=json.loads((latest.parent/'plan.json').read_text(encoding='utf-8'))
            return {'completed':len(journal.get('steps',[])), 'total':len(plan['elements']),
                    'phase':'原生元素写入'}
        except (OSError,ValueError,KeyError,TypeError):return None

    def checks(self):
        checks = [dict(name='独立 Python 运行环境',ok=True,detail='无需启动 Codex')]
        direct_default = not bool(self.service and self.service.config.get('render_backend_default') == 'native_jianying')
        if direct_default:
            ffmpeg_candidates = []
            if self.service:
                for root in (self.service.config.get('direct_ffmpeg_dir'),
                             self.service.config.get('install_dir')):
                    if root:
                        ffmpeg_candidates.append(Path(root) / 'ffmpeg.exe')
            ffmpeg_ok = any(path.is_file() for path in ffmpeg_candidates) or bool(shutil.which('ffmpeg'))
            checks.append(dict(name='独立 FFmpeg 渲染器', ok=ffmpeg_ok,
                               detail='直接生成 MP4，不启动剪映'))
        else:
            checks.append(dict(name='已验证的剪映版本',ok=bool(self.settings['install_dir']) and Path(self.settings['install_dir']).name==TESTED_VERSION and
                (Path(self.settings['install_dir'])/'JianyingPro.exe').is_file(),detail=f'当前仅验证 Windows {TESTED_VERSION}'))
            checks.append(dict(name='剪映用户目录',ok=Path(self.settings['user_data_dir']).is_dir(),detail='使用这台电脑自己的剪映账号'))
            checks.append(dict(name='草稿目录',ok=Path(self.settings['draft_root']).is_dir(),detail=self.settings['draft_root']))
        checks.append(dict(name='可用磁盘空间',ok=shutil.disk_usage(self.root).free > 2*1024**3,detail='至少预留 2GB；实际需求取决于素材大小'))
        if self.service:
            model = Path(self.service.config['asr_model_dir'])
            checks.append(dict(name='素材语音分析依赖',ok=(model/'model.int8.onnx').is_file() and (model/'tokens.txt').is_file(),detail='本机分析资源'))
            configured = bool(load_key(target=target(self.service.config['provider'])))
            checks.append(dict(name='工作台访问凭证',ok=configured,detail='不需要填写 DeepSeek 或 Qwen API Key'))
        else: checks.append(dict(name='工作台配置',ok=False,detail='请完成设置'))
        return checks

    def status(self):
        export_index = self._load_export_index()
        export_reports = []
        for batch_id, item in export_index.items():
            report = load_report(item.get('output_dir')) if item.get('output_dir') else None
            if report:
                export_reports.append(report)
        return {'settings':self.settings,'direct_output_dir':self.default_output_directory(),
                'checks':self.checks(),'jobs':self.jobs(),
                'batches':self.batches(),'resource_slots':self.resource_slots.snapshot(),
                'busy_action':self.busy_action,
                'external_exports': export_reports,
                'external_export_defaults': {key: value.get('output_dir') for key, value in export_index.items()
                                             if isinstance(value, dict) and value.get('output_dir')},
                'state_dir':str(self.root),'models':{'planning':'DeepSeek Flash','visual_fallback':'DeepSeek text evidence only','review':'DeepSeek Flash'},
                'limitations':['成片使用 FFmpeg 生成 MP4，原始素材保持不变；无法可靠复现的效果会明确提示。',
                    '图片接口不可用时 DeepSeek 退回到已校验文字、时间戳和元数据，记录 image_fallbacks，不把文字分析伪装成视觉结论。',
                    f'当前素材单条不超过 7 分钟；长视频成片最长 10 分钟；带货混剪需 2–{MAX_MONTAGE_ASSETS} 条同画幅素材，原口播只作参考，默认生成全新口播和声音；批次最多 {MAX_BATCH_OUTPUTS} 条成品。剪辑任务逐条执行，单成品优先，再按批次和成品顺序处理；重试和恢复也排队。',
                    '抽样画面与声音信号检查不能代替完整视听验收。']}

    def _load_export_index(self):
        if not self.export_index_path.is_file():
            return {}
        try:
            data = json.loads(self.export_index_path.read_text(encoding='utf-8-sig'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_export_index(self, value):
        atomic_json(self.export_index_path, value)

    def _export_snapshot(self, batch_id):
        finalization_path = self.service.store.batch_dir(batch_id) / 'batch-finalization.json'
        if not finalization_path.is_file():
            self.service.finalize_batch(batch_id)
        if not finalization_path.is_file():
            raise ValueError('批次尚未形成可导出的主草稿')
        finalization = json.loads(finalization_path.read_text(encoding='utf-8-sig'))
        if finalization.get('render_backend') == 'direct_ffmpeg':
            raise ValueError('直接成片批次会在剪辑完成后自动生成独立 MP4，无需再次导出；请在任务与草稿查看成品或打开成品目录。')
        master_path = Path(finalization.get('master_snapshot') or '')
        if not master_path.is_file():
            raise ValueError('批次主草稿快照不存在或不可读')
        snapshot = json.loads(master_path.read_text(encoding='utf-8-sig'))
        if finalization.get('validation', {}).get('status') != 'clear':
            raise ValueError('批次时间区段校验未通过，拒绝猜测导出')
        draft = finalization.get('master_draft')
        return snapshot, draft

    def external_export_status(self, batch_id=None):
        index = self._load_export_index()
        if batch_id:
            item = index.get(batch_id)
            return load_report(item.get('output_dir')) if item and item.get('output_dir') else None
        return [load_report(item.get('output_dir')) for item in index.values()
                if item.get('output_dir') and load_report(item.get('output_dir'))]

    def start_external_export(self, batch_id, output_dir, *, conflict='rename', output_ids=None):
        if not self.service:
            raise ValueError('请先完成工作台设置')
        if not output_dir:
            raise ValueError('未选择输出目录，不能开始导出')
        with self.mutex:
            thread = self.export_threads.get(batch_id)
            if thread and thread.is_alive():
                raise ValueError('该批次已有独立导出正在进行')
            snapshot, draft = self._export_snapshot(batch_id)
            source_paths = []
            for group in (snapshot.get('materials') or {}).values():
                for material in group if isinstance(group, list) else []:
                    if isinstance(material, dict) and material.get('path'):
                        source_paths.append(material['path'])
            output = validate_output_directory(output_dir, source_paths=source_paths,
                                              draft_paths=[draft] if draft else [])
            index = self._load_export_index()
            index[batch_id] = {'output_dir': str(output), 'updated_at': stamp(),
                               'last_conflict': conflict}
            self._save_export_index(index)
            cancel = threading.Event(); self.export_cancel_events[batch_id] = cancel
            exporter = IndependentExport(batch_id=batch_id, master_snapshot=snapshot,
                                         output_dir=output, install_dir=self.service.config.get('direct_ffmpeg_dir'),
                                         conflict=conflict, source_draft=draft,
                                         cancel_event=cancel)
            def worker():
                try:
                    exporter.run(selected=output_ids)
                finally:
                    with self.mutex:
                        self.export_threads.pop(batch_id, None)
                        self.export_cancel_events.pop(batch_id, None)
            thread = threading.Thread(target=worker, name='independent-export-'+batch_id[-8:], daemon=True)
            self.export_threads[batch_id] = thread; thread.start()
            return {'batch_id': batch_id, 'state': 'QUEUED', 'output_dir': str(output),
                    'backend': 'FFmpeg external renderer'}

    def retry_external_export(self, batch_id, output_id):
        index = self._load_export_index(); item = index.get(batch_id)
        if not item or not item.get('output_dir'):
            raise ValueError('该批次还没有导出目录')
        return self.start_external_export(batch_id, item['output_dir'],
                                          conflict=item.get('last_conflict', 'rename'),
                                          output_ids=[output_id])

    def cancel_external_export(self, batch_id):
        event = self.export_cancel_events.get(batch_id)
        if not event:
            raise ValueError('该批次当前没有进行中的导出')
        event.set()
        return {'batch_id': batch_id, 'state': 'CANCELLED'}

    def default_output_directory(self):
        return self.settings.get('direct_output_dir') or (
            self.service.config.get('direct_output_dir', '') if self.service else '')

    def save_output_directory(self, output_dir):
        """Save a new-task default; never reconfigure workers or existing specs."""
        if not isinstance(output_dir, str) or not output_dir.strip():
            raise ValueError('请选择成品保存文件夹')
        with self.mutex:
            sources = []
            if self.service:
                with self.service.store._connect() as db:
                    for row in db.execute('SELECT metadata_json FROM assets'):
                        sources.append(json.loads(row[0]).get('path'))
            protected = [self.settings.get('draft_root'), ROOT, self.root/'tasks']
            from .installed_runtime import is_installed
            candidate = Path(output_dir.strip()).expanduser().resolve()
            if is_installed(ROOT) and candidate != self.root and candidate.is_relative_to(self.root):
                # Installed UserData belongs to the user, not the shipped
                # program. Keep checkpoints and runtime caches protected.
                protected = [self.settings.get('draft_root')] + [self.root/name for name in
                             ('tasks', 'VoiceCache', 'ModelCache', 'Temp')]
            folder = validate_output_directory(output_dir.strip(), source_paths=sources,
                                               draft_paths=protected)
            # Verify actual write access, not just the Windows access flag.
            import tempfile
            with tempfile.TemporaryFile(dir=folder):
                pass
            settings = {**self.settings, 'direct_output_dir': str(folder)}
            atomic_json(self.settings_path, settings)
            self.settings = settings
            return {'saved': True, 'output_dir': str(folder)}

    def _bind_output_directory(self, spec):
        if spec.get('render_backend') == 'direct_ffmpeg' and self.settings.get('direct_output_dir'):
            return {**spec, 'output_dir': self.settings['direct_output_dir']}
        return spec

    def save_settings(self, data):
        with self.mutex:
            if self.active() or any(j['state']=='CREATED' for j in self.jobs()): raise ValueError('请等待队列结束后再修改配置')
            if set(data)-SETTING_KEYS-{'access_token'}: raise ValueError('未知设置项')
            settings = {**self.settings,**{k:v.strip() for k,v in data.items() if k in SETTING_KEYS and isinstance(v,str)}}
            profile = {'transport':'gateway','api_key_env':'JIANYING_GATEWAY_TOKEN','base_url':settings['gateway_url']}
            validate_profile(profile)
            if data.get('access_token'): save_token(profile, data['access_token'].strip())
            self.settings = settings; atomic_json(self.settings_path,settings); self._configure()
            return {'saved':True}

    def gateway_status(self):
        if not self.service: raise ValueError('请先保存网关和剪映路径')
        with self.mutex:
            token = load_key(target=target(self.service.config['provider']))
            if not token: raise ValueError('请先保存工作台访问凭证')
            request = urllib.request.Request(self.settings['gateway_url']+'/status',headers={'Authorization':'Bearer '+validate_token(token),'User-Agent':'Jianying-Agent-Workbench/0.1'})
            try:
                with urllib.request.build_opener(NoRedirect()).open(request,timeout=20) as response:
                    return json.loads(response.read(64000))
            except Exception:
                raise ValueError('网关连接失败，请检查网络、地址和访问凭证；本次没有调用模型') from None

    def register_files(self, paths):
        if not self.service: raise ValueError('请先完成设置')
        if not isinstance(paths,list) or not 1 <= len(paths) <= MAX_MONTAGE_ASSETS: raise ValueError(f'一次请选择 1–{MAX_MONTAGE_ASSETS} 个视频')
        result=[]
        for path in paths:
            source=Path(path).resolve(strict=True)
            if source.suffix.lower() not in ('.mp4','.mov','.mkv','.m4v'): raise ValueError('请选择视频文件')
            aid=self.service.register_asset(source)
            asset=self.service.store.get_asset(aid)
            result.append({'asset_id':aid,'name':source.name,'duration_us':asset['video_duration_us'],
                           'width':asset['width'],'height':asset['height']})
        return result

    def _prepare_spec(self, data):
        instruction=data.get('instruction','').strip()
        workflow = data.get('workflow','custom')
        long_mode = workflow in ('long_video','long_video_no_voice')
        spec=task_spec(instruction,data.get('asset_ids',[]),workflow,
                       target_country=data.get('target_country'),
                       render_backend=data.get('render_backend', 'direct_ffmpeg'))
        if long_mode:
            workflow = 'custom'
            duration = data.get('target_duration_ms')
            if duration is not None:
                if type(duration) is not int or not 1_000 <= duration <= 600_000:
                    raise ValueError('长视频目标时长应为 1 秒至 10 分钟')
                if spec['target']['duration_ms'] not in (None,duration):
                    raise ValueError('所选时长与剪辑要求冲突，请统一目标时长')
                spec['target']['duration_ms'] = duration
            if workflow == 'custom' and data.get('workflow') == 'long_video_no_voice':
                if spec['target']['duration_ms'] is not None and spec['target']['duration_ms'] < 40_000:
                    raise ValueError('长视频（无口播）需要完整保留开头四十秒，目标时长不能少于四十秒')
        # Direct FFmpeg delivery is the default workbench path.  The legacy
        # Jianying draft mode remains available to callers that explicitly set
        # render_backend=native_jianying.
        # Keep the existing workbench review-scope policy.  Direct MP4
        # rendering is isolated from the resource-use label and records its
        # online/builtin source policy separately.
        spec['resource_use']='local_review'
        from .font_library import validate_subtitle_font_id
        font_id = validate_subtitle_font_id(data.get('subtitle_font_id'))
        if font_id:
            spec['subtitle_font_id'] = font_id
        spec['defaults_applied'].append('workbench_friend_beta_local_review')
        if isinstance(data.get('direct_media'), dict):
            spec['direct_media'] = dict(data['direct_media'])
        if type(data.get('require_direct_audio_resources')) is bool:
            spec['require_direct_audio_resources'] = data['require_direct_audio_resources']
        if spec['render_backend'] == 'direct_ffmpeg' and workflow in ('custom','crossborder_montage'):
            from .editing_rules import configure_rules
            return configure_rules(spec)
        if spec['workflow'] in ('product_ad', 'custom', 'crossborder_montage'):
            aliases={'music':'音乐','sound':'音效','sticker':'贴纸','transition':'转场','effect':'特效','filter':'滤镜','text':'文案'}
            disabled={kind for kind,label in aliases.items()
                      if re.search(r'(?:不要|不加|无需|不用)\s*'+label,instruction)}
            mandatory=set(spec.get('required_native_categories', []))
            if spec['workflow'] in ('product_ad', 'crossborder_montage'):
                # These are part of the formal-ad contract. Do not let a
                # casual omission turn into a source-only rough cut.
                forbidden=mandatory & disabled
                if forbidden:
                    labels='、'.join(aliases[k] for k in sorted(forbidden))
                    raise ValueError('正式广告混剪必须包含：'+labels)
            enabled=[kind for kind in aliases if kind not in disabled]
            spec['required_native_categories']=sorted(mandatory | set(enabled))
            base_counts=dict(spec.get('minimum_native_counts', {}))
            base_counts.update({kind:(2 if kind in ('sound','transition','text') else 1)
                                for kind in spec['required_native_categories']})
            if spec['workflow'] == 'product_ad':
                base_counts.update(FORMAL_AD_MINIMUM_NATIVE_COUNTS)
            spec['minimum_native_counts']=base_counts
            if 'text' in enabled:
                spec['require_online_font']=True
                spec['minimum_online_fonts']=2 if '多字体' in instruction else 1
            spec['defaults_applied'].append('formal_ad_elements_required' if spec['workflow'] == 'product_ad'
                                            else 'product_ad_complete_native_packaging')
            if spec['workflow'] == 'custom':
                spec['allow_single_source'] = True
                keep_source = bool(re.search(r'(?:保留原声|保留原口播|不要(?:新)?口播|不要配音|不配音)', instruction))
                spec['narration_mode'] = 'preserve_source' if keep_source else 'new_native'
                spec['separate_narration'] = not keep_source
                spec['defaults_applied'].append('custom_visual_first_new_native_narration')
            elif spec['workflow'] == 'crossborder_montage':
                spec['narration_mode'] = 'text_only'
                spec['separate_narration'] = False
                spec['copy_only'] = True
                # Music and sound effects are required even though narration
                # remains disabled. Unsupported visual elements are checked by
                # the selected renderer instead of being silently dropped.
                spec['require_direct_audio_resources'] = True
                spec['defaults_applied'].append('crossborder_localized_ad_elements_required_no_narration')
        return spec

    def create_batch(self, data):
        with self.mutex:
            if any(not c['ok'] for c in self.checks()): raise ValueError('请先解决环境自检中的问题')
            outputs = data.get('outputs')
            if outputs is None:
                count = data.get('output_count', 1)
                if type(count) is not int or not 1 <= count <= MAX_BATCH_OUTPUTS:
                    raise ValueError(f'批量成品数量需为 1–{MAX_BATCH_OUTPUTS}')
                outputs = [{**data, 'output_id': f'output_{i:02d}'} for i in range(1, count + 1)]
            specs = []
            for row in outputs:
                spec = self._bind_output_directory(self._prepare_spec(row))
                spec['output_id'] = row.get('output_id')
                specs.append(spec)
            return self.service.create_batch(specs, gap_us=data.get('gap_us', DEFAULT_GAP_US),
                                             quality=data.get('quality', 'standard'))

    def create_job(self, data):
        with self.mutex:
            if any(not c['ok'] for c in self.checks()): raise ValueError('请先解决环境自检中的问题')
            count = data.get('output_count', 1)
            if type(count) is int and count > 1:
                return self.create_batch(data)
            spec = self._bind_output_directory(self._prepare_spec(data))
            job_id=self.service.create_job(spec)
            return {'job_id':job_id,'queued':True}

    def scheduler(self):
        while not self.stop.wait(2):
            try:
                self.scheduler_tick()
            except Exception as exc:
                # Keep the scheduler alive, but surface the poll failure so a
                # corrupt child cannot become a silent batch-wide stall.
                self._scheduler_error(exc)

    def _scheduler_error(self, exc):
        try:
            atomic_json(self.root/'scheduler-error.json', {
                'schema_version': '1.0', 'updated_at': stamp(),
                'error_type': type(exc).__name__, 'message': self.safe_error(exc)})
        except Exception:
            pass

    def execution_capacity(self, jobs=None):
        """Run one whole editing job at a time, including model-only stages."""
        return MAX_CONCURRENT_JOBS

    def _execution_order(self, jobs):
        """Singles first; FIFO batches with children in explicit ordinal order.

        Rebuild from persisted identities, so retries and restarts use the same
        order. Already running workers are allowed to finish without preemption.
        """
        batches = {}
        for job in jobs:
            batch = job.get('batch')
            if batch:
                batch_id = batch['batch_id']
                created = job.get('created_at', '')
                batches[batch_id] = min(batches.get(batch_id, created), created)

        def key(job):
            batch = job.get('batch')
            if not batch:
                return (0, job.get('created_at', ''), job['job_id'], 0)
            return (1, batches[batch['batch_id']], batch['batch_id'], batch['ordinal'])

        return sorted(jobs, key=key)

    def _sync_worker_slots(self, jobs):
        """Keep the in-process execution slot view aligned with live workers."""
        live_ids = {job['job_id'] for job in jobs if job.get('worker_running')}
        for job_id, lease in list(self.worker_leases.items()):
            if job_id not in live_ids:
                try:
                    self.resource_slots.release(lease)
                except ValueError:
                    pass
                self.worker_leases.pop(job_id, None)
        # A workbench can be restarted while workers continue. Re-acquire a
        # lease for those workers so the displayed capacity remains honest.
        for job in jobs:
            job_id = job['job_id']
            if job_id in self.worker_leases or not job.get('worker_running'):
                continue
            lease = self.resource_slots.acquire('model')
            if lease is not None:
                self.worker_leases[job_id] = lease

    def _stale_worker(self, job):
        """Return true when a detached worker died before finishing its job."""
        if job.get('state') in ('COMPLETED', 'READY_FOR_REVIEW', 'FAILED',
                                'CANCELLED', 'WAITING_FOR_INPUT', 'BLOCKED_RESOURCES'):
            return False
        if job.get('worker_running'):
            return False
        receipt_path = self.service.store.job_dir(job['job_id']) / 'background-latest.json'
        if not receipt_path.is_file():
            return False
        try:
            receipt = json.loads(receipt_path.read_text(encoding='utf-8-sig'))
            report_path = Path(receipt['worker_report'])
            report = json.loads(report_path.read_text(encoding='utf-8-sig')) if report_path.is_file() else {}
            status = report.get('status', receipt.get('status'))
            if status == 'worker_finished':
                return False
            pid = int(receipt.get('pid', 0))
            if pid > 0:
                try:
                    import psutil
                    if psutil.pid_exists(pid):
                        return False
                except ImportError:
                    # The bundled runtime may omit psutil. A zero-signal
                    # probe is enough for stale-worker recovery and avoids
                    # turning a missing optional dependency into a stall.
                    try:
                        os.kill(pid, 0)
                        return False
                    except OSError:
                        pass
            started = receipt.get('started_at') or report.get('started_at')
            # Give a just-spawned process time to write its worker report.
            if started:
                age = time.time() - __import__('datetime').datetime.fromisoformat(
                    started.replace('Z', '+00:00')).timestamp()
                if age < 8:
                    return False
            return True
        except (OSError, ValueError, KeyError, TypeError, ImportError):
            return False

    def _recoverable_jobs(self, jobs):
        result = []
        for job in jobs:
            if self._stale_worker(job):
                result.append(job)
        return result

    def _auto_finalize_batches(self, jobs):
        """Refresh completed batch masters and register them once per batch."""
        if not self.service:
            return
        service = self.service
        try:
            batches = service.list_batches()
        except (AttributeError, TypeError):
            # Lightweight stage-one scheduler fakes may not expose batches.
            return
        if not isinstance(batches, list):
            return
        for batch in batches:
            if batch.get('state') not in ('READY_FOR_REVIEW', 'PARTIAL_FAILURE'):
                continue
            child_jobs = [job for job in jobs if (job.get('batch') or {}).get('batch_id') == batch['batch_id']]
            if any(job.get('worker_running') for job in child_jobs):
                continue
            try:
                finalization = service.finalize_batch(batch['batch_id'])
                if (finalization.get('status') == 'master_ready' and
                        not finalization.get('missing') and
                        not finalization.get('registration')):
                    # Preserve serialization of legacy native publication;
                    # ordinary read/hash maintenance needs no creation lock.
                    with self.mutex:
                        if self.service is not service or self.busy_action is not None or editor_pids():
                            continue
                        service.publish_batch(batch['batch_id'])
            except Exception as exc:
                self._scheduler_error(exc)

    def scheduler_tick(self):
        with self.mutex:
            if not self.service or self.busy_action is not None: return
            jobs = self.jobs(all_jobs=True)
            self._sync_worker_slots(jobs)
            running = sum(bool(job['worker_running']) for job in jobs)
            pending = [job for job in reversed(jobs) if job['state']=='READY_FOR_REVIEW'
                       and not job.get('publication') and not job.get('cancel_requested')
                       and not job.get('batch')
                       and self.registration_status(job['job_id'])['status'] != 'failed']
            if pending:
                if running:
                    for job in pending:
                        self._registration_status(job['job_id'], 'waiting_workers',
                            '草稿自动检查已通过，等待其他剪辑任务结束后自动登记。')
                    return  # Drain active editors before starting another queued job.
                try:
                    opened = bool(editor_pids())
                except Exception as exc:
                    for job in pending:
                        self._registration_status(job['job_id'], 'failed',
                            '无法检查剪映运行状态，草稿已保留：'+self.safe_error(exc)+'；处理后可重试登记。')
                    return
                if opened:
                    for job in pending:
                        self._registration_status(job['job_id'], 'waiting_editor',
                            '草稿自动检查已通过，等待退出剪映后自动登记；请保持工作台运行。')
                else:
                    self._start_registration(pending[0]['job_id'])
                    return
            capacity = self.execution_capacity(jobs)
            recoverable = {job['job_id'] for job in self._recoverable_jobs(jobs)}
            for job in self._execution_order(jobs):
                if running >= capacity: break
                if not job['worker_running'] and (job['state'] == 'CREATED' or job['job_id'] in recoverable):
                    if job['cancel_requested']:
                        self.service.store.set_state(job['job_id'],'CANCELLED',message='排队任务已取消'); continue
                    from .background import launch
                    lease = self.resource_slots.acquire('model')
                    if lease is None:
                        break
                    try: launch(self.config_path,self.service.store.root,job['job_id'])
                    except Exception as exc:
                        self.resource_slots.release(lease)
                        self.service.store.set_state(job['job_id'],'WAITING_FOR_INPUT',error=self.safe_error(exc),message='任务未能启动，可查看原因后恢复')
                    else:
                        self.worker_leases[job['job_id']] = lease
                        if job['job_id'] in recoverable:
                            self.service.store.event(job['job_id'], 'RECOVERY', 'progress',
                                {'message': '后台 worker 已退出，按最近检查点恢复批次子任务'})
                        running += 1
        # Finalizing historical batches may hash large MP4s. Do it outside the
        # queue mutex, and reuse the queue snapshot instead of reading history
        # a second time. Never hold up task creation for idle maintenance.
        if running == 0:
            self._auto_finalize_batches(jobs)

    def safe_error(self, exc):
        from .user_messages import chinese_diagnostic
        message = re.sub(r'jyg_[A-Za-z0-9_-]{43}', '[访问凭证已隐藏]', str(exc))
        return chinese_diagnostic(_redact_error_text(message,os.environ.get('JIANYING_GATEWAY_TOKEN',''),400))

    def action(self, data):
        action=data['action']
        if not self.service: raise ValueError('请先完成设置')
        if action == 'retry_batch_child':
            result = self.service.retry_batch_child(data['batch_id'], data['output_id'])
            return result
        if action == 'finalize_batch':
            return self.service.finalize_batch(data['batch_id'])
        if action == 'register_batch':
            return self.service.publish_batch(data['batch_id'])
        if action == 'export_batch':
            return self.start_external_export(data['batch_id'], data.get('output_dir'),
                                              conflict=data.get('conflict', 'rename'),
                                              output_ids=data.get('output_ids'))
        if action == 'external_export_retry':
            return self.retry_external_export(data['batch_id'], data['output_id'])
        if action == 'external_export_cancel':
            return self.cancel_external_export(data['batch_id'])
        if action == 'external_export_status':
            return self.external_export_status(data.get('batch_id'))
        job_id=data['job_id']
        if action in ('diagnose', 'save_script_repair', 'fix_script_language', 'suggest_script_rewrite'):
            from .task_recovery import diagnose, save_correction, suggest_language_fix, suggest_script_rewrite
            with self.mutex:
                if action == 'diagnose':
                    result = diagnose(self.service, job_id)
                    result['editable'] = result['editable'] and not self.live_worker(job_id)
                    result['reason'] = self.safe_error(result['reason'])
                    return result
                if self.live_worker(job_id):
                    raise ValueError('此任务正在执行，请等待停止后再修订')
                if action == 'fix_script_language':
                    return suggest_language_fix(self.service, job_id, data.get('checkpoint_id'))
                if action == 'suggest_script_rewrite':
                    return suggest_script_rewrite(self.service, job_id, data.get('checkpoint_id'), data.get('draft'))
                result = save_correction(self.service, job_id, data.get('checkpoint_id'),
                                         data.get('headings'), data.get('narration_script'))
                if data.get('continue') is True:
                    self.service.store.clear_cancel(job_id)
                    self.service.store.set_state(job_id, 'CREATED',
                        message='用户修订已保存，排队重新检查内容、配音和成片')
                    return {**result, 'queued':True}
                return {**result, 'diagnostics':diagnose(self.service, job_id)}
        if action=='cancel': self.service.cancel_job(job_id); return {'accepted':True}
        if action=='open_folder':
            job=self.job(job_id)
            path=(job['publication'] or {}).get('registered_draft_path')
            if not path or not Path(path).is_dir(): raise ValueError('草稿尚未登记成功')
            os.startfile(path); return {'opened':True}
        with self.mutex:
            if action=='resume':
                job = self.service.get_job(job_id)
                if job['state'] in ('COMPLETED', 'READY_FOR_REVIEW'):
                    raise ValueError('该任务已生成草稿，请检查或登记草稿')
                if self.live_worker(job_id):
                    raise ValueError('该任务正在执行，请等待完成')
                if job['state'] != 'CREATED' or job.get('cancel_requested'):
                    self.service.store.clear_cancel(job_id)
                    self.service.store.event(job_id, 'RECOVERY', 'progress', {
                        'message': '从检查点继续已排队，单成品优先，逐条执行',
                        'previous_state': job['state'], 'previous_error': job.get('error')})
                    self.service.store.set_state(job_id, 'CREATED', error=None,
                        message='等待顺序执行；启动时仍校验素材、程序版本和检查点')
                return {'accepted':True, 'queued':True, 'job_id':job_id}
            if action=='publish':
                if self.active(): raise ValueError('已有任务正在执行，请等待完成')
                job = self.job(job_id)
                if job.get('publication'): return {'registered':True}
                if job['state']!='READY_FOR_REVIEW': raise ValueError('草稿自动检查通过后才能登记')
                if editor_pids():
                    self._registration_status(job_id, 'waiting_editor',
                        '草稿自动检查已通过，等待退出剪映后自动登记；请保持工作台运行。')
                    return {'accepted':True,'waiting_for_editor':True}
                self._start_registration(job_id)
                return {'accepted':True}
        raise ValueError('不支持此操作')


class Handler(BaseHTTPRequestHandler):
    server_version='JianyingWorkbench/0.1'
    def setup(self):
        super().setup()
        self.connection.settimeout(120)
    def log_message(self,*args): pass
    def send_json(self, value, code=200, cookie=None):
        raw=json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(code); self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(raw)))
        if cookie: self.send_header('Set-Cookie',cookie)
        self.end_headers(); self.wfile.write(raw)
    def allowed(self, mutation=False):
        if self.headers.get('Host') != self.server.host_header: return False
        origin=self.headers.get('Origin')
        if origin and origin!='http://'+self.server.host_header: return False
        cookie=SimpleCookie()
        try: cookie.load(self.headers.get('Cookie',''))
        except Exception: return False
        value=cookie.get('jy_session')
        if not value or not hmac.compare_digest(value.value,self.server.session): return False
        return not mutation or hmac.compare_digest(self.headers.get('X-Workbench-CSRF',''),self.server.csrf)
    def read_json(self):
        if self.headers.get('Content-Type','').split(';')[0]!='application/json': raise ValueError('需要 JSON 请求')
        length=int(self.headers.get('Content-Length','0'))
        if not 0 < length <= 100000: raise ValueError('请求大小无效')
        return json.loads(self.rfile.read(length))
    def do_GET(self):
        if self.headers.get('Host')!=self.server.host_header: return self.send_json({'error':'禁止访问'},403)
        if self.path in ('/','/app.js','/style.css'):
            name={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}[self.path]
            raw=(WEB/name).read_bytes()
            self.send_response(200); self.send_header('Content-Type',{'/':'text/html','/app.js':'text/javascript','/style.css':'text/css'}[self.path]+'; charset=utf-8')
            self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
            self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        if not self.allowed(): return self.send_json({'error':'请从启动器打开工作台'},403)
        try:
            if self.path=='/api/status': return self.send_json({**self.server.app.status(),'csrf':self.server.csrf})
            if self.path=='/api/batches': return self.send_json({'batches': self.server.app.batches()})
            self.send_json({'error':'不存在'},404)
        except Exception as exc: self.send_json({'error':self.server.app.safe_error(exc)},400)
    def do_POST(self):
        try:
            if self.path=='/api/unlock':
                if self.headers.get('Host')!=self.server.host_header or self.headers.get('Origin')!='http://'+self.server.host_header: return self.send_json({'error':'禁止访问'},403)
                value=self.read_json()
                if not hmac.compare_digest(str(value.get('token','')),self.server.session): return self.send_json({'error':'启动会话无效'},403)
                return self.send_json({'csrf':self.server.csrf},cookie='jy_session='+self.server.session+'; HttpOnly; SameSite=Strict; Path=/')
            if not self.allowed(True): return self.send_json({'error':'禁止访问'},403)
            if self.path=='/api/upload': return self.upload()
            data=self.read_json(); app=self.server.app
            if self.path=='/api/settings': result=app.save_settings(data)
            elif self.path=='/api/gateway-status': result=app.gateway_status()
            elif self.path=='/api/jobs': result=app.create_job(data)
            elif self.path=='/api/batches': result=app.create_batch(data)
            elif self.path=='/api/action': result=app.action(data)
            elif self.path=='/api/shutdown':
                with app.mutex:
                    if app.active(): raise ValueError('任务仍在运行，请先等待完成或取消任务')
                    app.stop.set()
                    self.send_json({'stopped':True})
                    threading.Thread(target=self.server.shutdown,daemon=True).start()
                    return
            elif self.path=='/api/pick':
                result=app.register_files(pick_files())
            else: return self.send_json({'error':'不存在'},404)
            self.send_json(result)
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception as exc: self.send_json({'error':self.server.app.safe_error(exc)},400)
    def upload(self):
        app=self.server.app
        if not app.service: raise ValueError('请先完成设置')
        length=int(self.headers.get('Content-Length','0'))
        if not 0 < length <= 2*1024**3: raise ValueError('单个上传文件需小于 2GB；更大文件请使用本地选择')
        if shutil.disk_usage(app.root).free < length+2*1024**3: raise ValueError('磁盘空间不足')
        name=urllib.parse.unquote(self.headers.get('X-File-Name','video.mp4'))
        suffix=Path(name).suffix.lower()
        if suffix not in ('.mp4','.mov','.mkv','.m4v'): raise ValueError('请选择视频文件')
        directory=app.root/'uploads'; directory.mkdir(exist_ok=True)
        path=directory/(uuid.uuid4().hex+suffix)
        try:
            remaining=length
            with path.open('xb') as out:
                while remaining:
                    chunk=self.rfile.read(min(1024*1024,remaining))
                    if not chunk: raise ValueError('文件传输中断')
                    out.write(chunk); remaining-=len(chunk)
            result=app.register_files([str(path)]); result[0]['name']=Path(name).name
            self.send_json(result)
        except Exception:
            path.unlink(missing_ok=True)
            raise


def pick_files():
    # This is an explicit user-triggered file picker, never editor UI automation.
    script="[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.OpenFileDialog; $d.Multiselect=$true; $d.Filter='Video|*.mp4;*.mov;*.mkv;*.m4v'; if($d.ShowDialog() -eq 'OK'){ ConvertTo-Json -InputObject @($d.FileNames) -Compress }"
    p=subprocess.run(['powershell.exe','-NoProfile','-STA','-WindowStyle','Hidden','-Command',script],capture_output=True,text=True,encoding='utf-8',
        env={**os.environ,'PYTHONIOENCODING':'utf-8'},creationflags=subprocess.CREATE_NO_WINDOW)
    if not p.stdout.strip(): raise ValueError('未选择素材')
    return json.loads(p.stdout)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir',default=os.environ.get('SystemDrive','C:')+'/JianyingAgentWork/Workbench')
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    app=Workbench(args.state_dir)
    with draft_lock(app.root/'.workbench.lock'):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.app=app; server.host_header='127.0.0.1:'+str(server.server_port)
        server.session=secrets.token_urlsafe(32); server.csrf=secrets.token_urlsafe(32)
        atomic_json(app.root/'workbench-status.json',{'pid':os.getpid(),'port':server.server_port,'state_dir':str(app.root),'codex_required':False})
        threading.Thread(target=app.scheduler,daemon=True).start()
        if not args.no_browser: webbrowser.open('http://'+server.host_header+'/#'+server.session)
        try: server.serve_forever()
        finally: app.stop.set(); server.server_close()


if __name__=='__main__': main()
