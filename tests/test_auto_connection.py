"""Synthetic automatic connection. No real WeChat, accounts or attachments."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from wechat_local.builtin_provider import BuiltinProvider
from wechat_local.common import atomic_json
from wechat_local.credentials import protect_key
from wechat_local.discovery import discover_accounts
from wechat_local.key_access import AccessError,connect_database


class AutomaticConnectionTests(unittest.TestCase):
    def setUp(self):
        from test_builtin_provider import encrypted_database,KEY
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name).resolve()
        self.account=self.base/'xwechat_files/wxid_synthetic_ab12'
        (self.account/'db_storage/message').mkdir(parents=True)
        encrypted_database(self.account/'db_storage/session/session.db',['CREATE TABLE SessionTable(username TEXT)'])
        encrypted_database(self.account/'db_storage/contact/contact.db',['CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT)'])
        self.key=KEY
        self.settings=SimpleNamespace(provider_backend='builtin',data_dir=self.base/'private')
        self.provider=BuiltinProvider(self.settings.data_dir,self.settings.data_dir/'cache')
        self.path=self.base/'Weixin.exe'
        self.detected=(self.path,(42,1,123.0),{self.account})

    def test_discovery_is_shallow_deduplicated_and_includes_active_custom_location(self):
        root=self.account.parent
        self.assertEqual(discover_accounts([root,root]),[self.account])
        self.assertEqual(discover_accounts([],observed=[self.account]),[self.account])
        # A top-level disk root is never recursively searched.
        self.assertEqual(discover_accounts([self.base]),[])
        self.assertEqual(discover_accounts([self.account/'db_storage']),[self.account])

    def test_no_consent_never_inspects_processes(self):
        with patch('wechat_local.discovery.running_wechat') as discover:
            with self.assertRaises(AccessError):connect_database(self.settings)
        discover.assert_not_called()

    def test_running_instance_supplies_executable_and_active_location(self):
        from wechat_local.discovery import running_wechat
        process=Mock();process.info={'pid':42,'name':'Weixin.exe'}
        process.username.return_value='synthetic-owner'
        process.exe.return_value=str(self.path)
        process.open_files.return_value=[SimpleNamespace(path=str(self.account/'db_storage/session/session.db'))]
        current=Mock();current.username.return_value='synthetic-owner'
        with patch('psutil.Process',return_value=current),\
             patch('psutil.process_iter',return_value=[process]),\
             patch('wechat_local.discovery.main_process',return_value=self.detected[1]):
            self.assertEqual(running_wechat(),self.detected)

    def test_saved_legacy_authorization_reuses_key_and_corrects_owner_without_frida(self):
        record={'root':str(self.account),'account':'wxid_synthetic_ab12','key':self.key.hex()}
        atomic_json(self.provider.authorization,{'version':1,'dpapi':protect_key(json.dumps(record))})
        with patch('wechat_local.discovery.running_wechat',return_value=self.detected),\
             patch('wechat_local.discovery.discover_accounts',return_value=[self.account]),\
             patch('wechat_local.key_access.inspect_executable',return_value=(self.path,'synthetic')),\
             patch('wechat_local.key_access.importlib.metadata.version') as dependency,\
             patch('wechat_local.key_access.capture') as capture:
            result=connect_database(self.settings,consent=True)
        self.assertTrue(result['reused']);dependency.assert_not_called();capture.assert_not_called()
        self.assertEqual(self.provider._access()[1],'wxid_synthetic')

    def test_first_connection_finds_key_account_without_any_path_argument(self):
        phases=[]
        def capture(device,pid,verify,**kwargs):
            self.assertEqual(pid,42)
            self.assertTrue(verify(self.key))
            return self.key
        with patch('wechat_local.discovery.running_wechat',return_value=self.detected),\
             patch('wechat_local.discovery.discover_accounts',return_value=[self.account]),\
             patch('wechat_local.key_access.inspect_executable',return_value=(self.path,'synthetic')),\
             patch('wechat_local.key_access.main_process',return_value=self.detected[1]),\
             patch('frida.get_local_device',return_value=Mock()),\
             patch('wechat_local.key_access.capture',side_effect=capture):
            result=connect_database(self.settings,consent=True,progress=phases.append)
        self.assertFalse(result['reused'])
        self.assertLess(phases.index('detached'),phases.index('validating'))
        self.assertEqual(self.provider._access()[0],self.account)

    def test_multiple_matching_accounts_are_not_chosen_by_newest_timestamp(self):
        other=self.base/'xwechat_files/wxid_other_cd34'
        (other/'db_storage/session').mkdir(parents=True)
        (other/'db_storage/session/session.db').write_bytes((self.account/'db_storage/session/session.db').read_bytes())
        detected=(self.path,self.detected[1],set())
        def capture(device,pid,verify,**kwargs):
            verify(self.key)
            self.fail('ambiguous account must not authorize')
        with patch('wechat_local.discovery.running_wechat',return_value=detected),\
             patch('wechat_local.discovery.discover_accounts',return_value=[other,self.account]),\
             patch('wechat_local.key_access.inspect_executable',return_value=(self.path,'synthetic')),\
             patch('wechat_local.key_access.main_process',return_value=self.detected[1]),\
             patch('frida.get_local_device',return_value=Mock()),\
             patch('wechat_local.key_access.capture',side_effect=capture):
            with self.assertRaises(AccessError) as error:connect_database(self.settings,consent=True)
        self.assertEqual(error.exception.code,'access_account_ambiguous')
        self.assertFalse(self.provider.authorization.exists())

    def test_cancel_waiting_for_normal_login_has_no_attachment(self):
        with patch('wechat_local.discovery.running_wechat',return_value=None),\
             patch('wechat_local.key_access.capture') as capture:
            with self.assertRaises(KeyboardInterrupt):connect_database(self.settings,consent=True,stop=lambda:True)
        capture.assert_not_called()

    def test_normal_connection_dialog_has_no_entries_or_file_chooser(self):
        import tkinter as tk
        from tkinter import ttk
        from wechat_local.access_ui import AccessDialog
        root=tk.Tk();root.withdraw()
        self.addCleanup(root.destroy)
        app=SimpleNamespace(settings=self.settings,selected=set(),invalidate=Mock())
        dialog=AccessDialog(app,root)
        def walk(widget):
            yield widget
            for child in widget.winfo_children():yield from walk(child)
        widgets=list(walk(dialog.window))
        self.assertFalse(any(isinstance(widget,ttk.Entry) for widget in widgets))
        self.assertEqual(sum(isinstance(widget,ttk.Checkbutton) for widget in widgets),1)
        self.assertEqual(dialog.start_button.cget('text'),'连接微信')
        dialog.close()

    def test_success_from_main_window_refreshes_chat_list_automatically(self):
        import tkinter as tk
        from wechat_local.access_ui import AccessDialog
        root=tk.Tk();root.withdraw()
        self.addCleanup(root.destroy)
        app=SimpleNamespace(root=root,settings=self.settings,selected=set(),invalidate=Mock(),refresh=Mock())
        dialog=AccessDialog(app,root)
        dialog.events.put(('done',{'success':True}))
        dialog.poll()
        self.assertFalse(dialog.window.winfo_exists())
        app.refresh.assert_called_once()


if __name__=='__main__':unittest.main()
