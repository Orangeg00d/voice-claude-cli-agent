"""Configuration management."""

import shutil
from pathlib import Path


def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def get_agent_state_dir() -> Path:
    return get_project_root() / "agent_state"


def get_sessions_log_path() -> Path:
    return get_agent_state_dir() / "sessions.jsonl"


def get_last_result_path() -> Path:
    return get_agent_state_dir() / "last_result.json"


def find_claude_executable() -> str | None:
    return shutil.which("claude")


def check_mic_permission() -> tuple[bool, str]:
    """Check if macOS microphone permission has been granted.

    Returns (has_permission, detail_message).
    On non-macOS platforms, always returns (True, '').
    """
    import platform

    if platform.system() != "Darwin":
        return True, ""

    # Try opening a sounddevice InputStream briefly — this will raise
    # PortAudioError with a clear message if permission is denied.
    try:
        import sounddevice as sd
        import numpy as np

        stream = sd.InputStream(
            samplerate=16000,
            channels=1,
            dtype=np.float32,
        )
        stream.start()
        stream.stop()
        stream.close()
        return True, "microphone accessible"
    except ImportError:
        return False, "sounddevice not installed — cannot verify microphone"
    except Exception as e:
        msg = str(e).lower()
        if "permission" in msg or "not authorized" in msg or "input device" in msg:
            return False, f"Microphone permission denied: {e}"
        return True, f"Microphone check passed with warning: {e}"


def check_apple_speech_available() -> bool:
    """Check if macOS NSSpeechRecognizer is available."""
    return shutil.which("say") is not None


DEFAULT_TIMEOUT_SECONDS = 300
