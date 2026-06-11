"""Configuration management."""

import json
import os
import shutil
import unicodedata
import uuid
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
    "VOICE_TTS_BACKEND",
    "VOICE_REPLY_STYLE",
    "VOICE_TTS_SUMMARY_MAX_CHARS",
    "VOICE_CLAUDE_WORKDIR",
    "VOICE_CLAUDE_TIMEOUT_SECONDS",
    "VOICE_CONVERSATION_MODE",
    "VOICE_CLAUDE_SESSION_ID",
    "WHISPER_CPP_MODEL",
    "WHISPER_CPP_LANGUAGE",
    "VOLCENGINE_ASR_API_KEY",
    "VOLCENGINE_ASR_APP_ID",
    "VOLCENGINE_ASR_ACCESS_TOKEN",
    "VOLCENGINE_ASR_RESOURCE_ID",
    "VOLCENGINE_ASR_CLUSTER",
    "VOLCENGINE_ASR_LANGUAGE",
    "VOLCENGINE_ASR_ENDPOINT",
    "VOLCENGINE_TTS_API_KEY",
    "VOLCENGINE_TTS_RESOURCE_ID",
    "VOLCENGINE_TTS_VOICE_TYPE",
    "VOLCENGINE_TTS_AUDIO_FORMAT",
    "VOLCENGINE_TTS_ENDPOINT",
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


def _mojibake_key(text: str) -> str:
    """Normalize mojibake-looking text for loose path-name matching."""
    return "".join(
        ch
        for ch in text.replace("Â", "")
        if not unicodedata.category(ch).startswith("C")
    )


def _latin1_mojibake_variants(text: str) -> set[str]:
    """Return common variants produced by repeatedly decoding UTF-8 as Latin-1."""
    variants: set[str] = set()
    current = text
    for _ in range(3):
        try:
            current = current.encode("utf-8").decode("latin1")
        except UnicodeError:
            break
        variants.add(current)
    return variants


def _repair_mojibake_path(path: Path) -> Path:
    """Repair a configured path if only the final directory name is mojibake.

    This is intentionally conservative: it only runs when the configured path
    does not exist and only substitutes a real sibling directory whose name
    matches a Latin-1 mojibake variant of the requested final component.
    """
    if path.exists():
        return path

    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        return path

    target_key = _mojibake_key(path.name)
    for candidate in parent.iterdir():
        if not candidate.is_dir():
            continue
        for variant in _latin1_mojibake_variants(candidate.name):
            if _mojibake_key(variant) == target_key:
                return candidate
    return path


def get_claude_workdir() -> Path:
    """Resolve the working directory used for Claude CLI subprocesses."""
    path = Path(get_config_value("VOICE_CLAUDE_WORKDIR", str(get_project_root()))).expanduser()
    return _repair_mojibake_path(path)


DEFAULT_TIMEOUT_SECONDS = 300


def get_claude_timeout() -> int:
    """Resolve Claude CLI timeout: VOICE_CLAUDE_TIMEOUT_SECONDS → config.json → default 300.

    Rejects values below 10 seconds (falls back to default).
    """
    raw = get_config_value("VOICE_CLAUDE_TIMEOUT_SECONDS", "")
    if raw:
        try:
            val = int(raw)
            if val >= 10:
                return val
        except ValueError:
            pass
    return DEFAULT_TIMEOUT_SECONDS


# ── Phase 25: Conversation mode ─────────────────────────


def get_conversation_mode() -> bool:
    """Resolve VOICE_CONVERSATION_MODE with env > config.json > default (true)."""
    raw = get_config_value("VOICE_CONVERSATION_MODE", "true")
    return raw.lower() in ("true", "1", "yes", "on")


def get_claude_session_id(create_if_missing: bool = True) -> str:
    """Return the Claude conversation session UUID.

    Priority: env var > config.json > auto-generate and persist.
    When VOICE_CONVERSATION_MODE is false, returns empty string.
    """
    if not get_conversation_mode():
        return ""

    sid = get_config_value("VOICE_CLAUDE_SESSION_ID", "")
    if sid:
        return sid
    if not create_if_missing:
        return ""
    return _generate_and_persist_session_id()


def _generate_and_persist_session_id() -> str:
    """Generate a new UUID, persist it to config.json, and return it."""
    import json as _json

    sid = str(uuid.uuid4())
    cfg_path = get_config_path()
    cfg = load_config()
    cfg["VOICE_CLAUDE_SESSION_ID"] = sid
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cfg_path.with_name(f"{cfg_path.name}.tmp")
    tmp_path.write_text(_json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(cfg_path)
    return sid


def new_conversation_session() -> str:
    """Generate a new session UUID, persist, and return it. Replaces any existing ID."""
    import json as _json

    sid = str(uuid.uuid4())
    cfg_path = get_config_path()
    cfg = load_config()
    cfg["VOICE_CLAUDE_SESSION_ID"] = sid
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = cfg_path.with_name(f"{cfg_path.name}.tmp")
    tmp_path.write_text(_json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(cfg_path)
    return sid


def is_valid_uuid(s: str) -> bool:
    """Return True if s is a valid UUID string."""
    if not s:
        return False
    try:
        uuid.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False
