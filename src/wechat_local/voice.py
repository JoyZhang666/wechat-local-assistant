"""Read one selected WeChat voice asset and decode SILK locally.

No keys, process hooks, external executables or inference are used here. Media
paths must be the read-only snapshots supplied by the authorized provider.
"""
from __future__ import annotations

from hashlib import sha256
import importlib
import io
from pathlib import Path
import sqlite3
from uuid import uuid4
import wave


SAMPLE_RATE = 24000
MAX_SILK_BYTES = 16 * 1024 * 1024
MAX_PCM_BYTES = SAMPLE_RATE * 2 * 600


class VoiceError(RuntimeError):
    """A public error code; the optional local asset is for the local exporter."""

    def __init__(self, code: str, asset: dict | None = None):
        super().__init__(code)
        self.code = code
        self.asset = asset or {}


def _identifier(value, allow_missing=False):
    if isinstance(value, bool):
        raise VoiceError("voice_invalid_selection")
    if allow_missing and value in (None, "", 0, "0"):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value and value.lstrip("-").isdigit():
        return str(int(value))
    raise VoiceError("voice_invalid_selection")


def _selected_blob(media_paths, chat_id, local_id, server_id, timestamp):
    if not isinstance(chat_id, str) or not chat_id:
        raise VoiceError("voice_invalid_selection")
    local = _identifier(local_id)
    server = _identifier(server_id, allow_missing=True)
    if type(timestamp) is not int or timestamp < 0:
        raise VoiceError("voice_invalid_selection")
    blobs = {}
    for media_path in media_paths:
        connection = None
        try:
            # as_uri percent-encodes spaces and URI-significant characters.
            connection = sqlite3.connect(Path(media_path).resolve().as_uri() + "?mode=ro", uri=True)
            connection.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "VoiceInfo" not in tables:
                continue
            if "Name2Id" not in tables:
                raise VoiceError("voice_schema_not_supported")
            query = ("SELECT v.voice_data FROM VoiceInfo AS v JOIN Name2Id AS n "
                     "ON n.rowid=v.chat_name_id WHERE n.user_name=? "
                     "AND CAST(v.local_id AS TEXT)=? AND v.create_time=?")
            values = [chat_id, local, timestamp]
            if server is not None:
                query += " AND CAST(v.svr_id AS TEXT)=?"
                values.append(server)
            for (data,) in connection.execute(query, values):
                if data is None or data == b"":
                    continue
                if not isinstance(data, bytes) or len(data) > MAX_SILK_BYTES:
                    raise VoiceError("voice_invalid_data")
                digest = sha256(data).digest()
                blobs[digest] = data
                if len(blobs) > 1:
                    raise VoiceError("voice_conflict")
        except VoiceError:
            raise
        except (OSError, ValueError, sqlite3.Error):
            raise VoiceError("voice_read_failed") from None
        finally:
            if connection is not None:
                connection.close()
    if not blobs:
        raise VoiceError("voice_not_downloaded")
    return next(iter(blobs.values()))


class _BoundedPCM(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_PCM_BYTES:
            raise VoiceError("voice_duration_exceeded")
        return super().write(data)


def _decode_to_wav(data: bytes, wav_path: Path) -> dict:
    payload = data[1:] if data.startswith(b"\x02#!SILK_V3") else data
    if not payload.startswith(b"#!SILK_V3"):
        raise VoiceError("voice_invalid_silk_header")
    try:
        decoder = importlib.import_module("pysilk")
    except ImportError:
        raise VoiceError("voice_decoder_not_installed") from None
    try:
        with io.BytesIO(payload) as silk, _BoundedPCM() as pcm:
            # Public silk-python API: BinaryIO -> signed 16-bit little-endian PCM.
            decoder.decode(silk, pcm, SAMPLE_RATE)
            decoded = pcm.getvalue()
        if not decoded or len(decoded) % 2:
            raise VoiceError("voice_invalid_pcm")
        with wave.open(str(wav_path), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(decoded)
        return {"sample_rate": SAMPLE_RATE, "duration_seconds": len(decoded) / (SAMPLE_RATE * 2),
                "channels": 1, "sample_width": 2}
    except VoiceError:
        raise
    except Exception:
        raise VoiceError("voice_decode_failed") from None


def selected_voice_asset(media_paths, chat_id, local_id, server_id, timestamp, outdir) -> dict:
    """Return exact original SILK plus decoded WAV for a selected message only.

    On decoding failure, VoiceError.asset retains the original SILK path so an
    exporter can preserve the attachment and explicitly mark it untranscribed.
    Exceptions and this module's stdout never expose private input or DB paths.
    """
    data = _selected_blob(media_paths, chat_id, local_id, server_id, timestamp)
    folder = Path(outdir) / ("voice-" + uuid4().hex)
    asset = {}
    try:
        folder.mkdir(parents=True, exist_ok=False)
        silk_path, wav_path = folder / "original.silk", folder / "decoded.wav"
        silk_path.write_bytes(data)
        asset = {"silk_path": str(silk_path), "wav_path": None,
                 "sha256": sha256(data).hexdigest(), "decoded": False}
        details = _decode_to_wav(data, wav_path)
        return {**asset, **details, "wav_path": str(wav_path), "decoded": True}
    except VoiceError as error:
        error.asset = asset
        raise
    except Exception:
        raise VoiceError("voice_write_failed", asset) from None
