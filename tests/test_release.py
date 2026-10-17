"""Public contract checks, using synthetic settings and source-only manifests."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"src"))
from wechat_local import __version__
from wechat_local.provider import create_provider, WechatProvider
from wechat_local.builtin_provider import BuiltinProvider
from wechat_local.settings import Settings


class ReleaseTests(unittest.TestCase):
    def test_settings_preserve_existing_external_and_allow_builtin_member(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"settings.json"
            data={"data_dir":temp,"model_backend":"membership","provider_dir":temp}
            path.write_text(json.dumps(data))
            settings=Settings.load(path)
            self.assertIsInstance(create_provider(settings,Path(temp)/"cache"),WechatProvider)
            del data["provider_dir"]
            data["provider_backend"]="builtin"
            path.write_text(json.dumps(data))
            self.assertIsInstance(create_provider(Settings.load(path),Path(temp)/"cache"),BuiltinProvider)

    def test_build_and_docs_share_current_version(self):
        spec=importlib.util.spec_from_file_location("build_public_source",ROOT/"scripts/build_public_source.py")
        build=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(build)
        self.assertEqual(build.SOURCE_VERSION,"v"+__version__)
        content=build.payloads(ROOT)
        self.assertIn("README.md",content)
        self.assertNotIn("README.public.md",content)
        for name in ("README.md","docs/public-install.md","docs/distribution.md","skills/wechat-local-summary/SKILL.md"):
            self.assertIn(__version__,content[name].decode("utf-8"))
        self.assertTrue(all(not name.lower().endswith((".dll",".exe",".db",".gguf",".log")) for name in content))
        self.assertIn("third_party/wechat-decrypt/LICENSE",content)

    def test_first_run_reader_controls_without_private_data(self):
        import tkinter as tk
        from tkinter import ttk
        from wechat_local.gui import App
        with tempfile.TemporaryDirectory() as temp:
            root=tk.Tk()
            root.withdraw()
            try:
                app=App(root,Path(temp)/"settings.json")
                app.configure()
                def walk(widget):
                    yield widget
                    for child in widget.winfo_children():
                        yield from walk(child)
                nodes=list(walk(root))
                self.assertTrue(any(isinstance(w,ttk.Button) and w.cget("text")=="验证并导入数据库密钥（仅本机）" for w in nodes))
                self.assertTrue(any(isinstance(w,ttk.Checkbutton) and "本人账号" in w.cget("text") for w in nodes))
                advanced=next(w for w in nodes if isinstance(w,ttk.LabelFrame) and w.cget("text")=="高级手动接入（通常不用）")
                self.assertEqual(advanced.winfo_manager(),"")
                self.assertTrue(any(isinstance(w,ttk.Button) and w.cget("text")=="连接微信（自动识别）" for w in nodes))
                self.assertFalse((Path(temp)/"reader/authorization.json").exists())
            finally:
                for callback in root.tk.call("after","info"):root.after_cancel(callback)
                root.update_idletasks()
                root.destroy()


if __name__=="__main__":
    unittest.main()
