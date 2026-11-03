"""Native Windows workbench; direct service calls, no HTTP server or browser."""
from __future__ import annotations

import argparse
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .desktop_studio import (StudioLayout, BG, TEXT, MUTED, AMBER, SUCCESS, RED,
                             EXPORT_LABELS, timecode, read_sections)
from .local_executor import draft_lock
from .task_service import MAX_MONTAGE_ASSETS
from .batch_model import MAX_BATCH_OUTPUTS
from .task_store import atomic_json
from .workbench import ROOT, Workbench
from . import market_rules

from .version import VERSION
MARKET_PRESETS = {
    '跨境电商广告 · 日本区': '日本',
    '跨境电商广告 · 美国区': '美国',
}
WORKFLOWS = {
    '跨境电商广告 · 日本区': 'crossborder_montage',
    '跨境电商广告 · 美国区': 'crossborder_montage',
    '自定义剪辑': 'custom',
    '长视频': 'long_video',
    '长视频（无口播）': 'long_video_no_voice',
}
HINTS = {
    'long_video_no_voice': '前四十秒完整保留一条素材开头，之后按语义交叉混剪；全片保留原声与已有字幕并添加装饰元素。',
    'long_video': '最多 20 条素材，每条最长 7 分钟；按主题分段组织内容，成片最长 10 分钟。',
    'custom': f'选择 1–{MAX_MONTAGE_ASSETS} 条视频，填写你的剪辑要求；按提示词规划，不强制带货结构、配音或包装。',
    'product_ad': f'选择 2–{MAX_MONTAGE_ASSETS} 条同画幅商品素材；无论是否有原口播，默认只作参考，最终生成全新口播和声音。',
    'crossborder_montage': f'选择 1–{MAX_MONTAGE_ASSETS} 条商品素材；按目标市场生成新口播与卖点字幕，去除原声；用户指令优先，缺配音时待补资源。',
    'product_montage': f'选择 2–{MAX_MONTAGE_ASSETS} 条同画幅素材，保留素材本身的声音。',
    'talking_head': '选择 1 条中文口播素材，保留完整表达。',
    'interview': '选择 1 条中文访谈素材，保留回答上下文。',
}
LABELS = dict(CREATED='排队中', ANALYZING='分析素材', ANALYZING_SOURCE='分析素材',
              TRANSCRIBING='理解口播', PLANNING='制定剪辑方案', EXECUTING='执行剪辑',
              BUILDING_DRAFT='构建草稿', REVIEWING='抽样复核', PACKAGING='编排元素',
              VALIDATING='检查素材与环境', REVISING='调整剪辑', READY_FOR_REVIEW='草稿待检查',
              COMPLETED='任务结束', WAITING_FOR_INPUT='需要处理', BLOCKED_RESOURCES='待补资源',
              FAILED='执行失败', CANCELLED='已取消', PUBLISHING='登记草稿',
              RENDERING='FFmpeg 直接渲染')
LABELS['COMPILING'] = '编译时间线'
READY = {'READY_FOR_REVIEW', 'COMPLETED'}


def default_instruction(workflow, market=None):
    if workflow == 'long_video_no_voice':
        from .locked_opening import DEFAULT_BRIEF
        return DEFAULT_BRIEF
    if workflow == 'long_video':
        return '根据素材组织主题和章节，保留有价值的完整表达和原声，删除重复与无意义停顿，保持叙事连贯，生成长视频成片。'
    if workflow == 'custom':
        return ''
    if workflow == 'product_ad':
        return '制作 28 秒带货混剪成片，使用全新口播、字幕文案、音乐、音效、转场、节奏和缩放。'
    if workflow == 'crossborder_montage':
        return market_rules.default_instruction(market or '日本')
    return '根据素材自主剪辑，保留有价值的信息，直接生成 MP4 成片。'


def event_text(event):
    payload = event.get('payload') or {}
    if payload.get('message') or payload.get('error'):
        return str(payload.get('message') or payload['error'])
    if payload.get('stage') == 'sentence_synthesized':
        return f"Framo 已生成第 {payload.get('slot', '?')} 句全新口播"
    stages = {'new_script_written': '全新口播文案已生成',
              'native_narration_quality_check': '检查全新口播、字幕与音轨'}
    return stages.get(payload.get('stage'), LABELS.get(event.get('stage'), event.get('stage', '')))


def local_time(value):
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone().strftime('%m-%d %H:%M:%S')
    except (ValueError, AttributeError):
        return str(value or '')


def job_label(job):
    if (job.get('publication') or {}).get('registered_draft_path'):
        return '已登记 · 待视听检查'
    if job['state']=='READY_FOR_REVIEW':
        return {'queued':'检查通过 · 等待自动登记', 'waiting_editor':'待登记 · 请退出剪映',
                'waiting_workers':'待登记 · 等待剪辑结束', 'registering':'正在自动登记',
                'failed':'草稿已保留 · 登记失败'}.get((job.get('registration') or {}).get('status'),
                                                   '检查通过 · 等待自动登记')
    if job.get('render_backend') == 'direct_ffmpeg' and job['state'] == 'COMPLETED':
        return 'MP4 已生成'
    label = LABELS.get(job['state'], job['state'])
    return label + (' · 执行中' if job.get('worker_running') else '')


def allowed_actions(job):
    if not job:
        return set()
    actions = set()
    registered = (job.get('publication') or {}).get('registered_draft_path')
    direct = job.get('render_backend') == 'direct_ffmpeg'
    if direct:
        actions.add('change_output_dir')
    ready = job['state'] in READY
    if registered or (direct and (job.get('delivery') or {}).get('output_file')):
        actions.add('open_folder')
    if (ready and not direct and not registered and not job.get('batch') and not job.get('worker_running') and
            (job.get('registration') or {}).get('status') != 'registering'):
        actions.add('publish')
    if not ready and not job.get('worker_running') and job['state'] != 'CREATED':
        actions.add('resume')
    if job['state'] in ('WAITING_FOR_INPUT', 'BLOCKED_RESOURCES', 'FAILED', 'CANCELLED'):
        actions.add('diagnose')
    if not ready and job['state'] != 'CANCELLED' and not job.get('cancel_requested'):
        actions.add('cancel')
    if job.get('batch') and job['state'] in ('FAILED', 'WAITING_FOR_INPUT', 'BLOCKED_RESOURCES'):
        actions.add('retry_batch_child')
    return actions


