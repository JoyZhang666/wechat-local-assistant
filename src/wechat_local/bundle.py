"""Save an existing private export without reading WeChat or loading a model."""
import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import uuid

from .common import atomic_json, read_json
from .export import ExportError, _cancelled, _plain, _time

RECORD_FILES=("records.md","index.html","messages.jsonl","annotations.jsonl",
              "manifest.json","AGENT-README.md")


def proposed_directory(export_dir, parent):
    manifest=read_json(Path(export_dir)/"manifest.json")
    name=re.sub(r'[<>:"/\\|?*\x00-\x1f]',"_",str(manifest["chat"].get("name") or "聊天"))
    name=name[:40].rstrip(" .") or "聊天"
    times=[_time(manifest[key]).replace("-","").replace(":","")[:13].replace("T","-")
           for key in ("start","end")]
    stem="聊天记录_"+name+"_"+"_".join(times)
    target=Path(parent)/stem
    number=2
    while target.exists():
        target=Path(parent)/(stem+"_"+str(number))
        number+=1
    return target


def _digest(path):
    digest=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):
            digest.update(chunk)
    return digest.hexdigest()


def _members(source):
    manifest=read_json(source/"manifest.json")
    if manifest.get("status")!="complete" or manifest.get("operation")!="selected-chat-export":
        raise ExportError("只能保存已经结束的聊天记录包；部分内容仍按原清单标注。")
    members={name:None for name in RECORD_FILES}
    for assets in manifest.get("media",{}).values():
        for asset in assets:
            relative=asset.get("relative_path","")
            path=PurePosixPath(relative)
            if (not relative or "\\" in relative or ":" in relative or "\0" in relative
                    or path.is_absolute() or len(path.parts)!=2 or path.parts[0]!="media"
                    or ".." in path.parts or relative!=path.as_posix()
                    or not re.fullmatch(r"[0-9a-f]{64}",asset.get("sha256",""))):
                raise ExportError("媒体清单路径或校验值无效。")
            if relative in members:
                raise ExportError("媒体清单存在重复文件。")
            members[relative]=asset["sha256"]
    for relative in members:
        path=source/relative
        _plain(path)
        if not path.is_file():
            raise ExportError("记录包中的必需文件或已列媒体已丢失。")
    return manifest,members


def cleanup_saved_export(owner_file):
    """Remove an owned incomplete stage, never the published destination."""
    owner_file=Path(owner_file)
    if not owner_file.is_file():
        return
    record=read_json(owner_file)
    if record.get("completed"):
        return
    stage=Path(record["stage"])
    destination=Path(record["destination"])
    if not stage.exists():
        return
    _plain(stage)
    identity=stage.stat()
    marker=stage/".save-owner.json"
    _plain(marker)
    if (stage.parent!=destination.parent or not stage.name.startswith(".wechat-save-")
            or not isinstance(record.get("token"),str) or not re.fullmatch(r"[a-f0-9]{32}",record["token"])
            or [identity.st_dev,identity.st_ino]!=record.get("stage_identity")
            or (marker.exists() and read_json(marker).get("token")!=record.get("token"))):
        raise ExportError("未确认临时保存目录的所有权，未执行清理。")
    for path in stage.rglob("*"):
        _plain(path)
    shutil.rmtree(stage)


def save_export_bundle(export_dir, destination, *, owner_file, cancelled=None, progress=None):
    source=Path(export_dir).absolute()
    target=Path(destination).absolute()
    _plain(source)
    _plain(target)
    source=source.resolve()
    target=target.resolve()
    if target.exists() or not target.parent.is_dir() or target.is_relative_to(source):
        raise ExportError("请选择原记录包以外的新保存目录；不会覆盖已有资料。")
    manifest,members=_members(source)
    _cancelled(cancelled)
    stage=Path(tempfile.mkdtemp(prefix=".wechat-save-",dir=target.parent))
    token=uuid.uuid4().hex
    identity=stage.stat()
    record={"stage":str(stage),"destination":str(target),"token":token,"completed":False,
            "stage_identity":[identity.st_dev,identity.st_ino]}
    try:
        atomic_json(stage/".save-owner.json",{"token":token})
        atomic_json(owner_file,record)
        (stage/"media").mkdir()
        for current,(relative,expected) in enumerate(members.items(),1):
            _cancelled(cancelled)
            incoming,outgoing=source/relative,stage/relative
            _plain(incoming)
            digest=hashlib.sha256()
            with incoming.open("rb") as original,outgoing.open("xb") as copy:
                while chunk:=original.read(1024*1024):
                    _cancelled(cancelled)
                    copy.write(chunk)
                    digest.update(chunk)
            if (expected and digest.hexdigest()!=expected) or _digest(outgoing)!=digest.hexdigest():
                raise ExportError("文件校验失败，未完成保存；原导出未改动。")
            if progress:
                progress({"phase":"saving-export","current":current,"total":len(members),
                          "message":"正在保存记录、媒体和阅读页；不运行模型。"})
        _cancelled(cancelled)
        if target.exists():
            raise ExportError("保存目录已被创建，未覆盖。")
        (stage/".save-owner.json").unlink()
        stage.rename(target)
        atomic_json(owner_file,{**record,"completed":True})
        return {"phase":"export-saved","success":True,"complete":manifest["content_complete"],
                "result_path":str(target/"manifest.json"),"total_messages":manifest["total_messages"]}
    except BaseException:
        # Filesystem identity covers interruption after marker removal too.
        if stage.is_dir():
            _plain(stage)
            current=stage.stat()
            if [current.st_dev,current.st_ino]!=record["stage_identity"]:
                raise ExportError("临时目录所有权发生变化，未清理。")
            for path in (stage,*stage.rglob("*")):
                _plain(path)
            marker=stage/".save-owner.json"
            if marker.exists() and read_json(marker).get("token")!=token:
                raise ExportError("临时目录所有权发生变化，未清理。")
            shutil.rmtree(stage)
        raise
