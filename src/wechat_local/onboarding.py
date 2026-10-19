"""Non-modal first-use guidance. Navigation never reads chats or calls a model."""
import tkinter as tk
from tkinter import ttk

from .common import atomic_json, read_json


STEPS = (
    ("配置模型与连接方式", "先明确目标：只导出原记录无需模型，可直接连接微信。要生成摘要或识别媒体时，打开“一、连接状态 → 模型、媒体与连接方式”，按需填写并保存。\n\n本地：安装Ollama及模型，填写程序、模型目录与名称。\n云端API：填写HTTPS服务地址、模型名与API Key，单独计费。\nChatGPT／Codex会员：在官方页面授权，选择返回的模型。\n\n图片与语音识别仍在本机，需要另外部署媒体组件。保存配置不代表模型连接已验证；云端可主动点击“检查云端连接”，仅发送内置虚构材料，不读取微信。", "打开模型配置"),
    ("连接本人的微信", "和往常一样打开Windows微信并登录，保持运行。回到“二、选择聊天”，点击“连接／刷新”。\n\n程序自动识别微信程序、账号和记录位置；首次确认本人本机授权即可，不需要找目录、选择程序或填写账号ID，也不要求先退出重开。连接成功自动加载聊天，之后复用已保存授权。\n\n首次必要的监听可能受客户端版本和登录时机影响；失败会明确提示并停止，不强关、不重启。手动入口仅在高级排错中，本引导不触发取钥。\n\n图片密钥另行处理；已有外置部署仍可按原方式直接刷新。", "打开微信连接说明"),
    ("刷新列表并选择聊天", "回到“二、选择聊天”。主动点击“刷新”后，搜索并勾选需要的单聊或群聊。\n\n首次练习建议只选一个聊天、较短时间。摘要可以选多个聊天；导出完整聊天记录必须只选一个。程序不会自动勾选全部。\n\n若刷新失败，查看底部状态栏，回到上一步检查账号接入。定位按钮只把焦点移到“刷新”，不会替你读取聊天。", "前往聊天选择"),
    ("选择时间范围", "在“三、时间范围”填写开始、结束时间，或选择今天、昨天、近24小时、过去48小时、过去72小时、近7天。\n\n时间使用北京时间，包含开始时刻，不包含结束时刻。先用较短范围确认数量；修改聊天、时间或模型设置后，需要重新预览。", "前往时间范围"),
    ("预览并开始处理", "在“四、开始处理”选择目标。\n\nA. 生成摘要：先点“1 预览数量”，核对范围，再点“2 生成摘要”。预览不调用模型；勾选图片识别、语音转写后，识别文本才纳入摘要。\n\nB. 导出完整聊天记录：只选一个聊天；不勾选识别时无需模型。选择“1 导出记录”或“2 导出并追加摘要”。\n\n观察底部进度条、阶段和失败原因；必要时取消。云端摘要仍需每次确认发送范围，翻看本引导不代表同意上传。", "前往开始处理"),
    ("阅读、保存与再次使用", "在“五、查看结果”打开完整摘要或聊天阅读页。结果自动保存在本机，可从“历史摘要”重新载入。\n\n分享或迁移时，选择保存完整摘要包／完整记录包，保留整个文件夹及media、manifest.json。单独Markdown不能携带附件。\n\n查看清单中的缺失、缩略图和识别失败；导出完成不保证所有内容完整。人物、日期和结论应对照原文复核。\n\n“完成引导”只表示已读完说明，不代表微信接入、模型连接或任务已经通过验证。以后可随时点击顶部“新手引导”重看。", "前往查看结果"),
)


def load_state(path):
    try:
        if path.stat().st_size > 4096:
            return {}
        state = read_json(path)
        if (not isinstance(state, dict) or state.get("version") != 1
                or type(state.get("step")) is not int or not 0 <= state["step"] < len(STEPS)
                or type(state.get("seen")) is not bool or type(state.get("completed")) is not bool):
            return {}
        return state
    except (OSError, ValueError):
        return {}


