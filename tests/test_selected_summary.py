"""Synthetic selection-boundary and evidence checks; no app/private-data access."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wechat_local import summary


class OutputTooLong(RuntimeError):
    pass


class ContextTooLong(RuntimeError):
    pass


def stamp(day="2026-01-05T10:00:00+08:00"):
    return int(datetime.fromisoformat(day).timestamp())


def message(index=1, text="请整理测试资料。", chat="one", when=None):
    return {"id": str(index), "chat_id": chat, "chat_name": "合成聊天", "sender_id": "synthetic",
            "sender": "合成甲", "is_self": False, "timestamp": when or stamp(), "type": 1, "text": text}


class FakeInfer:
    def __init__(self):
        self.prompts = []
        self.extracted = []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        data = json.loads(prompt.split("INPUT_JSON=\n", 1)[1])
        if "topic_facts" in data:
            return json.dumps({"topics":[{"title":"合成事项","fact_ids":[x["fact_id"] for x in data["topic_facts"]]}]},ensure_ascii=False)
        if "messages" in data:
            self.extracted.extend(data["messages"])
            items = []
            for row in data["messages"]:
                text = "整理所选消息内容。"
                if "明天" in row["text"]:
                    tomorrow = datetime.fromisoformat(row["at"]) + timedelta(days=1)
                    text = tomorrow.strftime("%Y-%m-%d") + " 15:00前整理测试资料（所选期间待办）。"
                items.append({"section": "待办", "text": text, "evidence_ids": [row["evidence_id"]]})
        else:
            facts = data["facts"]
            items = [{"section": "待办", "text": "合并所选期间待办。",
                      "fact_ids": [f["fact_id"] for f in facts]}]
        return json.dumps({"items": items}, ensure_ascii=False)


class SelectedSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def manifest(self, rows, chats=None, **updates):
        path = self.base / "snapshot.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        if chats is None:
            chats = [{"id": "one", "name": "合成聊天", "is_group": True,
                      "text_count": len(rows), "skipped_count": 2}]
        value = {"chats": chats, "total_text": len(rows), "total_skipped": 2,
                 "messages_file": str(path), "snapshot_end": stamp() + 3600,
                 "start": stamp() - 3600, "end": stamp() + 3600}
        value.update(updates)
        return value

    def run_summary(self, manifest, infer=None, progress=None):
        return summary.summarize_selection(manifest, self.base / "reports", infer or FakeInfer(), progress)

    def assert_error(self, code, manifest, infer=None):
        with self.assertRaises(summary.SummaryError) as caught:
            self.run_summary(manifest, infer)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_date_is_anchored_to_message_not_today(self):
        manifest = self.manifest([message(text="明天下午三点前整理测试资料。")])
        infer = FakeInfer()
        result = self.run_summary(manifest, infer)
        report = Path(result["chats"][0]["file"]).read_text(encoding="utf-8")
        self.assertIn("2026-01-06 15:00", report)
        self.assertIn("不表示截至现在仍未完成", report)
        self.assertIn("M000001", report)
        self.assertTrue(result["complete"])
        self.assertNotIn("请整理测试资料", Path(result["result_file"]).read_text(encoding="utf-8"))

    def test_all_evidence_including_appendix_uses_quote_blocks(self):
        item={"section":"待办","text":"测试待办","quotes":[{"evidence_id":"M000001-P0001","quote":"原文依据"}]}
        extracted={**item,"text":"另一条保留事实"}
        audit={"processed_messages":1,"message_count":1,"completed_batches":1,"batch_count":1}
        text=summary._markdown({"name":"合成","text_count":1},[item],stamp(),stamp()+1,False,audit,[extracted])
        evidence=[line for line in text.splitlines() if "依据：M" in line]
        self.assertEqual(len(evidence),2)
        self.assertTrue(all(line.startswith("  > ") for line in evidence))

    def test_unselected_chat_rejected_before_any_inference(self):
        infer = FakeInfer()
        manifest = self.manifest([message(), message(2, "LEAK_SENTINEL", chat="unselected")], chats=[
            {"id": "one", "name": "合成聊天", "text_count": 1, "skipped_count": 0}])
        self.assert_error("unselected_chat_in_snapshot", manifest, infer)
        self.assertEqual(infer.prompts, [])
        self.assertFalse((self.base / "reports").exists())

    def test_time_end_exclusive_and_start_inclusive(self):
        infer = FakeInfer()
        self.assert_error("out_of_range_message", self.manifest([
            message(text="TIME_LEAK_SENTINEL", when=stamp()+3600)]), infer)
        self.assertEqual(infer.prompts, [])
        result = self.run_summary(self.manifest([message(when=stamp()-3600)]))
        self.assertTrue(result["complete"])

    def test_unknown_identity_stays_unknown(self):
        row = message()
        row.update(sender="", is_self=None)
        infer = FakeInfer()
        self.run_summary(self.manifest([row]), infer)
        self.assertEqual(infer.extracted[0]["sender"], "待确认")
        self.assertIsNone(infer.extracted[0]["is_self"])

    def test_fabricated_quote_is_rejected(self):
        def fake(prompt):
            return json.dumps({"items": [{"section": "待办", "text": "编造事实", "sources": ["M000001"],
                                          "quote": "原文没有这句话"}]}, ensure_ascii=False)
        error = self.assert_error("invalid_model_item", self.manifest([message()]), fake)
        self.assertEqual(error.partial_results["chats"], [])
        self.assertEqual(json.loads(Path(error.partial_results["result_file"]).read_text(encoding="utf-8"))["status"], "failed")

    def test_source_from_other_chat_is_rejected(self):
        def fake(prompt):
            return json.dumps({"items": [{"section": "待办", "text": "测试", "sources": ["M999999"],
                                          "quote": "请整理"}]}, ensure_ascii=False)
        self.assert_error("invalid_model_item", self.manifest([message()]), fake)

    def test_bad_evidence_id_gets_one_same_input_correction(self):
        calls=[]
        good=FakeInfer()
        def infer(prompt):
            calls.append(prompt)
            if len(calls)==1:
                return json.dumps({"items":[{"section":"待办","text":"合成待办","evidence_ids":["M999999-P0001"]}]},ensure_ascii=False)
            return good(prompt)
        result=self.run_summary(self.manifest([message()]),infer)
        self.assertTrue(result["complete"])
        self.assertEqual(len(calls),2)
        self.assertTrue(calls[1].startswith(summary.FORMAT_REPAIR % "invalid_evidence_id"))
        self.assertEqual(calls[0].split("INPUT_JSON=\n",1)[1],calls[1].split("INPUT_JSON=\n",1)[1])
        self.assertLessEqual(len(calls[1]),summary.PROMPT_BUDGET)

    def test_quote_correction_is_bounded_and_still_strict(self):
        calls=[]
        def infer(prompt):
            calls.append(prompt)
            return json.dumps({"items":[{"section":"待办","text":"合成待办","evidence_ids":["M999999-P0001"]}]},ensure_ascii=False)
        self.assert_error("invalid_evidence_id",self.manifest([message()]),infer)
        self.assertEqual(len(calls),2)

    def test_invalid_item_correction_names_reason_without_response_body(self):
        calls=[]
        good=FakeInfer()
        def infer(prompt):
            calls.append(prompt)
            if len(calls)==1:
                return json.dumps({"items":[{"section":"wrong-private-category", "text":"PRIVATE_RESPONSE", "evidence_ids":["M000001-P0001"]}]})
            return good(prompt)
        result=self.run_summary(self.manifest([message()]),infer)
        self.assertTrue(result["complete"])
        self.assertIn(summary.VALIDATION_REASONS["section"],calls[1])
        self.assertNotIn("PRIVATE_RESPONSE",calls[1])
        self.assertNotIn("wrong-private-category",calls[1])
        self.assertLessEqual(len(calls[1]),summary.PROMPT_BUDGET)

    def test_format_failure_splits_once_and_preserves_quotes_and_cache(self):
        fake=FakeInfer()
        calls=[]
        def infer(prompt):
            calls.append(prompt)
            data=json.loads(prompt.split("INPUT_JSON=\n",1)[1])
            if len(data.get("messages",[]))>1:
                return json.dumps({"items":[{"section":"待办", "text":"合成", "evidence_ids":[]}]},ensure_ascii=False)
            return fake(prompt)
        events=[]
        manifest=self.manifest([message(1),message(2)])
        result=self.run_summary(manifest,infer,events.append)
        self.assertTrue(result["complete"])
        self.assertEqual([x["evidence_id"] for x in fake.extracted],["M000001-P0001","M000002-P0001"])
        self.assertTrue(result["chats"][0]["audit"]["quotes_verified"])
        self.assertEqual(len([p for p in calls if '"messages"' in p]),4)
        self.assertTrue(any(e.get("state")=="retry" for e in events))
        self.assertTrue(all(len(p)<=summary.PROMPT_BUDGET for p in calls))
        resumed=self.run_summary(manifest,lambda _:self.fail("accepted checkpoints must be reused"))
        self.assertTrue(resumed["complete"])

    def test_persistent_format_failure_is_bounded_and_diagnostic_is_private_safe(self):
        calls=[]
        def infer(prompt):
            calls.append(prompt)
            return json.dumps({"items":[{"section":"待办","text":"PRIVATE_BAD_RESPONSE","evidence_ids":[],"PRIVATE_FIELD":True}]})
        error=self.assert_error("invalid_model_item",self.manifest([message(i) for i in range(1,5)]),infer)
        self.assertEqual(len(calls),4)  # Original + correction; first half + correction; stop.
        self.assertEqual(error.partial_results["validation_error"],{"reason":"fields","item_index":1})
        saved=Path(error.partial_results["result_file"]).read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE_BAD_RESPONSE",saved)
        self.assertNotIn("PRIVATE_FIELD",saved)
        self.assertFalse(error.partial_results["complete"])

    def test_validation_reasons_remain_strict(self):
        base={"section":"待办","text":"合成","evidence_ids":["M000001-P0001"]}
        for updates,reason in (({"section":"不支持"},"section"),({"text":"x"*601},"text"),
                               ({"evidence_ids":["M000001-P0001"]*2},"refs")):
            with self.assertRaises(summary.SummaryError) as caught:
                summary._validate([{**base,**updates}],{})
            self.assertEqual(caught.exception.validation_error,{"reason":reason,"item_index":1})

    def test_long_message_keeps_all_fragments_and_prompt_budgets(self):
        text = "合成文字\"\\\n" * 900 + "独特末尾不会丢失"
        infer = FakeInfer()
        result = self.run_summary(self.manifest([message(text=text)]), infer)
        self.assertEqual("".join(row["text"] for row in infer.extracted), text)
        self.assertEqual({row["source"] for row in infer.extracted}, {"M000001"})
        for prompt in infer.prompts:
            self.assertLessEqual(len(prompt), summary.PROMPT_BUDGET)
            self.assertLessEqual(len(prompt.split("INPUT_JSON=\n", 1)[1]), summary.INPUT_BUDGET)
        self.assertTrue(result["complete"])

    def test_output_limit_retries_smaller_fragments_without_loss(self):
        fake = FakeInfer()
        successful = []
        def infer(prompt):
            payload = json.loads(prompt.split("INPUT_JSON=\n", 1)[1])
            if "messages" in payload:
                rows = payload["messages"]
                if sum(len(row["text"]) for row in rows) > 220:
                    raise OutputTooLong("synthetic")
                successful.extend(row["text"] for row in rows)
            return fake(prompt)
        text = "ABCD" * 400
        self.run_summary(self.manifest([message(text=text)]), infer)
        self.assertEqual("".join(successful), text)
        self.assertTrue(all(len(part) <= 120 for part in successful))

    def test_context_limit_retry_then_success(self):
        fake = FakeInfer()
        def infer(prompt):
            data = json.loads(prompt.split("INPUT_JSON=\n", 1)[1])
            if len(data.get("messages", [])) > 1:
                raise ContextTooLong("synthetic")
            return fake(prompt)
        result = self.run_summary(self.manifest([message(1), message(2)]), infer)
        self.assertTrue(result["complete"])

    def test_minimum_fragment_failure_is_explicit(self):
        def infer(prompt):
            raise OutputTooLong("private error text must not propagate")
        error = self.assert_error("minimum_fragment_failed", self.manifest([message(text="x" * 199)]), infer)
        self.assertNotIn("private error", str(error))

    def test_omitted_merge_fact_is_rejected(self):
        fake = FakeInfer()
        def infer(prompt):
            data = json.loads(prompt.split("INPUT_JSON=\n", 1)[1])
            if "facts" in data:
                fact = data["facts"][0]
                return json.dumps({"items": [{"section": fact["section"], "text": fact["text"],
                                             "fact_ids": [fact["fact_id"]]}]}, ensure_ascii=False)
            return fake(prompt)
        self.assert_error("merge_omitted_facts", self.manifest([message(1), message(2)]), infer)

    def test_failure_preserves_completed_chat_only(self):
        chats = [{"id": name, "name": "重名聊天", "text_count": 1, "skipped_count": 0} for name in ("one", "two", "three")]
        fake = FakeInfer()
        def infer(prompt):
            if "FAIL_PRIVATE_SENTINEL" in prompt:
                raise RuntimeError("private body must not be written in status")
            return fake(prompt)
        rows = [message(1), message(2, "FAIL_PRIVATE_SENTINEL", "two"), message(3, "SHOULD_NOT_RUN", "three")]
        error = self.assert_error("local_inference_failed", self.manifest(rows, chats), infer)
        self.assertEqual(len(error.partial_results["chats"]), 1)
        self.assertEqual(error.partial_results["chats"][0]["id"], "one")
        self.assertTrue(Path(error.partial_results["chats"][0]["file"]).is_file())
        self.assertEqual(len(list(Path(error.partial_results["job_dir"]).glob("chat-*.md"))), 1)
        self.assertFalse(any("SHOULD_NOT_RUN" in p for p in fake.prompts))
        self.assertNotIn("private body", Path(error.partial_results["result_file"]).read_text(encoding="utf-8"))

    def test_empty_chat_never_loads_model(self):
        infer = FakeInfer()
        result = self.run_summary(self.manifest([]), infer)
        self.assertEqual(infer.prompts, [])
        self.assertEqual(result["chats"][0]["item_count"], 0)

    def test_bad_snapshot_counts_duplicates_and_nontext_fail(self):
        self.assert_error("snapshot_count_mismatch", self.manifest([message()], total_text=2))
        self.assert_error("duplicate_snapshot_message", self.manifest([message(), message()]))
        row = message()
        row["type"] = True
        self.assert_error("non_text_message_in_snapshot", self.manifest([row]))

    def test_snapshot_end_only_compatibility(self):
        manifest = self.manifest([message()])
        manifest.pop("start")
        manifest.pop("end")
        self.assertTrue(self.run_summary(manifest)["complete"])

    def test_progress_has_no_body_and_markdown_disables_embeds(self):
        events = []
        text = "![图片](https://example.invalid/) <script>"
        result = self.run_summary(self.manifest([message(text=text)]), progress=events.append)
        self.assertTrue(events)
        self.assertTrue(all(text not in event["message"] for event in events))
        report = Path(result["chats"][0]["file"]).read_text(encoding="utf-8")
        self.assertIn("\\!\\[", report)
        self.assertIn("\\<script\\>", report)
        self.assertTrue((self.base / "snapshot.jsonl").exists())

    def test_all_quotes_are_program_slices_including_unicode_and_newlines(self):
        text = "原文“标点”、😀\n不采用方案B。" * 23 + "最后期限为明天下午三点。"
        result = self.run_summary(self.manifest([message(text=text)]))
        facts = json.loads((Path(result["job_dir"])/"chat-001-facts.json").read_text(encoding="utf-8"))
        for item in facts["items"]+facts["extracted"]:
            for quote in item["quotes"]:
                self.assertEqual(text[quote["start"]:quote["end"]], quote["quote"])
        self.assertEqual(facts["audit"]["processed_chars"],len(text))
        self.assertTrue(facts["audit"]["quotes_verified"])

    def test_failure_in_same_chat_resumes_finished_batches_without_inference(self):
        rows = [message(i, "唯一片段%d："%i+"测试资料"*25) for i in range(1,25)]
        manifest = self.manifest(rows)
        first = FakeInfer()
        calls = []
        def fail(prompt):
            calls.append(prompt)
            if len(calls)==3:
                raise RuntimeError("synthetic failure")
            return first(prompt)
        error = self.assert_error("local_inference_failed",manifest,fail)
        state = json.loads((Path(error.partial_results["checkpoint_dir"])/"state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["completed_batches"],2)
        second = FakeInfer()
        result = self.run_summary(manifest,second)
        self.assertTrue(result["complete"])
        self.assertGreaterEqual(result["checkpoint_reused"],2)
        self.assertNotIn(calls[0],second.prompts)
        self.assertNotIn(calls[1],second.prompts)
        self.assertEqual(result["chats"][0]["audit"]["processed_messages"],len(rows))

    def test_changed_input_range_or_backend_cannot_reuse_checkpoints(self):
        manifest = self.manifest([message()])
        summary.summarize_selection(manifest,self.base/"reports",FakeInfer(),checkpoint_context="model-a")
        for value, context in ((manifest,"model-b"),({**manifest,"start":manifest["start"]-1},"model-a")):
            infer=FakeInfer()
            summary.summarize_selection(value,self.base/"reports",infer,checkpoint_context=context)
            self.assertTrue(infer.prompts)
        manifest=self.manifest([message(text="已改写的原文。")])
        infer=FakeInfer()
        summary.summarize_selection(manifest,self.base/"reports",infer,checkpoint_context="model-a")
        self.assertTrue(infer.prompts)

    def test_corrupt_checkpoint_fails_closed_and_does_not_send(self):
        manifest=self.manifest([message()])
        result=self.run_summary(manifest)
        path=next(Path(result["checkpoint_dir"]).glob("*.json"))
        if path.name=="state.json":
            path=next(p for p in Path(result["checkpoint_dir"]).glob("*.json") if p.name!="state.json")
        data=json.loads(path.read_text(encoding="utf-8"))
        data["items"][0]["text"]="篡改测试"
        path.write_text(json.dumps(data),encoding="utf-8")
        infer=FakeInfer()
        self.assert_error("checkpoint_invalid",manifest,infer)
        self.assertEqual(infer.prompts,[])

    def test_every_input_batch_has_start_then_saved_done_progress(self):
        events=[]
        result=self.run_summary(self.manifest([message(i,"测试"*70) for i in range(1,10)]),progress=events.append)
        extraction=[e for e in events if e["phase"]=="extract"]
        self.assertEqual(extraction[0]["current"],0)
        self.assertEqual(extraction[0]["state"],"start")
        for start,done in zip(extraction[::2],extraction[1::2]):
            self.assertEqual(done["state"],"done")
            self.assertEqual(done["current"],start["current"]+1)
        self.assertEqual(extraction[-1]["current"],extraction[-1]["total"])
        self.assertIn("分段事实清单",Path(result["chats"][0]["file"]).read_text(encoding="utf-8"))

    def test_reviewed_checkpoint_preserves_all_facts_without_new_model_rewrite(self):
        from wechat_local.checkpoints import Checkpoints
        manifest=self.manifest([message(i) for i in range(1,6)])
        initial=self.run_summary(manifest)
        facts=json.loads((Path(initial["job_dir"])/"chat-001-facts.json").read_text(encoding="utf-8"))["extracted"]
        wire=summary._wire(facts)
        Checkpoints(initial["checkpoint_dir"]).save("reviewed",wire,wire)
        def forbidden(_):
            self.fail("a reviewed result must not make any model request")
        result=self.run_summary(manifest,forbidden)
        self.assertEqual(result["chats"][0]["item_count"],len(facts))
        self.assertEqual(result["chats"][0]["audit"]["summary_method"],"reviewed_facts_preserved")
        self.assertEqual(result["checkpoint_saved"],0)

    def test_explicit_review_correction_does_not_show_old_unsupported_text(self):
        from wechat_local.checkpoints import Checkpoints
        manifest=self.manifest([message(i) for i in range(1,4)])
        initial=self.run_summary(manifest)
        facts=json.loads((Path(initial["job_dir"])/"chat-001-facts.json").read_text(encoding="utf-8"))["extracted"]
        wire=summary._wire(facts)
        corrected=[dict(item) for item in wire]
        corrected[0]["text"]="复核后的合成事实。"
        Checkpoints(initial["checkpoint_dir"]).save("reviewed",wire,corrected)
        result=self.run_summary(manifest,lambda _:self.fail("must use reviewed checkpoint"))
        saved=json.loads((Path(result["job_dir"])/"chat-001-facts.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["items"][0]["text"],"复核后的合成事实。")
        self.assertEqual(saved["extracted"][0]["text"],"复核后的合成事实。")
        self.assertEqual(saved["audit"]["processed_messages"],3)


if __name__ == "__main__":
    unittest.main()
