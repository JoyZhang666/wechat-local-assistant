"""Create dedicated local Python images and a project-local Windows shortcut."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))


def literal(value):
    return "'"+str(value).replace("'","''")+"'"


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--config",required=True,type=Path)
    parser.add_argument("--initialize-only",action="store_true",help="仅初始化专用Python，不创建快捷方式")
    args=parser.parse_args()
    if Path(sys.prefix).resolve()!=(ROOT/".venv").resolve():
        raise RuntimeError("请使用本项目独立.venv的Python运行此安装步骤。")
    from wechat_local.local_runtime import initialize
    initialize(ROOT)
    scripts=Path(sys.prefix)/"Scripts"
    if args.initialize_only:
        print(json.dumps({"initialized":True,"firewall_verified":False,"startup_registered":False}))
        return
    shortcut=ROOT/"微信助手-界面.lnk"
    target=scripts/"pythonw-local-only.exe"
    entry=ROOT/"scripts"/"wechat_ui.py"
    arguments='-B -X utf8 "'+str(entry)+'" gui --config "'+str(args.config.resolve())+'"'
    command="$ws=New-Object -ComObject WScript.Shell; $link=$ws.CreateShortcut("+literal(shortcut)+"); $link.TargetPath="+literal(target)+"; $link.Arguments="+literal(arguments)+"; $link.WorkingDirectory="+literal(ROOT)+"; $link.Description='Local WeChat Summary'; $link.Save()"
    powershell=Path(os.environ.get("SystemRoot",r"C:\Windows"))/"System32/WindowsPowerShell/v1.0/powershell.exe"
    subprocess.run([str(powershell),"-NoProfile","-NonInteractive","-Command",command],check=True,capture_output=True)
    print(json.dumps({"created":True,"shortcut":str(shortcut),"dedicated_gui_image":str(target),
                      "startup_registered":False}))


if __name__=="__main__":
    main()
