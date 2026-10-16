"""Explicit account-scoped access. No process attachment or key extraction."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import stat

from .common import NamedMutex, atomic_json, read_json
from .credentials import protect_key, reveal_key
from .database_crypto import decrypt_pages
from .history import DatabaseSnapshot, HistoryError, READ_MUTEX
from .wal_snapshot import read_snapshot


def plain(path):
    """Reject links before resolving, including junctions on Windows."""
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("reader_link")
    return path.resolve()


class BuiltinProvider:
    def __init__(self, data_dir, cache_dir):
        self.authorization = plain(Path(data_dir) / "reader" / "authorization.json")
        self.cache_dir = plain(cache_dir)

    def import_key(self, account_root, account_id, key_hex, *, consent=False):
        """Save only after explicit consent and successful contact/session validation."""
        if not consent or not re.fullmatch(r"[A-Za-z0-9_@.-]{1,128}", account_id):
            raise ValueError("请选择本人账号并确认本次导入。")
        if not isinstance(key_hex, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", key_hex):
            raise ValueError("数据库密钥须为64位十六进制。")
        root = plain(account_root)
        if not root.is_dir() or not (root / "db_storage").is_dir():
            raise ValueError("请选择本人账号目录，其下应包含db_storage。")
        with NamedMutex(READ_MUTEX):
            # Staging is private, independent of an existing working account cache.
            import tempfile
            with tempfile.TemporaryDirectory(prefix="reader-check-", dir=self._private_parent()) as temp:
                for category in ("contact", "session"):
                    source = plain(root / "db_storage" / category / (category + ".db"))
                    plain(str(source) + "-wal")
                    output = Path(temp) / (category + ".db")
                    decrypt_pages(bytes.fromhex(key_hex), read_snapshot(str(source)), output)
                    with closing(sqlite3.connect(output.as_uri() + "?mode=ro", uri=True)) as conn:
                        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                            raise ValueError("database_integrity")
                        query = "SELECT username,remark,nick_name FROM contact LIMIT 0" if category == "contact" else "SELECT username FROM SessionTable LIMIT 0"
                        conn.execute(query)
            record = {"root": str(root), "account": account_id, "key": key_hex.lower()}
            atomic_json(self.authorization, {"version": 1, "dpapi": protect_key(json.dumps(record))})

    def _private_parent(self):
        plain(self.authorization.parent).mkdir(parents=True, exist_ok=True)
        return self.authorization.parent

    def _access(self):
        plain(self.authorization)
        if not self.authorization.is_file() or self.authorization.stat().st_size > 32768:
            raise HistoryError("请先在微信接入页导入本人账号的已授权数据库密钥。")
        record = json.loads(reveal_key(read_json(self.authorization)["dpapi"]))
        root = plain(record["root"])
        if not root.is_dir() or not re.fullmatch(r"[a-f0-9]{64}", record["key"]):
            raise HistoryError("本机接入记录无效，请重新导入。")
        identity = hashlib.sha256((str(root) + "\0" + record["account"] + "\0" + record["key"]).encode()).hexdigest()
        return root, record["account"], bytes.fromhex(record["key"]), identity

    def _decrypt(self, root, key, identity, relative):
        source = plain(root / "db_storage" / relative)
        if not source.is_relative_to(root / "db_storage"):
            raise ValueError("reader_scope")
        # Validate the companion before read_snapshot can open it.
        plain(str(source) + "-wal")
        destination = plain(self.cache_dir / identity / relative)
        data = read_snapshot(str(source))
        fingerprint = hashlib.sha256(data).hexdigest()
        stamp = destination.with_suffix(".stamp.json")
        plain(stamp)
        if destination.is_file() and stamp.is_file():
            record = read_json(stamp)
            if record.get("source") == fingerprint and record.get("output") == hashlib.sha256(destination.read_bytes()).hexdigest():
                return destination
        decrypt_pages(key, data, destination)
        atomic_json(stamp, {"source": fingerprint, "output": hashlib.sha256(destination.read_bytes()).hexdigest()})
        return destination

    def _shards(self, root, category):
        folder = plain(root / "db_storage" / category)
        if not folder.is_dir():
            return []
        return [path.relative_to(root / "db_storage") for path in sorted(folder.iterdir())
                if re.fullmatch(category + r"_\d+\.db", path.name) and plain(path).is_file()]

    def snapshot(self):
        try:
            with NamedMutex(READ_MUTEX):
                root, account, key, identity = self._access()
                paths = self._shards(root, "message")
                if not paths:
                    raise ValueError("message_shards_missing")
                messages = [self._decrypt(root, key, identity, path) for path in paths]
                session = self._decrypt(root, key, identity, Path("session/session.db"))
                contact = self._decrypt(root, key, identity, Path("contact/contact.db"))
                with closing(sqlite3.connect(contact.as_uri() + "?mode=ro", uri=True)) as conn:
                    display = {}
                    for username, remark, nick in conn.execute("SELECT username, remark, nick_name FROM contact"):
                        if not isinstance(username, str) or not username or any(v is not None and not isinstance(v, str) for v in (remark, nick)):
                            raise ValueError("contact_schema")
                        display[username] = remark or nick or username
                return DatabaseSnapshot(messages, session, display, account)
        except (OSError, ValueError, KeyError, sqlite3.Error):
            raise HistoryError("内置读取失败：请检查密钥、账号目录和客户端版本；原缓存未作为新结果返回。") from None

    def voice_snapshot(self):
        try:
            with NamedMutex(READ_MUTEX):
                root, _, key, identity = self._access()
                return [self._decrypt(root, key, identity, path) for path in self._shards(root, "media")]
        except (OSError, ValueError, KeyError):
            raise HistoryError("内置语音读取失败；未返回过期缓存。") from None

    def account_data_root(self):
        return self._access()[0]
