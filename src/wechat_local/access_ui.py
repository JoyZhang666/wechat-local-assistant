"""Explicit first-use access UI. No attachment happens merely by opening it."""
import queue
import threading
import tkinter as tk
from tkinter import ttk,filedialog

from .key_access import AccessError,acquire_database


PHASES={"checking":"检查组件与官方程序签名…","waiting":"请从桌面正常打开微信并登录；最多等待90秒。",
        "attached":"已短暂附加，等待数据库打开；可随时取消。","detached":"已解除附加。",
        "validating":"正在认证所选账号数据库与本人身份…","saved":"认证通过，已加密保存；没有常驻取钥程序。"}


class AccessDialog:
    def __init__(self,app,parent,account_root,account_id):
        self.app,self.parent=app,parent
        self.stop=threading.Event()
        self.thread=None
        self.events=queue.Queue()
        self.closing=False
        self.window=window=tk.Toplevel(parent)
        window.title("协助接入本人微信 · 本次授权")
        window.transient(parent)
        window.grab_set()
        window.protocol("WM_DELETE_WINDOW",self.close)
        window.bind("<Destroy>",lambda event:self.stop.set() if event.widget is window else None)
        frame=ttk.Frame(window,padding=18)
        frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="本人账号首次接入",style="Heading.TLabel").pack(anchor="w")
        ttk.Label(frame,text="使用前请保存本机配置，并安装可选接入依赖requirements-access.txt。\n本次会通过Frida短暂附加和挂钩微信，可能失败或影响稳定性；不承诺所有微信版本兼容。\n请自行正常退出微信，再点击开始等待，并从桌面重开。程序不强制关闭微信，只验证并保存所选账号，不上传。",
                  wraplength=640).pack(fill="x",pady=10)
        self.account_root=account_root
        self.account_id=account_id
        self.executable=tk.StringVar()
        self.fields=[]
        for label,var,kind in (("本人账号目录（含db_storage）",account_root,"directory"),
                              ("本人账号ID（wxid目录可留空自动识别）",account_id,None),
                              ("官方Weixin.exe路径",self.executable,"file")):
            ttk.Label(frame,text=label).pack(anchor="w")
            row=ttk.Frame(frame)
            row.pack(fill="x",pady=(3,8))
            entry=ttk.Entry(row,textvariable=var,width=64)
            entry.pack(side="left",fill="x",expand=True)
            self.fields.append(entry)
            if kind:
                def choose(value=var,which=kind):
                    path=filedialog.askdirectory(parent=window) if which=="directory" else filedialog.askopenfilename(parent=window,filetypes=[("微信程序","Weixin.exe")])
                    if path:value.set(path)
                button=ttk.Button(row,text="选择",command=choose)
                button.pack(side="left",padx=5)
                self.fields.append(button)
        self.consent=tk.BooleanVar(value=False)
        self.exited=tk.BooleanVar(value=False)
        ttk.Checkbutton(frame,text="我确认这是本人账号，同意仅本次临时附加、验证并DPAPI保存数据库密钥",variable=self.consent).pack(anchor="w",pady=4)
        ttk.Checkbutton(frame,text="我已保存工作并正常退出微信；由我从桌面重新打开",variable=self.exited).pack(anchor="w",pady=4)
        self.status=tk.StringVar(value="尚未开始。数据库密钥与图片密钥独立；本流程只接入数据库。")
        ttk.Label(frame,textvariable=self.status,wraplength=640).pack(fill="x",pady=12)
        self.progress=ttk.Progressbar(frame,mode="indeterminate")
        self.progress.pack(fill="x",pady=(0,12))
        row=ttk.Frame(frame)
        row.pack(fill="x")
        self.start_button=ttk.Button(row,text="开始本次接入并等待重开",style="Primary.TButton",command=self.start)
        self.start_button.pack(side="left")
        ttk.Button(row,text="取消／关闭",command=self.close).pack(side="right")

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self):
        if self.busy:return
        if not self.consent.get() or not self.exited.get():
            self.status.set("请先确认本次授权，并由你正常退出微信。")
            return
        if self.app.settings is None or self.app.settings.provider_backend!="builtin":
            self.status.set("请先选择内置后端并保存本机配置。")
            return
        args=(self.app.settings,self.account_root.get().strip(),self.account_id.get().strip(),self.executable.get().strip())
        self.stop.clear()
        self.closing=False
        self.start_button.configure(state="disabled")
        for widget in self.fields:widget.configure(state="disabled")
        self.progress.start(12)
        self.status.set("正在检查；尚未读取密钥。")
        def work():
            try:
                result=acquire_database(*args,consent=True,stop=self.stop.is_set,progress=lambda phase:self.events.put(("phase",phase)))
                self.events.put(("done",result))
            except KeyboardInterrupt:
                self.events.put(("error","本次操作已取消并结束；原授权保持。"))
            except AccessError as error:
                self.events.put(("error",str(error)))
            except Exception:
                self.events.put(("error","本次接入未完成，请核对目录、本人ID、程序和权限；未覆盖原授权。"))
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
                self.status.set("数据库已验证并加密保存（微信 "+value["wechat_version"]+"）。请关闭设置并刷新聊天；图片须单独接入。")
                self.app.selected.clear()
                self.app.invalidate()
        if self.busy:
            self.window.after(100,self.poll)
        else:
            self.progress.stop()
            self.consent.set(False)
            self.exited.set(False)
            self.start_button.configure(state="normal")
            for widget in self.fields:widget.configure(state="normal")
            if self.closing:self.close()

    def close(self):
        if self.busy:
            self.closing=True
            self.stop.set()
            self.status.set("正在取消并解除附加，请稍候…")
            return
        self.window.destroy()
        if self.parent.winfo_exists():self.parent.grab_set()
