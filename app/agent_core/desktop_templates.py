"""Native template library, with upstream-backed planning and explicit export."""
from pathlib import Path
import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .motion_templates import TemplateLibrary, TemplateRunner, model_generator, read_json, STATES

FACE_FREE_PREVIEW_FAMILIES = frozenset({'01', '02', '03'})


class TemplateLibraryView:
    def _templates_page(self, parent):
        from .desktop_studio import BG, PANEL, MUTED, UI_FONT
        self.template_library = TemplateLibrary(self.state_dir)
        self.template_cancel = threading.Event()
        self.template_families = self.template_library.catalog()
        self.template_job = None
        self.template_media = []
        parent.columnconfigure(0, minsize=235, weight=0)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)
        rail = ttk.Frame(parent, padding=(16,20))
        rail.grid(row=0,column=0,sticky='nsew')
        main = ttk.Frame(parent, padding=(20,20))
        main.grid(row=0,column=1,sticky='nsew')
        self.header(rail, '模板库')
        ttk.Label(rail,text='阿杜公开版 · 六类风格',style='Muted.TLabel').pack(anchor='w',pady=(6,14))
        self.template_tree = ttk.Treeview(rail,show='tree',selectmode='browse',height=6)
        self.template_tree.column('#0',width=210,minwidth=160)
        self.template_tree.pack(fill='x')
        for family in self.template_families:
            self.template_tree.insert('', 'end', iid=family['id'], text=family['title'])
        self.template_tree.bind('<<TreeviewSelect>>', self._select_template_family)
        self.template_poster = tk.Canvas(rail, height=145, width=210, background=PANEL,highlightthickness=0)
        self.template_poster.pack(fill='x',pady=(16,6))
        self.template_preview_caption=tk.StringVar()
        ttk.Label(rail,textvariable=self.template_preview_caption,style='Muted.TLabel',wraplength=210).pack(anchor='w')
        self.template_demo_button=self.button(rail,'播放风格示例',self._play_template_demo,fill='x',pady=(8,4))
        self.button(rail,'导入 .motion 包',self._import_motion,fill='x',pady=4)
        self.button(rail,'检查运行环境',self._check_template_runtime,fill='x',pady=4)
        self.template_install = tk.StringVar(value='adu-motion-video 已安装' if self.template_families else 'Skill 未安装')
        ttk.Label(rail,textvariable=self.template_install,style='Muted.TLabel',wraplength=210).pack(anchor='w',pady=10)

        self.template_title = tk.StringVar(value='选择模板')
        self.template_description = tk.StringVar()
        ttk.Label(main,textvariable=self.template_title,style='Title.TLabel').pack(anchor='w')
        ttk.Label(main,textvariable=self.template_description,style='Muted.TLabel',wraplength=700).pack(anchor='w',pady=(6,12))
        choice = ttk.Frame(main); choice.pack(fill='x',pady=(0,8))
        self.template_style = tk.StringVar()
        self.template_layout = tk.StringVar(value='竖屏 9:16')
        self.template_styles = ttk.Combobox(choice,textvariable=self.template_style,state='readonly',width=25)
        self.template_styles.pack(side='left',fill='x',expand=True)
        self.template_styles.bind('<<ComboboxSelected>>',self._template_style_changed)
        layouts = ttk.Combobox(choice,textvariable=self.template_layout,state='readonly',width=15,
                               values=['竖屏 9:16','横屏 16:9'])
        layouts.pack(side='right',padx=(12,0))
        layouts.bind('<<ComboboxSelected>>',self._template_style_changed)
        self.template_version = tk.StringVar()
        ttk.Label(main,textvariable=self.template_version,style='Muted.TLabel',wraplength=700).pack(anchor='w',pady=(0,8))

        form = ttk.Frame(main); form.pack(fill='x')
        self.template_inputs = {}
        brand_row=ttk.Frame(form);brand_row.pack(fill='x',pady=3)
        ttk.Label(brand_row,text='商品/品牌',width=10).pack(side='left')
        self.template_brand=tk.StringVar()
        ttk.Entry(brand_row,textvariable=self.template_brand).pack(side='left',fill='x',expand=True,padx=(0,8))
        for key,title,types in [('source','口播成片',[('视频','*.mp4 *.mov *.mkv *.m4v')]),
                                ('subtitle','对应字幕',[('SRT 字幕','*.srt')]),
                                ('music','背景音乐',[('音频','*.mp3 *.wav *.m4a *.flac')])]:
            row=ttk.Frame(form); row.pack(fill='x',pady=3)
            ttk.Label(row,text=title,width=10).pack(side='left')
            variable=tk.StringVar();self.template_inputs[key]=variable
            ttk.Entry(row,textvariable=variable).pack(side='left',fill='x',expand=True,padx=(0,8))
            self.button(row,'选择',lambda k=key,t=types:self._pick_template_input(k,t),side='right')
        self.template_no_music=tk.BooleanVar(value=True)
        ttk.Checkbutton(form,text='不加背景音乐，保留口播与模板动作音效',variable=self.template_no_music).pack(anchor='w',pady=(4,8))
        media_row=ttk.Frame(form);media_row.pack(fill='x')
        self.template_media_text=tk.StringVar(value='补充素材：未选择')
        ttk.Label(media_row,textvariable=self.template_media_text,style='Muted.TLabel').pack(side='left')
        self.button(media_row,'补充序列目录',self._pick_template_media_folder,side='right')
        self.button(media_row,'补充商品图 / 视频',self._pick_template_media,side='right',padx=(0,8))
        ttk.Label(main,text='剪辑要求',style='Heading.TLabel').pack(anchor='w',pady=(12,6))
        self.template_instruction=self._text(main,height=3)
        self.template_instruction.pack(fill='x')
        self.template_instruction.insert('1.0','用于 TikTok 商品讲解。突出口播已有的商品卖点，保持原语言和完整表达。')
        actions=ttk.Frame(main);actions.pack(fill='x',pady=(12,8))
        self.template_plan_button=self.button(actions,'生成模板方案',self._plan_template,primary=True,side='left')
        self.template_render_button=self.button(actions,'导出模板视频',self._render_template,side='left',padx=8)
        # Cancellation stays available while the generic operation disables ordinary actions.
        from .desktop_studio import CutButton
        self.template_cancel_button=CutButton(actions,'取消',self.template_cancel.set,width=68)
        self.template_cancel_button.pack(side='right')
        self.template_render_button.state(['disabled'])
        self.template_status=tk.StringVar(value='先选择已剪好的口播与对应 SRT，再生成方案。')
        ttk.Label(main,textvariable=self.template_status,style='Muted.TLabel',wraplength=700).pack(anchor='w',pady=(0,8))
        history_head=ttk.Frame(main);history_head.pack(fill='x')
        ttk.Label(history_head,text='模板工程',style='Heading.TLabel').pack(side='left')
        self.button(history_head,'打开工程',self._open_template_project,side='right')
        self.button(history_head,'查看报告',self._open_template_report,side='right',padx=8)
        self.template_history=ttk.Treeview(main,columns=('style','state'),show='headings',height=3,selectmode='browse')
        self.template_history.heading('style',text='风格');self.template_history.heading('state',text='状态')
        self.template_history.column('style',width=180);self.template_history.column('state',width=220)
        self.template_history.pack(fill='both',expand=True,pady=(6,0))
        self.template_history.bind('<<TreeviewSelect>>',self._select_template_job)
        self._refresh_template_jobs()
        if self.template_families:
            self.template_tree.selection_set(self.template_families[0]['id'])
            self._select_template_family()

    def _template_family(self):
        selection=self.template_tree.selection()
        return next((f for f in self.template_families if selection and f['id']==selection[0]),None)

    def _select_template_family(self,event=None):
        family=self._template_family()
        if not family:return
        self.template_title.set(family['title'])
        self.template_description.set(family['purpose'])
        values=[s['id']+' · '+s['title'] for s in family['styles']]
        self.template_styles.configure(values=values)
        self.template_style.set(values[0])
        self._template_style_changed()

    def _template_style_changed(self,event=None):
        family=self._template_family()
        if not family:return
        sid=self.template_style.get().split(' · ')[0]
        style=next(s for s in family['styles'] if s['id']==sid)
        layout='portrait' if self.template_layout.get().startswith('竖') else 'landscape'
        try:
            manifest=self.template_library.resolve_pack(style,layout)
            info=read_json(manifest)
            self.template_version.set(f"公开版已安装 · {info['version']} · {len(info.get('scenes',[]))} 个镜头组 · 新片需看听核验")
            self.template_plan_button.state(['disabled'] if self.busy else ['!disabled'])
        except ValueError as exc:
            self.template_version.set(str(exc));self.template_plan_button.state(['disabled'])
        self.template_poster.delete('all')
        self._template_photo=None
        if family['id'] in FACE_FREE_PREVIEW_FAMILIES:
            self._draw_face_free_template_poster(family,style)
            self.template_preview_caption.set('无人脸版式示意 · 作者旧示例已隐藏')
            self.template_demo_button.state(['disabled'])
            self._template_action_states()
            return
        self.template_preview_caption.set('作者示例，仅用于查看风格')
        self.template_demo_button.state(['!disabled'])
        preview=self.template_library.skill/style.get('preview','')
        if preview.is_file():
            try:
                from PIL import Image,ImageTk
                with Image.open(preview) as im:
                    im=im.convert('RGB');im.thumbnail((210,140))
                self._template_photo=ImageTk.PhotoImage(im,master=self.window)
                self.template_poster.create_image(105,72,image=self._template_photo)
            except (OSError,ValueError):pass
        self._template_action_states()

    def _draw_face_free_template_poster(self,family,style):
        from .desktop_studio import UI_FONT
        canvas=self.template_poster
        canvas.create_rectangle(3,3,207,141,outline='#444852',width=1)
        canvas.create_text(13,13,text=style['id'],anchor='nw',fill='#aab8ff',font=(UI_FONT,10,'bold'))
        canvas.create_rectangle(14,43,196,113,outline='#586174',fill='#222833',width=1)
        if family['id']=='01':
            canvas.create_rectangle(25,54,88,102,fill='#333e5b',outline='')
            canvas.create_rectangle(100,55,182,63,fill='#82a1ff',outline='')
            canvas.create_rectangle(100,71,169,76,fill='#6e788a',outline='')
            canvas.create_rectangle(100,84,178,89,fill='#6e788a',outline='')
        elif family['id']=='02':
            canvas.create_polygon(31,97,72,54,167,54,181,97,fill='#f0ede7',outline='')
            canvas.create_line(64,78,153,78,fill='#6a7080',width=3)
            canvas.create_line(51,88,168,88,fill='#6a7080',width=3)
        else:
            for x,y,w,h in ((25,58,72,40),(106,53,74,28),(112,87,62,17)):
                canvas.create_rectangle(x,y,x+w,y+h,fill='#2b3855',outline='#759bff')
            canvas.create_line(97,79,107,68,fill='#81a4ff',width=2)
        canvas.create_text(105,127,text='版式示意 · 无人物素材',fill='#adb4c3',font=(UI_FONT,9))

    def _pick_template_input(self,key,types):
        path=filedialog.askopenfilename(parent=self.window,title='选择'+{'source':'口播成片','subtitle':'对应字幕','music':'背景音乐'}[key],filetypes=types)
        if path:
            self.template_inputs[key].set(path)
            if key=='music':self.template_no_music.set(False)

    def _pick_template_media(self):
        paths=filedialog.askopenfilenames(parent=self.window,title='选择本期商品图或视频',filetypes=[('图片与视频','*.png *.jpg *.jpeg *.webp *.mp4 *.mov *.mkv')])
        if paths:
            self.template_media=list(dict.fromkeys([*self.template_media,*paths]))
            self.template_media_text.set(f'补充素材：{len(self.template_media)} 项')

    def _pick_template_media_folder(self):
        path=filedialog.askdirectory(parent=self.window,title='选择本期序列图片目录')
        if path:
            self.template_media=list(dict.fromkeys([*self.template_media,path]))
            self.template_media_text.set(f'补充素材：{len(self.template_media)} 项')

    def _plan_template(self):
        if self.busy:return
        source=self.template_inputs['source'].get().strip()
        subtitle=self.template_inputs['subtitle'].get().strip()
        if not source or not subtitle:
            self.notice('请选择口播成片和与它对应的 SRT 字幕。',True);return
        if not self.template_brand.get().strip():
            self.notice('请填写本期商品或品牌名称，模板画面会使用它。',True);return
        if not self.template_no_music.get() and not self.template_inputs['music'].get().strip():
            self.notice('请选择音乐，或勾选不加背景音乐。',True);return
        service=getattr(self.backend,'service',None)
        if service is None:
            self.notice('请先在设置中配置模型连接。',True);return
        request=dict(source=source,subtitle=subtitle,brand=self.template_brand.get().strip(),
                     style=self.template_style.get().split(' · ')[0],
                     layout='portrait' if self.template_layout.get().startswith('竖') else 'landscape',
                     music='' if self.template_no_music.get() else self.template_inputs['music'].get().strip(),
                     media=self.template_media[:],instruction=self.template_instruction.get('1.0','end').strip())
        self.template_cancel.clear()
        runner=TemplateRunner(self.template_library,self.template_cancel)
        self.run('正在规划模板…',lambda:runner.plan(request,model_generator(service.config)),self._template_done)
        self._watch_template_operation()

    def _render_template(self):
        if self.busy or not self.template_job:return
        directory=Path(self.template_job)
        self.template_cancel.clear()
        runner=TemplateRunner(self.template_library,self.template_cancel)
        self.run('正在构建模板工程…',lambda:runner.render(directory),self._template_done)
        self._watch_template_operation()

    def _watch_template_operation(self):
        self._refresh_template_jobs()
        rows=self.template_library.jobs()
        if rows:
            row=rows[0]
            self.template_status.set(STATES.get(row['state'],row['state'])+(' · '+self.backend.safe_error(row['error']) if row.get('error') else ''))
        if self.busy and not self.closed:self.later(1000,self._watch_template_operation)
        else:self._template_action_states()

    def _template_done(self,directory):
        self._refresh_template_jobs()
        self.template_history.selection_set(Path(directory).name)
        self._select_template_job()
        job=read_json(Path(directory)/'job.json')
        self.notice('模板视频已导出，请打开工程查看成片并连续看听。' if job['state']=='COMPLETED' else
                    '方案已就绪，可以导出模板视频。' if job['state']=='READY' else '方案需要调整，请查看报告。')

    def _refresh_template_jobs(self):
        selected=self.template_history.selection()
        rows=self.template_library.jobs()[:50]
        ids={row['id'] for row in rows}
        for iid in self.template_history.get_children():
            if iid not in ids:self.template_history.delete(iid)
        for index,row in enumerate(rows):
            values=(row.get('style_title',row['style']),STATES.get(row['state'],row['state']))
            if self.template_history.exists(row['id']):self.template_history.item(row['id'],values=values)
            else:self.template_history.insert('',index,iid=row['id'],values=values)
        if selected and selected[0] in ids:self.template_history.selection_set(selected)

    def _select_template_job(self,event=None):
        selected=self.template_history.selection()
        if not selected:return
        self.template_job=self.template_library.state/'jobs'/selected[0]
        job=read_json(self.template_job/'job.json')
        self.template_status.set(STATES.get(job['state'],job['state'])+(' · '+self.backend.safe_error(job['error']) if job.get('error') else ''))
        self._template_action_states()

    def _template_action_states(self):
        if not hasattr(self,'template_render_button'):return
        selected_style=self.template_style.get().split(' · ')[0]
        layout='portrait' if self.template_layout.get().startswith('竖') else 'landscape'
        style=next((s for f in self.template_families for s in f['styles'] if s['id']==selected_style),None)
        available=style is not None and self.template_library.resolve_pack(style,layout,strict=False) is not None
        self.template_plan_button.state(['!disabled'] if available and not self.busy else ['disabled'])
        ready=bool(self.template_job and (Path(self.template_job)/'job.json').is_file() and read_json(Path(self.template_job)/'job.json')['state']=='READY')
        self.template_render_button.state(['!disabled'] if ready and not self.busy else ['disabled'])

    def _open_template_project(self):
        if self.template_job:os.startfile(str(self.template_job))
        else:self.notice('请先选择模板工程。')

    def _open_template_report(self):
        if not self.template_job:return
        directory=Path(self.template_job)
        report=directory/'plan/report.md'
        os.startfile(str(report if report.exists() else directory/'pipeline.log' if (directory/'pipeline.log').exists() else directory/'job.json'))

    def _play_template_demo(self):
        family=self._template_family()
        if not family:return
        if family['id'] in FACE_FREE_PREVIEW_FAMILIES:
            self.notice('该类作者旧示例含人脸，已从 Framo 预览中隐藏。')
            return
        sid=self.template_style.get().split(' · ')[0]
        style=next(s for s in family['styles'] if s['id']==sid)
        path=self.template_library.skill/style.get('demo','')
        if path.is_file():os.startfile(str(path))
        else:self.notice('此公开包没有本地示例。')

    def _import_motion(self):
        path=filedialog.askopenfilename(parent=self.window,title='导入新版 MotionGit 模板',filetypes=[('MotionGit 模板','*.motion')])
        if path:self.run('正在验证并收录模板包…',lambda:self.template_library.import_motion(path),
                         lambda _:self.notice('已收录新版包；当前制作入口使用 GitHub 公开版。新版运行适配需单独验证。'))

    def _check_template_runtime(self):
        def finished(checks):
            self.template_install.set('运行环境就绪' if all(ok for _,ok in checks) else '缺少：'+'、'.join(n for n,ok in checks if not ok))
            self.notice('；'.join(n+('：就绪' if ok else '：缺失') for n,ok in checks))
        self.run('正在检查模板环境…',self.template_library.check,finished)
