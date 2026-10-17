"""Consented local access. Automatic discovery never prints keys or private paths."""
import hashlib
import hmac
import importlib.metadata
import json
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import time

from .builtin_provider import BuiltinProvider, account_identity, plain
from .key_hook import HOOK_SCRIPT


MESSAGES={
    "access_consent_required":"请先确认本次本人账号接入授权。",
    "access_dependency_missing":"请在本项目venv安装requirements-access.txt，再重新接入。",
    "access_executable_invalid":"请选择有效的官方Weixin.exe程序。",
    "access_signature_invalid":"未能确认微信的有效腾讯数字签名，未附加进程。",
    "access_process_denied":"无法确认目标进程的当前用户或路径，未继续接入。",
    "access_wechat_running":"请先正常退出微信，再开始等待；程序不会强制关闭微信。",
    "access_multiple_instances":"发现多个微信主实例或不同安装，未猜测目标；请先保留唯一目标。",
    "access_timeout":"本次接入已超时并解除附加。可核对版本后重新授权尝试。",
    "access_capture_failed":"此微信版本或启动时机未能取得有效密钥；已停止，原授权保持。",
    "access_detach_failed":"无法确认附加已解除；请检查微信状态，原授权未覆盖。",
    "access_validation_failed":"所选账号的完整数据库认证或身份核对未通过，原授权保持。",
    "access_account_not_found":"尚未发现可用的微信登录记录。请正常打开并登录Windows微信，再点连接；无需选择文件夹。",
    "access_account_ambiguous":"未能唯一确认本次登录账号，未按文件新旧猜测。请在微信完成目标账号登录后重试。",
}


class AccessError(RuntimeError):
    def __init__(self,code):
        self.code=code
        super().__init__(MESSAGES[code])


def inspect_executable(executable):
    path=plain(executable)
    if path.name.lower()!="weixin.exe" or not path.is_file():
        raise AccessError("access_executable_invalid")
    literal=str(path).replace("'","''")
    script=("$ErrorActionPreference='Stop';$s=Get-AuthenticodeSignature -LiteralPath '"+literal+
            "';$v=(Get-Item -LiteralPath '"+literal+"').VersionInfo;"+
            "@{status=[string]$s.Status;subject=[string]$s.SignerCertificate.Subject;version=[string]$v.FileVersion}|ConvertTo-Json -Compress")
    import base64
    try:
        powershell=Path(os.environ.get("SystemRoot",r"C:\Windows"))/"System32/WindowsPowerShell/v1.0/powershell.exe"
        result=subprocess.run([str(powershell),"-NoProfile","-NonInteractive","-EncodedCommand",
            base64.b64encode(script.encode("utf-16le")).decode("ascii")],capture_output=True,
            encoding="utf-8",errors="replace",timeout=15,creationflags=subprocess.CREATE_NO_WINDOW,check=True)
        info=json.loads(result.stdout.lstrip("\ufeff"))
        if info["status"]!="Valid" or not re.search(r"Tencent|腾讯",info["subject"],re.I):
            raise ValueError()
    except (OSError,ValueError,KeyError,subprocess.SubprocessError):
        raise AccessError("access_signature_invalid") from None
    return path,info["version"]


def main_process(executable):
    import psutil
    owner=psutil.Process().username().casefold()
    candidates=[]
    for process in psutil.process_iter(["pid","name","ppid"]):
        if (process.info["name"] or "").lower()!="weixin.exe":
            continue
        try:
            if process.username().casefold()!=owner:
                continue
            if Path(process.exe()).resolve()!=executable:
                raise AccessError("access_multiple_instances")
            candidates.append((process.pid,process.info["ppid"],process.create_time()))
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            raise AccessError("access_process_denied") from None
    ids={p[0] for p in candidates}
    roots=[p for p in candidates if p[1] not in ids]
    if len(roots)>1:
        raise AccessError("access_multiple_instances")
    return roots[0] if roots else None


def matches_page(raw,page):
    if len(raw)!=32 or len(page)!=4096:
        return False
    key=hashlib.pbkdf2_hmac("sha512",raw,page[:16],256000,32)
    mac=hashlib.pbkdf2_hmac("sha512",key,bytes(v^0x3a for v in page[:16]),2,32)
    return hmac.compare_digest(hmac.new(mac,page[16:4032]+struct.pack("<I",1),hashlib.sha512).digest(),page[4032:])


def capture(device,pid,verify,*,stop,deadline,progress):
    """The raw Frida channel never reaches stdout, diagnostic logs or the GUI."""
    pending=queue.Queue(maxsize=32)
    seen=set()
    def receive(message,data):
        if message.get("type")=="error":
            payload="ERR:"
        elif message.get("type")=="send":
            payload=message.get("payload","")
        else:
            return
        if not isinstance(payload,str) or len(payload)>512:
            return
        if payload.startswith("ERR:"):
            item=None
        elif re.fullmatch(r"KEY:[0-9a-fA-F]{64}",payload):
            item=bytes.fromhex(payload[4:])
            if item in seen:return
            if len(seen)>=32:return
            seen.add(item)
        else:
            return
        try:pending.put_nowait(item)
        except queue.Full:pass
    session=None
    script=None
    try:
        session=device.attach(pid)
        script=session.create_script(HOOK_SCRIPT)
        script.on("message",receive)
        script.load()
        progress("attached")
        while time.monotonic()<deadline:
            if stop():raise KeyboardInterrupt()
            try:item=pending.get(timeout=0.05)
            except queue.Empty:continue
            if item is None:raise AccessError("access_capture_failed")
            if verify(item):
                if stop():raise KeyboardInterrupt()
                return item
        raise AccessError("access_timeout")
    except (KeyboardInterrupt,AccessError):
        raise
    except Exception:
        raise AccessError("access_capture_failed") from None
    finally:
        if script is not None:
            try:script.unload()
            except Exception:pass
        if session is not None:
            try:session.detach()
            except Exception:raise AccessError("access_detach_failed") from None


