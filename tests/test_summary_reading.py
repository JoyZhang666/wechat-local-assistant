"""Synthetic offline reading, portability and citation-format checks."""
import contextlib
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local.common import atomic_json,read_json
from wechat_local.export import ExportError,ExportCancelled
from wechat_local import summary_reading as reading
from wechat_local.settings import Settings
from wechat_local.worker import execute


class Page(HTMLParser):
    def __init__(self,text):
        super().__init__()
        self.tags=[]
        self.attrs=[]
        self.text=[]
        self.feed(text)
    def handle_starttag(self,tag,attrs):
        self.tags.append(tag)
        self.attrs.extend(attrs)
    def handle_data(self,text):
        self.text.append(text)


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.report=self.root/"reports"/("a"*32)
        self.report.mkdir(parents=True)
        self.result=self.report/"result.json"
        self.original="# 合成聊天\n\n## 待办\n\n- 整理测试资料。\n  - 依据：M000001-P0001；原文：明天下午完成。\n"
        (self.report/"chat-001.md").write_text(self.original,encoding="utf-8")
        self.fact={"section":"待办","text":"整理测试资料。","evidence_ids":["M000001-P0001"],
                   "sources":["M000001"],"quotes":[{"evidence_id":"M000001-P0001","source":"M000001","start":0,"end":7,"quote":"明天下午完成。"}]}
        atomic_json(self.report/"chat-001-facts.json",{"items":[self.fact],"extracted":[self.fact],"audit":{"quote_count":1}})
        atomic_json(self.result,{"status":"complete","complete":True,"start":1791558000,"end":1791644400,
                    "total_text":1,"total_skipped":2,"model":"synthetic","created_at":"2026-10-11T01:00:00+08:00",
                    "checkpoint_dir":"private-cache","secret":"not-for-sharing",
                    "chats":[{"name":"合成聊天","file":str(self.report/"chat-001.md"),"id":"private-id","item_count":1}]})

    def test_legacy_quotes_change_only_markup_and_conversion_is_idempotent(self):
        quoted=reading.quoted_markdown(self.original)
        self.assertIn("\n\n  > 依据：M000001-P0001；原文：明天下午完成。",quoted)
        self.assertNotIn("  - 依据",quoted)
        self.assertEqual(reading.quoted_markdown(quoted),quoted)

    def test_reading_preserves_originals_and_displays_blockquotes(self):
        before={x.name:x.read_bytes() for x in self.report.iterdir()}
        page=reading.ensure_reading(self.result)
        parsed=Page(page.read_text(encoding="utf-8"))
        self.assertIn("blockquote",parsed.tags)
        self.assertIn("明天下午完成。","".join(parsed.text))
        for name,data in before.items():
            self.assertEqual((self.report/name).read_bytes(),data)
        manifest=read_json(self.report/"manifest.json")
        for name,digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((self.report/name).read_bytes()).hexdigest(),digest)

    def test_html_is_escaped_and_has_no_external_resources_or_executable_links(self):
        (self.report/"chat-001.md").write_text("# 合成\n- <script>alert(1)</script> [链接](https://example.invalid)\n  > 依据：M000001；原文：<img src=x onerror=alert(2)>",encoding="utf-8")
        parsed=Page(reading.ensure_reading(self.result).read_text(encoding="utf-8"))
        self.assertNotIn("script",parsed.tags)
        self.assertNotIn("img",parsed.tags)
        for key,value in parsed.attrs:
            if key in ("href","src"):
                self.assertTrue(value.startswith("#"))
        self.assertIn("<script>alert(1)</script>","".join(parsed.text))

    def test_complete_reader_has_no_preview_length_limit(self):
        (self.report/"chat-001.md").write_text("# 合成\n- "+"长文"*120000+"完整末尾哨兵",encoding="utf-8")
        text=reading.ensure_reading(self.result).read_text(encoding="utf-8")
        self.assertIn("完整末尾哨兵",text)

    def test_multiple_chats_have_distinct_anchors(self):
        data=read_json(self.result)
        (self.report/"chat-002.md").write_text("# 第二个合成聊天\n## 待办\n- 第二项",encoding="utf-8")
        data["chats"].append({"name":"第二个合成聊天","file":str(self.report/"chat-002.md")})
        atomic_json(self.result,data)
        parsed=Page(reading.ensure_reading(self.result).read_text(encoding="utf-8"))
        ids=[value for key,value in parsed.attrs if key=="id"]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertIn("chat-2",ids)

    def test_topic_contents_groups_section_links_into_the_correct_row(self):
        markdown="# 合成聊天\n## 优先关注\n- 先处理项目问题\n## 主题：项目进展\n### 未决问题\n- 日期待确认\n### 待办\n- 整理资料\n## 主题：预算\n### 重要信息\n- 上限300元\n### 明确决定\n- 采用方案A\n"
        (self.report/"chat-001.md").write_text(markdown,encoding="utf-8")
        html=reading.ensure_reading(self.result).read_text(encoding="utf-8")
        parsed=Page(html)
        self.assertIn("table",parsed.tags)
        self.assertEqual(parsed.tags.count("td"),8)
        table=html.split("<tbody>",1)[1].split("</tbody>",1)[0]
        rows=table.split("</tr>")
        self.assertIn('href="#chat-1-section-3">项目进展',rows[0])
        self.assertIn('href="#chat-1-section-4">未决问题',rows[0])
        self.assertIn('href="#chat-1-section-5">待办',rows[0])
        self.assertNotIn("重要信息",rows[0])
        self.assertIn('href="#chat-1-section-6">预算',rows[1])
        self.assertIn("重要信息",rows[1])
        self.assertIn("明确决定",rows[1])
        self.assertNotIn("未决问题",rows[1])
        anchors={value for key,value in parsed.attrs if key=="id"}
        for key,value in parsed.attrs:
            if key=="href" and value.startswith("#"):
                self.assertIn(value[1:],anchors)

    def test_contents_table_escapes_topic_names_and_preserves_empty_categories(self):
        markdown='# 合成\n## 主题：<script>测试</script>\n### 结论与要点\n- 仅有关注点\n'
        body,headings=reading._markdown_html(markdown,"synthetic")
        toc=reading._contents_table(markdown,headings)
        parsed=Page(toc+body)
        self.assertNotIn("script",parsed.tags)
        self.assertEqual(toc.count('class="empty"'),3)
        self.assertIn("&lt;script&gt;测试&lt;/script&gt;",toc)
        self.assertIn('href="#synthetic-section-3">结论与要点',toc)

    def test_legacy_section_only_summary_has_a_table_without_rewriting_markdown(self):
        page=reading.ensure_reading(self.result).read_text(encoding="utf-8")
        self.assertIn('class="topic-contents"',page)
        self.assertIn('href="#chat-1-section-2">待办',page)
        self.assertEqual((self.report/"chat-001.md").read_text(encoding="utf-8"),self.original)

    def test_failed_reports_remain_failed_and_history_does_not_read_messages(self):
        data=read_json(self.result)
        data.update(status="failed",complete=False,error_code="synthetic_failure")
        atomic_json(self.result,data)
        with patch("wechat_local.provider.WechatProvider",side_effect=AssertionError("must not read")),\
             patch("wechat_local.model.LocalModel",side_effect=AssertionError("must not infer")):
            entries=reading.history(self.root/"reports")
            text=reading.ensure_reading(self.result).read_text(encoding="utf-8")
        self.assertFalse(entries[0]["complete"])
        self.assertEqual(entries[0]["error_code"],"synthetic_failure")
        self.assertIn("未完成／部分结果",text)

    def test_export_is_relative_private_fields_excluded_and_hashes_survive_move(self):
        data=read_json(self.report/"chat-001-facts.json")
        data["token"]="not-for-sharing"
        data["items"][0]["secret"]="not-for-sharing"
        atomic_json(self.report/"chat-001-facts.json",data)
        target=self.root/"saved"
        reading.save_summary(self.result,target,owner_file=self.root/"owner.json")
        moved=self.root/"moved"
        target.rename(moved)
        manifest=read_json(moved/"manifest.json")
        for name,digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((moved/name).read_bytes()).hexdigest(),digest)
            self.assertNotIn(str(self.root),(moved/name).read_text(encoding="utf-8"))
            self.assertNotIn("not-for-sharing",(moved/name).read_text(encoding="utf-8"))
        self.assertFalse((moved/"result.json").exists())
        self.assertIn("  > 依据",(moved/"chat-001.md").read_text(encoding="utf-8"))
        self.assertEqual((self.report/"chat-001.md").read_text(encoding="utf-8"),self.original)

    def test_overwrite_and_saving_inside_original_are_rejected(self):
        for target in (self.root,self.report/"saved"):
            with self.assertRaises(ExportError):
                reading.save_summary(self.result,target,owner_file=self.root/"owner.json")

    def test_outside_or_traversal_report_paths_are_rejected(self):
        for filename in (str(self.root/"chat-001.md"),"../chat-001.md"):
            data=read_json(self.result)
            data["chats"][0]["file"]=filename
            atomic_json(self.result,data)
            with self.assertRaises(ExportError):
                reading.ensure_reading(self.result)

    def test_cancel_cleans_only_owned_stage(self):
        owner=self.root/"owner.json"
        def cancel():
            return owner.exists()
        with self.assertRaises(ExportCancelled):
            reading.save_summary(self.result,self.root/"saved",owner_file=owner,cancelled=cancel)
        self.assertFalse(list(self.root.glob(".wechat-save-*")))
        self.assertTrue(self.result.exists())

    def test_render_and_save_worker_never_instantiate_provider_or_model(self):
        settings=Settings(*(self.root for _ in range(4)),model="synthetic")
        with patch("wechat_local.worker.NamedMutex",side_effect=lambda *_:contextlib.nullcontext()),\
             patch("wechat_local.provider.WechatProvider",side_effect=AssertionError("must not read")),\
             patch("wechat_local.model.LocalModel",side_effect=AssertionError("must not infer")):
            event=execute(SimpleNamespace(command="render-summary",job="a"*32),settings,Mock())
            self.assertEqual(event["phase"],"summary-ready")
            saved=execute(SimpleNamespace(command="save-summary",job="a"*32,destination=self.root/"saved"),settings,Mock())
            self.assertEqual(saved["phase"],"summary-saved")

    def test_new_generation_auto_saves_reader_and_quote_markdown(self):
        from test_selected_summary import FakeInfer,message,stamp
        job=self.root/"jobs"/("b"*32)
        snapshot=job/"snapshot"/"messages.jsonl"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_text(json.dumps(message(),ensure_ascii=False)+"\n",encoding="utf-8")
        atomic_json(job/"manifest.json",{"chats":[{"id":"one","name":"合成","text_count":1}],
                    "total_text":1,"total_skipped":0,"start":stamp()-3600,"end":stamp()+3600,"messages_file":str(snapshot)})
        settings=Settings(*(self.root for _ in range(4)),model="synthetic")
        model=SimpleNamespace(infer=FakeInfer(),stop=Mock())
        with patch("wechat_local.worker.NamedMutex",side_effect=lambda *_:contextlib.nullcontext()),\
             patch("wechat_local.model.LocalModel",return_value=model):
            event=execute(SimpleNamespace(command="generate",job="b"*32),settings,Mock())
        directory=Path(event["result_path"]).parent
        self.assertTrue((directory/"index.html").is_file())
        self.assertTrue((directory/"manifest.json").is_file())
        self.assertIn("  > 依据",(directory/"chat-001.md").read_text(encoding="utf-8"))
        self.assertFalse((job/"snapshot").exists())


if __name__=="__main__":
    unittest.main()
