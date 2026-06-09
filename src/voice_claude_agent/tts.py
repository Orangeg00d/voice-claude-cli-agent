"""Text-to-Speech abstraction layer.

Backends:
- MacOSSaySpeaker: macOS `say` command (default fallback).
- VolcengineDoubaoSpeaker: Volcengine BigModel TTS HTTP streaming API (Phase 15).
- FakeSpeaker: records calls for testing.
"""

import json
import os
import threading
import tempfile
import urllib.request
import uuid
from pathlib import Path
from typing import Protocol

from voice_claude_agent.config import get_config_value

# ── Global TTS process tracking for cancellation ───────────
_tts_pids: list[int] = []
_tts_lock = threading.Lock()


def _register_tts_pid(pid: int) -> None:
    with _tts_lock:
        _tts_pids.append(pid)


def _unregister_tts_pid(pid: int) -> None:
    with _tts_lock:
        try:
            _tts_pids.remove(pid)
        except ValueError:
            pass


def cancel_all_tts() -> int:
    """Kill all running TTS processes. Returns number of processes killed."""
    with _tts_lock:
        pids = list(_tts_pids)
        _tts_pids.clear()
    if pids:
        mark_tts_cancelled()
    killed = 0
    for pid in pids:
        try:
            os.kill(pid, 15)  # SIGTERM
            killed += 1
        except Exception:
            pass
    return killed


def is_tts_speaking() -> bool:
    with _tts_lock:
        return len(_tts_pids) > 0


# ── TTS cancellation state (F080) ──────────────────────────
_tts_cancelled = False


def mark_tts_cancelled() -> None:
    global _tts_cancelled
    _tts_cancelled = True


def is_tts_cancelled() -> bool:
    return _tts_cancelled


def reset_tts_cancelled() -> None:
    global _tts_cancelled
    _tts_cancelled = False


class Speaker(Protocol):
    """Abstract TTS interface. Implementations must provide speak()."""

    def speak(self, text: str) -> None: ...


class FakeSpeaker:
    """Fake speaker for testing. Records spoken text."""

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def speak(self, text: str) -> None:
        self.spoken.append(text)


class MacOSSaySpeaker:
    """TTS using macOS `say` command."""

    def speak(self, text: str) -> None:
        import subprocess as _subprocess

        safe_text = text.replace('"', '\\"')
        try:
            proc = _subprocess.Popen(
                ["say", safe_text],
                stdout=_subprocess.DEVNULL,
                stderr=_subprocess.DEVNULL,
            )
            _register_tts_pid(proc.pid)
            try:
                proc.wait(timeout=30)
            except _subprocess.TimeoutExpired:
                pass
            finally:
                _unregister_tts_pid(proc.pid)
                if proc.poll() is None:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
        except FileNotFoundError:
            pass


# ── Volcengine/Doubao TTS backend (Phase 15, F071) ────────────────
#
# Official endpoint: POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
# Docs: https://www.volcengine.com/docs/6561/1598757
# project-local: docs/VOLCENGINE_TTS_SETUP.md

_VOLCENGINE_TTS_DEFAULT_ENDPOINT = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
# moon_bigtts speakers belong to Seed TTS 1.0. Using seed-tts-2.0 with this
# default voice returns code=55000000 resource mismatch.
_VOLCENGINE_TTS_DEFAULT_RESOURCE_ID = "seed-tts-1.0"
_VOLCENGINE_TTS_DEFAULT_VOICE_TYPE = "zh_female_shuangkuaisisi_moon_bigtts"
_VOLCENGINE_TTS_DEFAULT_AUDIO_FORMAT = "mp3"


def _resolve_tts_backend() -> str:
    """Return the active TTS backend name: macos-say or volcengine-doubao."""
    return get_config_value("VOICE_TTS_BACKEND", "macos-say") or "macos-say"


def create_speaker() -> Speaker:
    """Factory: return the configured TTS speaker, falling back to macOS say."""
    backend = _resolve_tts_backend()
    if backend == "volcengine-doubao":
        return VolcengineDoubaoSpeaker()
    return MacOSSaySpeaker()


