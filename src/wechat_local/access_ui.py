"""Explicit first-use access UI. No attachment happens merely by opening it."""
import queue
import threading
import tkinter as tk
from tkinter import ttk

from .key_access import AccessError,connect_database


PHASES={"checking":"已发现微信，正在检查官方程序并自动识别账号…","waiting":"请和往常一样打开Windows微信并登录；程序会自动发现。",
        "attached":"首次连接正在等待微信完成登录／打开记录；可随时取消。","detached":"已解除临时附加。",
        "validating":"正在认证所选账号数据库与本人身份…","saved":"认证通过，已加密保存；没有常驻取钥程序。"}


class AccessDialog:
    def __init__(self,app,parent):
        self.app,self.parent=app,parent
        self.stop=threading.Event()
        self.thread=None
        self.events=queue.Queue()
        self.closing=False
        self.connected=False
        self.window=window=tk.Toplevel(parent)
        window.title("连接微信 · 自动识别")
        window.transient(parent)
        window.grab_set()
        window.protocol("WM_DELETE_WINDOW",self.close)
        window.bind("<Destroy>",lambda event:self.stop.set() if event.widget is window else None)
        frame=ttk.Frame(window,padding=18)
        frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="正常登录微信，然后连接",style="Heading.TLabel").pack(anchor="w")
        ttk.Label(frame,text="1. 和往常一样打开Windows微信并登录。\n2. 确认下方本机授权，点击“连接微信”。\n程序自动识别微信、账号和记录位置，优先复用已保存授权；不需要选择文件夹、程序或填写账号ID。\n首次接入必要时会短暂附加微信并验证密钥，可能受版本影响；不强关、不自动重启、不上传。",
                  wraplength=640).pack(fill="x",pady=10)
        self.consent=tk.BooleanVar(value=False)
        ttk.Checkbutton(frame,text="这是本人微信，同意本机连接；首次必要时临时附加、验证并加密保存授权",variable=self.consent).pack(anchor="w",pady=4)
        self.status=tk.StringVar(value="尚未连接。账号位置由程序识别，密钥不会显示或发送。")
        ttk.Label(frame,textvariable=self.status,wraplength=640).pack(fill="x",pady=12)
        self.progress=ttk.Progressbar(frame,mode="indeterminate")
        self.progress.pack(fill="x",pady=(0,12))
        row=ttk.Frame(frame)
        row.pack(fill="x")
        self.start_button=ttk.Button(row,text="连接微信",style="Primary.TButton",command=self.start)
        self.start_button.pack(side="left")
        ttk.Button(row,text="取消／关闭",command=self.close).pack(side="right")

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.busy:return
        if not self.consent.get():
            self.status.set("请先确认本次本人微信的本机连接授权。")
            return
        if self.app.settings is None or self.app.settings.provider_backend!="builtin":
            self.status.set("请先选择内置后端并保存本机配置。")
            return
        settings=self.app.settings
        self.stop.clear()
        self.closing=False
        self.connected=False
        self.start_button.configure(state="disabled")
        self.progress.start(12)
        self.status.set("正在检查；尚未读取密钥。")
        def work():
            try:
                result=connect_database(settings,consent=True,stop=self.stop.is_set,progress=lambda phase:self.events.put(("phase",phase)))
                self.events.put(("done",result))
            except KeyboardInterrupt:
                self.events.put(("error","本次操作已取消并结束；原授权保持。"))
            except AccessError as error:
                self.events.put(("error",str(error)))
            except Exception:
                self.events.put(("error","本次连接未完成。请确认Windows微信已正常登录后重试；原授权保持，无需选择文件夹。"))
        self.thread=threading.Thread(target=work,name="wechat-first-access",daemon=False)
        self.thread.start()
        self.window.after(100,self.poll)

    def poll(self):
        if not self.window.winfo_exists():return
        while not self.events.empty():
            kind,value=self.events.get_nowait()
            if kind=="phase":self.status.set(PHASES.get(value,"正在处理…"))
            elif kind=="error":self.status.set(value)
            elif kind=="done":
                self.connected=True
                self.status.set("已连接微信，账号及记录位置已自动识别。请关闭设置并刷新聊天列表。")
                self.app.selected.clear()
                self.app.bound_account=None
                self.app.offline_account=False
                self.app.invalidate()
        if self.busy:
            self.window.after(100,self.poll)
        else:
            self.progress.stop()
            self.consent.set(False)
            self.start_button.configure(state="normal")
            if self.closing:self.close()
            elif self.connected and self.parent is getattr(self.app,"root",None):
                self.close()
                self.app.refresh()

    def close(self):
        if self.busy:
            self.closing=True
            self.stop.set()
            self.status.set("正在取消并解除附加，请稍候…")
            return
        self.window.destroy()
        if self.parent.winfo_exists():self.parent.grab_set()
