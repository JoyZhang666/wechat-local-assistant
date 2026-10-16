"""Synthetic encrypted SQLite fixtures. Never reads installed WeChat data."""
import hashlib
from contextlib import closing
import hmac
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from Crypto.Cipher import AES
from wechat_local.builtin_provider import BuiltinProvider, plain
from wechat_local.database_crypto import decrypt_pages
from wechat_local.history import HistoryError
from wechat_local.wal_snapshot import _checksum, merge_committed_pages, read_snapshot

KEY = bytes(range(32))
SALT = bytes(range(16))


def encrypted_database(path, statements):
    temporary = path.with_suffix(".synthetic")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(temporary, isolation_level=None)) as conn:
        conn.execute("PRAGMA page_size=4096")
        for sql in statements:
            conn.execute(sql)
    data = bytearray(temporary.read_bytes())
    temporary.unlink()
    # Tiny leaf-only fixtures: move cells to reserve 80 bytes on each page.
    for offset in range(0, len(data), 4096):
        header = offset + (100 if offset == 0 else 0)
        assert data[header] == 13
        count = struct.unpack_from(">H", data, header + 3)[0]
        start = struct.unpack_from(">H", data, header + 5)[0]
        data[offset + start - 80:offset + 4016] = data[offset + start:offset + 4096]
        data[offset + 4016:offset + 4096] = bytes(80)
        struct.pack_into(">H", data, header + 5, start - 80)
        for i in range(count):
            pointer = header + 8 + i * 2
            struct.pack_into(">H", data, pointer, struct.unpack_from(">H", data, pointer)[0] - 80)
    data[20] = 80
    temporary.write_bytes(data)
    with closing(sqlite3.connect(temporary)) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
    temporary.unlink()
    key = hashlib.pbkdf2_hmac("sha512", KEY, SALT, 256000, 32)
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(v ^ 0x3a for v in SALT), 2, 32)
    output = bytearray()
    for number, offset in enumerate(range(0, len(data), 4096), 1):
        start = 16 if number == 1 else 0
        iv = bytes([number]) * 16
        page = (SALT if number == 1 else b"") + AES.new(key, AES.MODE_CBC, iv).encrypt(bytes(data[offset + start:offset + 4016])) + iv
        page += hmac.new(mac_key, page[start:] + struct.pack("<I", number), hashlib.sha512).digest()
        output.extend(page)
    path.write_bytes(output)
    return bytes(data)


def wal(frames):
    header = struct.pack(">IIII", 0x377f0682, 3007000, 4096, 0) + b"12345678"
    state = _checksum(header, "<")
    output = header + struct.pack(">II", *state)
    for number, commit_size, page in frames:
        frame = struct.pack(">II", number, commit_size) + b"12345678"
        state = _checksum(frame[:8] + page, "<", state)
        output += frame + struct.pack(">II", *state) + page
    return output


class BuiltinTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.account = self.root / "account"
        self.storage = self.account / "db_storage"
        self.contact = self.storage / "contact/contact.db"
        self.expected = encrypted_database(self.contact, ["CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT)",
            "INSERT INTO contact VALUES ('chat_a','Synthetic friend','')"])
        encrypted_database(self.storage / "session/session.db", ["CREATE TABLE SessionTable(username TEXT)",
            "INSERT INTO SessionTable VALUES ('chat_a')"])
        encrypted_database(self.storage / "message/message_0.db", ["CREATE TABLE Name2Id(user_name TEXT)"])
        self.provider = BuiltinProvider(self.root / "private", self.root / "cache")

    def authorize(self):
        self.provider.import_key(self.account, "self_synthetic", KEY.hex(), consent=True)

    def test_authentication_round_trip_and_cached_snapshot(self):
        self.authorize()
        first = self.provider.snapshot()
        self.assertEqual(first.my_account_id, "self_synthetic")
        self.assertEqual(first.display_names["chat_a"], "Synthetic friend")
        self.assertEqual(self.provider.account_data_root(), self.account)
        stamp = first.message_paths[0].stat().st_mtime_ns
        self.assertEqual(self.provider.snapshot().message_paths[0].stat().st_mtime_ns, stamp)
        self.assertEqual(self.provider.voice_snapshot(), [])
        self.assertNotIn(KEY.hex(), self.provider.authorization.read_text())

    def test_wrong_key_and_corrupt_later_page_keep_old_output(self):
        out = self.root / "output.db"
        decrypt_pages(KEY, self.contact.read_bytes(), out)
        before = out.read_bytes()
        for offset in range(0,len(before),4096):
            self.assertEqual(before[offset:offset+4016], self.expected[offset:offset+4016])
        for key, data in ((bytes(32), self.contact.read_bytes()),
                          (KEY, self.contact.read_bytes()[:-1] + bytes([self.contact.read_bytes()[-1] ^ 1]))):
            with self.assertRaises(ValueError):
                decrypt_pages(key, data, out)
            self.assertEqual(out.read_bytes(), before)

    def test_import_failure_preserves_authorization(self):
        self.authorize()
        before = self.provider.authorization.read_bytes()
        for key, consent in ((KEY.hex(), False), ("0" * 64, True), ("", True)):
            with self.assertRaises(ValueError):
                self.provider.import_key(self.account, "self_synthetic", key, consent=consent)
            self.assertEqual(before, self.provider.authorization.read_bytes())

    def test_cache_corruption_rebuilt_source_corruption_fails_closed(self):
        self.authorize()
        snapshot = self.provider.snapshot()
        snapshot.message_paths[0].write_bytes(b"corrupt cache")
        self.assertTrue(self.provider.snapshot().message_paths[0].read_bytes().startswith(b"SQLite format 3\0"))
        source = self.storage / "message/message_0.db"
        source.write_bytes(source.read_bytes()[:-1] + b"!")
        with self.assertRaises(HistoryError):
            self.provider.snapshot()

    def test_account_isolation_and_path_escape(self):
        self.authorize()
        first = self.provider.snapshot().message_paths[0]
        self.provider.import_key(self.account, "other_synthetic", KEY.hex(), consent=True)
        second = self.provider.snapshot().message_paths[0]
        self.assertNotEqual(first.parent.parent, second.parent.parent)
        root, _, key, identity = self.provider._access()
        with self.assertRaises(ValueError):
            self.provider._decrypt(root, key, identity, Path("../../outside.db"))

    def test_wal_committed_only_and_bad_checksum(self):
        main, committed, pending = b"a" * 4096, b"b" * 4096, b"c" * 4096
        self.assertEqual(merge_committed_pages(main, wal([(1, 1, committed), (1, 0, pending)])), committed)
        self.assertEqual(merge_committed_pages(main, wal([(1, 0, pending)])), main)
        self.assertEqual(merge_committed_pages(main, wal([(1, 1, committed)]) + b"incomplete"), committed)
        broken = bytearray(wal([(1, 1, committed)]))
        broken[-1] ^= 1
        with self.assertRaises(ValueError):
            merge_committed_pages(main, broken)
        with self.assertRaises(ValueError):
            merge_committed_pages(main, wal([(2, 3, committed)]))

    def test_changing_files_never_fall_back(self):
        with patch("wechat_local.wal_snapshot._metadata", side_effect=range(12)):
            with self.assertRaisesRegex(ValueError, "changed"):
                read_snapshot(str(self.contact))

    def test_atomic_publish_failure_preserves_previous_cache(self):
        output=self.root/"old.db"
        output.write_bytes(b"prior-cache")
        with patch("wechat_local.database_crypto.os.replace",side_effect=OSError("synthetic")):
            with self.assertRaises(OSError):
                decrypt_pages(KEY,self.contact.read_bytes(),output)
        self.assertEqual(output.read_bytes(),b"prior-cache")
        self.assertEqual(list(self.root.glob("old.db.*")),[])

    def test_wal_growth_truncate_and_old_generation(self):
        first,second=b"a"*4096,b"b"*4096
        self.assertEqual(merge_committed_pages(first,wal([(2,2,second)])),first+second)
        old=wal([(1,1,second)])
        self.assertEqual(merge_committed_pages(first,old+struct.pack(">II",1,1)+b"old-salt"+bytes(8)+first),second)
        with self.assertRaises(ValueError):
            merge_committed_pages(first+second,wal([(1,1,first),(3,3,second)]))

    def test_junction_rejected(self):
        info = self.root.stat()
        with patch("wechat_local.builtin_provider.Path.lstat") as mocked:
            mocked.return_value.st_mode = info.st_mode
            mocked.return_value.st_file_attributes = 0x400
            with self.assertRaisesRegex(ValueError, "link"):
                plain(self.account)


if __name__ == "__main__":
    unittest.main()
