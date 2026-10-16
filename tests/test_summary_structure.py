"""Topic coverage, personal ordering and style use synthetic facts only."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local import summary
from wechat_local.summary_structure import organize,ordered_topics,attention,load_profile,STYLE
from wechat_local.checkpoints import Checkpoints
from wechat_local.common import atomic_json
from test_selected_summary import FakeInfer,message,stamp


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.checkpoints=Checkpoints(self.root/"checkpoints")
        self.profile={"reader_names":["Joy"],"focus":[{"name":"AI与自动化","keywords":["AI","Agent","模型"]}]}

    def fact(self,text,section="重要信息",topic="讨论"):
        return {"text":text,"section":section,"topic":topic,"evidence_ids":["M000001-P0001"],
                "quotes":[{"evidence_id":"M000001-P0001","quote":text}],"sources":["M000001"]}

    def test_no_fact_can_disappear_duplicate_or_gain_an_unknown_id(self):
        items=[self.fact("事项甲"),self.fact("事项乙")]
        for refs in (["F000001"],["F000001","F000001","F000002"],["F000001","F999999"]):
            calls=[]
            def infer(prompt):
                calls.append(prompt)
                return json.dumps({"topics":[{"title":"合成主题","fact_ids":refs}]})
            with self.assertRaises(summary.SummaryError) as caught:
                organize(items,infer,self.checkpoints)
            self.assertEqual(caught.exception.code,"topic_coverage_failed")
            self.assertEqual(len(calls),2)

    def test_grouping_does_not_rewrite_facts_or_quotes_and_resumes_without_model(self):
        items=[self.fact("事项甲"),self.fact("事项乙")]
        infer=lambda _:json.dumps({"topics":[{"title":"主题甲","fact_ids":["F000001"]},{"title":"主题乙","fact_ids":["F000002"]}]})
        result=organize(items,infer,self.checkpoints)
        resumed=organize(items,lambda _:self.fail("must reuse"),self.checkpoints)
        self.assertEqual(result,resumed)
        for before,after in zip(items,result):
            self.assertEqual(before["text"],after["text"])
            self.assertEqual(before["quotes"],after["quotes"])

    def test_attention_prioritizes_named_action_and_preferences_without_dropping_others(self):
        items=[self.fact("美食推荐",topic="美食"),self.fact("本地AI模型资料",topic="模型"),
               self.fact("Joy请确认测试方案，期限待确认。","待办","测试确认")]
        titles=[x[0] for x in ordered_topics(items,self.profile)]
        self.assertEqual(titles,["测试确认","模型","美食"])
        self.assertEqual(sum(len(x[1]) for x in ordered_topics(items,self.profile)),3)

    def test_other_speakers_i_and_email_are_not_personal_ai_signals(self):
        self.assertEqual(attention([self.fact("我去办理事项。","待办")],self.profile)[0][0],2)
        self.assertEqual(attention([self.fact("email系统文档")],self.profile)[0][0],4)

    def test_unknown_deadline_negative_and_completed_content_are_not_rewritten(self):
        text="预算尚未花费；不采用方案B；已完成资料整理；负责人和期限待确认。"
        item=self.fact(text,"明确决定")
        output=summary._markdown({"name":"合成","text_count":1},[item],stamp(),stamp()+1,False,
                    {"processed_messages":1,"message_count":1,"completed_batches":1,"batch_count":1},[item],self.profile)
        self.assertIn(text,output)
        self.assertIn("## 优先关注",output)
        self.assertIn("## 主题：讨论",output)
        self.assertIn("### 明确决定",output)

    def test_unrelated_topics_are_never_merged_together(self):
        snapshot=self.root/"messages.jsonl"
        rows=[message(1,"模型事项一"),message(2,"模型事项二"),message(3,"会议安排一"),message(4,"会议安排二")]
        snapshot.write_text("".join(json.dumps(x,ensure_ascii=False)+"\n" for x in rows),encoding="utf-8")
        fake=FakeInfer()
        merged=[]
        def infer(prompt):
            data=json.loads(prompt.split("INPUT_JSON=\n",1)[1])
            if "messages" in data:
                return json.dumps({"items":[{"section":"重要信息","text":x["text"],"evidence_ids":[x["evidence_id"]]} for x in data["messages"]]},ensure_ascii=False)
            if "topic_facts" in data:
                groups={}
                for fact in data["topic_facts"]:
                    title="模型" if "模型" in fact["text"] else "会议"
                    groups.setdefault(title,[]).append(fact["fact_id"])
                return json.dumps({"topics":[{"title":k,"fact_ids":v} for k,v in groups.items()]},ensure_ascii=False)
            merged.append(data["facts"])
            return fake(prompt)
        manifest={"messages_file":str(snapshot),"chats":[{"id":"one","name":"合成","text_count":4}],
                  "start":stamp()-1,"end":stamp()+1,"total_text":4}
        result=summary.summarize_selection(manifest,self.root/"reports",infer,summary_profile=self.profile)
        self.assertEqual(result["chats"][0]["audit"]["topic_count"],2)
        self.assertEqual(len(merged),2)
        self.assertTrue(all(all("模型" in f["text"] for f in group) or all("会议" in f["text"] for f in group) for group in merged))

    def test_style_instruction_is_present_and_missing_profile_remains_generic(self):
        self.assertIn(STYLE,summary.EXTRACT_INSTRUCTION)
        self.assertIn(STYLE,summary.MERGE_INSTRUCTION)
        self.assertEqual(load_profile(self.root),{"reader_names":[],"focus":[]})
        atomic_json(self.root/"summary_profile.json",{"reader_names":[],"focus":[{"name":"缺少关键词"}]})
        with self.assertRaises(ValueError):
            load_profile(self.root)


if __name__=="__main__":
    unittest.main()
