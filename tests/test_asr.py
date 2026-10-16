"""Synthetic WAV and native-log boundary tests; no real chats or model loads."""
import json
import importlib.util
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from wechat_local import asr


def logs(text="language Chinese<asr_text>请在十月十二日前提交资料。", gpu=True):
    diagnostics = (["using device Vulkan0 (Intel(R) Arc(TM) 140V)",
                    "load_tensors: offloaded 29/29 layers to GPU\n",
                    "clip_model_loader: CLIP using Vulkan0 backend\n"] if gpu else [])
    events = [{"type": "log", "level": "info", "msg": value} for value in diagnostics]
    events += [{"type": "log", "level": "none", "msg": value}
               for value in ("\n", text, "\n", "\n\n")]
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


class ASRTests(unittest.TestCase):
    def setUp(self):
        temporary = Path(__file__).resolve().parents[1] / "tmp"
        temporary.mkdir(exist_ok=True)
        self.temp = TemporaryDirectory(dir=temporary)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"executable": "llama-mtmd-cli.exe", "model": "qwen-q8.gguf",
                       "mmproj": "mmproj-q8.gguf", "device": "Vulkan0",
                       "runtime_version": "b11516", "model_revision": "a" * 40}
        for name in ("executable", "model", "mmproj"):
            (self.root / self.config[name]).write_bytes(b"synthetic fixture")
        self.save_config()
        self.wav = self.write_wav()

    def save_config(self):
        (self.root / "runtime.json").write_text(json.dumps(self.config), encoding="utf-8")

    def write_wav(self, seconds=0.5, channels=1, width=2, rate=24000):
        path = self.root / "synthetic.wav"
        with wave.open(str(path), "wb") as audio:
            audio.setparams((channels, width, rate, 0, "NONE", "not compressed"))
            audio.writeframes(b"\1" * int(seconds * rate) * channels * width)
        return path

    def launcher(self, output=None, code=0):
        self.process = Mock()
        self.process.wait.return_value = code
        self.process.poll.return_value = code
        def launch(command, **kwargs):
            kwargs["stdout"].write((logs() if output is None else output).encode("utf-8"))
            kwargs["stdout"].flush()
            return self.process
        return patch.object(asr.subprocess, "Popen", side_effect=launch)

    def assert_error(self, code, function):
        with self.assertRaises(asr.ASRError) as caught:
            function()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_local_command_and_raw_transcript_stay_separate(self):
        runtime = asr.LocalASR(self.root)
        with self.launcher() as launcher:
            result = runtime.transcribe(self.wav)
        command, options = launcher.call_args.args[0], launcher.call_args.kwargs
        self.assertIn("--offline", command)
        self.assertIn("--jinja", command)
        self.assertIn("--log-jsonl", command)
        self.assertNotIn("-hf", command)
        self.assertNotIn("--simple-io", command)
        self.assertNotIn("--no-display-prompt", command)
        self.assertEqual(command[command.index("--audio") + 1], str(self.wav.resolve()))
        self.assertEqual(command[command.index("-p") + 1], "<__media__>")
        self.assertEqual(command[command.index("--device") + 1], "Vulkan0")
        self.assertEqual(command[command.index("--mmproj-device") + 1], "Vulkan0")
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertEqual(result["transcript"], "请在十月十二日前提交资料。")
        self.assertTrue(result["raw_model_output"].startswith("language Chinese<asr_text>"))
        self.assertTrue(result["derived"])
        self.assertFalse(result["human_verified"])
        self.assertTrue(result["acceleration"]["gpu_allocation_verified"])
        self.assertEqual(result["segments"][0]["end_seconds"], 0.5)
        self.assertEqual(result["timestamp_precision"], "whole_clip")
        self.assertIsNone(runtime.process)
        self.assertFalse(list(self.root.glob(".asr-*")))

    def test_inherited_remote_and_log_settings_are_removed(self):
        with patch.dict(asr.os.environ, {"LLAMA_ARG_MODEL_URL": "https://example.invalid",
                                         "LLAMA_ARG_RPC": "example.invalid:9000",
                                         "LLAMA_ARG_LOG_FILE": "PRIVATE_SENTINEL",
                                         "MTMD_BACKEND_DEVICE": "CPU"}):
            with self.launcher() as launcher:
                asr.LocalASR(self.root).transcribe(self.wav)
        environment = launcher.call_args.kwargs["env"]
        self.assertFalse(any(key.upper().startswith("LLAMA_ARG_") for key in environment))
        self.assertNotIn("MTMD_BACKEND_DEVICE", environment)
        self.assertEqual(environment["HF_HUB_OFFLINE"], "1")

    def test_cpu_baseline_is_explicit_not_a_gpu_fallback(self):
        self.config["device"] = "none"
        self.save_config()
        with self.launcher(logs(gpu=False)) as launcher:
            result = asr.LocalASR(self.root).transcribe(self.wav)
        command = launcher.call_args.args[0]
        self.assertEqual(command[command.index("-ngl") + 1], "0")
        self.assertIn("--no-mmproj-offload", command)
        self.assertFalse(result["acceleration"]["gpu_allocation_verified"])

    def test_gpu_requires_device_decoder_and_audio_encoder_evidence(self):
        for output in (logs(gpu=False), logs().replace("29/29", "0/29"),
                       logs().replace("29/29", "12/29"),
                       logs().replace("CLIP using Vulkan0", "CLIP using CPU"),
                       logs().replace("Intel(R) Arc(TM) 140V", "unverified device")):
            with self.subTest(output=output), self.launcher(output):
                self.assert_error("asr_gpu_not_verified", lambda: asr.LocalASR(self.root).transcribe(self.wav))

    def test_invalid_wav_empty_and_overlong_never_launch(self):
        for code, options in (("asr_empty_audio", {"seconds": 0}),
                              ("asr_duration_exceeded", {"seconds": 60.1}),
                              ("asr_invalid_wav_format", {"channels": 2}),
                              ("asr_invalid_wav_format", {"width": 1}),
                              ("asr_invalid_wav_format", {"rate": 8000})):
            with self.subTest(code=code), self.launcher() as launcher:
                self.write_wav(**options)
                self.assert_error(code, lambda: asr.LocalASR(self.root).transcribe(self.wav))
                launcher.assert_not_called()

    def test_truncated_riff_is_not_treated_as_shorter_audio(self):
        self.wav.write_bytes(self.wav.read_bytes()[:-10])
        with self.launcher() as launcher:
            self.assert_error("asr_incomplete_wav", lambda: asr.LocalASR(self.root).transcribe(self.wav))
            launcher.assert_not_called()

    def test_empty_repetition_and_control_tokens_are_not_repaired(self):
        cases = (("asr_empty_output", "language Chinese<asr_text>"),
                 ("asr_empty_output", ""),
                 ("asr_inconsistent_speech_output", "language None<asr_text>伪造内容"),
                 ("asr_repeated_output", "language Chinese<asr_text>" + "提交资料" * 6),
                 ("asr_repeated_output", "language Chinese<asr_text>内容<asr_text>重复"),
                 ("asr_unexpected_output", "language Chinese<asr_text>内容<|im_start|>"),
                 ("asr_invalid_text_encoding", "language Chinese<asr_text>名字\ufffd"),
                 ("asr_invalid_language_output", "unexpected metadata<asr_text>内容"))
        for code, text in cases:
            with self.subTest(code=code), self.launcher(logs(text)):
                self.assert_error(code, lambda: asr.LocalASR(self.root).transcribe(self.wav))

    def test_explicit_no_speech_output_is_a_model_abstention(self):
        with self.launcher(logs("language None<asr_text>")):
            result = asr.LocalASR(self.root).transcribe(self.wav)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["language"], "None")
        self.assertEqual(result["transcript"], "")
        self.assertFalse(result["speech_detected"])
        self.assertEqual(result["speech_detection_source"], "model")
        self.assertFalse(result["human_verified"])
        self.assertEqual(result["raw_model_output"], "language None<asr_text>")
        self.assertTrue(result["acceleration"]["gpu_allocation_verified"])

    def test_plain_text_and_mixed_language_are_preserved(self):
        with self.launcher(logs("今天 review PO26001，金额三千元。")):
            result = asr.LocalASR(self.root).transcribe(self.wav)
        self.assertEqual(result["language"], "")
        self.assertEqual(result["transcript"], "今天 review PO26001，金额三千元。")

    def test_token_limit_does_not_masquerade_as_complete(self):
        output = logs()
        lines = output.splitlines()
        prefix, _, _, suffix = lines[-4:]
        tokens = [json.dumps({"type": "log", "level": "none", "msg": "甲"})
                  for _ in range(asr.MAX_OUTPUT_TOKENS)]
        output = "\n".join(lines[:-4] + [prefix] + tokens + [suffix])
        with self.launcher(output):
            self.assert_error("asr_output_truncated", lambda: asr.LocalASR(self.root).transcribe(self.wav))

    def test_corrupt_or_incomplete_json_logs_fail_without_private_detail(self):
        for code, output in (("asr_unexpected_output", "PRIVATE_SENTINEL"),
                             ("asr_incomplete_output", logs().rsplit("\n", 1)[0])):
            with self.subTest(code=code), self.launcher(output):
                error = self.assert_error(code, lambda: asr.LocalASR(self.root).transcribe(self.wav))
                self.assertNotIn("PRIVATE_SENTINEL", str(error))

    def test_runtime_failure_returns_fixed_code_and_cleans_files(self):
        with self.launcher(code=7):
            self.assert_error("asr_process_failed", lambda: asr.LocalASR(self.root).transcribe(self.wav))
        self.assertFalse(list(self.root.glob(".asr-*")))

    def test_owned_snapshot_contains_logs_and_success_and_error_clean_them(self):
        work_dir=self.root/"job"/"snapshot"/"asr-work"
        for code in (0,7):
            with self.subTest(code=code), self.launcher(code=code) as launcher:
                runtime=asr.LocalASR(self.root,work_dir=work_dir)
                if code==0:
                    runtime.transcribe(self.wav)
                else:
                    self.assert_error("asr_process_failed",lambda:runtime.transcribe(self.wav))
            generated_log=Path(launcher.call_args.kwargs["stdout"].name)
            self.assertEqual(generated_log.parent.parent,work_dir.resolve())
            self.assertTrue(work_dir.is_dir())
            self.assertFalse(generated_log.parent.exists())
            self.assertFalse(list(work_dir.iterdir()))
            self.assertFalse(list(self.wav.parent.glob(".asr-*")))

    def configure_vad(self):
        self.config["vad_model"]="synthetic-vad.onnx"
        (self.root/self.config["vad_model"]).write_bytes(b"synthetic VAD fixture")
        self.save_config()

    def test_configured_vad_no_speech_does_not_load_qwen(self):
        self.configure_vad()
        decision={"speech_detected":False,"detection_source":"silero_vad_cpu"}
        with patch.object(asr,"detect_speech",return_value=decision) as detect,self.launcher() as launcher:
            result=asr.LocalASR(self.root).transcribe(self.wav)
        launcher.assert_not_called()
        self.assertEqual(detect.call_args.args[:2],(self.wav.resolve(),self.root/"synthetic-vad.onnx"))
        self.assertEqual(result["status"],"complete")
        self.assertFalse(result["asr_invoked"])
        self.assertFalse(result["speech_detected"])
        self.assertEqual(result["speech_detection_source"],"silero_vad_cpu")
        self.assertIsNone(result["raw_model_output"])
        self.assertEqual(result["transcript"],"")
        self.assertFalse(result["human_verified"])
        self.assertFalse(result["acceleration"]["gpu_allocation_verified"])

    def test_vad_speech_preserves_original_complete_wav_for_asr(self):
        self.configure_vad()
        original=self.wav.read_bytes()
        decision={"speech_detected":True,"detection_source":"silero_vad_cpu"}
        with patch.object(asr,"detect_speech",return_value=decision),self.launcher() as launcher:
            result=asr.LocalASR(self.root).transcribe(self.wav)
        command=launcher.call_args.args[0]
        self.assertEqual(command[command.index("--audio")+1],str(self.wav.resolve()))
        self.assertEqual(self.wav.read_bytes(),original)
        self.assertEqual(result["vad"],decision)
        self.assertEqual(result["transcript"],"请在十月十二日前提交资料。")

    def test_vad_failure_does_not_fall_back_to_unguarded_asr(self):
        self.configure_vad()
        with patch.object(asr,"detect_speech",side_effect=asr.ASRError("vad_detection_failed")),self.launcher() as launcher:
            self.assert_error("vad_detection_failed",lambda:asr.LocalASR(self.root).transcribe(self.wav))
        launcher.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("numpy") and importlib.util.find_spec("sherpa_onnx"), "optional VAD dependencies not installed")
    def test_vad_helper_resamples_full_clip_uses_cpu_and_flushes_tail(self):
        import numpy as np
        self.configure_vad()
        for rate in (16000,24000,48000):
            self.write_wav(rate=rate)
            with self.subTest(rate=rate),patch("sherpa_onnx.VoiceActivityDetector") as factory:
                detector=factory.return_value
                detector.empty.return_value=True
                result=asr.detect_speech(self.wav,self.root/"synthetic-vad.onnx")
                samples=np.concatenate([call.args[0] for call in detector.accept_waveform.call_args_list])
                self.assertEqual(len(samples),8000)
                self.assertEqual(samples.dtype,np.float32)
                config=factory.call_args.args[0]
                self.assertEqual((config.provider,config.num_threads,config.sample_rate),("cpu",1,16000))
                self.assertEqual(config.silero_vad.threshold,0.5)
                self.assertEqual([call[0] for call in detector.mock_calls][-2:],["flush","empty"])
                self.assertFalse(result["speech_detected"])
                self.assertFalse(result["human_verified"])

    @unittest.skipUnless(importlib.util.find_spec("sherpa_onnx"), "optional VAD dependencies not installed")
    def test_vad_native_failure_and_cancellation_have_honest_boundaries(self):
        self.configure_vad()
        with patch("sherpa_onnx.VoiceActivityDetector",side_effect=RuntimeError("PRIVATE_SENTINEL")):
            error=self.assert_error("vad_detection_failed",lambda:asr.detect_speech(self.wav,self.root/"synthetic-vad.onnx"))
        self.assertNotIn("PRIVATE_SENTINEL",str(error))
        with self.assertRaises(KeyboardInterrupt),patch("sherpa_onnx.VoiceActivityDetector") as factory:
            asr.detect_speech(self.wav,self.root/"synthetic-vad.onnx",cancelled=lambda:True)
        factory.assert_not_called()

    def test_timeout_kills_owned_process(self):
        runtime = asr.LocalASR(self.root, timeout=0.5)
        with self.launcher(), patch.object(asr.time, "monotonic", side_effect=[0, 0, 1]):
            self.process.wait.side_effect = [subprocess.TimeoutExpired("synthetic", 0.2), 0]
            self.process.poll.return_value = None
            self.assert_error("asr_timeout", lambda: runtime.transcribe(self.wav))
        self.process.kill.assert_called_once()
        self.assertIsNone(runtime.process)
        self.assertFalse(list(self.root.glob(".asr-*")))

    def test_cancel_kills_owned_process_and_releases_handle(self):
        checks = iter([False, False, True])
        runtime = asr.LocalASR(self.root, cancelled=lambda: next(checks))
        with self.launcher():
            self.process.poll.return_value = None
            with self.assertRaises(KeyboardInterrupt):
                runtime.transcribe(self.wav)
        self.process.kill.assert_called_once()
        self.assertIsNone(runtime.process)

    def test_close_is_idempotent_and_blocks_future_launch(self):
        runtime = asr.LocalASR(self.root)
        runtime.close()
        runtime.close()
        with self.launcher() as launcher, self.assertRaises(KeyboardInterrupt):
            runtime.transcribe(self.wav)
        launcher.assert_not_called()

    def test_remote_configuration_and_unknown_device_are_rejected(self):
        for key, value, code in (("model", "https://example.invalid/model.gguf", "asr_invalid_configuration"),
                                 ("device", "CPU", "asr_invalid_configuration"),
                                 ("runtime_version", "master", "asr_invalid_configuration"),
                                 ("model_revision", "latest", "asr_invalid_configuration"),
                                 ("mmproj", "missing.gguf", "asr_local_file_missing")):
            original = dict(self.config)
            self.config[key] = value
            self.save_config()
            self.assert_error(code, lambda: asr.LocalASR(self.root))
            self.config = original


if __name__ == "__main__":
    unittest.main()
