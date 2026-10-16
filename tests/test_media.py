"""Selected asset scope and ASR gates, with synthetic files only."""
from datetime import datetime,timezone,timedelta
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from wechat_local.media import MediaResolver,MediaError


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.provider=Mock()
        self.provider.account_data_root.return_value=self.root/"account"
        self.model=Mock()
        self.model.settings=SimpleNamespace(asr_model_dir=None)
        self.resolver=MediaResolver(self.provider,self.model,self.root/"job")
        timestamp=int(datetime(2026,10,9,12,tzinfo=timezone(timedelta(hours=8))).timestamp())
        self.message={"id":"a"*64,"chat_id":"synthetic@chatroom","kind":"file","timestamp":timestamp,
                      "attachment_hints":[]}
        self.scope=self.root/"account"/"msg"/"attach"/hashlib.md5(self.message["chat_id"].encode()).hexdigest()/"2026-10"/"File"
        self.scope.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_file_card_hint(self):
        (self.scope/"synthetic.pdf").write_bytes(b"synthetic file")
        self.message["attachment_hints"]=[{"type":"filename","value":"synthetic.pdf"}]
        assets=self.resolver.resolve(self.message)
        self.assertEqual(Path(assets[0]["path"]),self.scope/"synthetic.pdf")

    def test_arbitrary_local_path_is_not_read(self):
        outside=self.root/"unselected.txt"
        outside.write_text("synthetic unselected")
        for kind,value in (("path",str(outside)),("filename","../unselected.txt"),("filename","synthetic.txt:stream")):
            self.message["attachment_hints"]=[{"type":kind,"value":value}]
            with self.assertRaises(MediaError):
                self.resolver.resolve(self.message)

    def test_voice_not_sent_to_failed_gemma_asr(self):
        self.message["kind"]="voice"
        result=self.resolver.annotate(self.message,[{"kind":"voice_audio","path":"synthetic.wav"}])
        self.assertEqual(result,{"status":"error","error_code":"voice_transcription_not_configured"})
        self.model.transcribe_audio.assert_not_called()
        self.model.stop.assert_not_called()

    def test_image_voice_image_releases_gemma_before_asr_and_restarts_vision(self):
        self.model.settings.asr_model_dir=self.root/"asr"
        sequence=Mock()
        sequence.attach_mock(self.model,"gemma")
        transcription=Mock()
        transcription.transcribe.return_value={"status":"complete","transcript":"合成转写"}
        sequence.attach_mock(transcription,"asr")
        self.model.analyze_image.side_effect=[{"status":"complete","ocr_text":"合成图片一"},
                                             {"status":"complete","ocr_text":"合成图片二"}]
        self.message["kind"]="image"
        first=self.resolver.annotate(self.message,[{"kind":"image","path":"one.png","quality":"original"}])
        with patch("wechat_local.asr.LocalASR",return_value=transcription) as load:
            self.message["kind"]="voice"
            voice=self.resolver.annotate(self.message,[{"kind":"voice_audio","path":"synthetic.wav"}])
            self.message["kind"]="image"
            second=self.resolver.annotate(self.message,[{"kind":"image","path":"two.png","quality":"original"}])
        self.assertEqual([call[0] for call in sequence.mock_calls],
                         ["gemma.analyze_image","gemma.stop","asr.transcribe","gemma.analyze_image"])
        load.assert_called_once_with(self.root/"asr",work_dir=self.root/"job"/"snapshot"/"asr-work")
        self.assertEqual(voice["transcript"],"合成转写")
        self.assertEqual((first["ocr_text"],second["ocr_text"]),("合成图片一","合成图片二"))
        self.model.transcribe_audio.assert_not_called()
        self.resolver.close()
        self.resolver.close()
        transcription.close.assert_called_once()
        self.assertIsNone(self.resolver.asr)

    def test_missing_voice_wav_does_not_stop_gemma_or_load_asr(self):
        self.model.settings.asr_model_dir=self.root/"asr"
        self.message["kind"]="voice"
        with patch("wechat_local.asr.LocalASR") as load:
            result=self.resolver.annotate(self.message,[{"kind":"voice_original","path":"synthetic.silk"}])
        self.assertEqual(result,{"status":"error","error_code":"decoded_voice_missing"})
        self.model.stop.assert_not_called()
        load.assert_not_called()

    def test_encrypted_image_not_sent_to_vision(self):
        self.message["kind"]="image"
        result=self.resolver.annotate(self.message,[{"kind":"image_encrypted","path":"synthetic.bin"}])
        self.assertEqual(result["status"],"error")
        self.model.analyze_image.assert_not_called()


if __name__=="__main__":
    unittest.main()
