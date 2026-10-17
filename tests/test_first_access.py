"""Synthetic first-access checks. No real WeChat process or credentials."""
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local.builtin_provider import account_identity
from wechat_local.key_access import AccessError,capture,matches_page,acquire_database
from wechat_local.local_runtime import initialize
from wechat_local.process_job import ProcessJob


class AccessTests(unittest.TestCase):
    def device(self,messages=None,load_error=False):
        device=Mock();session=device.attach.return_value;script=session.create_script.return_value
        handler=[]
        script.on.side_effect=lambda event,callback:handler.append(callback)
        def load():
            if load_error:raise RuntimeError('synthetic secret must not escape')
            for payload in messages or []:handler[0]({'type':'send','payload':payload},None)
        script.load.side_effect=load
        return device,session,script

    def test_capture_validates_only_expected_and_detaches_no_output(self):
        good=bytes(range(32));bad=bytes(32)
        device,session,script=self.device(['KEY:'+bad.hex(),'warning private memory address','KEY:'+good.hex()])
        output=io.StringIO()
        import contextlib
        with contextlib.redirect_stdout(output):
            result=capture(device,42,lambda value:value==good,stop=lambda:False,deadline=time.monotonic()+1,progress=lambda phase:None)
        self.assertEqual(result,good)
        self.assertEqual(output.getvalue(),'')
        session.detach.assert_called_once();script.unload.assert_called_once()

    def test_cancel_timeout_error_and_detach_failure(self):
        for cancelled,error in ((True,None),(False,'ERR: secret details')):
            device,session,script=self.device([error] if error else [])
            with self.assertRaises(KeyboardInterrupt if cancelled else AccessError):
                capture(device,1,lambda value:False,stop=lambda:cancelled,deadline=time.monotonic()+.2,progress=lambda phase:None)
            session.detach.assert_called_once()
        device,session,script=self.device()
        with self.assertRaisesRegex(AccessError,'超时'):
            capture(device,1,lambda value:False,stop=lambda:False,deadline=time.monotonic()-.1,progress=lambda phase:None)
        session.detach.assert_called_once()
        device,session,script=self.device(load_error=True)
        with self.assertRaises(AccessError) as raised:
            capture(device,1,lambda value:False,stop=lambda:False,deadline=time.monotonic()+1,progress=lambda phase:None)
        self.assertNotIn('secret',str(raised.exception));session.detach.assert_called_once()
        device,session,script=self.device(['KEY:'+bytes(32).hex()]);session.detach.side_effect=RuntimeError()
        with self.assertRaises(AccessError) as raised:
            capture(device,1,lambda value:True,stop=lambda:False,deadline=time.monotonic()+1,progress=lambda phase:None)
        self.assertEqual(raised.exception.code,'access_detach_failed')

    def test_no_consent_has_no_dependency_or_process_side_effects(self):
        with patch('wechat_local.key_access.inspect_executable') as inspect:
            with self.assertRaises(AccessError):acquire_database(None,None,None,None)
        inspect.assert_not_called()

    def test_owner_and_device_suffix(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'wxid_synthetic_ab12';root.mkdir()
            self.assertEqual(account_identity(root),'wxid_synthetic')
            self.assertEqual(account_identity(root,'wxid_synthetic'),'wxid_synthetic')
            for wrong in ('wxid_other','wxid_synthetic_ab12'):
                with self.assertRaises(ValueError):account_identity(root,wrong)

    def test_page_authentication_rejects_wrong_key_and_corruption(self):
        from test_builtin_provider import encrypted_database,KEY
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'sample.db';encrypted_database(path,['CREATE TABLE sample(x TEXT)'])
            page=path.read_bytes()[:4096]
            self.assertTrue(matches_page(KEY,page))
            self.assertFalse(matches_page(bytes(32),page))
            self.assertFalse(matches_page(KEY,page[:-1]+bytes([page[-1]^1])))

    def test_runtime_initialization_is_scoped_idempotent_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);base=root/'python-base';base.mkdir();prefix=root/'.venv';scripts=prefix/'Scripts';scripts.mkdir(parents=True)
            for name in ('python.exe','pythonw.exe','python312.dll'):(base/name).write_bytes(name.encode())
            with patch('wechat_local.local_runtime.sys.prefix',str(prefix)),patch('wechat_local.local_runtime.sys.base_prefix',str(base)):
                self.assertEqual(initialize(root),(scripts/'python-local-only.exe').resolve())
                self.assertEqual(initialize(root),(scripts/'python-local-only.exe').resolve())
                (scripts/'python-local-only.exe').write_bytes(b'previous image')
                with self.assertRaises(ValueError):initialize(root)
                self.assertEqual((scripts/'python-local-only.exe').read_bytes(),b'previous image')
            with self.assertRaises(ValueError):initialize(root)

    def test_failed_process_creation_and_pipe_write_close_job(self):
        kernel=Mock();kernel.CreateJobObjectW.return_value=123;kernel.SetInformationJobObject.return_value=True;kernel.AssignProcessToJobObject.return_value=True
        with patch('wechat_local.process_job.ctypes.WinDLL',return_value=kernel),patch('wechat_local.process_job.subprocess.Popen',side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):ProcessJob(['not-a-real-program'],None)
        kernel.CloseHandle.assert_called_once_with(123)
        kernel.CloseHandle.reset_mock()
        process=Mock();process._handle=77;process.poll.return_value=None;process.stdin.write.side_effect=BrokenPipeError
        with patch('wechat_local.process_job.ctypes.WinDLL',return_value=kernel),patch('wechat_local.process_job.subprocess.Popen',return_value=process):
            with self.assertRaises(BrokenPipeError):ProcessJob(['synthetic'],None)
        kernel.CloseHandle.assert_called_once_with(123);process.kill.assert_called_once();process.wait.assert_called_once_with(10)

    def test_ui_opening_and_unchecked_start_never_extract(self):
        import tkinter as tk
        from wechat_local.access_ui import AccessDialog
        from wechat_local.image_access_ui import ImageAccessDialog
        root=tk.Tk();root.withdraw()
        self.addCleanup(root.destroy)
        app=SimpleNamespace(settings=SimpleNamespace(provider_backend='builtin'),selected=set(),invalidate=Mock())
        with patch('wechat_local.access_ui.connect_database') as database,patch('wechat_local.image_access_ui.acquire_images') as images:
            dialog=AccessDialog(app,root)
            dialog.start()
            self.assertFalse(dialog.busy)
            self.assertIn('确认',dialog.status.get())
            dialog.close()
            image=ImageAccessDialog(app,root)
            image.start()
            self.assertFalse(image.busy)
            image.close()
        database.assert_not_called();images.assert_not_called()

    def test_ui_close_cancels_worker_and_waits_for_cleanup(self):
        import tkinter as tk
        from wechat_local.access_ui import AccessDialog
        root=tk.Tk();root.withdraw()
        self.addCleanup(root.destroy)
        app=SimpleNamespace(settings=SimpleNamespace(provider_backend='builtin'),selected=set(),invalidate=Mock())
        dialog=AccessDialog(app,root)
        entered=threading.Event();finished=threading.Event()
        def acquire(*args,stop,**kwargs):
            entered.set()
            while not stop():time.sleep(.01)
            finished.set()
            raise KeyboardInterrupt()
        with patch('wechat_local.access_ui.connect_database',side_effect=acquire):
            dialog.consent.set(True)
            dialog.start()
            self.assertTrue(entered.wait(1))
            dialog.close()
            self.assertTrue(dialog.window.winfo_exists())
            self.assertTrue(finished.wait(1))
            dialog.thread.join(1)
            dialog.poll()
            self.assertFalse(dialog.window.winfo_exists())
        for callback in root.tk.call('after','info'):root.after_cancel(callback)


if __name__=='__main__':unittest.main()
