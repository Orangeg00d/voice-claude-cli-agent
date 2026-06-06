"""Speech-to-Text abstraction layer.

Phase 2 — real audio recording + manual record/transcribe flow.
Provides Transcriber protocol, text-input fallback, recording transcriber,
and a placeholder for future Whisper/Apple Speech backends.
"""

import subprocess
import tempfile
from pathlib import Path
from typing import Protocol


class Transcriber(Protocol):
    """Abstract STT interface. Implementations convert audio to text."""

    def transcribe(self, audio_data: bytes | None = None) -> str: ...


class TextInputTranscriber:
    """Development transcriber: returns text directly (no audio processing)."""

    def transcribe(self, audio_data: bytes | None = None) -> str:
        if audio_data is not None:
            return f"[mock STT: {len(audio_data)} bytes of audio]"
        return input("Enter command text: ").strip()


class FakeTranscriber:
    """Fake transcriber for testing. Returns a preset response."""

    def __init__(self, response: str = "") -> None:
        self.response = response
        self.calls: list[bytes | None] = []

    def transcribe(self, audio_data: bytes | None = None) -> str:
        self.calls.append(audio_data)
        return self.response


class RecordingTranscriber:
    """STT transcriber that receives audio from an AudioRecorder.

    Saves recorded audio to a temp WAV and invokes a configurable backend.
    MVP uses the text-input fallback (prints audio stats).
    Future: swap in Whisper CLI or Apple Speech.
    """

    def __init__(self, backend: str = "text-input") -> None:
        self.backend = backend

    def transcribe(self, audio_data: bytes | None = None) -> str:
        if not audio_data:
            return ""
        if self.backend == "text-input":
            return f"[recorded {len(audio_data)} bytes, {(len(audio_data) / 2 / 16000):.1f}s audio]"
        if self.backend == "whisper-cli":
            return _transcribe_whisper_cli(audio_data)
        return f"[unknown backend: {self.backend}]"


def _transcribe_whisper_cli(audio_data: bytes) -> str:
    """Transcribe via a local whisper.cpp CLI. Returns empty string on failure."""
    import shutil

    whisper_bin = shutil.which("whisper-cpp") or shutil.which("whisper")
    if not whisper_bin:
        return "[whisper CLI not found]"

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(audio_data)
        tmp_path = f.name

    try:
        proc = subprocess.run(
            [whisper_bin, "-f", tmp_path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.stdout.strip() or proc.stderr.strip()
    except Exception:
        return "[whisper transcription failed]"
    finally:
        Path(tmp_path).unlink(missing_ok=True)
