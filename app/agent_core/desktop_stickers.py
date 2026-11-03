"""Preview downloaded sticker assets with their real pixels and provenance."""
import hashlib,json,tkinter as tk
from pathlib import Path
from tkinter import ttk
from PIL import Image,ImageTk
from . import desktop_studio as studio

ROOT=Path(__file__).resolve().parents[1]/'desktop-assets/media-library/sticker-library'

def category_rows(rows,category):
    if category=='高级立体':return [r for r in rows if r.get('collection')=='premium_commerce_3d_v1']
    if category=='国内电商':return [r for r in rows if r.get('collection') in ('premium_commerce_3d_v1','iconpark_cn_commerce')]
    return rows

class StickerLibrary:
    def __init__(self,parent):
        self.rows=json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))['stickers']
        self.images=[];self.cards=[]
        self.window=tk.Toplevel(parent);self.window.title('贴纸库 · 高级立体')
        self.window.configure(bg=studio.BG);self.window.geometry('900x720');self.window.minsize(720,520)
        self.window.transient(parent);self.window.protocol('WM_DELETE_WINDOW',self.close)
        self.window.bind('<Escape>',lambda _:self.close())
        header=ttk.Frame(self.window,padding=(20,16));header.pack(fill='x')
        ttk.Label(header,text='贴纸库',style='Heading.TLabel').pack(side='left')
        self.category=tk.StringVar(value='高级立体')
        picker=ttk.Combobox(header,textvariable=self.category,values=('高级立体','国内电商','全部'),state='readonly',width=14)
        picker.pack(side='right');picker.bind('<<ComboboxSelected>>',lambda _:self.refresh())
        self.info=tk.StringVar()
        ttk.Label(self.window,textvariable=self.info,style='Muted.TLabel',wraplength=830).pack(anchor='w',padx=20,pady=(0,12))
        footer=ttk.Frame(self.window,padding=(20,12));footer.pack(side='bottom',fill='x')
        ttk.Label(footer,text='仅预览素材；创建任务后按剪辑要求自动选用。',style='Muted.TLabel').pack(side='left')
        studio.CutButton(footer,'关闭',self.close,width=96).pack(side='right')
        body=ttk.Frame(self.window);body.pack(fill='both',expand=True,padx=20)
        self.canvas=tk.Canvas(body,bg=studio.BG,highlightthickness=0)
        scroll=ttk.Scrollbar(body,orient='vertical',command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set);scroll.pack(side='right',fill='y');self.canvas.pack(fill='both',expand=True)
        self.grid=ttk.Frame(self.canvas);self.item=self.canvas.create_window(0,0,window=self.grid,anchor='nw')
        self.grid.bind('<Configure>',lambda _:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',lambda e:self.canvas.itemconfigure(self.item,width=e.width))
        self.window.bind('<MouseWheel>',lambda e:self.canvas.yview_scroll(-int(e.delta/120),'units'))
        self.refresh()
        parent.update_idletasks()
        x=max(0,min(parent.winfo_screenwidth()-900,parent.winfo_rootx()+(parent.winfo_width()-900)//2))
        y=max(0,min(parent.winfo_screenheight()-760,parent.winfo_rooty()+(parent.winfo_height()-720)//2))
        self.window.geometry(f'900x720+{x}+{y}')

    def refresh(self):
        for widget in self.grid.winfo_children():widget.destroy()
        self.images=[];self.cards=[]
        category=self.category.get();rows=category_rows(self.rows,category)
        descriptions={'高级立体':'黑金材质与金属高光 · 400×400 原始透明素材 · 3dicons v1 / CC0 · 非 AI',
            '国内电商':'高级立体 15 款 + IconPark 36 款 · CC0 / Apache-2.0 · 按画面尺寸显示',
            '全部':'现有素材与下载素材；国内电商剪辑优先选用高级立体款'}
        self.info.set(f'{len(rows)} 款 · '+descriptions[category])
        for col in range(4):self.grid.columnconfigure(col,weight=1,uniform='stickers')
        for i,row in enumerate(rows):
            card=ttk.Frame(self.grid,padding=8);card.grid(row=i//4,column=i%4,sticky='nsew',padx=4,pady=6)
            path=(ROOT/row['file']).resolve()
            if not path.is_relative_to(ROOT.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']:
                ttk.Label(card,text='素材校验失败').pack();continue
            with Image.open(path) as im:
                im=im.convert('RGBA');im.thumbnail((144,144),Image.Resampling.LANCZOS)
                image=ImageTk.PhotoImage(im,master=self.window)
            self.images.append(image);ttk.Label(card,image=image).pack()
            label=row['label'].replace('国内电商 · ','').replace('高级立体 · ','')
            ttk.Label(card,text=label,wraplength=160,anchor='center').pack(fill='x',pady=(6,0))
            ttk.Label(card,text=f"{row['width']}×{row['height']}",style='Muted.TLabel').pack()
            self.cards.append(card)
        self.canvas.yview_moveto(0)
        self.window.after_idle(lambda:self.canvas.yview_moveto(0))

    def close(self):
        if self.window.winfo_exists():self.window.destroy()
        self.images.clear()
