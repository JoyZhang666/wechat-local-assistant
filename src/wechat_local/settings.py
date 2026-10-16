"""Machine paths belong in private settings, never in distributable source."""
from dataclasses import dataclass, field
import os
from pathlib import Path
from .common import read_json


def expanded(value):
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def default_config():
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "WechatLocalAssistant" / "settings.json"


@dataclass
class Settings:
    data_dir: Path
    provider_dir: Path
    ollama_executable: Path
    model_dir: Path
    model: str
    port: int = 11534
    expected_version: str = "0.40.1"
    asr_model_dir: Path | None = None
    model_backend: str = "local"
    cloud_base_url: str = ""
    cloud_model: str = ""
    cloud_key_dpapi: str = field(default="",repr=False)
    cloud_timeout: int = 180
    membership_account: str = ""
    membership_model: str = ""
    provider_backend: str = "external"

    @classmethod
    def load(cls, path):
        if not Path(path).is_file():
            raise ValueError("尚未配置。请将 config.example.json 复制为私有配置，并填写本机路径。")
        data = read_json(path)
        backend=data.get("model_backend","local")
        if backend not in ("local","cloud","membership"):
            raise ValueError("模型来源必须为本地或云端。")
        provider_backend=data.get("provider_backend","external" if data.get("provider_dir") else "builtin")
        if provider_backend not in ("builtin","external"):
            raise ValueError("读取后端必须为builtin或external。")
        required=("data_dir",) + (("provider_dir",) if provider_backend=="external" else ())
        if backend=="local":
            required+=("ollama_executable","model_dir","model")
        for name in required:
            if not isinstance(data.get(name), str) or not data[name].strip():
                raise ValueError("本机配置缺少：" + name)
        port = data.get("port", 11534)
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("本机端口必须在1024至65535之间。")
        for name in ("provider_dir","ollama_executable","model_dir","model"):
            data.setdefault(name,"")
            if not isinstance(data[name],str):
                raise ValueError("本地配置格式无效。")
        if any(c in data["model"] for c in ("\n", "\r", "\0")):
            raise ValueError("本地模型名称无效。")
        asr=data.get("asr_model_dir")
        if asr is not None and not isinstance(asr,str):
            raise ValueError("语音模型目录必须为本机路径或空白。")
        timeout=data.get("cloud_timeout",180)
        if isinstance(timeout,bool) or not isinstance(timeout,int) or not 30<=timeout<=600:
            raise ValueError("云端超时须为30至600秒。")
        url,model,key=(data.get(n,"") for n in ("cloud_base_url","cloud_model","cloud_key_dpapi"))
        if any(not isinstance(x,str) for x in (url,model,key)):
            raise ValueError("云端配置格式无效。")
        if backend=="cloud":
            from .cloud import cloud_endpoint
            cloud_endpoint(url)
            if not model.strip() or any(c in model for c in ("\n","\r","\0")) or not key:
                raise ValueError("请填写云端模型名和API Key。")
        account,member_model=data.get("membership_account",""),data.get("membership_model","")
        import re
        if (not isinstance(account,str) or (account and not re.fullmatch(r"[a-f0-9]{64}",account))
                or not isinstance(member_model,str) or any(c in member_model for c in ("\n","\r","\0"))):
            raise ValueError("会员账户或模型配置格式无效。")
        return cls(*(expanded(data[n]) for n in ("data_dir", "provider_dir", "ollama_executable", "model_dir")),
                   data["model"], port, data.get("expected_version", "0.40.1"),expanded(asr) if asr else None,
                   backend,url,model,key,timeout,account,member_model,provider_backend)

    @property
    def base_url(self):
        return "http://127.0.0.1:" + str(self.port)
