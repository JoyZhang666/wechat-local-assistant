"""Read selected WeChat history without invoking the supplied provider's CLI."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile


READ_MUTEX = r"Local\WechatAiAssistantReadOnly"
TEXT_TYPES = frozenset({1, 2147483653, 1099511627781})
MAX_MESSAGE_BYTES = 8*1024*1024
REQUIRED_COLUMNS = {
    "local_type", "create_time", "real_sender_id", "message_content",
    "WCDB_CT_message_content", "local_id", "server_id",
}


class HistoryError(RuntimeError):
    """A selected history snapshot cannot be read completely."""


@dataclass(frozen=True)
class DatabaseSnapshot:
    message_paths: list[Path]
    session_path: Path
    display_names: dict[str, str]
    my_account_id: str


@contextmanager
def _read_lock():
    from .common import NamedMutex
    with NamedMutex(READ_MUTEX):
        yield


@contextmanager
def _open(path: Path):
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON")
        yield conn
    except (OSError, sqlite3.Error):
        raise HistoryError("历史数据库无法完整读取；请检查接入及本地日志。") from None
    finally:
        if "conn" in locals():
            conn.close()


def _decode_text(content, compression: int, sender_id: str) -> str:
    if isinstance(content,(bytes,str)) and len(content)>MAX_MESSAGE_BYTES:
        raise HistoryError("单条消息超过处理限额；原数据库保持，未截断返回。")
    if compression not in (0, 1, 2, 3, 4):
        raise HistoryError("遇到未知文本编码；停止本次摘要准备。")
    if compression == 4:
        if not isinstance(content, bytes):
            raise HistoryError("压缩文本格式异常；停止本次摘要准备。")
        try:
            import zstandard
            # Streaming decompression handles frames without an advertised length.
            with zstandard.ZstdDecompressor().stream_reader(content) as stream:
                content = stream.read(MAX_MESSAGE_BYTES+1)
        except Exception:
            raise HistoryError("压缩文本无法解码；停止本次摘要准备。") from None
        if len(content)>MAX_MESSAGE_BYTES:
            raise HistoryError("单条压缩消息解码超限；原数据库保持，未截断返回。")
    if isinstance(content, bytes):
        try:
            if content.startswith((b"\xff\xfe", b"\xfe\xff")):
                content = content.decode("utf-16")
            elif content.startswith(b"\xef\xbb\xbf"):
                content = content.decode("utf-8-sig")
            elif b"\x00" in content:
                # Unmarked UTF-16 is accepted only when its byte pattern has an
                # unambiguous endian marker (ASCII text occupies one zero lane).
                odd = content[1::2]
                even = content[::2]
                if len(content) % 2 or not odd:
                    raise UnicodeError()
                if odd.count(0) > len(odd) // 2 and even.count(0) <= len(even) // 2:
                    content = content.decode("utf-16-le")
                elif even.count(0) > len(even) // 2 and odd.count(0) <= len(odd) // 2:
                    content = content.decode("utf-16-be")
                else:
                    raise UnicodeError()
            else:
                content = content.decode("utf-8")
        except UnicodeError:
            raise HistoryError("文本无法无损解码；停止本次摘要准备。") from None
    if not isinstance(content, str) or "\x00" in content:
        raise HistoryError("文本格式异常；停止本次摘要准备。")
    # Only strip a proven sender envelope, never a general 'title:\n' prefix.
    if sender_id and content.startswith(sender_id + ":\n"):
        content = content[len(sender_id) + 2:]
    return content.replace("\r\n", "\n").replace("\r", "\n")


class HistoryReader:
    def __init__(self, snapshot: DatabaseSnapshot):
        self.snapshot = snapshot

    def _inventory(self) -> tuple[list[dict], dict[Path, set[str]]]:
        names = set(self.snapshot.display_names)
        with _open(self.snapshot.session_path) as conn:
            names.update(row[0] for row in conn.execute("SELECT username FROM SessionTable")
                         if isinstance(row[0], str) and row[0])
        tables_by_path: dict[Path, set[str]] = {}
        for path in self.snapshot.message_paths:
            with _open(path) as conn:
                names.update(row[0] for row in conn.execute("SELECT user_name FROM Name2Id")
                             if isinstance(row[0], str) and row[0])
                tables_by_path[path] = {
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*'")
                }
        username_by_table = {
            "Msg_" + hashlib.md5(name.encode("utf-8")).hexdigest(): name for name in names
        }
        aggregate: dict[str, int] = {}
        for path, tables in tables_by_path.items():
            with _open(path) as conn:
                for table in tables:
                    if table not in username_by_table:
                        raise HistoryError("存在无法识别所属聊天的消息表；停止历史读取。")
                    columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                    if not REQUIRED_COLUMNS.issubset(columns):
                        raise HistoryError("消息分片结构不受支持；停止历史读取。")
                    last = conn.execute(f'SELECT MAX(create_time) FROM "{table}"').fetchone()[0]
                    if last is not None:
                        username = username_by_table[table]
                        aggregate[username] = max(aggregate.get(username, 0), int(last))
        chats = [{"id": name, "name": self.snapshot.display_names.get(name) or name,
                  "is_group": name.endswith("@chatroom"), "last_time": last}
                 for name, last in aggregate.items()]
        return sorted(chats, key=lambda c: (-c["last_time"], c["id"])), tables_by_path

    def list_chats(self) -> list[dict]:
        with _read_lock():
            return self._inventory()[0]

    def prepare_selection(self, ids: list[str], start: int, end: int,
                          output_dir: Path, *, include_media=False) -> dict:
        result=self._prepare(ids, start, end, output_dir, export=include_media)
        if include_media:
            result["include_media"]=True
        return result

    def prepare_export(self, chat_id: str, start: int, end: int,
                       output_dir: Path) -> dict:
        result = self._prepare([chat_id], start, end, output_dir, export=True)
        return {"chat": result["chats"][0], "start": start, "end": end,
                "snapshot_end": end, "total_messages": result["total_text"] + result["total_skipped"],
                "kind_counts": result["kind_counts"], "messages_file": result["messages_file"]}

    def _prepare(self, ids: list[str], start: int, end: int, output_dir: Path,
                 export: bool) -> dict:
        if not ids or any(not isinstance(value, str) for value in ids):
            raise HistoryError("请至少选择一个聊天。")
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool) or start >= end):
            raise HistoryError("时间范围无效。")
        ids = list(dict.fromkeys(ids))
        with _read_lock():
            chats, tables_by_path = self._inventory()
            selected = {chat["id"]: chat for chat in chats if chat["id"] in ids}
            if len(selected) != len(ids):
                raise HistoryError("所选聊天不存在；请刷新列表并重新选择。")
            output_dir.mkdir(parents=True, exist_ok=True)
            final = output_dir / ("records.jsonl" if export else "messages.jsonl")
            with tempfile.TemporaryDirectory(prefix="selection-", dir=output_dir) as temp:
                index = sqlite3.connect(str(Path(temp) / "selection.db"))
                try:
                    index.execute("CREATE TABLE selected (id TEXT PRIMARY KEY, chat TEXT, "
                                  "timestamp INTEGER, sequence INTEGER, local_id INTEGER, "
                                  "is_text INTEGER, body TEXT)")
                    for path, tables in tables_by_path.items():
                        with _open(path) as conn:
                            senders = dict(conn.execute("SELECT rowid, user_name FROM Name2Id"))
                            for username in ids:
                                table = "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()
                                if table not in tables:
                                    continue
                                columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
                                sequence_column = "sort_seq" if "sort_seq" in columns else "local_id"
                                fallback_column = "compress_content" if export and "compress_content" in columns else "NULL"
                                packed_column = "packed_info_data" if export and "packed_info_data" in columns else "NULL"
                                cur = conn.execute(
                                    'SELECT local_type, create_time, real_sender_id, message_content, '
                                    f'WCDB_CT_message_content, local_id, server_id, {sequence_column}, '
                                    f'{fallback_column}, {packed_column} FROM "{table}" '
                                    'WHERE create_time >= ? AND create_time < ? '
                                    f'ORDER BY create_time, {sequence_column}, local_id', (start, end))
                                while rows := cur.fetchmany(1000):
                                    for kind, ts, sid, content, compression, local_id, server_id, sequence, fallback, packed in rows:
                                        stable = ([username, "server", int(server_id)] if server_id else
                                                  [username, "local", path.name, int(local_id)])
                                        key = hashlib.sha256(json.dumps(
                                            stable, ensure_ascii=False).encode("utf-8")).hexdigest()
                                        if index.execute("SELECT 1 FROM selected WHERE id=?", (key,)).fetchone():
                                            continue
                                        is_text = kind in TEXT_TYPES
                                        body = None
                                        if is_text or export:
                                            sender_id = senders.get(sid) or ""
                                            message = {
                                                "id": key, "chat_id": username,
                                                "chat_name": selected[username]["name"],
                                                "sender_id": sender_id,
                                                "sender": self.snapshot.display_names.get(sender_id) or sender_id or "未知发送者",
                                                "is_self": (sender_id == self.snapshot.my_account_id
                                                            if sender_id and self.snapshot.my_account_id else None),
                                                "timestamp": int(ts), "type": int(kind) if export else 1,
                                                "original_type": int(kind),
                                            }
                                            if export:
                                                from .raw_history import export_body, packed_attachment_hints
                                                payload_source = "message_content"
                                                if content in (None, "", b"") and fallback not in (None, "", b""):
                                                    content = fallback
                                                    compression = (4 if isinstance(content, bytes)
                                                                   and content.startswith(b"\x28\xb5\x2f\xfd") else 0)
                                                    payload_source = "compress_content"
                                                elif content is None and not is_text:
                                                    content, compression = "", 0
                                                message.update(export_body(kind, content, compression, sender_id))
                                                for hint in packed_attachment_hints(packed):
                                                    if hint not in message["attachment_hints"]:
                                                        message["attachment_hints"].append(hint)
                                                message.update(local_id=int(local_id), server_id=int(server_id or 0),
                                                               sequence=int(sequence), payload_source=payload_source)
                                            else:
                                                message["text"] = _decode_text(content, compression, sender_id)
                                            body = json.dumps(message, ensure_ascii=False)
                                        index.execute("INSERT INTO selected VALUES (?,?,?,?,?,?,?)",
                                                      (key, username, int(ts), int(sequence), int(local_id),
                                                       int(is_text), body))
                    index.commit()
                    result_chats = []
                    for username in ids:
                        text_count, skipped_count = index.execute(
                            "SELECT COALESCE(SUM(is_text),0), COALESCE(SUM(1-is_text),0) "
                            "FROM selected WHERE chat=?", (username,)).fetchone()
                        result_chats.append({
                            "id": username, "name": selected[username]["name"],
                            "is_group": selected[username]["is_group"],
                            "text_count": text_count, "skipped_count": skipped_count,
                        })
                    kind_counts = {}
                    draft = Path(temp) / final.name
                    with draft.open("w", encoding="utf-8", newline="\n") as handle:
                        for (body,) in index.execute(
                                "SELECT body FROM selected " + ("" if export else "WHERE is_text=1 ") +
                                "ORDER BY timestamp, sequence, local_id, id"):
                            handle.write(body + "\n")
                            if export:
                                kind = json.loads(body)["kind"]
                                kind_counts[kind] = kind_counts.get(kind, 0) + 1
                    draft.replace(final)
                except (sqlite3.Error, ValueError, TypeError, OverflowError):
                    raise HistoryError("消息数据格式异常；停止本次摘要准备。") from None
                finally:
                    index.close()
            return {"chats": result_chats,
                    "total_text": sum(chat["text_count"] for chat in result_chats),
                    "total_skipped": sum(chat["skipped_count"] for chat in result_chats),
                    "messages_file": str(final), "snapshot_end": end,
                    **({"kind_counts": kind_counts} if export else {})}
