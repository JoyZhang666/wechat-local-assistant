"""Local WeChat location discovery; no keys, chat bodies or recursive disk scans."""
import os
from itertools import islice
from pathlib import Path

from .builtin_provider import account_identity,plain
from .key_access import AccessError,main_process


def running_wechat():
    import psutil
    owner=psutil.Process().username().casefold()
    paths=set()
    processes=[]
    for process in psutil.process_iter(['pid','name']):
        if (process.info['name'] or '').lower()!='weixin.exe':continue
        try:
            if process.username().casefold()!=owner:continue
            paths.add(plain(process.exe()))
            processes.append(process)
        except psutil.NoSuchProcess:continue
        except (psutil.AccessDenied,OSError,ValueError):
            raise AccessError('access_process_denied') from None
    if not paths:return None
    if len(paths)!=1:raise AccessError('access_multiple_instances')
    executable=paths.pop()
    target=main_process(executable)
    if not target:return None
    # Open-file names can prove an active location; their contents are not read.
    observed=set()
    for process in processes[:8]:
        try:
            for item in process.open_files():
                path=Path(item.path)
                if (path.name.lower() in ('session.db','session.db-wal')
                        and path.parent.name.lower()=='session'
                        and path.parent.parent.name.lower()=='db_storage'):
                    observed.add(plain(path.parent.parent.parent))
        except (psutil.NoSuchProcess,psutil.AccessDenied,OSError,ValueError):continue
    return executable,target,observed


def storage_roots():
    """Only conventional locations and current-user storage settings, one level deep."""
    import winreg
    documents=[Path.home()/'Documents']
    roots=[]
    settings=((r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders','Personal'),
              (r'Software\Tencent\WeChat','FileSavePath'),
              (r'Software\Tencent\Weixin','FileSavePath'))
    for key,name in settings:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,key) as handle:
                value,_=winreg.QueryValueEx(handle,name)
            if not isinstance(value,str) or not value.strip() or value=='MyDocument:':continue
            path=Path(os.path.expandvars(value))
            if not path.is_absolute():continue
            if name=='Personal':documents.append(path)
            else:roots.extend((path,path/'xwechat_files'))
        except OSError:continue
    if os.environ.get('OneDrive'):documents.append(Path(os.environ['OneDrive'])/'Documents')
    roots.extend(path/'xwechat_files' for path in documents)
    return list(dict.fromkeys(roots))


def discover_accounts(roots=None,observed=()):
    candidates=set()
    for root in list(observed)+list(storage_roots() if roots is None else roots):
        try:
            root=plain(root)
            if not root.is_dir():continue
            if root.name.lower()=='db_storage':root=root.parent
            items=[root] if (root/'db_storage').is_dir() else list(islice(root.iterdir(),128))
            for item in items:
                try:
                    item=plain(item)
                    account_identity(item)
                    storage=item/'db_storage'
                    if (plain(storage/'session/session.db').is_file()
                            and plain(storage/'contact/contact.db').is_file()
                            and plain(storage/'message').is_dir()):candidates.add(item)
                except (OSError,ValueError):continue
        except (OSError,ValueError):continue
    return sorted(candidates,key=lambda path:str(path).casefold())
