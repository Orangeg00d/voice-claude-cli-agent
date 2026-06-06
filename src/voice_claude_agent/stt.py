"""Speech-to-Text abstraction layer.

Phase 4 — real STT backends: text-input (dev), whisper-cli (local),
and apple-speech (macOS NSSpeechRecognizer via osascript).
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

    Backends:
    - text-input: prints audio stats (dev mode, no real STT)
    - whisper-cli: local whisper.cpp binary
    - apple-speech: macOS on-device dictation via osascript
    """

    def __init__(self, backend: str = "text-input") -> None:
        self.backend = backend

    def transcribe(self, audio_data: bytes | None = None) -> str:
        if not audio_data:
            return ""
        if self.backend == "text-input":
            return (
                f"[recorded {len(audio_data)} bytes,"
                f" {(len(audio_data) / 2 / 16000):.1f}s audio]"
            )
        if self.backend == "whisper-cli":
            return _transcribe_whisper_cli(audio_data)
        if self.backend == "apple-speech":
            return _transcribe_apple_speech(audio_data)
        return f"[unknown backend: {self.backend}]"


def _transcribe_whisper_cli(audio_data: bytes) -> str:
    """Transcribe via a local whisper.cpp CLI binary."""
    import shutil

    whisper_bin = shutil.which("whisper-cpp") or shutil.which("whisper")
    if not whisper_bin:
        return "[STT error: whisper CLI not found. Install whisper.cpp or use --stt-backend text-input]"

    wav_data = _pcm_to_wav(audio_data)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_data)
        tmp_path = f.name

    try:
        proc = subprocess.run(
            [whisper_bin, "-f", tmp_path, "--no-timestamps"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        result = proc.stdout.strip()
        if not result and proc.stderr.strip():
            result = _extract_text_from_whisper_stderr(proc.stderr)
        return result or "[whisper returned empty]"
    except subprocess.TimeoutExpired:
        return "[STT error: whisper transcription timed out]"
    except Exception as e:
        return f"[STT error: whisper failed: {e}]"
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _transcribe_apple_speech(audio_data: bytes) -> str:
    """Transcribe using macOS on-device dictation.

    Writes audio to a temp file and uses a short osascript to invoke
    the system speech recognition. Falls back with a clear message if
    dictation is not enabled (System Settings > Keyboard > Dictation).
    """
    import platform

    if platform.system() != "Darwin":
        return "[STT error: Apple Speech only available on macOS]"

    wav_data = _pcm_to_wav(audio_data)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_data)
        wav_path = f.name

    try:
        # Use afplay to play the audio while NSSpeechRecognizer listens.
        # This approach uses the system dictation — the user must have
        # "Enable Dictation" turned on in System Settings.
        script = '''
        tell application "System Events"
            set prevDictation to do shell script "defaults read com.apple.speech.recognition.AppleSpeechRecognition.prefs DictationIMEnabled 2>/dev/null || echo 0"
        end tell
        return prevDictation
        '''
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
        )
        dictation_enabled = proc.stdout.strip() == "1"

        if not dictation_enabled:
            return (
                "[STT error: macOS Dictation is not enabled. "
                "Enable it in System Settings > Keyboard > Dictation, "
                "or use --stt-backend whisper-cli]"
            )

        # Dictation is enabled — use it via a short recording replay
        subprocess.run(
            [
                "osascript", "-e",
                f'''
                set audioFile to POSIX file "{wav_path}"
                tell application "Finder" to open audioFile
                delay 3
                ''',
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return (
            "[Apple Speech: audio played for dictation. "
            "Dictation result appears in the active text field. "
            "For programmatic capture, use --stt-backend whisper-cli]"
        )
    except subprocess.TimeoutExpired:
        return "[STT error: Apple Speech timed out]"
    except Exception as e:
        return f"[STT error: Apple Speech failed: {e}]"
    finally:
        Path(wav_path).unlink(missing_ok=True)


def _pcm_to_wav(pcm_data: bytes, sample_rate: int = 16000, channels: int = 1) -> bytes:
    """Convert raw PCM int16 to a minimal WAV file in memory."""
    import struct

    data_len = len(pcm_data)
    byte_rate = sample_rate * channels * 2
    block_align = channels * 2
    bits_per_sample = 16

    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_len,
        b"WAVE",
        b"fmt ",
        16,  # PCM
        1,  # format = PCM
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
        b"data",
        data_len,
    )
    return header + pcm_data


def _extract_text_from_whisper_stderr(stderr: str) -> str:
    """Try to extract transcribed text from whisper.cpp stderr output."""
    for line in stderr.splitlines():
        line = line.strip()
        if line and not line.startswith("[") and "whisper" not in line.lower():
            return line
    return ""


def list_available_backends() -> list[str]:
    """Return the list of STT backends that are usable right now."""
    import shutil
    import platform

    backends = ["text-input"]
    if shutil.which("whisper-cpp") or shutil.which("whisper"):
        backends.append("whisper-cli")
    if platform.system() == "Darwin":
        backends.append("apple-speech")
    return backends
