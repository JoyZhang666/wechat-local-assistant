"""Offline summary reading and portable saving; no provider or model imports."""
from datetime import datetime, timezone, timedelta
import hashlib
from html import escape
import json
from pathlib import Path
import re
import tempfile
import uuid

from .common import atomic_json, read_json
from .export import ExportError, _plain, _cancelled
from .bundle import cleanup_saved_export

ZONE=timezone(timedelta(hours=8))
AUDIT_KEYS=("message_count","part_count","batch_count","processed_messages","processed_parts",
            "processed_chars","input_chars","completed_batches","review_base_facts",
            "summary_method","quotes_verified","quote_count","extracted_facts","merge_coverage_checked","topic_count")


def quoted_markdown(text):
    """Adapt legacy evidence lists without changing their text or source files."""
    lines=[]
    for line in text.splitlines():
        line=re.sub(r"^  - (依据：M\d+.*)$",r"  > \1",line)
        if line.startswith("  > ") and lines and lines[-1].startswith("- "):
            lines.append("")
        lines.append(line)
    return "\n".join(lines)+("\n" if text.endswith("\n") else "")


def _text(text):
    return escape(re.sub(r"\\([\\`*_{}\[\]<>#!|])",r"\1",text))


def _markdown_html(markdown,prefix):
    # Only our plain summary headings, bullets and quotes are interpreted.
    # Links, raw HTML, images and scripts in messages remain escaped text.
    parts=[]
    headings=[]
    list_open=False
    item_open=False
    quote_open=False
    def close_quote():
        nonlocal quote_open
        if quote_open:
            parts.append("</blockquote>")
            quote_open=False
    def close_list():
        nonlocal list_open,item_open
        close_quote()
        if item_open:
            parts.append("</li>")
            item_open=False
        if list_open:
            parts.append("</ul>")
            list_open=False
    for line in quoted_markdown(markdown).splitlines():
        if not line.strip():
            continue
        if line.startswith("  > "):
            if not quote_open:
                parts.append("<blockquote>")
                quote_open=True
            parts.append("<p>"+_text(line[4:])+"</p>")
        elif line.startswith("- "):
            close_quote()
            if item_open:
                parts.append("</li>")
            if not list_open:
                parts.append("<ul>")
                list_open=True
            parts.append("<li>"+_text(line[2:]))
            item_open=True
        else:
            close_list()
            heading=re.match(r"^(#{1,3}) (.*)$",line)
            if heading:
                level=min(4,len(heading[1])+1)
                anchor=prefix+"-section-"+str(len(headings)+1)
                headings.append((anchor,heading[2]))
                parts.append(f'<h{level} id="{anchor}">'+_text(heading[2])+f'</h{level}>')
            else:
                parts.append("<p>"+_text(line)+"</p>")
    close_list()
    return "".join(parts),headings


def _json(value):
    return json.dumps(value,ensure_ascii=False,indent=2).encode("utf-8")


def _contents_table(markdown,headings):
    columns=("未解决问题","待办事项","关注点","决定与重要信息")
    categories={"未决问题":0,"待办":1,"主题要点":2,"结论与要点":2,"明确决定":3,"重要信息":3}
    rows=[]
    extras=[]
    current=None
    levels=[len(match[1]) for line in quoted_markdown(markdown).splitlines()
            if (match:=re.match(r"^(#{1,3}) (.*)$",line))]
    def link(heading,label=None):
        anchor,title=heading
        return '<a href="#'+anchor+'">'+_text(title if label is None else label)+'</a>'
    for level,heading in zip(levels,headings):
        title=heading[1]
        if level==2 and title.startswith("主题："):
            current={"heading":link(heading,title[3:]),"cells":[[],[],[],[]]}
            rows.append(current)
        elif title in categories and (level==3 or level==2):
            if current is None:
                current={"heading":link(headings[0],"摘要要点"),"cells":[[],[],[],[]]}
                rows.append(current)
            current["cells"][categories[title]].append(link(heading))
        elif level>1:
            extras.append(link(heading))
            if level==2:
                current=None
    if not rows:
        return '<nav class="sections" aria-label="摘要目录">'+"".join(link(h) for h in headings)+'</nav>'
    header='<tr>'+''.join('<th scope="col">'+title+'</th>' for title in ("主题",*columns))+'</tr>'
    body=''.join('<tr><th scope="row">'+row["heading"]+'</th>'+''.join(
        '<td>'+('<br>'.join(cell) if cell else '<span class="empty">—</span>')+'</td>' for cell in row["cells"])
        +'</tr>' for row in rows)
    return ('<div class="contents"><nav aria-label="其他摘要章节">'+''.join(extras)+
            '</nav><div class="table-scroll"><table class="topic-contents"><caption>主题目录 · 点击进入正文</caption>'+
            '<thead>'+header+'</thead><tbody>'+body+'</tbody></table></div></div>')


def _file_hash(value):
    if isinstance(value,Path):
        from .bundle import _digest
        return _digest(value)
    return hashlib.sha256(value).hexdigest()


def _audit(value):
    return {key:value[key] for key in AUDIT_KEYS if key in value}