class Onboarding:
    def __init__(self, app, path):
        self.app, self.path = app, path
        state = load_state(path)
        self.step = 0 if state.get("completed") else state.get("step", 0)
        self.completed = False
        self.window = window = tk.Toplevel(app.root)
        window.title("微信本地助手 · 新手引导")
        window.transient(app.root)
        px = app.pixels
        available_w=max(400,app.root.winfo_screenwidth()-60)
        available_h=max(300,app.root.winfo_screenheight()-90)
        window.geometry(f"{min(px(660),available_w)}x{min(px(530),available_h)}")
        window.minsize(min(px(520),available_w), min(px(420),available_h))
        window.protocol("WM_DELETE_WINDOW", self.pause)
        content = ttk.Frame(window, padding=px(16), style="Card.TFrame")
        content.pack(fill="both", expand=True)
        self.heading = ttk.Label(content, style="Heading.TLabel")
        self.heading.pack(fill="x")
        self.heading.bind("<Configure>", lambda e: self.heading.configure(wraplength=max(160, e.width)))
        self.progress = ttk.Progressbar(content, maximum=len(STEPS), mode="determinate")
        self.progress.pack(fill="x", pady=px((10, 14)))
        self.notice = tk.StringVar()
        notice = ttk.Label(content, textvariable=self.notice, style="Muted.TLabel")
        notice.pack(side="bottom", fill="x", pady=px((10, 0)))
        notice.bind("<Configure>", lambda e: notice.configure(wraplength=max(160, e.width)))
        nav = ttk.Frame(content, style="Card.TFrame")
        nav.pack(side="bottom", fill="x")
        self.previous = ttk.Button(nav, text="上一步", command=lambda: self.move(-1))
        self.previous.pack(side="left")
        ttk.Button(nav, text="稍后再看", command=self.pause).pack(side="left", padx=px(8))
        self.next = ttk.Button(nav, style="Primary.TButton", command=lambda: self.move(1))
        self.next.pack(side="right")
        self.action = ttk.Button(content, command=self.locate)
        self.action.pack(side="bottom", fill="x", pady=px((12, 14)))
        reading = ttk.Frame(content, style="Card.TFrame")
        reading.pack(fill="both", expand=True)
        self.body = tk.Text(reading, wrap="char", font=("Microsoft YaHei UI", 10),
                            relief="flat", background="#ffffff", foreground="#182230", spacing3=6)
        scroll = ttk.Scrollbar(reading, command=self.body.yview)
        self.body.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.body.pack(fill="both", expand=True)
        self.render()
        self.save()

    def save(self):
        try:
            atomic_json(self.path, {"version": 1, "seen": True, "step": self.step, "completed": self.completed})
            return True
        except OSError:
            self.notice.set("无法保存引导进度；请检查配置目录是否可写。不会影响已有设置。")
            return False

    def render(self):
        title, body, action = STEPS[self.step]
        self.heading.configure(text=f"第 {self.step + 1} / {len(STEPS)} 步 · {title}")
        self.progress.configure(value=self.step + 1)
        self.body.configure(state="normal")
        self.body.delete("1.0", "end")
        self.body.insert("1.0", body)
        self.body.insert("end","\n\n当前状态："+self.app.account_label.get()+"；"+self.app.model_label.get()+"。保存设置不等于连接验证通过。")
        self.body.configure(state="disabled")
        self.body.yview_moveto(0)
        self.previous.configure(state="disabled" if self.step == 0 else "normal")
        self.next.configure(text="完成引导" if self.step == len(STEPS) - 1 else "下一步")
        self.action.configure(text=action)
        self.notice.set("引导可随时暂停；定位后在主界面完成操作，再回来点下一步。")

    def move(self, delta):
        if delta > 0 and self.step == len(STEPS) - 1:
            self.completed = True
            if self.save():
                self.window.destroy()
            return
        self.step = max(0, min(len(STEPS) - 1, self.step + delta))
        self.render()
        self.save()

    def pause(self):
        self.save()
        self.window.destroy()

    def locate(self):
        if self.app.active:
            self.notice.set("任务正在运行。可继续阅读引导，请等任务结束后再调整配置或范围。")
            return
        if self.step < 2:
            self.app.configure(initial_tab="model" if self.step == 0 else "reader")
        else:
            target = (self.app.refresh_button, self.app.range_controls[0],
                      self.app.task_tabs, self.app.result_combo)[self.step - 2]
            self.app.root.lift()
            target.focus_set()
            self.notice.set("已定位主界面；请按本步说明操作，完成后回到引导点下一步。")
