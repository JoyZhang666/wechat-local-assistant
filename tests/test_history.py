"""Synthetic SQLite fixtures only; never loads installed private access data."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from wechat_local.history import DatabaseSnapshot, HistoryError, HistoryReader
from wechat_local.provider import WechatProvider


CHATS = ["chat_a", "chat_b", "group@chatroom"]


def table(chat):
    return "Msg_" + hashlib.md5(chat.encode("utf-8")).hexdigest()


@contextmanager
def database(path):
    conn = sqlite3.connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="wechat-synthetic-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.session = self.root / "session.db"
        with database(self.session) as conn:
            conn.execute("CREATE TABLE SessionTable (username TEXT, summary TEXT)")
            conn.executemany("INSERT INTO SessionTable VALUES (?,?)",
                             [(chat, "unselected old summary must not leak") for chat in CHATS])
        self.shards = []
        self.add_shard()

    def add_shard(self):
        path = self.root / f"message_{len(self.shards)}.db"
        with database(path) as conn:
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.executemany("INSERT INTO Name2Id VALUES (?)", [("self_id",), ("sender_id",)])
            for chat in CHATS:
                conn.execute(f'CREATE TABLE "{table(chat)}" (local_type INTEGER, create_time INTEGER, '
                             'real_sender_id INTEGER, message_content BLOB, WCDB_CT_message_content INTEGER, '
                             'local_id INTEGER, server_id INTEGER, sort_seq INTEGER)')
        self.shards.append(path)
        return path

    def insert(self, chat="chat_a", ts=100, sender=2, content="hello", compression=0,
               lid=1, server=1, kind=1, shard=0, sequence=None):
        with database(self.shards[shard]) as conn:
            conn.execute(f'INSERT INTO "{table(chat)}" VALUES (?,?,?,?,?,?,?,?)',
                         (kind, ts, sender, content, compression, lid, server,
                          lid if sequence is None else sequence))

    def reader(self):
        return HistoryReader(DatabaseSnapshot(
            self.shards, self.session,
            {"chat_a": "same name", "chat_b": "same name", "group@chatroom": "group",
             "self_id": "synthetic me", "sender_id": "synthetic sender"}, "self_id"))

    def selection(self, ids=None, start=0, end=10000):
        result = self.reader().prepare_selection(ids or ["chat_a"], start, end, self.root / "job")
        rows = [json.loads(line) for line in Path(result["messages_file"]).read_text(encoding="utf-8").splitlines()]
        return result, rows

    def test_same_display_name_uses_exact_ids(self):
        self.insert(content="selected")
        self.insert(chat="chat_b", content="not selected")
        chats = self.reader().list_chats()
        self.assertEqual({c["id"] for c in chats}, {"chat_a", "chat_b"})
        self.assertEqual(chats[0]["name"], chats[1]["name"])
        self.assertFalse(any("summary" in chat for chat in chats))
        result, rows = self.selection()
        self.assertEqual(result["total_text"], 1)
        self.assertEqual([row["text"] for row in rows], ["selected"])

    def test_half_open_time_boundaries(self):
        for number, ts in enumerate([99, 100, 199, 200], 1):
            self.insert(ts=ts, lid=number, server=number)
        result, rows = self.selection(start=100, end=200)
        self.assertEqual([row["timestamp"] for row in rows], [100, 199])
        self.assertEqual(result["snapshot_end"], 200)

    def test_media_preview_freezes_nontext_without_recognition(self):
        self.insert(content="文字",ts=101)
        self.insert(kind=3,content="<msg><img/></msg>",ts=102,lid=2,server=2)
        result=self.reader().prepare_selection(["chat_a"],100,200,self.root/"media-job",include_media=True)
        rows=[json.loads(line) for line in Path(result["messages_file"]).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(result["include_media"])
        self.assertEqual(result["total_text"],1)
        self.assertEqual(result["total_skipped"],1)
        self.assertEqual([r["kind"] for r in rows],["text","image"])

    def test_cross_shard_server_dedup_and_local_fallback(self):
        self.add_shard()
        self.insert(server=50)
        self.insert(server=50, shard=1, lid=99)
        self.insert(server=0, lid=10)
        self.insert(server=0, lid=10, shard=1)
        result, rows = self.selection()
        self.assertEqual(result["total_text"], 3)
        self.assertEqual(len({row["id"] for row in rows}), 3)
        self.assertEqual([row["id"] for row in rows], [row["id"] for row in self.selection()[1]])

    def test_more_than_6000_rows_is_complete(self):
        with database(self.shards[0]) as conn:
            conn.executemany(f'INSERT INTO "{table("chat_a")}" VALUES (?,?,?,?,?,?,?,?)',
                             [(1, 100 + n, 2, f"text {n}", 0, n + 1, n + 1, n + 1)
                              for n in range(6001)])
        result, rows = self.selection()
        self.assertEqual(result["total_text"], 6001)
        self.assertEqual(len(rows), 6001)
        self.assertEqual(rows[-1]["text"], "text 6000")

    def test_actual_zstd_and_utf_encodings(self):
        import zstandard
        contents = ["整理中文测试".encode("utf-8"), "UTF16 中文".encode("utf-16"),
                    b"\xfe\xff" + "UTF16 中文".encode("utf-16-be"),
                    "UTF16 text".encode("utf-16-le"), "UTF16 text".encode("utf-16-be"),
                    zstandard.ZstdCompressor(write_content_size=False).compress("压缩测试".encode("utf-8"))]
        for n, content in enumerate(contents, 1):
            self.insert(content=content, compression=4 if n == 6 else 0, lid=n, server=n)
        self.assertEqual([r["text"] for r in self.selection()[1]],
                         ["整理中文测试", "UTF16 中文", "UTF16 中文", "UTF16 text", "UTF16 text", "压缩测试"])

    def test_nontext_counted_once_not_exported(self):
        self.add_shard()
        self.insert(kind=3, content=b"invalid media data", server=12)
        self.insert(kind=3, content=b"invalid media data", server=12, shard=1)
        self.insert(kind=34, server=13, lid=2)
        self.insert(kind=2147483653, server=14, lid=3)
        result, rows = self.selection()
        self.assertEqual(result["total_skipped"], 2)
        self.assertEqual(result["total_text"], 1)
        self.assertEqual(rows[0]["type"], 1)
        self.assertEqual(rows[0]["original_type"], 2147483653)

    def test_self_identity_by_name2id_not_zero_sender(self):
        for n, sender in enumerate([1, 2, 0], 1):
            self.insert(sender=sender, lid=n, server=n)
        rows = self.selection()[1]
        self.assertEqual([r["is_self"] for r in rows], [True, False, None])
        self.assertEqual(rows[0]["sender_id"], "self_id")

    def test_strip_only_verified_sender_envelope(self):
        self.insert(content="sender_id:\n正文", server=1)
        self.insert(content="Title:\n正文", server=2, lid=2)
        self.assertEqual([r["text"] for r in self.selection()[1]], ["正文", "Title:\n正文"])

    def test_unknown_empty_and_invalid_selection(self):
        self.insert()
        for ids, start, end in [(["missing"], 0, 200), ([], 0, 200), (["chat_a"], 200, 200),
                                (["chat_a"], True, 200)]:
            with self.subTest(ids=ids, start=start), self.assertRaises(HistoryError):
                self.reader().prepare_selection(ids, start, end, self.root / "job")

    def test_empty_selected_time_window(self):
        self.insert()
        result, rows = self.selection(start=200, end=300)
        self.assertEqual(result["total_text"], 0)
        self.assertEqual(rows, [])

    def test_bad_encoding_or_compression_fails_explicitly(self):
        for content, compression in [(b"\xff", 0), (b"not zstd", 4), ("test", 99)]:
            with self.subTest(compression=compression):
                with database(self.shards[0]) as conn:
                    conn.execute(f'DELETE FROM "{table("chat_a")}"')
                self.insert(content=content, compression=compression)
                with self.assertRaises(HistoryError):
                    self.selection()
                self.assertFalse((self.root / "job" / "messages.jsonl").exists())

    def test_nonselected_bad_text_is_not_decoded(self):
        self.insert()
        self.insert(chat="chat_b", content=b"\xff")
        self.assertEqual(self.selection()[0]["total_text"], 1)

    def test_corrupt_or_unsupported_shard_fails_explicitly(self):
        self.insert()
        broken = self.root / "message_1.db"
        broken.write_bytes(b"not a database")
        self.shards.append(broken)
        with self.assertRaises(HistoryError):
            self.reader().list_chats()
        self.shards.pop()
        with database(self.shards[0]) as conn:
            conn.execute(f'DROP TABLE "{table("chat_a")}"')
            conn.execute(f'CREATE TABLE "{table("chat_a")}" (create_time INTEGER)')
        with self.assertRaises(HistoryError):
            self.reader().list_chats()

    def test_same_second_preserves_sort_sequence(self):
        self.insert(content="second", sequence=2, server=1, lid=1)
        self.insert(content="first", sequence=1, server=2, lid=2)
        self.assertEqual([r["text"] for r in self.selection()[1]], ["first", "second"])

    def test_all_type_export_preserves_records_and_scrubs_credentials(self):
        self.insert(content="selected text")
        self.insert(kind=3, lid=2, server=2, content='<msg><img md5="' + 'a' * 32 +
                    '" localpath="relative/image.dat" cdnthumbaeskey="synthetic_secret" /></msg>')
        self.insert(kind=34, lid=3, server=3, content='<msg><voice length="10" /></msg>')
        self.insert(kind=244813135921, lid=4, server=4,
                    content='<msg><appmsg><title>reply</title><refermsg><content>quote</content></refermsg>'
                    '<type>57</type><aeskey>synthetic_secret</aeskey></appmsg></msg>')
        self.insert(kind=49, lid=5, server=5,
                    content='<msg><appmsg><title>report.pdf</title><type>6</type></appmsg></msg>')
        self.insert(chat="chat_b", content="never exported")
        result = self.reader().prepare_export("chat_a", 0, 200, self.root / "export")
        raw = Path(result["messages_file"]).read_text(encoding="utf-8")
        rows = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual(result["total_messages"], 5)
        self.assertEqual([r["kind"] for r in rows], ["text", "image", "voice", "quote", "file"])
        self.assertEqual([r["local_id"] for r in rows], [1, 2, 3, 4, 5])
        self.assertEqual(rows[0]["text"], "selected text")
        self.assertIn("quote", rows[3]["raw_payload"])
        self.assertNotIn("synthetic_secret", raw)
        self.assertNotIn("never exported", raw)
        self.assertEqual(rows[1]["media_status"], "pending")
        self.assertIn({"type": "path", "value": "relative/image.dat"}, rows[1]["attachment_hints"])

    def test_opaque_binary_export_is_explicit_partial_not_base64(self):
        self.insert(kind=3, content=b"\xff\x00\x80\x81")
        result = self.reader().prepare_export("chat_a", 0, 200, self.root / "export")
        row = json.loads(Path(result["messages_file"]).read_text(encoding="utf-8"))
        self.assertEqual(row["payload_status"], "opaque_binary_not_exported")
        self.assertIsNone(row["body_binary"])
        self.assertEqual(row["binary_size"], 4)
        self.assertEqual(len(row["binary_sha256"]), 64)

    def test_all_type_export_over_6000_rows_and_time_boundary(self):
        with database(self.shards[0]) as conn:
            conn.executemany(f'INSERT INTO "{table("chat_a")}" VALUES (?,?,?,?,?,?,?,?)',
                             [(3 if n % 2 else 1, 100 + n, 2, f"payload {n}", 0,
                               n + 1, n + 1, n + 1) for n in range(6002)])
        result = self.reader().prepare_export("chat_a", 100, 6101, self.root / "export")
        rows = Path(result["messages_file"]).read_text(encoding="utf-8").splitlines()
        self.assertEqual(result["total_messages"], 6001)
        self.assertEqual(len(rows), 6001)
        self.assertEqual(json.loads(rows[-1])["timestamp"], 6100)

    def test_export_optional_packed_locators_and_compressed_fallback(self):
        import zstandard
        self.insert(kind=3, content=None)
        with database(self.shards[0]) as conn:
            conn.execute(f'ALTER TABLE "{table("chat_a")}" ADD COLUMN packed_info_data BLOB')
            conn.execute(f'ALTER TABLE "{table("chat_a")}" ADD COLUMN compress_content BLOB')
            conn.execute(f'UPDATE "{table("chat_a")}" SET packed_info_data=?, compress_content=?',
                         ((b"localfile=" + b"a" * 32 + b"_h.dat\x00aeskey=" + b"b" * 32),
                          zstandard.ZstdCompressor().compress(b'<msg><img md5="' + b"a" * 32 + b'" /></msg>')))
        result = self.reader().prepare_export("chat_a", 0, 200, self.root / "export")
        raw = Path(result["messages_file"]).read_text(encoding="utf-8")
        row = json.loads(raw)
        self.assertEqual(row["payload_source"], "compress_content")
        self.assertIn({"type": "filename", "value": "a" * 32 + "_h.dat"}, row["attachment_hints"])
        self.assertNotIn("b" * 32, raw)
        self.assertNotIn("packed_info_data", raw)

    def test_empty_nontext_payload_is_not_called_complete(self):
        self.insert(kind=3, content=None)
        result = self.reader().prepare_export("chat_a", 0, 200, self.root / "export")
        row = json.loads(Path(result["messages_file"]).read_text(encoding="utf-8"))
        self.assertEqual(row["payload_status"], "empty_nontext_payload")
        self.assertEqual(row["media_status"], "pending")


class ProviderTests(unittest.TestCase):
    def test_explicit_provider_cache_refresh_and_private_logging(self):
        with tempfile.TemporaryDirectory(prefix="wechat-provider-synthetic-") as folder:
            root = Path(folder).resolve()
            vendor = root / "independent-provider"
            vendor.mkdir()
            (vendor / "wechat_access.py").write_text(
                "print('synthetic provider import log')\n"
                "import pathlib\n"
                "def load_access(): return {'account_wxid': 'synthetic_self'}\n"
                "def find_databases(category, pattern, access):\n"
                " assert category=='media' and pattern=='media_*.db'\n"
                " return [pathlib.Path(__file__).parent / 'media_0.db']\n"
                "def database_root(access): return pathlib.Path(__file__).parent / 'account' / 'db_storage'\n",
                encoding="utf-8")
            (vendor / "query_wechat.py").write_text(
                "import wechat_access, pathlib, sqlite3\n"
                "CACHE_DIR='unused'\n_msg_dbs=['stale']\n_display_cache={'stale':'stale'}\n"
                "def get_msg_dbs():\n"
                " assert _msg_dbs is None\n"
                " print('synthetic snapshot log')\n"
                " p=pathlib.Path(CACHE_DIR)/'message_0.db'; p.touch(); return [str(p)]\n"
                "def decrypt_db(name, key):\n"
                " p=pathlib.Path(CACHE_DIR)/(name+'.db')\n"
                " if name=='contact':\n"
                "  with sqlite3.connect(p) as c: c.execute('CREATE TABLE IF NOT EXISTS contact (username, remark, nick_name)')\n"
                "  c.close()\n"
                " else: p.touch()\n"
                " return str(p)\n"
                "def decrypt_source(source, target):\n"
                " p=pathlib.Path(target); p.touch(); return str(p)\n"
                "def load_display():\n"
                " assert _display_cache is None\n"
                " return {'chat_a': 'synthetic name'}\n", encoding="utf-8")
            provider = WechatProvider(vendor, root / "private" / "cache")
            for _ in range(2):
                snapshot = provider.snapshot()
                self.assertEqual(snapshot.my_account_id, "synthetic_self")
                self.assertEqual(snapshot.session_path.parent, root / "private" / "cache")
                self.assertEqual(snapshot.display_names, {"chat_a": "synthetic name"})
            self.assertEqual(provider.log_path.read_text(encoding="utf-8").count("synthetic snapshot log"), 2)
            self.assertEqual(provider.voice_snapshot(), [root / "private" / "cache" / "media_0.db"])
            self.assertEqual(provider.account_data_root(), vendor / "account")

    def test_missing_provider_fails_without_hook(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            with self.assertRaises(HistoryError):
                WechatProvider(root / "absent", root / "private" / "cache").snapshot()


if __name__ == "__main__":
    unittest.main()