def _facts(value):
    def clean(item):
        result={key:item[key] for key in ("section","topic","text","sources","evidence_ids","fact_id") if key in item}
        result["quotes"]=[{key:q[key] for key in ("evidence_id","source","start","end","quote","origin","media_kind") if key in q}
                          for q in item.get("quotes",[])]
        return result
    return {"items":[clean(x) for x in value.get("items",[])],
            "extracted":[clean(x) for x in value.get("extracted",[])],"audit":_audit(value.get("audit",{}))}


def _payload(result_path):
    result_path=Path(result_path).absolute()
    _plain(result_path)
    result=read_json(result_path)
    directory=result_path.parent.resolve()
    meta={key:result[key] for key in ("status","complete","start","end","total_text","total_skipped",
                                    "created_at","model_backend","model","error_code") if key in result}
    meta["human_verified"]=False
    meta["complete"]=bool(result.get("complete")) and result.get("status")=="complete"
    meta["chats"]=[]
    files={}
    articles=[]
    navigation=[]
    if result.get("media_records"):
        from .bundle import _members
        meta["media_content_complete"]=bool(result.get("media_content_complete"))
        meta["media_records"]=[]
        for record in result["media_records"]:
            relative=record.get("directory","")
            if not re.fullmatch(r"records/chat-\d{3,}",relative):
                raise ExportError("摘要记录包路径无效。")
            source=directory/relative
            _plain(source)
            _,members=_members(source)
            for name in members:
                files[relative+"/"+name]=source/name
            meta["media_records"].append({key:record[key] for key in ("name","directory","stats","content_complete")})
            navigation.append('<a href="'+relative+'/index.html">'+escape(str(record["name"]))+' · 原记录与媒体</a>')
    for index,chat in enumerate(result.get("chats",[]),1):
        original=Path(chat["file"])
        name=original.name
        if not re.fullmatch(r"chat-\d{3,}\.md",name):
            raise ExportError("摘要文件名无效，未读取其他文件。")
        path=directory/name
        _plain(path)
        if original.is_absolute() and original.resolve()!=path.resolve():
            raise ExportError("摘要引用越过当前报告目录。")
        if not original.is_absolute() and str(original)!=name:
            raise ExportError("摘要相对路径无效。")
        markdown=quoted_markdown(path.read_text(encoding="utf-8"))
        files[name]=markdown.encode("utf-8")
        facts_name=name[:-3]+"-facts.json"
        fact_path=directory/facts_name
        _plain(fact_path)
        if fact_path.is_file():
            files[facts_name]=_json(_facts(read_json(fact_path)))
        safe_chat={"name":chat.get("name","聊天"),"file":name,"item_count":chat.get("item_count",0),
                   "audit":_audit(chat.get("audit",{}))}
        meta["chats"].append(safe_chat)
        body,headings=_markdown_html(markdown,"chat-"+str(index))
        navigation.append(f'<a href="#chat-{index}">'+escape(str(safe_chat["name"]))+'</a>')
        toc=_contents_table(markdown,headings)
        articles.append(f'<article id="chat-{index}">'+toc+body+'</article>')
    files["summary.json"]=_json(meta)
    state="已完成" if result.get("complete") and result.get("status")=="complete" else "未完成／部分结果"
    model=str(meta.get("model","未记录"))
    files["index.html"]=('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>聊天摘要 · 离线阅读</title><style>
body{margin:0;background:#f3f5f7;color:#202c38;font:17px/1.8 system-ui,"Microsoft YaHei",sans-serif}
main{max-width:960px;margin:24px auto;padding:28px;background:white;border-radius:12px}
h1{font-size:1.7em}h2,h3,h4{line-height:1.5;scroll-margin-top:20px}h3{border-bottom:1px solid #e2e8ee;padding-bottom:6px}
li{margin:.9em 0;overflow-wrap:anywhere}p{overflow-wrap:anywhere}
blockquote{margin:.5em 0 .9em;padding:4px 14px;border-left:3px solid #cbd5df;background:#f7f9fb;color:#667481;font-size:.86em;line-height:1.7}
blockquote p{margin:4px 0;white-space:pre-wrap}.meta{font-size:.85em;color:#536371}
nav{display:flex;flex-wrap:wrap;gap:8px 18px}nav a{color:#365d84}.sections{font-size:.85em;margin-top:22px}
.contents{margin:22px 0;font-size:.9em}.contents nav{margin-bottom:10px}.table-scroll{overflow-x:auto}
.topic-contents{width:100%;min-width:680px;border-collapse:collapse;table-layout:fixed}
.topic-contents caption{text-align:left;font-weight:700;margin-bottom:8px;color:#33475b}
.topic-contents th,.topic-contents td{border:1px solid #d6e0ea;padding:12px;vertical-align:top;text-align:left;overflow-wrap:anywhere}
.topic-contents thead th{background:#eaf0f8;color:#253c55}.topic-contents tbody th{background:#f6f8fc}
.topic-contents a{color:#2459a0;text-underline-offset:3px}.empty{color:#8a97a5}
article{margin-top:24px}a{overflow-wrap:anywhere}@media(max-width:650px){main{margin:0;border-radius:0;padding:16px}body{font-size:16px}}
@media print{body{background:white}main{max-width:none;margin:0;padding:0}nav{display:none}blockquote{break-inside:avoid}.table-scroll{overflow:visible}.topic-contents{min-width:0}}
</style></head><body><main><h1>聊天摘要</h1><p class="meta">'''+escape(state)+
        " · 模型："+escape(model)+" · 结论与待办尚需人工复核。<br>本机离线阅读；Ctrl+F查找，Ctrl+加减缩放，Ctrl+P打印。依据原文以引用块显示。</p><nav>"+
        "".join(navigation)+"</nav>"+"".join(articles)+"</main></body></html>").encode("utf-8")
    return result,files


def _manifest(result,files):
    return {"operation":"selected-summary-reading","version":1,"status":result.get("status","unknown"),
            "complete":bool(result.get("complete")) and result.get("status")=="complete",
            "files":{name:_file_hash(data) for name,data in files.items()}}


def ensure_reading(result_path):
    result,files=_payload(result_path)
    directory=Path(result_path).absolute().parent
    # Preserve original Markdown and facts when adapting historical reports.
    for name in ("index.html","summary.json"):
        path=directory/name
        _plain(path)
        temporary=path.with_suffix(path.suffix+".new")
        _plain(temporary)
        temporary.write_bytes(files[name])
        temporary.replace(path)
    existing={name:directory/name for name in files}
    _plain(directory/"manifest.json")
    _plain(directory/"manifest.json.new")
    atomic_json(directory/"manifest.json",_manifest(result,existing))
    return directory/"index.html"


def history(reports_dir):
    reports_dir=Path(reports_dir)
    _plain(reports_dir)
    entries=[]
    if not reports_dir.is_dir():
        return entries
    for directory in reports_dir.iterdir():
        if directory.name.startswith(".") or not directory.is_dir():
            continue
        path=directory/"result.json"
        try:
            _plain(path)
            result=read_json(path)
            entries.append({"path":path,"names":"、".join(str(x.get("name","聊天")) for x in result.get("chats",[])) or "无可用摘要",
                            "start":result.get("start"),"end":result.get("end"),"status":result.get("status","unknown"),
                            "complete":bool(result.get("complete")),"readable":bool(result.get("chats")),
                            "created":result.get("created_at") or datetime.fromtimestamp(path.stat().st_mtime,ZONE).isoformat(timespec="seconds"),
                            "error_code":result.get("error_code","")})
        except (OSError,ValueError,ExportError,TypeError):
            continue
    return sorted(entries,key=lambda x:x["created"],reverse=True)


def proposed_directory(parent):
    stem="聊天摘要_"+datetime.now(ZONE).strftime("%Y%m%d-%H%M%S")
    target=Path(parent)/stem
    number=2
    while target.exists():
        target=Path(parent)/(stem+"_"+str(number))
        number+=1
    return target


def save_summary(result_path,destination,*,owner_file,cancelled=None,progress=None):
    result,files=_payload(result_path)
    if not result.get("chats"):
        raise ExportError("本次没有可保存的摘要。")
    source=Path(result_path).absolute().parent
    target=Path(destination).absolute()
    _plain(source)
    _plain(target)
    target=target.resolve()
    if target.exists() or not target.parent.is_dir() or target.is_relative_to(source.resolve()):
        raise ExportError("请选择报告目录外的新保存目录；不会覆盖已有资料。")
    files["manifest.json"]=_json(_manifest(result,files))
    _cancelled(cancelled)
    stage=Path(tempfile.mkdtemp(prefix=".wechat-save-summary-",dir=target.parent))
    token=uuid.uuid4().hex
    identity=stage.stat()
    record={"stage":str(stage),"destination":str(target),"token":token,"completed":False,
            "stage_identity":[identity.st_dev,identity.st_ino]}
    try:
        atomic_json(stage/".save-owner.json",{"token":token})
        atomic_json(owner_file,record)
        for index,(name,data) in enumerate(files.items(),1):
            _cancelled(cancelled)
            output=stage/name
            output.parent.mkdir(parents=True,exist_ok=True)
            if isinstance(data,Path):
                with data.open("rb") as incoming,output.open("xb") as outgoing:
                    while chunk:=incoming.read(1024*1024):
                        _cancelled(cancelled)
                        outgoing.write(chunk)
            else:
                output.write_bytes(data)
            if _file_hash(output)!=_file_hash(data):
                raise ExportError("摘要副本校验失败，原报告保留。")
            if progress:
                progress({"phase":"saving-summary","current":index,"total":len(files),"message":"正在保存摘要、引用和阅读页；不运行模型。"})
        _cancelled(cancelled)
        if target.exists():
            raise ExportError("目标目录已存在，未覆盖。")
        (stage/".save-owner.json").unlink()
        stage.rename(target)
        atomic_json(owner_file,{**record,"completed":True})
        return {"phase":"summary-saved","success":True,"complete":bool(result.get("complete")) and result.get("status")=="complete",
                "result_path":str(target/"index.html")}
    except BaseException:
        cleanup_saved_export(owner_file)
        raise
