"""Synthetic guidance lifecycle; no credentials, WeChat or model requests."""
import json
from pathlib import Path
import sys
import tempfile
import tkinter as tk
from unittest.mock import patch
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local.gui import App
from wechat_local.onboarding import load_state, STEPS


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=Path(self.temp.name)/"settings.json"
        self.state=self.config.with_suffix(".onboarding.json")
        self.root=tk.Tk()
        self.root.withdraw()
        self.app=App(self.root,self.config)

    def tearDown(self):
        for pending in self.root.tk.call("after","info"):
            self.root.after_cancel(pending)
        self.root.update_idletasks()
        self.root.destroy()
        self.temp.cleanup()

    def test_first_start_once_and_button_resumes_without_work(self):
        with patch.object(self.app,"run") as run,patch.object(self.app,"refresh") as refresh:
            self.app.show_onboarding(automatic=True)
            guide=self.app.onboarding
            self.assertEqual(guide.step,0)
            self.assertFalse(self.config.exists())
            self.assertEqual(set(load_state(self.state)),{"version","seen","step","completed"})
            guide.move(1)
            guide.move(1)
            guide.pause()
            self.app.show_onboarding(automatic=True)
            self.assertFalse(guide.window.winfo_exists())
            self.app.onboarding_button.invoke()
            self.assertEqual(self.app.onboarding.step,2)
            self.app.onboarding.move(-1)
            self.assertEqual(load_state(self.state)["step"],1)
        run.assert_not_called()
        refresh.assert_not_called()
        self.assertEqual(list(Path(self.temp.name).iterdir()),[self.state])

    def test_finish_is_not_validation_and_manual_restart_starts_at_one(self):
        self.app.show_onboarding()
        guide=self.app.onboarding
        for _ in range(len(STEPS)-1):
            guide.next.invoke()
        self.assertEqual(guide.step,len(STEPS)-1)
        self.assertIn("不代表",guide.body.get("1.0","end"))
        guide.next.invoke()
        self.assertTrue(load_state(self.state)["completed"])
        self.assertFalse(guide.window.winfo_exists())
        self.assertIsNone(self.app.settings)
        self.app.show_onboarding(automatic=True)
        self.assertFalse(guide.window.winfo_exists())
        self.app.show_onboarding()
        self.assertEqual(self.app.onboarding.step,0)

    def test_location_opens_correct_settings_or_only_focuses(self):
        self.app.show_onboarding()
        guide=self.app.onboarding
        with patch.object(self.app,"configure") as configure,patch.object(self.app,"run") as run:
            guide.locate()
            configure.assert_called_once_with(initial_tab="model")
            guide.move(1)
            guide.locate()
            configure.assert_called_with(initial_tab="reader")
            for _ in range(4):
                guide.move(1)
                guide.locate()
            self.assertEqual(configure.call_count,2)
        run.assert_not_called()
        self.assertEqual(self.app.selected,set())
        self.assertIsNone(self.app.prepared)

    def test_running_task_blocks_actions_and_duplicate_window(self):
        self.app.show_onboarding()
        guide=self.app.onboarding
        self.app.active=object()
        with patch.object(self.app,"configure") as configure:
            guide.locate()
            self.assertIn("任务正在运行",guide.notice.get())
            configure.assert_not_called()
        self.app.show_onboarding()
        self.assertIs(self.app.onboarding,guide)
        self.app.active=None

    def test_invalid_state_and_unwritable_state_are_recoverable(self):
        for data in ("not json",json.dumps({"version":1,"seen":True,"step":999,"completed":False}),"[]"):
            self.state.write_text(data,encoding="utf-8")
            self.assertEqual(load_state(self.state),{})
        self.state.write_text("x"*4097,encoding="utf-8")
        self.assertEqual(load_state(self.state),{})
        with patch("wechat_local.onboarding.atomic_json",side_effect=PermissionError):
            self.app.show_onboarding(automatic=True)
            guide=self.app.onboarding
            self.assertIn("无法保存",guide.notice.get())
            guide.step=len(STEPS)-1
            guide.move(1)
            self.assertTrue(guide.window.winfo_exists())
        self.assertFalse(self.config.exists())

    def test_reader_and_model_actions_select_requested_settings_tab(self):
        from tkinter import ttk
        for name,expected in (("reader","微信首次接入"),("model","本机与媒体")):
            before=set(self.root.winfo_children())
            self.app.configure(initial_tab=name)
            window=next(w for w in self.root.winfo_children() if w not in before)
            def walk(widget):
                for child in widget.winfo_children():
                    yield child
                    yield from walk(child)
            book=next(w for w in walk(window) if isinstance(w,ttk.Notebook))
            self.assertEqual(book.tab(book.select(),"text"),expected)
            window.destroy()

    def test_small_window_keeps_navigation_and_scrollable_text(self):
        self.root.deiconify()
        self.app.show_onboarding()
        guide=self.app.onboarding
        guide.window.geometry(f"{self.app.pixels(520)}x{self.app.pixels(420)}")
        self.root.update()
        for button in (guide.next,guide.previous,guide.action):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),guide.window.winfo_rooty()+guide.window.winfo_height())
        self.assertEqual(guide.body.cget("wrap"),"char")
        self.assertEqual(guide.body.cget("state"),"disabled")


if __name__=="__main__":
    unittest.main()
