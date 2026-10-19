"""Local GUI/CLI bridge. Public stdout contains metadata, never private text."""
import argparse
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import re
import shutil
import stat
import sys
from .common import NamedMutex,atomic_json,inside,read_json
from .settings import Settings,default_config
from . import __version__

READ_LOCK=r"Local\WechatAiAssistantReadOnly"
MODEL_LOCK=r"Local\WechatLocalSummaryModel"


def emit(event,gui=False):
    safe=("phase","success","complete","job_id","current","total","total_text",
          "total_skipped","total_messages","chat_count","error_code","result_path","configured","gui_version")
    print(json.dumps(event if gui else {k:v for k,v in event.items() if k in safe},
                     ensure_ascii=False),flush=True)


def task_path(settings,job_id):
    if not re.fullmatch(r"[0-9a-f]{32}",job_id or ""):
        raise ValueError("任务编号无效。")
    return inside(settings.data_dir/"jobs"/job_id,settings.data_dir)


def clean_snapshot(job_dir):
    directory=Path(job_dir)/"snapshot"
    if directory.is_dir():
        inside(directory,job_dir)
        if any(x.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
               for x in (directory,*directory.rglob("*"))):
            raise ValueError("临时快照包含链接，未执行清理。")
        shutil.rmtree(directory)


