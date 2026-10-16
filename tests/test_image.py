"""Synthetic image bytes and attachment paths only."""

from datetime import datetime, timezone, timedelta
import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import struct
import types
import unittest
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wechat_local.image import ImageError, selected_image_asset, V2


class ImageTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        base = Path(__file__).resolve().parents[1] / "tmp" / "image-tests"
        base.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=base, prefix="synthetic-")
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name).resolve()
        self.account = self.root / "account"
        self.account.mkdir()
        chat = "synthetic_chat"
        self.message = {"id": "synthetic-image", "chat_id": chat,
                        "timestamp": int(datetime(2026, 10, 9, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()),
                        "attachment_hints": [{"type": "hash", "value": "a" * 32}]}
        self.directory = self.account / "msg" / "attach" / hashlib.md5(chat.encode()).hexdigest() / "2026-10" / "Img"
        self.directory.mkdir(parents=True)
        self.output = self.root / "export"
        buffer = BytesIO()
        Image.new("RGB", (3, 2), (42, 99, 128)).save(buffer, format="PNG")
        self.png = buffer.getvalue()

    def asset(self, suffix=".dat", data=None):
        path = self.directory / ("a" * 32 + suffix)
        path.write_bytes(self.png if data is None else data)
        return path

    def test_plain_image_verified_and_unchanged(self):
        source = self.asset()
        assets = selected_image_asset(self.account, self.message, self.output)
        self.assertEqual(len(assets), 1)
        self.assertEqual(assets[0]["kind"], "image")
        self.assertEqual(assets[0]["quality"], "unknown")
        self.assertEqual(Path(assets[0]["path"]).read_bytes(), self.png)
        self.assertEqual(source.read_bytes(), self.png)

    def test_legacy_xor_and_encrypted_source(self):
        encrypted = bytes(byte ^ 0x35 for byte in self.png)
        self.asset("_h.dat", encrypted)
        assets = selected_image_asset(self.account, self.message, self.output)
        self.assertEqual(assets[0]["quality"], "original")
        self.assertEqual(Path(assets[0]["path"]).read_bytes(), self.png)
        self.assertEqual(assets[1]["kind"], "image_encrypted")
        self.assertEqual(Path(assets[1]["path"]).read_bytes(), encrypted)

    def test_thumbnail_is_never_original(self):
        self.asset("_t.dat")
        assets = selected_image_asset(self.account, self.message, self.output)
        self.assertEqual(assets[0]["quality"], "thumbnail")

    def test_wxgf_embedded_png(self):
        self.asset(data=b"wxgf" + b"\x00" * 16 + self.png + b"container tail")
        assets = selected_image_asset(self.account, self.message, self.output)
        self.assertEqual(Path(assets[0]["path"]).read_bytes(), self.png)
        self.assertEqual(assets[1]["kind"], "image_container")

    def test_wxgf_embedded_jpeg(self):
        from PIL import Image
        buffer = BytesIO()
        Image.new("RGB", (4, 3), (55, 66, 77)).save(buffer, format="JPEG")
        data = buffer.getvalue()
        self.asset(data=b"wxgf" + b"\x00" * 16 + data)
        assets = selected_image_asset(self.account, self.message, self.output)
        self.assertEqual(Path(assets[0]["path"]).read_bytes(), data)

    def test_wxgf_without_embedded_image_requires_conversion(self):
        self.asset(data=b"wxgf" + b"\x00" * 30)
        with self.assertRaises(ImageError) as error:
            selected_image_asset(self.account, self.message, self.output)
        self.assertEqual(error.exception.code, "wxgf_conversion_required")
        self.assertEqual(error.exception.assets[0]["kind"], "image_container")

    def test_v2_needs_key_and_preserves_encrypted_original(self):
        encrypted = V2 + b"\x00" * 30
        self.asset(data=encrypted)
        with self.assertRaises(ImageError) as error:
            selected_image_asset(self.account, self.message, self.output)
        self.assertEqual(error.exception.code, "image_key_required")
        self.assertEqual(Path(error.exception.assets[0]["path"]).read_bytes(), encrypted)
        self.assertEqual(error.exception.assets[0]["kind"], "image_encrypted")

    def encrypted_v2(self,plain,key=b'0123456789abcdef',xor=73):
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad
        head=32;tail=8
        return V2+struct.pack('<II',head,tail)+b'\x01'+AES.new(key,AES.MODE_ECB).encrypt(pad(plain[:head],16))+plain[head:-tail]+bytes(b^xor for b in plain[-tail:])

    def test_v2_recovers_exact_png_and_preserves_encrypted_source(self):
        encrypted=self.encrypted_v2(self.png)
        source=self.asset('_h.dat',encrypted)
        assets=selected_image_asset(self.account,self.message,self.output,image_keys={'aes':b'0123456789abcdef','xor':None})
        self.assertEqual(Path(assets[0]['path']).read_bytes(),self.png)
        self.assertEqual(assets[0]['quality'],'original')
        self.assertEqual(Path(assets[1]['path']).read_bytes(),encrypted)
        self.assertEqual(source.read_bytes(),encrypted)

    def test_v2_recovers_jpeg_without_guessing_pixel_content(self):
        from PIL import Image
        buffer=BytesIO();Image.new('RGB',(24,16),(37,93,127)).save(buffer,format='JPEG')
        jpeg=buffer.getvalue();self.asset('_h.dat',self.encrypted_v2(jpeg))
        assets=selected_image_asset(self.account,self.message,self.output,image_keys={'aes':b'0123456789abcdef','xor':None})
        self.assertEqual(Path(assets[0]['path']).read_bytes(),jpeg)

    def test_v2_image_survives_complete_export_copy_and_move(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import Mock
        from wechat_local.image_keys import ImageKeyStore
        from wechat_local.media import MediaResolver
        from wechat_local.export import write_export
        from wechat_local.bundle import save_export_bundle
        private=self.root/'private';ImageKeyStore(private).save('0123456789abcdef')
        self.asset('_h.dat',self.encrypted_v2(self.png))
        message={**self.message,'id':'a'*64,'kind':'image','sender':'合成发送者','is_self':False,'payload_status':'complete','raw_payload':'<msg><img/></msg>'}
        snapshot=self.root/'snapshot.jsonl';snapshot.write_text(json.dumps(message)+'\n',encoding='utf-8')
        provider=Mock();provider.account_data_root.return_value=self.account
        model=Mock();model.settings=SimpleNamespace(data_dir=private,asr_model_dir=None)
        model.analyze_image.return_value={'status':'complete','description':'虚构图片说明'}
        resolver=MediaResolver(provider,model,self.root/'job')
        manifest={'chat':{'id':message['chat_id'],'name':'合成群'},'start':message['timestamp']-1,'end':message['timestamp']+1,'total_messages':1,'messages_file':str(snapshot)}
        result=write_export(manifest,self.output,resolve_media=resolver.resolve,annotate_media=resolver.annotate)
        self.assertTrue(result['content_complete'])
        target=self.root/'saved';save_export_bundle(self.output,target,owner_file=self.root/'owner.json')
        moved=self.root/'moved';target.rename(moved)
        asset=result['media'][message['id']][0]
        self.assertEqual((moved/asset['relative_path']).read_bytes(),self.png)
        html=(moved/'index.html').read_text(encoding='utf-8')
        self.assertIn('<img loading="lazy"',html)
        self.assertNotIn(str(self.account),html)
        self.assertNotIn('0123456789abcdef',html)

    def test_v2_wrong_key_or_xor_never_produces_an_image(self):
        self.asset(data=self.encrypted_v2(self.png))
        for keys,code in (({'aes':b'fedcba9876543210','xor':73},'image_key_invalid'),
                          ({'aes':b'0123456789abcdef','xor':12},'image_decode_invalid')):
            with self.assertRaises(ImageError) as caught:
                selected_image_asset(self.account,self.message,self.output,image_keys=keys)
            self.assertEqual(caught.exception.code,code)
            self.assertEqual(caught.exception.assets[0]['kind'],'image_encrypted')
        self.assertFalse(list(self.output.glob('*.png')))

    def test_v2_malformed_lengths_are_rejected_without_large_allocations(self):
        self.asset(data=V2+struct.pack('<II',0xffffffff,0xffffffff)+b'\x01'+b'0'*32)
        with self.assertRaises(ImageError) as caught:
            selected_image_asset(self.account,self.message,self.output,image_keys={'aes':b'0123456789abcdef','xor':0})
        self.assertEqual(caught.exception.code,'image_header_invalid')

    def test_image_key_store_encrypts_and_rejects_bad_input_without_overwrite(self):
        from wechat_local.image_keys import ImageKeyStore
        store=ImageKeyStore(self.root/'private')
        self.assertIsNone(store.load())
        store.save('0123456789abcdef',73)
        saved=store.path.read_bytes()
        self.assertNotIn(b'0123456789abcdef',saved)
        self.assertEqual(store.load(),{'aes':b'0123456789abcdef','xor':73})
        for value,xor in (('too-short',0),('0123456789abcdef',256),('0123456789abcdef',True)):
            with self.assertRaises(ValueError):store.save(value,xor)
            self.assertEqual(store.path.read_bytes(),saved)

    def test_path_traversal_and_account_output_are_rejected(self):
        self.asset()
        self.message["attachment_hints"] = [{"type": "filename", "value": "../../outside.dat"}]
        with self.assertRaises(ImageError) as error:
            selected_image_asset(self.account, self.message, self.output)
        self.assertEqual(error.exception.code, "unsafe_asset_path")
        self.message["attachment_hints"] = [{"type": "hash", "value": "a" * 32}]
        with self.assertRaises(ImageError):
            selected_image_asset(self.account, self.message, self.account / "export")

    def test_no_other_chat_or_month_scan(self):
        source = self.asset()
        other = self.account / "msg" / "attach" / ("b" * 32) / "2026-10" / "Img"
        other.mkdir(parents=True)
        source.rename(other / ("a" * 32 + ".dat"))
        with self.assertRaises(ImageError) as error:
            selected_image_asset(self.account, self.message, self.output)
        self.assertEqual(error.exception.code, "media_not_found")

    def test_conflicting_exact_sources_not_guessed(self):
        self.asset(".dat")
        self.asset(".png", self.png + b"different")
        with self.assertRaises(ImageError) as error:
            selected_image_asset(self.account, self.message, self.output)
        self.assertEqual(error.exception.code, "image_conflict")

    def test_reparse_detection_rejects_before_read(self):
        source = self.asset()
        original = Path.lstat
        def stat(path, *args, **kwargs):
            if path == source:
                return types.SimpleNamespace(st_file_attributes=0x400, st_mode=original(path).st_mode)
            return original(path, *args, **kwargs)
        with patch.object(Path, "lstat", stat), self.assertRaises(ImageError) as error:
            selected_image_asset(self.account, self.message, self.output)
        self.assertEqual(error.exception.code, "unsafe_asset_path")


if __name__ == "__main__":
    unittest.main()
