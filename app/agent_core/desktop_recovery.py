"""Task failure explanation and editable saved copy, using the existing studio UI."""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk

from . import desktop_studio as studio


class TaskRepairDialog:
    def __init__(self, owner, data):
        self.owner, self.data = owner, data
        self.working = False
        self.current = None
        self.window = tk.Toplevel(owner.window)
        self.window.withdraw()
        self.window.title('修改文案并重新生成')
        self.window.configure(background=studio.BG)
        self.window.transient(owner.window)
        self.window.minsize(840, 560)
        width, height = 960, min(700, max(580, owner.window.winfo_height()-60))
        x = max(0, owner.window.winfo_rootx()+(owner.window.winfo_width()-width)//2)
        y = max(0, owner.window.winfo_rooty()+(owner.window.winfo_height()-height)//2)
        self.window.geometry(f'{width}x{height}+{x}+{y}')
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Escape>', lambda _: self.close())

        top = ttk.Frame(self.window, padding=(20, 12, 20, 0)); top.pack(fill='x')
        ttk.Label(top, text='修改文案并重新生成', style='Heading.TLabel').pack(anchor='w')
        self.summary = tk.StringVar()
        ttk.Label(top, textvariable=self.summary, style='Muted.TLabel').pack(anchor='w', pady=(4, 6))
        self.guide_title = tk.StringVar()
        ttk.Label(top, textvariable=self.guide_title, foreground=studio.AMBER).pack(anchor='w', pady=(0, 8))
        self.reason = owner._text(top, height=2)
        self.reason.pack(fill='x')
        self.reason.configure(foreground=studio.AMBER)
        self.advice = tk.StringVar()
        self.advice_label = ttk.Label(top, textvariable=self.advice, wraplength=920, justify='left')
        self.advice_label.pack(fill='x', pady=(8, 0))
        top.bind('<Configure>', lambda e: self.advice_label.configure(wraplength=max(500, e.width-40)))

        footer = ttk.Frame(self.window, padding=(20, 4, 20, 12))
        footer.pack(side='bottom', fill='x')
        self.message = tk.StringVar()
        ttk.Label(footer, textvariable=self.message, foreground=studio.AMBER,
                  wraplength=900).pack(fill='x', pady=(0, 4))
        ttk.Label(footer, text='AI 改写、翻译和重新生成会使用额度；只保存文案不会。建议仍需重新审核。',
                  style='Muted.TLabel').pack(anchor='w', pady=(0, 8))
        actions = ttk.Frame(footer); actions.pack(fill='x')
        self.close_button = studio.CutButton(actions, '关闭', self.close, width=80)
        self.close_button.pack(side='right')
        self.continue_button = studio.CutButton(actions, '保存并重新生成', lambda: self.save(True), primary=True, width=168)
        self.continue_button.pack(side='right', padx=8)
        self.save_button = studio.CutButton(actions, '只保存文案', self.save, width=116)
        self.save_button.pack(side='right')

        tools = ttk.Frame(self.window, padding=(20, 8, 20, 0)); tools.pack(fill='x')
        self.rewrite_button = studio.CutButton(tools, 'AI 帮我改写', self.rewrite, width=144)
        self.rewrite_button.pack(side='left', padx=(0, 8))
        self.translate_button = studio.CutButton(tools, '自动翻译问题字幕', self.translate, width=168)
        self.translate_button.pack(side='left')
        self.new_button = studio.CutButton(tools, '带入原素材重新创建', self.prefill, width=180)
        self.new_button.pack(side='left', padx=8)
        self.copy_button = studio.CutButton(tools, '复制错误详情', self.copy_report, width=132)
        self.copy_button.pack(side='left')
        style = ttk.Style(self.window)
        style.configure('Repair.TNotebook', background=studio.BG, borderwidth=0,
                        bordercolor=studio.LINE, lightcolor=studio.LINE, darkcolor=studio.LINE)
        style.configure('Repair.TNotebook.Tab', background=studio.FIELD, foreground=studio.TEXT,
                        bordercolor=studio.LINE, lightcolor=studio.LINE, darkcolor=studio.LINE,
                        padding=(16, 8))
        style.map('Repair.TNotebook.Tab', background=[('selected', studio.SELECT)])
        self.tabs = ttk.Notebook(self.window, style='Repair.TNotebook')
        self.tabs.pack(fill='both', expand=True, padx=20, pady=(8, 0))
        captions, narration, details = (ttk.Frame(self.tabs, padding=12) for _ in range(3))
        self.tabs.add(captions, text='字幕 · 逐条修改')
        self.tabs.add(narration, text='口播 · 直接输入')
        self.tabs.add(details, text='详细日志')
        self.tabs.bind('<<NotebookTabChanged>>', self.select_tab)
        captions.columnconfigure(1, weight=1); captions.rowconfigure(0, weight=1)
        listing = ttk.Frame(captions)
        listing.grid(row=0, column=0, sticky='ns', padx=(0, 16))
        self.tree = ttk.Treeview(listing, columns=('status',), show='tree headings', selectmode='browse', height=4)
        self.tree.heading('#0', text='字幕'); self.tree.heading('status', text='状态')
        self.tree.column('#0', width=180, stretch=False); self.tree.column('status', width=80, stretch=False)
        scroll = ttk.Scrollbar(listing, orient='vertical', command=self.tree.yview)
        scroll.pack(side='right', fill='y');self.tree.pack(side='left', fill='both', expand=True)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.tag_configure('problem', foreground=studio.AMBER)
        editor = ttk.Frame(captions);editor.grid(row=0, column=1, sticky='nsew')
        self.caption_title = tk.StringVar(value='选择一条字幕')
        ttk.Label(editor, textvariable=self.caption_title, style='Heading.TLabel').pack(anchor='w', pady=(0, 8))
        self.caption_hint = tk.StringVar()
        ttk.Label(editor, textvariable=self.caption_hint, wraplength=540, justify='left', foreground=studio.AMBER).pack(fill='x', pady=(0, 8))
        self.heading = owner._text(editor, height=3);self.heading.pack(fill='both', expand=True)
        ttk.Label(editor, text='在框内直接输入或粘贴。字幕可以与口播不同，不会改变镜头顺序。', style='Muted.TLabel').pack(anchor='w', pady=(8, 0))
        self.narration_meta = tk.StringVar()
        ttk.Label(narration, text='在下方直接输入或粘贴完整口播；不要填写镜头编号或审核说明。', style='Muted.TLabel').pack(anchor='w', pady=(0, 8))
        self.narration_meta_label = ttk.Label(narration, textvariable=self.narration_meta, style='Muted.TLabel')
        self.narration_meta_label.pack(anchor='w', pady=(0, 8))
        self.narration = owner._text(narration, height=5);self.narration.pack(fill='both', expand=True)
        self.report = owner._text(details, height=6);self.report.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select_caption)
        self.narration.bind('<<Modified>>', self.update_counts)
        self.heading.bind('<<Modified>>', self.update_counts)
        self.load(data)
        self.window.deiconify();self.window.update_idletasks();self.window.grab_set()

    def load(self, data):
        self.data, self.current = data, None
        self.message.set('')
        self.headings = {r['clip_index']:dict(r) for r in data.get('headings', [])}
        locale = {'en-US':'英语', 'ja-JP':'日语', 'zh-CN':'中文'}.get(data.get('locale'), data.get('locale', '自动'))
        status = '已生成成片' if data.get('state') == 'COMPLETED' else '尚未生成成片'
        self.summary.set(f"目标语言：{locale}  ·  已尝试恢复 {data.get('resume_count', 0)} 次  ·  {status}")
        self.reason.configure(state='normal');self.reason.delete('1.0', 'end')
        from .task_recovery import repair_guide
        guide = data.get('guide') or repair_guide(data)
        self.guide_title.set(guide['title'])
        problems = guide.get('problems', [])
        detail = '\n'.join(('原句：'+p['quote']+'\n' if p.get('quote') else '')+'需要修改：'+p['problem'] for p in problems[:4])
        self.reason.insert('1.0', detail or data['reason']);self.reason.configure(state='disabled')
        self.advice.set('  '.join(f'{i}. {step}' for i,step in enumerate(guide['steps'],1)))
        self.tree.delete(*self.tree.get_children())
        for index, row in self.headings.items():
            self.tree.insert('', 'end', iid=str(index), text=f"{index:02d} · {row['heading'][:24]}",
                             values=('请修改' if row.get('problem') else '原文',),
                             tags=('problem',) if row.get('problem') else ())
        self.narration.configure(state='normal');self.narration.delete('1.0', 'end')
        self.narration.insert('1.0', data.get('narration_script', ''))
        self.update_counts()
        self.report.configure(state='normal');self.report.delete('1.0', 'end')
        self.report.insert('1.0', self.report_text());self.report.configure(state='disabled')
        if self.headings:
            index = next((i for i,row in self.headings.items() if row.get('problem')), next(iter(self.headings)))
            self.tree.selection_set(str(index));self.tree.see(str(index));self.select_caption()
        self.controls()
        if (data.get('legacy_narration_recovered') or
                (not any(row.get('problem') for row in self.headings.values())
                 and any(word in data.get('reason', '') for word in ('旁白', '口播', '精简', '时长')))):
            self.tabs.select(1)
        self.tabs.select({'captions':0, 'narration':1, 'details':2}.get(guide['focus'], 1))
        if not data.get('editable'):
            self.message.set(data['advice'])

    def select_tab(self, _event=None):
        # Switching tabs must use the current service decision, including
        # reloading a successfully migrated legacy narration checkpoint.
        self.controls()
        if (self.tabs.index('current') == 1 and self.data.get('editable') and not self.working):
            self.narration.focus_set()

    def controls(self):
        editable = bool(self.data.get('editable')) and not self.working
        for button in (self.save_button, self.continue_button):
            button.state(['!disabled'] if editable else ['disabled'])
        self.rewrite_button.state(['!disabled'] if editable else ['disabled'])
        self.translate_button.state(['!disabled'] if editable and any(r.get('problem') for r in self.headings.values()) else ['disabled'])
        for button in (self.copy_button, self.new_button, self.close_button):
            button.state(['disabled'] if self.working else ['!disabled'])
        self.heading.configure(state='normal' if editable else 'disabled')
        self.narration.configure(state='normal' if editable else 'disabled')

    def update_counts(self, _event=None):
        from .market_rules import speech_units
        for widget in (self.narration, self.heading):
            widget.edit_modified(False)
        text = self.narration.get('1.0', 'end-1c')
        locale = self.data.get('locale', 'zh-CN')
        count = speech_units({'market_rules':{'locale':locale}}, text)
        unit = '词' if locale == 'en-US' else '字'
        limit = self.data.get('target_units')
        label = f'已写 {count} {unit}' + (f' / 建议最多 {limit} {unit}' if limit else ' · 配音后仍会实测时长')
        if not text.strip():label += ' · 口播不能为空'
        elif limit and count > limit:label += f' · 建议再删 {count-limit} {unit}'
        self.narration_meta.set(label)
        self.narration_meta_label.configure(foreground=studio.AMBER if not text.strip() or (limit and count>limit) else studio.MUTED)
        row = self.headings.get(self.current, {}) if hasattr(self, 'headings') else {}
        length = len(self.heading.get('1.0', 'end-1c'))
        language = {'en-US':'英语','ja-JP':'日语','zh-CN':'中文'}.get(locale, '目标语言')
        maximum = row.get('max_length', 36)
        self.caption_hint.set(f'请使用{language}，已写 {length} 个字符，最多 {maximum} 个。' +
            ('  这条字幕需要修改语言。' if row.get('problem') else '') +
            ('  超出上限，请精简。' if length>maximum else ''))

    def rewrite(self):
        self.flush()
        def apply(result):
            for row in result['headings']:
                index = row['clip_index']
                self.headings[index]['heading'] = row['heading']
                self.tree.item(str(index), text=f"{index:02d} · {row['heading'][:24]}", values=('建议待保存',))
            self.current = None
            if self.tree.selection():self.select_caption()
            self.narration.configure(state='normal');self.narration.delete('1.0', 'end')
            self.narration.insert('1.0', result['narration_script'])
            self.data['target_units'] = result.get('target_units')
            self.update_counts();self.tabs.select(1)
            self.message.set('AI 建议已填入，尚未保存或通过审核。确认后点击「保存并重新生成」。')
        self.submit({'action':'suggest_script_rewrite', 'job_id':self.data['job_id'],
                     'checkpoint_id':self.data['checkpoint_id'],
                     'draft':{'headings':list(self.headings.values()),
                              'narration_script':self.narration.get('1.0', 'end-1c')}}, apply)

    def flush(self):
        if self.current in self.headings:
            self.headings[self.current]['heading'] = self.heading.get('1.0', 'end-1c')

    def select_caption(self, _event=None):
        rows = self.tree.selection()
        if not rows:
            return
        self.flush()
        self.current = int(rows[0])
        self.heading.configure(state='normal');self.heading.delete('1.0', 'end')
        self.heading.insert('1.0', self.headings[self.current]['heading'])
        self.caption_title.set('第 '+str(self.current)+' 条字幕 · 可直接修改' if self.data.get('editable') else '第 '+str(self.current)+' 条字幕 · 当前只读')
        self.update_counts()
        self.controls()

    def submit(self, payload, callback):
        if self.working or self.owner.busy:
            return
        self.working = True;self.controls();self.message.set('正在处理，请稍候…')
        def run():
            try:
                return {'ok':True, 'value':self.owner.backend.action(payload)}
            except Exception as exc:
                return {'ok':False, 'error':self.owner.backend.safe_error(exc)}
        def finish(result):
            self.working = False
            if not self.window.winfo_exists():
                return
            self.controls()
            if result['ok']:
                callback(result['value'])
            else:
                self.message.set(result['error'])
        self.owner.run('正在处理任务修订…', run, finish)

    def translate(self):
        self.flush()
        def apply(result):
            for row in result['caption_fixes']:
                self.headings[row['clip_index']]['heading'] = row['heading']
                self.tree.item(str(row['clip_index']), text=f"{row['clip_index']:02d} · {row['heading'][:24]}", values=('建议待保存',))
            if self.current:
                self.heading.configure(state='normal');self.heading.delete('1.0', 'end')
                self.heading.insert('1.0', self.headings[self.current]['heading'])
            self.message.set('翻译建议已填入；确认后点击「保存并重新生成」。')
        self.submit({'action':'fix_script_language', 'job_id':self.data['job_id'],
                     'checkpoint_id':self.data['checkpoint_id']}, apply)

    def save(self, continue_job=False):
        self.flush()
        if not self.narration.get('1.0', 'end-1c').strip():
            self.message.set('口播还没有内容。请直接输入完整口播，或点击「AI 帮我改写」。')
            self.tabs.select(1);self.narration.focus_set();return
        payload = {'action':'save_script_repair', 'job_id':self.data['job_id'],
                   'checkpoint_id':self.data['checkpoint_id'], 'headings':list(self.headings.values()),
                   'narration_script':self.narration.get('1.0', 'end-1c'), 'continue':continue_job}
        def saved(result):
            if result.get('queued'):
                self.owner.notice('修订已保存，任务已排队重新检查；旧镜头与检查点保留。')
                self.close()
            else:
                self.load(result['diagnostics'])
                self.message.set('文案已保存，尚未重新审核。点击「保存并重新生成」继续。')
        self.submit(payload, saved)

    def report_text(self):
        return ('任务：'+self.data['job_id']+'\n原因：'+self.data['reason']+'\n建议：'+self.data['advice']+
                '\n\n字幕：\n'+'\n'.join(str(i)+'：'+r['heading'] for i,r in self.headings.items())+
                '\n\n旁白：\n'+self.data.get('narration_script', ''))

    def copy_report(self):
        self.flush()
        self.data['narration_script'] = self.narration.get('1.0', 'end-1c')
        self.window.clipboard_clear();self.window.clipboard_append(self.report_text())
        self.message.set('排查信息已复制，可以直接粘贴发送。')

    def prefill(self):
        from .desktop_workbench import WORKFLOWS, MARKET_PRESETS
        task = self.data.get('source_task') or {}
        label = next((name for name,flow in WORKFLOWS.items() if flow == task.get('workflow')
                      and (flow != 'crossborder_montage' or MARKET_PRESETS.get(name) == task.get('target_country'))), '自定义剪辑')
        self.owner.workflow.set(label);self.owner.update_hint()
        self.owner.instruction.delete('1.0', 'end');self.owner.instruction.insert('1.0', task.get('instruction', ''))
        self.owner.assets = [];self.owner._removed_assets = [];self.owner._asset_paths.clear()
        self.owner._added(self.data.get('assets', []))
        self.owner.batch_count.set(1)
        if task.get('subtitle_font_id'):
            from .font_library import bundled_font_library
            family = bundled_font_library().families.get(task['subtitle_font_id'])
            if family:self.owner._apply_subtitle_font(family['id'], family['label'])
        else:
            self.owner._apply_subtitle_font(None, '自动匹配')
        self.owner.show('create');self.owner.notice('已带入原素材和剪辑要求，请检查、修改后创建新任务。旧任务保留。')
        self.close()

    def close(self):
        if self.working:
            self.message.set('当前操作尚未结束，请稍候。');return
        if self.window.winfo_exists():
            self.window.grab_release();self.window.destroy()
