"""Pipe-only, text-only API worker. Never reads WeChat, files or data providers."""
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local.cloud import request_completion

try:
    raw=sys.stdin.read(65537)
    if len(raw)>65536:
        raise ValueError("云端请求过大。")
    request=json.loads(raw)
    if request.get("backend")=="membership":
        from wechat_local.membership import dispatch
        result=dispatch(request)
    else:
        result={"success":True,"content":request_completion(request)}
    print(json.dumps(result,ensure_ascii=False),flush=True)
except Exception as error:
    # Only controlled diagnostics, never HTTP bodies, URLs, keys or chat text.
    safe=("ContextTooLong","OutputTooLong","RuntimeError","ValueError","MembershipError")
    message=str(error) if type(error).__name__ in safe else "云端请求格式或执行失败。"
    result={"success":False,"message":message,"error_code":getattr(error,"code",type(error).__name__)}
    if type(error).__name__=="MembershipError" and error.diagnostic:
        result["transport_diagnostic"]=error.diagnostic
    print(json.dumps(result,ensure_ascii=False),flush=True)
    raise SystemExit(1)
