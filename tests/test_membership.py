"""Synthetic OAuth keys/tokens and streaming events; no OpenAI login or traffic."""
from dataclasses import replace
import io
from http.client import IncompleteRead
import json
from pathlib import Path
import sys
import tempfile
import time
import socket
import ssl
from urllib.error import URLError
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock
from cryptography.hazmat.primitives.asymmetric import rsa
import jwt

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from wechat_local.membership import AUTH,API,MembershipError,MembershipModel,Store,validate_callback,validate_identity,read_response_stream,dispatch,http,connection_failure,InferenceHTTPSHandler
from wechat_local.settings import Settings
from wechat_local.worker import execute


class MembershipTests(unittest.TestCase):
    def setUp(self):
        (ROOT/"tmp").mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/"tmp",prefix="membership-test-")
        self.root=Path(self.temp.name)
        self.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        self.jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwk["kid"]="synthetic-key"
        self.claims={"iss":AUTH,"aud":"synthetic-client","sub":"synthetic-user","nonce":"synthetic-nonce",
                     "iat":int(time.time()),"exp":int(time.time())+3600}

    def tearDown(self):
        self.temp.cleanup()

    def tokens(self,**updates):
        claims={**self.claims,**updates}
        return {"id_token":jwt.encode(claims,self.key,algorithm="RS256",headers={"kid":"synthetic-key"}),
                "access_token":"synthetic-access","token_type":"Bearer","scope":"openid resource.invoke chatgpt.tokens.use.direct"}

    def verify(self,tokens):
        with patch("wechat_local.membership.http",side_effect=[{"issuer":AUTH,"jwks_uri":AUTH+"/jwks"},{"keys":[self.jwk]}]):
            return validate_identity(tokens,"synthetic-client","synthetic-nonce")

    def test_signature_issuer_audience_expiry_and_nonce(self):
        self.assertEqual(self.verify(self.tokens())[0]["sub"],"synthetic-user")
        for updates in ({"iss":"https://example.invalid"},{"aud":"other-client"},{"exp":int(time.time())-100},
                        {"nonce":"wrong-nonce"}):
            with self.assertRaises(MembershipError): self.verify(self.tokens(**updates))
        self.jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa.generate_private_key(public_exponent=65537,key_size=2048).public_key()))
        self.jwk["kid"]="synthetic-key"
        with self.assertRaises(MembershipError): self.verify(self.tokens())

    def test_identity_without_plan_scope_is_not_enough(self):
        tokens=self.tokens()
        tokens["scope"]="openid profile email"
        with self.assertRaises(MembershipError): self.verify(tokens)

    def test_callback_state_client_and_denial(self):
        query={"state":["state"],"code":["synthetic-code"],"client_id":["issued-client"]}
        self.assertEqual(validate_callback(query,"state"),("issued-client","synthetic-code"))
        for value in ({**query,"state":["wrong"]},{**query,"error":["access_denied"]},
                      {**query,"client_id":["dynamic_agent_client"]},{**query,"state":["state","state"]}):
            with self.assertRaises(MembershipError): validate_callback(value,"state")
        with self.assertRaises(MembershipError): validate_callback(query,"state","other-client")

    def stream(self,*events):
        return io.BytesIO("".join("data: "+json.dumps(event)+"\n\n" for event in events).encode())

    def test_stream_requires_terminal_completed(self):
        delta={"type":"response.output_text.delta","delta":'{"items":[]}'}
        done={"type":"response.completed","response":{"status":"completed"}}
        self.assertEqual(read_response_stream(self.stream(delta,done)),'{"items":[]}')
        for event in ({"type":"response.failed"},{"type":"response.incomplete"},{"type":"error"}):
            with self.assertRaises(MembershipError): read_response_stream(self.stream(delta,event))
        with self.assertRaises(MembershipError): read_response_stream(self.stream(delta))

    def test_store_dpapi_and_path_boundary(self):
        store=Store(self.root)
        account="a"*64
        record={"email":"synthetic@example.invalid","access_token":"synthetic-access","client_id":"client","subject":"user"}
        store.save(account,record)
        self.assertNotIn("synthetic-access",store.path(account).read_text(encoding="utf-8"))
        self.assertEqual(store.read(account),record)
        self.assertEqual(store.host(),store.host())
        with self.assertRaises(MembershipError): store.read("../../outside")

    def test_completed_http_stream_with_invalid_model_json_has_specific_code(self):
        response=self.stream({"type":"response.output_text.delta","delta":"not-json-synthetic"},
                             {"type":"response.completed","response":{"status":"completed"}})
        with self.assertRaises(MembershipError) as caught:
            read_response_stream(response)
        self.assertEqual(caught.exception.code,"membership_model_json_invalid")
        self.assertNotIn("not-json-synthetic",str(caught.exception))

    def test_interrupted_stream_and_bad_sse_are_distinct(self):
        with self.assertRaises(MembershipError) as caught:
            read_response_stream(self.stream({"type":"response.output_text.delta","delta":'{}'}))
        self.assertEqual(caught.exception.code,"membership_stream_interrupted")
        with self.assertRaises(MembershipError) as caught:
            read_response_stream(io.BytesIO(b'data: not-json\n\n'))
        self.assertEqual(caught.exception.code,"membership_sse_json_invalid")

    def test_server_incomplete_and_failed_do_not_claim_quota_failure(self):
        for kind in ("response.failed","response.incomplete"):
            with self.assertRaises(MembershipError) as caught:
                read_response_stream(self.stream({"type":kind}))
            self.assertEqual(caught.exception.code,"membership_"+kind.replace(".","_"))
            self.assertNotIn("额度",str(caught.exception))

    def test_refresh_rotates_tokens_and_keeps_catalog(self):
        store=Store(self.root)
        account="b"*64
        store.save(account,{"client_id":"client","subject":"user","access_token":"old-access","refresh_token":"old-refresh",
                           "expires_at":0,"scopes":["resource.invoke","chatgpt.tokens.use.direct"]},[{"slug":"synthetic-model","name":"Synthetic"}])
        with patch("wechat_local.membership.http",return_value={"access_token":"new-access","refresh_token":"new-refresh","expires_in":3600}) as call:
            self.assertEqual(store.token(account),"new-access")
        self.assertNotIn("scope",call.call_args.kwargs["form"])
        self.assertEqual(store.read(account)["refresh_token"],"new-refresh")
        self.assertEqual(store.state()["accounts"][0]["models"][0]["slug"],"synthetic-model")

    def test_member_generate_still_requires_per_task_consent(self):
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),model_backend="membership")
        with self.assertRaises(ValueError): execute(SimpleNamespace(command="generate",job="a"*32),settings,lambda _:None)

    def test_responses_request_has_only_supported_fields(self):
        store=Store(self.root)
        account="c"*64
        store.save(account,{"access_token":"synthetic-access","expires_at":time.time()+3600},[{"slug":"synthetic-model","name":"Synthetic"}])
        response=self.stream({"type":"response.output_text.delta","delta":'{"items":[]}'},
                             {"type":"response.completed","response":{"status":"completed"}})
        with patch("wechat_local.membership.http",return_value=response) as call:
            result=dispatch({"data_dir":str(self.root),"account":account,"action":"infer","prompt":"合成测试",
                             "model":"synthetic-model","timeout":30})
        self.assertTrue(result["success"])
        payload=call.call_args.kwargs["payload"]
        self.assertFalse(payload["store"])
        self.assertTrue(payload["stream"])
        self.assertEqual(call.call_args.args[0],API+"/responses")
        self.assertEqual(set(payload),{"model","store","stream","instructions","input"})

    def test_connection_diagnostics_are_classified_without_private_exception_text(self):
        for error,reason in ((URLError(socket.gaierror(-2,"PRIVATE_HOST")),"dns"),
                             (URLError(TimeoutError("PRIVATE_HOST")),"timeout"),
                             (ConnectionResetError("PRIVATE_HOST"),"reset"),
                             (OSError(10054,"PRIVATE_HOST"),"reset"),
                             (ConnectionRefusedError("PRIVATE_HOST"),"refused"),
                             (OSError(10060,"PRIVATE_HOST"),"timeout"),
                             (ssl.SSLCertVerificationError("PRIVATE_HOST"),"certificate"),
                             (ssl.SSLEOFError("PRIVATE_HOST"),"tls_eof"),
                             (ssl.SSLError("PRIVATE_HOST"),"tls")):
            classified=connection_failure(error)
            self.assertEqual(classified.code,"membership_connect_"+reason)
            self.assertNotIn("PRIVATE_HOST",str(classified))

    def test_only_inference_open_errors_get_retryable_codes_not_token_refresh(self):
        with patch("wechat_local.membership.build_opener") as opener:
            opener.return_value.open.side_effect=URLError(ConnectionResetError("PRIVATE_BODY"))
            for url,stream,expected in ((API+"/responses",True,"membership_connect_reset"),
                                        (AUTH+"/api/accounts/oauth/token",False,"membership_transport_failed")):
                with self.assertRaises(MembershipError) as caught:
                    http(url,payload={},stream=stream)
                self.assertEqual(caught.exception.code,expected)
                self.assertNotIn("PRIVATE_BODY",str(caught.exception))

    def test_transient_connect_failure_retries_same_request_then_succeeds(self):
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),cloud_timeout=30)
        model=MembershipModel(settings,self.root)
        events=[]
        model.retry_callback=events.append
        calls=[]
        def call(request,timeout):
            calls.append(request)
            if len(calls)<3:
                model.last_error_code="membership_connect_reset"
                model.last_error="connection reset"
                raise RuntimeError("controlled")
            return {"content":'{"items":[]}'}
        with patch.object(model,"call",side_effect=call),patch("wechat_local.membership.time.sleep") as sleep:
            self.assertEqual(model.infer("合成输入"),'{"items":[]}')
        self.assertEqual(len(calls),3)
        self.assertEqual([c["prompt"] for c in calls],["合成输入"]*3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[5,15])
        self.assertEqual([e["attempt"] for e in events],[2,3])
        self.assertEqual(model.last_error_code,"")
        self.assertEqual(len(model.retry_history),2)
        with patch.object(model,"call",return_value={"content":'{"items":[]}'}):
            model.infer("另一段合成输入")
        self.assertEqual(model.retry_events,[])
        self.assertEqual(len(model.retry_history),2)

    def test_connection_retries_are_bounded_and_auth_quota_stream_errors_not_replayed(self):
        settings=Settings(*(self.root for _ in range(4)),model="synthetic")
        for code,count in (("membership_connect_reset",5),("membership_http_401",1),
                           ("membership_http_403",1),("membership_http_429",1),
                           ("membership_connect_certificate",1),("membership_stream_transport_failed",1),
                           ("membership_stream_interrupted",1),("membership_model_json_invalid",1),
                           ("membership_transport_failed",1),("membership_delivery_uncertain",1)):
            model=MembershipModel(settings,self.root)
            def call(*args,**kwargs):
                model.last_error_code=code
                raise RuntimeError("controlled")
            with patch.object(model,"call",side_effect=call) as request,patch("wechat_local.membership.time.sleep"):
                with self.assertRaises(RuntimeError): model.infer("合成输入")
            self.assertEqual(request.call_count,count,code)

    def test_retry_budget_expiration_and_cancellation_stop_without_another_call(self):
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic",),cloud_timeout=30)
        model=MembershipModel(settings,self.root)
        def fail(*args,**kwargs):
            model.last_error_code="membership_connect_reset"
            raise RuntimeError("controlled")
        with patch.object(model,"call",side_effect=fail) as request,\
             patch("wechat_local.membership.time.monotonic",side_effect=[0,0,30]),patch("wechat_local.membership.time.sleep") as sleep:
            with self.assertRaises(RuntimeError): model.infer("合成输入")
        self.assertEqual(request.call_count,1)
        sleep.assert_not_called()
        with patch.object(model,"call",side_effect=KeyboardInterrupt) as request:
            with self.assertRaises(KeyboardInterrupt): model.infer("合成输入")
        self.assertEqual(request.call_count,1)

    def test_handshake_eof_and_post_send_eof_have_different_replay_boundaries(self):
        error=ssl.SSLEOFError("PRIVATE_BODY_TOKEN")
        safe=connection_failure(error,{"phase":"tls_handshake","request_may_have_been_sent":False})
        self.assertEqual(safe.code,"membership_connect_tls_eof")
        uncertain=connection_failure(error,{"phase":"response_headers","request_may_have_been_sent":True})
        self.assertEqual(uncertain.code,"membership_delivery_uncertain")
        self.assertNotIn("PRIVATE",str(uncertain))
        self.assertEqual(uncertain.diagnostic["reason"],"tls_eof")

    def test_inference_handler_tracks_handshake_and_caps_connect_timeout(self):
        handler=InferenceHTTPSHandler({"request_may_have_been_sent":False})
        with patch.object(handler,"do_open") as open_http:
            handler.https_open(Mock())
        connection_type=open_http.call_args.args[0]
        self.assertIs(open_http.call_args.kwargs["context"],handler._context)
        self.assertTrue(handler._context.check_hostname)
        self.assertEqual(handler._context.verify_mode,ssl.CERT_REQUIRED)
        connection=connection_type("synthetic.invalid",timeout=180,context=handler._context)
        create_socket=connection._create_connection=Mock(return_value=Mock())
        def fail_tls(conn):
            conn._create_connection((conn.host,conn.port),conn.timeout)
            raise ssl.SSLEOFError("PRIVATE_HOST")
        with patch("wechat_local.membership.HTTPSConnection.connect",new=fail_tls):
            with self.assertRaises(ssl.SSLEOFError): connection.send(b"PRIVATE_REQUEST")
        self.assertEqual(create_socket.call_args.args[1],15)
        self.assertEqual(handler.diagnostic["phase"],"tls_handshake")
        self.assertFalse(handler.diagnostic["request_may_have_been_sent"])

    def test_inference_handler_marks_send_as_uncertain_before_socket_write(self):
        handler=InferenceHTTPSHandler({"request_may_have_been_sent":False})
        with patch.object(handler,"do_open") as opened:handler.https_open(Mock())
        conn=opened.call_args.args[0]("synthetic.invalid",timeout=30)
        conn.sock=Mock()
        conn.sock.sendall.side_effect=ssl.SSLEOFError("PRIVATE_REQUEST")
        with self.assertRaises(ssl.SSLEOFError):conn.send(b"PRIVATE_REQUEST")
        self.assertEqual(handler.diagnostic["phase"],"request_send")
        self.assertTrue(handler.diagnostic["request_may_have_been_sent"])
        self.assertNotIn("PRIVATE",str(handler.diagnostic))

    def test_connect_limit_does_not_shorten_model_response_timeout(self):
        handler=InferenceHTTPSHandler({})
        with patch.object(handler,"do_open") as opened:handler.https_open(Mock())
        conn=opened.call_args.args[0]("synthetic.invalid",timeout=180)
        sock=Mock()
        sock.version.return_value="TLSv1.3"
        def connected(value):value.sock=sock
        with patch("wechat_local.membership.HTTPSConnection.connect",new=connected):conn.connect()
        self.assertEqual(conn.timeout,180)
        sock.settimeout.assert_called_once_with(180)
        self.assertEqual(handler.diagnostic["tls_version"],"TLSv1.3")

    def test_proxy_connect_failure_does_not_mark_selected_data_as_sent(self):
        handler=InferenceHTTPSHandler({"request_may_have_been_sent":False})
        with patch.object(handler,"do_open") as opened:handler.https_open(Mock())
        conn=opened.call_args.args[0]("proxy.invalid",timeout=30)
        conn.set_tunnel("synthetic.invalid")
        raw_socket=Mock()
        conn._create_connection=Mock(return_value=raw_socket)
        def proxy_failed(value):
            value.sock=value._create_connection((value.host,value.port),value.timeout)
            value.send(b"CONNECT synthetic.invalid:443 HTTP/1.1\r\n\r\n")
            raise ConnectionResetError("PRIVATE_PROXY")
        with patch("wechat_local.membership.HTTPSConnection.connect",new=proxy_failed):
            with self.assertRaises(ConnectionResetError):conn.send(b"PRIVATE_CHAT_REQUEST")
        raw_socket.sendall.assert_called_once()
        self.assertEqual(handler.diagnostic["phase"],"proxy_tunnel")
        self.assertFalse(handler.diagnostic["request_may_have_been_sent"])

    def test_incomplete_http_stream_keeps_non_replayable_error_and_phase(self):
        account="e"*64
        store=Store(self.root)
        store.save(account,{"access_token":"synthetic-access","expires_at":time.time()+3600},[{"slug":"synthetic-model"}])
        response=io.BytesIO()
        response.transport_diagnostic={"phase":"response_stream","request_may_have_been_sent":True}
        with patch("wechat_local.membership.http",return_value=response),\
             patch("wechat_local.membership.read_response_stream",side_effect=IncompleteRead(b"PRIVATE_TEXT")):
            with self.assertRaises(MembershipError) as caught:
                dispatch({"data_dir":str(self.root),"account":account,"action":"infer","model":"synthetic-model","prompt":"合成输入","timeout":30})
        self.assertEqual(caught.exception.code,"membership_stream_transport_failed")
        self.assertEqual(caught.exception.diagnostic["phase"],"response_stream")
        self.assertNotIn("PRIVATE",str(caught.exception))

    def test_short_outage_beyond_old_three_attempt_limit_can_recover(self):
        settings=replace(Settings(*(self.root for _ in range(4)),model="synthetic"),cloud_timeout=180)
        model=MembershipModel(settings,self.root)
        attempts=[]
        def call(request,timeout):
            attempts.append(request["prompt"])
            if len(attempts)<5:
                model.last_error_code="membership_connect_tls_eof"
                model.last_transport_diagnostic={"phase":"tls_handshake","request_may_have_been_sent":False}
                raise RuntimeError("controlled")
            return {"content":'{"items":[]}'}
        with patch.object(model,"call",side_effect=call),patch("wechat_local.membership.time.sleep") as sleep:
            self.assertEqual(model.infer("合成测试"),'{"items":[]}')
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[5,15,30,45])
        self.assertEqual(attempts,["合成测试"]*5)
        self.assertEqual(len(model.retry_history),4)
        self.assertEqual(model.retry_history[-1]["transport_diagnostic"]["phase"],"tls_handshake")

    def test_revocation_failure_clears_only_this_app_session(self):
        store=Store(self.root)
        account="d"*64
        store.save(account,{"client_id":"client","subject":"user","access_token":"synthetic-access","refresh_token":"synthetic-refresh","id_token":"synthetic-id"})
        with patch("wechat_local.membership.http",side_effect=MembershipError("synthetic network failure")):
            result=dispatch({"data_dir":str(self.root),"account":account,"action":"logout"})
        self.assertFalse(result["revoked"])
        record=store.read(account)
        self.assertNotIn("access_token",record)
        self.assertEqual(record["client_id"],"client")


if __name__=="__main__":
    unittest.main()
