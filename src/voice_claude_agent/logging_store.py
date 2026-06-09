"""JSONL session logger.

Writes one JSON line per user command execution to agent_state/sessions.jsonl.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

from voice_claude_agent.config import get_sessions_log_path, get_last_result_path, get_app_events_log_path


def _tz_now() -> str:
    tz = timezone(timedelta(hours=8))
    return datetime.now(tz).isoformat(timespec="seconds")


def write_session(entry: dict) -> Path:
    """Append a session entry to the JSONL log."""
    log_path = get_sessions_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "timestamp": _tz_now(),
        "input_mode": entry.get("input_mode", "text"),
        "transcript": entry.get("transcript", ""),
        "classified_intent": entry.get("classified_intent", "run_claude_task"),
        "risk_level": entry.get("risk_level", "read_only"),
        "confirmation_required": entry.get("confirmation_required", False),
        "confirmation_received": entry.get("confirmation_received", False),
        "claude_command": entry.get("claude_command", []),
        "claude_cwd": entry.get("claude_cwd", ""),
        "exit_code": entry.get("exit_code", -1),
        "timed_out": entry.get("timed_out", False),
        "cancelled": entry.get("cancelled", False),
        "tts_cancelled": entry.get("tts_cancelled", False),
        "claude_stdout": entry.get("claude_stdout", ""),
        "claude_stderr": entry.get("claude_stderr", ""),
        "summary": entry.get("summary", ""),
        "spoken_summary": entry.get("spoken_summary", entry.get("summary", "")),
        "spoken": entry.get("spoken", False),
        "reply_style": entry.get("reply_style", "normal"),
        "stt_backend": entry.get("stt_backend", ""),
        "tts_backend": entry.get("tts_backend", ""),
        "tts_voice_type": entry.get("tts_voice_type", ""),
        "tts_resource_id": entry.get("tts_resource_id", ""),
        "tts_duration_seconds": entry.get("tts_duration_seconds"),
        "tts_fallback_used": entry.get("tts_fallback_used", False),
        "tts_fallback_reason": entry.get("tts_fallback_reason", ""),
        "tts_fallback_detail": entry.get("tts_fallback_detail", ""),
    }

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return log_path


def write_last_result(result: dict) -> Path:
    """Write the last execution result for quick access."""
    result_path = get_last_result_path()
    result_path.parent.mkdir(parents=True, exist_ok=True)

    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return result_path


def write_app_event(event_type: str, **fields) -> Path:
    """Append a structured app-event entry to agent_state/app_events.jsonl.

    Used for F044: trace trigger, recording, STT, Claude, TTS lifecycle.
    """
    record = {
        "timestamp": _tz_now(),
        "event": event_type,
    }
    record.update(fields)

    log_path = get_app_events_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return log_path
