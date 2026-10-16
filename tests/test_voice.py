"""Synthetic VoiceInfo matching and decoder isolation; never read real media."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wechat_local import voice


SILK = b"\x02#!SILK_V3" + b"synthetic codec payload"


class VoiceTests(unittest.TestCase):
    def setUp(self):
        temporary = Path(__file__).resolve().parents[1] / "tmp"
        temporary.mkdir(exist_ok=True)
        self.temp = TemporaryDirectory(dir=temporary)
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.received = []
        def decode(source, output, sample_rate):
            self.received.append(source.read())
            self.assertEqual(sample_rate, 24000)
            output.write(b"\x00\x00" * 2400)
        self.decoder = SimpleNamespace(decode=decode)

    def db(self, rows, name="one.db"):
        path = self.root / name
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.executemany("INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)", [(1, "chat-one"), (2, "chat-two")])
            conn.execute("CREATE TABLE VoiceInfo(chat_name_id INTEGER, local_id INTEGER, svr_id INTEGER, create_time INTEGER, voice_data BLOB)")
            conn.executemany("INSERT INTO VoiceInfo VALUES(?,?,?,?,?)", rows)
            conn.commit()
        return path

    def run_asset(self, paths, local=7, server=8, at=9, chat="chat-one"):
        with patch.object(voice.importlib, "import_module", return_value=self.decoder):
            return voice.selected_voice_asset(paths, chat, local, server, at, self.root / "output")

    def assert_error(self, code, paths, **selection):
        with self.assertRaises(voice.VoiceError) as caught:
            self.run_asset(paths, **selection)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_exact_voice_becomes_valid_wav_and_preserves_original(self):
        db = self.db([(1, 7, 8, 9, SILK)])
        result = self.run_asset([db])
        self.assertTrue(result["decoded"])
        self.assertEqual(Path(result["silk_path"]).read_bytes(), SILK)
        self.assertEqual(self.received, [SILK[1:]])
        with wave.open(result["wav_path"], "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 24000))
            self.assertEqual(wav.getnframes(), 2400)
        self.assertEqual(result["duration_seconds"], 0.1)

    def test_chat_local_server_and_time_must_all_match(self):
        path = self.db([(1, 7, 8, 9, SILK), (2, 7, 8, 9, SILK+b"other")])
        for selection in ({"local": 77}, {"server": 88}, {"at": 99}, {"chat": "missing"}):
            self.assert_error("voice_not_downloaded", [path], **selection)
        self.assertEqual(self.received, [])

    def test_missing_server_can_use_unique_chat_local_time(self):
        path = self.db([(1, 7, 8, 9, SILK)])
        self.assertTrue(self.run_asset([path], server=0)["decoded"])

    def test_different_cross_shard_blobs_are_a_conflict(self):
        one = self.db([(1, 7, 8, 9, SILK)])
        two = self.db([(1, 7, 8, 9, SILK+b"different")], "two.db")
        self.assert_error("voice_conflict", [one, two])
        self.assertEqual(self.received, [])

    def test_identical_cross_shard_copy_is_deduplicated(self):
        one = self.db([(1, 7, 8, 9, SILK)])
        two = self.db([(1, 7, 8, 9, SILK)], "two.db")
        self.run_asset([one, two])
        self.assertEqual(len(self.received), 1)

    def test_empty_or_absent_blob_is_not_downloaded(self):
        self.assert_error("voice_not_downloaded", [self.db([(1, 7, 8, 9, None)])])
        self.assert_error("voice_not_downloaded", [])

    def test_readonly_does_not_create_missing_database(self):
        path = self.root / "PRIVATE_SENTINEL.db"
        error = self.assert_error("voice_read_failed", [path])
        self.assertFalse(path.exists())
        self.assertNotIn("PRIVATE_SENTINEL", str(error))

    def test_connection_is_closed_after_success_and_failure(self):
        path = self.db([(1, 7, 8, 9, SILK)])
        real_connect = voice.sqlite3.connect
        connections = []
        def connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            connections.append(conn)
            return conn
        with patch.object(voice.sqlite3, "connect", side_effect=connect):
            self.run_asset([path])
            self.assert_error("voice_not_downloaded", [path], local=77)
        for conn in connections:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_missing_decoder_keeps_original_and_reports_fixed_code(self):
        path = self.db([(1, 7, 8, 9, SILK)])
        with patch.object(voice.importlib, "import_module", side_effect=ImportError("PRIVATE_SENTINEL")):
            with self.assertRaises(voice.VoiceError) as caught:
                voice.selected_voice_asset([path], "chat-one", 7, 8, 9, self.root / "output")
        self.assertEqual(caught.exception.code, "voice_decoder_not_installed")
        self.assertNotIn("PRIVATE_SENTINEL", str(caught.exception))
        self.assertEqual(Path(caught.exception.asset["silk_path"]).read_bytes(), SILK)
        self.assertIsNone(caught.exception.asset["wav_path"])

    def test_decode_errors_are_fixed_codes_not_private_messages(self):
        def fail(*args):
            raise RuntimeError("PRIVATE_DECODER_PATH")
        self.decoder.decode = fail
        error = self.assert_error("voice_decode_failed", [self.db([(1, 7, 8, 9, SILK)])])
        self.assertNotIn("PRIVATE_DECODER_PATH", str(error))
        self.assertTrue(Path(error.asset["silk_path"]).exists())

    def test_invalid_header_never_loads_decoder(self):
        error = self.assert_error("voice_invalid_silk_header", [self.db([(1, 7, 8, 9, b"not silk")])])
        self.assertTrue(error.asset)
        self.assertEqual(self.received, [])

    def test_invalid_pcm_and_identity_are_rejected(self):
        self.decoder.decode = lambda source, output, rate: output.write(b"x")
        path = self.db([(1, 7, 8, 9, SILK)])
        self.assert_error("voice_invalid_pcm", [path])
        self.assert_error("voice_invalid_selection", [path], local=True)


if __name__ == "__main__":
    unittest.main()
