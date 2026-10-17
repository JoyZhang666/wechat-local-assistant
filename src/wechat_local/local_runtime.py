"""Initialize only this project's dedicated Python images; no firewall changes."""
import hashlib
from pathlib import Path
import shutil
import sys
from .export import ExportError,_plain


def initialize(root=None):
    if sys.version_info[:2]!=(3,12):
        raise ValueError("本版专用运行程序需要Windows Python 3.12。")
    root=Path(root or Path(__file__).absolute().parents[2]).absolute()
    prefix=Path(sys.prefix).absolute()
    try:
        for path in (root,prefix,Path(sys.base_prefix)):_plain(path)
    except ExportError:raise ValueError("初始化不接受链接或目录联接。") from None
    root,prefix=root.resolve(),prefix.resolve()
    if prefix!=(root/".venv").resolve():
        raise ValueError("请使用本项目.venv中的Python初始化专用运行程序。")
    base=Path(sys.base_prefix)
    scripts=prefix/"Scripts"
    copies=[(base/name,scripts/target) for name,target in
            (("python.exe","python-local-only.exe"),("pythonw.exe","pythonw-local-only.exe"))]
    copies += [(base/name,scripts/name) for name in
               ("python312.dll","python3.dll","vcruntime140.dll","vcruntime140_1.dll") if (base/name).is_file()]
    for source,target in copies:
        try:
            _plain(source)
            _plain(target)
        except ExportError:raise ValueError("专用运行程序来源或目标含链接，未复制。") from None
        if not source.is_file() or source.is_symlink() or target.is_symlink():
            raise ValueError("专用运行程序来源或目标无效。")
        if target.exists() and hashlib.sha256(target.read_bytes()).digest()!=hashlib.sha256(source.read_bytes()).digest():
            raise ValueError("已有专用运行程序版本不同，未覆盖；请保留旧环境后重新建立项目venv。")
    for source,target in copies:
        if not target.exists():
            shutil.copy2(source,target)
    return scripts/"python-local-only.exe"
