"""A focused before/after repair workspace, using the existing studio tokens."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from .subtitle_removal import (ProcessInpainter, RemovalCancelled, checked_region,
                               detect_subtitle_regions, export_repaired,
                               inspect_video, make_subtitle_tracker, read_frame)


class SubtitleRemovalDialog:
    def __init__(self, owner, source=None):
        # Intent: prepare subtitle-free input for an ad editor. The picture and
        # its exact repair mask lead; file metadata and export actions follow.
        # Hierarchy: two matched monitors, one primary export action.
        # Palette/depth/surfaces: inherit Framo's BG/PANEL/FIELD, thin LINE;
        # CYAN marks only the editable mask, RED only failures.
        # Typography: existing Heading/Muted styles and CutButton fonts.
        # Spacing: 4px grid, 20px outer padding, 16px monitor gap, 8px controls.
        from .desktop_studio import BG, PANEL, TEXT, MUTED, CutButton
        self.owner=owner
        self.closed=False;self.busy=False
        self.info=None;self.tools=None;self.region=None
        self.before=None;self.after=None;self.output=None;self.drag=None
        self.photos={};self.mapping=None
        self.cancel=threading.Event()
        self.queue=queue.Queue()
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='subtitle-removal')
        self.painter=ProcessInpainter()
        self.window=tk.Toplevel(owner.window)
        self.window.title('Framo · 去字幕')
        self.window.configure(background=BG)
        self.window.geometry('1080x760')
        self.window.minsize(900,660)
        self.window.transient(owner.window)
        self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.window.bind('<Escape>',lambda _:self.close())
        main=ttk.Frame(self.window,padding=20);main.pack(fill='both',expand=True)
        head=ttk.Frame(main);head.pack(fill='x')
        ttk.Label(head,text='去字幕',style='Title.TLabel').pack(side='left')
        self.file_label=tk.StringVar(value='导入含字幕的视频')
        ttk.Label(head,textvariable=self.file_label,style='Muted.TLabel',
                  width=42,anchor='e').pack(side='right',padx=(12,0))
        ttk.Label(main,text='软件会自动扫描字幕位置并快速修补；识别不到时仍可拖动调整区域。',
                  style='Muted.TLabel').pack(anchor='w',pady=(8,16))
        monitors=ttk.Frame(main);monitors.pack(fill='both',expand=True)
        monitors.rowconfigure(1,weight=1)
        self.canvases={}
        for index,(key,title) in enumerate((('before','原片 · 拖动框选字幕区域'),('after','修补预览'))):
            monitors.columnconfigure(index,weight=1,uniform='monitor')
            ttk.Label(monitors,text=title,style='Heading.TLabel').grid(row=0,column=index,sticky='w',pady=(0,8),padx=(0,8) if not index else (8,0))
            canvas=tk.Canvas(monitors,bg=PANEL,highlightthickness=0,takefocus=1)
            canvas.grid(row=1,column=index,sticky='nsew',padx=(0,8) if not index else (8,0))
            canvas.bind('<Configure>',lambda _,k=key:self.paint(k))
            self.canvases[key]=canvas
        canvas=self.canvases['before']
        canvas.bind('<ButtonPress-1>',self.drag_start)
        canvas.bind('<B1-Motion>',self.drag_move)
        canvas.bind('<ButtonRelease-1>',self.drag_end)
        canvas.bind('<Delete>',lambda _:self.reset_region())
        self.region_label=tk.StringVar(value='未框选')
        row=ttk.Frame(main);row.pack(fill='x',pady=(8,8))
        ttk.Label(row,textvariable=self.region_label,style='Muted.TLabel').pack(side='left')
        self.reset_button=ttk.Button(row,text='重设区域',command=self.reset_region)
        self.reset_button.pack(side='right')
        seek=ttk.Frame(main);seek.pack(fill='x',pady=(0,12))
        ttk.Label(seek,text='预览时间',style='Muted.TLabel').pack(side='left',padx=(0,8))
        self.seconds=tk.DoubleVar(value=0)
        self.scale=ttk.Scale(seek,variable=self.seconds,from_=0,to=1,
                             command=self.seek_changed)
        self.scale.pack(side='left',fill='x',expand=True)
        self.time_label=tk.StringVar(value='0.0 秒')
        ttk.Label(seek,textvariable=self.time_label,style='Muted.TLabel',width=10,anchor='e').pack(side='left',padx=8)
        self.read_button=ttk.Button(seek,text='读取画面',command=self.load_frame)
        self.read_button.pack(side='right')
        self.note=ttk.Label(main,text='自动识别＋逐帧 AI 修补，保留原片声音。复杂纹理、人物遮挡和快速运动可能出现残影；请先预览确认。',
                            style='Muted.TLabel',wraplength=980)
        self.note.pack(fill='x',pady=(0,12))
        self.note.bind('<Configure>',lambda e:self.note.configure(wraplength=max(160,e.width)))
        self.progress=ttk.Progressbar(main,mode='determinate',maximum=100)
        self.progress.pack(fill='x')
        self.status=tk.StringVar(value='选择视频后，在左侧框选字幕位置。')
        self.status_label=ttk.Label(main,textvariable=self.status,style='Muted.TLabel',wraplength=980)
        self.status_label.pack(fill='x',pady=(8,12))
        self.status_label.bind('<Configure>',lambda e:self.status_label.configure(wraplength=max(160,e.width)))
        controls=ttk.Frame(main);controls.pack(fill='x')
        def button(text,command,**kwargs):
            b=CutButton(controls,text,command,**kwargs);b.pack(side='left',padx=(0,8));return b
        self.pick_button=button('导入视频',self.pick,width=110)
        self.preview_button=button('预览修补',self.preview,width=120)
        self.export_button=button('导出去字幕 MP4',self.export,primary=True,width=174)
        self.cancel_button=button('取消处理',self.cancel_export,width=104)
        self.folder_button=button('打开目录',self.open_folder,width=104)
        self.import_button=CutButton(controls,'用于新建剪辑',self.use_output,width=144)
        self.import_button.pack(side='right')
        self.update_states()
        self.timer=self.window.after(40,self.drain)
        if source:self.window.after(0,lambda:self.load(source))

    def update_states(self):
        ready=bool(self.info and self.before is not None and self.region)
        for button,enabled in ((self.pick_button,not self.busy),
                (self.preview_button,ready and not self.busy),
                (self.export_button,ready and not self.busy),
                (self.cancel_button,self.busy),
                (self.import_button,bool(self.output) and not self.busy),
                (self.folder_button,bool(self.output) and not self.busy),
                (self.reset_button,ready and not self.busy),
                (self.read_button,bool(self.info) and not self.busy),
                (self.scale,bool(self.info) and not self.busy)):
            button.state(['!disabled'] if enabled else ['disabled'])

    def submit(self,title,function,callback):
        if self.busy or self.closed:return
        self.busy=True;self.cancel.clear()
        self.status.set(title)
        from .desktop_studio import MUTED
        self.status_label.configure(foreground=MUTED)
        self.progress.configure(value=0)
        self.update_states()
        future=self.executor.submit(function)
        future.add_done_callback(lambda f:self.queue.put(('done',f,callback)))

    def drain(self):
        if self.closed:return
        from .desktop_studio import RED, MUTED
        while not self.queue.empty():
            kind,value,callback=self.queue.get_nowait()
            if kind=='progress':
                self.progress.configure(value=value.get('percent',0))
                elapsed=value.get('elapsed_s')
                detail=f' · 已处理 {value["frames"]} 帧 · {elapsed:.0f} 秒' if elapsed else ''
                if value.get('remaining_s') is not None:
                    detail+=f' · 预计剩余 {max(1,round(value["remaining_s"]/60))} 分钟'
                self.status.set(value['phase']+detail)
                continue
            self.busy=False
            try:
                result=value.result()
                if self.cancel.is_set() and not (isinstance(result,dict) and result.get('output_file')):
                    raise RemovalCancelled('已取消去字幕，原片保留')
                callback(result)
            except RemovalCancelled as exc:self.status.set(str(exc))
            except Exception as exc:
                if self.cancel.is_set():self.status.set('已取消去字幕，原片保留')
                else:
                    message=self.owner.backend.safe_error(exc)
                    self.status.set('去字幕未完成：'+str(message))
                    self.status_label.configure(foreground=RED)
            if self.cancel.is_set():
                self.painter.close()
                self.painter=ProcessInpainter()
            self.update_states()
        self.timer=self.window.after(40,self.drain)

    def pick(self):
        if self.busy:return
        path=filedialog.askopenfilename(parent=self.window,title='导入需要去字幕的视频',
            filetypes=[('视频','*.mp4 *.mov *.mkv *.avi *.webm *.m4v'),('所有文件','*.*')])
        if path:self.load(path)

    def load(self,path):
        if self.busy:return
        def work():
            from .independent_export import discover_tools
            tools=discover_tools(Path(__file__).resolve().parents[1]/'toolchain')
            info=inspect_video(path,tools)
            frame=read_frame(info,0,tools)
            samples=[frame]
            for fraction in (.25,.5,.75):
                samples.append(read_frame(info,info['duration_s']*fraction,tools))
            regions=detect_subtitle_regions(samples)
            return tools,info,frame,regions
        def ready(result):
            self.tools,self.info,self.before,regions=result
            self.after=None;self.output=None
            self.file_label.set(Path(self.info['path']).name)
            self.seconds.set(0);self.time_label.set('0.0 秒')
            self.scale.configure(to=max(0,self.info['duration_s']-.1))
            if regions:
                self.set_region(regions[0])
                self.status.set(f'已自动识别 {len(regions)} 个字幕候选区域；可拖动微调后预览。')
            else:
                self.reset_region()
                self.status.set('未可靠识别到字幕，已保留默认区域；请拖动框选后预览。')
            self.paint('before');self.paint('after')
        self.submit('正在读取视频…',work,ready)

    def seek_changed(self,value):
        self.time_label.set(f'{float(value):.1f} 秒')

    def load_frame(self):
        if not self.info or self.busy:return
        info,seconds=dict(self.info),self.seconds.get()
        def ready(image):
            self.before=image;self.after=None
            self.paint('before');self.paint('after')
            self.status.set('已读取画面，可重新框选或预览快速修补。')
        self.submit('正在读取画面…',lambda:read_frame(info,seconds,self.tools),ready)

    def reset_region(self):
        if not self.info or self.busy:return
        w,h=self.info['width'],self.info['height']
        self.set_region((round(w*.1),round(h*.78),round(w*.9),round(h*.9)))

    def set_region(self,region):
        self.region=tuple(region);self.after=None
        x0,y0,x1,y1=self.region
        self.region_label.set(f'字幕区域  {x1-x0} × {y1-y0}  ·  左上角 {x0}, {y0}')
        self.paint('before');self.paint('after')
        self.update_states()

    def paint(self,key):
        from PIL import ImageTk
        from .desktop_studio import CYAN, MUTED
        canvas=self.canvases[key];canvas.delete('all')
        image=self.before if key=='before' else self.after
        w,h=max(1,canvas.winfo_width()),max(1,canvas.winfo_height())
        if image is None:
            canvas.create_text(w/2,h/2,text='导入视频' if key=='before' else '点击“预览修补”查看效果',fill=MUTED)
            return
        fitted=image.copy();fitted.thumbnail((w,h))
        x,y=(w-fitted.width)/2,(h-fitted.height)/2
        self.photos[key]=ImageTk.PhotoImage(fitted,master=self.window)
        canvas.create_image(x,y,image=self.photos[key],anchor='nw')
        if key=='before':
            self.mapping=(x,y,fitted.width/image.width,fitted.height/image.height)
            if self.region:
                x0,y0,x1,y1=self.region;sx,sy=self.mapping[2:]
                canvas.create_rectangle(x+x0*sx,y+y0*sy,x+x1*sx,y+y1*sy,
                                        outline=CYAN,width=2,tags='region')

    def point(self,event):
        if not self.mapping or not self.info:return None
        x,y,sx,sy=self.mapping
        return (max(0,min(self.info['width'],round((event.x-x)/sx))),
                max(0,min(self.info['height'],round((event.y-y)/sy))))

    def drag_start(self,event):
        if self.busy or self.before is None:return
        self.drag=self.point(event)
        self.canvases['before'].focus_set()

    def drag_move(self,event):
        if self.drag is None or self.busy:return
        p=self.point(event)
        x0,y0=self.drag;x1,y1=p
        self.set_region((min(x0,x1),min(y0,y1),max(x0,x1),max(y0,y1)))

    def drag_end(self,event):
        if self.drag is None:return
        self.drag_move(event);self.drag=None

    def preview(self):
        if self.busy or not self.info:return
        try:region=checked_region(self.region,self.info['width'],self.info['height'])
        except ValueError as exc:self.status.set(str(exc));return
        image=self.before.copy()
        def ready(result):
            self.after=result;self.paint('after')
            self.status.set('快速修补预览已生成。检查字幕残留和画面细节，再导出。')
        self.submit('正在使用本地 AI 修补预览…',lambda:self.painter.repair(image,region),ready)

    def export(self):
        if self.busy or not self.info:return
        try:region=checked_region(self.region,self.info['width'],self.info['height'])
        except ValueError as exc:self.status.set(str(exc));return
        path=filedialog.asksaveasfilename(parent=self.window,title='保存去字幕视频',
            defaultextension='.mp4',initialfile=Path(self.info['path']).stem+'_去字幕.mp4',
            initialdir=str(Path(self.owner.state_dir)/'Exports'/'SubtitleRemoval'),
            filetypes=[('MP4 视频','*.mp4')])
        if not path:return
        self.output=None
        info=dict(self.info)
        tracker=make_subtitle_tracker(region, info['width'], info['height'])
        def work():
            return export_repaired(info,region,path,self.tools,self.painter,cancel=self.cancel,
                progress=lambda value:self.queue.put(('progress',value,None)),
                region_selector=tracker)
        def ready(report):
            self.output=report['output_file'];self.progress.configure(value=100)
            self.status.set('已导出：'+self.output+'。可直接用于新建剪辑。')
        self.submit('正在启动去字幕导出…',work,ready)

    def cancel_export(self):
        if self.busy:
            self.cancel.set()
            self.painter.close()
            self.status.set('正在取消并清理临时视频…')

    def open_folder(self):
        if self.output:os.startfile(str(Path(self.output).parent))

    def use_output(self):
        if not self.output or self.busy:return
        if self.owner.busy:
            self.status.set('请等待主窗口当前操作完成后，再导入去字幕成品。');return
        self.owner.import_repaired_source(self.info['path'],self.output)
        self.owner.window.lift()

    def close(self,force=False):
        if self.closed:return
        if self.busy and not force:
            self.cancel_export()
            self.status.set('正在取消处理，结束后可关闭窗口。')
            return
        self.closed=True;self.cancel.set()
        self.painter.close()
        self.window.after_cancel(self.timer)
        self.executor.shutdown(wait=False,cancel_futures=True)
        self.window.destroy()
