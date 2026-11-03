"""Obsidian studio presentation. No task execution, scheduling or draft writes."""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
import io
import json
import os
import queue
from pathlib import Path
import subprocess
import time
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont
from tkinter.scrolledtext import ScrolledText

from .output_preview import OutputPreviewStream
from .desktop_templates import TemplateLibraryView

# Obsidian Studio palette: near-black canvas, graphite surfaces, thin
# low-contrast borders, and an off-white primary accent.  The legacy GREEN
# name is retained because the presentation layer uses it in many places.
BG = '#0e0f10'
PANEL = '#171718'
FIELD = '#202023'
LINE = '#343438'
TEXT = '#f0f0f2'
MUTED = '#a1a1a7'
DIM = '#93949c'
GREEN = '#f1f1f2'
SELECT = '#2d2d30'
AMBER = '#d9a866'
RED = '#ed8b9b'
SUCCESS = '#79c7a7'
CYAN = '#80aeb8'
MINT = '#83b39f'
PURPLE_SOFT = '#38383d'
PURPLE_DARK = '#d9d9de'
DISABLED = '#29292c'
UI_FONT = 'Microsoft YaHei UI'
CONTROL_FONT = UI_FONT
_LOADED_FONT_PATHS = set()
EXPORT_LABELS = {'QUEUED': '排队中', 'PREPARING': '准备中', 'EXPORTING': '导出中',
                 'COMPLETED': '已完成', 'FAILED': '失败', 'CANCELLED': '已取消', 'SKIPPED': '已跳过'}


def configure_studio_font(window):
    """Use one readable native UI family, including menus and dialog controls."""
    global UI_FONT, CONTROL_FONT
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        add_font = ctypes.windll.gdi32.AddFontResourceExW
        add_font.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
        add_font.restype = ctypes.c_int
        folder = Path(__file__).resolve().parent.parent / 'desktop-assets/fonts'
        for name in ('LXGW975YuanSC-400W.ttf', 'LXGW975YuanSC-700W.ttf'):
            path = folder / name
            key = str(path)
            if key not in _LOADED_FONT_PATHS and path.is_file() and add_font(key, 0x10, None):
                _LOADED_FONT_PATHS.add(key)
    available = set(tkfont.families(window))
    UI_FONT = next((name for name in ('Microsoft YaHei UI', '微软雅黑 UI',
                                     'Microsoft YaHei', '微软雅黑', 'Noto Sans CJK SC',
                                     'Segoe UI') if name in available),
                   tkfont.nametofont('TkDefaultFont', root=window).actual('family'))
    CONTROL_FONT = next((name for name in ('霞鹜975圆体SC 400W', 'LXGW 975 Yuan SC 400W',
                                          'LXGW 975 Yuan SC') if name in available), UI_FONT)
    for name in tkfont.names(window):
        if name.startswith('Tk'):
            tkfont.nametofont(name, root=window).configure(
                family=UI_FONT, size=9 if name == 'TkSmallCaptionFont' else 10,
                weight='bold' if name == 'TkHeadingFont' else 'normal')
    # Entry fonts are widget options, not reliably inherited from ttk styles.
    # Avoid a wildcard *Font, which would flatten heading/metric typography.
    for control in ('TEntry', 'TCombobox', 'TSpinbox'):
        window.option_add('*'+control+'.font', (UI_FONT, 10))
    window.option_add('*TCombobox*Listbox.font', (UI_FONT, 10))
    return UI_FONT


def timecode(value):
    seconds = max(0, int(value or 0)) / 1_000_000
    return f'{int(seconds // 60):02d}:{seconds % 60:05.2f}'


def read_sections(batch):
    """Only display recorded absolute ranges; never infer a missing timeline."""
    final = (batch or {}).get('finalization') or {}
    path = final.get('master_snapshot')
    if not path:
        return [], '主草稿尚未生成'
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        rows = data.get('batch_outputs') or []
        if not rows:
            return [], '主草稿中没有可识别的成品区段'
        previous = 0
        for row in rows:
            start, end = row.get('start_us'), row.get('end_us')
            if (type(start) is not int or type(end) is not int or
                    start < previous or end <= start or not row.get('output_id')):
                return [], '时间区段无效，请查看批次报告'
            previous = end
        return rows, ''
    except (OSError, ValueError, TypeError, AttributeError):
        return [], '无法读取主草稿时间区段'


class CutButton(tk.Canvas):
    """Keyboard-accessible rounded button with ttk-compatible state methods."""
    def __init__(self, parent, text, command, *, primary=False, width=160, height=42):
        background = (ttk.Style(parent).lookup(parent.cget('style') or 'TFrame', 'background')
                      if isinstance(parent, ttk.Frame) else parent.cget('background'))
        super().__init__(parent, width=width, height=height, bg=background or BG,
                         highlightthickness=0, bd=0, takefocus=1, cursor='hand2')
        self._text, self.command, self.primary = text, command, primary
        self.disabled, self.hover, self.focused = False, False, False
        self.selected = False
        self.bind('<Configure>', lambda _: self._draw())
        self.bind('<Enter>', lambda _: self._hover(True))
        self.bind('<Leave>', lambda _: self._hover(False))
        self.bind('<FocusIn>', lambda _: self._focus(True))
        self.bind('<FocusOut>', lambda _: self._focus(False))
        self.bind('<Button-1>', lambda _: self.invoke())
        self.bind('<Return>', lambda _: self.invoke())
        self.bind('<space>', lambda _: self.invoke())

    def _hover(self, value):
        self.hover = value
        self._draw()

    def _focus(self, value):
        self.focused = value
        self._draw()

    @staticmethod
    def _rounded_points(x1, y1, x2, y2, radius):
        """Return a smooth polygon around a rounded rectangle.

        Tk's Canvas has no native rounded rectangle primitive.  A small set of
        arc points keeps the button crisp on the bundled Tk versions while
        avoiding platform-specific ttk border rendering.
        """
        import math
        radius = max(1, min(radius, (x2-x1)/2, (y2-y1)/2))
        points = []
        corners = (
            (x2-radius, y1+radius, -90, 0),
            (x2-radius, y2-radius, 0, 90),
            (x1+radius, y2-radius, 90, 180),
            (x1+radius, y1+radius, 180, 270),
        )
        for cx, cy, start, end in corners:
            for step in range(7):
                angle = math.radians(start + (end-start) * step / 6)
                points.extend((cx + radius*math.cos(angle),
                               cy + radius*math.sin(angle)))
        return points

    def _draw(self):
        w, h = self.winfo_width(), self.winfo_height()
        if self.disabled:
            fill, fg, border = DISABLED, DIM, DISABLED
        elif self.primary:
            fill, fg = '#f1f1f2', '#101011'
            border = '#ffffff' if self.focused or self.hover else '#d8d8dc'
        else:
            fill, fg = (SELECT if self.hover or self.selected else FIELD), TEXT
            border = '#86868d' if self.focused or self.hover else LINE
        points = self._rounded_points(1, 1, max(2, w-1), max(2, h-1), min(12, h/2-1))
        if not hasattr(self, '_body_item'):
            self._body_item = self.create_polygon(*points, width=1, smooth=True)
            self._label_item = self.create_text(w/2, h/2)
        self.coords(self._body_item, *points)
        self.itemconfigure(self._body_item, fill=fill, outline=border)
        self.coords(self._label_item, w/2, h/2)
        self.itemconfigure(self._label_item, text=self._text, fill=fg,
                           font=(CONTROL_FONT, 10, 'bold' if self.primary or self.selected else 'normal'))

    def configure(self, cnf=None, **kw):
        if 'text' in kw:
            self._text = kw.pop('text')
        if 'command' in kw:
            self.command = kw.pop('command')
        if 'selected' in kw:
            self.selected = kw.pop('selected')
        result = super().configure(cnf, **kw) if cnf is not None or kw else None
        if hasattr(self, '_text'):
            self._draw()
        return result

    config = configure

    def cget(self, key):
        return self._text if key == 'text' else super().cget(key)

    def state(self, states=None):
        if states:
            previous = self.disabled
            if 'disabled' in states:
                self.disabled = True
            if '!disabled' in states:
                self.disabled = False
            if self.disabled != previous:
                super().configure(cursor='arrow' if self.disabled else 'hand2')
                self._draw()
        return ('disabled',) if self.disabled else ()

    def instate(self, states):
        return all(self.disabled if s == 'disabled' else not self.disabled for s in states)

    def invoke(self):
        if not self.disabled:
            self.focus_set()
            return self.command()