class DesktopWorkbench(StudioLayout):
    """Tk widgets stay on the main thread. Service work is serialized off-thread."""

    def __init__(self, window, backend, *, poll=True):
        self.window = window
        self.backend = backend
        self.assets = []
        self.jobs = {}
        self.snapshot = {}
        self.closed = False
        self.busy = False
        self.refresh_pending = False
        self.settings_loaded = False
        self.poll_enabled = poll
        self.inbox = queue.Queue()
        self.timers = set()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='desktop-service')
        # History inspection must never queue ahead of user mutations.
        self.status_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='desktop-status')
        self._status_generation = 0
        # Preview decoding is isolated from the serialized backend executor so
        # refreshing tasks or starting a render never stalls the UI preview.
        self.preview_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='desktop-preview')
        self.buttons = []
        self.views = {}
        self.nav_buttons = {}
        self.state_dir = Path(backend.root)
        self._style()
        self._build()
        self.window.protocol('WM_DELETE_WINDOW', self.on_close)
        self.window.bind('<Control-o>', lambda _: self.pick_files())
        self.window.bind('<Control-Return>', lambda _: self.create_job() if self.current_view == 'create' else None)
        for number, view in enumerate(('create', 'tasks', 'export', 'settings'), 1):
            self.window.bind(f'<Alt-Key-{number}>', lambda _, v=view: self.show(v))
        self.window.bind('<Alt-Key-5>', lambda _: self.show('templates'))
        self.show('create')
        self.later(60, self._drain)
        if poll:
            self.later(100, self.refresh)
            self.later(4000, self._poll)

    def later(self, delay, callback):
        def run():
            self.timers.discard(token)
            if not self.closed:
                callback()
        token = self.window.after(delay, run)
        self.timers.add(token)


    def _export_conflict_value(self):
        return {'自动重命名': 'rename', '覆盖': 'overwrite', '跳过': 'skip'}.get(self.export_conflict.get(), 'rename')

    def refresh_export_page(self):
        self.render_export_groups()
        self.refresh_export_items()

    def _selected_export_report(self):
        batch_id = self.export_batch.get().strip()
        for report in self.snapshot.get('external_exports', []):
            if report.get('batch_id') == batch_id:
                return report
        return None

    def _direct_export_view(self):
        """Read current direct deliveries; this is not an external export receipt."""
        batch = self.batches.get(self.export_batch.get().strip()) or {}
        final = batch.get('finalization') or {}
        children = batch.get('children') or []
        known_jobs = [self.jobs.get(c.get('job_id')) or {} for c in children]
        if (final.get('render_backend') != 'direct_ffmpeg' and
                not (known_jobs and all(j.get('render_backend') == 'direct_ffmpeg' for j in known_jobs))):
            return None
        outputs = {r['output_id']: r for r in final.get('outputs', []) if r.get('output_id')}
        rows = []
        for child, job in zip(children, known_jobs):
            current = outputs.get(child.get('output_id')) or job.get('delivery') or {}
            filename = current.get('output_file') or ''
            path = Path(filename) if filename else None
            state = job.get('state') or child.get('state')
            try:
                exists = bool(path and path.suffix.lower() == '.mp4' and path.is_file())
            except OSError:
                exists = False
            ready = state in ('COMPLETED', 'READY_FOR_REVIEW')
            status = ('COMPLETED' if exists else 'FAILED') if ready else (
                state if state in ('FAILED', 'CANCELLED', 'WAITING_FOR_INPUT', 'BLOCKED_RESOURCES') else 'PREPARING')
            error = ('成品文件缺失，当前无法打开。请回到任务与草稿检查产物记录。'
                     if ready and not exists else self.backend.safe_error(job.get('error') or child.get('error') or ''))
            rows.append({'output_id':child.get('output_id'), 'status':status,
                         'output_file':filename, 'duration_us':current.get('duration_us'),
                         'status_text':('已生成' if exists else '文件缺失') if ready else LABELS.get(state, '准备中'),
                         'missing_output':ready and not exists, 'error':error})
        return {'items':rows, 'batch':batch}

    def refresh_export_items(self):
        batch_id = self.export_batch.get().strip()
        report = self._selected_export_report()
        direct = self._direct_export_view()
        if getattr(self, '_export_last_batch', None) != batch_id:
            drafts = getattr(self, '_export_directory_drafts', {})
            previous = getattr(self, '_export_last_batch', None)
            if previous:
                drafts[previous] = self.export_output.get()
            self._export_last_batch = batch_id
            self._export_directory_drafts = drafts
            fallback = (self.snapshot.get('external_export_defaults') or {}).get(batch_id) or (report or {}).get('output_dir') or ''
            self.export_output.set(drafts.get(batch_id, fallback))
        selected = self.export_tree.selection()
        if direct is not None:
            rows = direct['items']
            folders = list(dict.fromkeys(str(Path(r['output_file']).parent) for r in rows if r.get('output_file')))
            self.export_output.set(folders[0] if folders else '')
            finished = sum(r['status'] == 'COMPLETED' for r in rows)
            self.export_report_var.set('已自动生成，无需再次导出。' if rows and finished == len(rows) else
                f'已自动生成 {finished} / {len(rows)} 条成品；其余请在任务与草稿查看处理原因，无需从主草稿再次导出。')
        elif not report:
            batch = self.batches.get(batch_id) or {}
            sections, warning = read_sections(batch)
            by_id = {row['output_id']: row for row in sections}
            rows = [{'output_id': child.get('output_id'), 'status': 'NOT_STARTED',
                     'time_range_us': [by_id[child['output_id']]['start_us'], by_id[child['output_id']]['end_us']] if child.get('output_id') in by_id else []}
                    for child in batch.get('children', [])]
            self.export_report_var.set('尚未开始导出。'+(' '+warning if warning else ''))
        else:
            rows = report.get('items', [])
            summary = report.get('summary') or {}
            self.export_report_var.set(f"成功 {summary.get('success', 0)}  /  失败 {summary.get('failed', 0)}  /  跳过 {summary.get('skipped', 0)}  /  取消 {summary.get('cancelled', 0)}")
        self.export_tree.heading('range', text='视频时长' if direct is not None else '绝对时间范围')
        ids = {row.get('output_id') for row in rows}
        for iid in self.export_tree.get_children():
            if iid not in ids:
                self.export_tree.delete(iid)
        for index, row in enumerate(rows):
            iid = row.get('output_id')
            if not iid:
                continue
            rng = row.get('time_range_us') or []
            duration = f'{timecode(rng[0])} — {timecode(rng[1])}' if len(rng) == 2 else '尚未确定'
            if direct is not None:
                duration = timecode(row['duration_us']) if type(row.get('duration_us')) is int else '未记录'
            status = '已跳过' if row.get('skipped') else EXPORT_LABELS.get(row.get('status'), '未开始')
            if row.get('missing_output'):
                status = '文件缺失'
            if direct is not None:
                status = row['status_text']
            tags = ('bad',) if row.get('status') == 'FAILED' else ('good',) if row.get('status') == 'COMPLETED' else ()
            values = (iid, status, duration, Path(row.get('output_file') or '').name)
            if self.export_tree.exists(iid):
                self.export_tree.item(iid, values=values, tags=tags)
                self.export_tree.move(iid, '', index)
            else:
                self.export_tree.insert('', index, iid=iid, values=values, tags=tags)
        target = selected[0] if selected and selected[0] in ids else (rows[0].get('output_id') if rows else None)
        if target and self.export_tree.selection() != (target,):
            self.export_tree.selection_set(target)
        finished = sum(r.get('status') == 'COMPLETED' and not r.get('skipped') for r in rows)
        self.export_metric.set(f'{finished:02d} / {len(rows):02d}')
        terminal = sum(r.get('status') in ('COMPLETED', 'FAILED', 'CANCELLED', 'SKIPPED') for r in rows)
        self.export_progress.configure(value=100*terminal/len(rows) if rows else 0)
        self.draw_export_error()
        self.export_action_states()

    def draw_export_error(self):
        report = self._selected_export_report(); selected = self.export_tree.selection()
        direct = self._direct_export_view()
        row = next((item for item in (direct if direct is not None else report or {}).get('items', []) if selected and item.get('output_id') == selected[0]), {})
        message = self.backend.safe_error(row['error']) if row.get('error') else ('该成品暂无错误记录。' if row else '选择成品以查看详情。')
        if row.get('output_file'):
            message += '\n输出：'+str(row['output_file'])
        if row.get('started_at'):
            message += '\n开始：'+local_time(row['started_at'])
        if report and report.get('error'):
            message += '\n批次：'+self.backend.safe_error(report['error'])
        self.export_error.configure(state='normal')
        self.export_error.delete('1.0', 'end')
        self.export_error.insert('end', message)
        self.export_error.configure(state='disabled')
        self.export_action_states()

    def pick_export_directory(self):
        if self._direct_export_view() is not None:
            return
        path = filedialog.askdirectory(parent=self.window, title='选择独立导出输出文件夹', initialdir=self.export_output.get() or None)
        if path:
            self.export_output.set(path)

    def open_export_directory(self):
        path = self.export_output.get().strip()
        direct = self._direct_export_view()
        if direct is not None:
            selected = self.export_tree.selection()
            row = next((r for r in direct['items'] if selected and r['output_id'] == selected[0]), {})
            if row.get('output_file'):
                path = str(Path(row['output_file']).parent)
        if not path or not Path(path).is_dir():
            self.notice('请先选择一个已存在的输出目录。', True); return
        os.startfile(path)

    def start_export(self):
        if self._direct_export_view() is not None:
            self.notice('直接成片任务会自动生成 MP4，无需再次独立导出。'); return
        batch_id = self.export_batch.get().strip(); output = self.export_output.get().strip()
        if not batch_id:
            self.notice('请先选择批次。', True); return
        if not output:
            self.notice('未选择输出目录，不能开始导出。', True); return
        self.run('正在启动 FFmpeg 独立导出…', lambda: self.backend.action({'action': 'export_batch', 'batch_id': batch_id, 'output_dir': output, 'conflict': self._export_conflict_value()}), lambda _: self.notice('独立导出已开始，可在此页查看每条成品状态。'))

    def cancel_export(self):
        batch_id = self.export_batch.get().strip()
        if batch_id:
            self.run('正在取消独立导出…', lambda: self.backend.action({'action': 'external_export_cancel', 'batch_id': batch_id}), lambda _: self.notice('已请求取消，当前片段结束后停止。'))

    def retry_export(self):
        batch_id = self.export_batch.get().strip(); selected = self.export_tree.selection()
        if not batch_id or not selected:
            self.notice('请先选择批次和失败的成品。', True); return
        self.run('正在重试选中的成品…', lambda: self.backend.action({'action': 'external_export_retry', 'batch_id': batch_id, 'output_id': selected[0]}), lambda _: self.notice('已重新加入独立导出队列。'))

    def _settings_page(self, parent):
        self.header(parent, '连接模型与本机资源', 'DeepSeek 负责分析和规划，本机 FFmpeg 直接生成 MP4，不需要剪映。')
        area = ttk.Frame(parent); area.pack(fill='both', expand=True)
        area.columnconfigure(0, weight=3); area.columnconfigure(1, weight=2); area.rowconfigure(0, weight=1)
        form = ttk.Frame(area, style='Card.TFrame', padding=18); form.grid(row=0, column=0, sticky='nsew', padx=(0, 12))
        checks = ttk.Frame(area, style='Card.TFrame', padding=18); checks.grid(row=0, column=1, sticky='nsew')
        ttk.Label(form, text='连接设置', style='Heading.TLabel').pack(anchor='w', pady=(0, 10))
        self.settings_vars = {}
        self.directory_buttons = []
        for key, title in [('gateway_url', '模型网关地址'), ('access_token', '工作台访问码（已保存则留空）')]:
            ttk.Label(form, text=title, style='Card.TLabel').pack(anchor='w', pady=(8, 4))
            row = ttk.Frame(form, style='Card.TFrame'); row.pack(fill='x')
            variable = tk.StringVar(); self.settings_vars[key] = variable
            entry = ttk.Entry(row, textvariable=variable, show='●' if key=='access_token' else '')
            entry.pack(side='left', fill='x', expand=True)
            if key.endswith('_dir') or key=='draft_root':
                button = self.button(row, '…', lambda k=key: self.pick_directory(k), side='right', padx=(5, 0))
                self.directory_buttons.append(button)
        ttk.Label(form, text='访问码保存在 Windows 凭据管理器。无需填写模型 API Key。', style='CardMuted.TLabel', wraplength=410).pack(anchor='w', pady=(10, 12))
        row = ttk.Frame(form, style='Card.TFrame'); row.pack(fill='x')
        self.button(row, '检测网关', self.check_gateway, side='left')
        self.button(row, '保存设置', self.save_settings, primary=True, side='right')
        self.gateway_result = tk.StringVar()
        ttk.Label(form, textvariable=self.gateway_result, style='CardMuted.TLabel', wraplength=400).pack(anchor='w', pady=(10, 0))
        ttk.Label(checks, text='环境自检', style='Heading.TLabel').pack(anchor='w', pady=(0, 12))
        self.checks_text = ScrolledText(checks, height=15, width=28, wrap='word', relief='flat', background='white', state='disabled')
        self.checks_text.pack(fill='both', expand=True)
        self.checks_text.tag_configure('good', foreground=SUCCESS)
        self.checks_text.tag_configure('bad', foreground=RED)
        ttk.Label(checks, text='DeepSeek：文案、画面证据与剪辑决策\nFFmpeg：本机直接渲染 MP4\n本地音色库：日语、英语新配音', style='CardMuted.TLabel', wraplength=280).pack(anchor='w', pady=(12, 0))


    def notice(self, message, error=False):
        self.notice_var.set(str(message))
        self.notice_label.configure(foreground=RED if error else MUTED)

    def _set_busy(self, value):
        self.busy = value
        self._sync_preview_entry()
        for button in self.buttons:
            button.state(['disabled'] if value else ['!disabled'])
        if value:
            self.progress.start(15)
        else:
            self.progress.stop()
            self._action_states()
        self.export_action_states()
        self.sync_material_controls()
        self._set_output_preview_buttons()
        self._template_action_states()

    def run(self, title, function, callback=None):
        if self.busy or self.closed:
            return
        self._set_busy(True)
        self._status_generation += 1
        self.notice(title)
        future = self.executor.submit(function)
        future.add_done_callback(lambda f: self.inbox.put(('operation', f, callback)))

    def _drain(self):
        if self.closed:
            return
        deadline = time.perf_counter() + .008
        while True:
            try:
                kind, future, callback = self.inbox.get_nowait()
            except queue.Empty:
                break
            if kind=='operation':
                self._status_generation += 1
                self._set_busy(False)
            elif kind == 'refresh':
                self.refresh_pending = False
            try:
                value = future.result()
                if callback:
                    callback(value)
                if self.closed:
                    return
            except CancelledError:
                # A new seek/selection cancels obsolete preview work. It is
                # normal control flow, not a failed user operation.
                pass
            except Exception as exc:
                self.notice(self.backend.safe_error(exc), True)
            if kind=='operation' and not self.closed:
                self.refresh()
            if time.perf_counter() >= deadline:
                break
        if not self.closed:
            self.later(1 if not self.inbox.empty() else 30, self._drain)

    def refresh(self):
        if self.refresh_pending or self.closed:
            return
        self.refresh_pending = True
        generation = self._status_generation
        future = self.status_executor.submit(self.backend.status)
        def apply(snapshot):
            if generation != self._status_generation:
                self.refresh()
                return
            self.render_status(snapshot, visible_only=True)
        future.add_done_callback(lambda f: self.inbox.put(('refresh', f, apply)))

    def _poll(self):
        if self.closed:
            return
        wake = self.state_dir/'desktop-wake.json'
        if wake.exists():
            wake.unlink(missing_ok=True)
            self.window.deiconify(); self.window.lift()
        self.refresh()
        self.later(4000, self._poll)

    def render_status(self, snapshot, *, visible_only=False):
        self.snapshot = snapshot
        if visible_only and time.perf_counter() < getattr(self, '_page_transition_until', 0):
            return  # show() coalesces pending snapshots after the animation.
        if not self.settings_loaded:
            for key, variable in self.settings_vars.items():
                if key!='access_token':
                    variable.set(snapshot.get('settings', {}).get(key, ''))
            self.settings_loaded = True
        checks_key = repr((snapshot.get('checks'), snapshot.get('state_dir'), snapshot.get('limitations')))
        if checks_key != getattr(self, '_checks_render_key', None):
            self.checks_text.configure(state='normal'); self.checks_text.delete('1.0', 'end')
            for item in snapshot.get('checks', []):
                self.checks_text.insert('end', ('✓  ' if item['ok'] else '!  ') + item['name']+'\n', 'good' if item['ok'] else 'bad')
                self.checks_text.insert('end', item['detail']+'\n\n')
            self.checks_text.insert('end', '本机工作目录\n'+str(snapshot.get('state_dir', self.state_dir))+'\n\n'+'\n\n'.join(snapshot.get('limitations', [])))
            self.checks_text.configure(state='disabled')
            self._checks_render_key = checks_key
        self.jobs = {job['job_id']: job for job in snapshot.get('jobs', [])}
        self.batches = {batch['batch_id']: batch for batch in snapshot.get('batches', [])
                        if batch.get('batch_id') != 'unknown'}
        section_revisions = []
        for batch in self.batches.values():
            path = (batch.get('finalization') or {}).get('master_snapshot')
            if path:
                try:
                    stat = Path(path).stat()
                    section_revisions.append((path, stat.st_mtime_ns, stat.st_size))
                except OSError:
                    section_revisions.append((path, 'unavailable'))
        # Progress events do not change list rows. Keep the full jobs fresh,
        # but only rebuild list presentation when its visible data changes.
        groups_key = repr(([(j.get('job_id'), j.get('state'), j.get('instruction'),
                             j.get('created_at'), j.get('batch')) for j in self.jobs.values()],
                           self.batches, section_revisions, self.group_tree.winfo_width()))
        render_tasks = not visible_only or self.current_view == 'tasks'
        render_export = not visible_only or self.current_view == 'export'
        if render_tasks and groups_key != getattr(self, '_groups_render_key', None):
            self.render_job_groups()
            self._groups_render_key = groups_key
        if render_tasks:
            self.draw_job()
        export_key = repr((snapshot.get('external_exports'), snapshot.get('external_export_defaults'),
                           self.batches, section_revisions, snapshot.get('busy_action'), self.busy, self.export_batch.get()))
        if render_export and export_key != getattr(self, '_export_render_key', None):
            self.refresh_export_page()
            self._export_render_key = export_key

    def selected_job(self):
        selected = self.task_tree.selection()
        return self.jobs.get(selected[0]) if selected else None

    def _action_states(self):
        actions = allowed_actions(self.selected_job())
        registration = ((self.selected_job() or {}).get('registration') or {})
        self.action_buttons['publish'].configure(text='重试登记' if registration.get('status')=='failed' else '立即登记')
        for action, button in self.action_buttons.items():
            button.state(['!disabled'] if action in actions and not self.busy and
                         not self.snapshot.get('busy_action') else ['disabled'])

    def draw_job(self):
        job = self.selected_job()
        detail_key = repr((job, self.batches.get(((job or {}).get('batch') or {}).get('batch_id')),
                           self.busy, self.snapshot.get('busy_action')))
        if detail_key == getattr(self, '_detail_render_key', None):
            self.detail.see('end')
            return
        state = (job or {}).get('state')
        color = RED if state == 'FAILED' else AMBER if state in ('WAITING_FOR_INPUT', 'BLOCKED_RESOURCES') else SUCCESS if state == 'COMPLETED' else TEXT
        self.task_state_var.set(job_label(job) if job else '未选择任务')
        self.task_state_label.configure(foreground=color)
        problem = (job or {}).get('error') or ((job or {}).get('direct_render_report') or {}).get('error')
        if problem and ('Runtime differs from the saved task' in str(problem) or
                        'Model, installation or tool version changed' in str(problem)):
            problem = '程序版本已更新，与此任务保存版本不同。请按原素材和要求新建任务；旧记录保留。重复点击继续无法解决版本差异。'
        self.task_problem_var.set(self.backend.safe_error(str(problem))[:180] if problem else
                                  '选择下方恢复操作，任务记录会保留。' if state in ('FAILED', 'WAITING_FOR_INPUT', 'BLOCKED_RESOURCES') else
                                  '点击播放预览，在软件内核对画面与配音。' if state == 'COMPLETED' else '')
        if state in ('FAILED', 'WAITING_FOR_INPUT', 'BLOCKED_RESOURCES'):
            self.task_problem_var.set(self.task_problem_var.get()+'\n点击「查看并修复」定位问题文案。')
        self.task_problem_label.configure(foreground=color if problem else MUTED)
        self._detail_job_id = job['job_id'] if job else None
        self.detail.configure(state='normal'); self.detail.delete('1.0', 'end')
        if not job:
            self.detail.insert('end', '选择任务以查看进度。\n添加素材后，新建剪辑任务将显示在这里。')
        else:
            self.detail.insert('end', job_label(job)+'\n', 'heading')
            self.detail.insert('end', job['instruction']+'\n\n任务编号：'+job['job_id']+'\n')
            batch_link = job.get('batch') or {}
            if batch_link:
                batch = self.batches.get(batch_link.get('batch_id'), {})
                completed = sum(self.jobs.get(c.get('job_id'),{}).get('state') == 'COMPLETED' for c in batch.get('children',[]))
                self.detail.insert('end', f"批次：{batch_link.get('batch_id')} · 成品 {batch_link.get('ordinal', '?')}\n"
                                   f"批次状态：{batch.get('state', '处理中')} · 已完成 {completed} / {len(batch.get('children', []))}\n")
                finalization = (batch.get('finalization') or {})
                if finalization.get('master_draft'):
                    self.detail.insert('end', '批次主草稿：'+str(finalization['master_draft'])+'\n')
                batch_registration = finalization.get('registration') or {}
                if batch_registration.get('homepage_index_registered'):
                    self.detail.insert('end', '批次主草稿已登记到剪映首页：'+
                                       str(batch_registration.get('registered_draft_path', ''))+'\n')
                exports = finalization.get('exports') or {}
                if exports.get('status') == 'temporary_drafts_ready_for_export':
                    self.detail.insert('end', '独立成品草稿已拆分，当前剪映导出接口不可用，需逐条导出或等待受支持接口。\n', 'warning')
            spec = job.get('task_spec') or {}
            if spec.get('target_country'):
                narration = spec.get('narration_mode')
                voice_note = ('（生成新口播）' if narration == 'new_native' else
                              '（保留原声）' if narration == 'preserve_source' else
                              '（不生成新口播）' if narration == 'text_only' or spec.get('copy_only') else '')
                self.detail.insert('end', '目标国家：'+str(spec['target_country'])+voice_note+'\n')
            publication = job.get('publication') or {}
            registration = job.get('registration') or {}
            if registration.get('message') and not publication.get('registered_draft_path'):
                self.detail.insert('end', '\n'+registration['message']+'\n',
                                   'warning' if registration.get('status')=='failed' else 'heading')
            if publication.get('registered_draft_path'):
                self.detail.insert('end', '\n草稿位置\n'+publication['registered_draft_path']+'\n')
                if not publication.get('full_quality_accepted'):
                    self.detail.insert('end', '技术登记完成，完整视听验收尚未通过；请在剪映检查后手动导出。\n', 'warning')
            output_file = self._job_output_file(job)
            if output_file:
                self.detail.insert('end', '\n成品 MP4\n'+output_file+'\n')
            if job.get('error'):
                self.detail.insert('end', '\n需要处理：'+self.backend.safe_error(job['error'])+'\n', 'bad' if job.get('state') == 'FAILED' else 'warning')
            render_report = job.get('direct_render_report') or {}
            if render_report.get('status') == 'FAILED' and render_report.get('error'):
                self.detail.insert('end', '\n独立渲染实际原因：'+self.backend.safe_error(render_report['error'])+'\n', 'bad')
            integrity = job.get('integrity_warnings') or []
            if integrity:
                self.detail.insert('end', f'历史产物完整性警告：{len(integrity)} 条旧记录未通过指纹校验，已保留并隔离。\n', 'warning')
            progress = job.get('native_progress')
            if progress:
                self.detail.insert('end', f"\n原生包装：已写入 {progress['completed']} / {progress['total']} 个元素。\n")
            issues = (job.get('review') or {}).get('issues') or []
            if issues:
                self.detail.insert('end', '\n此前抽样复核提醒（最终草稿仍需检查）\n', 'warning')
                for issue in issues[:8]:
                    self.detail.insert('end', '• '+str(issue.get('problem', ''))[:800]+'\n', 'warning')
            self.detail.insert('end', '\n最近进度\n', 'heading')
            events = job.get('events', [])
            # Insert the progress block once instead of crossing into Tcl for
            # every event. Preserve the complete history and Chinese messages.
            self.detail.insert('end', ''.join(
                local_time(event.get('timestamp'))+'  '+self.backend.safe_error(event_text(event))+'\n'
                for event in events))
        self.detail.configure(state='disabled')
        # The progress feed follows the newest entry after selection/refresh.
        # Keeping a fractional scroll offset left it stranded in older events.
        self.detail.see('end')
        self._action_states()
        self.sync_draft_panel()
        self._detail_render_key = detail_key

    def update_hint(self):
        label = self.workflow.get()
        workflow = WORKFLOWS[label]
        market = MARKET_PRESETS.get(label)
        hint = HINTS[workflow]
        if market:
            hint = f'{market}区跨境电商广告：' + hint
            self.target_country.set(market)
        self.workflow_hint.set(hint)
        for name in ('voice_library_button','font_library_button'):
            button=getattr(self,name,None)
            if button is not None:
                button.state(['disabled'] if workflow=='long_video_no_voice' else ['!disabled'])
        if hasattr(self, 'long_settings'):
            if workflow in ('long_video','long_video_no_voice'):
                self.long_settings.pack(fill='x',pady=(0,12),after=self.workflow_select)
            else:
                self.long_settings.pack_forget()
        self.packaging_var.set((('日语口播 · 温柔自然' if market == '日本' else '英语口播 · 轻快直接') +
            '\n新文案与卖点字幕 · 原声移除\n用户指令优先 · 缺配音待补资源') if market else
            ('原口播与原字幕保留\n前四十秒锁定 · 后续语义混剪\n全片添加转场、贴纸与动效' if workflow == 'long_video_no_voice' else
             '按主题与章节组织长视频\n最多 20 条 · 单条 7 分钟 · 成片 10 分钟\n默认保留原声，可要求生成中文口播' if workflow == 'long_video'
             else '以用户提示词为准\n不强制广告结构或新配音'))
        current = self.instruction.get('1.0', 'end').strip()
        if current in {default_instruction(WORKFLOWS[item], MARKET_PRESETS.get(item)) for item in WORKFLOWS}:
            self.instruction.delete('1.0', 'end')
            self.instruction.insert('1.0', default_instruction(workflow, market))

    def pick_files(self):
        if self.busy:
            return
        files = filedialog.askopenfilenames(parent=self.window, title='添加视频素材', filetypes=[('视频素材', '*.mp4 *.mov *.mkv *.m4v')])
        if files:
            self.add_files(files)

    def open_sticker_library(self):
        if self.busy:return
        existing=getattr(self,'sticker_picker',None)
        if existing and existing.window.winfo_exists():
            existing.window.lift();return
        from .desktop_stickers import StickerLibrary
        try:self.sticker_picker=StickerLibrary(self.window)
        except (OSError,ValueError,KeyError) as exc:self.notice('贴纸库不可用：'+str(exc),True)

    def open_font_library(self):
        if self.busy:
            return
        existing = getattr(self, 'font_picker', None)
        if existing and existing.window.winfo_exists():
            existing.window.lift()
            return
        self._pause_source_preview()
        if self._output_preview_playing:
            self._output_preview_playing = False
            self._stop_output_preview_stream()
            self._set_output_preview_buttons()
        from .desktop_fonts import FontPicker
        try:
            self.font_picker = FontPicker(self.window, self.selected_subtitle_font_id,
                                          self._apply_subtitle_font)
        except (OSError, ValueError, KeyError) as exc:
            self.notice('字体库不可用：' + str(exc), True)

    def _apply_subtitle_font(self, font_id, label):
        self.selected_subtitle_font_id = font_id
        short = label.split(' / ')[-1]
        self.font_library_button.configure(text='字体库 · ' + (short if len(short) <= 18 else short[:17] + '…'))
        self.notice('新任务字幕字体：' + label + '。' +
                    ('不支持的语言或字符将自动使用兼容字体。' if font_id else '按视频风格与字幕语言选择。'))

    def open_voice_library(self):
        existing = getattr(self, '_voice_library_dialog', None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        self._pause_source_preview()
        if self._output_preview_playing:
            self._output_preview_playing=False
            self._stop_output_preview_stream()
            self._set_output_preview_buttons()
        from .local_voice import library_root, catalog, inside
        service = getattr(self.backend, 'service', None)
        config = getattr(service, 'config', {})
        root = library_root(config) if isinstance(config, dict) else None
        if not root:
            self.notice('本机尚未安装本地音色库。', True)
            return
        try:
            data = catalog(root)
        except (OSError, ValueError, KeyError) as exc:
            self.notice('音色库不可用：' + str(exc), True)
            return
        market = MARKET_PRESETS.get(self.workflow.get())
        locale = {'日本':'ja-JP', '美国':'en-US'}.get(market,'zh-CN')
        voices = data['voices']
        dialog = tk.Toplevel(self.window)
        self._voice_library_dialog = dialog
        dialog.configure(background=BG)
        dialog.title('本地音色库 · 选择与试听')
        x=max(0,self.window.winfo_rootx()+(self.window.winfo_width()-860)//2)
        y=max(0,self.window.winfo_rooty()+(self.window.winfo_height()-560)//2)
        dialog.geometry(f'860x560+{x}+{y}')
        dialog.minsize(700, 420)
        dialog.transient(self.window)
        ttk.Label(dialog, text=f'本地音色库 · {len(voices)} 种音色',
                  style='Heading.TLabel').pack(anchor='w', padx=20, pady=15)
        ttk.Label(dialog, text='单击即选择并试听；中文、日语和英语分别记住所选音色。',
                  style='Muted.TLabel').pack(anchor='w', padx=20)
        frame = ttk.Frame(dialog); frame.pack(fill='both', expand=True, padx=20, pady=12)
        frame.rowconfigure(0, weight=1)
        trees = {}
        active_key = None
        def select_language(event):
            ids = event.widget.selection()
            if not ids or active_key == (event.widget, ids[0]):
                return
            choose_voice(event.widget, ids[0])
        def click_voice(event):
            tree = event.widget
            row = tree.identify_row(event.y)
            if row and tree.identify_region(event.x, event.y) == 'cell':
                tree.focus_set()
                choose_voice(tree, row)
                return 'break'
        languages=[(code,label) for code,label in (('zh-CN','中文'),('ja-JP','日语'),('en-US','英语'))
                   if any(v['locale']==code for v in voices)]
        for column, (language, label) in enumerate(languages):
            frame.columnconfigure(column, weight=1, uniform='voice_language')
            panel = ttk.Frame(frame)
            panel.grid(row=0, column=column, sticky='nsew', padx=(0, 10) if column == 0 else (10, 0))
            group = [v for v in voices if v['locale'] == language]
            ttk.Label(panel, text=f'{label} · {len(group)} 种', style='Heading.TLabel').pack(anchor='w', pady=(0, 10))
            listing = ttk.Frame(panel); listing.pack(fill='both', expand=True)
            tree = ttk.Treeview(listing, columns=('voice', 'chosen'), show='headings', selectmode='browse')
            tree.heading('voice', text='音色'); tree.column('voice', width=225, minwidth=130)
            tree.heading('chosen', text=''); tree.column('chosen', width=36, minwidth=36, stretch=False, anchor='center')
            scroll = ttk.Scrollbar(listing, orient='vertical', command=tree.yview)
            tree.configure(yscrollcommand=scroll.set)
            scroll.pack(side='right', fill='y'); tree.pack(fill='both', expand=True)
            for v in group:
                preferred = getattr(self, 'selected_voice_ids', {}).get(language) or data['defaults'].get(language)
                tree.insert('', 'end', iid=v['id'], values=(v['label'], '✓' if v['id'] == preferred else ''))
            tree.bind('<<TreeviewSelect>>', select_language)
            tree.bind('<Button-1>', click_voice)
            tree.bind('<Return>', lambda e: replay_selected(e.widget))
            tree.bind('<space>', lambda e: replay_selected(e.widget))
            trees[language] = tree
        controls = ttk.Frame(dialog); controls.pack(side='bottom',fill='x', padx=20,pady=(0,18),before=frame)
        credit_notice = ttk.Label(controls,text='',style='Muted.TLabel',wraplength=660)
        def update_credit(voice):
            credit_notice.configure(text=('发布视频时请署名：'+voice.get('attribution','')+'；成品旁会附署名说明。'
                if voice.get('attribution_required') else ''))
        def preview(voice):
            import winsound
            try:
                stop_preview()
                sample=inside(root, 'samples/'+voice['id']+'.wav')
                if not sample.is_file():
                    raise ValueError('试听文件尚未生成。')
                winsound.PlaySound(str(sample),winsound.SND_FILENAME|winsound.SND_ASYNC|winsound.SND_NODEFAULT)
                self.notice('已选择并正在试听：'+voice['label'])
            except (OSError, RuntimeError, ValueError) as exc:
                self.notice('已选择 '+voice['label']+'；试听失败：'+str(exc),True)
        def stop_preview():
            import winsound
            winsound.PlaySound(None,0)
        def close():
            stop_preview()
            if getattr(self, '_voice_library_dialog', None) is dialog:
                self._voice_library_dialog = None
                self._close_voice_library = None
            dialog.grab_release()
            dialog.destroy()
        self._close_voice_library = close
        def choose_voice(tree, voice_id):
            nonlocal active_key
            voice = next((v for v in voices if v['id'] == voice_id), None)
            if voice:
                # Set before generating selection events; each click plays exactly once.
                active_key = (tree, voice_id)
                tree.selection_set(voice_id)
                tree.focus(voice_id)
                for other in trees.values():
                    if other is not tree and other.selection():
                        other.selection_remove(*other.selection())
                self.selected_voice_ids = getattr(self, 'selected_voice_ids', {})
                self.selected_voice_ids[voice['locale']] = voice['id']
                if not market:
                    self.custom_voice_id = voice['id']
                for row in tree.get_children():
                    tree.set(row, 'chosen', '✓' if row == voice_id else '')
                update_credit(voice)
                preview(voice)
        def replay_selected(tree):
            ids = tree.selection()
            if ids:
                choose_voice(tree, ids[0])
            return 'break'
        ttk.Button(controls, text='停止',command=stop_preview).pack(side='left')
        credit_notice.pack(side='left',fill='x',expand=True,padx=(16,0))
        dialog.protocol('WM_DELETE_WINDOW',close)
        dialog.bind('<Escape>', lambda _: close())
        # One preview owns audio while choosing a voice. Prevent the parent
        # video controls from starting a second player underneath the dialog.
        dialog.grab_set()
        if voices:
            preferred = getattr(self, 'selected_voice_ids', {}).get(locale) or data['defaults'].get(locale)
            if not market:
                preferred = getattr(self, 'custom_voice_id', None) or preferred
            voice = next((v for v in voices if v['id'] == preferred), voices[0])
            active_tree = trees.get(voice['locale'])
            if active_tree is not None:
                active_key = (active_tree, voice['id'])
                active_tree.selection_set(voice['id'])
                active_tree.focus(voice['id'])
                active_tree.see(voice['id'])
                update_credit(voice)

    def open_subtitle_removal(self):
        existing = getattr(self, 'subtitle_removal_dialog', None)
        if existing and not existing.closed:
            existing.window.lift()
            return
        from .desktop_subtitle_removal import SubtitleRemovalDialog
        selected = self.asset_tree.selection()
        asset = next((a for a in self.assets if selected and a['asset_id'] == selected[0]), None)
        self.subtitle_removal_dialog = SubtitleRemovalDialog(self, self._asset_path(asset) if asset else None)

    def import_repaired_source(self, original, output):
        """Register first, then replace only this input in the unsaved selection."""
        if self.busy or self.closed:
            return
        original = Path(original).resolve()
        def added(assets):
            replaced = [a['asset_id'] for a in self.assets
                        if self._asset_path(a) and Path(self._asset_path(a)).resolve() == original]
            if not replaced and len(self.assets) >= MAX_MONTAGE_ASSETS:
                self.notice('去字幕成品已导出；当前素材已满，请移除一条后再导入。', True)
                return
            self.assets = [a for a in self.assets if a['asset_id'] not in replaced]
            for aid in replaced:
                self._asset_paths.pop(aid, None)
            self._removed_assets = []
            for asset in assets:
                self._asset_paths[asset['asset_id']] = str(output)
            self._added(assets)
            self.asset_tree.selection_set(assets[0]['asset_id'])
            self.preview_asset()
            self.show('create')
            self.notice('已使用去字幕视频替换当前素材；原始文件保留，可以继续自动剪辑。')
        self.run('正在导入去字幕成品…', lambda:self.backend.register_files([str(output)]), added)

    def add_files(self, paths):
        if len(paths)+len(self.assets)>MAX_MONTAGE_ASSETS:
            self.notice(f'每个任务最多添加 {MAX_MONTAGE_ASSETS} 条素材。', True)
            return
        paths = list(paths)
        def added(assets):
            for asset, path in zip(assets, paths):
                self._asset_paths[asset['asset_id']] = str(path)
            self._added(assets)
        self.run('正在读取素材信息…', lambda: self.backend.register_files(paths), added)

    def _added(self, assets):
        known = {a['asset_id'] for a in self.assets}
        for asset in assets:
            if asset['asset_id'] not in known:
                self.assets.append(asset); known.add(asset['asset_id'])
        self.draw_assets()
        self.notice('素材已添加，可以填写要求并开始剪辑。')

    def draw_assets(self):
        selected = self.asset_tree.selection()
        self.asset_tree.delete(*self.asset_tree.get_children())
        for asset in self.assets:
            self.asset_tree.insert('', 'end', iid=asset['asset_id'], values=(asset['name'], f"{asset['duration_us']/1e6:.1f} 秒", f"{asset['width']} × {asset['height']}"))
        self.asset_count.set(f'素材  {len(self.assets):02d}')
        sizes = {(a['width'], a['height']) for a in self.assets}
        self.source_format.set('画幅    '+('未添加素材' if not sizes else ' / '.join(f'{w} × {h}' for w, h in sorted(sizes))))
        if self.assets:
            remaining = [iid for iid in selected if self.asset_tree.exists(iid)]
            self.asset_tree.selection_set(remaining or [self.assets[0]['asset_id']])
        self.preview_asset()

    def remove_files(self, asset_ids=None):
        if self.busy:
            return
        selected = set(self.asset_tree.selection() if asset_ids is None else asset_ids)
        removed = [(i, dict(a), self._asset_paths.get(a['asset_id'])) for i, a in enumerate(self.assets) if a['asset_id'] in selected]
        if not removed:
            return
        self._removed_assets = removed
        for iid in selected:
            self._asset_paths.pop(iid, None)
        self.assets = [a for a in self.assets if a['asset_id'] not in selected]
        self.draw_assets()
        self.notice(f'已移除 {len(removed)} 条素材，可撤销；原文件保留。')

    def undo_remove_files(self):
        if self.busy or not self._removed_assets:
            return
        known = {a['asset_id'] for a in self.assets}
        restored = [row for row in self._removed_assets if row[1]['asset_id'] not in known]
        if len(self.assets)+len(restored) > MAX_MONTAGE_ASSETS:
            self.notice('当前素材数量已满，请先移除部分素材再撤销。', True)
            return
        for index, asset, path in restored:
            self.assets.insert(min(index,len(self.assets)),asset)
            if path:
                self._asset_paths[asset['asset_id']] = path
        self._removed_assets = []
        self.draw_assets()
        if restored:
            self.asset_tree.selection_set([row[1]['asset_id'] for row in restored])
            self.preview_asset()
        self.notice(f'已恢复 {len(restored)} 条素材。')

    def create_job(self):
        if self.busy:
            return
        label = self.workflow.get()
        workflow = WORKFLOWS[label]
        market = MARKET_PRESETS.get(label)
        instruction = self.instruction.get('1.0', 'end').strip() or default_instruction(workflow, market)
        if workflow == 'custom' and not instruction:
            self.notice('请先填写自定义剪辑要求。', True)
            self.instruction.focus_set()
            return
        if len(instruction)>10000:
            self.notice('剪辑要求请控制在 10,000 字以内。', True)
            return
        try:
            output_count = int(self.batch_count.get())
        except (TypeError, ValueError, tk.TclError):
            self.notice(f'批量成品数量需为 1–{MAX_BATCH_OUTPUTS}。', True)
            return
        if not 1 <= output_count <= MAX_BATCH_OUTPUTS:
            self.notice(f'批量成品数量需为 1–{MAX_BATCH_OUTPUTS}。', True)
            return
        payload = {'workflow': workflow, 'instruction': instruction, 'asset_ids': [a['asset_id'] for a in self.assets]}
        if self.selected_subtitle_font_id:
            payload['subtitle_font_id'] = self.selected_subtitle_font_id
        if workflow in ('custom','long_video') and getattr(self, 'custom_voice_id', None):
            payload['direct_media'] = {'voice_id':self.custom_voice_id}
        if workflow in ('long_video','long_video_no_voice'):
            selected = getattr(self, 'long_duration', None)
            value = selected.get() if selected else '自动（最多10分钟）'
            minutes = {'1分钟':1,'3分钟':3,'5分钟':5,'10分钟':10}.get(value)
            if minutes:
                payload['target_duration_ms'] = minutes*60_000
        if output_count > 1:
            payload['output_count'] = output_count
        if workflow == 'crossborder_montage':
            country = market
            if not country:
                self.notice('请选择日本区或美国区广告类别。', True)
                return
            payload['target_country'] = country
            voice = getattr(self, 'selected_voice_ids', {}).get({'日本':'ja-JP','美国':'en-US'}.get(country))
            if voice:
                payload['direct_media'] = {'voice_id':voice}
        self._creating_payload = dict(payload)
        self.run('正在创建批量任务…' if output_count > 1 else '正在创建任务…',
                 lambda: self.backend.create_job(payload), self._created)

    def _created(self, result):
        self._group = result.get('batch_id') or result.get('job_id')
        self._removed_assets = []
        self._asset_paths.clear()
        self.assets = []
        # Show confirmed IDs immediately, without waiting for a full historical
        # snapshot. These queued rows are replaced by authoritative polling.
        payload = getattr(self, '_creating_payload', {})
        created = datetime.now().astimezone().isoformat()
        children = result.get('children') or ([{'job_id':result['job_id']}] if result.get('job_id') else [])
        new_jobs = []
        for child in children:
            row = {'job_id':child['job_id'], 'state':'CREATED', 'created_at':created,
                   'instruction':payload.get('instruction', ''), 'events':[],
                   'render_backend':'direct_ffmpeg', 'workflow':payload.get('workflow')}
            if result.get('batch_id'):
                row['batch'] = {**child, 'batch_id':result['batch_id']}
            new_jobs.append(row)
        snapshot = {**self.snapshot, 'jobs':new_jobs + list(self.jobs.values())}
        if result.get('batch_id'):
            snapshot['batches'] = [{**result, 'created_at':created}] + list(self.batches.values())
        self.snapshot = snapshot
        already_on_tasks = self.current_view == 'tasks'
        self.show('tasks')
        if already_on_tasks:
            self.later(1, lambda: self.render_status(self.snapshot, visible_only=True))
        self.later(20, self.draw_assets)
        self.notice('任务已加入队列，将按顺序直接生成 MP4；不启动剪映、不修改草稿。')

    def pick_directory(self, key):
        path = filedialog.askdirectory(parent=self.window, title='选择目录', initialdir=self.settings_vars[key].get() or None)
        if path:
            self.settings_vars[key].set(path)

    def save_settings(self):
        data = {key: variable.get().strip() for key, variable in self.settings_vars.items()}
        # Clear the visible control immediately; only the credential store persists it.
        self.settings_vars['access_token'].set('')
        self.run('正在保存设置…', lambda: self.backend.save_settings(data), lambda _: self.notice('设置已保存。访问码保存在本机 Windows 凭据管理器。'))

    def check_gateway(self):
        self.run('正在检测模型网关…', self.backend.gateway_status, self._gateway_result)

    def _gateway_result(self, result):
        self.gateway_result.set(f"网关已连接。今日已用 {result['used_units']} / {result['daily_units']} 请求单位（UTC {result['day']}）。\nMax 每次计 15 单位，其余模型每次 1 单位；这不是人民币账单。")
        self.notice('网关连接正常，本次检测没有调用模型。')

    def change_output_directory(self):
        if self.busy:
            return
        current = (self.snapshot.get('direct_output_dir') or
                   self.snapshot.get('settings', {}).get('direct_output_dir') or
                   getattr(self, '_draft_path', None))
        options = {'parent': self.window, 'title': '选择新任务的默认成品目录', 'mustexist': True}
        if current:
            options['initialdir'] = str(current)
        folder = filedialog.askdirectory(**options)
        if not folder:
            return
        def saved(result):
            self.snapshot['direct_output_dir'] = result['output_dir']
            self.notice('新任务成品目录已改为：'+result['output_dir']+'；已有任务和成品位置不变。')
        self.run('正在保存成品目录…', lambda: self.backend.save_output_directory(folder), saved)

    def job_action(self, action):
        job = self.selected_job()
        if action == 'open_folder' and not self.busy and getattr(self, '_draft_path', None) and job.get('render_backend') == 'direct_ffmpeg':
            self._open_path(self._draft_path, '草稿目录不存在。')
            return
        if action not in allowed_actions(job) or self.busy:
            return
        if action == 'change_output_dir':
            self.change_output_directory()
            return
        if action == 'diagnose':
            def show(data):
                from .desktop_recovery import TaskRepairDialog
                existing = getattr(self, 'task_repair_dialog', None)
                if existing and existing.window.winfo_exists():
                    existing.close()
                self._pause_source_preview()
                self._output_preview_playing = False
                self._stop_output_preview_stream()
                self._set_output_preview_buttons()
                self.task_repair_dialog = TaskRepairDialog(self, data)
            self.run('读取任务检查点与问题文案…',
                     lambda: self.backend.action({'action':'diagnose', 'job_id':job['job_id']}), show)
            return
        if action=='resume' and not messagebox.askyesno('继续执行', '将从可用检查点继续。后续模型调用会消耗访问额度。继续执行？', parent=self.window):
            return
        payload = {'job_id': job['job_id'], 'action': action}
        if action == 'retry_batch_child':
            payload.update({'batch_id': job['batch']['batch_id'], 'output_id': job['batch']['output_id']})
        self.run('正在处理任务操作…', lambda: self.backend.action(payload), lambda _: self.notice('操作已提交。'))

    def on_close(self):
        removal = getattr(self, 'subtitle_removal_dialog', None)
        if removal and not removal.closed and removal.busy:
            removal.window.lift()
            self.notice('去字幕仍在处理，请等待完成，或在去字幕窗口取消后再退出。', True)
            return
        if self.busy:
            messagebox.showinfo('操作尚未结束', '请等待当前读取、保存或连接操作完成。你可以先最小化窗口。', parent=self.window)
            return
        def check():
            jobs = self.backend.jobs()
            return {'publishing': bool(self.backend.busy_action), 'running': self.backend.active(),
                    'queued': any(j['state']=='CREATED' for j in jobs),
                    'registration_pending': any(j['state']=='READY_FOR_REVIEW' and not j.get('publication')
                                                for j in jobs)}
        self.run('检查退出状态…', check, self._finish_close)

    def _finish_close(self, state):
        if state['publishing']:
            self.notice('草稿仍在登记，请完成后退出。可先最小化窗口。', True)
            return
        if (state['running'] or state['queued'] or state.get('registration_pending')):
            message = '已启动的剪辑会在后台继续；排队任务将在下次启动工作台时继续。'
            if state.get('registration_pending'):
                message += '\n自动登记将在下次启动工作台时继续。'
            message += '\n\n要关闭窗口吗？如需停止剪辑，请返回后使用“取消任务”。'
            if not messagebox.askyesno('关闭工作台', message, parent=self.window):
                return
        self.shutdown()

    def shutdown(self):
        close_voice_library = getattr(self, '_close_voice_library', None)
        if close_voice_library is not None:
            close_voice_library()
        removal = getattr(self, 'subtitle_removal_dialog', None)
        if removal and not removal.closed:
            removal.close(force=True)
        picker = getattr(self, 'font_picker', None)
        if picker and picker.window.winfo_exists():
            picker.close()
        stickers=getattr(self,'sticker_picker',None)
        if stickers and stickers.window.winfo_exists():stickers.close()
        self.closed = True
        self._output_preview_playing = False
        self._stop_output_preview_stream()
        self._source_preview_playing = False
        self._stop_source_preview_stream()
        self.backend.stop.set()
        for token in self.timers:
            self.window.after_cancel(token)
        self.timers.clear()
        self.progress.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.status_executor.shutdown(wait=False, cancel_futures=True)
        self.preview_executor.shutdown(wait=False, cancel_futures=True)
        self.window.destroy()


def wake_existing(state_dir):
    """Ask only this app's verified live desktop process to raise its own window."""
    try:
        import psutil
        receipt = json.loads((state_dir/'desktop-status.json').read_text(encoding='utf-8'))
        process = psutil.Process(receipt['pid'])
        command = process.cmdline()
        if abs(process.create_time()-receipt['process_created'])>1:
            return False
        if not any(Path(p).name=='Launch-Desktop.pyw' or p=='agent_core.desktop_workbench' for p in command):
            return False
        atomic_json(state_dir/'desktop-wake.json', {'requested': True})
        return True
    except (OSError, ValueError, KeyError, psutil.Error):
        return False


def guard_runtime_change(state_dir):
    """A package switch must not overwrite a live worker's runtime configuration."""
    config_path = state_dir/'runtime-config.json'
    if not config_path.exists():
        return
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if Path(config.get('toolchain_root', '')).resolve() == (ROOT/'toolchain').resolve():
        return
    import psutil
    for receipt_path in (state_dir/'tasks/jobs').glob('*/background-latest.json'):
        try:
            data = json.loads(receipt_path.read_text(encoding='utf-8'))
            process = psutil.Process(data['pid'])
            command = process.cmdline()
            if 'agent_core.background' in command and receipt_path.parent.name in command and data['worker_id'] in command:
                raise RuntimeError('旧版本仍有剪辑任务运行。请等待任务结束，再启动新版桌面工作台。')
        except (OSError, ValueError, KeyError, psutil.Error):
            continue


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default_state = os.environ.get('JIANYING_AGENT_STATE_DIR')
    if not default_state:
        default_state = 'D:/JianyingAgentWork/Workbench' if Path('D:/').is_dir() else \
            os.environ.get('SystemDrive', 'C:') + '/JianyingAgentWork/Workbench'
    parser.add_argument('--state-dir', default=default_state)
    args = parser.parse_args()
    state_dir = Path(args.state_dir).resolve()
    if wake_existing(state_dir):
        return
    if os.name=='nt':
        import ctypes
        ctypes.windll.user32.SetProcessDPIAware()
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Framo.Studio')
    window = tk.Tk(); window.withdraw()
    try:
        # Lock BEFORE Workbench config writes, including when a legacy web window exists.
        with draft_lock(state_dir/'.workbench.lock'):
            guard_runtime_change(state_dir)
            backend = Workbench(state_dir)
            app = DesktopWorkbench(window, backend)
            import psutil
            receipt = {
                'pid': os.getpid(), 'process_created': psutil.Process().create_time(),
                'version': VERSION, 'ui': 'native_tk', 'browser_required': False,
                'http_server': False, 'package_root': str(ROOT), 'state_dir': str(state_dir),
            }
            atomic_json(state_dir/'desktop-status.json', receipt)
            threading.Thread(target=backend.scheduler, daemon=True).start()
            window.deiconify()
            def ready():
                window.update_idletasks()
                atomic_json(state_dir/'desktop-status.json', {**receipt, 'window_state': window.state(),
                    'window_mapped': bool(window.winfo_ismapped()), 'title': window.title(),
                    'width': window.winfo_width(), 'height': window.winfo_height()})
            app.later(250, ready)
            window.mainloop()
            backend.stop.set()
    except Exception as exc:
        # Do not print credentials even if initialization fails.
        message = Workbench.safe_error(None, exc)
        if 'already being written' in str(exc):
            message = '已有工作台正在运行，请退出旧版浏览器工作台后再启动桌面版。'
        messagebox.showerror('工作台未能启动', message, parent=window)
        window.destroy()


if __name__=='__main__':
    main()
