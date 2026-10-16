"""Model boundaries tested with synthetic requests and mocked local responses."""
import json
import io
from pathlib import Path
import socket
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local.common import BusyError,ContextTooLong,OutputTooLong,atomic_json
from wechat_local.model import LocalModel,stop_owned_model
from wechat_local.settings import Settings


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.settings=Settings(self.root,self.root,self.root/"ollama.exe",self.root,"synthetic",11534)
        self.model=LocalModel(self.settings,self.root/"job")

    def tearDown(self):
        self.temp.cleanup()

    def request(self,response,payload=None):
        if payload is None: payload={"messages":[{"source":"M000001","evidence_id":"M000001-P0001","text":"synthetic"}]}
        with patch.object(self.model,"start"),patch.object(self.model,"api",return_value=response) as api:
            value=self.model.infer("INPUT_JSON=\n"+json.dumps(payload))
        return value,api.call_args.args[1]

    def test_section_and_source_constraints_and_local_only(self):
        value,request=self.request({"done":True,"message":{"content":"{\"items\":[]}"}})
        schema=request["format"]["properties"]["items"]["items"]
        self.assertEqual(schema["properties"]["evidence_ids"]["items"]["enum"],["M000001-P0001"])
        self.assertNotIn("quote",schema["properties"])
        self.assertNotIn("sources",schema["properties"])
        self.assertIn("待办",schema["properties"]["section"]["enum"])
        self.assertFalse(request["think"])
        self.assertEqual(self.settings.base_url,"http://127.0.0.1:11534")

    def test_merge_must_return_fact_ids(self):
        _,request=self.request({"done":True,"message":{"content":"{}"}},
            {"facts":[{"fact_id":"F1","sources":["M000001"]}]})
        schema=request["format"]["properties"]["items"]["items"]
        self.assertIn("fact_ids",schema["required"])
        self.assertEqual(schema["properties"]["fact_ids"]["items"]["enum"],["F1"])

    def test_topic_grouping_schema_returns_only_titles_and_legal_ids(self):
        _,request=self.request({"done":True,"message":{"content":"{}"}},
                              {"topic_facts":[{"fact_id":"F000001","section":"待办","text":"合成事项"}]})
        schema=request["format"]["properties"]["topics"]["items"]
        self.assertEqual(set(schema["properties"]),{"title","fact_ids"})
        self.assertEqual(schema["properties"]["fact_ids"]["items"]["enum"],["F000001"])

    def test_truncated_output_is_rejected(self):
        with self.assertRaises(OutputTooLong):
            self.request({"done_reason":"length","done":True,"message":{"content":"partial"}})

    def test_context_overflow_is_rejected(self):
        with self.assertRaises(ContextTooLong):
            self.request({"prompt_eval_count":3072,"done":True,"message":{"content":"partial"}})
        with self.assertRaises(ContextTooLong),patch.object(self.model,"start") as start:
            self.model.infer("x"*2801)
        start.assert_not_called()

    def test_busy_port_does_not_take_over_service(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1",0))
            server.listen()
            self.settings.port=server.getsockname()[1]
            with self.assertRaises(BusyError),patch("subprocess.Popen") as launch:
                self.model.start()
            launch.assert_not_called()

    def test_pid_reuse_guard_never_kills_other_process(self):
        atomic_json(self.root/"job"/"model-owner.json",{"pid":123,"created":1,"executable":str(self.settings.ollama_executable)})
        with patch("wechat_local.model.psutil.Process") as factory:
            process=factory.return_value
            process.exe.return_value=str(self.root/"unrelated.exe")
            with self.assertRaises(RuntimeError):
                stop_owned_model(self.root/"job",self.settings)
            process.kill.assert_not_called()

    def test_delete_uses_delete_method_and_accepts_empty_body(self):
        with patch.object(self.model.opener,"open") as opener:
            opener.return_value.__enter__.return_value.read.return_value=b""
            self.assertEqual(self.model.api("/api/delete",{"model":"synthetic-stage"}),{})
        request=opener.call_args.args[0]
        self.assertEqual(request.get_method(),"DELETE")
        self.assertTrue(request.full_url.startswith("http://127.0.0.1:"))

    def test_image_annotation_remains_derived(self):
        image=self.root/"synthetic.png"
        image.write_bytes(b"synthetic fixture bytes")
        output={"ocr_text":"300元","description":"合成截图","uncertain":[]}
        with patch.object(self.model,"start"),patch.object(self.model,"api",return_value={"done":True,"message":{"content":json.dumps(output)}}):
            value=self.model.analyze_image(image)
        self.assertTrue(value["derived"])
        self.assertFalse(value["human_verified"])
        self.assertEqual(value["ocr_text"],"300元")

    def test_audio_longer_than_25_seconds_is_split(self):
        audio=self.root/"synthetic.wav"
        with wave.open(str(audio),"wb") as stream:
            stream.setparams((1,2,16000,0,"NONE","not compressed"))
            stream.writeframes(b"\1\0"*16000*26)
        response={"done":True,"message":{"content":json.dumps({"transcript":"合成转写","uncertain":[]})}}
        with patch.object(self.model,"start"),patch.object(self.model,"api",return_value=response) as api:
            result=self.model.transcribe_audio(audio)
        self.assertEqual(api.call_count,2)
        self.assertEqual(result["segments"][0]["end_seconds"],25)
        self.assertEqual(result["segments"][1]["end_seconds"],26)
        self.assertEqual(result["transcript"],"合成转写\n合成转写")


if __name__=="__main__":
    unittest.main()
