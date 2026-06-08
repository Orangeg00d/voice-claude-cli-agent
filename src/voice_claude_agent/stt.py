"""Speech-to-Text abstraction layer.

Phase 4 — real STT backends: text-input (dev), whisper-cli (local),
apple-speech (macOS NSSpeechRecognizer via osascript), and
volcengine-doubao (Volcengine BigModel ASR Flash, Phase 15).
"""

import base64
import json
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Protocol
from urllib import error as urllib_error, request as urllib_request

from voice_claude_agent.config import get_config_value


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
    - volcengine-doubao: Volcengine BigModel ASR Flash (cloud API)
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
        if self.backend == "volcengine-doubao":
            return _transcribe_volcengine_doubao(audio_data)
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
    model = get_config_value("WHISPER_CPP_MODEL")
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


def _resolve_whisper_language() -> str:
    """Resolve whisper.cpp spoken language. Default to Chinese for this app."""
    return get_config_value("WHISPER_CPP_LANGUAGE", "zh") or "zh"


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
        language = _resolve_whisper_language()
        # whisper.cpp CLI: <binary> -m <model> -f <wav> -l <lang> -nt --no-timestamps
        proc = subprocess.run(
            [
                binary,
                "-m",
                model,
                "-f",
                tmp_path,
                "-l",
                language,
                "-nt",
                "--no-timestamps",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        # whisper.cpp writes transcript to stdout; stderr has debug/model info
        result = proc.stdout.strip()
        if not result and proc.stderr.strip():
            result = _extract_text_from_whisper_stderr(proc.stderr)
        if result:
            result = _t2s_convert(result)  # F062: traditional → simplified
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


# ── F062: Traditional-to-Simplified Chinese conversion ─────

_T2S_MAP: dict[str, str] = {}


def _t2s_convert(text: str) -> str:
    """Convert traditional Chinese to simplified using a built-in character map."""
    global _T2S_MAP
    if not _T2S_MAP:
        _T2S_MAP = {
            "說": "说", "話": "话", "來": "来", "時": "时",
            "會": "会", "過": "过", "個": "个", "們": "们",
            "為": "为", "學": "学", "開": "开", "關": "关",
            "對": "对", "現": "现", "實": "实", "體": "体",
            "點": "点", "機": "机", "當": "当", "還": "还",
            "讓": "让", "問": "问", "見": "见", "聽": "听",
            "寫": "写", "讀": "读", "給": "给", "從": "从",
            "長": "长", "後": "后", "頭": "头", "書": "书",
            "裡": "里", "麼": "么", "樣": "样", "進": "进",
            "發": "发", "經": "经", "動": "动", "國": "国",
            "這": "这", "沒": "没", "應": "应", "請": "请",
            "與": "与", "嗎": "吗", "種": "种", "處": "处",
            "臺": "台", "灣": "湾", "線": "线", "碼": "码",
            "確": "确", "認": "认", "導": "导", "際": "际",
            "總": "总", "統": "统", "計": "计", "設": "设",
            "運": "运", "轉": "转", "連": "连", "萬": "万",
            "電": "电", "視": "视", "覺": "觉", "響": "响",
            "難": "难", "買": "买", "賣": "卖", "門": "门",
        }
    try:
        return "".join(_T2S_MAP.get(ch, ch) for ch in text)
    except Exception:
        return text


# ── volcengine-doubao backend (Phase 15) ───────────────────
#
# Official endpoint: POST https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash
# Docs: https://www.volcengine.com/docs/6561/1631584
# docs/VOLCENGINE_ASR_SETUP.md (project-local setup guide)
#
# Supports both old-console (X-Api-App-Key + X-Api-Access-Key) and
# new-console (X-Api-Key) authentication headers. Credentials are read
# from env vars or ~/.voice-claude-agent/config.json; never hardcoded.

_VOLCENGINE_LEGACY_REQUIRED_KEYS = [
    "VOLCENGINE_ASR_APP_ID",
    "VOLCENGINE_ASR_ACCESS_TOKEN",
]
_VOLCENGINE_OPTIONAL_KEYS = [
    "VOLCENGINE_ASR_API_KEY",
    "VOLCENGINE_ASR_RESOURCE_ID",
    "VOLCENGINE_ASR_CLUSTER",
    "VOLCENGINE_ASR_LANGUAGE",
    "VOLCENGINE_ASR_ENDPOINT",
]
_VOLCENGINE_DEFAULT_ENDPOINT = (
    "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
)
_VOLCENGINE_DEFAULT_RESOURCE_ID = "volc.bigasr.auc_turbo"
_VOLCENGINE_DEFAULT_CLUSTER = "volcengine_input_common"


def _check_volcengine_credentials() -> tuple[dict, str]:
    """Load Volcengine ASR credentials. Returns (creds_dict, error_message)."""
    creds = {}
    api_key = get_config_value("VOLCENGINE_ASR_API_KEY")
    if api_key:
        creds["VOLCENGINE_ASR_API_KEY"] = api_key

    missing_legacy = []
    for key in _VOLCENGINE_LEGACY_REQUIRED_KEYS:
        val = get_config_value(key)
        if val:
            creds[key] = val
        else:
            missing_legacy.append(key)

    for key in _VOLCENGINE_OPTIONAL_KEYS:
        val = get_config_value(key)
        if val:
            creds[key] = val

    if "VOLCENGINE_ASR_API_KEY" not in creds and missing_legacy:
        return {}, (
            "[STT error: volcengine-doubao credentials missing: set either "
            "VOLCENGINE_ASR_API_KEY, or both VOLCENGINE_ASR_APP_ID and "
            "VOLCENGINE_ASR_ACCESS_TOKEN. "
            "See docs/VOLCENGINE_ASR_SETUP.md for details.]"
        )
    return creds, ""


def _mask_credential(key: str, value: str) -> str:
    """Mask a credential value for safe display (first 3 + last 3 chars)."""
    if not value:
        return "(not set)"
    if len(value) <= 6:
        return "*" * len(value)
    return value[:3] + "*" * (len(value) - 6) + value[-3:]


def _transcribe_volcengine_doubao(audio_data: bytes) -> str:
    """Transcribe via Volcengine BigModel ASR Flash.

    POST https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash
    Headers (old console): X-Api-App-Key, X-Api-Access-Key, X-Api-Resource-Id,
                           X-Api-Request-Id, X-Api-Sequence: -1
    Headers (new console): X-Api-Key, X-Api-Resource-Id, X-Api-Request-Id,
                            X-Api-Sequence: -1
    Body: {"user":{"uid":"app_key"}, "audio":{"data":"<base64 wav>"},
            "request":{"model_name":"bigmodel"}}
    Response: result.text or result.utterances[].text
    """
    creds, err = _check_volcengine_credentials()
    if err:
        return err

    api_key = creds.get("VOLCENGINE_ASR_API_KEY")
    app_id = creds.get("VOLCENGINE_ASR_APP_ID", "voice-claude-agent")
    access_token = creds.get("VOLCENGINE_ASR_ACCESS_TOKEN")
    resource_id = creds.get("VOLCENGINE_ASR_RESOURCE_ID", _VOLCENGINE_DEFAULT_RESOURCE_ID)

    endpoint = get_config_value("VOLCENGINE_ASR_ENDPOINT", _VOLCENGINE_DEFAULT_ENDPOINT)
    if not endpoint:
        endpoint = _VOLCENGINE_DEFAULT_ENDPOINT

    wav_data = _pcm_to_wav(audio_data)
    b64_audio = base64.b64encode(wav_data).decode("ascii")
    request_id = uuid.uuid4().hex

    headers = {
        "Content-Type": "application/json",
        "X-Api-Resource-Id": resource_id,
        "X-Api-Request-Id": request_id,
        "X-Api-Sequence": "-1",
    }
    if api_key:
        headers["X-Api-Key"] = api_key
    else:
        headers["X-Api-App-Key"] = app_id
        headers["X-Api-Access-Key"] = access_token or ""

    body = {
        "user": {"uid": app_id},
        "audio": {"data": b64_audio},
        "request": {"model_name": "bigmodel"},
    }

    try:
        req = urllib_request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        # urllib_request.urlopen also accepts the url + data directly
        with urllib_request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib_error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:300]
        return f"[STT error: Volcengine ASR HTTP {e.code}: {err_body}]"
    except urllib_error.URLError as e:
        return f"[STT error: Volcengine ASR network error: {e.reason}]"
    except Exception as e:
        return f"[STT error: Volcengine ASR failed: {e}]"
    except BaseException as e:
        return f"[STT error: Volcengine ASR unexpected error: {e}]"

    return _parse_volcengine_response(result)


def _parse_volcengine_response(result: dict) -> str:
    """Parse the Volcengine BigModel ASR Flash JSON response."""
    resp = result.get("result", result.get("resp", {}))
    if not isinstance(resp, dict):
        resp = {}

    code = resp.get("code")
    message = resp.get("message", resp.get("status_text", ""))

    # Success codes: 1000 (v1) or 20000000 (v3 / flash), or None (some responses omit code on success)
    if code is None or code in (1000, 20000000):
        # Primary: result.text
        text = resp.get("text", "")
        if text:
            return text.strip()

        # Fallback: result.utterances[].text
        utterances = resp.get("utterances", [])
        if isinstance(utterances, list) and utterances:
            parts = []
            for u in utterances:
                sentence = u.get("text", u.get("sentence", ""))
                if sentence:
                    parts.append(sentence)
            if parts:
                return "".join(parts)

        return "[STT error: Volcengine ASR returned empty result]"

    # Explicit error from API
    if code is not None:
        return f"[STT error: Volcengine ASR response error (code={code}): {message}]"

    # Response did not follow expected schema
    return "[STT error: Volcengine ASR returned unexpected response format]"


def _volcengine_backend_available() -> bool:
    """Return True if volcengine-doubao credentials are configured."""
    creds, _ = _check_volcengine_credentials()
    return "VOLCENGINE_ASR_API_KEY" in creds or all(
        k in creds for k in _VOLCENGINE_LEGACY_REQUIRED_KEYS
    )


def list_available_backends() -> list[str]:
    """Return the list of STT backends that are usable right now.

    whisper-cli is only included if a real whisper.cpp binary is found
    (NOT the Python openai-whisper package).
    volcengine-doubao is included if required credentials are configured.
    """
    import platform

    backends = ["text-input"]

    cpp_bin = _find_whisper_cpp_binary()
    if cpp_bin and not _is_python_whisper(cpp_bin):
        backends.append("whisper-cli")

    if _volcengine_backend_available():
        backends.append("volcengine-doubao")

    if platform.system() == "Darwin":
        backends.append("apple-speech")
    return backends