def execute(args,settings,progress):
    job=task_path(settings,args.job) if args.job else None
    if args.command in ("member-login","member-models","member-logout"):
        if getattr(args,"member_account",None) is not None:
            from dataclasses import replace
            settings=replace(settings,membership_account=args.member_account)
        from .membership import MembershipModel
        model=MembershipModel(settings,job)
        result=model.command(args.command.removeprefix("member-"))
        return {"phase":"member-ready","success":True,"result_path":result["state_path"],
                "revoked":result.get("revoked"),"action":args.command}
    if args.command=="cloud-test":
        if settings.model_backend not in ("cloud","membership") or not getattr(args,"allow_cloud",False):
            raise ValueError("请先配置云端模型，并确认仅发送内置虚构材料。")
        if job.exists():
            raise ValueError("连接检查需要新的任务编号，不覆盖现有资料。")
        start=int(datetime.fromisoformat("2026-10-10T09:00:00+08:00").timestamp())
        texts=("内置虚构材料：由成员甲整理材料，截止时间为2026年10月12日15:00。",
               "内置虚构材料：明确采用方案A，不采用方案B。",
               "内置虚构材料：本次预算上限300元，预算尚未花费。")
        rows=[{"id":str(i),"chat_id":"synthetic-cloud-test","sender":"虚构成员",
               "is_self":False,"timestamp":start+i,"type":1,"text":text} for i,text in enumerate(texts,1)]
        snapshot=job/"snapshot"/"messages.jsonl"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text("".join(json.dumps(row,ensure_ascii=False)+"\n" for row in rows),encoding="utf-8")
        atomic_json(job/"manifest.json",{"chats":[{"id":"synthetic-cloud-test","name":"云端连接检查（内置虚构材料）",
                    "text_count":3,"skipped_count":0}],"total_text":3,"total_skipped":0,
                    "start":start,"end":start+3600,"messages_file":str(snapshot)})
        from types import SimpleNamespace
        result=execute(SimpleNamespace(command="generate",job=args.job,allow_cloud=True),settings,progress)
        return {**result,"synthetic_test":True}
    append_summary=args.command=="export" and getattr(args,"summarize",False)
    if (args.command=="generate" or append_summary) and settings.model_backend!="local" and not getattr(args,"allow_cloud",False):
        raise ValueError("云端摘要需要本次任务的发送确认；请在界面确认，或显式传入--allow-cloud。")
    if args.command=="save-export":
        from .bundle import save_export_bundle
        if not getattr(args,"destination",None):
            raise ValueError("保存记录包需要新目标目录。")
        source=inside(settings.data_dir/"exports"/args.job,settings.data_dir)
        with NamedMutex(r"Local\WechatLocalExportSave-"+args.job):
            result=save_export_bundle(source,args.destination,owner_file=job/"save-owner.json",progress=progress)
        return {**result,"job_id":args.job}
    if args.command in ("render-summary","save-summary"):
        from .summary_reading import ensure_reading,save_summary
        source=inside(settings.data_dir/"reports"/args.job/"result.json",settings.data_dir/"reports")
        with NamedMutex(r"Local\WechatLocalSummarySave-"+args.job):
            if args.command=="render-summary":
                ensure_reading(source)
                return {"phase":"summary-ready","success":True,"result_path":str(source)}
            if not getattr(args,"destination",None):
                raise ValueError("保存摘要包需要新目标目录。")
            return save_summary(source,args.destination,owner_file=job/"save-owner.json",progress=progress)
    from .provider import create_provider
    from .history import HistoryReader
    if args.command=="status":
        return {"phase":"status","success":True,"configured":True,
                "gui_version":__version__,"private_material_returned":False}
    if args.command=="cancel":
        from .model import stop_owned_model
        stop_owned_model(job,settings)
        clean_snapshot(job)
        return {"phase":"cancelled","job_id":args.job,"success":True}
    if args.command in ("list","prepare"):
        private=inside(args.private_output,settings.data_dir) if args.private_output else None
        if args.command=="list" and private is None:
            # Names are deliberately not returned through the Agent CLI.
            private=settings.data_dir/"chat-list.json"
        progress({"phase":"reading","message":"读取本机聊天记录；此阶段不加载模型。"})
        with NamedMutex(READ_LOCK):
            provider=create_provider(settings,settings.data_dir/"cache")
            from .account_guard import require_account
            require_account(provider,getattr(args,"account_binding",None),getattr(args,"offline_account",False))
            reader=HistoryReader(provider.snapshot())
            if args.command=="list":
                chats=reader.list_chats()
                atomic_json(private,{"chats":chats})
                return {"phase":"listed","success":True,"chat_count":len(chats)}
            selection=read_json(inside(job/"selection.json",settings.data_dir))
            snapshot=job/"snapshot"
            manifest=reader.prepare_selection(selection["ids"],selection["start"],selection["end"],snapshot,
                include_media=bool(selection.get("include_media",False)))
            manifest.update(start=selection["start"],end=selection["end"],snapshot_end=selection["end"])
            for name in ("recognize_images","recognize_voice","account_binding","offline_account"):
                if name in selection:manifest[name]=selection[name]
            atomic_json(job/"manifest.json",manifest)
            return {"phase":"prepared","success":True,"job_id":args.job,
                    "total_text":manifest["total_text"],"total_skipped":manifest["total_skipped"],
                    "chat_count":len(manifest["chats"])}
    if args.command=="export":
        from .export import write_export
        from .media import MediaResolver
        from .model import LocalModel
        selection=read_json(job/"selection.json")
        if len(selection.get("ids",[]))!=1:
            raise ValueError("完整记录导出需要且只能选择一个聊天。")
        progress({"phase":"reading","message":"读取所选聊天全部消息和媒体线索。"})
        with NamedMutex(READ_LOCK):
            provider=create_provider(settings,settings.data_dir/"cache")
            from .account_guard import require_account
            require_account(provider,selection.get("account_binding"),selection.get("offline_account",False))
            manifest=HistoryReader(provider.snapshot()).prepare_export(selection["ids"][0],
                selection["start"],selection["end"],job/"snapshot")
        images=selection.get("recognize_images",False)
        voice=selection.get("recognize_voice",False)
        model=LocalModel(settings,job) if images or voice else None
        resolver=MediaResolver(provider,model,job,progress,settings=settings,images=images,voice=voice)
        output=inside(settings.data_dir/"exports"/args.job,settings.data_dir)
        with NamedMutex(MODEL_LOCK):
            try:
                result=write_export(manifest,output,resolve_media=resolver.resolve,annotate_media=resolver.annotate,
                    progress=lambda event:progress({"phase":"exporting","message":"正在逐条导出所选记录和媒体。",**event}))
                exported={"phase":"exported","success":True,"complete":result["content_complete"],
                        "job_id":args.job,"total_messages":result["total_messages"],
                        "result_path":str(output/"manifest.json")}
                if not append_summary:
                    return exported
                progress(exported)
            finally:
                try:
                    resolver.close()
                finally:
                    if model is not None:model.stop()
                    if not append_summary:
                        clean_snapshot(job)
        from .media_summary import summary_input
        manifest=summary_input([output],job/"snapshot"/"summary-messages.jsonl")
        atomic_json(job/"manifest.json",manifest)
    from .model import LocalModel
    from .summary import summarize_selection
    manifest=read_json(job/"manifest.json")
    inside(manifest["messages_file"],job/"snapshot")
    if manifest.get("include_media") and not manifest.get("media_records"):
        from .media import MediaResolver
        from .media_summary import recognize_selection,summary_input
        local=LocalModel(settings,job)
        provider=create_provider(settings,settings.data_dir/"cache")
        from .account_guard import require_account
        require_account(provider,manifest.get("account_binding"),manifest.get("offline_account",False))
        resolver=MediaResolver(provider,local,job,progress,images=manifest.get("recognize_images",True),voice=manifest.get("recognize_voice",True))
        with NamedMutex(MODEL_LOCK):
            try:
                archives=recognize_selection(manifest,settings.data_dir/"exports"/args.job,job/"snapshot",resolver,progress)
                manifest=summary_input(archives,job/"snapshot"/"summary-messages.jsonl")
                atomic_json(job/"manifest.json",manifest)
            finally:
                try:
                    resolver.close()
                finally:
                    local.stop()
        progress({"phase":"media-ready","message":"媒体已保留，正在根据文字及标注的识别结果生成摘要。"})
    if settings.model_backend=="membership":
        from .membership import MembershipModel
        model=MembershipModel(settings,job)
        model.retry_callback=progress
    elif settings.model_backend=="cloud":
        from .cloud import CloudModel
        model=CloudModel(settings,job)
    else:
        model=LocalModel(settings,job)
    with NamedMutex(MODEL_LOCK):
        try:
            from .checkpoints import digest
            context=digest({"backend":settings.model_backend,
                           "model":settings.membership_model if settings.model_backend=="membership" else settings.cloud_model if settings.model_backend=="cloud" else settings.model,
                           "account":settings.membership_account if settings.model_backend=="membership" else digest(settings.cloud_key_dpapi) if settings.model_backend=="cloud" else settings.expected_version,
                           "endpoint":settings.cloud_base_url if settings.model_backend=="cloud" else ""})
            from .summary_structure import load_profile
            profile=load_profile(settings.data_dir)
            result=summarize_selection(manifest,settings.data_dir/"reports",model.infer,progress,checkpoint_context=context,summary_profile=profile)
            if manifest.get("media_records"):
                from .media_summary import attach_records
                progress({"phase":"saving-media","message":"正在将原记录与媒体纳入可独立保存的摘要包。"})
                attach_records(result,manifest)
            report_path=Path(result.get("result_file",result.get("result_path","")))
            if report_path.is_file():
                report=read_json(report_path)
                retry_history=getattr(model,"retry_history",None)
                if isinstance(retry_history,list) and retry_history:
                    report["connection_retry_history"]=retry_history
                report["model_backend"]=settings.model_backend
                report["model"]=settings.membership_model if settings.model_backend=="membership" else settings.cloud_model if settings.model_backend=="cloud" else settings.model
                report["checkpoint_only"]=(len(result.get("chats",[]))==1 and result.get("checkpoint_saved")==0
                                           and result.get("checkpoint_reused",0)>0)
                atomic_json(report_path,report)
                from .summary_reading import ensure_reading
                ensure_reading(report_path)
            return {"phase":"complete","success":True,"complete":True,"job_id":args.job,"model_backend":settings.model_backend,
                    "result_path":result.get("result_file",result.get("result_path","")),
                    "checkpoint_only":len(result.get("chats",[]))==1 and result.get("checkpoint_saved")==0 and result.get("checkpoint_reused",0)>0,
                    "include_media":bool(manifest.get("include_media")),"media_content_complete":result.get("media_content_complete"),
                    "export_summary":append_summary,
                    "total_text":manifest["total_text"],"total_skipped":manifest["total_skipped"]}
        except Exception as error:
            partial=getattr(error,"partial_results",None)
            if manifest.get("media_records") and isinstance(partial,dict) and partial.get("result_file") and "media_records" not in partial:
                from .media_summary import attach_records
                attach_records(partial,manifest)
            if settings.model_backend!="local":
                from .summary import SummaryError
                if isinstance(error,SummaryError):
                    if error.code=="local_inference_failed":
                        message=getattr(model,"last_error","") or "云端请求未完成。"
                        error.code="cloud_inference_failed"
                        specific=getattr(model,"last_error_code",None)
                        if isinstance(specific,str) and specific:
                            error.code=specific
                        error.args=("云端摘要失败："+message,)
                    else:
                        error.args=("摘要格式或原文依据校验失败："+error.code,)
                    partial=error.partial_results
                    if partial.get("result_file"):
                        report_path=inside(partial["result_file"],settings.data_dir/"reports")
                        partial["error_code"]=error.code
                        retries=getattr(model,"retry_events",None)
                        if isinstance(retries,list):
                            partial["connection_retries"]=retries
                        partial["diagnostic_message"]=str(error)
                        diagnostic=getattr(model,"last_transport_diagnostic",None)
                        if isinstance(diagnostic,dict) and diagnostic:
                            partial["transport_diagnostic"]=diagnostic
                        atomic_json(report_path,partial)
            raise
        finally:
            model.stop()
            clean_snapshot(job)


