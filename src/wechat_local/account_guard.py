"""Check the bound account against process metadata; never infer identity by age."""
import hashlib
import os
from pathlib import Path

from .discovery import running_wechat
from .history import HistoryError


def account_state(provider):
    root=provider.account_data_root()
    normalized=os.path.normcase(str(Path(root).resolve()))
    binding=hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    try:
        current=running_wechat()
    except (OSError,ValueError,RuntimeError):
        current="unknown"
    observed={os.path.normcase(str(Path(p).resolve())) for p in current[2]} if isinstance(current,tuple) else set()
    status=("matched" if observed=={normalized} else "mismatch" if observed else
            "offline" if current is None else "unknown")
    return {"binding":binding,"account":Path(root).name,"status":status}


def require_account(provider, binding=None, offline=False):
    state=account_state(provider)
    if state["status"]=="mismatch":
        raise HistoryError("当前微信账号与已绑定账号不一致，已停止读取。请重新连接微信。")
    if binding and binding!=state["binding"]:
        raise HistoryError("绑定账号已改变，旧选择已失效。请重新连接并选择聊天。")
    if state["status"]!="matched" and not (offline and binding==state["binding"]):
        raise HistoryError("无法确认当前微信账号。请在界面核对已绑定账号，并明确确认读取其本机记录。")
    return state
