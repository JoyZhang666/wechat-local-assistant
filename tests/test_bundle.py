"""Synthetic portable records, never real chats or model inference."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local.bundle import cleanup_saved_export, proposed_directory, save_export_bundle
from wechat_local.export import ExportCancelled,ExportError,write_export
from wechat_local.settings import Settings
from wechat_local.worker import execute
from wechat_local.process_job import ProcessJob


class BundleTests(unittest.TestCase):
    def setUp(self):
        temporary=Path(__file__).resolve().parents[1]/"tmp"
        temporary.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix="bundle-test-",dir=temporary)
        self.root=Path(self.temp.name)
        self.original=self.root/"original"
        self.input=self.root/"input.jsonl"
        self.owner=self.root/"job"/"save-owner.json"
        self.target=self.root/"中文 路径"/"saved"
        self.target.parent.mkdir()
        self.rows=[]
        self.media={}

    def tearDown(self):
        self.temp.cleanup()

    def build(self,media=False):
        kinds=["text","image","voice","video","file","emoji"] if media else ["text"]
        suffixes={"image":".png","voice":".wav","video":".mp4","file":".pdf","emoji":".gif"}
        for index,kind in enumerate(kinds,1):
            row={"id":str(index),"chat_id":"synthetic-room","timestamp":100+index,"sender":"合成人员",
                 "kind":kind,"text":"仅合成记录" if kind=="text" else "","raw_payload":"<msg>synthetic</msg>"}
            self.rows.append(row)
            if kind in suffixes:
                path=self.root/(kind+suffixes[kind])
                path.write_bytes(("synthetic-"+kind).encode())
                self.media[row["id"]]=[{"path":str(path),"kind":kind,"quality":"original","name":"合成 "+path.name}]
        self.input.write_text("".join(json.dumps(x,ensure_ascii=False)+"\n" for x in self.rows),encoding="utf-8")
        manifest={"chat":{"id":"synthetic-room","name":"合成:/群?"},"start":100,"end":200,
                  "total_messages":len(self.rows),"messages_file":str(self.input)}
        return write_export(manifest,self.original,resolve_media=lambda x:self.media.get(x["id"],[]),
            annotate_media=lambda x,a:{"status":"complete","transcript":"合成转写"} if x["kind"]=="voice"
                else {"status":"complete","ocr_text":"合成图片文字"})

    def save(self,**kwargs):
        return save_export_bundle(self.original,self.target,owner_file=self.owner,**kwargs)

    def test_move_retains_every_original_file_and_links(self):
        manifest=self.build(media=True)
        (self.original/"debug.log").write_text("not included")
        (self.original/"media"/"unlisted.bin").write_text("not included")
        originals={p.relative_to(self.original).as_posix():p.read_bytes() for p in self.original.rglob("*") if p.is_file()
                   and p.name not in ("debug.log","unlisted.bin")}
        result=self.save()
        self.assertTrue(result["success"])
        shutil.rmtree(self.original)  # Only this owned synthetic source is removed.
        saved={p.relative_to(self.target).as_posix():p.read_bytes() for p in self.target.rglob("*") if p.is_file()}
        self.assertEqual(saved,originals)
        for assets in manifest["media"].values():
            for asset in assets:
                self.assertEqual(hashlib.sha256((self.target/asset["relative_path"]).read_bytes()).hexdigest(),asset["sha256"])
                self.assertIn(asset["relative_path"],(self.target/"records.md").read_text(encoding="utf-8"))
                self.assertIn(asset["relative_path"],(self.target/"index.html").read_text(encoding="utf-8"))

    def test_partial_content_is_preserved_not_reported_complete(self):
        self.build()
        manifest=json.loads((self.original/"manifest.json").read_text(encoding="utf-8"))
        manifest.update(content_complete=False,completeness="partial",media_missing=["synthetic-missing"])
        (self.original/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
        self.assertFalse(self.save()["complete"])
        self.assertEqual(json.loads((self.target/"manifest.json").read_text(encoding="utf-8")),manifest)

    def test_existing_destination_and_nested_destination_are_rejected(self):
        self.build()
        self.target.mkdir()
        marker=self.target/"keep.txt"
        marker.write_text("keep")
        with self.assertRaises(ExportError): self.save()
        self.assertEqual(marker.read_text(),"keep")
        with self.assertRaises(ExportError):
            save_export_bundle(self.original,self.original/"nested",owner_file=self.owner)

    def test_hash_failure_preserves_source_and_removes_stage(self):
        manifest=self.build(media=True)
        asset=next(iter(manifest["media"].values()))[0]
        (self.original/asset["relative_path"]).write_bytes(b"changed")
        with self.assertRaises(ExportError): self.save()
        self.assertTrue(self.original.is_dir())
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob(".wechat-save-*")),[])

    def test_copy_failure_and_cancel_never_publish_partial(self):
        self.build()
        with self.assertRaises(OSError),patch("wechat_local.bundle._digest",side_effect=OSError("synthetic disk full")):
            self.save()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob(".wechat-save-*")),[])
        cancelled=[False]
        def progress(event): cancelled[0]=True
        with self.assertRaises(ExportCancelled): self.save(progress=progress,cancelled=lambda:cancelled[0])
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob(".wechat-save-*")),[])

    def test_manifest_path_escape_and_missing_members_fail(self):
        manifest=self.build(media=True)
        asset=next(iter(manifest["media"].values()))[0]
        for path in ("../outside", "media/../../outside", "https://example.invalid/a", "media/a\\b"):
            asset["relative_path"]=path
            (self.original/"manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
            with self.assertRaises(ExportError): self.save()
        (self.original/"index.html").unlink()
        with self.assertRaises(ExportError): self.save()

    def test_numbered_directory_name_uses_safe_chat_and_range(self):
        self.build()
        target=proposed_directory(self.original,self.target.parent)
        self.assertNotIn(":",target.name)
        self.assertNotIn("?",target.name)
        self.assertNotIn("/",target.name)
        target.mkdir()
        self.assertEqual(proposed_directory(self.original,self.target.parent).name,target.name+"_2")

    def test_crash_cleanup_cannot_remove_published_destination(self):
        self.build()
        self.save()
        cleanup_saved_export(self.owner)
        self.assertTrue(self.target.is_dir())
        record=json.loads(self.owner.read_text(encoding="utf-8"))
        record.update(completed=False,stage=str(self.target))
        self.owner.write_text(json.dumps(record),encoding="utf-8")
        with self.assertRaises(ExportError): cleanup_saved_export(self.owner)
        self.assertTrue(self.target.is_dir())

    def test_forced_worker_cancel_removes_only_owned_stage(self):
        self.build()
        code=("import sys,time;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
              "from wechat_local.bundle import save_export_bundle;sys.stdin.readline();"
              "save_export_bundle(Path(sys.argv[2]),Path(sys.argv[3]),owner_file=Path(sys.argv[4]),"
              "progress=lambda event:(print('copying',flush=True),time.sleep(60)))")
        with tempfile.TemporaryFile() as log:
            job=ProcessJob([sys.executable,"-B","-c",code,str(Path(__file__).resolve().parents[1]/"src"),
                            str(self.original),str(self.target),str(self.owner)],log)
            try:
                self.assertEqual(job.process.stdout.readline().strip(),"copying")
                job.cancel()
            finally:
                job.close()
                job.process.stdout.close()
        self.assertTrue(list(self.target.parent.glob(".wechat-save-*")))
        cleanup_saved_export(self.owner)
        self.assertFalse(self.target.exists())
        self.assertTrue(self.original.is_dir())
        self.assertEqual(list(self.target.parent.glob(".wechat-save-*")),[])

    def test_marker_removal_before_publish_still_allows_owned_cleanup(self):
        self.build()
        captured=[]
        def interrupted_rename(stage,target):
            captured.append(stage)
            raise OSError("synthetic publish interruption")
        with patch.object(Path,"rename",interrupted_rename),self.assertRaises(OSError):
            self.save()
        self.assertTrue(captured)
        self.assertFalse(captured[0].exists())
        self.assertFalse(self.target.exists())

    def test_save_worker_never_invokes_provider_or_model(self):
        self.build()
        settings=Settings(*(self.root for _ in range(4)),model="synthetic")
        job="a"*32
        source=settings.data_dir/"exports"/job
        source.parent.mkdir()
        self.original.rename(source)
        with patch("wechat_local.provider.WechatProvider",side_effect=AssertionError("must not read")),\
                patch("wechat_local.model.LocalModel",side_effect=AssertionError("must not infer")):
            result=execute(SimpleNamespace(command="save-export",job=job,destination=self.target),settings,lambda _:None)
        self.assertTrue(result["success"])
        self.assertEqual(result["job_id"],job)


if __name__=="__main__":
    unittest.main()
