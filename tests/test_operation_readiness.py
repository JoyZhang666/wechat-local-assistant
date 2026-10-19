"""Synthetic operation boundaries: no live accounts, models or network."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local.account_guard import account_state,require_account
from wechat_local.history import HistoryError
from wechat_local.settings import Settings
from wechat_local.media import MediaResolver
from wechat_local.export import _annotation


class AccountGuardTests(unittest.TestCase):
    def setUp(self):
        self.root=Path("synthetic-account-a").resolve()
        self.provider=Mock()
        self.provider.account_data_root.return_value=self.root

    def test_matching_active_account_does_not_need_offline_consent(self):
        with patch("wechat_local.account_guard.running_wechat",return_value=(None,None,{self.root})):
            self.assertEqual(require_account(self.provider)["status"],"matched")

    def test_switched_or_multiple_accounts_rejected_even_with_old_consent(self):
        with patch("wechat_local.account_guard.running_wechat",return_value=None):
            binding=account_state(self.provider)["binding"]
        for observed in ({self.root.parent/"account-b"},{self.root,self.root.parent/"account-b"}):
            with patch("wechat_local.account_guard.running_wechat",return_value=(None,None,observed)):
                with self.assertRaises(HistoryError):require_account(self.provider,binding,True)

    def test_unknown_and_stopped_require_exact_bound_account_confirmation(self):
        for current in (None,(None,None,set())):
            with patch("wechat_local.account_guard.running_wechat",return_value=current):
                state=account_state(self.provider)
                with self.assertRaises(HistoryError):require_account(self.provider)
                with self.assertRaises(HistoryError):require_account(self.provider,"0"*64,True)
                self.assertEqual(require_account(self.provider,state["binding"],True),state)

    def test_rebound_authorization_invalidates_old_selection(self):
        with patch("wechat_local.account_guard.running_wechat",return_value=(None,None,{self.root})):
            with self.assertRaises(HistoryError):require_account(self.provider,"0"*64)

    def test_process_access_denied_never_silently_matches(self):
        with patch("wechat_local.account_guard.running_wechat",side_effect=RuntimeError()):
            self.assertEqual(account_state(self.provider)["status"],"unknown")
            with self.assertRaises(HistoryError):require_account(self.provider)


class ConfigurationTests(unittest.TestCase):
    def test_reader_and_membership_login_do_not_require_any_model(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"settings.json"
            path.write_text(json.dumps({"data_dir":temp,"provider_backend":"builtin"}))
            self.assertEqual(Settings.load(path,purpose="basic").model,"")
            Settings.load(path,purpose="reader")
            for purpose in ("summary","local","voice"):
                with self.assertRaises(ValueError):Settings.load(path,purpose=purpose)

    def test_cloud_settings_can_be_saved_incomplete_but_not_used_for_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"settings.json"
            path.write_text(json.dumps({"data_dir":temp,"provider_backend":"builtin","model_backend":"cloud"}))
            Settings.load(path,purpose="basic")
            Settings.load(path,purpose="reader")
            with self.assertRaises(ValueError):Settings.load(path,purpose="summary")

    def test_external_reader_configuration_only_required_for_reading(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"settings.json"
            path.write_text(json.dumps({"data_dir":temp,"provider_backend":"external"}))
            Settings.load(path,purpose="basic")
            with self.assertRaises(ValueError):Settings.load(path,purpose="reader")

    def test_unselected_recognition_is_missing_not_error_and_never_uses_model(self):
        resolver=MediaResolver(Mock(),None,Path("unused"),settings=SimpleNamespace(data_dir=Path("unused")),images=False,voice=False)
        for kind in ("image","voice"):
            result=_annotation({"id":"synthetic","kind":kind},[{"path":"unused"}],resolver.annotate)
            self.assertEqual(result["status"],"missing")
            self.assertEqual(result["data"]["error_code"],"recognition_not_requested")
        resolver.close()


class ReadinessGuiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from wechat_local.gui import App
        self.temp=tempfile.TemporaryDirectory()
        self.root=tk.Tk()
        self.root.withdraw()
        self.app=App(self.root,Path(self.temp.name)/"settings.json")

    def tearDown(self):
        for pending in self.root.tk.call("after","info"):self.root.after_cancel(pending)
        self.root.update_idletasks()
        self.root.destroy()
        self.temp.cleanup()

    def test_first_member_login_has_no_ollama_or_provider_requirement(self):
        with patch("wechat_local.cloud.ensure_cloud_interpreter"),\
             patch("wechat_local.gui.messagebox.askyesno",return_value=True),\
             patch.object(self.app,"run") as run:
            self.app.member_login()
        self.assertEqual(run.call_args.args[0],"member-login")
        self.assertEqual(self.app.settings.model,"")
        self.assertEqual(self.app.settings.provider_backend,"builtin")

    def test_account_mismatch_clears_old_choices_and_never_reads(self):
        self.app.need_settings()
        self.app.chats=[{"id":"a","name":"Synthetic","is_group":False,"last_time":0}]
        self.app.selected={"a"}
        with patch("wechat_local.account_guard.account_state",return_value={"account":"synthetic-a","binding":"a"*64,"status":"mismatch"}),\
             patch("wechat_local.gui.messagebox.showinfo"),patch.object(self.app,"run") as run:
            self.app.refresh()
        self.assertFalse(self.app.chats)
        self.assertFalse(self.app.selected)
        run.assert_not_called()

    def test_offline_decline_does_not_read_or_claim_current_account(self):
        self.app.need_settings()
        with patch("wechat_local.account_guard.account_state",return_value={"account":"synthetic-a","binding":"a"*64,"status":"unknown"}),\
             patch("wechat_local.gui.messagebox.askyesno",return_value=False),patch.object(self.app,"run") as run:
            self.app.refresh()
        self.assertIn("未确认",self.app.account_label.get())
        run.assert_not_called()

    def test_model_free_export_selection_keeps_media_flags_off(self):
        from wechat_local.common import read_json
        self.app.need_settings()
        self.app.selected={"synthetic-a"}
        with patch.object(self.app,"check_account",return_value=True),patch.object(self.app,"run") as run:
            self.app.export_records()
        selection=read_json(self.app.settings.data_dir/"jobs"/run.call_args.args[1]/"selection.json")
        self.assertFalse(selection["recognize_images"])
        self.assertFalse(selection["recognize_voice"])
        self.assertEqual(run.call_args.args[0],"export")


if __name__=="__main__":unittest.main()
