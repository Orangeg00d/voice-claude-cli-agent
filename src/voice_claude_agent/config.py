"""Configuration management."""

import json
import os
import shutil
from pathlib import Path


def get_project_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def get_agent_state_dir() -> Path:
    state_dir = os.environ.get("VOICE_CLAUDE_AGENT_STATE_DIR")
    if state_dir:
        return Path(state_dir).expanduser()
    return get_project_root() / "agent_state"


def get_sessions_log_path() -> Path:
    return get_agent_state_dir() / "sessions.jsonl"


def get_last_result_path() -> Path:
    return get_agent_state_dir() / "last_result.json"


def get_app_events_log_path() -> Path:
    return get_agent_state_dir() / "app_events.jsonl"


def find_claude_executable() -> str | None:
    return shutil.which("claude")


def _portaudio_failure_detail(error: Exception) -> str:
    return (
        f"PortAudio unavailable — sounddevice library load failed: {error}. "
        "If this happens inside the .app bundle, rebuild with python setup.py py2app "
        "so libportaudio.dylib is extracted to the real filesystem."
    )


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
    except ImportError as e:
        msg = str(e).lower()
        if "_sounddevice_data" in msg or "libportaudio" in msg or "portaudio" in msg:
            return False, _portaudio_failure_detail(e)
        return False, "sounddevice not installed — cannot verify microphone"
    except Exception as e:
        msg = str(e).lower()
        if "permission" in msg or "not authorized" in msg or "input device" in msg:
            return False, f"Microphone permission denied: {e}"
        # Detect PortAudio dynamic library load failures
        if "cannot load library" in msg and "libportaudio" in msg:
            return False, _portaudio_failure_detail(e)
        if "portaudio" in msg:
            return False, _portaudio_failure_detail(e)
        return True, f"Microphone check passed with warning: {e}"


def check_apple_speech_available() -> bool:
    """Check if macOS NSSpeechRecognizer is available."""
    return shutil.which("say") is not None


CONFIG_KEYS = {
    "VOICE_RECORD_SECONDS",
    "VOICE_STT_BACKEND",
    "VOICE_CLAUDE_WORKDIR",
    "WHISPER_CPP_MODEL",
    "WHISPER_CPP_LANGUAGE",
    "VOLCENGINE_ASR_API_KEY",
    "VOLCENGINE_ASR_APP_ID",
    "VOLCENGINE_ASR_ACCESS_TOKEN",
    "VOLCENGINE_ASR_RESOURCE_ID",
    "VOLCENGINE_ASR_CLUSTER",
    "VOLCENGINE_ASR_LANGUAGE",
    "VOLCENGINE_ASR_ENDPOINT",
}


# ── F069: Mask credential values for safe display ──────────

def mask_credential(value: str) -> str:
    """Mask a credential value, showing only first 4 and last 4 characters."""
    if not value:
        return "(not set)"
    if len(value) <= 6:
        return "*" * len(value)
    return value[:4] + "*" * max(0, len(value) - 8) + value[-4:]

def get_config_path() -> Path:
    return Path.home() / ".voice-claude-agent" / "config.json"


def load_config() -> dict:
    """Load settings from ~/.voice-claude-agent/config.json.

    Returns a dict of recognized keys, or empty dict on any failure.
    Priority: environment variable > config.json > default.
    """
    config_path = get_config_path()
    if not config_path.exists():
        return {}
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
    except Exception:
        return {}

    return {k: v for k, v in data.items() if k in CONFIG_KEYS and v is not None}


def get_config_value(key: str, default: str = "") -> str:
    """Resolve one config value with env var > config.json > default priority."""
    env_value = os.environ.get(key, "").strip()
    if env_value:
        return env_value

    value = load_config().get(key)
    if value is None:
        return default
    return str(value).strip() or default


def get_claude_workdir() -> Path:
    """Resolve the working directory used for Claude CLI subprocesses."""
    return Path(get_config_value("VOICE_CLAUDE_WORKDIR", str(get_project_root()))).expanduser()


DEFAULT_TIMEOUT_SECONDS = 300
