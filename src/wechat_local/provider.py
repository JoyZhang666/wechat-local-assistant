"""Explicit bridge to an independently installed, user-authorized provider.

No third-party source or native component is bundled by this module.
"""

from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import ctypes
import importlib.util
import os
from pathlib import Path
import sys
import traceback

from .history import DatabaseSnapshot, HistoryError, READ_MUTEX


def create_provider(settings, cache_dir):
    if settings.provider_backend == "builtin":
        from .builtin_provider import BuiltinProvider
        return BuiltinProvider(settings.data_dir, cache_dir)
    return WechatProvider(settings.provider_dir, cache_dir)


@contextmanager
def _private_output(log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        saved = []
        handles = []
        try:
            for fd in (1, 2):
                try:
                    old = os.dup(fd)
                    os.dup2(log.fileno(), fd)
                    saved.append((fd, old))
                except OSError:
                    pass
            if os.name == "nt":
                import msvcrt
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.GetStdHandle.argtypes = [ctypes.c_uint32]
                kernel.GetStdHandle.restype = ctypes.c_void_p
                kernel.SetStdHandle.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
                kernel.SetStdHandle.restype = ctypes.c_int
                for channel in (-11, -12):
                    code = channel & 0xFFFFFFFF
                    handles.append((code, kernel.GetStdHandle(code)))
                    kernel.SetStdHandle(code, msvcrt.get_osfhandle(log.fileno()))
            with redirect_stdout(log), redirect_stderr(log):
                yield log
        finally:
            for code, handle in handles:
                kernel.SetStdHandle(code, handle)
            for fd, old in saved:
                os.dup2(old, fd)
                os.close(old)


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise HistoryError("本机微信读取组件无法载入。")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class WechatProvider:
    def __init__(self, provider_dir: Path, cache_dir: Path):
        self.provider_dir = provider_dir.resolve()
        self.cache_dir = cache_dir.resolve()
        self.log_path = self.cache_dir.parent / "logs" / "provider.log"
        self._access = None
        self._query = None

    def _load(self):
        if self._query is not None:
            return
        access_path = self.provider_dir / "wechat_access.py"
        query_path = self.provider_dir / "query_wechat.py"
        if not access_path.is_file() or not query_path.is_file():
            raise HistoryError("请先配置已授权的本机微信读取组件目录。")
        access = _module(access_path, "_wechat_local_access")
        previous = sys.modules.get("wechat_access")
        sys.modules["wechat_access"] = access
        try:
            query = _module(query_path, "_wechat_local_query")
        finally:
            if previous is None:
                sys.modules.pop("wechat_access", None)
            else:
                sys.modules["wechat_access"] = previous
        self._access, self._query = access, query

    def snapshot(self) -> DatabaseSnapshot:
        from .common import NamedMutex
        with NamedMutex(READ_MUTEX), _private_output(self.log_path) as private_log:
            try:
                self._load()
                access = self._access.load_access()
                my_account_id = access.get("account_wxid")
                if not isinstance(my_account_id, str) or not my_account_id:
                    raise HistoryError("接入配置缺少已确认的本人账号标识。")
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._query.CACHE_DIR = str(self.cache_dir)
                self._query._msg_dbs = None
                self._query._display_cache = None
                messages = self._query.get_msg_dbs()
                session = self._query.decrypt_db("session", "")
                contact = self._query.decrypt_db("contact", "")
                if not messages or not session or not contact:
                    raise HistoryError("本机消息、会话或联系人缓存刷新失败。")
                display = self._query.load_display()
                # The legacy loader hides SQLite errors, so validate contact
                # structure separately instead of treating failure as an empty map.
                import sqlite3
                conn = sqlite3.connect(Path(contact).resolve().as_uri() + "?mode=ro", uri=True)
                try:
                    for username, remark, nick in conn.execute("SELECT username, remark, nick_name FROM contact"):
                        if (not isinstance(username, str) or not username
                                or any(value is not None and not isinstance(value, str) for value in (remark, nick))
                                or username not in display):
                            raise HistoryError("联系人显示名无法完整读取。")
                finally:
                    conn.close()
                return DatabaseSnapshot([Path(path) for path in messages], Path(session),
                                        dict(display), my_account_id)
            except Exception:
                traceback.print_exc(file=private_log)
                raise HistoryError("本机历史读取准备失败；请检查接入及本地日志。") from None

    def voice_snapshot(self) -> list[Path]:
        """Refresh authorized media shards only; never attach to WeChat."""
        from .common import NamedMutex
        with NamedMutex(READ_MUTEX), _private_output(self.log_path) as private_log:
            try:
                self._load()
                access = self._access.load_access()
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                self._query.CACHE_DIR = str(self.cache_dir)
                sources = self._access.find_databases("media", "media_*.db", access)
                paths = []
                for source in sources:
                    source = Path(source)
                    path = self._query.decrypt_source(str(source), str(self.cache_dir / source.name))
                    if not path:
                        raise HistoryError("本机语音媒体分片刷新失败。")
                    paths.append(Path(path))
                return paths
            except Exception:
                traceback.print_exc(file=private_log)
                raise HistoryError("本机语音媒体读取准备失败；请检查本地日志。") from None

    def account_data_root(self) -> Path:
        """Private attachment locator; caller must not log/return this path."""
        from .common import NamedMutex
        with NamedMutex(READ_MUTEX), _private_output(self.log_path) as private_log:
            try:
                self._load()
                access = self._access.load_access()
                return Path(self._access.database_root(access)).resolve().parent
            except Exception:
                traceback.print_exc(file=private_log)
                raise HistoryError("本机附件目录无法定位；请检查接入及本地日志。") from None
