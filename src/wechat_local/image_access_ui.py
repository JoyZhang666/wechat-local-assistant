"""Explicit image-only consent; no process inspection, key display or network."""
import queue
import threading
import tkinter as tk
from tkinter import ttk,filedialog

from .image_access import acquire_images


class ImageAccessDialog:
    def __init__(self,app,parent):
        self.app,self.parent=app,parent
        self.stop=threading.Event()
        self.events=queue.Queue(maxsize=128)
        self.thread=None
        self.closing=False
        self.window=window=tk.Toplevel(parent)
        window.title("图片离线接入 · 本次授权")
        window.transient(parent)
        window.grab_set()
        window.protocol("WM_DELETE_WINDOW",self.close)
        window.bind("<Destroy>",lambda event:self.stop.set() if event.widget is window else None)
        frame=ttk.Frame(window,padding=18)
        frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="只使用你选定的本人账号目录和2—9张不同V2加密JPEG图片，在本机限时派生并验证。\n不会读取微信进程或上传；不支持的格式会停止。原图和现有不匹配授权保持。\n选择已导出包中保留的加密图片，或本人微信目录下的相应文件。",wraplength=620).pack(fill="x")
        self.account=tk.StringVar()
        self.files=()
        ttk.Button(frame,text="1 选择本人账号目录",command=self.choose_root).pack(fill="x",pady=(12,6))
        ttk.Entry(frame,textvariable=self.account,width=64).pack(fill="x")
        ttk.Button(frame,text="2 选择2—9张加密图片",command=self.choose_files).pack(fill="x",pady=6)
        self.consent=tk.BooleanVar(value=False)
        ttk.Checkbutton(frame,text="这是本人数据，同意仅本次离线派生、验证并DPAPI加密保存",variable=self.consent).pack(anchor="w",pady=8)
        self.status=tk.StringVar(value="尚未选择图片；不会自动读取或接入。")
        ttk.Label(frame,textvariable=self.status,wraplength=620).pack(fill="x",pady=8)
        self.progress=ttk.Progressbar(frame,maximum=1)
        self.progress.pack(fill="x",pady=8)
        self.start_button=ttk.Button(frame,text="3 开始本次图片接入",style="Primary.TButton",command=self.start)
        self.start_button.pack(side="left")
        ttk.Button(frame,text="取消／关闭",command=self.close).pack(side="right")

    @property
    def busy(self):return self.thread is not None and self.thread.is_alive()

    def choose_root(self):
        if self.busy:return
        value=filedialog.askdirectory(parent=self.window,title="选择本人账号目录（含设备后缀）")
        if value:self.account.set(value)

    def choose_files(self):
        if self.busy:return
        values=filedialog.askopenfilenames(parent=self.window,title="仅选择本人账号2—9张不同V2加密JPEG图片",filetypes=[("加密图片","*.dat"),("所有文件","*.*")])
        if values:
            self.files=tuple(values)
            self.status.set(f"已选择{len(values)}张；授权开始后才会读取。")

    def start(self):
        if self.busy:return
        if not self.consent.get() or self.app.settings is None:
            self.status.set("请先保存本机配置，并确认本次图片接入授权。")
            return
        root,files,data=self.account.get().strip(),self.files,self.app.settings.data_dir
        self.stop.clear()
        self.closing=False
        self.start_button.configure(state="disabled")
        self.status.set("离线验证中，最多90秒；进度表示搜索范围，不是剩余时间。")
        def emit(kind,value):
            while True:
                try:
                    self.events.put_nowait((kind,value))
                    return
                except queue.Full:
                    if kind=="progress":return
                    try:self.events.get_nowait()
                    except queue.Empty:pass
        def work():
            try:
                result=acquire_images(data,root,files,consent=True,stop=self.stop.is_set,progress=lambda value:emit("progress",value))
                emit("done",result)
            except KeyboardInterrupt:emit("error","已取消；原图片与原授权保持。")
            except ValueError as error:emit("error",str(error))
            except Exception:emit("error","图片接入未完成；请核对配置和样本，原授权保持。")
        self.thread=threading.Thread(target=work,name="image-offline-access",daemon=False)
        self.thread.start()
        self.window.after(100,self.poll)

    def poll(self):
        if not self.window.winfo_exists():return
        while not self.events.empty():
            kind,value=self.events.get_nowait()
            if kind=="progress":self.progress.configure(value=value)
            elif kind=="error":self.status.set(value)
            elif kind=="done":
                self.status.set(f"{value['images_verified']}张格式与像素验证通过；已{'复用原' if value['reused'] else '加密保存'}授权。请重新导出。")
                self.app.invalidate()
        if self.busy:self.window.after(100,self.poll)
        else:
            self.start_button.configure(state="normal")
            self.consent.set(False)
            if self.closing:self.close()

    def close(self):
        if self.busy:
            self.closing=True
            self.stop.set()
            self.status.set("正在取消，请稍候…")
            return
        self.window.destroy()
        if self.parent.winfo_exists():self.parent.grab_set()
