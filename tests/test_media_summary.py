"""Synthetic record/annotation joins and portable summaries; no real accounts."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local.common import atomic_json,read_json
from wechat_local.export import write_export
from wechat_local.media_summary import summary_input,attach_records,recognize_selection
from wechat_local.settings import Settings
from wechat_local.summary import summarize_selection
from wechat_local.summary_reading import ensure_reading,save_summary
from wechat_local.worker import execute
from test_selected_summary import FakeInfer


class MediaSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary=Path(__file__).resolve().parents[1]/"tmp"
        temporary.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix="media-summary-test-",dir=temporary)
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.asset=self.root/"image.png"
        self.asset.write_bytes(b"invented-image-not-real-media")
        self.rows=[{"id":hashlib.sha256(str(i).encode()).hexdigest(),"chat_id":"fake-room","timestamp":100+i,
                    "sender":"虚构甲","is_self":False,"kind":kind,"type":1,"text":"预算300元。" if kind=="text" else "",
                    "raw_payload":"合成载荷","payload_status":"complete"}
                   for i,kind in enumerate(("text","image","voice","video"),1)]
        self.source=self.root/"records.jsonl"
        self.source.write_text("".join(json.dumps(r,ensure_ascii=False)+"\n" for r in self.rows),encoding="utf-8")
        self.frozen={"chat":{"id":"fake-room","name":"虚构群","text_count":1,"skipped_count":3},
                     "start":100,"end":200,"total_messages":4,"messages_file":str(self.source)}
        self.settings=Settings(*(self.root for _ in range(4)),model="synthetic")

    def archive(self):
        directory=self.root/"archive"
        def annotate(message,assets):
            return {"status":"complete","ocr_text":"截止改为周五。","description":"合成会议安排"} if message["kind"]=="image" else {"status":"complete","transcript":"采用方案A，取消方案B。"}
        write_export(self.frozen,directory,resolve_media=lambda _: [{"path":str(self.asset),"kind":"image","quality":"original"}],annotate_media=annotate)
        return directory

    def test_join_keeps_identity_and_marks_unrecognized_and_derived_sources(self):
        archive=self.archive()
        original=(archive/"messages.jsonl").read_bytes()
        manifest=summary_input([archive],self.root/"summary.jsonl")
        rows=[json.loads(line) for line in Path(manifest["messages_file"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual([r["id"] for r in rows],[r["id"] for r in self.rows])
        self.assertEqual([r["timestamp"] for r in rows],[r["timestamp"] for r in self.rows])
        self.assertEqual(rows[0]["evidence_origin"],"original-text")
        self.assertIn("截止改为周五",rows[1]["text"])
        self.assertIn("采用方案A",rows[2]["text"])
        self.assertEqual(rows[1]["evidence_origin"],"machine-annotation")
        self.assertEqual(rows[3]["evidence_origin"],"media-status")
        self.assertIn("暂无内容识别能力",rows[3]["text"])
        self.assertFalse(manifest["media_records"][0]["content_complete"])
        self.assertEqual((archive/"messages.jsonl").read_bytes(),original)

    def test_summary_quotes_and_saved_bundle_keep_media_after_move(self):
        archive=self.archive()
        manifest=summary_input([archive],self.root/"summary.jsonl")
        result=summarize_selection(manifest,self.root/"reports",FakeInfer())
        attach_records(result,manifest)
        page=ensure_reading(result["result_file"])
        self.assertIn("records/chat-001/index.html",page.read_text(encoding="utf-8"))
        markdown=Path(result["chats"][0]["file"]).read_text(encoding="utf-8")
        self.assertIn("媒体识别（未经人工核验）",markdown)
        self.assertIn("媒体 3 条",markdown)
        saved=self.root/"saved"
        save_summary(result["result_file"],saved,owner_file=self.root/"owner.json")
        saved.rename(self.root/"moved")
        media=self.root/"moved/records/chat-001/media"
        self.assertTrue(media.is_dir())
        self.assertEqual(next(media.iterdir()).read_bytes(),self.asset.read_bytes())
        facts=read_json(self.root/"moved/chat-001-facts.json")
        self.assertTrue(any(q.get("origin")=="machine-annotation" for x in facts["extracted"] for q in x["quotes"]))

    def test_partial_recognition_does_not_fabricate_missing_content(self):
        archive=self.root/"archive"
        write_export(self.frozen,archive)
        manifest=summary_input([archive],self.root/"summary.jsonl")
        text=Path(manifest["messages_file"]).read_text(encoding="utf-8")
        self.assertIn("原件缺失",text)
        self.assertNotIn("截止改为周五",text)
        self.assertFalse(manifest["media_records"][0]["content_complete"])

    def test_frozen_multi_chat_recognition_never_expands_selection(self):
        resolver=Mock()
        resolver.resolve.return_value=[]
        resolver.annotate.return_value={"status":"error"}
        manifest={"chats":[self.frozen["chat"]],"start":100,"end":200,"messages_file":str(self.source)}
        self.rows.append({**self.rows[0],"chat_id":"not-selected"})
        self.source.write_text("".join(json.dumps(r)+"\n" for r in self.rows),encoding="utf-8")
        with self.assertRaises(ValueError):
            recognize_selection(manifest,self.root/"archives",self.root,resolver,lambda _:None)
        resolver.resolve.assert_not_called()

    def test_export_summary_cloud_denial_happens_before_provider_or_model(self):
        args=SimpleNamespace(command="export",job="a"*32,summarize=True,allow_cloud=False)
        settings=replace(self.settings,model_backend="cloud")
        with patch("wechat_local.provider.WechatProvider") as provider,patch("wechat_local.model.LocalModel") as model:
            with self.assertRaises(ValueError):
                execute(args,settings,lambda _:None)
        provider.assert_not_called()
        model.assert_not_called()

    def test_export_only_never_calls_text_model_and_append_uses_existing_archive(self):
        for append in (False,True):
            with self.subTest(append=append):
                job=self.root/"jobs"/(("b" if append else "c")*32)
                atomic_json(job/"selection.json",{"ids":["fake-room"],"start":100,"end":200,"recognize_images":True,"recognize_voice":True})
                snapshot=job/"snapshot"
                snapshot.mkdir()
                path=snapshot/"records.jsonl"
                path.write_bytes(self.source.read_bytes())
                frozen={**self.frozen,"messages_file":str(path)}
                model=Mock(); model.infer.side_effect=FakeInfer()
                events=[]
                with patch("wechat_local.account_guard.require_account"),patch("wechat_local.provider.WechatProvider"),patch("wechat_local.history.HistoryReader") as reader,\
                     patch("wechat_local.model.LocalModel",return_value=model),patch("wechat_local.media.MediaResolver") as resolver:
                    reader.return_value.prepare_export.return_value=frozen
                    resolver.return_value.resolve.return_value=[]
                    result=execute(SimpleNamespace(command="export",job=job.name,summarize=append,allow_cloud=False),self.settings,events.append)
                self.assertFalse(snapshot.exists())
                self.assertEqual(result["phase"],"complete" if append else "exported")
                self.assertEqual(model.infer.called,append)
                self.assertEqual(reader.return_value.prepare_export.call_count,1)
                self.assertTrue(model.stop.called)
                if append:
                    self.assertTrue(any(e["phase"]=="exported" for e in events))
                    self.assertFalse(result["media_content_complete"])
                    self.assertTrue(read_json(result["result_path"])["media_records"])

    def test_generate_recognizes_frozen_media_before_text_and_closes_components(self):
        job=self.root/"jobs"/("d"*32)
        snapshot=job/"snapshot"
        snapshot.mkdir(parents=True)
        source=snapshot/"records.jsonl"
        source.write_bytes(self.source.read_bytes())
        atomic_json(job/"manifest.json",{"chats":[self.frozen["chat"]],"start":100,"end":200,
                    "messages_file":str(source),"total_text":1,"total_skipped":3,"include_media":True})
        order=[]
        model=Mock()
        infer=FakeInfer()
        def summarize(prompt):
            order.append("summary")
            return infer(prompt)
        model.infer.side_effect=summarize
        with patch("wechat_local.account_guard.require_account"),patch("wechat_local.provider.WechatProvider") as provider,patch("wechat_local.model.LocalModel",return_value=model),\
             patch("wechat_local.media.MediaResolver") as resolver:
            resolver.return_value.resolve.return_value=[{"path":str(self.asset),"kind":"image","quality":"original"}]
            def recognize(message,assets):
                order.append("recognize")
                return {"status":"complete","transcript":"合成媒体：会议取消。"}
            resolver.return_value.annotate.side_effect=recognize
            result=execute(SimpleNamespace(command="generate",job=job.name,allow_cloud=False),self.settings,lambda _:None)
        self.assertLess(order.index("recognize"),order.index("summary"))
        provider.return_value.snapshot.assert_not_called()
        resolver.return_value.close.assert_called_once()
        self.assertTrue(model.stop.called)
        self.assertFalse(snapshot.exists())
        self.assertTrue(Path(result["result_path"]).parent.joinpath("records/chat-001/media").is_dir())

    def test_raw_export_preserves_assets_without_constructing_any_model(self):
        job=self.root/"jobs"/("e"*32)
        atomic_json(job/"selection.json",{"ids":["fake-room"],"start":100,"end":200,
                    "recognize_images":False,"recognize_voice":False})
        snapshot=job/"snapshot"
        snapshot.mkdir()
        path=snapshot/"records.jsonl"
        path.write_bytes(self.source.read_bytes())
        frozen={**self.frozen,"messages_file":str(path)}
        with patch("wechat_local.account_guard.require_account"),patch("wechat_local.provider.WechatProvider"),\
             patch("wechat_local.history.HistoryReader") as reader,\
             patch("wechat_local.media.MediaResolver.resolve",return_value=[{"path":str(self.asset),"kind":"image","quality":"original"}]),\
             patch("wechat_local.model.LocalModel",side_effect=AssertionError("raw export must not construct a model")) as model:
            reader.return_value.prepare_export.return_value=frozen
            result=execute(SimpleNamespace(command="export",job=job.name,summarize=False,allow_cloud=False),self.settings,lambda _:None)
        model.assert_not_called()
        directory=Path(result["result_path"]).parent
        self.assertEqual([json.loads(line) for line in (directory/"messages.jsonl").read_text(encoding="utf-8").splitlines()],self.rows)
        self.assertTrue(list((directory/"media").iterdir()))
        self.assertIn('<img ',(directory/"index.html").read_text(encoding="utf-8"))
        annotations=[json.loads(line) for line in (directory/"annotations.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(all(x["status"]=="missing" for x in annotations))

    def test_damaged_media_copy_is_not_declared_preserved(self):
        archive=self.archive()
        manifest=summary_input([archive],self.root/"summary.jsonl")
        result=summarize_selection(manifest,self.root/"reports",FakeInfer())
        next((archive/"media").iterdir()).write_bytes(b"changed-after-recognition")
        with self.assertRaises(ValueError):
            attach_records(result,manifest)
        self.assertFalse(read_json(result["result_file"])["complete"])


if __name__=="__main__":
    unittest.main()