class VolcengineDoubaoSpeaker:
    """TTS via Volcengine BigModel HTTP streaming API.

    Calls POST /api/v3/tts/unidirectional, collects base64 audio chunks,
    writes to a temporary file, and plays with macOS afplay.
    Falls back to MacOSSaySpeaker on any failure.
    """

    def __init__(self) -> None:
        self._fallback = MacOSSaySpeaker()
        self._fallback_called = False
        self._fallback_reason = ""
        self._fallback_detail = ""

    # ── public API ─────────────────────────────────────────

    def speak(self, text: str) -> None:
        self._fallback_called = False
        self._fallback_reason = ""
        self._fallback_detail = ""
        creds, err = _check_volcengine_tts_credentials()
        if err:
            self._speak_fallback(text, "missing_credentials", err)
            return

        api_key = creds.get("VOLCENGINE_TTS_API_KEY", "")
        resource_id = creds.get("VOLCENGINE_TTS_RESOURCE_ID", _VOLCENGINE_TTS_DEFAULT_RESOURCE_ID)
        voice_type = creds.get("VOLCENGINE_TTS_VOICE_TYPE", _VOLCENGINE_TTS_DEFAULT_VOICE_TYPE)
        audio_format = creds.get("VOLCENGINE_TTS_AUDIO_FORMAT", _VOLCENGINE_TTS_DEFAULT_AUDIO_FORMAT)

        endpoint = get_config_value("VOLCENGINE_TTS_ENDPOINT", _VOLCENGINE_TTS_DEFAULT_ENDPOINT)
        if not endpoint:
            endpoint = _VOLCENGINE_TTS_DEFAULT_ENDPOINT

        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-App-Key": "aGjiRDfUWi",
            "X-Api-Request-Id": uuid.uuid4().hex,
            "Connection": "keep-alive",
        }

        payload = {
            "user": {"uid": "voice-claude-agent"},
            "req_params": {
                "text": text,
                "speaker": voice_type,
                "audio_params": {
                    "format": audio_format,
                    "sample_rate": 24000,
                },
            },
        }

        audio_bytes = b""
        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                for line in resp:
                    line = line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    code = chunk.get("code", -1)
                    if code == 20000000:
                        break
                    if code != 0:
                        self._speak_fallback(text, "api_error", str(chunk))
                        return
                    data_b64 = chunk.get("data", "")
                    if data_b64:
                        import base64
                        audio_bytes += base64.b64decode(data_b64)

        except Exception as e:
            self._speak_fallback(text, "network_or_parse", str(e))
            return

        if not audio_bytes:
            self._speak_fallback(text, "empty_audio", "no audio data returned from Volcengine TTS")
            return

        # Write to temp file and play with afplay
        try:
            suffix = f".{audio_format}"
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
                f.write(audio_bytes)
                tmp_path = Path(f.name)

            import subprocess as _subprocess
            proc = _subprocess.Popen(
                ["afplay", str(tmp_path)],
                stdout=_subprocess.DEVNULL,
                stderr=_subprocess.DEVNULL,
            )
            _register_tts_pid(proc.pid)
            try:
                proc.wait(timeout=60)
            except _subprocess.TimeoutExpired:
                pass
            finally:
                _unregister_tts_pid(proc.pid)
                if proc.poll() is None:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
        except Exception as e:
            self._speak_fallback(text, "playback_error", str(e))
        finally:
            if "tmp_path" in dir() and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _speak_fallback(self, text: str, reason: str, detail: str = "") -> None:
        self._fallback_called = True
        self._fallback_reason = reason
        self._fallback_detail = detail
        _log_tts_fallback(reason, detail)
        self._fallback.speak(text)


# ── credential helpers ────────────────────────────────────

def _volcengine_tts_required_keys() -> list[str]:
    return ["VOLCENGINE_TTS_API_KEY"]


def _check_volcengine_tts_credentials() -> tuple[dict, str]:
    """Load Volcengine TTS credentials. Returns (creds, error)."""
    creds = {}
    missing = []
    for key in _volcengine_tts_required_keys():
        val = get_config_value(key)
        if not val:
            missing.append(key)
        else:
            creds[key] = val

    for key in ("VOLCENGINE_TTS_RESOURCE_ID", "VOLCENGINE_TTS_VOICE_TYPE",
                "VOLCENGINE_TTS_AUDIO_FORMAT", "VOLCENGINE_TTS_ENDPOINT"):
        val = get_config_value(key)
        if val:
            creds[key] = val

    if missing:
        return {}, f"TTS credentials missing: {', '.join(missing)}"
    return creds, ""


def _mask_tts_credential(key: str, value: str) -> str:
    if not value:
        return "(not set)"
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]


def _log_tts_fallback(reason: str, detail: str = "") -> None:
    """Log a TTS fallback event to app_events.jsonl."""
    try:
        from voice_claude_agent.logging_store import write_app_event
        write_app_event("tts_fallback", reason=reason, detail=detail[:200])
    except Exception:
        pass
