"""Synthetic export tests; never open a real provider, chat, or model."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wechat_local.export import ExportCancelled, ExportError, write_export


class ExportTests(unittest.TestCase):
    def setUp(self):
        temp_root = Path(__file__).resolve().parents[1] / "tmp"
        temp_root.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="export-test-", dir=temp_root)
        self.root = Path(self.tmp.name)
        self.source = self.root / "snapshot.jsonl"
        self.manifest = {"chat": {"id": "synthetic-room", "name": "示例群", "is_group": True},
                         "start": 100, "end": 200, "total_messages": 1, "messages_file": str(self.source)}

    def tearDown(self):
        self.tmp.cleanup()

    def record(self, number=1, kind="text", **extra):
        return {"id": f"synthetic-{number}", "chat_id": "synthetic-room", "timestamp": 100 + number,
                "sender": "虚构成员", "kind": kind, "type": 1, "original_type": 1,
                "text": "明天整理测试资料。" if kind == "text" else "", "payload_status": "complete", **extra}

    def export(self, records, **kwargs):
        self.source.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
        self.manifest["total_messages"] = len(records)
        self.output = self.root / "bundle"
        return write_export(self.manifest, self.output, **kwargs)

    def test_text_originals_and_views(self):
        source = self.record(text="原文\n第二行", is_self=False)
        result = self.export([source])
        self.assertTrue(result["event_coverage_complete"])
        self.assertTrue(result["content_complete"])
        actual = json.loads((self.output / "messages.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(actual, source)
        self.assertEqual((self.output / "annotations.jsonl").read_text(encoding="utf-8"), "")
        for name in ("index.html", "records.md", "manifest.json", "AGENT-README.md"):
            self.assertTrue((self.output / name).is_file())

    def test_media_and_annotations_separate_with_hash(self):
        image = self.root / "example.png"
        image.write_bytes(b"synthetic-image-bytes")
        source = self.record(kind="image", raw_payload="<msg><img /></msg>")
        def annotate(message, assets):
            self.assertEqual(Path(assets[0]["path"]).read_bytes(), image.read_bytes())
            return {"status": "complete", "description": "合成图片注释", "human_verified": True}
        result = self.export([source], resolve_media=lambda message: [{"path": str(image), "name": "示例图片", "kind": "image", "quality": "original"}],
                             annotate_media=annotate)
        self.assertTrue(result["content_complete"])
        annotation = json.loads((self.output / "annotations.jsonl").read_text(encoding="utf-8"))
        self.assertTrue(annotation["derived"])
        self.assertFalse(annotation["human_verified"])
        original = json.loads((self.output / "messages.jsonl").read_text(encoding="utf-8"))
        self.assertNotIn("description", original)
        asset = result["media"][source["id"]][0]
        self.assertNotIn("path", asset)
        self.assertEqual(asset["sha256"], hashlib.sha256(image.read_bytes()).hexdigest())
        self.assertEqual((self.output / asset["relative_path"]).read_bytes(), image.read_bytes())
        html=(self.output/'index.html').read_text(encoding='utf-8')
        self.assertIn('title="打开原尺寸图片"',html)
        self.assertIn('<img loading="lazy"',html)

    def test_missing_image_key_has_actionable_reading_notice_and_preserves_ciphertext(self):
        from wechat_local.image import ImageError
        encrypted=self.root/'cipher.dat';encrypted.write_bytes(b'synthetic-ciphertext')
        def resolve(message):
            raise ImageError('image_key_required','safe controlled message',assets=[{'path':str(encrypted),'kind':'image_encrypted','quality':'thumbnail'}])
        result=self.export([self.record(kind='image')],resolve_media=resolve)
        html=(self.output/'index.html').read_text(encoding='utf-8')
        self.assertIn('图片解码',html)
        self.assertNotIn('<img ',html)
        self.assertFalse(result['content_complete'])
        self.assertEqual(result['media_errors'][0]['error_code'],'image_key_required')

    def test_missing_media_is_partial(self):
        result = self.export([self.record(kind="voice")])
        self.assertTrue(result["event_coverage_complete"])
        self.assertFalse(result["content_complete"])
        self.assertEqual(result["annotation_counts"]["missing"], 1)
        self.assertIn("媒体原件缺失", (self.output / "index.html").read_text(encoding="utf-8"))

    def test_markdown_links_and_readable_media_annotations(self):
        image=self.root/"example.png"
        image.write_bytes(b"synthetic-image")
        voice=self.root/"example.wav"
        voice.write_bytes(b"synthetic-audio")
        records=[self.record(1,kind="image",raw_payload="<msg>原始图片描述</msg>"),
                 self.record(2,kind="voice",raw_payload="<msg>原始语音描述</msg>")]
        result=self.export(records,resolve_media=lambda row:[{"path":str(image if row["kind"]=="image" else voice),
            "name":"附件 [合成]","kind":row["kind"],"quality":"original"}],
            annotate_media=lambda row,assets:{"status":"complete","ocr_text":"合成图片文字","description":"会议白板"}
                if row["kind"]=="image" else {"status":"complete","transcript":"合成语音转写","raw_model_output":"model metadata"})
        md=(self.output/"records.md").read_text(encoding="utf-8")
        html=(self.output/"index.html").read_text(encoding="utf-8")
        self.assertIn("[打开多媒体阅读页](index.html)",md)
        first=result["media"][records[0]["id"]][0]["relative_path"]
        self.assertIn("![附件 \\[合成\\]]("+first+")",md)
        self.assertIn("合成语音转写",md)
        self.assertIn("<audio controls",html)
        self.assertNotIn("autoplay",html)
        self.assertIn("<summary>原始媒体描述</summary>",html)
        self.assertIn("未经人工核验",html)

    def test_no_speech_decision_is_readable_and_retains_original_audio(self):
        voice=self.root/"silent.wav"
        voice.write_bytes(b"synthetic-silent-audio")
        result=self.export([self.record(kind="voice")],resolve_media=lambda _:[{"path":str(voice),"kind":"voice_audio"}],
            annotate_media=lambda *_:{"status":"complete","speech_detected":False,"raw_model_output":None})
        self.assertTrue(result["content_complete"])
        for filename in ("records.md","index.html"):
            self.assertIn("原语音仍可回听",(self.output/filename).read_text(encoding="utf-8"))

    def test_failed_annotation_is_partial(self):
        voice = self.root / "voice.wav"
        voice.write_bytes(b"synthetic-voice")
        result = self.export([self.record(kind="voice")],
                             resolve_media=lambda message: [{"path": str(voice)}],
                             annotate_media=lambda *_: {"status": "error"})
        self.assertFalse(result["content_complete"])
        self.assertEqual(result["annotation_counts"]["error"], 1)

    def test_annotation_id_mismatch_fails(self):
        image = self.root / "image.png"
        image.write_bytes(b"synthetic")
        result = self.export([self.record(kind="image")],
                             resolve_media=lambda _: [{"path": str(image)}],
                             annotate_media=lambda *_: {"message_id": "other", "status": "complete", "text": "x"})
        self.assertFalse(result["content_complete"])

    def test_unknown_and_opaque_payload_are_partial(self):
        result = self.export([self.record(kind="unknown", payload_status="opaque_binary_not_exported",
                                         binary_size=17, binary_sha256="synthetic-placeholder")])
        self.assertTrue(result["event_coverage_complete"])
        self.assertFalse(result["content_complete"])
        self.assertEqual(result["payload_gaps"], ["synthetic-1"])

    def test_html_xml_script_and_markdown_are_inert(self):
        text = '<script>window.EVIL=1</script>\n```\n![fetch](https://example.invalid/a.png)'
        self.export([self.record(text=text, sender="<img src=x onerror=evil()>")])
        html = (self.output / "index.html").read_text(encoding="utf-8")
        md = (self.output / "records.md").read_text(encoding="utf-8")
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("````text\n" + text + "\n````", md)

    def test_sender_and_chat_markdown_images_are_inert(self):
        attack = "![synthetic](https://example.invalid/pixel)"
        self.manifest["chat"]["name"] = attack
        self.export([self.record(sender=attack)])
        md = (self.output / "records.md").read_text(encoding="utf-8")
        self.assertNotIn(attack, md)
        self.assertIn(r"\!\[synthetic\]\(https://example\.invalid/pixel\)", md)

    def test_unsafe_attachment_suffix_and_filename(self):
        attachment = self.root / "page.html"
        attachment.write_text("<script>evil()</script>", encoding="utf-8")
        result = self.export([self.record(kind="file")],
                             resolve_media=lambda _: [{"path": str(attachment), "name": '../../<script>x</script>'}])
        asset = result["media"]["synthetic-1"][0]
        self.assertTrue(asset["relative_path"].endswith(".bin"))
        self.assertEqual(len(list((self.output / "media").iterdir())), 1)
        self.assertNotIn("<script>", (self.output / "index.html").read_text(encoding="utf-8"))

    def test_resolver_url_cannot_download(self):
        result = self.export([self.record(kind="image", raw_payload="https://example.invalid/private")],
                             resolve_media=lambda _: [{"path": "https://example.invalid/private"}])
        self.assertFalse(result["content_complete"])
        self.assertEqual(result["media_errors"][0]["stage"], "media-resolution")

    def test_selection_mismatch_rejected(self):
        with self.assertRaises(ExportError):
            self.export([self.record(chat_id="unselected-chat")])
        result = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(result["event_coverage_complete"])

    def test_duplicate_and_boundary_records_rejected(self):
        with self.assertRaises(ExportError):
            self.export([self.record(timestamp=200)])
        # A distinct output path is needed; existing output is never overwritten.
        self.source.write_text("\n".join(json.dumps(self.record()) for _ in range(2)) + "\n", encoding="utf-8")
        self.manifest["total_messages"] = 2
        with self.assertRaises(ExportError):
            write_export(self.manifest, self.root / "duplicate-bundle")

    def test_count_mismatch_and_existing_directory(self):
        self.source.write_text(json.dumps(self.record()) + "\n", encoding="utf-8")
        self.manifest["total_messages"] = 2
        with self.assertRaises(ExportError):
            write_export(self.manifest, self.root / "mismatch")
        existing = self.root / "existing"
        existing.mkdir()
        marker = existing / "preserved.txt"
        marker.write_text("do not change", encoding="utf-8")
        with self.assertRaises(ExportError):
            write_export(self.manifest, existing)
        self.assertEqual(marker.read_text(encoding="utf-8"), "do not change")

    def test_cancel_has_partial_marker(self):
        with self.assertRaises(ExportCancelled):
            self.export([self.record()], cancelled=lambda: True)
        result = json.loads((self.output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "cancelled")
        self.assertFalse(result["content_complete"])

    def test_more_than_5000_records_not_truncated(self):
        self.manifest["end"] = 6000
        result = self.export([self.record(number) for number in range(1, 5002)])
        self.assertEqual(result["total_messages"], 5001)
        self.assertEqual(len((self.output / "messages.jsonl").read_text(encoding="utf-8").splitlines()), 5001)

    def test_resolver_failures_do_not_leak_exception_detail(self):
        def broken(_):
            raise RuntimeError("SYNTHETIC_PRIVATE_ERROR_SENTINEL")
        self.export([self.record(kind="image")], resolve_media=broken)
        self.assertNotIn("SYNTHETIC_PRIVATE_ERROR_SENTINEL", (self.output / "manifest.json").read_text(encoding="utf-8"))

    def test_error_assets_are_preserved_with_safe_code(self):
        raw = self.root / "original.dat"
        raw.write_bytes(b"synthetic-encrypted-original")
        class Recoverable(Exception):
            code = "image_key_required"
            assets = [{"path": str(raw), "kind": "image_encrypted", "quality": "original"}]
        def recover(_):
            raise Recoverable("SYNTHETIC_PRIVATE_ERROR_SENTINEL")
        result = self.export([self.record(kind="image")], resolve_media=recover,
                             annotate_media=lambda *_: {"status": "error", "error_code": "decoded_image_missing"})
        self.assertEqual(result["media_errors"][0]["error_code"], "image_key_required")
        asset = result["media"]["synthetic-1"][0]
        self.assertEqual((self.output / asset["relative_path"]).read_bytes(), raw.read_bytes())
        self.assertFalse(result["content_complete"])
        annotation = json.loads((self.output / "annotations.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(annotation["data"]["error_code"], "decoded_image_missing")

    def test_error_codes_are_strict_and_not_exception_text(self):
        class Unsafe(Exception):
            code = "SYNTHETIC-PRIVATE.invalid/url"
        def resolver(_):
            raise Unsafe("sensitive synthetic diagnostic")
        result = self.export([self.record(kind="image")], resolve_media=resolver)
        self.assertEqual(result["media_errors"][0]["error_code"], "media_operation_failed")

    def test_thumbnail_is_partial_even_with_complete_annotation(self):
        image = self.root / "thumbnail.png"
        image.write_bytes(b"synthetic")
        result = self.export([self.record(kind="image")], resolve_media=lambda _: [{"path": str(image), "kind": "image", "quality": "thumbnail"}],
                             annotate_media=lambda *_: {"status": "complete", "description": "示例注释"})
        self.assertTrue(result["event_coverage_complete"])
        self.assertFalse(result["content_complete"])
        self.assertEqual(result["media_reduced_quality"], ["synthetic-1"])

    def test_progress_contains_counts_only(self):
        events = []
        self.export([self.record(1), self.record(2)], progress=events.append)
        self.assertEqual(events[-1], {"current": 2, "total": 2})
        self.assertTrue(all(set(event) == {"current", "total"} for event in events))

    def test_empty_payload_is_partial_even_with_asset(self):
        image = self.root / "image.png"
        image.write_bytes(b"synthetic")
        result = self.export([self.record(kind="image", payload_status="empty_nontext_payload")],
                             resolve_media=lambda _: [{"path": str(image), "kind": "image", "quality": "original"}],
                             annotate_media=lambda *_: {"status": "complete", "description": "示例注释"})
        self.assertFalse(result["content_complete"])
        self.assertEqual(result["payload_gaps"], ["synthetic-1"])


if __name__ == "__main__":
    unittest.main()
