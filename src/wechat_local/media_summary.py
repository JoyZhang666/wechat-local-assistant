"""Join preserved records and explicitly labelled machine annotations for summary."""
import json
from pathlib import Path
import shutil
from .common import atomic_json,read_json,inside
from .export import write_export,_records,_plain,MEDIA_KINDS
from .bundle import _members,_digest


def recognize_selection(manifest,archive_root,work_dir,resolver,progress):
    """One frozen selection, per-chat archives; never reread the selected messages."""
    work_dir=Path(work_dir)
    archives=[]
    for index,chat in enumerate(manifest["chats"],1):
        source=work_dir/f"chat-{index:03d}-records.jsonl"
        count=0
        with Path(manifest["messages_file"]).open(encoding="utf-8") as incoming,source.open("w",encoding="utf-8") as out:
            for line in incoming:
                row=json.loads(line)
                if row["chat_id"] not in {c["id"] for c in manifest["chats"]}:
                    raise ValueError("媒体快照包含未选择的聊天。")
                if row["chat_id"]==chat["id"]:
                    out.write(line)
                    count+=1
        frozen={"chat":chat,"start":manifest["start"],"end":manifest["end"],
                "total_messages":chat["text_count"]+chat["skipped_count"],"messages_file":str(source)}
        if count!=frozen["total_messages"]:
            raise ValueError("媒体快照数量与预览不一致。")
        directory=Path(archive_root)/f"chat-{index:03d}"
        write_export(frozen,directory,resolve_media=resolver.resolve,annotate_media=resolver.annotate,
                     progress=lambda event:progress({"phase":"exporting","message":"正在保留并识别预览范围内的媒体。",**event}))
        archives.append(directory)
    return archives


def summary_input(archives,output_file):
    """Derived content remains separate from original text and carries its origin."""
    chats=[]
    rows=[]
    records=[]
    for directory in map(Path,archives):
        _plain(directory)
        info=read_json(directory/"manifest.json")
        if info.get("status")!="complete" or not info.get("event_coverage_complete"):
            raise ValueError("记录尚未完整导出，不能生成摘要。")
        annotations={}
        with (directory/"annotations.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                annotation=json.loads(line)
                key=annotation["message_id"]
                if key in annotations:
                    raise ValueError("媒体识别编号重复。")
                annotations[key]=annotation
        chat=dict(info["chat"])
        stats={"original_text":0,"recognized":0,"unrecognized":0,"media_total":0}
        frozen={**info,"messages_file":str(directory/"messages.jsonl")}
        count=0
        for message in _records(directory/"messages.jsonl",frozen):
            count+=1
            kind=message.get("kind","unknown")
            text=message.get("text","") if kind=="text" else ""
            origin="original-text"
            if kind=="text":
                stats["original_text"]+=1
                if not text.strip():
                    text="〔空文字消息；没有可提取内容〕"
                    origin="media-status"
            else:
                origin="media-status"
                annotation=annotations.get(message["id"],{})
                data=annotation.get("data") or {}
                parts=[label+"："+data[key] for key,label in (("transcript","语音转写"),("ocr_text","图片文字"),("description","图片描述"))
                       if kind in {"image","voice"} and annotation.get("status")=="complete" and isinstance(data.get(key),str) and data[key].strip()]
                if kind in MEDIA_KINDS:
                    stats["media_total"]+=1
                if parts:
                    origin="machine-annotation"
                    stats["recognized"]+=1
                    text="〔媒体机器识别，未经人工核验〕\n"+"\n".join(parts)
                    if data.get("uncertain"):
                        text+="\n待核对："+str(data["uncertain"])
                else:
                    stats["unrecognized"]+=1
                    status="未检测到人声（机器判断）" if data.get("speech_detected") is False else "原件缺失" if message["id"] in info.get("media_missing",[]) else "识别失败" if annotation.get("status")=="error" else "暂无内容识别能力"
                    text=f"〔{kind}媒体状态：{status}；不能据此推断媒体内容〕"
            rows.append({"id":message["id"],"chat_id":chat["id"],"sender":message.get("sender","未知发送者"),
                         "is_self":message.get("is_self"),"timestamp":message["timestamp"],"type":1,"text":text,
                         "evidence_origin":origin,"media_kind":kind})
        if count!=info["total_messages"]:
            raise ValueError("记录数量不一致。")
        chat.update(text_count=count,skipped_count=0,media_stats=stats)
        chats.append(chat)
        records.append({"source":str(directory),"chat_id":chat["id"],"name":chat["name"],"stats":stats,
                        "content_complete":info["content_complete"] and stats["unrecognized"]==0})
    starts={read_json(Path(d)/"manifest.json")["start"] for d in archives}
    ends={read_json(Path(d)/"manifest.json")["end"] for d in archives}
    if len(starts)!=1 or len(ends)!=1:
        raise ValueError("记录时间范围不一致。")
    Path(output_file).write_text("".join(json.dumps(row,ensure_ascii=False)+"\n" for row in rows),encoding="utf-8")
    return {"chats":chats,"start":starts.pop(),"end":ends.pop(),"messages_file":str(output_file),
            "total_text":len(rows),"total_skipped":0,"include_media":True,"media_records":records}


def attach_records(result,manifest):
    """Copy only validated archive members so each saved summary remains portable."""
    report=Path(result["result_file"]).parent
    result["media_records"]=[]
    atomic_json(result["result_file"],{**result,"status":"packaging","complete":False})
    for index,record in enumerate(manifest.get("media_records",[]),1):
        source=Path(record["source"])
        _,members=_members(source)
        target=report/"records"/f"chat-{index:03d}"
        target.mkdir(parents=True,exist_ok=False)
        for name in members:
            destination=inside(target/name,target)
            destination.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source/name,destination)
            expected=members[name] or _digest(source/name)
            if _digest(destination)!=expected:
                raise ValueError("摘要附件副本校验失败。")
        result["media_records"].append({k:v for k,v in record.items() if k!="source"})
        result["media_records"][-1]["directory"]=target.relative_to(report).as_posix()
    result["media_content_complete"]=all(r["content_complete"] for r in result["media_records"])
    atomic_json(result["result_file"],result)
