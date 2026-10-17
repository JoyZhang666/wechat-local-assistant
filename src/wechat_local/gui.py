"""Native desktop UI. Private messages never pass through an Agent conversation."""
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk,messagebox,filedialog
import uuid
from .common import NamedMutex,atomic_json,read_json,inside
from .settings import Settings
from .process_job import ProcessJob

ZONE=timezone(timedelta(hours=8))


def parse_range(start,end):
    a=datetime.strptime(start.strip(),"%Y-%m-%d %H:%M").replace(tzinfo=ZONE)
    b=datetime.strptime(end.strip(),"%Y-%m-%d %H:%M").replace(tzinfo=ZONE)
    if a>=b:
        raise ValueError("开始时间必须早于结束时间。")
    return int(a.timestamp()),int(b.timestamp())


class App:
    def __init__(self,root,config_path):
        self.root=root
        self.config_path=Path(config_path)
        self.settings=None
        self.chats=[]
        self.selected=set()
        self.prepared=None
        self.job_id=None
        self.result_path=None
        self.report_scope="reports"
        self.export_dir=None
        self.saved_export_dir=None
        self.saved_summary_dir=None
        self.active=None
        self.active_command=None
        self.events=queue.Queue()
        self.closing=False
        self.pending_cancel=False
        self.failure_seen=False
        self.started_at=None
        self.last_progress_at=None
        self.ui_scale=max(1,float(self.root.tk.call("tk","scaling"))*0.75)
        self.root.title("微信本地助手 · 选择聊天，按需运行")
        self.root.geometry(f"{self.pixels(1160)}x{self.pixels(820)}")
        self.root.minsize(self.pixels(920),self.pixels(660))
        self.root.protocol("WM_DELETE_WINDOW",self.close)
        self.build()
        try:
            self.settings=Settings.load(self.config_path)
        except Exception:
            self.status.set("请先填写本机配置；不会自动连接微信或提取密钥。")
        else:
            self.update_model_label()
            self.root.after(200,self.restore_latest)
        self.root.after(150,self.poll)

    def pixels(self,value):
        return tuple(self.pixels(x) for x in value) if isinstance(value,tuple) else round(value*self.ui_scale)

    def build(self):
        px=self.pixels
        style=ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        bg,surface,ink,muted,line,accent="#f3f6fa","#ffffff","#182230","#53657b","#dce4ee","#2563eb"
        self.root.configure(background=bg)
        style.configure(".",font=("Microsoft YaHei UI",10),background=bg,foreground=ink)
        style.configure("TFrame",background=bg)
        style.configure("Card.TFrame",background=surface)
        style.configure("Panel.TFrame",background=surface,bordercolor="#bdcadc",lightcolor="#bdcadc",darkcolor="#bdcadc",borderwidth=1,relief="solid")
        style.configure("Card.TLabel",background=surface)
        style.configure("Heading.TLabel",background=surface,font=("Microsoft YaHei UI",11,"bold"))
        style.configure("Muted.TLabel",background=surface,foreground=muted,font=("Microsoft YaHei UI",9))
        style.configure("TButton",padding=px((10,5)),width=0,background="#eaf1ff",foreground="#174395",
                        bordercolor="#6f91c8",lightcolor="#6f91c8",darkcolor="#6f91c8",borderwidth=1,relief="solid",focuscolor=accent)
        style.map("TButton",background=[("disabled","#edf0f4"),("pressed","#cbdcf9"),("active","#dbe8ff")],
                  foreground=[("disabled","#909ba9")])
        for option in ("bordercolor","lightcolor","darkcolor"):
            style.map("TButton",**{option:[("disabled","#d6dce5"),("focus",accent)]})
        style.configure("Primary.TButton",background=accent,foreground=surface,bordercolor=accent,lightcolor=accent,darkcolor=accent)
        style.map("Primary.TButton",background=[("disabled","#edf0f4"),("pressed","#1e40af"),("active","#1d4ed8")],
                  foreground=[("disabled","#909ba9"),("!disabled",surface)])
        style.configure("Quiet.TButton",padding=px((8,4)),font=("Microsoft YaHei UI",9),background="#eaf0f7",foreground="#284562",
                        bordercolor="#97aac2",lightcolor="#97aac2",darkcolor="#97aac2",borderwidth=1,relief="solid")
        style.map("Quiet.TButton",background=[("disabled","#edf0f4"),("pressed","#cedbea"),("active","#dce7f4")])
        style.configure("TEntry",padding=px((6,4)),fieldbackground=surface,bordercolor=line,lightcolor=line,darkcolor=line)
        # Combobox's widget font overrides ttk style fonts; match nearby buttons.
        self.root.option_add("*TCombobox.font",("Microsoft YaHei UI",9))
        style.configure("TCombobox",padding=px((7,4)),arrowsize=px(18),fieldbackground=surface,
                        bordercolor="#97aac2",lightcolor="#97aac2",darkcolor="#97aac2",background="#eaf0f7",arrowcolor="#284562")
        style.map("TCombobox",fieldbackground=[("readonly",surface)],arrowcolor=[("disabled","#909ba9"),("active",accent)],
                  background=[("disabled","#edf0f4"),("active","#dce7f4")])
        style.configure("TRadiobutton",indicatorsize=px(20),indicatormargin=px((0,0,10,0)),padding=px((6,11)),
                        indicatorbackground=surface,indicatorforeground=accent,upperbordercolor="#6f91c8",lowerbordercolor="#6f91c8")
        style.map("TRadiobutton",background=[("active","#eaf1ff")],
                  indicatorbackground=[("disabled","#edf0f4"),("selected","#dbe8ff")],
                  indicatorforeground=[("disabled","#909ba9"),("selected",accent)])
        style.configure("Treeview",rowheight=px(30),background=surface,fieldbackground=surface,borderwidth=0)
        style.map("Treeview",background=[("selected","#e7efff")],foreground=[("selected",ink)])
        style.configure("Treeview.Heading",background="#f0f4f9",foreground=muted,padding=px((4,6)),relief="flat")
        style.configure("TNotebook",background=surface,borderwidth=0,tabmargins=px((0,0,0,8)),bordercolor=surface,lightcolor=surface,darkcolor=surface)
        style.configure("TNotebook.Tab",padding=px((16,7)),font=("Microsoft YaHei UI",10,"bold"),background="#eaf0f7",
                        foreground="#284562",borderwidth=1,bordercolor="#97aac2",lightcolor="#97aac2",darkcolor="#97aac2")
        style.map("TNotebook.Tab",padding=[("selected",px((16,7))),("!selected",px((16,7)))],
                  background=[("disabled","#edf0f4"),("selected",accent),("active","#dce7f4"),("!selected","#eaf0f7")],
                  foreground=[("disabled","#909ba9"),("selected",surface),("!selected","#284562")],
                  focuscolor=[("selected",surface),("!selected",accent)])
        for option in ("bordercolor","lightcolor","darkcolor"):
            style.map("TNotebook.Tab",**{option:[("disabled","#d6dce5"),("selected",accent),("!selected","#97aac2")]})
        style.configure("Horizontal.TProgressbar",background=accent,troughcolor="#dce6f2",borderwidth=1,thickness=px(14),bordercolor="#bdcadc",lightcolor=surface,darkcolor=surface)

        header=ttk.Frame(self.root,padding=px((16,12,16,8)))
        header.pack(fill="x")
        ttk.Label(header,text="微信本地助手",font=("Microsoft YaHei UI",18,"bold")).pack(side="left")
        self.guide_button=ttk.Button(header,text="使用说明",style="Quiet.TButton",command=self.toggle_guide)
        self.guide_button.pack(side="right")
        self.onboarding_button=ttk.Button(header,text="新手引导",command=self.show_onboarding)
        self.onboarding_button.pack(side="right",padx=px((0,8)))
        guide=ttk.Frame(self.root,padding=px((16,0,16,8)))
        guide.pack(fill="x")
        self.guide_label=ttk.Label(guide,text="左侧配置模型、勾选聊天 → 右侧选择时间、预览数量 → 生成摘要或导出 → 阅读与保存。",foreground=muted)
        self.guide_label.pack(fill="x")
        guide.bind("<Configure>",lambda e:self.guide_label.configure(wraplength=max(px(240),e.width-px(32))))

        self.workspace=ttk.Frame(self.root)
        self.workspace.pack(fill="both",expand=True,padx=px(16),pady=px((0,12)))
        self.workspace.rowconfigure(0,weight=1)
        self.workspace.columnconfigure(0,weight=3,uniform="workspace")
        self.workspace.columnconfigure(2,weight=7,uniform="workspace")
        left=ttk.Frame(self.workspace,padding=px((8,0)))
        right=ttk.Frame(self.workspace,padding=px((8,0)))
        left.grid(row=0,column=0,sticky="nsew")
        self.column_divider=tk.Frame(self.workspace,background="#bdcadc",width=px(2))
        self.column_divider.grid(row=0,column=1,sticky="ns")
        right.grid(row=0,column=2,sticky="nsew")
        self.chat_panel=left
        self.work_panel=right

        self.model_label=tk.StringVar(value="文字摘要：本地模型（默认）")
        connection=ttk.Frame(left,style="Panel.TFrame",padding=px(8))
        connection.pack(fill="x",pady=px((0,10)))
        self.model_heading=ttk.Label(connection,text="一、模型",style="Heading.TLabel")
        self.model_heading.pack(anchor="w",pady=px((0,6)))
        self.model_status_label=ttk.Label(connection,textvariable=self.model_label,style="Muted.TLabel")
        self.model_status_label.pack(fill="x",pady=px((0,8)))
        self.model_status_label.bind("<Configure>",lambda e:self.model_status_label.configure(wraplength=max(120,e.width)))
        self.config_button=ttk.Button(connection,text="选择模型与连接方式",command=self.configure)
        self.config_button.pack(fill="x")
        self.runtime_button=ttk.Button(connection,text="初始化本项目运行程序",style="Quiet.TButton",command=self.initialize_runtime)
        self.runtime_button.pack(fill="x",pady=px((6,0)))
        model_actions=ttk.Frame(connection,style="Card.TFrame")
        model_actions.pack(fill="x",pady=px((6,0)))
        model_actions.columnconfigure(0,weight=1)
        self.member_login_button=ttk.Button(model_actions,text="登录ChatGPT会员",style="Quiet.TButton",command=self.member_login)
        self.member_login_button.grid(row=0,column=0,sticky="ew")
        self.cloud_test_button=ttk.Button(model_actions,text="检查云端连接",style="Quiet.TButton",command=self.cloud_test,state="disabled")
        self.cloud_test_button.grid(row=1,column=0,sticky="ew",pady=px((6,0)))

        self.task_status=task_status=ttk.Frame(self.root,style="Panel.TFrame",padding=px((8,6)))
        task_status.pack(side="bottom",fill="x",padx=px(16),pady=px((0,12)),before=self.workspace)
        status_row=ttk.Frame(task_status,style="Card.TFrame")
        status_row.pack(fill="x")
        ttk.Label(status_row,text="任务进度",style="Heading.TLabel").pack(side="left")
        self.cancel_button=ttk.Button(status_row,text="取消任务",style="Quiet.TButton",command=self.cancel,state="disabled")
        self.cancel_button.pack(side="right",padx=px((12,0)))
        progress_track=ttk.Frame(status_row,height=px(14),style="Card.TFrame")
        progress_track.pack(side="left",fill="x",expand=True,padx=px((12,0)))
        progress_track.pack_propagate(False)
        self.progress=ttk.Progressbar(progress_track,mode="determinate")
        self.progress.pack(fill="both",expand=True)
        self.status=tk.StringVar(value="请先配置模型，再选择聊天和时间。")
        self.status_label=ttk.Label(task_status,textvariable=self.status,style="Card.TLabel")
        self.status_label.pack(fill="x",pady=px((4,0)))
        self.status_label.bind("<Configure>",lambda e:self.status_label.configure(wraplength=max(200,e.width)))
        self.activity=tk.StringVar(value="当前没有运行任务。")
        self.activity_label=ttk.Label(task_status,textvariable=self.activity,style="Muted.TLabel")
        self.activity_label.pack(fill="x")
        self.activity_label.bind("<Configure>",lambda e:self.activity_label.configure(wraplength=max(200,e.width)))

        self.chat_section=chats=ttk.Frame(left,style="Panel.TFrame",padding=px(8))
        chats.pack(fill="both",expand=True)
        ttk.Label(chats,text="二、选择聊天",style="Heading.TLabel").pack(anchor="w",pady=px((0,8)))
        row=ttk.Frame(chats,style="Card.TFrame")
        row.pack(fill="x")
        self.search=tk.StringVar()
        ttk.Label(row,text="搜索",style="Muted.TLabel").pack(side="left",padx=px((0,6)))
        ttk.Entry(row,textvariable=self.search,width=16).pack(side="left",fill="x",expand=True)
        self.search.trace_add("write",lambda *_:self.render_chats())
        self.refresh_button=ttk.Button(row,text="连接／刷新",style="Quiet.TButton",command=self.refresh)
        self.refresh_button.pack(side="right",padx=px((6,0)))
        filters=ttk.Frame(chats,style="Card.TFrame")
        filters.pack(fill="x",pady=px(8))
        self.kind=tk.StringVar(value="全部聊天")
        kind=ttk.Combobox(filters,textvariable=self.kind,values=("全部聊天","群聊","联系人"),state="readonly",width=9)
        kind.pack(side="left")
        kind.bind("<<ComboboxSelected>>",lambda *_:self.render_chats())
        ttk.Button(filters,text="全选列表",style="Quiet.TButton",command=self.select_visible).pack(side="left",padx=px(4))
        ttk.Button(filters,text="清空",style="Quiet.TButton",command=self.clear_selection).pack(side="left")
        treebox=ttk.Frame(chats,style="Card.TFrame")
        treebox.pack(fill="both",expand=True)
        self.tree=ttk.Treeview(treebox,columns=("check","name","kind","last"),show="headings",selectmode="browse")
        for key,title,width in (("check","选",32),("name","聊天名称",136),("kind","类型",55),("last","最近记录",100)):
            self.tree.heading(key,text=title)
            self.tree.column(key,width=px(width),minwidth=px(width if key!="name" else 80),stretch=key=="name")
        scrollbar=ttk.Scrollbar(treebox,orient="vertical",command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left",fill="both",expand=True)
        scrollbar.pack(side="right",fill="y")
        chats.bind("<Configure>",lambda e:self.tree.configure(displaycolumns=("check","name","kind","last") if e.width>=px(390) else ("check","name","kind")))
        self.tree.bind("<ButtonRelease-1>",self.toggle)
        self.tree.bind("<space>",self.toggle)
        self.selection_label=tk.StringVar(value="尚未选择聊天")
        selection=ttk.Label(chats,textvariable=self.selection_label,style="Muted.TLabel")
        selection.pack(fill="x",pady=px((8,0)))
        selection.bind("<Configure>",lambda e:selection.configure(wraplength=max(120,e.width)))

        self.time_section=ranges=ttk.Frame(right,style="Panel.TFrame",padding=px(8))
        ranges.pack(fill="x",pady=px((0,10)))
        self.range_heading=ttk.Label(ranges,text="三、时间范围",style="Heading.TLabel")
        self.range_heading.grid(row=0,column=0,sticky="w",pady=px((0,6)))
        ttk.Label(ranges,text="北京时间 · 结束时刻不包含",style="Muted.TLabel").grid(row=0,column=1,sticky="e",pady=px((0,6)))
        self.range_controls=[]
        self.start=tk.StringVar()
        self.end=tk.StringVar()
        self.preset("24")
        for column,(label,var) in enumerate((("开始",self.start),("结束",self.end))):
            ranges.columnconfigure(column,weight=1,uniform="dates")
            field=ttk.Frame(ranges,style="Card.TFrame")
            field.grid(row=1,column=column,sticky="ew",padx=px((0,8)) if column==0 else 0,pady=px((3,6)))
            ttk.Label(field,text=label,style="Muted.TLabel").pack(side="left",padx=px((0,6)))
            entry=ttk.Entry(field,textvariable=var,width=19)
            entry.pack(side="left",fill="x",expand=True)
            self.range_controls.append(entry)
        presets=ttk.Frame(ranges,style="Card.TFrame")
        presets.grid(row=2,column=0,columnspan=2,sticky="w")
        for value,label in (("today","今天"),("yesterday","昨天"),("24","近24小时"),
                            ("48","过去48小时"),("72","过去72小时"),("7","近7天")):
            button=ttk.Button(presets,text=label,style="Quiet.TButton",command=lambda v=value:self.preset(v))
            button.pack(side="left",padx=px((0,6)))
            self.range_controls.append(button)
        self.start.trace_add("write",lambda *_:self.invalidate())
        self.end.trace_add("write",lambda *_:self.invalidate())

        self.processing_section=processing=ttk.Frame(right,style="Panel.TFrame",padding=px(8))
        processing.pack(fill="x",pady=px((0,10)))
        self.processing_heading=ttk.Label(processing,text="四、开始处理",style="Heading.TLabel")
        self.processing_heading.pack(anchor="w",pady=px((0,8)))
        self.task_tabs=ttk.Notebook(processing)
        self.task_tabs.pack(fill="x")
        self.summary_task=ttk.Frame(self.task_tabs,style="Card.TFrame")
        self.export_task=ttk.Frame(self.task_tabs,style="Card.TFrame")
        self.task_tabs.add(self.summary_task,text="A. 生成摘要")
        self.task_tabs.add(self.export_task,text="B. 导出完整聊天记录")
        self.preview_label=tk.StringVar(value="先预览数量，再识别媒体并生成摘要；识别后的文字会纳入摘要。")
        preview=ttk.Label(self.summary_task,textvariable=self.preview_label,style="Muted.TLabel")
        preview.pack(fill="x",pady=px((0,6)))
        preview.bind("<Configure>",lambda e:preview.configure(wraplength=max(200,e.width)))
        actions=ttk.Frame(self.summary_task,style="Card.TFrame")
        actions.pack(fill="x")
        self.preview_button=ttk.Button(actions,text="1  预览数量",command=self.prepare)
        self.preview_button.pack(side="left",padx=px((0,8)))
        self.generate_button=ttk.Button(actions,text="2  识别媒体并生成摘要",style="Primary.TButton",command=self.generate,state="disabled")
        self.generate_button.pack(side="left")
        export_hint=ttk.Label(self.export_task,text="只选一个聊天。图片／语音本机识别；视频等保留原件并标注未识别。可只识别，也可追加摘要。",style="Muted.TLabel")
        export_hint.pack(fill="x",pady=px((0,6)))
        export_hint.bind("<Configure>",lambda e:export_hint.configure(wraplength=max(200,e.width)))
        export_actions=ttk.Frame(self.export_task,style="Card.TFrame")
        export_actions.pack(fill="x")
        self.record_button=ttk.Button(export_actions,text="1  只识别并导出记录",command=self.export_records,state="disabled")
        self.record_button.pack(side="left",padx=px((0,8)))
        self.export_summary_button=ttk.Button(export_actions,text="2  识别后生成摘要",style="Primary.TButton",command=lambda:self.export_records(summarize=True),state="disabled")
        self.export_summary_button.pack(side="left")

        self.results_section=results=ttk.Frame(right,style="Panel.TFrame",padding=px(8))
        results.pack(fill="both",expand=True)
        result_head=ttk.Frame(results,style="Card.TFrame")
        result_head.pack(fill="x")
        self.result_title=tk.StringVar(value="五、查看结果")
        ttk.Label(result_head,textvariable=self.result_title,style="Heading.TLabel").pack(side="left")
        self.history_button=ttk.Button(result_head,text="历史摘要",style="Quiet.TButton",command=self.show_history)
        self.history_button.pack(side="right")
        self.result_chat=tk.StringVar()
        self.result_combo=ttk.Combobox(result_head,textvariable=self.result_chat,state="readonly",width=16)
        self.result_combo.pack(side="left",fill="x",expand=True,padx=px(10))
        self.result_combo.bind("<<ComboboxSelected>>",lambda *_:self.show_report())
        self.result_actions=ttk.Frame(results,style="Card.TFrame")
        self.result_actions.pack(fill="x",pady=px((8,0)))
        self.open_summary_button=ttk.Button(self.result_actions,text="打开完整摘要",command=self.open_summary,state="disabled")
        self.save_summary_button=ttk.Button(self.result_actions,text="另存完整摘要包",style="Quiet.TButton",command=self.save_summary,state="disabled")
        self.summary_directory_button=ttk.Button(self.result_actions,text="打开摘要目录",style="Quiet.TButton",command=self.open_summary_directory,state="disabled")
        self.export_button=ttk.Button(self.result_actions,text="另存为Markdown",style="Quiet.TButton",command=self.export,state="disabled")
        self.open_reading_button=ttk.Button(self.result_actions,text="打开阅读页",command=self.open_reading,state="disabled")
        self.open_export_button=ttk.Button(self.result_actions,text="打开导出目录",style="Quiet.TButton",command=self.open_export,state="disabled")
        self.result_more_button=ttk.Button(self.result_actions,text="更多",style="Quiet.TButton",command=self.toggle_result_more)
        self.advanced_results=False
        self._result_layout=None
        self.result_actions.bind("<Configure>",self.layout_result_actions)
        self.layout_result_actions()
        self.saved_location=tk.StringVar(value="生成后自动保存；也可从历史摘要读取。")
        path_box=ttk.Frame(results,style="Card.TFrame")
        path_box.pack(fill="x")
        self.saved_location_entry=ttk.Entry(path_box,textvariable=self.saved_location,state="readonly",font=("Microsoft YaHei UI",9))
        textbox=ttk.Frame(results,style="Card.TFrame")
        textbox.pack(fill="both",expand=True)
        self.text=tk.Text(textbox,wrap="word",font=("Microsoft YaHei UI",10),state="normal",height=5,
                          background=surface,foreground=ink,relief="flat",padx=px(8),pady=px(8),spacing1=px(3),spacing3=px(5))
        scroll=ttk.Scrollbar(textbox,command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.tag_configure("evidence",foreground="#667481",font=("Microsoft YaHei UI",9),lmargin1=px(18),lmargin2=px(18))
        self.text.tag_configure("title",font=("Microsoft YaHei UI",14,"bold"),spacing3=px(10))
        self.text.tag_configure("heading",font=("Microsoft YaHei UI",12,"bold"),spacing1=px(8),spacing3=px(8))
        self.text.tag_configure("subheading",font=("Microsoft YaHei UI",10,"bold"),spacing1=px(6))
        self.text.tag_configure("blank",font=("Microsoft YaHei UI",3),spacing1=px(0),spacing3=px(0))
        self.text.tag_configure("empty",foreground=muted)
        self.text.insert("1.0","你的摘要会显示在这里。选择聊天和时间，预览后生成；已有摘要请点击“历史摘要”。","empty")
        self.text.configure(state="disabled")
        self.text.pack(side="left",fill="both",expand=True)
        scroll.pack(side="right",fill="y")
        self.reports=[]

    def layout_result_actions(self,event=None):
        summary=(self.open_summary_button,self.save_summary_button,self.result_more_button)
        exported=(self.open_reading_button,self.export_button,self.result_more_button)
        secondary=(self.summary_directory_button,self.export_button) if self.report_scope=="reports" else (self.open_export_button,)
        buttons=(summary if self.report_scope=="reports" else exported)+(secondary if self.advanced_results else ())
        width=self.result_actions.winfo_width()
        cell=max(button.winfo_reqwidth() for button in buttons)+self.pixels(8)
        columns=max(1,min(len(buttons),width//max(1,cell)))
        key=(self.report_scope,columns,self.advanced_results)
        if key==self._result_layout:
            return
        self._result_layout=key
        for button in (self.open_summary_button,self.save_summary_button,self.summary_directory_button,self.export_button,self.open_reading_button,self.open_export_button,self.result_more_button):
            button.grid_remove()
        for index,button in enumerate(buttons):
            button.grid(row=index//columns,column=index%columns,sticky="w",padx=self.pixels((0,8)),pady=self.pixels((0,4)))

    def toggle_result_more(self):
        self.advanced_results=not self.advanced_results
        self.result_more_button.configure(text="收起" if self.advanced_results else "更多")
        if self.advanced_results:
            self.saved_location_entry.pack(fill="x",pady=(4,6))
        else:
            self.saved_location_entry.pack_forget()
        self.layout_result_actions()

    def initialize_runtime(self):
        if self.active:
            return
        try:
            from .local_runtime import initialize
            initialize()
            self.status.set("项目运行程序已初始化。未修改防火墙；默认调用本机不等于已强制阻断外网。")
        except (OSError,ValueError) as error:
            messagebox.showerror("初始化未完成",str(error),parent=self.root)

    def show_onboarding(self,automatic=False):
        from .onboarding import Onboarding,load_state
        path=self.config_path.with_suffix(".onboarding.json")
        if automatic and load_state(path).get("seen"):
            return
        existing=getattr(self,"onboarding",None)
        if existing and existing.window.winfo_exists():
            existing.window.lift()
            return
        self.onboarding=Onboarding(self,path)

    def toggle_guide(self):
        if getattr(self,"guide_window",None) and self.guide_window.winfo_exists():
            self.guide_window.lift()
            return
        self.guide_window=window=tk.Toplevel(self.root)
        window.title("微信本地助手 · 使用说明")
        width,height=self.pixels(700),self.pixels(440)
        x=max(0,self.root.winfo_rootx()+(self.root.winfo_width()-width)//2)
        y=max(0,self.root.winfo_rooty()+(self.root.winfo_height()-height)//2)
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.minsize(self.pixels(540),self.pixels(360))
        window.transient(self.root)
        content=ttk.Frame(window,padding=self.pixels(20),style="Card.TFrame")
        content.pack(fill="both",expand=True)
        ttk.Label(content,text="从选择到阅读，按顺序完成",style="Heading.TLabel").pack(anchor="w",pady=self.pixels((0,12)))
        self.guide_paragraphs=(
            "一、模型（左侧）：选择模型与连接方式用于选择本地模型、云端API或会员模型并配置连接；登录ChatGPT会员用于授权本助手使用会员额度；检查云端连接仅发送内置虚构材料，验证连接和摘要能力，本地模式下不启用。",
            "二、聊天（左侧）：点击刷新读取列表，搜索后勾选需要处理的群聊或联系人。程序不会默认选择全部聊天。",
            "三、时间范围（右侧）：填写北京时间的开始／结束时间，结束时刻不包含在范围内；也可点击快捷日期。",
            "四、开始处理（右侧）：生成摘要页先点击1预览数量，再点击2识别媒体并生成摘要。完整导出只能选择一个聊天：1只识别并导出记录，2识别后生成摘要（同时保留记录包）。",
            "五、查看结果（右侧）：结果自动保存在本机。打开完整摘要，可进入附带的原记录与媒体阅读页；另存完整摘要包保留附件，单独Markdown不能携带媒体。灰色按钮表示条件尚未满足或任务正在运行。",
            "预览不调用模型；图片和语音识别在本机，识别文本会随文字纳入所选摘要模型。云端每次需明确确认这些文本的发送。视频、文件等尚未理解的内容和缺失项会列出；机器注释与原文分开，结论仍需核对。失败后用相同聊天、范围、模型和设置重新预览，匹配的摘要检查点可复用。",
        )
        ttk.Button(content,text="知道了",command=window.destroy).pack(anchor="e",side="bottom")
        reading=ttk.Frame(content,style="Card.TFrame")
        reading.pack(fill="both",expand=True,pady=self.pixels((0,12)))
        self.guide_text=tk.Text(reading,wrap="char",font=("Microsoft YaHei UI",10),relief="flat",background="#ffffff",foreground="#182230",padx=0,pady=0,spacing3=8)
        scroll=ttk.Scrollbar(reading,command=self.guide_text.yview)
        scroll.pack(side="right",fill="y")
        self.guide_text.configure(yscrollcommand=scroll.set)
        self.guide_text.insert("1.0","\n\n".join(self.guide_paragraphs))
        self.guide_text.configure(state="disabled")
        self.guide_text.pack(fill="both",expand=True)


    def restore_latest(self):
        from .summary_reading import history
        entries=history(self.settings.data_dir/"reports")
        latest=next((x for x in entries if x["complete"] and x["status"]=="complete" and x["readable"]),None)
        if latest:
            self.load_results(latest["path"])
        self.status.set("本机报告已加载。点击刷新读取聊天，或在历史摘要中阅读已有结果；未调用微信或模型。")

    def show_history(self):
        if self.active or not self.need_settings():
            return
        from .summary_reading import history
        entries=history(self.settings.data_dir/"reports")
        window=tk.Toplevel(self.root)
        window.title("历史摘要 · 仅本机报告")
        window.geometry(f"{self.pixels(900)}x{self.pixels(440)}")
        ttk.Label(window,text="双击一行载入已有摘要；不读取微信、不调用模型。失败或部分任务不会标作已完成。",wraplength=self.pixels(850)).pack(fill="x",padx=self.pixels(12),pady=self.pixels(10))
        frame=ttk.Frame(window)
        frame.pack(fill="both",expand=True,padx=12)
        tree=ttk.Treeview(frame,columns=("name","range","created","status"),show="headings",selectmode="browse")
        for key,title,width in (("name","聊天",190),("range","北京时间范围",310),("created","生成时间",190),("status","状态",130)):
            tree.heading(key,text=title)
            tree.column(key,width=self.pixels(width))
        scroll=ttk.Scrollbar(frame,command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left",fill="both",expand=True)
        scroll.pack(side="right",fill="y")
        for index,item in enumerate(entries):
            def stamp(value):
                return datetime.fromtimestamp(value,ZONE).strftime("%m-%d %H:%M") if isinstance(value,(int,float)) else "未记录"
            state="已完成" if item["complete"] and item["status"]=="complete" else "失败／部分" if item["status"]=="failed" else "处理中／未完成"
            tree.insert("","end",iid=str(index),values=(item["names"],stamp(item["start"])+" → "+stamp(item["end"]),item["created"],state))
        def choose(*_):
            selected=tree.selection()
            if not selected:
                return
            item=entries[int(selected[0])]
            self.load_results(item["path"])
            self.status.set("历史任务："+("已完成。可打开完整摘要。" if item["complete"] and item["status"]=="complete" else "未完成；原因："+(item["error_code"] or item["status"])))
            window.destroy()
        tree.bind("<Double-1>",choose)
        ttk.Button(window,text="载入所选摘要",command=choose).pack(anchor="e",padx=12,pady=10)

    def open_summary(self):
        if self.active or self.report_scope!="reports" or not self.reports:
            return
        if self.saved_summary_dir:
            self.open_summary_page(self.saved_summary_dir/"index.html")
        else:
            self.run("render-summary",self.result_path.parent.name)

    def open_summary_page(self,page):
        import os
        from .export import _plain,ExportError
        try:
            _plain(page)
            os.startfile(str(page))
            return True
        except (OSError,ValueError,ExportError):
            self.status.set("阅读页未能打开；摘要文件保留，可打开摘要目录手动读取index.html。")
            messagebox.showerror("无法打开阅读页","请检查本机浏览器的HTML关联，或打开摘要目录手动读取index.html。",parent=self.root)
            return False

    def open_summary_directory(self):
        if self.active or self.report_scope!="reports" or not self.result_path:
            return
        import os
        from .export import _plain
        directory=self.saved_summary_dir or inside(self.result_path,self.settings.data_dir/"reports").parent
        _plain(directory)
        os.startfile(str(directory))

    def save_summary(self):
        if self.active or self.report_scope!="reports" or not self.reports:
            return
        parent=filedialog.askdirectory(parent=self.root,title="选择完整摘要包的保存位置",mustexist=True)
        if parent:
            from .summary_reading import proposed_directory
            self.run("save-summary",self.result_path.parent.name,destination=proposed_directory(parent))

    def preset(self,value):
        now=datetime.now(ZONE).replace(second=0,microsecond=0)
        midnight=now.replace(hour=0,minute=0)
        a,b=(midnight,now) if value=="today" else ((midnight-timedelta(days=1),midnight) if value=="yesterday" else (now-timedelta(hours={"24":24,"48":48,"72":72,"7":168}[value]),now))
        self.start.set(a.strftime("%Y-%m-%d %H:%M"))
        self.end.set(b.strftime("%Y-%m-%d %H:%M"))

    def invalidate(self):
        if self.prepared and not self.active and self.settings:
            self.cleanup_job(self.prepared)
        self.prepared=None
        if hasattr(self,"generate_button"):
            self.generate_button.configure(state="disabled")
            self.preview_label.set("范围已改变，请重新预览；模型尚未加载。")

    def render_chats(self):
        self.tree.delete(*self.tree.get_children())
        counts={}
        for chat in self.chats:
            counts[chat["name"]]=counts.get(chat["name"],0)+1
        query=self.search.get().casefold()
        for chat in self.chats:
            if query not in chat["name"].casefold() and query not in chat["id"].casefold():
                continue
            if self.kind.get()=="群聊" and not chat["is_group"] or self.kind.get()=="联系人" and chat["is_group"]:
                continue
            name=chat["name"]+(" · "+chat["id"][-8:] if counts[chat["name"]]>1 else "")
            last=datetime.fromtimestamp(chat["last_time"],ZONE).strftime("%m-%d %H:%M") if chat["last_time"] else "—"
            self.tree.insert("", "end",iid=chat["id"],values=("☑" if chat["id"] in self.selected else "☐",name,"群聊" if chat["is_group"] else "联系人",last))
        self.selection_label.set("已选择 %d 个聊天；不会自动选择全部。"%len(self.selected))
        self.record_button.configure(state="normal" if len(self.selected)==1 and not self.active else "disabled")

    def toggle(self,event):
        if self.active:
            return
        item=self.tree.identify_row(event.y) if hasattr(event,"y") and event.type==tk.EventType.ButtonRelease else self.tree.focus()
        if not item:
            return
        if item in self.selected:
            self.selected.remove(item)
        else:
            self.selected.add(item)
        self.invalidate()
        self.render_chats()

    def select_visible(self):
        if not self.active:
            self.selected.update(self.tree.get_children())
            self.invalidate()
            self.render_chats()

    def clear_selection(self):
        if not self.active:
            self.selected.clear()
            self.invalidate()
            self.render_chats()

    def need_settings(self):
        if not self.settings:
            messagebox.showinfo("本机配置","请先填写微信读取组件和Ollama路径。",parent=self.root)
            return False
        return True

    def run(self,command,job_id=None,private_output=None,destination=None,allow_cloud=False,member_account=None,summarize=False):
        if self.active or not self.need_settings():
            return
        python=Path(sys.executable).with_name("python-local-only.exe")
        if not python.is_file():
            messagebox.showinfo("本机安装未完成","请先点击“一、模型 → 初始化本项目运行程序”，再重试。此操作不修改Windows防火墙。",parent=self.root)
            return
        self.job_id=job_id
        self.active_command=command
        self.pending_cancel=False
        self.failure_seen=False
        self.started_at=self.last_progress_at=time.monotonic()
        self.activity.set("任务已开始；模型响应期间分段进度可能暂不变化。")
        self.progress.configure(value=0)
        self.status.set("正在保存完整文件夹；不运行模型，可随时取消。" if command in ("save-export","save-summary")
                        else "正在准备完整摘要阅读页；不读取微信、不运行模型。" if command=="render-summary"
                        else "正在读取本机资料；请等待，可随时取消。")
        self.set_busy(True)
        script=Path(__file__).resolve().parents[2]/"scripts"/"wechat_ui.py"
        args=[str(python),"-B","-X","utf8",str(script),command,"--config",str(self.config_path),"--gui-channel"]
        if job_id:
            args += ["--job",job_id]
        if private_output:
            args += ["--private-output",str(private_output)]
        if destination:
            args += ["--destination",str(destination)]
        if allow_cloud:
            args += ["--allow-cloud"]
        if summarize:
            args += ["--summarize"]
        if member_account is not None:
            args += ["--member-account",member_account]
        self.settings.data_dir.mkdir(parents=True,exist_ok=True)
        logdir=self.settings.data_dir/"logs"
        logdir.mkdir(exist_ok=True)
        self.error_stream=(logdir/"gui-worker.log").open("ab")
        try:
            self.active=ProcessJob(args,self.error_stream)
        except Exception as error:
            self.error_stream.close()
            self.set_busy(False)
            messagebox.showerror("无法启动本机任务",str(error),parent=self.root)
            return
        def consume(worker):
            for line in worker.process.stdout:
                try:
                    self.events.put(json.loads(line))
                except ValueError:
                    # Never forward arbitrary third-party stdout into the UI/Agent.
                    pass
            code=worker.process.wait()
            self.events.put({"phase":"worker_exit","exit_code":code,"command":command})
        threading.Thread(target=consume,args=(self.active,),daemon=True).start()

    def refresh(self):
        if self.need_settings() and not self.active:
            if self.settings.provider_backend=="builtin":
                from .builtin_provider import BuiltinProvider
                from .history import HistoryError
                try:BuiltinProvider(self.settings.data_dir,self.settings.data_dir/"cache")._access()
                except (HistoryError,OSError,ValueError,KeyError):
                    self.connect_wechat()
                    return
            self.invalidate()
            self.run("list",private_output=self.settings.data_dir/"chat-list.json")

    def connect_wechat(self,parent=None):
        if not self.need_settings() or self.active:return
        if self.settings.provider_backend!="builtin":
            self.refresh()
            return
        from .access_ui import AccessDialog
        existing=getattr(self,"access_dialog",None)
        if existing and existing.window.winfo_exists():
            existing.window.lift()
            return
        self.access_dialog=AccessDialog(self,parent or self.root)

    def prepare(self):
        if not self.need_settings() or not self.selected or self.active:
            if not self.selected:
                messagebox.showinfo("请选择聊天","先勾选至少一个联系人或群聊。",parent=self.root)
            return
        try:
            start,end=parse_range(self.start.get(),self.end.get())
        except ValueError as error:
            messagebox.showerror("时间范围",str(error)+"\n格式：2026-10-09 09:00",parent=self.root)
            return
        self.invalidate()
        job=uuid.uuid4().hex
        atomic_json(self.settings.data_dir/"jobs"/job/"selection.json",{"ids":sorted(self.selected),"start":start,"end":end,"include_media":True})
        self.prepared=None
        self.run("prepare",job)

    def generate(self):
        if self.prepared and not self.active:
            cloud=self.settings.model_backend!="local"
            if cloud:
                manifest=read_json(self.settings.data_dir/"jobs"/self.prepared/"manifest.json")
                if not self.confirm_cloud_summary(manifest):
                    return
            self.run("generate",self.prepared,allow_cloud=True) if cloud else self.run("generate",self.prepared)

    def confirm_cloud_summary(self,manifest):
        count="%d条文字"%manifest["total_text"] if "total_text" in manifest else "时间范围内的文字"
        media="\n图片／语音先在本机识别；识别后的语音转写、图片文字和描述也将发送至所选云端模型。" if manifest.get("include_media") else ""
        notice=("本次将发送所选%d个聊天、%s，以及相关发送人、消息时间和分段/合并材料。%s\n"
                "服务：%s\n模型：%s\n图片、原语音和数据库不会上传。云服务可能计费并保留请求。\n匹配摘要检查点会复用；未通过格式校验时可能有限重试。是否允许本次发送？")%(
                    len(manifest["chats"]),count,media,"OpenAI会员（共用计划额度）" if self.settings.model_backend=="membership" else self.settings.cloud_base_url,
                    self.settings.membership_model if self.settings.model_backend=="membership" else self.settings.cloud_model)
        return messagebox.askyesno("确认本次云端摘要",notice,parent=self.root)

    def cloud_test(self):
        if self.active or not self.need_settings() or self.settings.model_backend=="local":
            return
        notice="仅发送3条内置虚构文字及其摘要合并材料，不读取微信。\n服务：%s\n模型：%s\nAPI模式可能产生少量费用；会员模式使用现有额度。是否检查连接与摘要能力？"%(
            "OpenAI会员（共用计划额度）" if self.settings.model_backend=="membership" else self.settings.cloud_base_url,
            self.settings.membership_model if self.settings.model_backend=="membership" else self.settings.cloud_model)
        if messagebox.askyesno("检查云端连接",notice,parent=self.root):
            self.invalidate()
            self.run("cloud-test",uuid.uuid4().hex,allow_cloud=True)

    def member_login(self,action="member-login",account=None):
        if self.active or not self.need_settings():
            return
        from .cloud import ensure_cloud_interpreter
        try:
            ensure_cloud_interpreter()
        except Exception as error:
            messagebox.showerror("会员组件",str(error),parent=self.root)
            return
        if action=="member-login" and not messagebox.askyesno("Continue with ChatGPT",
            "将在OpenAI官方网页登录并为Wechat Local Assistant授权使用会员额度。\n不读取Codex或OpenClaw的凭据；登录不发送微信内容。是否继续？",parent=self.root):
            return
        self.run(action,uuid.uuid4().hex,member_account=account)

    def export_records(self,summarize=False):
        if self.active or not self.need_settings() or len(self.selected)!=1:
            return
        try:
            start,end=parse_range(self.start.get(),self.end.get())
        except ValueError as error:
            messagebox.showerror("时间范围",str(error),parent=self.root)
            return
        cloud=summarize and self.settings.model_backend!="local"
        if cloud and not self.confirm_cloud_summary({"chats":[{}],"include_media":True}):
            return
        self.invalidate()
        job=uuid.uuid4().hex
        atomic_json(self.settings.data_dir/"jobs"/job/"selection.json",
                    {"ids":sorted(self.selected),"start":start,"end":end})
        if summarize:
            self.run("export",job,summarize=True,allow_cloud=cloud)
        else:
            self.run("export",job)

    def open_export(self):
        if self.export_dir and not self.active:
            import os
            from .export import _plain
            directory=self.saved_export_dir or inside(self.export_dir,self.settings.data_dir/"exports")
            _plain(directory)
            os.startfile(str(directory))

    def open_reading(self):
        if self.report_scope!="exports" or self.active:
            return
        from .export import _plain
        import os
        directory=self.saved_export_dir or self.export_dir
        if directory:
            page=Path(directory)/"index.html"
            _plain(page)
            if not page.is_file():
                messagebox.showerror("阅读页不可用","请保留整个记录包，包括index.html和media目录。",parent=self.root)
                return
            os.startfile(str(page))

    def set_busy(self,busy):
        self.layout_result_actions()
        self.result_more_button.configure(state="disabled" if busy else "normal")
        self.cloud_test_button.configure(state="normal" if not busy and self.settings and self.settings.model_backend!="local" else "disabled")
        self.member_login_button.configure(state="disabled" if busy else "normal")
        for button in (self.refresh_button,self.preview_button,self.config_button,self.history_button,*self.range_controls):
            button.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy else "disabled")
        self.generate_button.configure(state="normal" if not busy and self.prepared else "disabled")
        self.record_button.configure(state="normal" if not busy and len(self.selected)==1 else "disabled")
        self.export_summary_button.configure(state="normal" if not busy and len(self.selected)==1 else "disabled")
        self.open_export_button.configure(state="normal" if not busy and self.export_dir else "disabled")
        self.open_reading_button.configure(state="normal" if not busy and self.report_scope=="exports" and self.export_dir else "disabled")
        self.export_button.configure(state="normal" if not busy and self.reports else "disabled")
        for button in (self.open_summary_button,self.save_summary_button):
            button.configure(state="normal" if not busy and self.report_scope=="reports" and self.reports else "disabled")
        self.summary_directory_button.configure(state="normal" if not busy and self.report_scope=="reports" and self.result_path else "disabled")

    def poll(self):
        while not self.events.empty():
            event=self.events.get()
            self.last_progress_at=time.monotonic()
            phase=event.get("phase")
            if phase=="member-ready":
                self.status.set("会员账户和模型已更新。打开模型设置选择会员账户及模型，再点“检查云端连接”。" if event.get("action")!="member-logout"
                                else "已退出此会员账户。" if event.get("revoked") else "已清除本机会员令牌；远端撤销未确认，请在ChatGPT设置中断开。")
            elif phase=="listed":
                self.chats=read_json(self.settings.data_dir/"chat-list.json")["chats"]
                self.selected.intersection_update(x["id"] for x in self.chats)
                self.render_chats()
                self.status.set("聊天列表已刷新。请选择范围，再预览。")
            elif phase=="prepared":
                self.prepared=event["job_id"]
                manifest=read_json(self.settings.data_dir/"jobs"/self.prepared/"manifest.json")
                rows=["%s：%d条文字，%d条非文字%s"%(x["name"],x["text_count"],x["skipped_count"],"待识别／保留" if manifest.get("include_media") else "跳过") for x in manifest["chats"]]
                detail=rows[0]+"；" if len(rows)==1 else "已选%d个聊天；"%len(rows)
                self.preview_label.set(detail+"共%d条文字，范围已冻结。"%manifest["total_text"])
                self.status.set("预览完成。当前没有加载模型。")
            elif phase in ("complete","failed"):
                if event.get("result_path"):
                    self.load_results(event["result_path"])
                    if event.get("export_summary"):
                        self.task_tabs.select(self.export_task)
                if self.active_command not in ("render-summary","save-summary","save-export"):
                    self.prepared=None
                if phase=="complete":
                    self.status.set("云端连接检查完成，摘要已生成。请核对成员甲、10月12日15:00、方案A和300元预算；仅使用内置虚构材料，没有读取微信。" if event.get("synthetic_test")
                                    else "生成完成，复用了本机检查点，本次没有新增模型请求；快照已清理。" if event.get("checkpoint_only")
                                    else "生成完成，采用所选云端模型；摘要保存在本机，输入快照已清理。" if event.get("model_backend") in ("cloud","membership")
                                    else "生成完成，完整输入快照已清理，模型已释放。摘要与短引用保存在本机。")
                    if event.get("include_media"):
                        self.status.set("文字与媒体摘要已生成，原记录包已附在完整摘要包中；识别结果需人工核对。" if event.get("media_content_complete")
                                        else "摘要已生成，但有缺失或未识别媒体；打开完整摘要可查看附带原记录与缺失清单。")
                else:
                    self.failure_seen=True
                    self.progress.configure(value=0)
                    code=event.get("error_code","")
                    reasons={"invalid_evidence_id":"模型选择了本段不存在的证据编号。",
                             "invalid_merge_evidence":"合并结果引用了不存在的事实编号。",
                             "merge_omitted_facts":"合并遗漏了已提取事实，未接受不完整摘要。",
                             "invalid_model_item":"模型返回的结构不符合摘要格式。",
                             "invalid_model_json":"模型未返回有效JSON。",
                             "checkpoint_invalid":"检查点损坏或校验失败，未复用该结果。",
                             "quote_integrity_failed":"程序引用完整性校验失败。",
                             "incomplete_input_coverage":"所选文字没有全部处理完成。"}
                    reasons.update(topic_coverage_failed="主题归类遗漏或重复了事实，未接受该结果。",merge_mixed_topics="不同主题被混合，未接受该合并。")
                    message=reasons.get(code,event.get("message",code or "未知错误"))
                    if code=="unverified_model_quote":
                        message="模型引文与原文不一致，修正后仍未通过。"
                    if event.get("result_path"):
                        data=read_json(event["result_path"])
                        retries=data.get("connection_retries",[])
                        if isinstance(retries,list) and retries:
                            message+=" 已自动重试%d次，此段仍未完成。"%len(retries)
                        diagnostic=data.get("transport_diagnostic",{})
                        phase_names={"tcp_connect":"连接网络","proxy_tunnel":"建立代理隧道","tls_handshake":"TLS握手",
                                     "request_send":"发送请求","response_headers":"等待响应头","response_stream":"接收事件流"}
                        if isinstance(diagnostic,dict) and diagnostic.get("phase") in phase_names:
                            message+=" 故障阶段："+phase_names[diagnostic["phase"]]+"。"
                        from .summary import VALIDATION_REASONS
                        validation=data.get("validation_error",{})
                        detail=VALIDATION_REASONS.get(validation.get("reason"))
                        if detail:
                            message+=" 具体原因："+detail+"。"
                        stage=data.get("progress",{})
                        if stage.get("total"):
                            message+=" 已完成本阶段%d/%d段。"%(stage.get("current",0),stage["total"])
                        message+=" 已通过分段检查点保留；重新预览相同范围可继续，模型不会重做已匹配分段。"
                    self.status.set("任务失败："+message)
                    if not self.pending_cancel:
                        messagebox.showerror("本次任务未完成",message,parent=self.root)
            elif phase=="exported":
                exported=self.load_export(event["result_path"])
                self.prepared=None
                self.status.set("记录导出完成，原消息与识别结果分开保存；模型已释放。" if event["complete"]
                    else "消息记录已导出，但有缺失或未识别内容；请查看导出目录中的清单。模型已释放。")
                image_total=exported.get('kind_counts',{}).get('image',0)
                if image_total:
                    visible=sum(any(a.get('kind')=='image' for a in group) for group in exported.get('media',{}).values())
                    locked=sum(e.get('error_code')=='image_key_required' for e in exported.get('media_errors',[]))
                    self.status.set(self.status.get()+f" 图片已恢复{visible}/{image_total}张，请打开阅读页查看。"+
                        (f" {locked}张需要在模型设置的图片解码页配置密钥。" if locked else ""))
            elif phase=="export-saved":
                self.saved_export_dir=Path(event["result_path"]).parent
                self.status.set("完整记录包已保存，可打开阅读页；媒体随文件夹一起保留。" if event["complete"]
                    else "记录包已保存；原清单中的缺失内容仍未恢复，请查看阅读页和清单。")
            elif phase=="summary-ready":
                self.load_results(event["result_path"])
                if self.open_summary_page(self.result_path.parent/"index.html"):
                    self.status.set("完整摘要阅读页已打开；由已有报告离线生成，未调用模型。")
            elif phase=="summary-saved":
                self.saved_summary_dir=Path(event["result_path"]).parent
                self.saved_location.set("另存位置："+str(self.saved_summary_dir))
                self.status.set("完整摘要包已保存，可打开完整摘要；阅读页、Markdown和结构化事实随文件夹保留。" if event["complete"]
                                else "部分摘要包已保存，任务未完成状态保留。")
            elif phase=="worker_exit":
                if self.active:
                    self.active.close()
                    self.active=None
                    self.error_stream.close()
                self.set_busy(False)
                self.activity.set("任务已结束；后台请求进程已退出。")
                if self.job_id and event["command"] in ("generate","export","cloud-test"):
                    self.cleanup_job(self.job_id)
                    self.prepared=None
                if self.job_id and event["command"] in ("save-export","save-summary"):
                    self.cleanup_save(self.job_id)
                if event["exit_code"]!=0 and not self.pending_cancel and not self.failure_seen:
                    self.progress.configure(value=0)
                    self.status.set("本次任务未完成。已完成结果保留；详情仅在本机logs目录。")
                if self.pending_cancel and self.job_id:
                    if event["command"] not in ("save-export","save-summary","render-summary"):
                        self.cleanup_job(self.job_id)
                        self.prepared=None
                    self.status.set("保存任务已结束；原文件夹及已完成保存的结果保留。" if event["command"] in ("save-export","save-summary")
                        else "阅读页准备已取消；原摘要保留。" if event["command"]=="render-summary"
                        else "任务已取消；本次工作进程、模型和临时快照已结束。")
                if self.closing:
                    if self.prepared:
                        self.cleanup_job(self.prepared)
                    self.root.destroy()
                    return
            else:
                message=event.get("message","正在分段处理所选文字…")
                if phase in ("extract","merge","organize"):
                    prefix="分段整理" if phase=="extract" else "主题归类" if phase=="organize" else "合并第%d轮"%event.get("round",1)
                    message=(prefix+"：已完成%d/%d段"%(event.get("current",0),event.get("total",0)))
                    if event.get("state")=="start":
                        message+="，正在处理第%d段（等待模型响应）"%event["working"]
                    elif event.get("state")=="retry":
                        message+="；第%d段格式纠正未通过，正在缩小分段重试（最多一次拆分）"%event["working"]
                    elif event.get("reused"):
                        message+="；已复用检查点"
                    else:
                        message+="；检查点已保存"
                    self.status.set(message)
                    self.progress.configure(maximum=max(1,event.get("total",1)),value=event.get("current",0))
                    continue
                self.status.set(message+("（%d/%d）"%(event.get("current",0),event["total"]) if event.get("total") else ""))
                if event.get("total"):
                    self.progress.configure(maximum=event["total"],value=event.get("current",0))
        if self.active and self.started_at is not None:
            elapsed=int(time.monotonic()-self.started_at)
            waiting=int(time.monotonic()-(self.last_progress_at or self.started_at))
            note="本次已运行%d秒；当前阶段%d秒无新进度。模型单次请求有超时限制，可取消任务。"%(elapsed,waiting)
            if self.activity.get()!=note:
                self.activity.set(note)
        self.root.after(150,self.poll)

    def load_results(self,path):
        self.report_scope="reports"
        self.task_tabs.select(self.summary_task)
        self.result_title.set("五、查看结果")
        self.saved_summary_dir=None
        self.open_reading_button.configure(state="disabled")
        path=inside(path,self.settings.data_dir/"reports")
        data=read_json(path)
        self.reports=data.get("chats",[])
        self.result_combo.configure(values=[x["name"] for x in self.reports])
        if self.reports:
            self.result_combo.current(0)
            self.show_report()
        else:
            self.result_chat.set("")
            self.text.configure(state="normal")
            self.text.delete("1.0","end")
            self.text.insert("1.0","本次没有生成可用摘要。请查看进度条旁的失败原因；重新预览后可再次生成。")
            self.text.configure(state="disabled")
            self.export_button.configure(state="disabled")
        self.result_path=path
        self.saved_location.set("已自动保存："+str(path.parent))
        self.set_busy(bool(self.active))

    def load_export(self,path):
        path=inside(path,self.settings.data_dir/"exports")
        data=read_json(path)
        self.report_scope="exports"
        self.task_tabs.select(self.export_task)
        self.result_title.set("五、查看结果")
        self.export_dir=path.parent
        self.saved_export_dir=None
        self.reports=[{"name":data["chat"]["name"]+" · 导出记录","file":str(path.parent/"records.md")}]
        self.result_combo.configure(values=[self.reports[0]["name"]])
        self.result_combo.current(0)
        self.show_report()
        self.result_path=path
        self.saved_location.set("聊天记录导出："+str(path.parent))
        self.set_busy(bool(self.active))
        return data

    def show_report(self):
        index=self.result_combo.current()
        if index<0 or index>=len(self.reports):
            return
        report=self.reports[index]
        path=inside(report["file"],self.settings.data_dir/self.report_scope)
        self.text.configure(state="normal")
        self.text.delete("1.0","end")
        with path.open(encoding="utf-8") as handle:
            preview=handle.read(200001)
        if self.report_scope=="reports":
            from .summary_reading import quoted_markdown
            preview=quoted_markdown(preview)
        notice="这是记录的文字预览。图片、语音请点击“打开阅读页”查看；移动时请保存完整记录包。\n\n" if self.report_scope=="exports" else ""
        self.text.insert("1.0",notice+preview[:200000]+("\n\n[界面只预览前20万字符；完整记录保存在导出文件中。]" if len(preview)>200000 else ""))
        if self.report_scope=="reports":
            for number,line in enumerate(preview[:200000].splitlines(),1):
                if not line:
                    self.text.tag_add("blank",f"{number}.0",f"{number+1}.0")
                if line.startswith("  > "):
                    self.text.tag_add("evidence",f"{number}.0",f"{number}.end")
                for prefix,tag in (("### ","subheading"),("## ","heading"),("# ","title")):
                    if line.startswith(prefix):
                        self.text.delete(f"{number}.0",f"{number}.{len(prefix)}")
                        self.text.tag_add(tag,f"{number}.0",f"{number}.end")
                        break
        self.text.configure(state="disabled")
        self.export_button.configure(state="normal",text="保存完整记录包" if self.report_scope=="exports" else "另存为Markdown")

    def export(self):
        if self.active:
            return
        index=self.result_combo.current()
        if index<0:
            return
        if self.report_scope=="exports":
            parent=filedialog.askdirectory(parent=self.root,title="选择完整记录包的保存位置",mustexist=True)
            if parent:
                from .bundle import proposed_directory
                try:
                    destination=proposed_directory(self.export_dir,parent)
                except Exception:
                    messagebox.showerror("无法保存","无法读取当前记录包的清单。",parent=self.root)
                    return
                self.run("save-export",self.export_dir.name,destination=destination)
            return
        target=filedialog.asksaveasfilename(parent=self.root,defaultextension=".md",filetypes=[("Markdown","*.md")],initialfile="聊天摘要.md")
        if target:
            from .summary_reading import quoted_markdown
            path=inside(self.reports[index]["file"],self.settings.data_dir/"reports")
            Path(target).write_text(quoted_markdown(path.read_text(encoding="utf-8")),encoding="utf-8")
            self.status.set("已另存一份Markdown副本。")

    def cleanup_save(self,job):
        from .bundle import cleanup_saved_export
        from .export import ExportError
        from .worker import task_path
        try:
            cleanup_saved_export(task_path(self.settings,job)/"save-owner.json")
        except (OSError,ValueError,ExportError):
            messagebox.showerror("临时副本未清理","未能安全清理保存临时目录。原记录包和已保存结果保留，请检查本机保存位置。",parent=self.root)

    def cleanup_job(self,job):
        from .worker import clean_snapshot,task_path
        from .model import stop_owned_model
        path=task_path(self.settings,job)
        stop_owned_model(path,self.settings)
        clean_snapshot(path)

    def cancel(self):
        if self.active:
            self.pending_cancel=True
            self.status.set("正在结束本次任务…")
            self.active.cancel()

    def close(self):
        accesses=[getattr(self,name,None) for name in ("access_dialog","image_access_dialog")]
        busy=[access for access in accesses if access and access.busy]
        if busy:
            for access in busy:access.stop.set()
            self.root.after(150,self.close)
            return
        if self.active:
            self.closing=True
            self.cancel()
        else:
            if self.settings and self.prepared:
                self.cleanup_job(self.prepared)
            self.root.destroy()

    def configure(self,initial_tab=None):
        if self.active:
            return
        window=tk.Toplevel(self.root)
        window.title("模型与本机设置 · 私有配置")
        window.transient(self.root)
        window.grab_set()
        content=ttk.Frame(window,padding=15)
        content.pack(fill="both",expand=True)
        values=read_json(self.config_path) if self.config_path.is_file() else {}
        backend=tk.StringVar(value=values.get("model_backend","local"))
        model_choice=ttk.LabelFrame(content,text="文字摘要模型",padding=8)
        model_choice.pack(fill="x",pady=(0,8))
        for value,label in (("local","连接本地Ollama（默认，材料不出本机）"),("cloud","连接云端兼容API（独立计费）"),
                            ("membership","ChatGPT／Codex会员（共用现有额度）")):
            ttk.Radiobutton(model_choice,text=label,variable=backend,value=value).pack(anchor="w",fill="x",pady=self.pixels((0,4)))
        tabs=ttk.Notebook(content)
        tabs.pack(fill="both",expand=True)
        reader_tab=ttk.Frame(tabs,padding=12)
        tabs.add(reader_tab,text="微信首次接入")
        reader_backend=tk.StringVar(value=values.get("provider_backend","external" if values.get("provider_dir") else "builtin"))
        advanced=ttk.LabelFrame(reader_tab,text="高级手动接入（通常不用）",padding=8)
        for value,label in (("builtin","内置MIT读取后端（新安装推荐）"),("external","兼容已有外置读取组件")):
            ttk.Radiobutton(advanced,text=label,variable=reader_backend,value=value).pack(anchor="w",fill="x",pady=4)
        ttk.Label(reader_tab,text="和往常一样打开Windows微信并登录，再点击“连接微信”。程序自动识别账号和记录位置，已保存授权自动复用，不需要选择目录或填写账号ID。首次连接仅确认一次本机授权。",wraplength=600).pack(anchor="w",pady=8)
        reader_root=tk.StringVar()
        reader_account=tk.StringVar()
        reader_key=tk.StringVar()
        reader_consent=tk.BooleanVar(value=False)
        def assist_access():
            self.connect_wechat(window)
        ttk.Button(reader_tab,text="连接微信（自动识别）",style="Primary.TButton",command=assist_access).pack(fill="x",pady=8)
        def toggle_reader_advanced():
            if advanced.winfo_manager():advanced.pack_forget()
            else:advanced.pack(fill="x",pady=8)
        ttk.Button(reader_tab,text="高级排错／手动导入",style="Quiet.TButton",command=toggle_reader_advanced).pack(anchor="w",pady=4)
        for title,var,hidden in (("本人账号目录（含db_storage）",reader_root,False),("本人账号ID（不是昵称）",reader_account,False),("已授权数据库密钥（64位十六进制）",reader_key,True)):
            ttk.Label(advanced,text=title).pack(anchor="w")
            row=ttk.Frame(advanced)
            row.pack(fill="x",pady=(3,7))
            ttk.Entry(row,textvariable=var,show="*" if hidden else "",width=55).pack(side="left",fill="x",expand=True)
            if var is reader_root:
                def choose_account():
                    path=filedialog.askdirectory(parent=window,title="选择本人账号目录（下含db_storage）")
                    if path:reader_root.set(path)
                ttk.Button(row,text="选择目录",command=choose_account).pack(side="left",padx=4)
        ttk.Checkbutton(advanced,text="我确认这是本人账号，并同意仅在本机导入和读取",variable=reader_consent).pack(anchor="w",pady=5)
        def import_database_key():
            try:
                from .builtin_provider import BuiltinProvider
                if self.settings is None or self.settings.provider_backend!="builtin":
                    raise ValueError("请先选择内置读取后端并保存配置，再打开此页。")
                BuiltinProvider(self.settings.data_dir,self.settings.data_dir/"cache").import_key(
                    reader_root.get(),reader_account.get().strip(),reader_key.get().strip(),consent=reader_consent.get())
                reader_key.set("")
                reader_consent.set(False)
                self.selected.clear()
                self.invalidate()
                messagebox.showinfo("本机接入已保存","数据库验证通过，已用当前Windows用户DPAPI加密保存。请刷新聊天列表。",parent=window)
            except Exception:
                reader_key.set("")
                messagebox.showerror("接入未保存","请检查已保存配置、本人账号目录与ID、密钥和确认选项。未覆盖原接入记录。",parent=window)
        ttk.Button(advanced,text="验证并导入数据库密钥（仅本机）",command=import_database_key).pack(anchor="w",pady=8)
        local_tab=ttk.Frame(tabs,padding=8)
        cloud_tab=ttk.Frame(tabs,padding=8)
        tabs.add(local_tab,text="本机与媒体")
        tabs.add(cloud_tab,text="云端连接引导")
        member_tab=ttk.Frame(tabs,padding=8)
        tabs.add(member_tab,text="会员授权")
        state_path=Path(values.get("data_dir",self.config_path.parent))/"membership"/"state.json"
        # Current deployment paths are absolute; expand public placeholders too.
        from .settings import expanded
        state_path=expanded(str(state_path))
        accounts=read_json(state_path).get("accounts",[]) if state_path.is_file() else []
        labels=[a.get("email","会员账户")+" · "+a["id"][:8]+("（已退出）" if not a.get("signed_in") else "") for a in accounts]
        account_combo=ttk.Combobox(member_tab,values=labels,state="readonly",width=60)
        account_combo.pack(fill="x",pady=5)
        account_id=tk.StringVar(value=values.get("membership_account",""))
        member_model=tk.StringVar(value=values.get("membership_model",""))
        model_combo=ttk.Combobox(member_tab,textvariable=member_model,state="readonly",width=60)
        model_combo.pack(fill="x",pady=5)
        def selected_account(*_):
            index=account_combo.current()
            if index<0:
                return
            account_id.set(accounts[index]["id"])
            choices=[m["slug"] for m in accounts[index].get("models",[])]
            model_combo.configure(values=choices)
            if member_model.get() not in choices:
                member_model.set(choices[0] if choices else "")
        account_combo.bind("<<ComboboxSelected>>",selected_account)
        if accounts:
            index=next((i for i,a in enumerate(accounts) if a["id"]==account_id.get()),0)
            account_combo.current(index)
            selected_account()
        ttk.Label(member_tab,text="1. 点击会员登录，在官方网页亲自确认授权。\n2. 登录完成后重新打开本设置页，选择账户和服务器返回的模型。\n3. 选择会员模式并保存，再点击“检查云端连接”。\n与Codex/ChatGPT Work共用额度；不自动转API收费。图片/语音仍本地。",wraplength=600).pack(pady=10)
        member_actions=ttk.Frame(member_tab)
        member_actions.pack(fill="x")
        def member_action(action,account):
            window.destroy()
            self.member_login(action,account)
        ttk.Button(member_actions,text="Continue with ChatGPT（新账户）",command=lambda:member_action("member-login","")).pack(side="left")
        ttk.Button(member_actions,text="重新登录",command=lambda:member_action("member-login",account_id.get())).pack(side="left",padx=4)
        ttk.Button(member_actions,text="刷新模型",command=lambda:member_action("member-models",account_id.get())).pack(side="left",padx=4)
        ttk.Button(member_actions,text="退出账户",command=lambda:member_action("member-logout",account_id.get())).pack(side="left")
        fields={}
        for key,title in (("data_dir","私人数据目录"),("provider_dir","微信读取组件目录"),("ollama_executable","官方ollama.exe"),("model_dir","本机模型目录"),("model","模型名称"),("asr_model_dir","语音模型目录（可选）"),("port","本机端口"),("expected_version","Ollama版本")):
            row=ttk.Frame(local_tab)
            row.pack(fill="x",pady=4)
            ttk.Label(row,text=title,width=19).pack(side="left")
            initial=values.get(key,{"port":11534,"model":"","expected_version":"0.40.1","data_dir":str(self.config_path.parent)}.get(key,""))
            var=tk.StringVar(value="" if initial is None else str(initial))
            fields[key]=var
            ttk.Entry(row,textvariable=var,width=55).pack(side="left",fill="x",expand=True)
        image_tab=ttk.Frame(tabs,padding=12)
        tabs.add(image_tab,text="图片解码")
        ttk.Label(image_tab,text="新版微信V2图片使用独立密钥。推荐先尝试离线接入：由你选本人账号目录和2—9张不同加密JPEG图片，单独授权后本机验证。不会附加微信、不上传；不支持时停止。手动填写仅作高级备用。",wraplength=600).pack(anchor="w",pady=(0,12))
        def assist_images():
            from .image_access_ui import ImageAccessDialog
            existing=getattr(self,"image_access_dialog",None)
            if existing and existing.window.winfo_exists():
                existing.window.lift()
                return
            self.image_access_dialog=ImageAccessDialog(self,window)
        ttk.Button(image_tab,text="协助接入图片（离线验证）",style="Primary.TButton",command=assist_images).pack(fill="x",pady=(0,12))
        image_aes=tk.StringVar()
        image_xor=tk.StringVar()
        for title,var,hidden in (("AES密钥（16字符／32位十六进制）",image_aes,True),("XOR值（0—255；未知可留空）",image_xor,False)):
            ttk.Label(image_tab,text=title).pack(anchor="w")
            ttk.Entry(image_tab,textvariable=var,show="*" if hidden else "",width=55).pack(fill="x",pady=(4,10))
        image_status=tk.StringVar(value="请先保存本机配置。密钥留空不会修改现有记录。JPEG／PNG等可从校验尾部验证XOR值；其他格式可能需要填写。")
        def save_image_key():
            try:
                from .image_keys import ImageKeyStore
                if self.settings is None:raise ValueError('请先保存本机配置。')
                ImageKeyStore(self.settings.data_dir).save(image_aes.get().strip(),int(image_xor.get().strip()) if image_xor.get().strip() else None)
                image_aes.set("")
                self.invalidate()
                image_status.set("图片密钥已用当前Windows用户DPAPI加密保存。请重新预览并导出，以恢复图片。")
            except (ValueError,OSError):
                messagebox.showerror("图片密钥未保存","请检查AES密钥长度、XOR值及本机数据目录；原记录未改。",parent=window)
        ttk.Button(image_tab,text="保存图片密钥（仅本机）",command=save_image_key).pack(anchor="w",pady=(0,12))
        ttk.Label(image_tab,textvariable=image_status,wraplength=600).pack(anchor="w")
        ttk.Label(local_tab,text="组件和模型须已部署；图片/语音识别始终使用这些本地设置。此处不取钥、不自动下载。",wraplength=600).pack(pady=10)
        ttk.Label(cloud_tab,text="1. 在服务商官网开通API，获取Key和模型名（不是聊天App登录）。\n2. 填Base URL，如 https://api.example.com/v1，不含/chat/completions。\n3. 填模型与Key并保存；生成摘要时再次确认发送范围。\n接口需支持Chat Completions与JSON模式。切换后不自动上传、不自动检查连接。",wraplength=600).pack(anchor="w",pady=(0,10))
        cloud_fields={}
        for key,title,initial in (("cloud_base_url","Base URL",values.get("cloud_base_url","")),
                                  ("cloud_model","云端模型名",values.get("cloud_model","")),
                                  ("cloud_timeout","超时（秒）",values.get("cloud_timeout",180)),
                                  ("api_key","API Key（隐藏）","")):
            row=ttk.Frame(cloud_tab)
            row.pack(fill="x",pady=4)
            ttk.Label(row,text=title,width=19).pack(side="left")
            var=tk.StringVar(value=str(initial))
            cloud_fields[key]=var
            ttk.Entry(row,textvariable=var,width=55,show="*" if key=="api_key" else "").pack(side="left",fill="x",expand=True)
        ttk.Label(cloud_tab,text="已有Key留空即保留；修改服务地址须重新填写Key。Key以当前Windows账户DPAPI保护，不进入源码包。\n云端API使用独立按需请求进程，不解除本地读取/界面的外网阻断；服务不支持或网络受限时会报错。",wraplength=600).pack(pady=10)
        def save():
            try:
                data={**values,**{key:var.get().strip() for key,var in fields.items()}}
                data["port"]=int(data["port"])
                data["model_backend"]=backend.get()
                data["provider_backend"]=reader_backend.get()
                data["membership_account"]=account_id.get()
                data["membership_model"]=member_model.get()
                data.update({key:var.get().strip() for key,var in cloud_fields.items() if key!="api_key"})
                data["cloud_timeout"]=int(data["cloud_timeout"])
                new_key=cloud_fields["api_key"].get().strip()
                if data["cloud_base_url"]!=values.get("cloud_base_url","") and not new_key:
                    data["cloud_key_dpapi"]=""
                if new_key:
                    from .credentials import protect_key
                    data["cloud_key_dpapi"]=protect_key(new_key)
                # Validate before replacing the existing working settings.
                temp=self.config_path.with_suffix(".validate.json")
                atomic_json(temp,data)
                try:
                    settings=Settings.load(temp)
                finally:
                    temp.unlink(missing_ok=True)
                if settings.model_backend!="local":
                    from .cloud import ensure_cloud_interpreter
                    ensure_cloud_interpreter()
                atomic_json(self.config_path,data)
                self.settings=settings
                self.selected.clear()
                self.invalidate()
                window.destroy()
                self.update_model_label()
                self.status.set("配置已保存，未发送聊天。请点击刷新、选择范围并预览。")
            except Exception as error:
                messagebox.showerror("配置无效",str(error),parent=window)
        ttk.Button(content,text="保存配置（不发送聊天）",command=save).pack(anchor="e")
        if initial_tab:
            tabs.select(reader_tab if initial_tab=="reader" else {"local":local_tab,"cloud":cloud_tab,"membership":member_tab}.get(backend.get(),local_tab))

    def update_model_label(self):
        self.model_label.set("文字摘要：会员 · "+(self.settings.membership_model or "登录后选模型") if self.settings.model_backend=="membership"
                             else "文字摘要：云端 · "+self.settings.cloud_model if self.settings.model_backend=="cloud"
                             else "文字摘要：本地 · "+self.settings.model)
        self.cloud_test_button.configure(state="normal" if self.settings.model_backend!="local" and not self.active else "disabled")


def launch(config_path):
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError,OSError):
        pass
    try:
        with NamedMutex(r"Local\WechatLocalSummaryUI"):
            root=tk.Tk()
            app=App(root,config_path)
            root.after(300,lambda:app.show_onboarding(automatic=True))
            root.mainloop()
    except Exception as error:
        root=tk.Tk()
        root.withdraw()
        messagebox.showerror("微信本地摘要",str(error),parent=root)
        root.destroy()
        return 1
    return 0
