"""Bundled subtitle font picker, with previews from the actual render fonts."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import desktop_studio as studio
from .font_library import bundled_font_library, validate_subtitle_font_id

AUTO = '__automatic__'
CATEGORIES = {'全部': None, '中文': ('zh-CN', 'zh-TW'), '英语': ('en',), '日语': ('ja',)}
LOCALE_LABELS = {'zh-CN': '简体中文', 'zh-TW': '繁体中文', 'en': '英语', 'ja': '日语'}
TAG_LABELS = {'clean': '简洁', 'professional': '专业', 'technology': '科技',
              'soft': '柔和', 'cute': '可爱', 'lifestyle': '生活', 'playful': '活泼',
              'bold': '醒目', 'commerce': '商业', 'energetic': '动感',
              'traditional': '古典', 'handwritten': '手写', 'casual': '休闲',
              'comic': '漫画', 'elegant': '优雅', 'expressive': '个性',
              'gaming': '游戏', 'sport': '运动'}
SAMPLES = {'zh-CN': '把日常，剪成好故事', 'zh-TW': '讓日常，成為好故事',
           'ja': '毎日を、ちょっと特別に。', 'en': 'Make every moment count'}


class FontPicker:
    def __init__(self, parent, selected, on_apply):
        self.library = bundled_font_library()
        self.on_apply = on_apply
        self.draft_id = selected or AUTO
        self._paint_timer = None
        self._image = None
        self._closed = False
        self.window = tk.Toplevel(parent)
        self.window.withdraw()
        self.window.title('字幕字体库')
        self.window.configure(background=studio.BG)
        self.window.transient(parent)
        self.window.minsize(780, 520)
        width = min(960, max(800, parent.winfo_width() - 100))
        height = min(640, max(540, parent.winfo_height() - 80))
        x = max(0, min(parent.winfo_rootx() + (parent.winfo_width()-width)//2,
                       parent.winfo_screenwidth()-width))
        y = max(0, min(parent.winfo_rooty() + (parent.winfo_height()-height)//2,
                       parent.winfo_screenheight()-height-48))
        self.window.geometry(f'{width}x{height}+{x}+{y}')
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.window.bind('<Escape>', lambda _: self.close())

        header = ttk.Frame(self.window, padding=(20, 16, 20, 0))
        header.pack(fill='x')
        ttk.Label(header, text='字幕字体库', style='Heading.TLabel').pack(side='left')
        ttk.Label(header, text=f'{len(self.library.families)} 款内置字体 · 无需下载',
                  style='Muted.TLabel').pack(side='right')
        ttk.Label(self.window, text='按字体主要语言分类；中文包含简体与繁体。选择后用于新建任务。',
                  style='Muted.TLabel').pack(anchor='w', padx=20, pady=(8, 0))

        footer = ttk.Frame(self.window, padding=(20, 12, 20, 16))
        footer.pack(side='bottom', fill='x')
        self.apply_button = studio.CutButton(footer, '使用此字体', self.apply, primary=True, width=144)
        self.apply_button.pack(side='right')
        self.cancel_button = studio.CutButton(footer, '取消', self.close, width=96)
        self.cancel_button.pack(side='right', padx=(0, 8))
        self.result = tk.StringVar()
        ttk.Label(footer, textvariable=self.result, foreground=studio.AMBER,
                  wraplength=420).pack(side='left', fill='x', expand=True)

        body = ttk.Frame(self.window, padding=(20, 16, 20, 0))
        body.pack(fill='both', expand=True)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body, width=300)
        left.grid(row=0, column=0, sticky='nsew', padx=(0, 20))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(3, weight=1)
        self.query = tk.StringVar()
        self.search = ttk.Entry(left, textvariable=self.query)
        ttk.Label(left, text='搜索字体名称或风格', style='Muted.TLabel').grid(row=0, column=0, sticky='w', pady=(0, 6))
        self.search.grid(row=1, column=0, columnspan=2, sticky='ew')
        self.language = tk.StringVar(value='全部')
        self.language_filter = ttk.Frame(left)
        self.language_filter.grid(row=2, column=0, columnspan=2, sticky='ew', pady=8)
        self.category_buttons = {}
        for column, label in enumerate(CATEGORIES):
            self.language_filter.columnconfigure(column, weight=1, uniform='font-category')
            button = studio.CutButton(self.language_filter, label,
                                      lambda value=label: self.language.set(value), width=64, height=36)
            button.grid(row=0, column=column, sticky='ew', padx=(0, 4 if column < 3 else 0))
            self.category_buttons[label] = button
        self.tree = ttk.Treeview(left, show='tree', selectmode='browse', height=8)
        self.tree.column('#0', width=270, minwidth=100, stretch=True)
        self.tree.grid(row=3, column=0, sticky='nsew')
        scroll = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        scroll.grid(row=3, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=scroll.set)
        self.count = tk.StringVar()
        ttk.Label(left, textvariable=self.count, style='Muted.TLabel').grid(row=4, column=0, sticky='w', pady=(8, 0))

        right = ttk.Frame(body)
        right.grid(row=0, column=1, sticky='nsew')
        self.name = tk.StringVar()
        self.name_label = ttk.Label(right, textvariable=self.name, style='Heading.TLabel', wraplength=460)
        self.name_label.pack(anchor='w')
        self.meta = tk.StringVar()
        self.meta_label = ttk.Label(right, textvariable=self.meta, style='Muted.TLabel', wraplength=460)
        self.meta_label.pack(anchor='w', pady=(6, 12))
        self.canvas = tk.Canvas(right, background=studio.PANEL, highlightthickness=1,
                                highlightbackground=studio.LINE, height=180)
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<Configure>', lambda _: self.schedule_preview())
        ttk.Label(right, text='输入文字试一试', style='Muted.TLabel').pack(anchor='w', pady=(12, 6))
        initial_family = self.library.families.get(selected)
        self.sample_text = tk.StringVar(value=SAMPLES[initial_family['languages'][0] if initial_family else 'zh-CN'])
        self.sample_entry = ttk.Entry(right, textvariable=self.sample_text)
        self.sample_entry.pack(fill='x')
        self.coverage = tk.StringVar()
        self.coverage_label = ttk.Label(right, textvariable=self.coverage, style='Muted.TLabel',
                                        wraplength=460, justify='left')
        self.coverage_label.pack(fill='x', pady=(8, 0))
        right.bind('<Configure>', lambda e: self._wrap(e.width))

        self.tree.bind('<<TreeviewSelect>>', self.choose)
        self.tree.bind('<Return>', lambda _: self.apply())
        self.query.trace_add('write', lambda *_: self.filter())
        self.language.trace_add('write', lambda *_: self.filter())
        self.sample_text.trace_add('write', lambda *_: self.schedule_preview())
        self.filter()
        self.window.deiconify()
        self.window.update_idletasks()
        self.window.grab_set()
        self.search.focus_set()

    def _wrap(self, width):
        for label in (self.name_label, self.meta_label, self.coverage_label):
            label.configure(wraplength=max(200, width))

    def filter(self):
        query = self.query.get().strip().casefold()
        category = self.language.get()
        locales = CATEGORIES[category]
        for label, button in self.category_buttons.items():
            button.configure(selected=label == category)
        self.tree.delete(*self.tree.get_children())
        self.tree.insert('', 'end', iid=AUTO, text='自动匹配 · 根据视频风格选择')
        count = 0
        for font_id, family in self.library.families.items():
            search_text = ' '.join([family['label'], font_id, *family['tags'],
                                   *(TAG_LABELS.get(t, t) for t in family['tags'])]).casefold()
            if query and query not in search_text:
                continue
            # Classify by the main design language, so Chinese/Japanese fonts
            # with Latin glyphs do not also fill the English category.
            if locales and family['languages'][0] not in locales:
                continue
            self.tree.insert('', 'end', iid=font_id, text=family['label'])
            count += 1
        self.count.set(f'{category} · {count} 款字体' if count else '没有匹配字体，请调整搜索条件')
        if not self.tree.exists(self.draft_id):
            self.draft_id = AUTO
        self.tree.selection_set(self.draft_id)
        self.tree.focus(self.draft_id)
        self.tree.see(self.draft_id)
        self.choose()

    def choose(self, _event=None):
        selected = self.tree.selection()
        if not selected:
            return
        changed = self.draft_id != selected[0]
        self.draft_id = selected[0]
        self.tree.see(self.draft_id)
        family = self.library.families.get(self.draft_id)
        self.result.set('')
        if family:
            self.name.set(family['label'])
            self.meta.set(' · '.join(TAG_LABELS.get(t, t) for t in family['tags']))
            if changed:
                self.sample_text.set(SAMPLES[family['languages'][0]])
        else:
            self.name.set('自动匹配')
            self.meta.set('结合视频风格、字幕语言与文字内容选择字体')
        self.apply_button.configure(text='使用自动匹配' if self.draft_id == AUTO else '使用此字体')
        self.schedule_preview()

    def schedule_preview(self):
        if self._closed:
            return
        if self._paint_timer:
            self.window.after_cancel(self._paint_timer)
        self._paint_timer = self.window.after(80, self.paint_preview)

    def paint_preview(self):
        from PIL import Image, ImageDraw, ImageTk
        if self._paint_timer:
            self.window.after_cancel(self._paint_timer)
        self._paint_timer = None
        if self._closed:
            return
        width, height = max(240, self.canvas.winfo_width()-2), max(100, self.canvas.winfo_height()-2)
        image = Image.new('RGB', (width, height), studio.PANEL)
        draw = ImageDraw.Draw(image)
        family = self.library.families.get(self.draft_id)
        text = self.sample_text.get().strip()[:160] or SAMPLES['zh-CN']
        try:
            if family:
                locale = family['languages'][0]
                role = 'body' if 'body' in family['roles'] else 'heading'
                if all(self.library.missing(r, text) for r in family['files']):
                    text = SAMPLES[locale]
                    coverage = '此字体不支持部分试字，已换用示例；成片遇到缺字会自动使用兼容字体。'
                    color = studio.AMBER
                else:
                    coverage = ('支持：' + ' / '.join(LOCALE_LABELS.get(l, l) for l in family['languages'])
                                + '。其它语言或缺字会自动使用兼容字体。')
                    color = studio.MUTED
                choice = self.library.select(text, locale, role, preferred=self.draft_id)
            else:
                import re
                locale = 'ja' if re.search(r'[\u3040-\u30ff]', text) else 'zh-CN' if re.search(r'[\u3400-\u9fff]', text) else 'en'
                choice = self.library.select(text, locale)
                coverage, color = '预览为示例；成片会根据视频内容自动选字。', studio.MUTED
            for size in range(46, 13, -2):
                font = self.library.load_pillow_font(choice, size)
                # Wrap long custom samples instead of shrinking to unreadable type.
                lines, current = [], ''
                for character in text:
                    if current and draw.textlength(current+character, font=font) > width-48:
                        lines.append(current); current = ''
                    current += character
                lines.append(current)
                display = '\n'.join(lines)
                box = draw.multiline_textbbox((0, 0), display, font=font, spacing=10, stroke_width=1)
                if box[2]-box[0] <= width-36 and box[3]-box[1] <= height-44:
                    break
            draw.multiline_text(((width-(box[2]-box[0]))/2-box[0], (height-(box[3]-box[1]))/2-box[1]),
                                display, font=font, fill=studio.TEXT, spacing=10, align='center',
                                stroke_width=1, stroke_fill=studio.TEXT)
            self.coverage.set(coverage)
            self.coverage_label.configure(foreground=color)
            self.apply_button.state(['!disabled'])
        except (OSError, ValueError, KeyError) as exc:
            self.coverage.set('无法预览字体：' + str(exc))
            self.coverage_label.configure(foreground=studio.AMBER)
            self.apply_button.state(['disabled'])
        self._image = ImageTk.PhotoImage(image, master=self.window)
        self.canvas.delete('all')
        self.canvas.create_image(width/2+1, height/2+1, image=self._image)

    def apply(self):
        if self._closed:
            return
        try:
            font_id = validate_subtitle_font_id(None if self.draft_id == AUTO else self.draft_id)
        except (OSError, ValueError, KeyError) as exc:
            self.result.set('无法使用此字体：' + str(exc))
            return
        label = self.library.families[font_id]['label'] if font_id else '自动匹配'
        self.on_apply(font_id, label)
        self.close()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._paint_timer:
            self.window.after_cancel(self._paint_timer)
            self._paint_timer = None
        self.window.grab_release()
        parent = self.window.master
        self.window.destroy()
        parent.focus_set()