def acquire_database(settings,account_root,account_id,executable,*,consent=False,stop=None,progress=None,seconds=90):
    if consent is not True:raise AccessError("access_consent_required")
    stop=stop or (lambda:False)
    progress=progress or (lambda phase:None)
    if not 1<=seconds<=120:raise ValueError("接入等待须为1—120秒。")
    root=plain(account_root)
    identity=account_identity(root,account_id)
    if settings.provider_backend!="builtin":raise AccessError("access_validation_failed")
    source=plain(root/"db_storage/session/session.db")
    with source.open("rb") as handle:page=handle.read(4096)
    if len(page)!=4096:raise AccessError("access_validation_failed")
    try:
        if importlib.metadata.version("frida")!="17.23.1":raise ImportError()
        import frida
    except (ImportError,importlib.metadata.PackageNotFoundError):
        raise AccessError("access_dependency_missing") from None
    progress("checking")
    path,version=inspect_executable(executable)
    if main_process(path):raise AccessError("access_wechat_running")
    if stop():raise KeyboardInterrupt()
    deadline=time.monotonic()+seconds
    progress("waiting")
    target=None
    while time.monotonic()<deadline:
        if stop():raise KeyboardInterrupt()
        target=main_process(path)
        if target:break
        time.sleep(0.04)
    if not target:raise AccessError("access_timeout")
    if main_process(path)!=target:raise AccessError("access_process_denied")
    raw=capture(frida.get_local_device(),target[0],lambda value:matches_page(value,page),
                stop=stop,deadline=deadline,progress=progress)
    progress("detached")
    if stop():raise KeyboardInterrupt()
    progress("validating")
    try:
        BuiltinProvider(settings.data_dir,settings.data_dir/"cache").import_key(
            root,identity,raw.hex(),consent=True,cancelled=stop)
    except (OSError,ValueError):
        raise AccessError("access_validation_failed") from None
    finally:
        raw=None
    progress("saved")
    return {"success":True,"wechat_version":version,"identity_confirmed":True}


def connect_database(settings,*,consent=False,stop=None,progress=None,seconds=90):
    """Normal flow: user opens/logs into WeChat; all technical paths are private."""
    if consent is not True:raise AccessError('access_consent_required')
    if settings.provider_backend!='builtin':raise AccessError('access_validation_failed')
    if not 1<=seconds<=120:raise ValueError('连接等待须为1—120秒。')
    from .discovery import running_wechat,discover_accounts
    from .common import read_json
    from .credentials import reveal_key
    stop=stop or (lambda:False)
    progress=progress or (lambda phase:None)
    deadline=time.monotonic()+seconds
    progress('waiting')
    detected=None
    while time.monotonic()<deadline:
        if stop():raise KeyboardInterrupt()
        detected=running_wechat()
        if detected:break
        time.sleep(.25)
    if not detected:raise AccessError('access_account_not_found')
    executable,target,observed=detected
    progress('checking')
    path,version=inspect_executable(executable)
    provider=BuiltinProvider(settings.data_dir,settings.data_dir/'cache')
    accounts=discover_accounts(observed=observed)
    active=[root for root in accounts if root in observed]
    # A previously authorized account needs no new Frida attachment. Legacy
    # records can be revalidated with the inferred canonical owner ID.
    if provider.authorization.is_file() and provider.authorization.stat().st_size<=32768:
        try:
            record=json.loads(reveal_key(read_json(provider.authorization)['dpapi']))
            root=plain(record['root'])
            allowed=active if active else accounts if len(accounts)==1 else []
            if root in allowed and re.fullmatch(r'[a-fA-F0-9]{64}',record['key']):
                if stop():raise KeyboardInterrupt()
                progress('validating')
                provider.import_key(root,account_identity(root),record['key'],consent=True,cancelled=stop)
                progress('saved')
                return {'success':True,'wechat_version':version,'identity_confirmed':True,'reused':True}
        except (OSError,ValueError,KeyError):pass
    try:
        if importlib.metadata.version('frida')!='17.23.1':raise ImportError()
        import frida
    except (ImportError,importlib.metadata.PackageNotFoundError):
        raise AccessError('access_dependency_missing') from None
    if main_process(path)!=target:raise AccessError('access_process_denied')
    matched=[]
    def verify(raw):
        if stop():raise KeyboardInterrupt()
        roots=discover_accounts(observed=observed)
        valid=[]
        for root in roots:
            if stop():raise KeyboardInterrupt()
            try:
                with plain(root/'db_storage/session/session.db').open('rb') as handle:page=handle.read(4096)
                if matches_page(raw,page):valid.append(root)
            except OSError:continue
        if len(valid)>1:
            active_valid=[root for root in valid if root in observed]
            if len(active_valid)!=1:raise AccessError('access_account_ambiguous')
            valid=active_valid
        matched[:]=valid
        return len(valid)==1
    raw=capture(frida.get_local_device(),target[0],verify,stop=stop,deadline=deadline,progress=progress)
    progress('detached')
    try:
        if stop():raise KeyboardInterrupt()
        progress('validating')
        provider.import_key(matched[0],account_identity(matched[0]),raw.hex(),consent=True,cancelled=stop)
    except (OSError,ValueError):raise AccessError('access_validation_failed') from None
    finally:raw=None
    progress('saved')
    return {'success':True,'wechat_version':version,'identity_confirmed':True,'reused':False}
