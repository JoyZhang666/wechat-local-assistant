"""Local Qwen3-ASR WAV transcription through pinned llama.cpp, never a cloud API."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import wave


MAX_SECONDS = 60
MAX_OUTPUT_TOKENS = 2048
MAX_LOG_BYTES = 8 * 1024 * 1024
MODEL_NAME = "Qwen3-ASR-1.7B"


class ASRError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _file(root, value):
    if not isinstance(value, str) or not value or "\0" in value or "://" in value:
        raise ASRError("asr_invalid_configuration")
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_file():
        raise ASRError("asr_local_file_missing")
    return path


def _wav_duration(path):
    try:
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels() != 1 or audio.getsampwidth() != 2
                    or audio.getcomptype() != "NONE" or audio.getframerate() not in (16000, 24000, 44100, 48000)):
                raise ASRError("asr_invalid_wav_format")
            frames, rate = audio.getnframes(), audio.getframerate()
            if frames == 0:
                raise ASRError("asr_empty_audio")
            duration = frames / rate
            if duration > MAX_SECONDS:
                raise ASRError("asr_duration_exceeded")
            # A truncated RIFF header must not silently become a shorter input.
            if len(audio.readframes(frames)) != frames * 2:
                raise ASRError("asr_incomplete_wav")
            return duration
    except ASRError:
        raise
    except (OSError, EOFError, wave.Error):
        raise ASRError("asr_invalid_wav") from None


def detect_speech(wav_path, vad_model, *, cancelled=None):
    """A local CPU VAD decision, never a transcript or an audio-cropping step."""
    check = cancelled or (lambda: False)
    path, model = Path(wav_path).resolve(), Path(vad_model).resolve()
    duration = _wav_duration(path)
    if not model.is_file():
        raise ASRError("vad_model_missing")
    if check():
        raise KeyboardInterrupt()
    try:
        import numpy as np
        import sherpa_onnx
    except ImportError:
        raise ASRError("vad_runtime_unavailable") from None
    try:
        with wave.open(str(path), "rb") as audio:
            rate = audio.getframerate()
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        if rate != 16000:
            positions = np.arange(max(1, round(len(samples) * 16000 / rate))) * (rate / 16000)
            samples = np.interp(positions, np.arange(len(samples)), samples).astype(np.float32)
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(model)
        config.silero_vad.threshold = 0.5
        config.silero_vad.min_speech_duration = 0.25
        config.silero_vad.min_silence_duration = 0.5
        config.silero_vad.max_speech_duration = MAX_SECONDS + 1
        config.silero_vad.window_size = 512
        config.sample_rate = 16000
        config.num_threads = 1
        config.provider = "cpu"
        config.debug = False
        detector = sherpa_onnx.VoiceActivityDetector(config, buffer_size_in_seconds=MAX_SECONDS + 1)
        for offset in range(0, len(samples), 512):
            if check():
                raise KeyboardInterrupt()
            detector.accept_waveform(samples[offset:offset + 512])
        detector.flush()
        if check():
            raise KeyboardInterrupt()
        return {"speech_detected": not detector.empty(), "detection_source": "silero_vad_cpu",
                "human_verified": False, "duration_seconds": duration, "sample_rate": 16000,
                "resampling": "none" if rate == 16000 else "linear",
                "parameters": {"threshold": 0.5, "min_speech_duration": 0.25,
                               "min_silence_duration": 0.5, "window_size": 512,
                               "max_speech_duration": MAX_SECONDS + 1, "num_threads": 1,
                               "provider": "cpu"}}
    except KeyboardInterrupt:
        raise
    except Exception:
        raise ASRError("vad_detection_failed") from None


def _parse_logs(stdout, stderr, device):
    generated, diagnostics = [], [stderr]
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            raise ASRError("asr_unexpected_output") from None
        if (not isinstance(event, dict) or event.get("type") != "log"
                or not isinstance(event.get("msg"), str)):
            raise ASRError("asr_unexpected_output")
        if event.get("level") == "none":
            generated.append(event["msg"])
        else:
            diagnostics.append(event["msg"])
    # b11516 mtmd emits one newline after evaluation and a final double newline.
    # Each generated token is an individual `none` log. At the cap, reject
    # conservatively; the CLI otherwise exits successfully on truncation.
    if len(generated) >= 2 and generated[0] == "\n" and generated[-1] == "\n\n":
        if len(generated[1:-1]) >= MAX_OUTPUT_TOKENS:
            raise ASRError("asr_output_truncated")
    else:
        raise ASRError("asr_incomplete_output")
    raw = "".join(generated).strip()
    if not raw:
        raise ASRError("asr_empty_output")
    if raw.count("<asr_text>") > 1:
        raise ASRError("asr_repeated_output")
    language, transcript = "", raw
    if "<asr_text>" in raw:
        metadata, transcript = raw.split("<asr_text>", 1)
        match = re.fullmatch(r"language\s+([A-Za-z]+)", metadata.strip())
        if not match:
            raise ASRError("asr_invalid_language_output")
        language = match[1]
    transcript = transcript.strip()
    if not transcript and language != "None":
        raise ASRError("asr_empty_output")
    if language == "None" and transcript:
        raise ASRError("asr_inconsistent_speech_output")
    if "\ufffd" in raw or any(0xD800 <= ord(character) <= 0xDFFF for character in raw):
        raise ASRError("asr_invalid_text_encoding")
    if re.search(r"<\|[^>]+\|>|<asr_text>", transcript):
        raise ASRError("asr_unexpected_output")
    # Keep the original output. Do not 'repair' repetition by deleting words.
    compact = re.sub(r"\s+", "", transcript)
    if re.search(r"(.{3,80}?)\1{5,}", compact):
        raise ASRError("asr_repeated_output")
    evidence = {"requested_device": device, "gpu_allocation_verified": False,
                "all_operators_on_gpu_verified": False}
    if device != "none":
        log = "\n".join(diagnostics)
        offload = re.search(r"offloaded\s+(\d+)/(\d+)\s+layers to GPU", log)
        projector = re.search(r"CLIP using\s+" + re.escape(device) + r"\s+backend", log)
        if (not offload or int(offload[1]) <= 0 or offload[1] != offload[2]
                or not projector or device not in log or not re.search(r"Intel|Arc", log, re.I)):
            raise ASRError("asr_gpu_not_verified")
        evidence.update(gpu_allocation_verified=True, decoder_gpu_layers=int(offload[1]),
                        audio_encoder_backend=device)
    return raw, language, transcript, evidence


class LocalASR:
    def __init__(self, model_dir, *, timeout=600, cancelled=None, work_dir=None):
        self.root = Path(model_dir).resolve()
        self.work_dir = Path(work_dir).resolve() if work_dir is not None else None
        self.timeout = timeout
        self.cancelled = cancelled or (lambda: False)
        self.process = None
        self.closed = False
        self.lock = threading.Lock()
        try:
            config = json.loads((self.root / "runtime.json").read_text(encoding="utf-8"))
            self.executable = _file(self.root, config["executable"])
            self.model = _file(self.root, config["model"])
            self.mmproj = _file(self.root, config["mmproj"])
            self.device = config["device"]
            self.runtime_version = config["runtime_version"]
            self.model_revision = config["model_revision"]
            self.vad_model = _file(self.root, config["vad_model"]) if config.get("vad_model") is not None else None
        except ASRError:
            raise
        except (OSError, ValueError, KeyError, TypeError):
            raise ASRError("asr_invalid_configuration") from None
        if (self.executable.name.lower() != "llama-mtmd-cli.exe"
                or self.model.suffix.lower() != ".gguf" or self.mmproj.suffix.lower() != ".gguf"
                or not isinstance(self.device, str) or not re.fullmatch(r"Vulkan\d+|none", self.device)
                or self.runtime_version != "b11516"
                or not isinstance(self.model_revision, str) or not re.fullmatch(r"[a-f0-9]{40}", self.model_revision)):
            raise ASRError("asr_invalid_configuration")
        self.integrity_verified=False
        if "sha256" in config:
            expected=config["sha256"]
            names={"executable","model","mmproj"}|({"vad_model"} if self.vad_model is not None else set())
            if not isinstance(expected,dict) or set(expected)!=names:
                raise ASRError("asr_invalid_checksum_configuration")
            for name in sorted(names):
                if not isinstance(expected[name],str) or not re.fullmatch(r"[a-f0-9]{64}",expected[name]):
                    raise ASRError("asr_invalid_checksum_configuration")
                digest=hashlib.sha256()
                try:
                    with getattr(self,name).open("rb") as source:
                        for chunk in iter(lambda:source.read(4*1024*1024),b""):
                            self._check_cancelled()
                            digest.update(chunk)
                except OSError:raise ASRError("asr_local_file_missing") from None
                if digest.hexdigest()!=expected[name]:raise ASRError("asr_checksum_mismatch")
            self.integrity_verified=True

    def command(self, wav_path):
        command = [str(self.executable), "-m", str(self.model), "--mmproj", str(self.mmproj),
                   "--audio", str(wav_path), "-p", "<__media__>", "--jinja", "--offline", "--temp", "0",
                   "-n", str(MAX_OUTPUT_TOKENS), "-c", "4096", "-t", "6", "--fit", "off",
                   "--log-jsonl", "-lv", "4", "--device", self.device]
        if self.device == "none":
            return command + ["-ngl", "0", "--no-mmproj-offload"]
        return command + ["-ngl", "999", "--mmproj-device", self.device, "--mmproj-offload"]

    def _check_cancelled(self):
        if self.closed or self.cancelled():
            raise KeyboardInterrupt()

    def transcribe(self, wav_path):
        self._check_cancelled()
        path = Path(wav_path).resolve()
        duration = _wav_duration(path)
        vad = None
        if self.vad_model is not None:
            vad = detect_speech(path, self.vad_model, cancelled=lambda: self.closed or self.cancelled())
            if not vad["speech_detected"]:
                return {"kind": "voice_transcript", "status": "complete", "model": "Silero VAD",
                        "asr_model_requested": MODEL_NAME, "asr_invoked": False,
                        "derived": True, "human_verified": False, "transcript": "",
                        "speech_detected": False, "speech_detection_source": vad["detection_source"],
                        "raw_model_output": None, "language": None, "duration_seconds": duration,
                        "segments": [], "timestamp_precision": "whole_clip", "vad": vad,
                        "acceleration": {"requested_device": self.device, "gpu_allocation_verified": False,
                                         "all_operators_on_gpu_verified": False}}
        env = {key: value for key, value in os.environ.items()
               if not key.upper().startswith("LLAMA_ARG_") and key.upper() != "MTMD_BACKEND_DEVICE"}
        env.update(HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
        try:
            # The GUI uses its owned snapshot so forced cancellation can remove
            # private logs even when this process cannot execute its finally block.
            work_dir = self.work_dir if self.work_dir is not None else path.parent
            work_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".asr-", dir=work_dir) as folder:
                out, err = Path(folder) / "output.jsonl", Path(folder) / "diagnostic.log"
                with out.open("wb") as stdout, err.open("wb") as stderr:
                    with self.lock:
                        self._check_cancelled()
                        if self.process is not None:
                            raise ASRError("asr_busy")
                        self.process = subprocess.Popen(self.command(path), cwd=self.executable.parent,
                            env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                            creationflags=subprocess.CREATE_NO_WINDOW)
                    process = self.process
                    try:
                        deadline = time.monotonic() + self.timeout
                        while True:
                            self._check_cancelled()
                            if out.stat().st_size + err.stat().st_size > MAX_LOG_BYTES:
                                raise ASRError("asr_output_exceeded")
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                raise ASRError("asr_timeout")
                            try:
                                code = process.wait(timeout=min(0.2, remaining))
                                break
                            except subprocess.TimeoutExpired:
                                continue
                        self._check_cancelled()
                        if code != 0:
                            raise ASRError("asr_process_failed")
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=10)
                        with self.lock:
                            self.process = None
                if out.stat().st_size + err.stat().st_size > MAX_LOG_BYTES:
                    raise ASRError("asr_output_exceeded")
                raw, language, transcript, evidence = _parse_logs(
                    out.read_text(encoding="utf-8"), err.read_text(encoding="utf-8", errors="replace"), self.device)
            result = {"kind": "voice_transcript", "status": "complete", "model": MODEL_NAME,
                    "runtime": "llama.cpp " + self.runtime_version, "model_revision": self.model_revision,
                    "derived": True, "human_verified": False, "transcript": transcript,
                    "speech_detected": bool(transcript), "speech_detection_source": "model",
                    "raw_model_output": raw, "language": language, "duration_seconds": duration,
                    "segments": [{"start_seconds": 0, "end_seconds": duration, "transcript": transcript}],
                    "timestamp_precision": "whole_clip", "acceleration": evidence}
            if vad is not None:
                result["vad"] = vad
            return result
        except (ASRError, KeyboardInterrupt):
            raise
        except (OSError, UnicodeError):
            raise ASRError("asr_local_runtime_failed") from None

    def close(self):
        with self.lock:
            self.closed = True
            if self.process is not None and self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
