"""Synthetic Windows lifecycle and selection checks; never reads WeChat."""
import contextlib
from dataclasses import replace
from datetime import datetime,timezone,timedelta
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from tkinter import ttk
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch
import psutil

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local.common import BusyError, NamedMutex, atomic_json, read_json
from wechat_local.gui import App, parse_range
from wechat_local.process_job import ProcessJob
from wechat_local.settings import Settings
from wechat_local.worker import clean_snapshot, emit, task_path


class WorkflowTests(unittest.TestCase):
    def test_stdout_excludes_private_material(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            emit({"phase":"ready","chat_count":2,"chat_name":"synthetic-private-name",
                  "message":"synthetic-private-text","key":"not-a-real-key"})
        self.assertEqual(json.loads(output.getvalue()),{"phase":"ready","chat_count":2})

    def test_fixed_timezone_and_end_exclusion_contract(self):
        a,b=parse_range("2026-10-09 09:00","2026-10-09 10:00")
        self.assertEqual(b-a,3600)
        self.assertEqual(a,1791507600)
        with self.assertRaises(ValueError):
            parse_range("2026-10-09 10:00","2026-10-09 09:00")

    def test_job_identifier_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            settings=Settings(*(Path(temp) for _ in range(4)),model="synthetic")
            for value in ("../other","", "a"*31,"a"*32+"/../other"):
                with self.assertRaises(ValueError):
                    task_path(settings,value)
            self.assertEqual(task_path(settings,"a"*32),(Path(temp)/"jobs"/("a"*32)).resolve())

    def test_snapshot_cleanup_keeps_reports_and_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            job=Path(temp)/"job"
            atomic_json(job/"snapshot"/"nested"/"selected.json",{"synthetic":True})
            atomic_json(job/"model-owner.json",{"stopped":True})
            atomic_json(job/"report.json",{"synthetic":True})
            clean_snapshot(job)
            self.assertFalse((job/"snapshot").exists())
            self.assertTrue((job/"report.json").is_file())
            self.assertTrue((job/"model-owner.json").is_file())

    def test_other_process_cannot_take_mutex(self):
        name=r"Local\WechatSyntheticMutexTest"
        code="import sys;sys.path.insert(0,sys.argv[1]);from wechat_local.common import NamedMutex,BusyError\ntry:\n with NamedMutex(sys.argv[2]): print('unexpected')\nexcept BusyError: print('busy')"
        import subprocess
        with NamedMutex(name):
            result=subprocess.run([sys.executable,"-B","-c",code,str(Path(__file__).resolve().parents[1]/"src"),name],capture_output=True,text=True)
        self.assertEqual(result.stdout.strip(),"busy")

    def test_cancel_terminates_worker_and_descendant(self):
        code="import sys,subprocess,time,json;sys.stdin.readline();p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);print(json.dumps({'pid':p.pid}),flush=True);time.sleep(60)"
        with tempfile.TemporaryFile() as log:
            job=ProcessJob([sys.executable,"-B","-c",code],log)
            try:
                child=json.loads(job.process.stdout.readline())["pid"]
                self.assertTrue(psutil.pid_exists(child))
                job.cancel()
                for _ in range(40):
                    if not psutil.pid_exists(child): break
                    time.sleep(.05)
                self.assertFalse(psutil.pid_exists(child))
                self.assertIsNotNone(job.process.poll())
            finally:
                job.close()
                job.process.stdout.close()


class NativeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=tk.Tk()
        self.root.withdraw()
        self.app=App(self.root,Path(self.temp.name)/"absent-config.json")
        self.app.chats=[{"id":"room@chatroom","name":"�ظ�����","is_group":True,"last_time":0},
                        {"id":"single-person","name":"�ظ�����","is_group":False,"last_time":0}]
        self.app.render_chats()

    def tearDown(self):
        for pending in self.root.tk.call("after","info"):
            self.root.after_cancel(pending)
        self.root.update_idletasks()
        self.root.destroy()
        self.temp.cleanup()

    def test_no_chat_selected_implicitly_and_duplicate_names_keep_ids(self):
        self.assertEqual(self.app.selected,set())
        self.assertEqual(set(self.app.tree.get_children()),{"room@chatroom","single-person"})
        labels=[self.app.tree.item(x,"values")[1] for x in self.app.tree.get_children()]
        self.assertEqual(len(set(labels)),2)
        self.app.kind.set("Ⱥ��")
        self.app.render_chats()
        self.app.select_visible()
        self.assertEqual(self.app.selected,{"room@chatroom"})

    def test_range_change_invalidates_and_cleans_only_prepared_job(self):
        self.app.settings=Settings(*(Path(self.temp.name) for _ in range(4)),model="synthetic")
        self.app.prepared="a"*32
        with patch.object(self.app,"cleanup_job") as clean:
            self.app.start.set("2026-10-09 09:00")
        clean.assert_called_once_with("a"*32)
        self.assertIsNone(self.app.prepared)
        self.assertEqual(str(self.app.generate_button["state"]),"disabled")

    def test_close_cleans_prepared_snapshot(self):
        self.app.settings=Settings(*(Path(self.temp.name) for _ in range(4)),model="synthetic")
        self.app.prepared="b"*32
        with patch.object(self.app,"cleanup_job") as clean,patch.object(self.root,"destroy"):
            self.app.close()
        clean.assert_called_once_with("b"*32)

    def cloud_preview(self):
        self.app.settings=replace(Settings(*(Path(self.temp.name) for _ in range(4)),model="synthetic"),
                                  model_backend="cloud",cloud_base_url="https://api.example.invalid/v1",cloud_model="synthetic-cloud")
        self.app.prepared="c"*32
        atomic_json(Path(self.temp.name)/"jobs"/self.app.prepared/"manifest.json",
                    {"chats":[{"name":"�ϳ�Ⱥ"}],"total_text":3})

    def test_cloud_decline_does_not_send_or_destroy_preview(self):
        self.cloud_preview()
        with patch("wechat_local.gui.messagebox.askyesno",return_value=False),patch.object(self.app,"run") as run:
            self.app.generate()
        run.assert_not_called()
        self.assertEqual(self.app.prepared,"c"*32)

    def test_cloud_confirmation_is_only_for_this_generate(self):
        self.cloud_preview()
        with patch("wechat_local.gui.messagebox.askyesno",return_value=True) as ask,patch.object(self.app,"run") as run:
            self.app.generate()
            self.app.generate()
        self.assertEqual(ask.call_count,2)
        self.assertIn("3������",ask.call_args.args[1])
        run.assert_called_with("generate","c"*32,allow_cloud=True)

    def test_local_generate_does_not_request_cloud_permission(self):
        self.app.settings=Settings(*(Path(self.temp.name) for _ in range(4)),model="synthetic")
        self.app.prepared="d"*32
        with patch("wechat_local.gui.messagebox.askyesno") as ask,patch.object(self.app,"run") as run:
            self.app.generate()
        ask.assert_not_called()
        run.assert_called_once_with("generate","d"*32)

    def test_membership_confirmation_mentions_shared_plan(self):
        self.cloud_preview()
        self.app.settings=replace(self.app.settings,model_backend="membership",membership_model="synthetic-member-model")
        with patch("wechat_local.gui.messagebox.askyesno",return_value=True) as ask,patch.object(self.app,"run") as run:
            self.app.generate()
        self.assertIn("���üƻ����",ask.call_args.args[1])
        self.assertIn("synthetic-member-model",ask.call_args.args[1])
        run.assert_called_once_with("generate","c"*32,allow_cloud=True)

    def test_selected_login_account_is_passed_to_worker_not_overwritten(self):
        self.app.settings=Settings(*(Path(self.temp.name) for _ in range(4)),model="synthetic")
        with patch("wechat_local.cloud.ensure_cloud_interpreter"),patch("wechat_local.gui.messagebox.askyesno",return_value=True),\
             patch.object(self.app,"run") as run:
            self.app.member_login(account="a"*64)
        self.assertEqual(run.call_args.kwargs["member_account"],"a"*64)
        self.assertEqual(self.app.settings.membership_account,"")

    def test_full_export_requires_exactly_one_chat(self):
        self.assertEqual(str(self.app.record_button["state"]),"disabled")
        self.app.selected={"room@chatroom"}
        self.app.render_chats()
        self.assertEqual(str(self.app.record_button["state"]),"normal")
        self.app.selected.add("single-person")
        self.app.render_chats()
        self.assertEqual(str(self.app.record_button["state"]),"disabled")
        self.assertEqual(str(self.app.export_summary_button["state"]),"disabled")

    def test_media_summary_preview_and_export_buttons_preserve_cloud_consent(self):
        self.cloud_preview()
        self.app.selected={"room@chatroom"}
        with patch.object(self.app,"run") as run:
            self.app.prepare()
        prepared=read_json(Path(self.temp.name)/"jobs"/run.call_args.args[1]/"selection.json")
        self.assertTrue(prepared["include_media"])
        with patch("wechat_local.gui.messagebox.askyesno",return_value=False) as ask,patch.object(self.app,"run") as run:
            self.app.export_records(summarize=True)
        run.assert_not_called()
        self.assertIn("ʶ��������תд",ask.call_args.args[1])
        with patch("wechat_local.gui.messagebox.askyesno") as ask,patch.object(self.app,"run") as run:
            self.app.export_records()
        ask.assert_not_called()
        self.assertEqual(run.call_args.args[0],"export")
        self.assertNotIn("summarize",run.call_args.kwargs)
        with patch("wechat_local.gui.messagebox.askyesno",return_value=True),patch.object(self.app,"run") as run:
            self.app.export_records(summarize=True)
        self.assertTrue(run.call_args.kwargs["summarize"])
        self.assertTrue(run.call_args.kwargs["allow_cloud"])
        self.assertEqual(self.app.model_heading["text"],"һ��ģ��")
        self.assertEqual(self.app.range_heading["text"],"����ʱ�䷶Χ")
        self.assertEqual(self.app.processing_heading["text"],"�ġ���ʼ����")
        self.assertEqual(self.app.result_title.get(),"�塢�鿴���")

    def test_missing_dedicated_interpreter_blocks_private_worker(self):
        with patch.object(self.app,"need_settings",return_value=True),patch.object(Path,"is_file",return_value=False),\
                patch("wechat_local.gui.messagebox.showinfo") as notice,patch("wechat_local.gui.ProcessJob") as worker:
            self.app.run("list")
        notice.assert_called_once()
        worker.assert_not_called()
        self.assertFalse(self.app.active)

    def synthetic_export(self):
        self.app.settings=Settings(*(Path(self.temp.name) for _ in range(4)),model="synthetic")
        directory=Path(self.temp.name)/"exports"/("a"*32)
        directory.mkdir(parents=True)
        atomic_json(directory/"manifest.json",{"chat":{"name":"�ϳ�Ⱥ"},"start":100,"end":200})
        (directory/"records.md").write_text("�ϳɼ�¼",encoding="utf-8")
        (directory/"index.html").write_text("<html>�ϳ��Ķ�ҳ</html>",encoding="utf-8")
        self.app.load_export(directory/"manifest.json")
        return directory

    def test_records_save_a_bundle_while_summary_stays_markdown(self):
        self.synthetic_export()
        self.assertIn('����Ԥ��',self.app.text.get('1.0','end'))
        self.assertEqual(self.app.export_button["text"],"����������¼��")
        with patch("wechat_local.gui.filedialog.askdirectory",return_value=self.temp.name),\
                patch("wechat_local.gui.filedialog.asksaveasfilename") as md,patch.object(self.app,"run") as run:
            self.app.export()
        md.assert_not_called()
        self.assertEqual(run.call_args.args,("save-export","a"*32))
        self.assertTrue(run.call_args.kwargs["destination"].name.startswith("�����¼_"))
        self.app.report_scope="reports"
        with patch("wechat_local.gui.filedialog.asksaveasfilename",return_value="") as md:
            self.app.export()
        md.assert_called_once()

    def test_settings_provide_local_image_key_tab_without_connecting(self):
        with patch.object(self.app,'run') as run:
            self.app.configure()
        window=next(w for w in self.root.winfo_children() if isinstance(w,tk.Toplevel))
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        books=[w for w in descendants(window) if isinstance(w,ttk.Notebook)]
        self.assertTrue(any('ͼƬ����' in [b.tab(t,'text') for t in b.tabs()] for b in books))
        key_button=next(w for w in descendants(window) if isinstance(w,ttk.Button) and w['text']=='����ͼƬ��Կ����������')
        with patch('wechat_local.image_keys.ImageKeyStore.save') as save,patch('wechat_local.gui.messagebox.showerror') as notice:
            key_button.invoke()
        save.assert_not_called()
        notice.assert_called_once()
        run.assert_not_called()
        window.grab_release();window.destroy()

    def test_browser_reading_uses_saved_directory_without_reading_chat(self):
        self.synthetic_export()
        saved=Path(self.temp.name)/"saved"
        saved.mkdir()
        (saved/"index.html").write_text("<html>�ϳ��Ķ�ҳ</html>",encoding="utf-8")
        self.app.saved_export_dir=saved
        with patch("os.startfile") as opened:
            self.app.open_reading()
        opened.assert_called_once_with(str(saved/"index.html"))

    def test_save_cancellation_preserves_other_prepared_selection(self):
        self.synthetic_export()
        self.app.prepared="b"*32
        self.app.job_id="a"*32
        self.app.pending_cancel=True
        self.app.active=SimpleNamespace(close=Mock())
        self.app.error_stream=Mock()
        self.app.events.put({"phase":"worker_exit","exit_code":1,"command":"save-export"})
        with patch.object(self.app,"cleanup_save") as save_clean,patch.object(self.app,"cleanup_job") as read_clean:
            self.app.poll()
        save_clean.assert_called_once_with("a"*32)
        read_clean.assert_not_called()
        self.assertEqual(self.app.prepared,"b"*32)

    def test_failure_remains_visible_after_worker_exit_and_resets_progress(self):
        self.app.settings=Settings(*(Path(self.temp.name) for _ in range(4)),model="synthetic")
        self.app.progress.configure(maximum=20,value=1)
        self.app.active=SimpleNamespace(close=Mock())
        self.app.error_stream=Mock()
        self.app.events.put({"phase":"failed","error_code":"unverified_model_quote"})
        self.app.events.put({"phase":"worker_exit","exit_code":1,"command":"generate"})
        with patch("wechat_local.gui.messagebox.showerror") as alert:
            self.app.poll()
        alert.assert_called_once()
        self.assertIn("������ԭ�Ĳ�һ��",self.app.status.get())
        self.assertEqual(self.app.progress["value"],0)
        self.assertIn("�ѽ���",self.app.activity.get())

    def test_progress_counts_completed_work_and_checkpoint_reuse(self):
        self.app.events.put({"phase":"extract","state":"start","current":2,"total":5,"working":3})
        self.app.poll()
        self.assertIn("�����2/5",self.app.status.get())
        self.assertIn("�ȴ�ģ����Ӧ",self.app.status.get())
        self.assertEqual(self.app.progress["value"],2)
        self.app.events.put({"phase":"extract","state":"done","current":3,"total":5,"reused":True})
        self.app.poll()
        self.assertIn("���ü���",self.app.status.get())

    def test_cache_only_generation_does_not_claim_new_cloud_send(self):
        self.app.events.put({"phase":"complete","model_backend":"membership","checkpoint_only":True})
        self.app.poll()
        self.assertIn("û������ģ������",self.app.status.get())

    def test_failed_job_explains_saved_progress_and_resume(self):
        self.app.settings=Settings(*(Path(self.temp.name) for _ in range(4)),model="synthetic")
        path=Path(self.temp.name)/"reports"/"synthetic"/"result.json"
        atomic_json(path,{"chats":[],"progress":{"phase":"extract","current":7,"total":12}})
        self.app.events.put({"phase":"failed","error_code":"invalid_evidence_id","result_path":str(path)})
        self.app.events.put({"phase":"worker_exit","exit_code":1,"command":"generate"})
        with patch("wechat_local.gui.messagebox.showerror"):
            self.app.poll()
        self.assertIn("֤�ݱ��",self.app.status.get())
        self.assertIn("7/12",self.app.status.get())
        self.assertIn("���㱣��",self.app.status.get())
        self.assertEqual(self.app.progress["value"],0)

    def test_task_status_is_visible_at_minimum_window_size(self):
        self.root.geometry("920x620")
        self.root.deiconify()
        self.app.status.set("����ʧ�ܣ��ϲ���©������ȡ��ʵ������ɱ��׶�7/12�Ρ���ͨ���ֶμ��㱣��������Ԥ����ͬ��Χ�ɼ�����")
        self.app.activity.set("����������180�룻��ǰ�׶�12�����½��ȡ�")
        self.root.update()
        for widget in (self.app.progress,self.app.status_label,self.app.activity_label):
            self.assertTrue(widget.winfo_ismapped())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),
                                 self.root.winfo_rooty()+self.root.winfo_height())

    def synthetic_summary(self):
        self.app.settings=Settings(*(Path(self.temp.name) for _ in range(4)),model="synthetic")
        directory=Path(self.temp.name)/"reports"/("e"*32)
        directory.mkdir(parents=True)
        (directory/"chat-001.md").write_text("# �ϳ�ժҪ\n\n- ���Դ��졣\n  - ���ݣ�M000001-P0001��ԭ�ģ��������ݡ�\n",encoding="utf-8")
        path=directory/"result.json"
        atomic_json(path,{"status":"complete","complete":True,"chats":[{"name":"�ϳ�","file":str(directory/"chat-001.md")} ]})
        self.app.load_results(path)
        return path

    def test_summary_buttons_and_muted_quote_preview(self):
        path=self.synthetic_summary()
        for button in (self.app.open_summary_button,self.app.summary_directory_button,self.app.save_summary_button):
            self.assertEqual(str(button["state"]),"normal")
        self.assertEqual(str(self.app.open_reading_button["state"]),"disabled")
        self.assertIn("  > ����",self.app.text.get("1.0","end"))
        self.assertTrue(self.app.text.tag_ranges("evidence"))
        self.assertIn(str(path.parent.resolve()),self.app.saved_location.get())

    def test_failed_report_clears_previous_label_and_shows_specific_reason(self):
        self.synthetic_summary()
        path=Path(self.temp.name)/"reports"/"failed"/"result.json"
        atomic_json(path,{"chats":[],"validation_error":{"reason":"text","item_index":2},
                          "progress":{"phase":"extract","current":32,"total":59}})
        self.app.events.put({"phase":"failed","error_code":"invalid_model_item","result_path":str(path)})
        with patch("wechat_local.gui.messagebox.showerror"):
            self.app.poll()
        self.assertEqual(self.app.result_chat.get(),"")
        self.assertEqual(self.app.reports,[])
        self.assertIn("600��",self.app.status.get())
        self.assertIn("32/59",self.app.status.get())
        self.assertEqual(str(self.app.open_summary_button["state"]),"disabled")

    def test_format_retry_progress_does_not_claim_checkpoint_saved(self):
        self.app.events.put({"phase":"extract","state":"retry","current":32,"total":59,"working":33})
        self.app.poll()
        self.assertIn("��33��",self.app.status.get())
        self.assertIn("���һ�β��",self.app.status.get())
        self.assertNotIn("�����ѱ���",self.app.status.get())

    def test_cloud_retry_progress_and_exhausted_failure_are_visible(self):
        self.app.events.put({"phase":"cloud-retry","message":"��Ա������ʱʧ�ܣ�2������ԣ���3/3�Σ�������ɷֶα��ֲ��䡣"})
        self.app.poll()
        self.assertIn("��3/3��",self.app.status.get())
        self.app.settings=Settings(*(Path(self.temp.name) for _ in range(4)),model="synthetic")
        path=Path(self.temp.name)/"reports"/"retry-failed"/"result.json"
        atomic_json(path,{"chats":[],"connection_retries":[{},{}],"progress":{"current":42,"total":58},
                          "transport_diagnostic":{"phase":"tls_handshake"}})
        self.app.events.put({"phase":"failed","error_code":"membership_connect_reset", "message":"��Ա�ӿ����ӱ����û�رա�","result_path":str(path)})
        with patch("wechat_local.gui.messagebox.showerror"):
            self.app.poll()
        self.assertIn("���Զ�����2��",self.app.status.get())
        self.assertIn("42/58",self.app.status.get())
        self.assertIn("���Ͻ׶Σ�TLS����",self.app.status.get())

    def test_open_summary_uses_offline_worker(self):
        self.synthetic_summary()
        with patch.object(self.app,"run") as run:
            self.app.open_summary()
        run.assert_called_once_with("render-summary","e"*32)

    def test_reading_ready_opens_page_without_starting_generation(self):
        path=self.synthetic_summary()
        self.app.events.put({"phase":"summary-ready","result_path":str(path)})
        with patch("os.startfile") as opened,patch.object(self.app,"generate") as generate:
            self.app.poll()
        opened.assert_called_once_with(str(path.parent.resolve()/"index.html"))
        generate.assert_not_called()

    def test_offline_startup_restores_history_without_refreshing_wechat(self):
        path=self.synthetic_summary()
        with patch.object(self.app,"refresh") as refresh,patch.object(self.app,"run") as run:
            self.app.restore_latest()
            self.app.show_history()
        refresh.assert_not_called()
        run.assert_not_called()
        self.assertEqual(self.app.result_path,path.resolve())
        for window in self.root.winfo_children():
            if isinstance(window,tk.Toplevel):
                window.destroy()

    def test_summary_save_cancellation_keeps_other_prepared_selection(self):
        self.synthetic_summary()
        self.app.prepared="b"*32
        self.app.job_id="e"*32
        self.app.pending_cancel=True
        self.app.active=SimpleNamespace(close=Mock())
        self.app.error_stream=Mock()
        self.app.events.put({"phase":"worker_exit","exit_code":1,"command":"save-summary"})
        with patch.object(self.app,"cleanup_save") as saved,patch.object(self.app,"cleanup_job") as reading:
            self.app.poll()
        saved.assert_called_once_with("e"*32)
        reading.assert_not_called()
        self.assertEqual(self.app.prepared,"b"*32)

    def test_offline_save_failure_keeps_other_prepared_selection(self):
        self.synthetic_summary()
        self.app.prepared="b"*32
        self.app.active_command="save-summary"
        self.app.events.put({"phase":"failed","error_code":"synthetic_save_failure"})
        with patch("wechat_local.gui.messagebox.showerror"):
            self.app.poll()
        self.assertEqual(self.app.prepared,"b"*32)

    def test_summary_actions_visible_at_minimum_window_size(self):
        self.synthetic_summary()
        self.app.toggle_result_more()
        self.root.geometry("920x620")
        self.root.deiconify()
        self.root.update()
        for button in (self.app.open_summary_button,self.app.summary_directory_button,self.app.save_summary_button):
            self.assertTrue(button.winfo_ismapped())
            self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())

    def test_workflow_layout_reflows_and_keeps_primary_actions_in_order(self):
        self.synthetic_summary()
        for width,height in ((920,620),(1160,820),(1600,960)):
            self.root.geometry(f"{width}x{height}")
            self.root.deiconify()
            self.root.update()
            self.assertLess(self.app.config_button.winfo_rooty(),self.app.refresh_button.winfo_rooty())
            self.assertLess(self.app.chat_panel.winfo_rootx(),self.app.work_panel.winfo_rootx())
            self.assertEqual(self.app.model_heading.winfo_rooty(),self.app.range_heading.winfo_rooty())
            self.assertEqual(self.app.column_divider.winfo_height(),self.app.workspace.winfo_height())
            self.assertEqual(self.app.column_divider.winfo_rooty(),self.app.workspace.winfo_rooty())
            self.assertIs(self.app.config_button.master.master,self.app.chat_panel)
            self.assertIs(self.app.task_status.master,self.root)
            self.assertLess(self.app.range_controls[0].winfo_rooty(),self.app.preview_button.winfo_rooty())
            self.assertLess(self.app.preview_button.winfo_rootx(),self.app.generate_button.winfo_rootx())
            self.assertLess(self.app.generate_button.winfo_rooty(),self.app.open_summary_button.winfo_rooty())
            self.assertLess(self.app.open_summary_button.winfo_rooty(),self.app.status_label.winfo_rooty())
            for widget in (self.app.config_button,self.app.preview_button,self.app.generate_button,self.app.open_summary_button,self.app.result_more_button):
                self.assertTrue(widget.winfo_ismapped())
                self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
                self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
            self.assertGreaterEqual(self.app.text.winfo_height(),60)
            for button in self.app.range_controls[2:]:
                self.assertTrue(button.winfo_ismapped())
                self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),self.app.time_section.winfo_rootx()+self.app.time_section.winfo_width())

    def test_button_affordances_distinguish_available_disabled_and_primary(self):
        style=ttk.Style(self.root)
        for name in ("TButton","Quiet.TButton","Primary.TButton"):
            available=style.lookup(name,"background",("!disabled","!active","!pressed"))
            disabled=style.lookup(name,"background",("disabled",))
            self.assertNotEqual(available,"#ffffff")
            self.assertNotEqual(available,disabled)
            self.assertGreaterEqual(int(style.lookup(name,"borderwidth")),1)
            self.assertNotEqual(style.lookup(name,"foreground",("!disabled",)),style.lookup(name,"foreground",("disabled",)))
        self.assertNotEqual(style.lookup("Primary.TButton","background"),style.lookup("Quiet.TButton","background"))
        self.assertTrue(self.app.generate_button.instate(("disabled",)))
        self.assertFalse(self.app.preview_button.instate(("disabled",)))

    def test_selection_controls_have_larger_targets_and_label_can_select_radio(self):
        self.root.deiconify()
        probe=tk.Toplevel(self.root)
        probe.geometry("400x160")
        combo=ttk.Combobox(probe,state="readonly",values=("�ϳ�ѡ��",))
        combo.pack()
        nearby=ttk.Button(probe,text="��ʷժҪ",style="Quiet.TButton")
        nearby.pack()
        selected=tk.StringVar(value="other")
        radio=ttk.Radiobutton(probe,text="�����һ���п�ѡ��",variable=selected,value="chosen")
        radio.pack(fill="x")
        self.root.update()
        self.assertLessEqual(abs(combo.winfo_height()-nearby.winfo_height()),self.app.pixels(4))
        arrows=sum(combo.identify(x,combo.winfo_height()//2)=="downarrow" for x in range(combo.winfo_width()))
        self.assertGreaterEqual(arrows,self.app.pixels(16))
        self.assertLessEqual(arrows,self.app.pixels(20))
        self.assertGreaterEqual(radio.winfo_height(),self.app.pixels(40))
        radio.event_generate("<ButtonPress-1>",x=radio.winfo_width()-8,y=radio.winfo_height()//2)
        radio.event_generate("<ButtonRelease-1>",x=radio.winfo_width()-8,y=radio.winfo_height()//2)
        self.root.update()
        self.assertEqual(selected.get(),"chosen")
        combo.destroy()
        radio.destroy()
        probe.destroy()

    def test_48_and_72_hour_presets_use_rolling_beijing_time_and_invalidate_preview(self):
        fixed=datetime(2026,10,15,0,31,48,tzinfo=timezone(timedelta(hours=8)))
        labels=[str(button["text"]) for button in self.app.range_controls[2:]]
        self.assertIn("��ȥ48Сʱ",labels)
        self.assertIn("��ȥ72Сʱ",labels)
        for hours in (48,72):
            self.app.prepared="synthetic-old-preview"
            self.app.generate_button.configure(state="normal")
            with patch("wechat_local.gui.datetime") as clock:
                clock.now.return_value=fixed
                self.app.preset(str(hours))
            start,end=parse_range(self.app.start.get(),self.app.end.get())
            self.assertEqual(end-start,hours*3600)
            self.assertEqual(self.app.end.get(),"2026-10-15 00:31")
            self.assertIsNone(self.app.prepared)
            self.assertTrue(self.app.generate_button.instate(("disabled",)))

    def test_usage_text_wraps_with_width_and_guide_is_separate(self):
        self.root.geometry("920x620")
        self.root.deiconify()
        self.root.update()
        narrow=int(self.app.guide_label["wraplength"])
        narrow_width=self.app.guide_label.master.winfo_width()
        self.assertNotIn("\n",self.app.guide_label["text"])
        self.root.geometry("1600x960")
        self.root.update()
        width_growth=self.app.guide_label.master.winfo_width()-narrow_width
        self.assertGreater(width_growth,0)
        self.assertAlmostEqual(int(self.app.guide_label["wraplength"])-narrow,width_growth,delta=2)
        with patch.object(self.app,"run") as run:
            self.app.toggle_guide()
            self.app.guide_window.geometry("640x440")
            self.root.update()
        run.assert_not_called()
        self.assertTrue(self.app.guide_window.winfo_exists())
        self.assertTrue(all("\n" not in text for text in self.app.guide_paragraphs))
        self.app.guide_window.geometry("540x360")
        self.root.update()
        self.assertTrue(self.app.guide_text.winfo_ismapped())
        self.assertEqual(self.app.guide_text["wrap"],"char")
        self.app.guide_window.destroy()

    def test_task_tabs_keep_size_and_selection_emphasis_when_switched(self):
        self.root.deiconify()
        self.root.update()
        book=self.app.task_tabs
        sizes=[]
        with patch.object(self.app,"run") as run:
            for selected in (self.app.summary_task,self.app.export_task):
                book.select(selected)
                self.root.update()
                bounds=[]
                for index in (0,1):
                    xs=[x for x in range(0,book.winfo_width(),4) if book.identify(x,8) and book.index(f"@{x},8")==index]
                    x=xs[len(xs)//2]
                    ys=[y for y in range(book.winfo_height()) if book.identify(x,y) in ("tab","padding","focus","label") and book.index(f"@{x},{y}")==index]
                    height=max(ys)-min(ys)+1
                    self.assertGreaterEqual(height,self.app.preview_button.winfo_height())
                    bounds.append((min(xs),max(xs),height))
                sizes.append(bounds)
        run.assert_not_called()
        self.assertEqual(sizes[0],sizes[1])
        style=ttk.Style(self.root)
        self.assertNotEqual(style.lookup("TNotebook.Tab","background",("selected",)),style.lookup("TNotebook.Tab","background",("!selected",)))
        self.assertEqual(style.lookup("TNotebook.Tab","foreground",("selected",)),"#ffffff")

    def test_five_sections_separate_time_processing_and_results(self):
        self.root.deiconify()
        self.root.update()
        self.assertIs(self.app.task_tabs.master,self.app.processing_section)
        self.assertIs(self.app.result_actions.master,self.app.results_section)
        for before,after in ((self.app.time_section,self.app.processing_section),(self.app.processing_section,self.app.results_section)):
            self.assertGreater(after.winfo_rooty(),before.winfo_rooty()+before.winfo_height())
        style=ttk.Style(self.root)
        self.assertGreaterEqual(int(style.lookup("Panel.TFrame","borderwidth")),1)
        self.assertIn("���ӷ�ʽ",self.app.config_button["text"])
        self.assertIn("ChatGPT",self.app.member_login_button["text"])
        self.assertIn("ʾ��",self.app.cloud_test_button["text"])

    def test_fixed_footer_keeps_progress_and_dynamic_text_visible(self):
        self.root.geometry("920x660")
        self.root.deiconify()
        self.root.update()
        self.assertTrue(self.app.activity_label.winfo_ismapped())
        self.assertIn("û������",self.app.activity.get())
        self.assertGreaterEqual(self.app.progress.winfo_height(),self.app.pixels(12))
        self.assertGreater(self.app.status_label.winfo_rooty(),self.app.progress.winfo_rooty()+self.app.progress.winfo_height())
        self.assertGreater(self.app.activity_label.winfo_rooty(),self.app.status_label.winfo_rooty())
        self.assertGreater(self.app.task_status.winfo_width(),self.app.work_panel.winfo_width())
        self.app.events.put({"phase":"extract","state":"start","current":7,"total":12,"working":8})
        self.app.poll()
        self.root.update()
        self.assertIn("�����7/12",self.app.status.get())
        self.assertEqual(self.app.progress["value"],7)
        self.assertTrue(self.app.status_label.winfo_ismapped())

    def test_secondary_result_actions_are_disclosed_without_starting_work(self):
        self.synthetic_summary()
        self.root.deiconify()
        self.root.update()
        self.assertFalse(self.app.summary_directory_button.winfo_ismapped())
        self.assertFalse(self.app.export_button.winfo_ismapped())
        with patch.object(self.app,"run") as run:
            self.app.toggle_result_more()
            self.root.update()
        run.assert_not_called()
        self.assertTrue(self.app.summary_directory_button.winfo_ismapped())
        self.assertTrue(self.app.export_button.winfo_ismapped())
        self.assertTrue(self.app.saved_location_entry.winfo_ismapped())
        self.app.toggle_result_more()
        self.root.update()
        self.assertFalse(self.app.saved_location_entry.winfo_ismapped())

    def test_summary_and_record_tasks_are_distinct_and_preview_has_heading_style(self):
        path=self.synthetic_summary()
        original=(path.parent/"chat-001.md").read_bytes()
        self.root.deiconify()
        self.root.update()
        self.assertFalse(self.app.record_button.winfo_ismapped())
        self.assertTrue(self.app.text.tag_ranges("title"))
        self.assertNotIn("# �ϳ�ժҪ",self.app.text.get("1.0","end"))
        self.app.task_tabs.select(self.app.export_task)
        self.root.update()
        self.assertTrue(self.app.record_button.winfo_ismapped())
        self.assertFalse(self.app.generate_button.winfo_ismapped())
        self.assertEqual((path.parent/"chat-001.md").read_bytes(),original)

    def test_larger_font_scaling_keeps_workflow_visible(self):
        old_scale=float(self.root.tk.call("tk","scaling"))
        try:
            for scale in (5/3,2.0,8/3):
                self.root.tk.call("tk","scaling",scale)
                window=tk.Tk()
                try:
                    window.tk.call("tk","scaling",scale)
                    app=App(window,Path(self.temp.name)/"absent-config.json")
                    combo=ttk.Combobox(window)
                    nearby=ttk.Button(window,text="��ʷժҪ",style="Quiet.TButton")
                    radio=ttk.Radiobutton(window,text="�ϳ�ģʽ")
                    window.update_idletasks()
                    self.assertLessEqual(abs(combo.winfo_reqheight()-nearby.winfo_reqheight()),app.pixels(4))
                    self.assertGreaterEqual(radio.winfo_reqheight(),app.pixels(40))
                    window.geometry("1160x820")
                    window.update()
                    for widget in (app.config_button,app.range_controls[0],app.preview_button,app.generate_button,app.open_summary_button,app.status_label):
                        self.assertTrue(widget.winfo_ismapped())
                        self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),window.winfo_rootx()+window.winfo_width())
                        self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),window.winfo_rooty()+window.winfo_height())
                    self.assertGreaterEqual(app.text.winfo_height(),60)
                finally:
                    for pending in window.tk.call("after","info"):
                        window.after_cancel(pending)
                    window.destroy()
        finally:
            self.root.tk.call("tk","scaling",old_scale)

    def test_browser_open_failure_preserves_task_polling(self):
        path=self.synthetic_summary()
        self.app.events.put({"phase":"summary-ready","result_path":str(path)})
        with patch("os.startfile",side_effect=OSError("synthetic")),patch("wechat_local.gui.messagebox.showerror"):
            self.app.poll()
        self.assertIn("�Ķ�ҳδ�ܴ�",self.app.status.get())


if __name__=="__main__":
    unittest.main()