class StudioLayout(TemplateLibraryView):
    """Presentation mixin. Backend actions remain in DesktopWorkbench."""
    def _style(self):
        from .workbench import ROOT
        self.ui_font_family = configure_studio_font(self.window)
        self.window.title('Framo')
        self.window.configure(background=BG)
        width = min(1560, self.window.winfo_screenwidth()-60)
        height = min(960, self.window.winfo_screenheight()-90)
        self.window.geometry(f'{width}x{height}')
        self.window.minsize(min(1120, width), min(740, height))
        self.window.option_add('*TCombobox*Listbox.background', FIELD)
        self.window.option_add('*TCombobox*Listbox.foreground', TEXT)
        self.window.option_add('*TCombobox*Listbox.selectBackground', SELECT)
        self.window.option_add('*TCombobox*Listbox.selectForeground', GREEN)
        icon = ROOT/'desktop-assets/framo.ico'
        if icon.is_file():
            self.window.iconbitmap(str(icon))
        style = ttk.Style(self.window)
        style.theme_use('clam')
        style.configure('.', font=(UI_FONT, 10), background=BG, foreground=TEXT)
        for name, bg in [('TFrame', BG), ('Card.TFrame', PANEL), ('Side.TFrame', BG)]:
            style.configure(name, background=bg)
        for name, fg, bg, font in [
            ('TLabel', TEXT, BG, 10), ('Card.TLabel', TEXT, PANEL, 10),
            ('Muted.TLabel', MUTED, BG, 9), ('CardMuted.TLabel', MUTED, PANEL, 9),
            ('Title.TLabel', TEXT, BG, 22), ('Heading.TLabel', TEXT, BG, 13),
            ('Accent.TLabel', GREEN, BG, 10), ('Metric.TLabel', TEXT, BG, 34)]:
            style.configure(name, foreground=fg, background=bg,
                            font=(CONTROL_FONT if name in ('Title.TLabel', 'Heading.TLabel') else UI_FONT, font,
                                  'bold' if name in ('Title.TLabel', 'Heading.TLabel') else 'normal'))
        style.configure('TButton', padding=(12, 8), background=FIELD, foreground=GREEN,
                        borderwidth=0, font=(CONTROL_FONT, 10))
        style.map('TButton', background=[('active', SELECT)], foreground=[('disabled', DIM)])
        for name in ('TEntry', 'TCombobox', 'TSpinbox'):
            style.configure(name, padding=7, fieldbackground=FIELD, background=FIELD,
                            foreground=TEXT, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                            arrowcolor=GREEN, insertcolor=GREEN)
            style.map(name, fieldbackground=[('readonly', FIELD), ('disabled', BG)],
                      foreground=[('disabled', DIM), ('readonly', TEXT)], bordercolor=[('focus', GREEN)])
        style.configure('Treeview', background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=54, borderwidth=0, bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
        style.configure('Treeview.Heading', background=FIELD, foreground=MUTED,
                        padding=(8, 10), borderwidth=0, font=(UI_FONT, 9))
        # Keep per-row failure/warning colours even while a row is selected.
        style.map('Treeview', background=[('selected', SELECT)], foreground=[])
        style.map('Treeview.Heading', background=[('active', FIELD)])
        style.configure('Rail.Treeview', background=PANEL, fieldbackground=PANEL, rowheight=72)
        style.configure('Task.Treeview', rowheight=44)
        style.configure('TProgressbar', background=GREEN, troughcolor=LINE, borderwidth=0,
                        lightcolor=GREEN, darkcolor=GREEN, thickness=3)
        style.configure('TSeparator', background=LINE, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        style.configure('Vertical.TScrollbar', background=LINE, troughcolor=BG, borderwidth=0,
                        arrowcolor=MUTED, arrowsize=10, lightcolor=LINE, darkcolor=LINE, bordercolor=BG)
        style.configure('Horizontal.TScrollbar', background=LINE, troughcolor=BG, borderwidth=0,
                        arrowcolor=MUTED, arrowsize=10, lightcolor=LINE, darkcolor=LINE, bordercolor=BG)
        # Clam's stock elements keep platform-specific borders in some Tk builds.
        # Image elements retain a flat track on every bundled Tk version.
        self._style_images = []
        for name, color in [('track', BG), ('thumb', LINE), ('bar', GREEN)]:
            img = tk.PhotoImage(master=self.window, width=8, height=8)
            img.put(color, to=(0, 0, 8, 8))
            self._style_images.append(img)
            element = 'Studio.'+name
            if element not in style.element_names():
                style.element_create(element, 'image', img, border=0, sticky='nsew')
        for orient, sticky in [('Vertical', 'ns'), ('Horizontal', 'we')]:
            style.layout(orient+'.TScrollbar', [('Studio.track', {'sticky': sticky, 'children':
                [('Studio.thumb', {'sticky': 'nsew'})]})])
        separator = tk.PhotoImage(master=self.window, width=1, height=1)
        separator.put(LINE, to=(0, 0, 1, 1))
        self._style_images.append(separator)
        if 'Studio.separator' not in style.element_names():
            style.element_create('Studio.separator', 'image', separator, sticky='nsew')
        style.layout('TSeparator', [('Studio.separator', {'sticky': 'nsew'})])
        style.configure('TScale', background=FIELD, troughcolor=PANEL, bordercolor=LINE,
                        lightcolor=LINE, darkcolor=LINE)
        style.map('TScale', background=[('disabled', DISABLED), ('active', MUTED)])
        style.layout('Horizontal.TProgressbar', [('Studio.track', {'sticky': 'nsew', 'children':
            [('Studio.bar', {'side': 'left', 'sticky': 'ns'})]})])
        style.configure('Horizontal.TProgressbar', thickness=3, background=GREEN, troughcolor=LINE)
        if os.name == 'nt':
            # Native title bar stays available for resize, minimize and accessibility.
            try:
                import ctypes
                self.window.update_idletasks()
                hwnd = ctypes.windll.user32.GetParent(self.window.winfo_id())
                flag = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(flag), 4)
            except (AttributeError, OSError):
                pass

    def button(self, parent, text, command, primary=False, **layout):
        width = max(46, min(260, 32 + len(text)*13))
        button = CutButton(parent, text, command, primary=primary, width=width)
        self.buttons.append(button)
        if layout:
            button.pack(**layout)
        return button

    def _build(self):
        self.batches = {}
        self._group = None
        self._sections = []
        self._asset_paths = {}
        self._removed_assets = []
        self._preview_cache = OrderedDict()
        self._preview_pending = set()
        self._preview_failed = set()
        self._thumbnail_photos = []
        self._preview_key = None
        self._source_preview_playing = False
        self._source_preview_stream = None
        self._source_preview_position = 0.0
        self._source_preview_duration = 0.0
        self._source_preview_token = 0
        self._source_audio_status = '内嵌有声预览'
        self._output_audio_status = '内嵌有声预览'
        self._source_canvas_items = {}
        self._source_draw_key = None
        self._asset_strip_signature = None
        self._nav_animation_token = 0
        self._nav_indicator_y = None
        # The finished-product preview is rendered as decoded frames inside
        # the workbench.  It deliberately reads the delivered MP4 only and
        # never writes beside the source, draft, or delivery file.
        self._output_preview_path = None
        self._output_preview_image = None
        self._output_preview_duration = 0.0
        self._output_preview_position = 0.0
        self._output_preview_playing = False
        self._output_preview_token = 0
        self._output_preview_stream = None
        self._output_preview_future = None
        self.sidebar = ttk.Frame(self.window, width=156, style='Side.TFrame', padding=(12, 24))
        self.sidebar.pack(side='left', fill='y')
        self.sidebar.pack_propagate(False)
        ttk.Label(self.sidebar, text='Framo', style='TLabel',
                  font=(CONTROL_FONT, 20, 'bold')).pack(anchor='w', padx=8)
        ttk.Label(self.sidebar, text='创作工作台', style='Muted.TLabel').pack(anchor='w', padx=8, pady=(4, 38))
        for key, title in [('create', '新建剪辑'), ('templates', '模板库'), ('tasks', '任务与草稿'), ('export', '独立导出'), ('settings', '设置')]:
            button = CutButton(self.sidebar, title, lambda v=key: self.show(v), width=132, height=44)
            button.pack(side='bottom' if key == 'settings' else 'top', fill='x', pady=5)
            self.nav_buttons[key] = button
        self._nav_indicator = tk.Canvas(self.sidebar, width=3, height=24, background=BG,
                                         highlightthickness=0, bd=0, takefocus=0)
        self._nav_marker = self._nav_indicator.create_rectangle(0, 0, 3, 24, fill=TEXT, outline='')
        self._nav_indicator.place(x=4, y=0, relheight=1, bordermode='outside')
        self.sidebar.bind('<Configure>', lambda _: self._move_nav_indicator(animate=False))
        ttk.Separator(self.window, orient='vertical').pack(side='left', fill='y')
        workspace = ttk.Frame(self.window)
        workspace.pack(side='left', fill='both', expand=True)
        footer = ttk.Frame(workspace, padding=(22, 8))
        footer.pack(side='bottom', fill='x')
        self.breadcrumb = tk.StringVar()
        ttk.Label(footer, textvariable=self.breadcrumb, style='Muted.TLabel').pack(side='left')
        self.notice_var = tk.StringVar(value='添加素材，开始创作。')
        self.notice_label = ttk.Label(footer, textvariable=self.notice_var, style='Muted.TLabel', anchor='e')
        self.notice_label.pack(side='right', fill='x', expand=True, padx=(25, 0))
        self.progress = ttk.Progressbar(workspace, mode='indeterminate')
        self.progress.pack(side='bottom', fill='x')
        self.progress.configure(value=0)
        self.pages = ttk.Frame(workspace)
        self.pages.pack(fill='both', expand=True)
        self.pages.rowconfigure(0, weight=1)
        self.pages.columnconfigure(0, weight=1)
        self.page_canvases = {}
        for name in ('create', 'templates', 'tasks', 'export', 'settings'):
            viewport = ttk.Frame(self.pages)
            viewport.grid(row=0, column=0, sticky='nsew')
            canvas = tk.Canvas(viewport, bg=BG, highlightthickness=0)
            scrollbar = ttk.Scrollbar(viewport, orient='vertical', command=canvas.yview)
            scrollbar.pack(side='right', fill='y')
            canvas.pack(fill='both', expand=True)
            page = ttk.Frame(canvas)
            item = canvas.create_window(0, 0, anchor='nw', window=page)
            def resize(event, c=canvas, i=item, s=scrollbar, minimum=790 if name in ('settings', 'templates') else 680):
                height = max(minimum, event.height)
                size = (event.width, height)
                if getattr(c, '_page_size', None) != size:
                    c._page_size = size
                    c.itemconfigure(i, width=event.width, height=height)
                    c.configure(scrollregion=(0, 0, event.width, height))
                if event.height >= minimum:
                    if s.winfo_manager(): s.pack_forget()
                elif not s.winfo_manager():
                    s.pack(side='right', fill='y', before=c)
            canvas.bind('<Configure>', resize)
            canvas.configure(yscrollcommand=scrollbar.set)
            self.page_canvases[name] = canvas
            self.views[name] = viewport
            setattr(self, '_'+name+'_content', page)
        self._create_page(self._create_content)
        self._tasks_page(self._tasks_content)
        self._export_page(self._export_content)
        self._templates_page(self._templates_content)
        self._settings_content.configure(padding=24)
        self._settings_page(self._settings_content)
        self.checks_text.configure(background=PANEL, foreground=TEXT, insertbackground=GREEN)
        self.checks_text.tag_configure('good', foreground=SUCCESS)
        self.checks_text.tag_configure('bad', foreground=RED)

    def _columns(self, parent):
        # Keep the rail compact and give the working area and action panel
        # enough width for readable labels and stable button rows.
        parent.columnconfigure(0, weight=0, minsize=204)
        parent.columnconfigure(2, weight=1, minsize=330)
        parent.columnconfigure(4, weight=0, minsize=284)
        parent.rowconfigure(0, weight=1)
        left = ttk.Frame(parent, style='Side.TFrame', width=204, padding=(12, 20))
        left.grid(row=0, column=0, sticky='nsew')
        left.grid_propagate(False)
        center = ttk.Frame(parent, padding=20)
        center.grid(row=0, column=2, sticky='nsew')
        center.pack_propagate(False)
        right = ttk.Frame(parent, width=284, padding=(16, 20))
        right.grid(row=0, column=4, sticky='nsew')
        right.pack_propagate(False)
        for column in (1, 3):
            ttk.Separator(parent, orient='vertical').grid(row=0, column=column, sticky='ns')
        # Left panels use pack; disable its propagation after the geometry is fixed.
        left.pack_propagate(False)
        return left, center, right

    @staticmethod
    def header(parent, title, description=''):
        ttk.Label(parent, text=title, style='Title.TLabel').pack(anchor='w')
        if description:
            ttk.Label(parent, text=description, style='Muted.TLabel', wraplength=650).pack(anchor='w', pady=(6, 18))

    @staticmethod
    def card(parent, **pack):
        frame = ttk.Frame(parent, style='Card.TFrame', padding=18)
        frame.pack(**pack)
        return frame

    @staticmethod
    def tree(parent, columns, widths, height, *, rail=False):
        holder = ttk.Frame(parent)
        holder.pack(fill='both', expand=True)
        tree = ttk.Treeview(holder, columns=[c[0] for c in columns], show='headings',
                           height=height, selectmode='extended', style='Rail.Treeview' if rail else 'Treeview')
        for (key, title), width in zip(columns, widths):
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=45, stretch=True)
        y = ttk.Scrollbar(holder, orient='vertical', command=tree.yview)
        y.pack(side='right', fill='y')
        x = ttk.Scrollbar(holder, orient='horizontal', command=tree.xview)
        x.pack(side='bottom', fill='x')
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        tree.pack(fill='both', expand=True)
        for name, color in [('good', SUCCESS), ('bad', RED), ('warning', AMBER), ('muted', DIM)]:
            tree.tag_configure(name, foreground=color)
        return tree

    @staticmethod
    def _line(parent, pady=18):
        ttk.Separator(parent).pack(fill='x', pady=pady)

    def _text(self, parent, height=5):
        field = ScrolledText(parent, height=height, width=20, wrap='word', relief='flat',
                             background=FIELD, foreground=TEXT, insertbackground=GREEN,
                             selectbackground=SELECT, padx=14, pady=12, borderwidth=0,
                             font=(UI_FONT, 10), spacing1=2, spacing3=3)
        field.vbar.pack_forget()
        scrollbar = ttk.Scrollbar(field.frame, orient='vertical', command=field.yview)
        scrollbar.pack(side='right', fill='y')
        field.configure(yscrollcommand=scrollbar.set)
        return field

    def _create_page(self, parent):
        from .desktop_workbench import WORKFLOWS, MARKET_PRESETS, default_instruction
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(2, weight=0, minsize=310)
        parent.rowconfigure(0, weight=1)
        center = ttk.Frame(parent, padding=20)
        center.grid(row=0, column=0, sticky='nsew')
        center.pack_propagate(False)
        ttk.Separator(parent, orient='vertical').grid(row=0, column=1, sticky='ns')
        right = ttk.Frame(parent, width=310, padding=(16, 20))
        right.grid(row=0, column=2, sticky='nsew')
        right.pack_propagate(False)
        self.create_center, self.create_inspector = center, right
        head = ttk.Frame(center); head.pack(fill='x', pady=(0, 20))
        ttk.Label(head, text='新建剪辑', style='Title.TLabel').pack(side='left')
        self.button(head, '＋ 添加素材', self.pick_files, side='right')
        self.button(head, '去字幕', self.open_subtitle_removal, side='right', padx=8)

        # Preserve the existing selection model and removal semantics while
        # presenting materials as a filmstrip rather than another narrow rail.
        self.asset_tree = ttk.Treeview(center, columns=('name', 'duration', 'size'), selectmode='extended')
        self.asset_tree.bind('<<TreeviewSelect>>', lambda _: self.preview_asset())
        library = ttk.Frame(center)
        library.pack(side='bottom', fill='x', pady=(18, 0))
        bar = ttk.Frame(library); bar.pack(fill='x', pady=(0, 10))
        self.asset_count = tk.StringVar(value='素材  00')
        ttk.Label(bar, textvariable=self.asset_count, style='Heading.TLabel').pack(side='left')
        self.remove_assets_button = self.button(bar, '移除选中', self.remove_files, side='right')
        self.undo_assets_button = self.button(bar, '撤销移除', self.undo_remove_files, side='right', padx=8)
        self.asset_strip = tk.Canvas(library, height=132, background=BG, highlightthickness=0,
                                     takefocus=1, cursor='hand2')
        self.asset_strip.pack(fill='x')
        self.asset_scroll = ttk.Scrollbar(library, orient='horizontal', command=self._scroll_assets)
        self.asset_scroll.pack(fill='x', pady=(8, 0))
        self.asset_strip.configure(xscrollcommand=self.asset_scroll.set)
        self.asset_strip.bind('<Configure>', lambda _: self.paint_asset_strip())
        self.asset_strip.bind('<Button-1>', self._select_thumbnail)
        self.asset_strip.bind('<MouseWheel>', lambda e: self._scroll_assets('scroll', -1 if e.delta > 0 else 1, 'units'))
        self.asset_strip.bind('<Left>', lambda _: self._step_asset(-1))
        self.asset_strip.bind('<Right>', lambda _: self._step_asset(1))
        self.asset_strip.bind('<Delete>', lambda _: self.remove_files())
        controls = ttk.Frame(center)
        controls.pack(side='bottom', fill='x', pady=(10, 0))
        self.preview_name = tk.StringVar(value='本地素材')
        name_label = ttk.Label(center, textvariable=self.preview_name, style='Muted.TLabel', wraplength=240)
        name_label.pack(side='bottom', fill='x', pady=(8, 0))
        name_label.bind('<Configure>', lambda e: name_label.configure(wraplength=max(80, e.width)))
        self.source_play_button = self.button(controls, '播放预览', self.toggle_source_preview, side='left')
        self.source_audio_label = ttk.Label(controls, text='内嵌有声预览', style='Muted.TLabel')
        self.source_audio_label.pack(side='left', padx=12)
        self.source_open_button = self.button(controls, '打开原片 ↗', self.open_source, side='right')
        self.preview_retry_button = self.button(controls, '重试预览', self.retry_source_preview)
        self.preview_canvas = tk.Canvas(center, bg=BG, highlightthickness=0, height=330)
        self.preview_canvas.pack(fill='both', expand=True)
        self.preview_canvas.bind('<Configure>', lambda _: self.paint_preview())
        self.preview_canvas.bind('<Button-1>', self._activate_source_preview)
        self.preview_canvas.bind('<Return>', self._activate_source_preview)
        self.preview_canvas.bind('<space>', self._activate_source_preview)
        self.preview_canvas.bind('<FocusIn>', lambda _: self._sync_preview_entry())
        self.preview_canvas.bind('<FocusOut>', lambda _: self._sync_preview_entry())
        self.preview_image = None
        self.preview_caption = '点击此处添加素材'
        self.preview_meta = ''

        ttk.Label(right, text='创作设置', style='Heading.TLabel').pack(anchor='w', pady=(0, 16))
        self.workflow = tk.StringVar(value=next(iter(WORKFLOWS)))
        self.workflow_select = ttk.Combobox(right, textvariable=self.workflow, state='readonly', values=list(WORKFLOWS), width=23)
        self.workflow_select.pack(fill='x', pady=(0, 14))
        self.workflow_select.bind('<<ComboboxSelected>>', lambda _: self.update_hint())
        self.long_settings = ttk.Frame(right)
        ttk.Label(self.long_settings,text='长视频时长',style='Muted.TLabel').pack(anchor='w',pady=(0,6))
        self.long_duration = tk.StringVar(value='自动（最多10分钟）')
        ttk.Combobox(self.long_settings,textvariable=self.long_duration,state='readonly',
            values=('自动（最多10分钟）','1分钟','3分钟','5分钟','10分钟'),width=23).pack(fill='x')
        ttk.Label(self.long_settings,text='素材最多20条 · 单条最长7分钟',
                  style='Muted.TLabel').pack(anchor='w',pady=(6,0))
        # Keep the brief and primary action together in the inspector.
        self.button(right, '创建任务  →', self.create_job, primary=True, side='bottom', fill='x', pady=(14, 0))
        prompt = ttk.Frame(right)
        prompt.pack(side='bottom', fill='both', expand=True, pady=(14, 0))
        ttk.Label(prompt, text='剪辑要求', style='Heading.TLabel').pack(anchor='w')
        self.instruction = self._text(prompt, height=5)
        self.instruction.pack(fill='both', expand=True, pady=(10, 0))
        self.instruction.insert('1.0', default_instruction(WORKFLOWS[self.workflow.get()], MARKET_PRESETS.get(self.workflow.get())))
        # Retain the bound hint for workflow updates without a second wall of
        # explanatory copy below the editable brief.
        self.workflow_hint = tk.StringVar()
        self.hint_label = ttk.Label(prompt, textvariable=self.workflow_hint, style='CardMuted.TLabel', wraplength=250)
        count = ttk.Frame(right); count.pack(fill='x', pady=(0, 12))
        ttk.Label(count, text='成品数量', style='Muted.TLabel').pack(side='left')
        self.batch_count = tk.IntVar(value=1)
        self.button(count, '＋', lambda: self.change_count(1), side='right')
        self.count_label = ttk.Label(count, text='01', font=(UI_FONT, 18))
        self.count_label.pack(side='right', padx=12)
        self.button(count, '−', lambda: self.change_count(-1), side='right')
        self.batch_count.trace_add('write', lambda *_: self._draw_count())
        self.source_format = tk.StringVar(value='画幅    未添加素材')
        self.source_format_label = ttk.Label(right, textvariable=self.source_format, style='Muted.TLabel', wraplength=270)
        self.source_format_label.pack(anchor='w')
        # Market follows the selected Japan/US category; no duplicate editor.
        self.target_country = tk.StringVar()
        ttk.Separator(right).pack(fill='x', pady=18)
        self.packaging_heading = ttk.Label(right, text='音画策略', style='Muted.TLabel')
        self.packaging_heading.pack(anchor='w')
        self.packaging_var = tk.StringVar()
        ttk.Label(right, textvariable=self.packaging_var, wraplength=270, justify='left',
                  font=(UI_FONT, 9)).pack(anchor='w', pady=(8, 12))
        self.voice_library_button = self.button(right, '音色库 · 选择与试听', self.open_voice_library, fill='x')
        self.selected_subtitle_font_id = None
        self.font_library_button = self.button(right, '字体库 · 自动匹配', self.open_font_library,
                                               fill='x', pady=(8, 0))
        self.sticker_library_button = self.button(right, '贴纸库 · 立体与电商', self.open_sticker_library,
                                                 fill='x', pady=(8, 0))
        # Reserve inspector controls before giving the remaining height to
        # the expandable brief; otherwise long settings hide the voice tools.
        prompt.pack_forget()
        prompt.pack(side='bottom',fill='both',expand=True,pady=(14,0))
        self.update_hint()

    def _draw_count(self):
        try:
            self.count_label.configure(text=f'{int(self.batch_count.get()):02d}')
        except (ValueError, tk.TclError):
            self.count_label.configure(text='—')

    def change_count(self, delta):
        from .batch_model import MAX_BATCH_OUTPUTS
        if self.busy:
            return
        try:
            value = self.batch_count.get()
        except tk.TclError:
            value = 1
        self.batch_count.set(max(1, min(MAX_BATCH_OUTPUTS, value+delta)))

    def paint_preview(self):
        c = self.preview_canvas
        self._sync_preview_entry()
        self.sync_material_controls()
        w, h = c.winfo_width(), c.winfo_height()
        if w < 30 or h < 30:
            return
        items = self._source_canvas_items
        if not items:
            items.update(background=c.create_polygon(0, 0, 1, 1, smooth=True),
                         frame=c.create_image(0, 0, state='hidden'),
                         circle=c.create_oval(0, 0, 1, 1, fill=TEXT, outline='', state='hidden'),
                         triangle=c.create_polygon(0, 0, 1, 1, fill=BG, outline='', state='hidden'),
                         plus=c.create_text(0, 0, fill=BG, font=(UI_FONT, 25)),
                         label=c.create_text(0, 0, fill=MUTED, font=(UI_FONT, 11)),
                         clock=c.create_text(0, 0, anchor='se', fill=MUTED, font=(UI_FONT, 9)))
            c.addtag_withtag('source_play', items['circle'])
            c.addtag_withtag('source_play', items['triangle'])
        focused = self.preview_canvas == self.window.focus_get()
        c.coords(items['background'], *CutButton._rounded_points(1, 1, w-1, h-1, 18))
        c.itemconfigure(items['background'], fill=PANEL, outline=MUTED if focused else LINE)
        c.coords(items['frame'], w/2, h/2)
        c.coords(items['circle'], w/2-30, h/2-30, w/2+30, h/2+30)
        c.coords(items['triangle'], w/2-7, h/2-13, w/2-7, h/2+13, w/2+14, h/2)
        c.coords(items['plus'], w/2, h/2)
        c.coords(items['label'], w/2, h/2+53)
        c.coords(items['clock'], w-18, h-9)
        if self.preview_image is not None:
            from PIL import ImageTk, ImageOps
            draw_key = (id(self.preview_image), w, h)
            if draw_key != self._source_draw_key:
                fitted = ImageOps.contain(self.preview_image, (max(1, w-36), max(1, h-36)))
                photo = getattr(self, '_preview_photo', None)
                if photo is not None and (photo.width(), photo.height()) == fitted.size:
                    photo.paste(fitted)
                else:
                    photo = ImageTk.PhotoImage(fitted, master=self.window)
                    c.itemconfigure(items['frame'], image=photo)
                    self._preview_photo = photo
                self._source_draw_key = draw_key
            c.itemconfigure(items['frame'], state='normal')
            c.addtag_withtag('source_frame', items['frame'])
        else:
            c.itemconfigure(items['frame'], state='hidden', image='')
            c.dtag(items['frame'], 'source_frame')
            self._preview_photo = None
            self._source_draw_key = None
        playable = bool(self._preview_key and Path(self._preview_key[0]).is_file())
        overlay = not self.assets or (playable and not self._source_preview_playing)
        c.itemconfigure(items['circle'], state='normal' if overlay else 'hidden')
        c.itemconfigure(items['triangle'], state='normal' if playable and overlay else 'hidden')
        c.itemconfigure(items['plus'], text='＋' if not self.assets else '')
        c.itemconfigure(items['label'], text='' if playable else self.preview_caption, width=w-60)
        c.itemconfigure(items['clock'], text=(
            f'{self._source_preview_position:04.1f}s / {self._source_preview_duration:04.1f}s'
            if playable else ''))
        if self._preview_key in self._preview_failed:
            if not self.preview_retry_button.winfo_manager():
                self.preview_retry_button.pack(side='right', padx=8)
        else:
            self.preview_retry_button.pack_forget()

    @staticmethod
    def _asset_preview_key(path):
        if not path:
            return None
        try:
            stat = Path(path).stat()
            return (str(path), stat.st_size, stat.st_mtime_ns)
        except OSError:
            return (str(path), 0, 0)

    def _asset_path(self, asset):
        return asset.get('source_path') or self._asset_paths.get(asset.get('asset_id'))

    def _request_asset_preview(self, key):
        if (not key or self.closed or key in self._preview_cache or
                key in self._preview_pending or key in self._preview_failed):
            return
        self._preview_pending.add(key)
        # Preview never waits behind a model call / backend operation, and it
        # uses the same bundled-tool resolver as the delivered-MP4 preview.
        future = self.preview_executor.submit(self._load_preview, key[0], self._output_preview_install_dir())
        future.add_done_callback(lambda f: self.inbox.put(('preview', f, lambda value: self._preview_ready(key, value))))

    def paint_asset_strip(self):
        if self.closed:
            return
        self.sync_material_controls()
        from PIL import ImageTk, ImageOps
        c = self.asset_strip
        width = c.winfo_width()
        selected = set(self.asset_tree.selection())
        signature = (width, round(c.canvasx(0), 2), tuple(sorted(selected)), self.busy,
                     tuple((a['asset_id'], a.get('name'), a.get('duration_us'),
                            self._asset_preview_key(self._asset_path(a)),
                            id(self._preview_cache.get(self._asset_preview_key(self._asset_path(a)))),
                            self._asset_preview_key(self._asset_path(a)) in self._preview_failed) for a in self.assets))
        if signature == self._asset_strip_signature:
            return
        self._asset_strip_signature = signature
        c.delete('all')
        self._thumbnail_photos = []
        total = max(width, len(self.assets)*150)
        c.configure(scrollregion=(0, 0, total, 132))
        if total > width:
            if not self.asset_scroll.winfo_manager():
                self.asset_scroll.pack(fill='x', pady=(8, 0))
        else:
            self.asset_scroll.pack_forget()
        left, right = c.canvasx(0), c.canvasx(width)
        if not self.assets:
            c.create_text(16, 55, anchor='w', text='上传的素材会显示在这里', fill=DIM, font=(UI_FONT, 10))
        for index, asset in enumerate(self.assets):
            x = index*150 + 2
            if x+140 < left or x > right:
                continue
            key = self._asset_preview_key(self._asset_path(asset))
            active = asset['asset_id'] in selected
            c.create_polygon(*CutButton._rounded_points(x, 1, x+140, 131, 12), smooth=True,
                             fill=SELECT if active else PANEL, outline=MUTED if active else LINE)
            frame = self._preview_cache.get(key)
            if frame is not None:
                photo = ImageTk.PhotoImage(ImageOps.contain(frame, (124, 80)), master=self.window)
                self._thumbnail_photos.append(photo)
                c.create_image(x+70, 48, image=photo, tags=('asset_thumbnail',))
            else:
                c.create_text(x+70, 45, text='不可预览' if key in self._preview_failed or not key else '▷',
                              fill=DIM, font=(UI_FONT, 10))
                self._request_asset_preview(key)
            title = asset.get('name', '')
            font = tkfont.Font(root=self.window, family=UI_FONT, size=9)
            if font.measure(title) > 122:
                while title and font.measure(title+'…') > 122:
                    title = title[:-1]
                title += '…'
            c.create_text(x+10, 101, text=title, anchor='w', fill=TEXT, font=(UI_FONT, 9))
            seconds = max(0, asset.get('duration_us', 0))/1e6
            c.create_text(x+10, 119, text=f'{seconds:.1f} 秒', anchor='w', fill=MUTED, font=(UI_FONT, 9))
            c.create_oval(x+106, 3, x+138, 35, fill=FIELD, outline=LINE,
                                  tags=('asset_remove', 'remove:'+asset['asset_id']))
            c.create_text(x+122, 19, text='×', fill=DIM if self.busy else TEXT,
                          font=(UI_FONT, 13), tags=('asset_remove', 'remove:'+asset['asset_id']))

    def _scroll_assets(self, *args):
        self.asset_strip.xview(*args)
        self.paint_asset_strip()

    def _select_thumbnail(self, event):
        self.asset_strip.focus_set()
        index = int(self.asset_strip.canvasx(event.x)//150)
        if not 0 <= index < len(self.assets):
            return
        iid = self.assets[index]['asset_id']
        local_x = self.asset_strip.canvasx(event.x) - (index*150+2)
        if 106 <= local_x <= 138 and 2 <= event.y <= 35:
            self.remove_files(asset_ids=[iid])
            return 'break'
        selected = self.asset_tree.selection()
        if event.state & 0x0004:  # Ctrl-click preserves multi-select removal.
            if iid in selected:
                self.asset_tree.selection_remove(iid)
            else:
                self.asset_tree.selection_add(iid)
        elif event.state & 0x0001 and selected:
            anchor = next((i for i, a in enumerate(self.assets) if a['asset_id'] == selected[0]), index)
            self.asset_tree.selection_set([a['asset_id'] for a in self.assets[min(anchor, index):max(anchor, index)+1]])
        else:
            self.asset_tree.selection_set(iid)
        self.preview_asset()

    def _step_asset(self, delta):
        if not self.assets:
            return 'break'
        selected = self.asset_tree.selection()
        index = next((i for i, a in enumerate(self.assets) if selected and a['asset_id'] == selected[0]), 0)
        index = max(0, min(len(self.assets)-1, index+delta))
        self.asset_tree.selection_set(self.assets[index]['asset_id'])
        c = self.asset_strip
        if index*150 < c.canvasx(0) or (index+1)*150 > c.canvasx(c.winfo_width()):
            c.xview_moveto(index/max(1, len(self.assets)))
        self.preview_asset()
        return 'break'

    def retry_source_preview(self):
        self._preview_failed.discard(self._preview_key)
        self._preview_key = None
        self.preview_asset()

    def _sync_preview_entry(self):
        playable = bool(self.assets and self._preview_key and Path(self._preview_key[0]).is_file())
        enabled = not self.closed and (playable or (not self.assets and not self.busy))
        self.preview_canvas.configure(cursor='hand2' if enabled else 'arrow', takefocus=int(enabled),
                                      highlightbackground=GREEN if enabled and self.preview_canvas == self.window.focus_get() else LINE)

    def sync_material_controls(self):
        if not hasattr(self, 'source_play_button'):
            return
        playable = bool(self._preview_key and Path(self._preview_key[0]).is_file())
        self.source_play_button.configure(text='暂停预览' if self._source_preview_playing else '播放预览')
        self.source_audio_label.configure(text=self._source_audio_status)
        for button, enabled in ((self.source_play_button, playable), (self.source_open_button, playable),
                                (self.remove_assets_button, bool(self.asset_tree.selection())),
                                (self.undo_assets_button, bool(self._removed_assets))):
            button.state(['!disabled'] if enabled and not self.busy else ['disabled'])

    def _add_from_preview(self, event=None):
        # Use the existing validated picker; clicking a loaded image does not import again.
        if not self.assets and not self.busy and not self.closed:
            self.preview_canvas.focus_set()
            self.pick_files()
        return 'break'

    def _activate_source_preview(self, event=None):
        if not self.assets:
            return self._add_from_preview(event)
        self.preview_canvas.focus_set()
        # The central play button starts playback. Click the running picture
        # to pause; keyboard activation also toggles the selected source.
        if event is not None and getattr(event, 'keysym', '') not in ('Return', 'space'):
            w, h = self.preview_canvas.winfo_width(), self.preview_canvas.winfo_height()
            if not self._source_preview_playing and ((event.x-w/2)**2+(event.y-h/2)**2 > 34**2):
                return 'break'
        self.toggle_source_preview()
        return 'break'

    def _stop_source_preview_stream(self):
        self._source_preview_token += 1
        stream, self._source_preview_stream = self._source_preview_stream, None
        if stream is not None:
            stream.stop()

    def _pause_source_preview(self, *, repaint=True):
        self._source_preview_playing = False
        self._stop_source_preview_stream()
        if not self.closed and repaint:
            self.paint_preview()

    def toggle_source_preview(self):
        if self.closed or self.current_view != 'create' or not self._preview_key:
            return
        if self._source_preview_playing:
            self._pause_source_preview()
            return
        if not Path(self._preview_key[0]).is_file():
            self.notice('素材文件未找到，请重新添加或打开原片检查。', True)
            return
        if self._source_preview_duration and self._source_preview_position >= self._source_preview_duration-0.05:
            self._source_preview_position = 0.0
        self._stop_source_preview_stream()
        self._source_preview_playing = True
        w, h = max(320, self.preview_canvas.winfo_width()-36), max(240, self.preview_canvas.winfo_height()-36)
        scale = min(1.0, 960/w, 640/h)
        size = (max(2, int(w*scale)//2*2), max(2, int(h*scale)//2*2))
        stream = OutputPreviewStream(self._preview_key[0], self._source_preview_position,
                                     self._output_preview_install_dir(), size=size)
        self._source_preview_stream = stream
        token = self._source_preview_token
        stream.start()
        self.paint_preview()
        self.later(15, lambda: self._poll_source_preview_stream(token))

    def _poll_source_preview_stream(self, token):
        if (self.closed or token != self._source_preview_token or
                not self._source_preview_playing or self.current_view != 'create'):
            return
        stream = self._source_preview_stream
        if stream is None:
            return
        status=getattr(stream,'audio_status',None)
        if isinstance(status,str):self._source_audio_status=status
        latest, terminal = None, None
        while True:
            try:
                kind, value = stream.frames.get_nowait()
            except queue.Empty:
                break
            if kind == 'frame': latest = value
            elif kind in ('ended', 'error'): terminal = kind
        if latest is not None:
            self._source_preview_position, self.preview_image = latest
            self.paint_preview()
        if terminal:
            self._source_preview_playing = False
            self._stop_source_preview_stream()
            if terminal == 'ended':
                self._source_preview_position = self._source_preview_duration
            else:
                self.notice('素材预览失败：'+str(getattr(stream,'error',None) or '读取失败，请重试'), True)
            self.paint_preview()
            return
        self.later(15, lambda: self._poll_source_preview_stream(token))

    def preview_asset(self):
        selection = self.asset_tree.selection()
        asset = next((a for a in self.assets if selection and a['asset_id'] == selection[0]), None)
        self.preview_name.set(asset['name'] if asset else '本地素材')
        path = self._asset_path(asset or {})
        if not path:
            self._source_audio_status='内嵌有声预览'
            self._source_preview_playing = False
            self._stop_source_preview_stream()
            self._source_preview_position = self._source_preview_duration = 0.0
            self._preview_key = None
            self.preview_image = None
            self.preview_caption = '点击此处添加素材' if not self.assets else '素材预览不可用'
            self.paint_preview()
            self.paint_asset_strip()
            return
        key = self._asset_preview_key(path)
        if key == self._preview_key:
            self.paint_asset_strip()
            return
        self._source_preview_playing = False
        self._source_audio_status='内嵌有声预览'
        self._stop_source_preview_stream()
        self._source_preview_position = 0.0
        self._source_preview_duration = max(0, asset.get('duration_us', 0))/1e6
        self._preview_key = key
        self.preview_image = self._preview_cache.get(key)
        # 读取首帧时保持预览区域干净；素材已经上传并在左侧列表中可见，
        # 首帧准备好后直接显示。失败时仍由 _preview_ready() 给出明确提示。
        self.preview_caption = '预览不可用，可打开原片查看' if key in self._preview_failed else ''
        self.paint_preview()
        self._request_asset_preview(key)
        self.paint_asset_strip()

    @staticmethod
    def _load_preview(path, install_dir):
        from PIL import Image
        from .independent_export import discover_tools
        try:
            tools = discover_tools(install_dir or Path(__file__).resolve().parent.parent / 'toolchain')
            if not tools.get('ffmpeg'):
                return None
            args = [tools['ffmpeg'], '-v', 'error', '-nostdin', '-i', str(path), '-frames:v', '1',
                    '-vf', 'scale=720:720:force_original_aspect_ratio=decrease', '-f', 'image2pipe', '-vcodec', 'png', '-']
            result = subprocess.run(args, capture_output=True, timeout=12,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode or not result.stdout:
                return None
            with Image.open(io.BytesIO(result.stdout)) as pic:
                return pic.convert('RGB')
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def _preview_ready(self, key, value):
        self._preview_pending.discard(key)
        if self.closed:
            return
        if value is not None:
            self._preview_cache[key] = value
            self._preview_cache.move_to_end(key)
            while len(self._preview_cache) > 16:
                self._preview_cache.popitem(last=False)
        else:
            self._preview_failed.add(key)
        if key == self._preview_key and not self._source_preview_playing and not self._source_preview_position:
            self.preview_image = value
            self.preview_caption = '预览不可用，可打开原片查看' if value is None else ''
            self.paint_preview()
        self.paint_asset_strip()

    def open_source(self):
        selected = self.asset_tree.selection()
        asset = next((a for a in self.assets if selected and a['asset_id'] == selected[0]), {})
        path = asset.get('source_path') or self._asset_paths.get(asset.get('asset_id'))
        self._open_path(path, '请选择可用素材。')

    def _tasks_page(self, parent):
        left, center, right = self._columns(parent)
        ttk.Label(left, text='任务', style='TLabel', font=(CONTROL_FONT, 16, 'bold')).pack(anchor='w', pady=(0, 16))
        self.group_tree = self.tree(left, [('title', '批次 / 单成品')], [172], 6, rail=True)
        self.group_tree.configure(selectmode='browse')
        self.group_tree.bind('<<TreeviewSelect>>', lambda _: self.select_group())
        self.task_title = tk.StringVar(value='任务与草稿')
        ttk.Label(center, textvariable=self.task_title, style='Title.TLabel').pack(anchor='w')
        self.batch_summary = tk.StringVar(value='暂无任务')
        self.task_summary_label = ttk.Label(center, textvariable=self.batch_summary, style='Muted.TLabel', wraplength=600)
        self.task_summary_label.pack(fill='x', pady=(10, 18))
        self.task_summary_label.bind('<Configure>', lambda e: self.task_summary_label.configure(wraplength=max(150, e.width)))
        self.task_tree = self.tree(center, [('title', '成品 / 剪辑要求'), ('status', '状态'), ('time', '创建时间')], [170, 130, 100], 3)
        self.task_tree.configure(style='Task.Treeview')
        self.task_tree.master.pack_configure(expand=False)
        self.task_tree.configure(selectmode='browse')
        self.task_tree.bind('<<TreeviewSelect>>', lambda _: self.draw_job())
        self.timeline_panel = ttk.Frame(center)
        self.timeline_panel.pack(side='bottom', fill='x', pady=(12, 0))
        self.timeline_heading = tk.StringVar(value='批次时间区段')
        timeline_bar = ttk.Frame(self.timeline_panel); timeline_bar.pack(fill='x')
        self.timeline_toggle = self.button(timeline_bar, '展开时间区段', self.toggle_timeline, side='left')
        self.button(timeline_bar, '批次报告', self.open_batch_report, side='right')
        self.timeline_canvas = tk.Canvas(self.timeline_panel, bg=BG, height=70, highlightthickness=0)
        self.timeline_canvas.bind('<Configure>', lambda _: self.paint_timeline())
        self.delivery_panel = ttk.Frame(center)
        self.delivery_panel.pack(fill='both', expand=True, pady=(16, 0))
        monitor = self.delivery_panel
        ttk.Label(monitor, text='成品预览', style='Heading.TLabel').pack(anchor='w')
        self.output_preview_canvas = tk.Canvas(monitor, bg=PANEL, height=220, takefocus=1,
                                               highlightthickness=1, highlightbackground=LINE)
        self.output_preview_canvas.pack(fill='both', expand=True, pady=(10, 6))
        self.output_preview_canvas.bind('<Configure>', lambda _: self.paint_output_preview())
        self.output_preview_canvas.bind('<Button-1>', lambda _: self.toggle_output_preview())
        self.output_preview_canvas.bind('<space>', lambda _: self.toggle_output_preview())
        self.output_preview_canvas.bind('<Return>', lambda _: self.toggle_output_preview())
        self.output_preview_canvas.bind('<FocusIn>', lambda _: self.output_preview_canvas.configure(highlightbackground=TEXT))
        self.output_preview_canvas.bind('<FocusOut>', lambda _: self.output_preview_canvas.configure(highlightbackground=LINE))
        transport = ttk.Frame(monitor); transport.pack(side='bottom', fill='x', before=self.output_preview_canvas)
        self.output_preview_status = tk.StringVar(value='选择已完成的 MP4 查看预览')
        self.output_status_label = ttk.Label(transport, textvariable=self.output_preview_status, style='Muted.TLabel', wraplength=400)
        self.output_status_label.pack(fill='x')
        self.output_status_label.bind('<Configure>', lambda e: self.output_status_label.configure(wraplength=max(180,e.width)))
        self.output_preview_seek = tk.DoubleVar(value=0.0)
        self.output_preview_scale = ttk.Scale(transport, from_=0.0, to=1.0,
                                              variable=self.output_preview_seek,
                                              orient='horizontal')
        self.output_preview_scale.pack(fill='x', pady=(7, 0))
        self.output_preview_scale.bind('<ButtonRelease-1>', self.seek_output_preview)
        preview_tools = ttk.Frame(transport)
        preview_tools.pack(fill='x', pady=(8, 6))
        self.output_preview_toggle_button = self.button(preview_tools, '播放预览',
                                                        self.toggle_output_preview, side='left')
        self.output_preview_reset_button = self.button(preview_tools, '回到开头',
                                                       self.reset_output_preview, side='left', padx=8)
        self.output_preview_open_button = self.button(preview_tools, '打开文件',
                                                      self.open_output_preview_file,
                                                      side='right')
        ttk.Label(right, text='任务详情', style='Heading.TLabel').pack(anchor='w')
        self.task_state_var = tk.StringVar(value='未选择任务')
        self.task_state_label = ttk.Label(right, textvariable=self.task_state_var, font=(UI_FONT, 13, 'bold'), wraplength=248)
        self.task_state_label.pack(fill='x', pady=(16, 8))
        self.task_problem_var = tk.StringVar()
        self.task_problem_label = ttk.Label(right, textvariable=self.task_problem_var, style='Muted.TLabel', wraplength=248, justify='left')
        self.task_problem_label.pack(fill='x', pady=(0, 12))
        ttk.Label(right, text='产物与过程记录', style='Muted.TLabel').pack(anchor='w')
        self.draft_name = tk.StringVar(value='选择一个成品')
        ttk.Label(right, textvariable=self.draft_name, wraplength=248, foreground=TEXT).pack(anchor='w', pady=(8, 10))
        self.detail = self._text(right, height=7)
        self.detail.pack(fill='both', expand=True)
        self.detail.configure(state='disabled')
        self.detail.tag_configure('warning', foreground=AMBER)
        self.detail.tag_configure('bad', foreground=RED)
        self.detail.tag_configure('heading', font=(UI_FONT, 10, 'bold'))
        # Dynamic task actions share one bottom action rail, so the detail
        # panel can remain the main reading area instead of pushing buttons
        # below the fold.
        self.task_actions = ttk.Frame(right)
        self.task_actions.pack(side='bottom', fill='x', pady=(10, 0), before=right.winfo_children()[0])
        self.action_buttons = {}
        self.folder_actions = ttk.Frame(self.task_actions)
        self.folder_actions.columnconfigure((0, 1), weight=1, uniform='folder_actions')
        for action, label in [('open_folder', '打开草稿目录 ↗'), ('publish', '立即登记'),
                              ('change_output_dir', '更改成品目录'), ('diagnose', '查看并修复'), ('resume', '从检查点继续'),
                              ('retry_batch_child', '重试本条成品'), ('cancel', '取消任务')]:
            parent = self.folder_actions if action in ('open_folder', 'change_output_dir') else self.task_actions
            self.action_buttons[action] = self.button(parent, label,
                                                      lambda a=action: self.job_action(a))
        self.task_export_button = self.button(self.task_actions, '前往独立导出   →', self.go_export,
                                              primary=True, fill='x', pady=(10, 0))

    def toggle_timeline(self):
        if self.timeline_canvas.winfo_manager():
            self.timeline_canvas.pack_forget()
            self.timeline_toggle.configure(text='展开时间区段')
        else:
            self.timeline_canvas.pack(fill='x')
            self.timeline_toggle.configure(text='收起时间区段')
            self.paint_timeline()

    def render_job_groups(self):
        groups = {}
        for batch in self.batches.values():
            groups[batch['batch_id']] = ('批次 · '+batch['batch_id'][-8:],
                [c['job_id'] for c in batch.get('children', []) if c.get('job_id') in self.jobs])
        for job in self.jobs.values():
            link = (job.get('batch') or {}).get('batch_id')
            if link not in groups:
                groups[job['job_id']] = (job.get('instruction', '单成品').replace('\n', ' ')[:22], [job['job_id']])
        def created_time(record):
            try:
                value = datetime.fromisoformat(str(record.get('created_at', '')).replace('Z', '+00:00'))
                return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
            except (ValueError, TypeError, OverflowError, OSError):
                return float('-inf')

        def group_created(item):
            iid, (_, children) = item
            value = created_time(self.batches.get(iid, self.jobs.get(iid, {})))
            if value == float('-inf'):
                value = max((created_time(self.jobs[j]) for j in children), default=value)
            return value

        # Mix batches and single outputs in one chronology. Updates/retries
        # must not promote old jobs ahead of newly created work.
        groups = dict(sorted(groups.items(), key=group_created, reverse=True))
        self._task_groups = groups
        if self._group not in groups:
            self._group = next(iter(groups), None)
        for iid in self.group_tree.get_children():
            if iid not in groups:
                self.group_tree.delete(iid)
        for index, (iid, (title, children)) in enumerate(groups.items()):
            states = [self.jobs[j].get('state') for j in children]
            suffix = f'{len(children):02d} 条' if iid in self.batches else '单成品'
            if 'FAILED' in states:
                status = '有失败项'
            elif any(s in ('WAITING_FOR_INPUT', 'BLOCKED_RESOURCES') for s in states):
                status = '待处理'
            elif states and all(s == 'COMPLETED' for s in states):
                status = '已完成'
            elif states and all(s == 'CANCELLED' for s in states):
                status = '已取消'
            else:
                status = '处理中' if states else '暂无成品'
            # Keep one readable line per title; full brief lives in the detail pane.
            if not hasattr(self, '_group_title_font'):
                self._group_title_font = tkfont.Font(root=self.window, family=UI_FONT, size=10)
                self._group_title_cache = OrderedDict()
            font = self._group_title_font
            limit = max(100, self.group_tree.winfo_width()-22)
            key = (title, limit)
            short = self._group_title_cache.get(key)
            if short is None:
                short = title
                if font.measure(title) > limit:
                    low, high = 0, len(title)
                    while low < high:
                        middle = (low+high+1)//2
                        if font.measure(title[:middle]+'…') <= limit: low = middle
                        else: high = middle-1
                    short = title[:low]+'…'
                self._group_title_cache[key] = short
                if len(self._group_title_cache) > 1024:
                    self._group_title_cache.popitem(last=False)
            values = (short+'\n'+suffix+' · '+status,)
            tags = (('bad',) if 'FAILED' in states else ('warning',)
                    if any(state in ('WAITING_FOR_INPUT', 'BLOCKED_RESOURCES') for state in states) else ())
            if self.group_tree.exists(iid):
                self.group_tree.item(iid, values=values, tags=tags)
                self.group_tree.move(iid, '', index)
            else:
                self.group_tree.insert('', index, iid=iid, values=values, tags=tags)
        if self._group and self.group_tree.selection() != (self._group,):
            self.group_tree.selection_set(self._group)
        self.render_group_items()

    def select_group(self):
        selected = self.group_tree.selection()
        if selected and selected[0] != self._group:
            self._group = selected[0]
            self.render_group_items()
            self.draw_job()

    def render_group_items(self):
        from .desktop_workbench import job_label, local_time
        selected = self.task_tree.selection()
        title, ids = self._task_groups.get(self._group, ('任务与草稿', []))
        for iid in self.task_tree.get_children():
            if iid not in ids:
                self.task_tree.delete(iid)
        for index, iid in enumerate(ids):
            job = self.jobs[iid]
            link = job.get('batch') or {}
            instruction = job.get('instruction', '').replace('\n', ' ')
            name = (link['output_id']+' · '+instruction[:24]) if link.get('output_id') else instruction
            values = (name, job_label(job), local_time(job.get('created_at')))
            tags = (('bad',) if job.get('state') == 'FAILED' else ('warning',)
                    if job.get('state') in ('WAITING_FOR_INPUT', 'BLOCKED_RESOURCES') else ())
            if self.task_tree.exists(iid):
                self.task_tree.item(iid, values=values, tags=tags)
                self.task_tree.move(iid, '', index)
            else:
                self.task_tree.insert('', index, iid=iid, values=values, tags=tags)
        target = selected[0] if selected and selected[0] in ids else (ids[0] if ids else None)
        if target and self.task_tree.selection() != (target,):
            self.task_tree.selection_set(target)
        batch = self.batches.get(self._group)
        self.task_title.set('批次 · '+self._group[-8:] if batch else '单成品任务' if ids else '任务与草稿')
        states = [self.jobs[i].get('state') for i in ids]
        self.batch_summary.set(f"{len(ids)} 条成品 · 完成 {states.count('COMPLETED')} · 失败 {states.count('FAILED')} · 待处理 {sum(s in ('WAITING_FOR_INPUT', 'BLOCKED_RESOURCES') for s in states)}" if ids else '添加素材并创建任务，进度和成品会显示在这里。')
        if batch:
            if not self.timeline_panel.winfo_manager():
                self.timeline_panel.pack(side='bottom', fill='x', pady=(12,0), before=self.delivery_panel)
        else:
            self.timeline_panel.pack_forget()
        self._sections, self._timeline_error = read_sections(batch) if batch else ([], '单成品任务不含批次分段')
        self.paint_timeline()

    def paint_timeline(self):
        c = self.timeline_canvas
        c.delete('all')
        w = c.winfo_width()
        rows = self._sections
        if not rows:
            c.create_text(14, 48, anchor='w', text=getattr(self, '_timeline_error', '主草稿尚未生成'), fill=MUTED,
                          font=(UI_FONT, 10), width=max(100, w-25))
            return
        end = rows[-1]['end_us']
        span = max(1, w-28)
        for index, row in enumerate(rows):
            a, b = 14+span*row['start_us']/end, 14+span*row['end_us']/end
            c.create_rectangle(a, 20, b, 46, fill=SELECT, outline=LINE)
            if b-a > 38:
                c.create_text((a+b)/2, 33, text=f'{index+1:02d}', fill=TEXT, font=(UI_FONT, 10))
                c.create_text(a, 60, anchor='w', text=f"{row['start_us']/1e6:g}s", fill=MUTED, font=(UI_FONT, 9))
            if index < len(rows)-1:
                nxt = 14+span*rows[index+1]['start_us']/end
                gap = (rows[index+1]['start_us']-row['end_us'])/1e6
                if nxt-b > 15:
                    c.create_text((b+nxt)/2, 12, text=f'{gap:g}s', fill=DIM, font=(UI_FONT, 9))

    @staticmethod
    def _job_output_file(job):
        """Return only a verified-looking delivery path recorded by the backend."""
        job = job or {}
        delivery = job.get('delivery') or {}
        report = job.get('direct_render_report') or {}
        path = delivery.get('output_file') or report.get('output_file')
        return str(path) if path else None

    @staticmethod
    def _delivery_duration(job):
        delivery = (job or {}).get('delivery') or {}
        media = delivery.get('media') or {}
        for value in (delivery.get('duration_us'), media.get('duration_us')):
            try:
                if value is not None and float(value) > 0:
                    return float(value) / 1_000_000
            except (TypeError, ValueError):
                pass
        for value in (delivery.get('duration_ms'), media.get('duration_ms')):
            try:
                if value is not None and float(value) > 0:
                    return float(value) / 1_000
            except (TypeError, ValueError):
                pass
        for value in (delivery.get('duration_s'), media.get('duration_s')):
            try:
                if value is not None and float(value) > 0:
                    return float(value)
            except (TypeError, ValueError):
                pass
        return 0.0

    def _output_preview_install_dir(self):
        settings = getattr(self, 'settings_vars', {})
        variable = settings.get('install_dir') if isinstance(settings, dict) else None
        configured = variable.get().strip() if variable else ''
        if configured:
            return configured
        # The portable bundle always ships its own FFmpeg.  Do not depend on
        # a system PATH or on an optional Jianying installation for preview.
        return str(Path(__file__).resolve().parent.parent / 'toolchain')

    def paint_output_preview(self):
        canvas = getattr(self, 'output_preview_canvas', None)
        if canvas is None:
            return
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 30 or height < 30:
            return
        if not hasattr(self, '_output_canvas_items'):
            self._output_canvas_items = (
                canvas.create_image(0,0), canvas.create_polygon(0,0,1,1,fill=MUTED,outline=''),
                canvas.create_text(0,0,fill=MUTED,font=(UI_FONT,10)))
        frame, icon, caption = self._output_canvas_items
        canvas.coords(frame,width/2,height/2)
        canvas.coords(icon,width/2-10,height/2-28,width/2-10,height/2+2,width/2+16,height/2-13)
        canvas.coords(caption,width/2,height/2+32)
        image = self._output_preview_image
        if image is not None:
            from PIL import ImageOps, ImageTk
            key = (id(image),width,height)
            if key != getattr(self, '_output_draw_key', None):
                fitted = ImageOps.contain(image, (max(1, width-24), max(1, height-24)))
                photo = getattr(self, '_output_preview_photo', None)
                if photo is not None and (photo.width(),photo.height()) == fitted.size:
                    photo.paste(fitted)
                else:
                    photo = ImageTk.PhotoImage(fitted, master=self.window)
                    canvas.itemconfigure(frame,image=photo)
                    self._output_preview_photo = photo
                self._output_draw_key = key
            canvas.itemconfigure(frame,state='normal')
            canvas.itemconfigure(icon,state='hidden');canvas.itemconfigure(caption,state='hidden')
            return
        canvas.itemconfigure(frame,state='hidden',image='')
        self._output_preview_photo = None;self._output_draw_key = None
        label = '成品预览' if self._output_preview_path else '成品生成后，在这里预览'
        if not self._output_preview_path and (self.selected_job() or {}).get('state') in ('WAITING_FOR_INPUT', 'BLOCKED_RESOURCES', 'FAILED'):
            label = '任务尚未生成成片\n点击右侧「查看并修复」检查原因与文案'
        canvas.itemconfigure(icon,state='normal')
        canvas.itemconfigure(caption,state='normal',text=label,width=max(100,width-32))

    def _set_output_preview_buttons(self):
        if not hasattr(self, 'output_preview_toggle_button'):
            return
        can_preview = bool(self._output_preview_path)
        self.output_preview_canvas.configure(cursor='hand2' if can_preview else 'arrow', takefocus=int(can_preview))
        self.output_preview_toggle_button.configure(
            text='暂停预览' if self._output_preview_playing else '播放预览')
        self.output_preview_toggle_button.state(['!disabled'] if can_preview and not self.busy else ['disabled'])
        self.output_preview_reset_button.state(['!disabled'] if can_preview and not self.busy else ['disabled'])
        self.output_preview_open_button.state(['!disabled'] if can_preview and not self.busy else ['disabled'])
        self.output_preview_scale.state(['!disabled'] if can_preview and not self.busy else ['disabled'])

    def open_output_preview_file(self):
        if self._output_preview_path and Path(self._output_preview_path).is_file():
            os.startfile(self._output_preview_path)
        else:
            self.notice('当前成品尚未生成可打开的 MP4。', True)

    def sync_output_preview(self, job=None):
        """Bind the task selection to a read-only, in-app MP4 preview."""
        path = self._job_output_file(job)
        if path:
            path = str(Path(path))
        valid = bool(path and Path(path).is_file())
        if not valid:
            path = None
        if path == self._output_preview_path:
            self._set_output_preview_buttons()
            return
        self._stop_output_preview_stream()
        self._output_preview_playing = False
        self._output_preview_position = 0.0
        self._output_preview_duration = self._delivery_duration(job)
        self._output_preview_path = path
        self._output_audio_status='内嵌有声预览'
        self._output_preview_image = None
        self.output_preview_scale.configure(to=max(1.0, self._output_preview_duration or 1.0))
        self.output_preview_seek.set(0.0)
        if path:
            self.output_preview_status.set('正在读取成品预览…')
            self._request_output_preview_frame()
        else:
            recorded = self._job_output_file(job)
            self.output_preview_status.set('MP4 已记录，但文件尚未找到' if recorded else '选择已完成的 MP4 查看预览')
            self.paint_output_preview()
        self._set_output_preview_buttons()

    @staticmethod
    def _load_output_preview_frame(path, position, install_dir):
        from PIL import Image
        from .independent_export import discover_tools
        tools = discover_tools(install_dir)
        ffmpeg = tools.get('ffmpeg')
        if not ffmpeg:
            return None
        try:
            args = [ffmpeg, '-hide_banner', '-v', 'error', '-nostdin',
                    '-ss', f'{max(0.0, float(position)):.3f}', '-i', str(path),
                    '-frames:v', '1', '-vf', 'scale=720:720:force_original_aspect_ratio=decrease',
                    '-f', 'image2pipe', '-vcodec', 'png', '-']
            result = subprocess.run(args, capture_output=True, timeout=15,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode or not result.stdout:
                return None
            with Image.open(io.BytesIO(result.stdout)) as picture:
                return picture.convert('RGB')
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None

    def _request_output_preview_frame(self):
        path = self._output_preview_path
        if not path or not hasattr(self, 'preview_executor') or self.closed:
            return
        token = self._output_preview_token
        position = self._output_preview_position
        if self._output_preview_future is not None:
            self._output_preview_future.cancel()
        future = self.preview_executor.submit(self._load_output_preview_frame,
                                              path, position, self._output_preview_install_dir())
        self._output_preview_future = future
        future.add_done_callback(lambda f, t=token, p=position:
                                 self.inbox.put(('video_preview', f,
                                                lambda value: self._output_preview_ready(t, p, value))))

    def _stop_output_preview_stream(self):
        self._output_preview_token += 1
        stream, self._output_preview_stream = self._output_preview_stream, None
        if stream is not None:
            stream.stop()
        future = self._output_preview_future
        if future is not None:
            future.cancel()
            self._output_preview_future = None

    def _poll_output_preview_stream(self, token):
        if token != self._output_preview_token or not self._output_preview_playing or self.closed:
            return
        stream = self._output_preview_stream
        if stream is None:
            return
        status=getattr(stream,'audio_status',None)
        if isinstance(status,str):self._output_audio_status=status
        terminal = None
        latest = None
        while True:
            try:
                kind, value = stream.frames.get_nowait()
            except queue.Empty:
                break
            if kind == 'frame':
                latest = value
            elif kind in ('ended', 'error'):
                terminal = kind
        if latest is not None:
            position, image = latest
            self._output_preview_position = position
            self._output_preview_image = image
            if self._output_preview_duration:
                self.output_preview_seek.set(min(self._output_preview_duration, position))
                self.output_preview_status.set(
                    f'预览  {position:05.1f}s / {self._output_preview_duration:05.1f}s · {self._output_audio_status}')
            else:
                self.output_preview_status.set(f'预览  {position:05.1f}s · {self._output_audio_status}')
            self.paint_output_preview()
        if terminal:
            self._output_preview_playing = False
            if terminal == 'error':
                self.output_preview_status.set('预览失败：'+str(getattr(stream,'error',None) or '读取失败，请重试'))
            else:
                if self._output_preview_duration:
                    self._output_preview_position = self._output_preview_duration
                    self.output_preview_seek.set(self._output_preview_duration)
                self.output_preview_status.set('预览结束 · 点击播放预览重新开始')
            self._set_output_preview_buttons()
            self._stop_output_preview_stream()
            return
        self.later(15, lambda: self._poll_output_preview_stream(token))

    def _output_preview_ready(self, token, requested_position, value):
        if token != self._output_preview_token or not self._output_preview_path:
            return
        if value is None:
            self._output_preview_playing = False
            self.output_preview_status.set('预览帧读取失败，可打开输出文件查看。')
            self.paint_output_preview()
            self._set_output_preview_buttons()
            return
        self._output_preview_image = value
        self._output_preview_position = requested_position
        if self._output_preview_duration:
            self.output_preview_seek.set(min(self._output_preview_duration, requested_position))
        if self._output_preview_duration:
            self.output_preview_status.set(
                f'预览  {requested_position:05.1f}s / {self._output_preview_duration:05.1f}s · 待播放')
        else:
            self.output_preview_status.set(f'预览  {requested_position:05.1f}s · 待播放')
        self.paint_output_preview()
        self._set_output_preview_buttons()

    def toggle_output_preview(self):
        if self.busy or self.current_view != 'tasks':
            return
        if not self._output_preview_path:
            self.notice('当前成品尚未生成可读取的 MP4。', True)
            return
        if self._output_preview_playing:
            self._output_preview_playing = False
            self._stop_output_preview_stream()
            self.output_preview_status.set('已暂停预览')
            self._set_output_preview_buttons()
            return
        if self._output_preview_duration and self._output_preview_position >= self._output_preview_duration - 0.05:
            self._output_preview_position = 0.0
        self._output_preview_playing = True
        self._stop_output_preview_stream()
        token = self._output_preview_token
        w,h = max(320,self.output_preview_canvas.winfo_width()-24), max(240,self.output_preview_canvas.winfo_height()-24)
        scale = min(1.0,960/w,640/h)
        size = (max(2,int(w*scale)//2*2),max(2,int(h*scale)//2*2))
        stream = OutputPreviewStream(self._output_preview_path, self._output_preview_position,
                                     self._output_preview_install_dir(), size=size)
        self._output_preview_stream = stream
        stream.start()
        self.later(15, lambda: self._poll_output_preview_stream(token))
        self._set_output_preview_buttons()

    def reset_output_preview(self):
        if not self._output_preview_path:
            return
        self._stop_output_preview_stream()
        self._output_preview_playing = False
        self._output_preview_position = 0.0
        self.output_preview_status.set('正在回到开头…')
        self._request_output_preview_frame()
        self._set_output_preview_buttons()

    def seek_output_preview(self, event=None):
        if not self._output_preview_path:
            return 'break'
        try:
            position = max(0.0, float(self.output_preview_seek.get()))
        except (TypeError, ValueError, tk.TclError):
            return 'break'
        self._stop_output_preview_stream()
        self._output_preview_playing = False
        self._output_preview_position = position
        self.output_preview_status.set(f'正在定位到 {position:05.1f}s…')
        self._request_output_preview_frame()
        self._set_output_preview_buttons()
        return 'break'

    def sync_draft_panel(self):
        job = self.selected_job() or {}
        self.sync_output_preview(job)
        batch = self.batches.get((job.get('batch') or {}).get('batch_id')) or {}
        final = batch.get('finalization') or {}
        self._draft_path = ((final.get('registration') or {}).get('registered_draft_path') or
                            final.get('master_draft') or (job.get('publication') or {}).get('registered_draft_path'))
        if not self._draft_path and job.get('render_backend') == 'direct_ffmpeg':
            output_file = (job.get('delivery') or {}).get('output_file')
            self._draft_path = str(Path(output_file).parent) if output_file else None
        self.draft_name.set(Path(self._draft_path).name if self._draft_path else '尚无可打开产物')
        direct = job.get('render_backend') == 'direct_ffmpeg'
        show_folders = bool(self._draft_path) or direct
        if show_folders and not self.folder_actions.winfo_manager():
            self.folder_actions.pack(fill='x', pady=(6, 0), before=self.task_export_button)
        elif not show_folders:
            self.folder_actions.pack_forget()
        self.action_buttons['open_folder'].grid(row=0, column=0, sticky='ew', padx=(0, 4) if direct else 0,
                                               columnspan=1 if direct else 2)
        if direct:
            self.action_buttons['change_output_dir'].grid(row=0, column=1, sticky='ew', padx=(4, 0))
        else:
            self.action_buttons['change_output_dir'].grid_remove()
        # Keep all actions available, showing only those valid for this selection.
        for action, button in self.action_buttons.items():
            if action in ('open_folder', 'change_output_dir'):
                continue
            visible = not button.instate(['disabled']) or (action == 'open_folder' and bool(self._draft_path))
            if visible and not button.winfo_manager():
                button.pack(fill='x', pady=(6, 0), before=self.task_export_button)
            elif not visible:
                button.pack_forget()
        if self._draft_path and not self.busy:
            self.action_buttons['open_folder'].state(['!disabled'])
        self.action_buttons['open_folder'].configure(text='打开成品目录' if job.get('render_backend') == 'direct_ffmpeg' else '打开草稿目录')

    def _open_path(self, path, error):
        if path and Path(path).exists():
            os.startfile(str(path))
        else:
            self.notice(error, True)

    def open_batch_report(self):
        batch = self.batches.get(self._group) or {}
        self._open_path(batch.get('report_path'), '此任务暂无批次报告。')

    def go_export(self):
        job = self.selected_job() or {}
        batch_id = (job.get('batch') or {}).get('batch_id') or self._group
        if batch_id in self.batches:
            self.export_batch.set(batch_id)
            self.refresh_export_items()
        self.show('export')

    def _export_page(self, parent):
        left, center, right = self._columns(parent)
        ttk.Label(left, text='批次', style='TLabel', font=(CONTROL_FONT, 16, 'bold')).pack(anchor='w', pady=(0, 16))
        self.export_group_tree = self.tree(left, [('name', '可导出的批次')], [172], 6, rail=True)
        self.export_group_tree.configure(selectmode='browse')
        self.export_group_tree.bind('<<TreeviewSelect>>', lambda _: self.select_export_group())
        self.export_batch = tk.StringVar()
        self.header(center, '独立导出')
        self.export_metric = tk.StringVar(value='00 / 00')
        ttk.Label(center, textvariable=self.export_metric, style='Heading.TLabel').pack(anchor='w', pady=(16, 8))
        self.export_progress = ttk.Progressbar(center, mode='determinate', maximum=100)
        self.export_progress.pack(fill='x', pady=(0, 24))
        ttk.Label(center, text='导出列表', style='Heading.TLabel').pack(anchor='w', pady=(0, 14))
        export_details = ttk.Frame(center)
        export_details.pack(side='bottom', fill='x')
        self.export_tree = self.tree(center, [('id', '成品 ID'), ('status', '状态'), ('range', '绝对时间范围'), ('file', '输出文件')], [120, 88, 175, 160], 4)
        self.export_tree.configure(selectmode='browse')
        self.export_tree.bind('<<TreeviewSelect>>', lambda _: self.draw_export_error())
        tools = ttk.Frame(center)
        tools.pack(side='bottom', fill='x', pady=(12, 12), before=self.export_tree.master)
        self.export_retry_button = self.button(tools, '重试选中成品', self.retry_export, side='left')
        self.export_cancel_button = self.button(tools, '取消导出', self.cancel_export, side='right')
        ttk.Label(export_details, text='成品详情 / 错误信息', style='Muted.TLabel').pack(anchor='w')
        self.export_error = self._text(export_details, height=3)
        self.export_error.pack(fill='x', pady=(8, 8))
        self.export_error.configure(state='disabled')
        self.export_report_var = tk.StringVar(value='选择批次以查看导出状态。')
        report_label = ttk.Label(export_details, textvariable=self.export_report_var, style='Muted.TLabel', wraplength=600)
        report_label.pack(fill='x')
        report_label.bind('<Configure>', lambda e: report_label.configure(wraplength=max(100, e.width)))
        self.button(export_details, '查看导出报告 ↗', self.open_export_report, anchor='e', pady=(10, 0))
        ttk.Label(right, text='导出设置', style='Heading.TLabel').pack(anchor='w')
        ttk.Label(right, text='保存到', style='Muted.TLabel').pack(anchor='w', pady=(28, 12))
        self.export_output = tk.StringVar()
        self.export_directory_button = self.button(right, '选择输出文件夹', self.pick_export_directory, fill='x')
        self.export_directory_entry = ttk.Entry(right, textvariable=self.export_output)
        self.export_directory_entry.pack(fill='x', pady=(12, 0))
        self.export_output.trace_add('write', lambda *_: self.export_action_states())
        self._line(right)
        ttk.Label(right, text='同名文件', style='Muted.TLabel').pack(anchor='w', pady=(0, 10))
        self.export_conflict = tk.StringVar(value='自动重命名')
        self.export_conflict_select = ttk.Combobox(right, textvariable=self.export_conflict, values=['自动重命名', '覆盖', '跳过'], state='readonly', width=14)
        self.export_conflict_select.pack(fill='x')
        self._line(right)
        for label, value in [('格式', 'MP4'), ('音频', 'AAC'), ('画幅 / 帧率', '按草稿时间线'), ('后端', 'FFmpeg')]:
            row = ttk.Frame(right); row.pack(fill='x', pady=(0, 18))
            ttk.Label(row, text=label, style='Muted.TLabel').pack(side='left')
            ttk.Label(row, text=value, foreground=GREEN if label == '后端' else TEXT).pack(side='right')
        ttk.Label(right, text='每条成品独立输出\n无法还原的效果会明确报错', style='Muted.TLabel', wraplength=230).pack(anchor='w', pady=(8, 0))
        self.export_start_button = self.button(right, '批量开始     →', self.start_export, primary=True, side='bottom', fill='x', pady=(14, 0))
        self.button(right, '打开输出目录 ↗', self.open_export_directory, side='bottom', fill='x', pady=(14, 0))
        self.export_action_states()

    def select_export_group(self):
        selected = self.export_group_tree.selection()
        if selected and selected[0] != self.export_batch.get():
            self.export_batch.set(selected[0])
            self.refresh_export_items()

    def render_export_groups(self):
        values = set(self.batches)
        # Preserve previously exported batches even when they are outside the job list limit.
        values.update(r['batch_id'] for r in self.snapshot.get('external_exports', []) if r.get('batch_id'))
        for iid in self.export_group_tree.get_children():
            if iid not in values:
                self.export_group_tree.delete(iid)
        for iid in sorted(values):
            batch = self.batches.get(iid) or {}
            report = next((r for r in self.snapshot.get('external_exports', []) if r.get('batch_id') == iid), {})
            n = len(batch.get('children') or report.get('items') or [])
            label = '批次 · '+iid[-8:]+'\n'+f'{n:02d} 条独立成品'
            if self.export_group_tree.exists(iid):
                self.export_group_tree.item(iid, values=(label,))
            else:
                self.export_group_tree.insert('', 'end', iid=iid, values=(label,))
        if self.export_batch.get() not in values:
            self.export_batch.set(next(iter(sorted(values)), ''))
        if self.export_batch.get() and self.export_group_tree.selection() != (self.export_batch.get(),):
            self.export_group_tree.selection_set(self.export_batch.get())

    def export_action_states(self):
        if not hasattr(self, 'export_start_button'):
            return
        report = self._selected_export_report() or {}
        direct = self._direct_export_view() is not None
        active = report.get('state') in ('QUEUED', 'PREPARING', 'EXPORTING')
        ready = bool(self.export_batch.get() and self.export_output.get().strip()) and not direct
        selected = self.export_tree.selection()
        row = next((r for r in report.get('items', []) if selected and r.get('output_id') == selected[0]), {})
        for button, enabled in [(self.export_start_button, ready and not active),
                                (self.export_retry_button, row.get('status') in ('FAILED', 'CANCELLED') and not active and not direct),
                                (self.export_cancel_button, active and not direct)]:
            button.state(['!disabled'] if enabled and not self.busy else ['disabled'])
        self.export_start_button.configure(text='自动生成 MP4' if direct else '批量开始     →')
        for control in (self.export_directory_button, self.export_directory_entry):
            control.state(['disabled'] if direct or self.busy else ['!disabled'])
        self.export_conflict_select.configure(state='disabled' if direct or self.busy else 'readonly')

    def open_export_report(self):
        from .independent_export import REPORT_NAME
        report = self._selected_export_report() or {}
        direct = self._direct_export_view()
        if direct is not None:
            self._open_path(direct['batch'].get('report_path'), '该批次还没有成品报告。')
            return
        folder = report.get('output_dir')
        self._open_path(Path(folder)/REPORT_NAME if folder else None, '该批次还没有导出报告。')

    def _move_nav_indicator(self, animate=True):
        view = getattr(self, 'current_view', None)
        if self.closed or view not in self.nav_buttons:
            return
        target = self.nav_buttons[view].winfo_y() + 10
        self._nav_animation_token += 1
        token = self._nav_animation_token
        start = self._nav_indicator_y
        if not animate or start is None or abs(target-start) < 1:
            self._nav_indicator_y = target
            self._nav_indicator.coords(self._nav_marker, 0, target, 3, target+24)
            return
        began = time.perf_counter()
        def frame():
            if token != self._nav_animation_token or self.closed:
                return
            ratio = min(1.0, (time.perf_counter()-began)/.12)
            eased = 1-(1-ratio)**3
            self._nav_indicator_y = start+(target-start)*eased
            self._nav_indicator.coords(self._nav_marker, 0, self._nav_indicator_y, 3, self._nav_indicator_y+24)
            if ratio < 1:
                self.later(16, frame)
        frame()

    def show(self, view):
        if self.closed or view not in self.views:
            return
        previous = getattr(self, 'current_view', None)
        if previous == view:
            return
        if previous == 'create':
            self._pause_source_preview(repaint=False)
        if previous == 'tasks' and self._output_preview_playing:
            self._output_preview_playing = False
            self._stop_output_preview_stream()
            self._set_output_preview_buttons()
        # All pages remain mapped, with retained controls and canvas images.
        # Raise the prepared page once; only the navigation marker animates.
        self.current_view = view
        self._page_transition_until = time.perf_counter() + .14
        self.views[view].tkraise()
        title = {'create': '创作台', 'templates': '模板库', 'tasks': '任务与草稿', 'export': '独立导出', 'settings': '环境与设置'}[view]
        self.breadcrumb.set('Framo  /  '+title)
        if previous in self.nav_buttons:
            self.nav_buttons[previous].configure(selected=False)
        self.nav_buttons[view].configure(selected=True)
        self._move_nav_indicator(animate=previous is not None)
        # Let Tk expose the retained page and finish the 120 ms indicator
        # animation before touching lists, reports and preview frames.
        token = getattr(self, '_page_refresh_token', 0) + 1
        self._page_refresh_token = token
        def refresh_page():
            if not self.closed and token == self._page_refresh_token and self.snapshot:
                self.render_status(self.snapshot, visible_only=True)
                if view == 'create':
                    self.paint_preview()
                    self.paint_asset_strip()
        self.later(140, refresh_page)
