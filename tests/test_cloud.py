"""Synthetic compatible-API boundaries; no credentials or external requests."""
from dataclasses import replace
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock,Mock,patch
from urllib.error import HTTPError

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from wechat_local.cloud import CloudModel,NoRedirect,cloud_endpoint,request_completion,ensure_cloud_interpreter
from wechat_local.credentials import protect_key,reveal_key
from wechat_local.settings import Settings
from wechat_local.worker import execute
from wechat_local.common import OutputTooLong


class CloudTests(unittest.TestCase):
    def setUp(self):
        (ROOT/"tmp").mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/"tmp",prefix="cloud-test-")
        self.root=Path(self.temp.name)
        self.request={"base_url":"https://api.example.invalid/v1","model":"synthetic-model",
                      "key_dpapi":"synthetic-encrypted","timeout":30,"prompt":"合成测试，只返回JSON"}

    def tearDown(self):
        self.temp.cleanup()

    def opener(self,choice=None):
        result=MagicMock()
        choice=choice or {"finish_reason":"stop","message":{"content":'{"items":[]}'}}
        result.open.return_value.__enter__.return_value.read.return_value=json.dumps({"choices":[choice]}).encode()
        return result

    def test_https_endpoint_and_no_redirect(self):
        self.assertEqual(cloud_endpoint(self.request["base_url"]),self.request["base_url"]+"/chat/completions")
        for url in ("http://api.example.invalid/v1","https://user:pass@example.invalid/v1",
                    "https://localhost/v1","https://127.0.0.1/v1","https://example.invalid/v1?key=secret",
                    "https://example.invalid/v1#fragment","https://example.invalid/v1/chat/completions"):
            with self.assertRaises(ValueError): cloud_endpoint(url)
        self.assertIsNone(NoRedirect().redirect_request(None,None,None,None,None,None,None))

    def test_host_runtime_update_keeps_matching_owned_worker_without_overwrite(self):
        scripts=self.root/'venv'/'Scripts';scripts.mkdir(parents=True)
        (scripts.parent/'pyvenv.cfg').write_text('synthetic',encoding='utf-8')
        base=self.root/'base';base.mkdir()
        (base/'python.exe').write_bytes(b'updated-host-console')
        (scripts/'python-local-only.exe').write_bytes(b'trusted-app-console')
        target=scripts/'python-cloud-api.exe';target.write_bytes(b'trusted-app-console')
        with patch('wechat_local.cloud.sys.executable',str(scripts/'pythonw-local-only.exe')),\
             patch('wechat_local.cloud.sys.base_prefix',str(base)):
            self.assertEqual(ensure_cloud_interpreter(),target)
        self.assertEqual(target.read_bytes(),b'trusted-app-console')

    def test_unknown_cloud_interpreter_is_still_rejected_and_preserved(self):
        scripts=self.root/'venv'/'Scripts';scripts.mkdir(parents=True)
        (scripts.parent/'pyvenv.cfg').write_text('synthetic',encoding='utf-8')
        base=self.root/'base';base.mkdir()
        (base/'python.exe').write_bytes(b'current-host')
        (scripts/'python-local-only.exe').write_bytes(b'trusted-app-console')
        target=scripts/'python-cloud-api.exe';target.write_bytes(b'unknown-program')
        with patch('wechat_local.cloud.sys.executable',str(scripts/'pythonw-local-only.exe')),\
             patch('wechat_local.cloud.sys.base_prefix',str(base)),self.assertRaises(ValueError):
            ensure_cloud_interpreter()
        self.assertEqual(target.read_bytes(),b'unknown-program')

    def test_new_cloud_image_includes_missing_windows_runtime_without_overwrite(self):
        scripts=self.root/'venv'/'Scripts';scripts.mkdir(parents=True)
        (scripts.parent/'pyvenv.cfg').write_text('synthetic',encoding='utf-8')
        base=self.root/'base';base.mkdir()
        (base/'python.exe').write_bytes(b'host')
        (base/'python312.dll').write_bytes(b'host-runtime')
        (base/'vcruntime140.dll').write_bytes(b'new-runtime')
        (scripts/'vcruntime140.dll').write_bytes(b'existing-runtime')
        with patch('wechat_local.cloud.sys.executable',str(scripts/'python.exe')),\
             patch('wechat_local.cloud.sys.base_prefix',str(base)):
            target=ensure_cloud_interpreter()
        self.assertEqual(target.read_bytes(),b'host')
        self.assertEqual((scripts/'python312.dll').read_bytes(),b'host-runtime')
        self.assertEqual((scripts/'vcruntime140.dll').read_bytes(),b'existing-runtime')

    def test_api_payload_is_text_only_and_json(self):
        opener=self.opener()
        with patch("wechat_local.credentials.reveal_key",return_value="synthetic-test-key"):
            self.assertEqual(request_completion(self.request,opener),'{"items":[]}')
        request=opener.open.call_args.args[0]
        body=json.loads(request.data)
        self.assertEqual(body["response_format"],{"type":"json_object"})
        self.assertFalse(body["stream"])
        self.assertEqual(body["model"],"synthetic-model")
        self.assertNotIn("images",str(body))
        self.assertEqual(opener.open.call_args.kwargs["timeout"],30)

    def test_http_diagnostics_never_echo_body_key_or_url(self):
        opener=self.opener()
        opener.open.side_effect=HTTPError("https://private.invalid",401,"secret-body",{},io.BytesIO(b"secret-body"))
        with patch("wechat_local.credentials.reveal_key",return_value="synthetic-test-key"):
            with self.assertRaises(RuntimeError) as error: request_completion(self.request,opener)
        self.assertIn("401",str(error.exception))
        self.assertNotIn("secret",str(error.exception))
        self.assertNotIn("private",str(error.exception))

    def test_truncation_and_bad_json_are_not_success(self):
        with patch("wechat_local.credentials.reveal_key",return_value="synthetic-test-key"):
            with self.assertRaises(OutputTooLong):
                request_completion(self.request,self.opener({"finish_reason":"length","message":{"content":"{}"}}))
            with self.assertRaises(RuntimeError):
                request_completion(self.request,self.opener({"finish_reason":"stop","message":{"content":"not JSON"}}))

    def test_dpapi_roundtrip_is_not_plaintext(self):
        encrypted=protect_key("synthetic-only-key")
        self.assertNotIn("synthetic-only-key",encrypted)
        self.assertEqual(reveal_key(encrypted),"synthetic-only-key")
        with self.assertRaises(ValueError): reveal_key("not-a-protected-key")

    def test_old_settings_remain_local_and_cloud_can_omit_ollama(self):
        path=self.root/"settings.json"
        config={"data_dir":str(self.root),"provider_dir":str(self.root),"ollama_executable":str(self.root/"ollama.exe"),
                "model_dir":str(self.root),"model":"synthetic"}
        path.write_text(json.dumps(config),encoding="utf-8")
        self.assertEqual(Settings.load(path).model_backend,"local")
        config={"data_dir":str(self.root),"provider_dir":str(self.root),"model_backend":"cloud",
                "cloud_base_url":self.request["base_url"],"cloud_model":"synthetic","cloud_key_dpapi":"encrypted"}
        path.write_text(json.dumps(config),encoding="utf-8")
        value=Settings.load(path)
        self.assertEqual(value.model_backend,"cloud")
        self.assertNotIn("encrypted",repr(value))

    def test_cloud_generate_without_per_task_consent_never_reads_or_calls(self):
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),model_backend="cloud")
        with patch("wechat_local.provider.WechatProvider",side_effect=AssertionError("must not read")),\
             patch.object(CloudModel,"infer",side_effect=AssertionError("must not send")):
            with self.assertRaises(ValueError):
                execute(SimpleNamespace(command="generate",job="a"*32),settings,lambda _:None)

    def test_cloud_child_uses_pipe_not_key_or_chat_arguments(self):
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),model_backend="cloud",
                         cloud_base_url=self.request["base_url"],cloud_model="synthetic",cloud_key_dpapi="encrypted")
        process=Mock()
        process.communicate.return_value=(json.dumps({"success":True,"content":'{"items":[]}'}),"")
        process.returncode=0
        process.poll.return_value=0
        with patch.object(Path,"is_file",return_value=True),patch("wechat_local.cloud.subprocess.Popen",return_value=process) as launch:
            self.assertEqual(CloudModel(settings,self.root).infer("合成私有文字"),'{"items":[]}')
        arguments=str(launch.call_args.args[0])
        self.assertNotIn("encrypted",arguments)
        self.assertNotIn("合成私有文字",arguments)
        self.assertNotIn("example.invalid",arguments)
        self.assertIn("合成私有文字",process.communicate.call_args.args[0])

    def test_confirmed_generate_selects_cloud_and_keeps_local_off(self):
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),model_backend="cloud")
        job=settings.data_dir/"jobs"/("a"*32)
        (job/"snapshot").mkdir(parents=True)
        manifest={"messages_file":str(job/"snapshot"/"messages.jsonl"),"total_text":1,"total_skipped":0}
        (job/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
        model=Mock()
        with patch("wechat_local.cloud.CloudModel",return_value=model) as cloud,\
             patch("wechat_local.model.LocalModel",side_effect=AssertionError("must not run local")),\
             patch("wechat_local.summary.summarize_selection",return_value={"result_file":"synthetic-result"}) as summarize:
            result=execute(SimpleNamespace(command="generate",job="a"*32,allow_cloud=True),settings,lambda _:None)
        cloud.assert_called_once()
        self.assertEqual(summarize.call_args.args[2],model.infer)
        self.assertEqual(result["model_backend"],"cloud")
        model.stop.assert_called_once()
        self.assertFalse((job/"snapshot").exists())

    def test_cloud_validation_failure_keeps_specific_reason(self):
        from wechat_local.summary import SummaryError
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),model_backend="cloud")
        job=settings.data_dir/"jobs"/("f"*32)
        (job/"snapshot").mkdir(parents=True)
        (job/"manifest.json").write_text(json.dumps({"messages_file":str(job/"snapshot"/"messages.jsonl")}),encoding="utf-8")
        with patch("wechat_local.cloud.CloudModel"),patch("wechat_local.summary.summarize_selection",side_effect=SummaryError("unverified_model_quote")):
            with self.assertRaises(SummaryError) as error:
                execute(SimpleNamespace(command="generate",job="f"*32,allow_cloud=True),settings,lambda _:None)
        self.assertEqual(error.exception.code,"unverified_model_quote")
        self.assertIn("unverified_model_quote",str(error.exception))

    def test_cloud_failure_detail_and_code_are_persisted_after_translation(self):
        from wechat_local.summary import SummaryError
        from wechat_local.common import atomic_json,read_json
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),model_backend="cloud")
        job=settings.data_dir/"jobs"/("e"*32)
        atomic_json(job/"manifest.json",{"messages_file":str(job/"snapshot"/"messages.jsonl")})
        report=settings.data_dir/"reports"/"synthetic"/"result.json"
        partial={"result_file":str(report),"status":"failed","error_code":"local_inference_failed"}
        atomic_json(report,partial)
        model=Mock(last_error="云端连接失败或超时；没有改用其他服务。")
        with patch("wechat_local.cloud.CloudModel",return_value=model),\
             patch("wechat_local.summary.summarize_selection",side_effect=SummaryError("local_inference_failed",partial)):
            with self.assertRaises(SummaryError):
                execute(SimpleNamespace(command="generate",job="e"*32,allow_cloud=True),settings,lambda _:None)
        saved=read_json(report)
        self.assertEqual(saved["error_code"],"cloud_inference_failed")
        self.assertIn("连接失败或超时",saved["diagnostic_message"])

    def test_specific_membership_error_survives_child_and_parent(self):
        from wechat_local.membership import MembershipModel
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),model_backend="membership")
        process=Mock()
        process.communicate.return_value=(json.dumps({"success":False,"error_code":"membership_model_json_invalid",
                                            "message":"完成事件后的模型文字不是有效JSON。"}),"")
        process.returncode=1
        process.poll.return_value=1
        model=MembershipModel(settings,self.root)
        with patch.object(Path,"is_file",return_value=True),patch("wechat_local.cloud.subprocess.Popen",return_value=process):
            with self.assertRaises(RuntimeError):
                model.infer("合成材料")
        self.assertEqual(model.last_error_code,"membership_model_json_invalid")

    def test_transport_diagnostic_survives_child_without_unknown_fields(self):
        from wechat_local.membership import MembershipModel
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),model_backend="membership")
        process=Mock(returncode=1)
        process.poll.return_value=1
        process.communicate.return_value=(json.dumps({"success":False,"error_code":"membership_delivery_uncertain",
            "message":"请求交付未确定。","transport_diagnostic":{"phase":"request_send",
            "request_may_have_been_sent":True,"reason":"tls_eof","token":"PRIVATE_TOKEN","prompt":"PRIVATE_CHAT"}}),"")
        model=MembershipModel(settings,self.root)
        with patch.object(Path,"is_file",return_value=True),patch("wechat_local.cloud.subprocess.Popen",return_value=process) as launch:
            with self.assertRaises(RuntimeError):model.infer("合成材料")
        self.assertEqual(launch.call_count,1)
        self.assertEqual(model.last_transport_diagnostic,{"phase":"request_send","request_may_have_been_sent":True,"reason":"tls_eof"})

    def test_connection_retry_count_is_persisted_with_specific_failure(self):
        from wechat_local.summary import SummaryError
        from wechat_local.common import atomic_json,read_json
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),model_backend="membership")
        job=settings.data_dir/"jobs"/("d"*32)
        atomic_json(job/"manifest.json",{"messages_file":str(job/"snapshot"/"messages.jsonl")})
        report=settings.data_dir/"reports"/"synthetic"/"result.json"
        partial={"result_file":str(report),"status":"failed","error_code":"local_inference_failed"}
        atomic_json(report,partial)
        events=[{"error_code":"membership_connect_reset","attempt":2}, {"error_code":"membership_connect_reset","attempt":3}]
        diagnostic={"phase":"tls_handshake","request_may_have_been_sent":False}
        model=Mock(last_error="会员接口连接被重置或关闭。",last_error_code="membership_connect_reset",retry_events=events,last_transport_diagnostic=diagnostic)
        with patch("wechat_local.membership.MembershipModel",return_value=model),\
             patch("wechat_local.summary.summarize_selection",side_effect=SummaryError("local_inference_failed",partial)):
            with self.assertRaises(SummaryError):
                execute(SimpleNamespace(command="generate",job="d"*32,allow_cloud=True),settings,lambda _:None)
        self.assertEqual(read_json(report)["connection_retries"],events)
        self.assertEqual(read_json(report)["error_code"],"membership_connect_reset")
        self.assertEqual(read_json(report)["transport_diagnostic"],diagnostic)
        self.assertNotIn("合成材料",model.last_error)

    def test_cloud_timeout_kills_its_request_process(self):
        import subprocess
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),cloud_timeout=30)
        process=Mock()
        process.communicate.side_effect=[subprocess.TimeoutExpired("synthetic",45),("","")]
        process.poll.return_value=0
        with patch.object(Path,"is_file",return_value=True),patch("wechat_local.cloud.subprocess.Popen",return_value=process):
            with self.assertRaises(RuntimeError): CloudModel(settings,self.root).infer("合成超时测试")
        process.kill.assert_called_once()

    def test_synthetic_test_uses_builtin_text_without_provider(self):
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),model_backend="cloud")
        captured=[]
        def summarize(manifest,reports,infer,progress,**kwargs):
            rows=[json.loads(line) for line in Path(manifest["messages_file"]).read_text(encoding="utf-8").splitlines()]
            captured.extend(rows)
            self.assertEqual(manifest["total_text"],3)
            return {"result_file":"synthetic-result"}
        with patch("wechat_local.provider.WechatProvider",side_effect=AssertionError("must not read")),\
             patch("wechat_local.cloud.CloudModel"),patch("wechat_local.summary.summarize_selection",side_effect=summarize):
            result=execute(SimpleNamespace(command="cloud-test",job="b"*32,allow_cloud=True),settings,lambda _:None)
        self.assertTrue(result["synthetic_test"])
        self.assertEqual(len(captured),3)
        self.assertTrue(all(row["text"].startswith("内置虚构材料：") for row in captured))
        self.assertFalse((self.root/"jobs"/("b"*32)/"snapshot").exists())


if __name__=="__main__":
    unittest.main()
