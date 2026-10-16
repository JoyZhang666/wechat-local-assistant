"""Create dedicated local Python images and a project-local Windows shortcut."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def literal(value):
    return "'"+str(value).replace("'","''")+"'"


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",required=True,type=Path)
    args=parser.parse_args()
    if Path(sys.prefix).resolve()!=(ROOT/".venv").resolve():
        raise RuntimeError("请使用本项目独立.venv的Python运行此安装步骤。")
    base=Path(sys.base_prefix)
    scripts=Path(sys.prefix)/"Scripts"
    for source,target in (("python.exe","python-local-only.exe"),("pythonw.exe","pythonw-local-only.exe")):
        original=base/source
        destination=scripts/target
        if not original.is_file():
            raise RuntimeError("当前Python缺少Windows程序："+source)
        if destination.is_file():
            if hashlib.sha256(destination.read_bytes()).digest()!=hashlib.sha256(original.read_bytes()).digest():
                raise RuntimeError("已有专属解释器版本不同，未覆盖。")
        else:
            shutil.copy2(original,destination)
    for name in ("python312.dll","python3.dll","vcruntime140.dll","vcruntime140_1.dll"):
        if (base/name).is_file() and not (scripts/name).exists():
            shutil.copy2(base/name,scripts/name)
    shortcut=ROOT/"微信助手-界面.lnk"
    target=scripts/"pythonw-local-only.exe"
    entry=ROOT/"scripts"/"wechat_ui.py"
    arguments='-B -X utf8 "'+str(entry)+'" gui --config "'+str(args.config.resolve())+'"'
    command="$ws=New-Object -ComObject WScript.Shell; $link=$ws.CreateShortcut("+literal(shortcut)+"); $link.TargetPath="+literal(target)+"; $link.Arguments="+literal(arguments)+"; $link.WorkingDirectory="+literal(ROOT)+"; $link.Description='Local WeChat Summary'; $link.Save()"
    subprocess.run(["powershell.exe","-NoProfile","-NonInteractive","-Command",command],check=True,capture_output=True)
    print(json.dumps({"created":True,"shortcut":str(shortcut),"dedicated_gui_image":str(target),
                      "startup_registered":False}))


if __name__=="__main__":
    main()