def main(argv=None):
    parser=argparse.ArgumentParser(description="本机微信选择摘要；默认仅输出状态和数量")
    parser.add_argument("--version",action="version",version=__version__)
    parser.add_argument("command",choices=("gui","status","list","prepare","generate","export","save-export","render-summary","save-summary","cloud-test","member-login","member-models","member-logout","cancel"))
    parser.add_argument("--config",type=Path,default=default_config())
    parser.add_argument("--job")
    parser.add_argument("--private-output",type=Path)
    parser.add_argument("--destination",type=Path)
    parser.add_argument("--allow-cloud",action="store_true",help="仅本次文字摘要允许发送选定材料到已配置云端接口")
    parser.add_argument("--summarize",action="store_true",help="完整记录识别后追加摘要；云端仍需--allow-cloud")
    parser.add_argument("--account-binding",help="本次确认的绑定账号指纹，不是密钥")
    parser.add_argument("--offline-account",action="store_true",help="明确读取已确认绑定账号的本机记录")
    parser.add_argument("--member-account",help=argparse.SUPPRESS)
    parser.add_argument("--gui-channel",action="store_true",help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if args.command=="gui":
        from .gui import launch
        return launch(args.config)
    if args.gui_channel and sys.stdin.readline().strip()!="RUN":
        return 2
    log=None
    settings=None
    try:
        purpose="reader" if args.command in ("list","prepare","export") else "summary" if args.command in ("generate","cloud-test") else "basic"
        settings=Settings.load(args.config,purpose=purpose)
        if args.command in ("export","generate") and args.job:
            source=task_path(settings,args.job)/("selection.json" if args.command=="export" else "manifest.json")
            choice=read_json(source)
            if args.command=="export" and args.summarize:Settings.load(args.config,purpose="summary")
            if args.command=="export" or choice.get("include_media"):
                if choice.get("recognize_images",args.command=="generate"):Settings.load(args.config,purpose="local")
                if choice.get("recognize_voice",args.command=="generate"):Settings.load(args.config,purpose="voice")
        if args.command in ("prepare","generate","export","save-export","render-summary","save-summary","cloud-test","member-login","member-models","member-logout","cancel") and not args.job:
            raise ValueError("本命令需要已创建的本机任务编号。")
        settings.data_dir.mkdir(parents=True,exist_ok=True)
        (settings.data_dir/"logs").mkdir(exist_ok=True)
        log=(settings.data_dir/"logs"/"worker.log").open("a",encoding="utf-8")
        # Keep any third-party diagnostic output local, including native messages.
        def progress(event):
            emit(event,args.gui_channel)
        result=execute(args,settings,progress)
        emit(result,args.gui_channel)
        return 0
    except BaseException as error:
        if isinstance(error,KeyboardInterrupt):
            code="cancelled"
        else:
            code=getattr(error,"code",type(error).__name__)
        if log:
            import traceback
            traceback.print_exc(file=log)
        if settings and args.job and args.command not in ("save-export","render-summary","save-summary"):
            try:
                clean_snapshot(task_path(settings,args.job))
            except Exception:
                pass
        event={"phase":"failed","success":False,"job_id":args.job,"error_code":code}
        if args.gui_channel:
            event["message"]=str(error)
            partial=getattr(error,"partial_results",None)
            if isinstance(partial,dict):
                event["result_path"]=partial.get("result_file",partial.get("result_path",""))
        emit(event,args.gui_channel)
        return 1
    finally:
        if log:
            log.close()


if __name__=="__main__":
    raise SystemExit(main())
