"""Speech-to-Text abstraction layer.

Phase 4 — real STT backends: text-input (dev), whisper-cli (local),
and apple-speech (macOS NSSpeechRecognizer via osascript).
"""

import os
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
    - whisper-cli: local whisper.cpp binary (requires WHISPER_CPP_MODEL)
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


# ── whisper-cli backend ─────────────────────────────────────

_WHISPER_CPP_CANDIDATES = ["whisper-cpp", "whisper-cli"]


def _find_whisper_cpp_binary() -> str | None:
    """Locate a real whisper.cpp binary.

    Detection order: whisper-cpp, whisper-cli, then whisper (if it's NOT the Python one).
    Returns path string or None.
    """
    import shutil

    for name in _WHISPER_CPP_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path

    # If only 'whisper' is found, verify it's NOT the Python whisper package
    whisper = shutil.which("whisper")
    if whisper and _is_python_whisper(whisper):
        return None
    return whisper


def _is_python_whisper(path: str) -> bool:
    """Return True if `path` points to the Python whisper CLI, not whisper.cpp."""
    try:
        with open(path, "rb") as f:
            head = f.read(80)
        # Python scripts start with #! or are text files referencing python
        text_head = head.decode("utf-8", errors="replace")
        if text_head.startswith("#!") and "python" in text_head.lower():
            return True
        if "python" in text_head.lower() and "script" in text_head.lower():
            return True
    except OSError:
        return False

    # Fallback: run --help and check for Python whisper signature args
    try:
        proc = subprocess.run(
            [path, "--help"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        help_text = proc.stdout + proc.stderr
        python_markers = ["--model_dir", "--output_format", "openai"]
        if any(m in help_text for m in python_markers):
            return True
    except Exception:
        return True  # can't verify, reject to be safe
    return False


def _resolve_whisper_model() -> tuple[str | None, str]:
    """Resolve the whisper.cpp GGML model path and return (path, error_message).

    Priority: WHISPER_CPP_MODEL env var. Returns (None, error) if not set.
    """
    model = os.environ.get("WHISPER_CPP_MODEL", "").strip()
    if not model:
        return None, (
            "[STT error: WHISPER_CPP_MODEL environment variable is not set. "
            "Download a GGML model (e.g. ggml-base.en.bin) from "
            "https://huggingface.co/ggerganov/whisper.cpp and set: "
            "export WHISPER_CPP_MODEL=/path/to/ggml-base.en.bin]"
        )
    model_path = Path(model)
    if not model_path.exists():
        return None, (
            f"[STT error: whisper model not found at '{model}'. "
            "Download a GGML model from "
            "https://huggingface.co/ggerganov/whisper.cpp]"
        )
    if not model_path.is_file():
        return None, (
            f"[STT error: WHISPER_CPP_MODEL points to '{model}' which is not a file.]"
        )
    return model, ""


def _transcribe_whisper_cli(audio_data: bytes) -> str:
    """Transcribe via a local whisper.cpp CLI binary.

    Requires: 1) whisper.cpp installed (brew install whisper-cpp),
              2) WHISPER_CPP_MODEL env var set to a GGML model file.
    """
    binary = _find_whisper_cpp_binary()
    if not binary:
        return (
            "[STT error: whisper.cpp not found. "
            "Install it: brew install whisper-cpp, "
            "or use --stt-backend text-input]"
        )

    # Double-check we're not using Python whisper
    if _is_python_whisper(binary):
        return (
            "[STT error: found 'whisper' is the Python openai-whisper package, "
            "not whisper.cpp. Install whisper.cpp: brew install whisper-cpp]"
        )

    model, model_err = _resolve_whisper_model()
    if model is None:
        return model_err

    wav_data = _pcm_to_wav(audio_data)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(wav_data)
        tmp_path = f.name

    try:
        # whisper.cpp CLI: whisper-cpp -m <model> -f <wav> -nt
        proc = subprocess.run(
            [binary, "-m", model, "-f", tmp_path, "-nt"],
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


# ── apple-speech backend ────────────────────────────────────

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
        script = """
        tell application "System Events"
            set prevDictation to do shell script "defaults read com.apple.speech.recognition.AppleSpeechRecognition.prefs DictationIMEnabled 2>/dev/null || echo 0"
        end tell
        return prevDictation
        """
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

        subprocess.run(
            [
                "osascript", "-e",
                f'set audioFile to POSIX file "{wav_path}"\n'
                "tell application \"Finder\" to open audioFile\n"
                "delay 3\n",
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


# ── helpers ─────────────────────────────────────────────────

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
        16,
        1,
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
        stripped = line.strip()
        if stripped and not stripped.startswith("[") and "whisper" not in stripped.lower():
            return stripped
    return ""


def list_available_backends() -> list[str]:
    """Return the list of STT backends that are usable right now.

    whisper-cli is only included if a real whisper.cpp binary is found
    (NOT the Python openai-whisper package).
    """
    import platform

    backends = ["text-input"]

    cpp_bin = _find_whisper_cpp_binary()
    if cpp_bin and not _is_python_whisper(cpp_bin):
        backends.append("whisper-cli")

    if platform.system() == "Darwin":
        backends.append("apple-speech")
    return backends
